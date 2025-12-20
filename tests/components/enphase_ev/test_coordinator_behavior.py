import asyncio
import copy
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev.const import (
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_SCAN_INTERVAL,
    CONF_SERIALS,
    CONF_SITE_ID,
    CONF_SITE_ONLY,
    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
    DOMAIN,
    OPT_NOMINAL_VOLTAGE,
    OPT_SESSION_HISTORY_INTERVAL,
)
from custom_components.enphase_ev.coordinator import (
    FAST_TOGGLE_POLL_HOLD_S,
    ServiceValidationError,
)

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


def _make_coordinator(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev import coordinator as coord_mod

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
        CONF_SITE_ONLY: False,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    return EnphaseCoordinator(hass, cfg)


def _client_response_error(status: int, *, message: str = "", headers=None):
    req = aiohttp.RequestInfo(
        url=aiohttp.client.URL("https://example"),
        method="GET",
        headers={},
        real_url=aiohttp.client.URL("https://example"),
    )
    return aiohttp.ClientResponseError(
        request_info=req,
        history=(),
        status=status,
        message=message,
        headers=headers or {},
    )


@pytest.mark.asyncio
async def test_coordinator_init_normalizes_serials_and_options(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    class BadSerial:
        def __str__(self):
            raise ValueError("boom")

    config = {
        CONF_SITE_ID: "12345",
        CONF_SERIALS: [None, " EV01 ", "", "EV02", "EV01", BadSerial()],
        CONF_EAUTH: "token",
        CONF_COOKIE: "cookie",
        CONF_SCAN_INTERVAL: 30,
    }

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        options={
            OPT_NOMINAL_VOLTAGE: "bad",
            OPT_SESSION_HISTORY_INTERVAL: "not-a-number",
        },
    )
    entry.add_to_hass(hass)

    captured_tasks: list = []
    monkeypatch.setattr(hass, "async_create_task", lambda coro: captured_tasks.append(coro))
    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())

    class DummyClient:
        def __init__(self, *args, **kwargs) -> None:
            self.callbacks: list = []

        def set_reauth_callback(self, cb):
            async def _runner():
                self.callbacks.append(cb)
            return _runner()

    monkeypatch.setattr(coord_mod, "EnphaseEVClient", DummyClient)

    coord = EnphaseCoordinator(hass, config, config_entry=entry)

    assert coord.serials == {"EV01", "EV02"}
    assert coord._serial_order == ["EV01", "EV02"]
    assert coord._configured_serials == {"EV01", "EV02"}
    assert coord._nominal_v == 240
    assert coord._session_history_interval_min == DEFAULT_SESSION_HISTORY_INTERVAL_MIN
    assert coord._session_history_cache_ttl == DEFAULT_SESSION_HISTORY_INTERVAL_MIN * 60
    assert captured_tasks, "set_reauth_callback coroutine should be scheduled"
    await captured_tasks[0]


