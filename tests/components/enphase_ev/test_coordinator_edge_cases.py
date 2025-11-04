from __future__ import annotations

import asyncio
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev.const import (
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_SCAN_INTERVAL,
    CONF_SERIALS,
    CONF_SITE_ID,
    CONF_SESSION_ID,
    DOMAIN,
    OPT_API_TIMEOUT,
    OPT_SLOW_POLL_INTERVAL,
    DEFAULT_SLOW_POLL_INTERVAL,
)

from homeassistant.helpers.update_coordinator import UpdateFailed


class _BadStr:
    def __str__(self) -> str:
        raise RuntimeError("boom")


def _client_response_error(
    status: int, *, message: str = "", headers: dict[str, str] | None = None
) -> aiohttp.ClientResponseError:
    req = aiohttp.RequestInfo(
        url=aiohttp.client.URL("https://example.invalid"),
        method="GET",
        headers={},
        real_url=aiohttp.client.URL("https://example.invalid"),
    )
    return aiohttp.ClientResponseError(
        request_info=req,
        history=(),
        status=status,
        message=message,
        headers=headers or {},
    )


def _make_entry(hass, data_override: dict | None = None, *, options: dict | None = None):
    data = {
        CONF_SITE_ID: "111111",
        CONF_SERIALS: ["EV123"],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    if data_override:
        data.update(data_override)

    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options or {})
    entry.add_to_hass(hass)
    return entry


@pytest.mark.asyncio
async def test_coordinator_init_handles_bad_scalar_serial_and_legacy_super(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(
        hass,
        data_override={
            CONF_SERIALS: _BadStr(),
            OPT_API_TIMEOUT: 10,
        },
    )

    init_calls: list[dict] = []

    async def _fake_reauth_cb(*_):
        return None

    def fake_coord_init(self, hass_arg, logger, **kwargs):
        init_calls.append(dict(kwargs))
        if "config_entry" in kwargs:
            raise TypeError("legacy core missing config_entry kwarg")
        # Mimic Coordinator init side effects used later
        self.hass = hass_arg
        self.logger = logger
        self.update_interval = kwargs.get("update_interval")

    monkeypatch.setattr(
        coord_mod.DataUpdateCoordinator, "__init__", fake_coord_init, raising=False
    )
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "EnphaseEVClient",
        lambda *args, **kwargs: SimpleNamespace(set_reauth_callback=lambda *_: _fake_reauth_cb()),
    )

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)

    assert coord.serials == set()
    assert coord._serial_order == []
    assert len(init_calls) == 2
    assert "config_entry" in init_calls[0]
    assert "config_entry" not in init_calls[1]