def test_coordinator_init_handles_single_serial(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    config = {
        CONF_SITE_ID: "78901",
        CONF_SERIALS: " EV42 ",
        CONF_EAUTH: None,
        CONF_COOKIE: None,
        CONF_SCAN_INTERVAL: 60,
        CONF_SITE_ONLY: False,
    }

    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    monkeypatch.setattr(coord_mod, "EnphaseEVClient", lambda *args, **kwargs: SimpleNamespace(set_reauth_callback=lambda *_: None))

    coord = EnphaseCoordinator(hass, config)

    assert coord.serials == {"EV42"}
    assert coord._serial_order == ["EV42"]


@pytest.mark.asyncio
async def test_update_skips_status_when_site_only(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.site_only = True

    client = SimpleNamespace(
        status=AsyncMock(side_effect=AssertionError("should not call status"))
    )
    coord.client = client

    result = await coord._async_update_data()

    assert result == {}
    assert client.status.await_count == 0
    assert coord.last_success_utc is not None
    assert coord._has_successful_refresh is True


@pytest.mark.asyncio
async def test_update_skips_status_when_no_serials(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = set()
    coord._serial_order = []

    client = SimpleNamespace(
        status=AsyncMock(side_effect=AssertionError("should not call status"))
    )
    coord.client = client

    result = await coord._async_update_data()

    assert result == {}
    assert client.status.await_count == 0
    assert coord.last_success_utc is not None
    assert coord._has_successful_refresh is True


@pytest.mark.asyncio
async def test_site_only_clears_issues_and_counters(hass, monkeypatch, mock_issue_registry):
    coord = _make_coordinator(hass, monkeypatch)
    coord.site_only = True
    coord._network_issue_reported = True
    coord._cloud_issue_reported = True
    coord._dns_issue_reported = True
    coord._unauth_errors = 3
    coord._rate_limit_hits = 2
    coord._http_errors = 4
    coord._network_errors = 5
    coord._dns_failures = 6
    coord._last_error = "any error"
    coord.backoff_ends_utc = object()
    coord._backoff_until = 123.0
    cancelled = {"called": False}
    coord._backoff_cancel = lambda: cancelled.__setitem__("called", True)

    await coord._async_update_data()

    assert cancelled["called"] is True
    assert coord._network_issue_reported is False
    assert coord._cloud_issue_reported is False
    assert coord._dns_issue_reported is False
    assert coord._unauth_errors == 0
    assert coord._rate_limit_hits == 0
    assert coord._http_errors == 0
    assert coord._network_errors == 0
    assert coord._dns_failures == 0
    assert coord._last_error is None
    assert coord.backoff_ends_utc is None
    assert coord._backoff_until is None
    assert ("enphase_ev", "cloud_unreachable") in mock_issue_registry.deleted
    assert ("enphase_ev", "cloud_service_unavailable") in mock_issue_registry.deleted
    assert ("enphase_ev", "cloud_dns_resolution") in mock_issue_registry.deleted


@pytest.mark.asyncio
async def test_backoff_on_429(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)

    class StubClient:
        async def status(self):
            raise _client_response_error(429, headers={"Retry-After": "1"})

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None


@pytest.mark.asyncio
async def test_backoff_timer_requests_refresh(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)
    coord.async_request_refresh = AsyncMock()

    captured: dict[str, object] = {}

    def _fake_call_later(hass_obj, delay, cb):
        captured["delay"] = delay
        captured["callback"] = cb

        def _cancel():
            captured["cancelled"] = True

        return _cancel

    monkeypatch.setattr(coord_mod, "async_call_later", _fake_call_later)

    now = datetime(2025, 11, 3, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: now)

    coord._schedule_backoff_timer(2.5)

    assert captured["delay"] == 2.5
    assert coord.backoff_ends_utc == now + timedelta(seconds=2.5)
    assert callable(coord._backoff_cancel)

    await captured["callback"](now + timedelta(seconds=3))

    assert coord.async_request_refresh.await_count == 1
    assert coord.backoff_ends_utc is None
    assert coord._backoff_cancel is None


@pytest.mark.asyncio
async def test_http_error_issue(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import ISSUE_CLOUD_ERRORS
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)

    class FailingClient:
        async def status(self):
            raise _client_response_error(503)

    created = []
    deleted = []
    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda hass, domain, issue_id, **kwargs: created.append(
            (domain, issue_id, kwargs)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        coord_mod.ir,
        "async_delete_issue",
        lambda hass, domain, issue_id: deleted.append((domain, issue_id)),
        raising=False,
    )

    coord.client = FailingClient()

    for _ in range(3):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        coord._backoff_until = None

    matching = [
        kwargs for _, issue_id, kwargs in created if issue_id == ISSUE_CLOUD_ERRORS
    ]
    assert matching
    latest_payload = matching[-1]
    placeholders = latest_payload["translation_placeholders"]
    assert placeholders["site_id"] == coord.site_id
    metrics = latest_payload["data"]["site_metrics"]
    assert metrics["last_error"]

    class SuccessClient:
        async def status(self):
            return {"evChargerData": []}

    coord.client = SuccessClient()
    coord._backoff_until = None
    data = await coord._async_update_data()
    coord.async_set_updated_data(data)

    assert any(issue_id == ISSUE_CLOUD_ERRORS for _, issue_id in deleted)


@pytest.mark.asyncio
async def test_network_issue_includes_metrics(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.const import ISSUE_NETWORK_UNREACHABLE

    coord = _make_coordinator(hass, monkeypatch)
    coord.site_name = "Garage"

    class StubClient:
        async def status(self):
            raise aiohttp.ClientError("connection reset by peer")

    coord.client = StubClient()

    created: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda hass_, domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
        raising=False,
    )

    for _ in range(3):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        coord._backoff_until = None

    issue_map = {issue_id: kwargs for issue_id, kwargs in created}
    assert ISSUE_NETWORK_UNREACHABLE in issue_map
    payload = issue_map[ISSUE_NETWORK_UNREACHABLE]
    placeholders = payload["translation_placeholders"]
    assert placeholders["site_name"] == "Garage"
    metrics = payload["data"]["site_metrics"]
    assert metrics["network_errors"] >= 3


@pytest.mark.asyncio
async def test_dns_issue_includes_metrics(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.const import ISSUE_DNS_RESOLUTION

    coord = _make_coordinator(hass, monkeypatch)

    class StubClient:
        async def status(self):
            raise aiohttp.ClientError("Temporary failure in name resolution")

    coord.client = StubClient()

    created: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda hass_, domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
        raising=False,
    )

    for _ in range(4):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        coord._backoff_until = None

    issue_map = {issue_id: kwargs for issue_id, kwargs in created}
    assert ISSUE_DNS_RESOLUTION in issue_map
    dns_payload = issue_map[ISSUE_DNS_RESOLUTION]
    placeholders = dns_payload["translation_placeholders"]
    assert placeholders["site_id"] == coord.site_id
    metrics = dns_payload["data"]["site_metrics"]
    assert metrics["dns_errors"] >= 2


@pytest.mark.asyncio
async def test_http_error_description_from_json(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)
    payload = '{"error":{"details":[{"description":"Too many requests"}]}}'

    class StubClient:
        async def status(self):
            raise _client_response_error(429, message=payload)

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_status == 429
    assert coord.last_failure_description == "Too many requests"
    assert coord.last_failure_response == payload
    assert coord.last_failure_source == "http"


@pytest.mark.asyncio
async def test_http_error_description_plain_text(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)
    payload = " backend unavailable "

    class StubClient:
        async def status(self):
            raise _client_response_error(500, message=payload)

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_status == 500
    assert coord.last_failure_description == "Internal Server Error"
    assert coord.last_failure_response == payload


@pytest.mark.asyncio
async def test_http_error_description_falls_back_to_status_phrase(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)

    class StubClient:
        async def status(self):
            raise _client_response_error(503, message=" ")

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_status == 503
    assert coord.last_failure_description == "Service Unavailable"
    assert coord.last_failure_response == " "


def test_collect_site_metrics_and_placeholders(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    now = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.site_name = "Garage Site"
    coord.last_success_utc = now
    coord.last_failure_utc = now
    coord.last_failure_status = 503
    coord.last_failure_description = "Service Unavailable"
    coord.last_failure_source = "http"
    coord.last_failure_response = "response"
    coord.latency_ms = 123
    coord._backoff_until = time.monotonic() + 5
    coord.backoff_ends_utc = now
    coord._network_errors = 2
    coord._http_errors = 1
    coord._rate_limit_hits = 1
    coord._dns_failures = 0
    coord._last_error = "unauthorized"
    coord._phase_timings = {"status_s": 0.5}
    coord._session_history_cache_ttl = 300

    metrics = coord.collect_site_metrics()
    assert metrics["site_id"] == coord.site_id
    assert metrics["site_name"] == "Garage Site"
    assert metrics["last_success"] == now.isoformat()
    assert metrics["backoff_active"] is True
    assert metrics["phase_timings"] == {"status_s": 0.5}

    placeholders = coord._issue_translation_placeholders(metrics)
    assert placeholders["site_id"] == coord.site_id
    assert placeholders["site_name"] == "Garage Site"
    assert placeholders["last_error"] == "unauthorized"
    assert placeholders["last_status"] == "503"


@pytest.mark.asyncio
async def test_handle_client_unauthorized_refresh(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)
    coord._attempt_auto_refresh = AsyncMock(return_value=True)
    created: list[tuple[str, dict]] = []
    deleted: list[str] = []

    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda *args, **kwargs: created.append((args[2], kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        coord_mod.ir,
        "async_delete_issue",
        lambda hass_, domain, issue_id: deleted.append(issue_id),
        raising=False,
    )

    result = await coord._handle_client_unauthorized()
    assert result is True
    assert coord._unauth_errors == 0
    assert coord._last_error == "unauthorized"
    assert deleted == ["reauth_required"]
    assert created == []


@pytest.mark.asyncio
async def test_handle_client_unauthorized_failure(monkeypatch, hass):
    from homeassistant.exceptions import ConfigEntryAuthFailed
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)
    coord.site_name = "Garage Site"
    coord.last_failure_status = 401
    coord.last_failure_description = "Unauthorized"
    coord._last_error = "stale"
    coord._attempt_auto_refresh = AsyncMock(return_value=False)
    coord._unauth_errors = 1

    created: list[tuple[str, dict]] = []
    deleted: list[str] = []

    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda hass_, domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        coord_mod.ir,
        "async_delete_issue",
        lambda hass_, domain, issue_id: deleted.append(issue_id),
        raising=False,
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._handle_client_unauthorized()

    assert deleted == []
    assert coord._unauth_errors >= 2
    issue_id, payload = created[-1]
    assert issue_id == "reauth_required"
    placeholders = payload["translation_placeholders"]
    assert placeholders["site_id"] == coord.site_id
    assert placeholders["site_name"] == "Garage Site"
    assert placeholders["last_status"] == "401"
    assert placeholders["last_error"] == "unauthorized"
    metrics = payload["data"]["site_metrics"]
    assert metrics["site_name"] == "Garage Site"
    assert metrics["last_error"] == "unauthorized"


@pytest.mark.asyncio
async def test_async_start_stop_trigger_paths(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {RANDOM_SERIAL: {"charging_level": 18, "plugged": True}}
    coord.last_set_amps = {}

    coord.require_plugged = MagicMock()
    coord.set_last_set_amps = MagicMock()
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.async_request_refresh = AsyncMock()

    coordinator_data = {RANDOM_SERIAL: {"plugged": False, "charging_level": 20}}
    coord.data = coordinator_data

    async def _trigger_message(sn, message):
        return {"sent": message, "serial": sn}

    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value=None),
        trigger_message=AsyncMock(side_effect=_trigger_message),
    )

    await coord.async_start_charging(RANDOM_SERIAL, connector_id=None, fallback_amps=24)
    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 20, 1, include_level=None, strict_preference=False
    )
    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, True)

    coord.client.start_charging.reset_mock()
    coord.client.start_charging.return_value = {"status": "not_ready"}
    result = await coord.async_start_charging(
        RANDOM_SERIAL, requested_amps=10, connector_id=2, allow_unplugged=True
    )
    assert result == {"status": "not_ready"}

    await coord.async_stop_charging(RANDOM_SERIAL, allow_unplugged=False)
    coord.client.stop_charging.assert_awaited_once_with(RANDOM_SERIAL)
    coord.require_plugged.assert_called()

    reply = await coord.async_trigger_ocpp_message(RANDOM_SERIAL, "Status")
    coord.client.trigger_message.assert_awaited_once_with(RANDOM_SERIAL, "Status")
    assert reply["sent"] == "Status"


@pytest.mark.asyncio
async def test_async_start_charging_manual_mode_sends_requested_amps(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {
        RANDOM_SERIAL: {
            "plugged": True,
            "charging_level": 26,
            "charge_mode_pref": "MANUAL_CHARGING",
        }
    }
    coord.last_set_amps = {}
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value=None),
        set_charge_mode=AsyncMock(return_value={"status": "ok"}),
    )
    coord.async_request_refresh = AsyncMock()

    await coord.async_start_charging(RANDOM_SERIAL)

    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 26, 1, include_level=True, strict_preference=True
    )
    coord.client.set_charge_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_start_and_stop_preserve_scheduled_mode(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {
        RANDOM_SERIAL: {
            "plugged": True,
            "charging_level": 18,
            "charge_mode_pref": "SCHEDULED_CHARGING",
        }
    }
    coord.last_set_amps = {}
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value={"status": "ok"}),
        set_charge_mode=AsyncMock(return_value={"status": "ok"}),
    )
    coord.async_request_refresh = AsyncMock()

    await coord.async_start_charging(RANDOM_SERIAL)
    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 18, 1, include_level=True, strict_preference=True
    )
    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "SCHEDULED_CHARGING"
    )

    coord.client.set_charge_mode.reset_mock()
    await coord.async_stop_charging(RANDOM_SERIAL)
    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "SCHEDULED_CHARGING"
    )


@pytest.mark.asyncio
async def test_async_start_charging_green_mode_omits_amp_payload(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {
        RANDOM_SERIAL: {
            "plugged": True,
            "charging_level": 30,
            "charge_mode_pref": "GREEN_CHARGING",
        }
    }
    coord.last_set_amps = {}
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value=None),
        set_charge_mode=AsyncMock(return_value={"status": "ok"}),
    )
    coord.async_request_refresh = AsyncMock()

    await coord.async_start_charging(RANDOM_SERIAL)

    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 30, 1, include_level=False, strict_preference=True
    )
    coord.client.set_charge_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_amp_restart_cancels_existing_task(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    pending = asyncio.Future()
    coord._amp_restart_tasks[RANDOM_SERIAL] = pending

    calls: list[tuple[str, float]] = []

    async def _fake_restart(sn: str, delay: float) -> None:
        calls.append((sn, delay))

    coord._async_restart_after_amp_change = _fake_restart  # type: ignore[assignment]

    tasks: list[asyncio.Task] = []

    def _capture(coro, name=None):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(hass, "async_create_task", _capture)

    coord.schedule_amp_restart(RANDOM_SERIAL, delay=12)

    assert pending.cancelled()
    assert tasks, "restart task should be scheduled"
    await tasks[0]
    assert calls == [(RANDOM_SERIAL, 12)]
    assert RANDOM_SERIAL not in coord._amp_restart_tasks


@pytest.mark.asyncio
async def test_schedule_amp_restart_handles_typeerror(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)

    calls: list[tuple[str, float]] = []

    async def _fake_restart(sn: str, delay: float) -> None:
        calls.append((sn, delay))

    coord._async_restart_after_amp_change = _fake_restart  # type: ignore[assignment]

    tasks: list[asyncio.Task] = []

    def _create_task(coro, name=None):
        if name is not None:
            coro.close()
            raise TypeError("name kw not supported")
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(hass, "async_create_task", _create_task)

    coord.schedule_amp_restart(RANDOM_SERIAL, delay=8)

    assert tasks, "fallback task should be scheduled without a name kwarg"
    await tasks[0]
    assert calls == [(RANDOM_SERIAL, 8)]


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_flow(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock()
    with patch(
        "custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock
    ):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 5)

    coord.async_stop_charging.assert_awaited_once_with(
        RANDOM_SERIAL, hold_seconds=90.0, fast_seconds=60, allow_unplugged=True
    )
    sleep_mock.assert_awaited_once_with(5.0)
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_handles_start_error(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock(side_effect=ServiceValidationError("oops"))

    sleep_mock = AsyncMock()
    with patch(
        "custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock
    ):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 0)

    sleep_mock.assert_not_awaited()
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_handles_stop_error(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock(side_effect=RuntimeError("boom"))
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock()
    with patch(
        "custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock
    ):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 10)

    coord.async_start_charging.assert_not_awaited()
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_invalid_delay_defaults(
    hass, monkeypatch
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock()
    with patch(
        "custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock
    ):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, object())

    coord.async_stop_charging.assert_awaited_once_with(
        RANDOM_SERIAL, hold_seconds=90.0, fast_seconds=60, allow_unplugged=True
    )
    sleep_mock.assert_awaited_once_with(30.0)
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_sleep_error(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock(side_effect=RuntimeError("timer boom"))
    with patch(
        "custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock
    ):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 2)

    coord.async_stop_charging.assert_awaited_once()
    sleep_mock.assert_awaited_once_with(2.0)
    coord.async_start_charging.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_handles_generic_start_error(
    hass, monkeypatch
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock(side_effect=RuntimeError("start boom"))

    sleep_mock = AsyncMock()
    with patch(
        "custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock
    ):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 3)

    coord.async_stop_charging.assert_awaited_once()
    sleep_mock.assert_awaited_once_with(3.0)
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_fast_poll_kicked_on_external_toggle(hass, monkeypatch, load_fixture):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    idle_payload = load_fixture("status_idle.json")
    charging_payload = load_fixture("status_charging.json")

    class StubClient:
        def __init__(self, payload):
            self.payload = payload

        async def status(self):
            return copy.deepcopy(self.payload)

    client = StubClient(idle_payload)
    coord.client = client

    await coord._async_update_data()
    assert fast_windows == []

    client.payload = charging_payload
    await coord._async_update_data()
    assert fast_windows == [FAST_TOGGLE_POLL_HOLD_S]

    await coord._async_update_data()
    assert fast_windows == [FAST_TOGGLE_POLL_HOLD_S]

    client.payload = idle_payload
    await coord._async_update_data()
    assert fast_windows == [FAST_TOGGLE_POLL_HOLD_S, FAST_TOGGLE_POLL_HOLD_S]


@pytest.mark.asyncio
async def test_fast_poll_not_triggered_by_expectation_only(
    hass, monkeypatch, load_fixture
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    idle_payload = load_fixture("status_idle.json")

    class StubClient:
        def __init__(self, payload):
            self.payload = payload

        async def status(self):
            return copy.deepcopy(self.payload)

    client = StubClient(idle_payload)
    coord.client = client

    await coord._async_update_data()
    assert fast_windows == []

    coord.set_charging_expectation(RANDOM_SERIAL, True, hold_for=10)
    await coord._async_update_data()
    assert fast_windows == []


def test_record_actual_charging_clears_none_state(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord._last_actual_charging[RANDOM_SERIAL] = True

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    coord._record_actual_charging(RANDOM_SERIAL, None)

    assert RANDOM_SERIAL not in coord._last_actual_charging
    assert fast_windows == []


def test_record_actual_charging_ignores_repeated_state(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    coord._record_actual_charging(RANDOM_SERIAL, False)
    coord._record_actual_charging(RANDOM_SERIAL, False)

    assert coord._last_actual_charging[RANDOM_SERIAL] is False
    assert fast_windows == []


@pytest.mark.asyncio
async def test_runtime_serial_discovery(hass, monkeypatch, config_entry):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    class DummyClient:
        def __init__(self):
            self._calls = 0

        async def status(self):
            self._calls += 1
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    },
                    {
                        "sn": "NEW123456789",
                        "name": "Workshop EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    },
                ]
            }

        async def summary_v2(self):
            return [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "displayName": "Garage EV",
                    "maxCurrent": 48,
                },
                {
                    "serialNumber": "NEW123456789",
                    "displayName": "Workshop EV",
                    "maxCurrent": 32,
                    "hwVersion": "1.2.3",
                    "swVersion": "5.6.7",
                },
            ]

        async def charge_mode(self, sn: str):
            return None

        async def session_history(self, *args, **kwargs):
            return {"data": {"result": [], "hasMore": False}}

    cfg = dict(config_entry.data)
    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    coord.client = DummyClient()
    await coord.async_refresh()

    assert "NEW123456789" in coord.serials
    assert coord.iter_serials() == [RANDOM_SERIAL, "NEW123456789"]
    assert "NEW123456789" in coord.data
    assert coord.data["NEW123456789"]["display_name"] == "Workshop EV"


@pytest.mark.asyncio
async def test_first_refresh_defers_session_history(hass, monkeypatch, config_entry):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    class DummyClient:
        def __init__(self):
            self.history_calls = 0

        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    }
                ]
            }

        async def summary_v2(self):
            return [{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]

        async def charge_mode(self, sn: str):
            return None

        async def session_history(self, *args, **kwargs):
            self.history_calls += 1
            now = datetime.now(timezone.utc)
            epoch = now.timestamp()
            return {
                "data": {
                    "result": [
                        {
                            "sessionId": "42",
                            "startTime": epoch - 600,
                            "endTime": epoch - 300,
                            "aggEnergyValue": 1.234,
                            "activeChargeTime": 900,
                        }
                    ],
                    "hasMore": False,
                }
            }

    scheduled: list[tuple[tuple[str, ...], datetime]] = []
    original_schedule = coord_mod.EnphaseCoordinator._schedule_session_enrichment

    def capture_schedule(self, serials, day_local):
        scheduled.append((tuple(serials), day_local))
        return original_schedule(self, serials, day_local)

    monkeypatch.setattr(
        coord_mod.EnphaseCoordinator,
        "_schedule_session_enrichment",
        capture_schedule,
        raising=False,
    )

    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    client = DummyClient()
    coord.client = client

    await coord.async_refresh()

    assert client.history_calls == 0
    assert "status_s" in coord.phase_timings
    assert coord.data[RANDOM_SERIAL]["energy_today_sessions"] == []

    assert len(scheduled) == 1
    scheduled_serials, scheduled_day = scheduled[0]
    assert scheduled_serials == (RANDOM_SERIAL,)
    assert isinstance(scheduled_day, datetime)


@pytest.mark.asyncio
async def test_charge_mode_lookup_skipped_when_embedded(
    hass, monkeypatch, config_entry
):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    charge_mode_called = False

    async def fake_resolve(self, serials):
        nonlocal charge_mode_called
        charge_mode_called = True
        return {}

    monkeypatch.setattr(
        coord_mod.EnphaseCoordinator,
        "_async_resolve_charge_modes",
        fake_resolve,
        raising=False,
    )

    class DummyClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "chargeMode": "IMMEDIATE",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    }
                ]
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn: str):
            return "SCHEDULED"

        async def session_history(self, *args, **kwargs):
            return {"data": {"result": [], "hasMore": False}}

    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    coord.client = DummyClient()

    await coord.async_refresh()

    assert not charge_mode_called