def test_collect_site_metrics_handles_unfriendly_datetime(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord.site_id = "SITE"
    coord.site_name = "Garage"
    coord._backoff_until = 10.0
    coord.backoff_ends_utc = None
    coord._network_errors = 1
    coord._http_errors = 2
    coord._rate_limit_hits = 3
    coord._dns_failures = 4
    coord._summary_ttl = 60.0
    coord._phase_timings = {"status_s": 0.5}
    coord._last_error = "timeout"
    coord.last_failure_source = "http"
    coord.last_failure_response = "body"
    coord.last_failure_status = 503
    coord.last_failure_description = "Service Unavailable"
    coord.latency_ms = 120
    coord.last_success_utc = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    coord.last_failure_utc = datetime(2025, 1, 1, 11, 0, tzinfo=timezone.utc)

    class BadIso:
        def isoformat(self):
            raise ValueError("nope")

        def __str__(self):
            return "bad-date"

    coord.backoff_ends_utc = BadIso()

    metrics = coord.collect_site_metrics()

    assert metrics["site_id"] == "SITE"
    assert metrics["site_name"] == "Garage"
    assert metrics["backoff_active"] is False
    assert metrics["backoff_ends_utc"] == "bad-date"
    assert metrics["last_failure_status"] == 503
    assert metrics["network_errors"] == 1
    assert metrics["session_cache_ttl_s"] is None


@pytest.mark.asyncio
async def test_http_error_retry_after_date_triggers_rate_limit_issue(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass)

    time_keeper = SimpleNamespace(value=1_000.0)

    def monotonic():
        return time_keeper.value

    def fake_call_later(hass_obj, delay, callback):
        scheduled["delay"] = delay
        scheduled["callback"] = callback

        def _cancel():
            scheduled["cancelled"] = True

        return _cancel

    scheduled: dict[str, object] = {}
    issue_calls: list[tuple] = []

    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    monkeypatch.setattr(coord_mod.time, "monotonic", monotonic)
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *args, **kwargs: 2.0)
    monkeypatch.setattr(coord_mod, "async_call_later", fake_call_later)
    monkeypatch.setattr(
        coord_mod.dt_util,
        "utcnow",
        lambda: datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda *args, **kwargs: issue_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(coord_mod.ir, "async_delete_issue", lambda *args, **kwargs: None)

    class StubClient:
        async def status(self):
            raise _client_response_error(
                429,
                message="Too Many Requests",
                headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
            )

        async def summary_v2(self):
            return []

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord.client = StubClient()
    coord._rate_limit_hits = 1  # ensure branch triggers issue creation

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_status == 429
    assert coord.last_failure_source == "http"
    assert issue_calls, "Expected rate limit issue creation"
    assert scheduled["delay"] > 0
    assert coord._backoff_until == pytest.approx(time_keeper.value + scheduled["delay"])


@pytest.mark.asyncio
async def test_http_server_errors_raise_cloud_issue_and_clear_on_success(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass)

    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 2_000.0)
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *args, **kwargs: 1.5)

    created: list[tuple] = []
    deleted: list[tuple] = []

    monkeypatch.setattr(
        coord_mod.ir, "async_create_issue", lambda *args, **kwargs: created.append((args, kwargs))
    )
    monkeypatch.setattr(
        coord_mod.ir, "async_delete_issue", lambda *args, **kwargs: deleted.append((args, kwargs))
    )
    monkeypatch.setattr(coord_mod, "async_call_later", lambda *args, **kwargs: lambda: None)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: datetime.now(timezone.utc))

    class ErrorClient:
        async def status(self):
            raise _client_response_error(503, message="Service Unavailable")

        async def summary_v2(self):
            return []

    class SuccessClient:
        async def status(self):
            return {"evChargerData": [], "ts": None}

        async def summary_v2(self):
            return []

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord.client = ErrorClient()
    coord._http_errors = 2  # first increment -> 3 to trigger issue

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert any(
        call[0][2] == coord_mod.ISSUE_CLOUD_ERRORS for call in created
    ), "Cloud issue should be created"

    coord.client = SuccessClient()
    coord._cloud_issue_reported = True
    coord._dns_issue_reported = True
    coord._backoff_until = None
    coord.backoff_ends_utc = None
    created.clear()

    await coord._async_update_data()

    assert coord._cloud_issue_reported is False
    assert any(
        call[0][2] == coord_mod.ISSUE_CLOUD_ERRORS for call in deleted
    ), "Cloud issue should be cleared"
    assert any(
        call[0][2] == coord_mod.ISSUE_DNS_RESOLUTION for call in deleted
    ), "DNS issue should be cleared on success"