@pytest.mark.asyncio
async def test_http_backoff_respects_configured_slow_interval(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {OPT_SLOW_POLL_INTERVAL: 300}

        def async_on_unload(self, _cb):
            return None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *_: 1.0)
    scheduled: dict[str, float | object] = {}

    def fake_call_later(_hass, delay, callback):
        scheduled["delay"] = delay
        scheduled["callback"] = callback

        def _cancel():
            scheduled["cancelled"] = True

        return _cancel

    monkeypatch.setattr(coord_mod, "async_call_later", fake_call_later)

    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubRespErr(aiohttp.ClientResponseError):
        def __init__(self):
            req = aiohttp.RequestInfo(
                url=aiohttp.client.URL("https://example"),
                method="GET",
                headers={},
                real_url=aiohttp.client.URL("https://example"),
            )
            super().__init__(
                request_info=req,
                history=(),
                status=503,
                message="",
                headers={},
            )

    class FailingClient:
        async def status(self):
            raise StubRespErr()

    coord.client = FailingClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None
    remaining = coord._backoff_until - time.monotonic()
    assert remaining >= 295
    assert coord._backoff_cancel is not None
    assert scheduled["delay"] >= 300


@pytest.mark.asyncio
async def test_network_backoff_respects_slow_interval(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {OPT_SLOW_POLL_INTERVAL: 200}

        def async_on_unload(self, _cb):
            return None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *_: 1.0)
    scheduled: dict[str, float | object] = {}

    def fake_call_later(_hass, delay, callback):
        scheduled["delay"] = delay
        scheduled["callback"] = callback

        def _cancel():
            scheduled["cancelled"] = True

        return _cancel

    monkeypatch.setattr(coord_mod, "async_call_later", fake_call_later)

    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class FailingClient:
        async def status(self):
            raise aiohttp.ClientError()

    coord.client = FailingClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None
    remaining = coord._backoff_until - time.monotonic()
    assert remaining >= 195
    assert coord._backoff_cancel is not None
    assert scheduled["delay"] >= 200


@pytest.mark.asyncio
async def test_dynamic_poll_switch(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_FAST_POLL_INTERVAL,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    # no extra imports

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    options = {OPT_FAST_POLL_INTERVAL: 5, OPT_SLOW_POLL_INTERVAL: 20}

    class DummyEntry:
        def __init__(self, options):
            self.options = options

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry(options)
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

    # Charging -> fast
    payload_charging = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_charging)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 5

    # Idle -> temporarily stay fast due to recent toggle
    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_idle)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 5

    # Once the boost expires, fall back to the configured slow interval
    coord._fast_until = None
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 20

    # Connector status indicates charging even if flag remains false -> treat as active
    payload_conn_only = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "CHARGING"}],
            }
        ]
    }
    coord.client = StubClient(payload_conn_only)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 5
    assert coord.data[RANDOM_SERIAL]["charging"] is True

    # EVSE-side suspension should be treated as paused (not charging)
    payload_conn_suspended = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "SUSPENDED_EVSE"}],
            }
        ]
    }
    coord.client = StubClient(payload_conn_suspended)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 5
    assert coord.data[RANDOM_SERIAL]["charging"] is False
    assert coord.data[RANDOM_SERIAL]["suspended_by_evse"] is True

    coord._fast_until = None
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 20


@pytest.mark.asyncio
async def test_auto_resume_when_evse_suspended(monkeypatch, hass):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    coord = EnphaseCoordinator(hass, cfg)

    class StubClient:
        def __init__(self):
            self.payload = {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                        "connectors": [{"connectorStatusType": "SUSPENDED_EVSE"}],
                    }
                ]
            }
            self.start_calls: list[tuple[str, int, int]] = []

        async def status(self):
            return self.payload

        async def summary_v2(self):
            return []

        async def start_charging(
            self,
            sn,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((sn, amps, connector_id))
            return {"status": "ok"}

    client = StubClient()
    coord.client = client
    coord.async_request_refresh = AsyncMock()

    coord.set_desired_charging(RANDOM_SERIAL, True)
    coord._auto_resume_attempts.clear()

    await coord._async_update_data()
    await hass.async_block_till_done()

    assert client.start_calls == [(RANDOM_SERIAL, 32, 1)]
    assert coord.async_request_refresh.await_count >= 1


@pytest.mark.asyncio
async def test_charging_expectation_hold(monkeypatch, hass):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class TimeKeeper:
        def __init__(self):
            self.value = 1_000.0

        def monotonic(self):
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += float(seconds)

    tk = TimeKeeper()
    monkeypatch.setattr(coord_mod.time, "monotonic", tk.monotonic)
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    coord = EnphaseCoordinator(hass, cfg)

    class StubClient:
        def __init__(self, payload):
            self.payload = payload

        async def status(self):
            return self.payload

        async def summary_v2(self):
            return []

    payload_charging = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
            }
        ]
    }
    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "AVAILABLE"}],
            }
        ]
    }

    client = StubClient(payload_charging)
    coord.client = client
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True

    coord.set_charging_expectation(RANDOM_SERIAL, False, hold_for=90)
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is False

    tk.advance(60)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is False

    tk.advance(40)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True
    assert RANDOM_SERIAL not in coord._pending_charging

    client.payload = payload_idle
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is False

    coord.set_charging_expectation(RANDOM_SERIAL, True, hold_for=90)
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True
    assert RANDOM_SERIAL in coord._pending_charging

    client.payload = payload_charging
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True
    assert RANDOM_SERIAL not in coord._pending_charging