@pytest.mark.asyncio
async def test_network_error_dns_issue_reporting(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass)

    issue_calls: list[tuple] = []

    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 3_000.0)
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *args, **kwargs: 1.25)
    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda *args, **kwargs: issue_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(coord_mod.ir, "async_delete_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(coord_mod, "async_call_later", lambda *args, **kwargs: lambda: None)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: datetime.now(timezone.utc))

    class DnsClient:
        async def status(self):
            raise aiohttp.ClientConnectionError("Temporary failure in name resolution")

        async def summary_v2(self):
            return []

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord.client = DnsClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._dns_failures == 1
    assert coord._dns_issue_reported is False

    coord._backoff_until = None
    coord.backoff_ends_utc = None

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._dns_failures == 2
    assert coord._dns_issue_reported is True
    assert any(
        call[0][2] == coord_mod.ISSUE_DNS_RESOLUTION for call in issue_calls
    ), "DNS resolution issue should be raised"


@pytest.mark.asyncio
async def test_attempt_auto_refresh_success(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import AuthTokens

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord.client = SimpleNamespace(update_credentials=MagicMock())
    coord._persist_tokens = MagicMock()
    coord._tokens = AuthTokens(cookie="", session_id=None, access_token="", token_expires_at=None)

    new_tokens = AuthTokens(
        cookie="cookie",
        session_id="sess",
        access_token="token",
        token_expires_at=12345,
    )

    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    monkeypatch.setattr(coord_mod, "async_authenticate", AsyncMock(return_value=(new_tokens, {})))

    result = await coord._attempt_auto_refresh()

    assert result is True
    coord.client.update_credentials.assert_called_once_with(eauth="token", cookie="cookie")
    coord._persist_tokens.assert_called_once_with(new_tokens)
    assert coord._tokens == new_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_type",
    [
        "invalid",
        "mfa",
        "unavailable",
        "unexpected",
    ],
)
async def test_attempt_auto_refresh_failures(monkeypatch, hass, exc_type):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import (
        AuthTokens,
        EnlightenAuthInvalidCredentials,
        EnlightenAuthMFARequired,
        EnlightenAuthUnavailable,
    )

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "secret"
    coord._refresh_lock = asyncio.Lock()
    coord.client = SimpleNamespace(update_credentials=MagicMock())
    coord._persist_tokens = MagicMock()
    coord._tokens = AuthTokens(cookie="", session_id=None, access_token="", token_expires_at=None)

    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())

    if exc_type == "invalid":
        side_effect = EnlightenAuthInvalidCredentials()
    elif exc_type == "mfa":
        side_effect = EnlightenAuthMFARequired()
    elif exc_type == "unavailable":
        side_effect = EnlightenAuthUnavailable()
    else:
        side_effect = RuntimeError("boom")

    monkeypatch.setattr(coord_mod, "async_authenticate", AsyncMock(side_effect=side_effect))

    result = await coord._attempt_auto_refresh()

    assert result is False
    coord.client.update_credentials.assert_not_called()
    coord._persist_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_auto_refresh_requires_credentials(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord._email = None
    coord._remember_password = False
    coord._stored_password = None

    result = await coord._attempt_auto_refresh()
    assert result is False


@pytest.mark.asyncio
async def test_handle_client_unauthorized_refreshes_tokens(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord._unauth_errors = 0
    coord._last_error = None
    coord._attempt_auto_refresh = AsyncMock(return_value=True)
    coord.site_id = "SITE"
    coord.site_name = "Garage"
    coord._backoff_until = None
    coord.backoff_ends_utc = None
    coord.last_success_utc = None
    coord.last_failure_utc = None
    coord.last_failure_status = None
    coord.last_failure_description = None
    coord.last_failure_source = None
    coord.last_failure_response = None
    coord.latency_ms = 0
    coord._network_errors = 0
    coord._http_errors = 0
    coord._rate_limit_hits = 0
    coord._dns_failures = 0
    coord._phase_timings = {}

    deleted: list[tuple] = []
    monkeypatch.setattr(
        coord_mod.ir,
        "async_delete_issue",
        lambda *args, **kwargs: deleted.append((args, kwargs)),
    )
    monkeypatch.setattr(coord_mod.ir, "async_create_issue", lambda *args, **kwargs: None)

    result = await coord._handle_client_unauthorized()

    assert result is True
    assert coord._unauth_errors == 0
    assert deleted and deleted[0][0][2] == "reauth_required"


@pytest.mark.asyncio
async def test_handle_client_unauthorized_creates_issue_after_failures(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord._unauth_errors = 1
    coord._last_error = None
    coord._attempt_auto_refresh = AsyncMock(return_value=False)
    coord.site_id = "SITE"
    coord.site_name = "Garage"
    coord._backoff_until = None
    coord.backoff_ends_utc = None
    coord.last_success_utc = None
    coord.last_failure_utc = None
    coord.last_failure_status = None
    coord.last_failure_description = None
    coord.last_failure_source = None
    coord.last_failure_response = None
    coord.latency_ms = 0
    coord._network_errors = 0
    coord._http_errors = 0
    coord._rate_limit_hits = 0
    coord._dns_failures = 0
    coord._phase_timings = {}

    created: list[tuple] = []
    monkeypatch.setattr(coord_mod.ir, "async_delete_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )

    from homeassistant.exceptions import ConfigEntryAuthFailed

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._handle_client_unauthorized()

    assert coord._unauth_errors == 2
    assert created and created[0][0][2] == "reauth_required"


def test_persist_tokens_updates_entry(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.api import AuthTokens

    entry = _make_entry(hass)
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord.config_entry = entry

    captured: list[tuple] = []

    def fake_update_entry(entry_obj, data=None, **kwargs):
        captured.append((entry_obj, data))

    monkeypatch.setattr(hass.config_entries, "async_update_entry", fake_update_entry)

    tokens = AuthTokens(
        cookie="cookie",
        session_id="sess",
        access_token="token",
        token_expires_at=123,
    )
    coord._persist_tokens(tokens)

    assert captured
    updated_entry, payload = captured[0]
    assert updated_entry is entry
    assert payload[CONF_COOKIE] == "cookie"
    assert payload[CONF_EAUTH] == "token"
    assert payload[CONF_SESSION_ID] == "sess"


def test_kick_fast_handles_invalid_input(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord._fast_until = None

    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 500.0)

    coord.kick_fast("bad-input")

    assert coord._fast_until == pytest.approx(560.0)


def test_set_charging_expectation_handles_hold(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord._pending_charging = {}

    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 1_000.0)

    coord.set_charging_expectation("EV1", True, hold_for=10)
    assert coord._pending_charging["EV1"] == (True, 1_010.0)

    coord.set_charging_expectation("EV1", False, hold_for=0)
    assert "EV1" not in coord._pending_charging


def test_slow_interval_floor_with_invalid_option(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass, options={OPT_SLOW_POLL_INTERVAL: "not-int"})
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.config_entry = entry
    coord.update_interval = timedelta(seconds=5)

    result = coord._slow_interval_floor()
    assert result == DEFAULT_SLOW_POLL_INTERVAL


def test_clear_backoff_timer_handles_exception():
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    called = {"cancelled": False}

    def canceller():
        called["cancelled"] = True
        raise RuntimeError("fail")

    coord._backoff_cancel = canceller
    coord.backoff_ends_utc = object()

    coord._clear_backoff_timer()

    assert called["cancelled"] is True
    assert coord._backoff_cancel is None
    assert coord.backoff_ends_utc is None


def test_schedule_backoff_timer_zero_delay(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord._backoff_cancel = None
    coord._backoff_until = 123.0
    coord.async_request_refresh = AsyncMock()

    monkeypatch.setattr(coord_mod, "async_call_later", lambda *args, **kwargs: lambda: None)

    coord._schedule_backoff_timer(0)

    coord.async_request_refresh.assert_called_once()
    assert coord._backoff_until is None
    assert coord.backoff_ends_utc is None


def test_schedule_backoff_timer_sets_callback(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord.async_request_refresh = AsyncMock()
    callbacks: list = []

    monkeypatch.setattr(coord_mod, "async_call_later", lambda _hass, _delay, cb: callbacks.append(cb) or (lambda: None))
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: datetime(2025, 1, 1, tzinfo=timezone.utc))

    coord._backoff_cancel = None

    coord._schedule_backoff_timer(5.0)

    assert coord.backoff_ends_utc is not None
    assert callbacks, "callback should be registered"


def test_coerce_and_apply_amp_helpers(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {"EV1": {"min_amp": "10", "max_amp": "8"}}
    coord.last_set_amps = {}

    assert EnphaseCoordinator._coerce_amp(None) is None
    assert EnphaseCoordinator._coerce_amp(" 12 ") == 12
    assert EnphaseCoordinator._coerce_amp("bad") is None

    min_amp, max_amp = coord._amp_limits("EV1")
    assert min_amp == 10
    assert max_amp == 10  # inverted bounds clamp

    assert coord._apply_amp_limits("EV1", 40) == 10
    assert coord._apply_amp_limits("EV1", None) == 10

    coord.data["EV1"]["charging_level"] = " 16 "
    coord.data["EV1"]["session_charge_level"] = " 14 "
    coord.last_set_amps["EV1"] = 20
    assert coord.pick_start_amps("EV1", requested="18") == 10
    coord.data = {}
    coord.last_set_amps = {}
    assert coord.pick_start_amps("EV1", requested=None, fallback=24) == 24


@pytest.mark.asyncio
async def test_get_charge_mode_uses_cache_and_client(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord._charge_mode_cache = {"EV1": ("CACHED", coord_mod.time.monotonic())}
    coord.client = SimpleNamespace(charge_mode=AsyncMock(return_value="REMOTE"))

    cached = await coord._get_charge_mode("EV1")
    assert cached == "CACHED"

    coord._charge_mode_cache["EV1"] = ("OLD", coord_mod.time.monotonic() - 1_000)
    result = await coord._get_charge_mode("EV1")
    assert result == "REMOTE"
    assert coord._charge_mode_cache["EV1"][0] == "REMOTE"

    coord.client.charge_mode = AsyncMock(side_effect=RuntimeError("fail"))
    result = await coord._get_charge_mode("EV2")
    assert result is None


def test_set_charge_mode_cache_updates(monkeypatch, hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord._charge_mode_cache = {}

    coord.set_charge_mode_cache("EV1", "SMART")
    value, ts = coord._charge_mode_cache["EV1"]
    assert value == "SMART"
    assert ts >= 0


def test_set_last_set_amps_and_require_plugged(hass):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator, ServiceValidationError

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = hass
    coord.last_set_amps = {}
    coord.data = {"EV1": {"min_amp": 10, "max_amp": 40, "plugged": False, "name": "Garage"}}

    coord.set_last_set_amps("EV1", 50)
    assert coord.last_set_amps["EV1"] == 40

    with pytest.raises(ServiceValidationError):
        coord.require_plugged("EV1")

    coord.data["EV1"]["plugged"] = True
    coord.require_plugged("EV1")  # should not raise


@pytest.mark.asyncio
async def test_async_update_data_handles_complex_payload(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    entry = _make_entry(hass)

    time_keeper = SimpleNamespace(monotonic=1_000.0)

    def monotonic():
        return time_keeper.monotonic

    def advance(seconds: float):
        time_keeper.monotonic += seconds

    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    monkeypatch.setattr(coord_mod.time, "monotonic", monotonic)
    monkeypatch.setattr(coord_mod.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(
        coord_mod.dt_util,
        "utcnow",
        lambda: datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        coord_mod.dt_util,
        "now",
        lambda: datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(hass, "async_create_task", lambda coro: None)

    class BadDisplay:
        def __str__(self) -> str:
            raise ValueError("boom")

    class ComplexClient:
        def __init__(self):
            self.payload = {
                "ts": "2025-05-01T12:00:00Z[UTC]",
                "evChargerData": [
                    {
                        "sn": "EV1",
                        "name": "Main Charger",
                        "displayName": BadDisplay(),
                        "connected": " yes ",
                        "pluggedIn": " TRUE ",
                        "charging": True,
                        "chargingLevel": " 32 ",
                        "faulted": "0",
                        "connectors": [{"connectorStatusType": "Charging", "connectorStatusReason": "OK"}],
                        "session_d": {
                            "e_c": 500,
                            "charge_level": " 18 ",
                            "session_cost": " 3.4567 ",
                            "miles": " 12.3456 ",
                            "start_time": "1714550000000",
                        },
                        "sch_d": {
                            "status": "enabled",
                            "info": [{"type": "eco", "startTime": "06:00", "endTime": "08:00"}],
                        },
                    },
                    {
                        "sn": "EV2",
                        "name": "Second Charger",
                        "connected": 1,
                        "pluggedIn": True,
                        "charging": False,
                        "connectors": [{"connectorStatusType": "SUSPENDED_EVSE"}],
                        "session_d": {"plg_out_at": "1714553600000"},
                    },
                ],
            }

        async def status(self):
            return self.payload

        async def summary_v2(self):
            advance(0.1)
            return [
                {
                    "serialNumber": "EV1",
                    "displayName": "Driveway",
                    "maxCurrent": 48,
                    "chargeLevelDetails": {"min": " 6 ", "max": " 40 "},
                    "phaseMode": "single",
                    "status": "READY",
                    "activeConnection": " ethernet ",
                    "networkConfig": '[{"ipaddr":"192.168.1.20","connectionStatus":"1"}]',
                    "reportingInterval": " 300 ",
                    "dlbEnabled": "true",
                    "commissioningStatus": True,
                    "lastReportedAt": "2025-05-01T11:59:00Z",
                    "operatingVoltage": "240.5",
                    "lifeTimeConsumption": "12345.6",
                    "chargeLevel": "28",
                }
            ]

        async def charge_mode(self, sn: str):
            return {"EV2": "REMOTE"}.get(sn)

    client = ComplexClient()
    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    coord.client = client
    coord._async_resolve_charge_modes = AsyncMock(return_value={"EV1": None, "EV2": "REMOTE"})
    coord._pending_charging = {"EV1": (False, time_keeper.monotonic + 5)}
    coord._last_charging = {"EV2": True}
    coord._session_end_fix = {}
    coord._operating_v = {}
    coord.last_set_amps = {}
    coord.set_last_set_amps = MagicMock(side_effect=RuntimeError("bad"))
    coord._summary_cache = None
    coord._schedule_session_enrichment = MagicMock()

    data = await coord._async_update_data()

    assert data["EV1"]["charge_mode"] == "ECO"
    assert data["EV1"]["session_kwh"] == pytest.approx(0.5)
    assert data["EV1"]["session_charge_level"] == 18
    assert data["EV1"]["session_cost"] == pytest.approx(3.457)
    assert data["EV1"]["session_miles"] == pytest.approx(12.346)
    assert data["EV1"]["connector_status"] == "Charging"
    assert data["EV1"]["charging"] is False
    assert data["EV1"]["display_name"] == "Driveway"
    assert data["EV1"]["operating_v"] == 240
    assert data["EV1"]["reporting_interval"] == 300
    assert data["EV1"]["dlb_enabled"] is True
    assert data["EV1"]["ip_address"] == "192.168.1.20"

    assert data["EV2"]["suspended_by_evse"] is True
    assert isinstance(data["EV2"]["session_end"], int)