@pytest.mark.asyncio
async def test_default_fast_interval_used_when_charging(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_FAST_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry()
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn: str):
            return "IMMEDIATE"

    payload = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "AVAILABLE"}],
            }
        ]
    }
    coord.client = StubClient(payload)
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == DEFAULT_FAST_POLL_INTERVAL


@pytest.mark.asyncio
async def test_summary_refresh_speed_up_when_charging(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry()
    from custom_components.enphase_ev import coordinator as coord_mod

    current = {"value": 1000.0}

    def fake_monotonic():
        return current["value"]

    monkeypatch.setattr(coord_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self):
            self.summary_calls = 0

        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "connectors": [{"connectorStatusType": "AVAILABLE"}],
                    }
                ]
            }

        async def summary_v2(self):
            self.summary_calls += 1
            return [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "lifeTimeConsumption": 1000.0,
                    "lastReportedAt": "2025-10-17T12:00:00Z[UTC]",
                }
            ]

        async def charge_mode(self, sn: str):
            return "IMMEDIATE"

    stub = StubClient()
    coord.client = stub

    await coord._async_update_data()
    assert stub.summary_calls == 1

    current["value"] += 15.0
    await coord._async_update_data()
    assert stub.summary_calls == 1

    current["value"] += 15.0
    await coord._async_update_data()
    assert stub.summary_calls == 2

    current["value"] += 70.0
    await coord._async_update_data()
    assert stub.summary_calls == 3


@pytest.mark.asyncio
async def test_streaming_prefers_fast(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_FAST_POLL_INTERVAL,
        OPT_FAST_WHILE_STREAMING,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    # no extra imports

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    options = {
        OPT_FAST_POLL_INTERVAL: 6,
        OPT_SLOW_POLL_INTERVAL: 22,
        OPT_FAST_WHILE_STREAMING: True,
    }

    class DummyEntry:
        def __init__(self, options):
            self.options = options

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry(options)
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_idle)
    coord._streaming = True
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 6


@pytest.mark.asyncio
async def test_session_history_enrichment(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                    }
                ]
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn):
            return None

    coord.client = StubClient()

    async def _fake_sessions(self, sn, *, day_local=None):
        return [
            {
                "session_id": "stub-1",
                "start": "2025-10-16T00:00:00+00:00",
                "end": "2025-10-16T01:00:00+00:00",
                "energy_kwh_total": 4.5,
                "energy_kwh": 4.5,
                "active_charge_time_s": 3600,
                "auth_type": None,
                "auth_identifier": None,
                "auth_token": None,
                "miles_added": 15.0,
                "session_cost": 1.1,
                "avg_cost_per_kwh": 0.24,
                "cost_calculated": True,
                "manual_override": False,
                "session_cost_state": "COST_CALCULATED",
                "charge_profile_stack_level": 0,
            },
            {
                "session_id": "stub-2",
                "start": "2025-10-16T04:00:00+00:00",
                "end": "2025-10-16T05:30:00+00:00",
                "energy_kwh_total": 2.0,
                "energy_kwh": 2.0,
                "active_charge_time_s": 5400,
                "auth_type": "RFID",
                "auth_identifier": "user",
                "auth_token": "token",
                "miles_added": 8.0,
                "session_cost": 0.6,
                "avg_cost_per_kwh": 0.3,
                "cost_calculated": True,
                "manual_override": True,
                "session_cost_state": "COST_CALCULATED",
                "charge_profile_stack_level": 4,
            },
            {
                "session_id": "stub-3",
                "start": "2025-10-15T23:30:00+00:00",
                "end": "2025-10-16T00:30:00+00:00",
                "energy_kwh_total": 4.0,
                "energy_kwh": 2.0,
                "active_charge_time_s": 3600,
                "auth_type": None,
                "auth_identifier": None,
                "auth_token": None,
                "miles_added": 10.0,
                "session_cost": 0.5,
                "avg_cost_per_kwh": 0.25,
                "cost_calculated": True,
                "manual_override": False,
                "session_cost_state": "COST_CALCULATED",
                "charge_profile_stack_level": 2,
            },
        ]

    coord._async_fetch_sessions_today = _fake_sessions.__get__(coord, coord.__class__)

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    assert st["energy_today_sessions_kwh"] == 0.0
    assert st["energy_today_sessions"] == []

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    assert st["energy_today_sessions_kwh"] == pytest.approx(8.5, abs=1e-3)
    assert len(st["energy_today_sessions"]) == 3
    cross_midnight = st["energy_today_sessions"][2]
    assert cross_midnight["energy_kwh_total"] == pytest.approx(4.0)
    assert cross_midnight["energy_kwh"] == pytest.approx(2.0)


@pytest.mark.asyncio
@pytest.mark.session_history_real
async def test_session_history_cross_midnight_split(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from homeassistant.util import dt as dt_util

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    day_now = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day_now)

    client = coord.client
    calls: list[dict] = []

    async def fake_session_history(self, sn, *, start_date, end_date, offset, limit):
        calls.append(
            {
                "sn": sn,
                "start_date": start_date,
                "end_date": end_date,
                "offset": offset,
                "limit": limit,
            }
        )
        return {
            "data": {
                "result": [
                    {
                        "sessionId": 1,
                        "startTime": "2025-10-15T23:30:00Z[UTC]",
                        "endTime": "2025-10-16T01:30:00Z[UTC]",
                        "aggEnergyValue": 6.0,
                        "activeChargeTime": 7200,
                    },
                    {
                        "sessionId": 2,
                        "startTime": "2025-10-16T04:00:00Z[UTC]",
                        "endTime": "2025-10-16T05:00:00Z[UTC]",
                        "aggEnergyValue": 3.0,
                        "activeChargeTime": 3600,
                    },
                ],
                "hasMore": False,
                "startDate": start_date,
                "endDate": end_date,
            }
        }

    monkeypatch.setattr(
        client,
        "session_history",
        fake_session_history.__get__(client, client.__class__),
        raising=False,
    )

    sessions = await coord._async_fetch_sessions_today(RANDOM_SERIAL, day_local=day_now)
    assert calls, "session_history should have been called"
    assert len(sessions) == 2
    assert len(calls) == 1

    first = sessions[0]
    assert first["energy_kwh_total"] == pytest.approx(6.0)
    # Only 1.5 hours of a 2 hour session occur within the day -> 75%
    assert first["energy_kwh"] == pytest.approx(4.5)
    assert first["active_charge_time_overlap_s"] == 5400

    second = sessions[1]
    assert second["energy_kwh"] == pytest.approx(3.0)

    # Cached result should be reused
    calls.clear()
    again = await coord._async_fetch_sessions_today(RANDOM_SERIAL, day_local=day_now)
    assert not calls
    assert again == sessions


@pytest.mark.asyncio
@pytest.mark.session_history_real
async def test_session_history_unauthorized_falls_back(hass, monkeypatch):
    from custom_components.enphase_ev.api import Unauthorized
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                    }
                ],
                "ts": 1757299870275,
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn):
            return None

        async def session_history(self, *args, **kwargs):
            raise Unauthorized()

    coord.client = StubClient()

    data = await coord._async_update_data()
    st = data[RANDOM_SERIAL]
    assert st["energy_today_sessions"] == []
    assert st["energy_today_sessions_kwh"] == 0.0


@pytest.mark.asyncio
@pytest.mark.session_history_real
async def test_session_history_inflight_session_counts_energy(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from homeassistant.util import dt as dt_util

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    # Fix "now" for the coordinator so the ongoing session overlaps the day
    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    class StubClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                    }
                ],
                "ts": 1757299870275,
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn):
            return None

        async def session_history(self, *args, **kwargs):
            return {
                "data": {
                    "result": [
                        {
                            "sessionId": 99,
                            "startTime": "2025-10-16T09:30:00Z[UTC]",
                            "endTime": None,
                            "aggEnergyValue": 4.0,
                            "activeChargeTime": 7200,
                        }
                    ],
                    "hasMore": False,
                }
            }

    coord.client = StubClient()

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    assert not st["energy_today_sessions"]
    assert st["energy_today_sessions_kwh"] == 0.0

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    sessions = st["energy_today_sessions"]
    assert sessions and len(sessions) == 1
    inflight = sessions[0]
    assert inflight["energy_kwh_total"] == pytest.approx(4.0)
    assert inflight["energy_kwh"] == pytest.approx(4.0)
    assert inflight["active_charge_time_overlap_s"] > 0
    assert st["energy_today_sessions_kwh"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_timeout_backoff_issue_recovery(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_SCAN_INTERVAL,
        ISSUE_NETWORK_UNREACHABLE,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry()
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )

    create_calls: list[tuple[str, str, dict]] = []
    delete_calls: list[tuple[str, str]] = []

    def stub_create_issue(hass_arg, domain, issue_id, **kwargs):
        create_calls.append((domain, issue_id, kwargs))

    def stub_delete_issue(hass_arg, domain, issue_id):
        delete_calls.append((domain, issue_id))

    monkeypatch.setattr(coord_mod.ir, "async_create_issue", stub_create_issue)
    monkeypatch.setattr(coord_mod.ir, "async_delete_issue", stub_delete_issue)

    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class TimeoutClient:
        async def status(self):
            await asyncio.sleep(0)
            raise asyncio.TimeoutError()

    coord.client = TimeoutClient()

    for idx in range(2):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        assert coord._network_errors == idx + 1
        assert coord._backoff_until is not None
        assert not create_calls
        coord._backoff_until = 0

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord._network_errors == 3
    assert create_calls
    assert create_calls[0][0] == "enphase_ev"
    assert create_calls[0][1] == ISSUE_NETWORK_UNREACHABLE
    assert len(create_calls) == 1
    coord._backoff_until = 0

    class SuccessClient:
        async def status(self):
            return {"evChargerData": []}

    coord.client = SuccessClient()
    await coord._async_update_data()
    assert coord._network_errors == 0
    assert coord._last_error is None
    assert delete_calls
    assert delete_calls[-1][1] == ISSUE_NETWORK_UNREACHABLE
    assert len(delete_calls) == 2
    assert coord._backoff_until is None
