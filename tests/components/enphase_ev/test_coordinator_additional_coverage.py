"""Focused coverage tests for EnphaseCoordinator edge branches."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp.client_reqrep import RequestInfo
from homeassistant.helpers.update_coordinator import UpdateFailed
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from custom_components.enphase_ev import coordinator as coord_mod
from custom_components.enphase_ev.coordinator import (
    FAST_TOGGLE_POLL_HOLD_S,
    EnphaseCoordinator,
    ServiceValidationError,
)
from custom_components.enphase_ev.const import (
    ISSUE_CLOUD_ERRORS,
    ISSUE_DNS_RESOLUTION,
    ISSUE_NETWORK_UNREACHABLE,
)
from custom_components.enphase_ev.session_history import MIN_SESSION_HISTORY_CACHE_TTL
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL as SERIAL_ONE

pytestmark = pytest.mark.session_history_real

pytest.importorskip("homeassistant")


def _request_info() -> RequestInfo:
    """Build a minimal RequestInfo for ClientResponseError."""
    return RequestInfo(
        url=URL("https://enphase.example/status"),
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL("https://enphase.example/status"),
    )


@pytest.mark.asyncio
async def test_async_enrich_sessions_invokes_history(coordinator_factory):
    coord = coordinator_factory()
    serials = [SERIAL_ONE]
    fake_history = SimpleNamespace(
        async_enrich=AsyncMock(return_value={"123": []}),
        cache_ttl=MIN_SESSION_HISTORY_CACHE_TTL,
    )
    coord.session_history = fake_history

    day = datetime(2025, 5, 1, tzinfo=timezone.utc)
    result = await coord._async_enrich_sessions(serials, day, in_background=False)

    assert result == {"123": []}
    fake_history.async_enrich.assert_awaited_once_with(
        serials, day, in_background=False
    )


@pytest.mark.asyncio
async def test_async_fetch_sessions_today_handles_timezone_error(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))

    original = coord_mod.dt_util.as_local
    calls = {"count": 0}

    def fake_as_local(value):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError("boom")
        return original(value)

    monkeypatch.setattr(coord_mod.dt_util, "as_local", fake_as_local)
    coord.session_history = SimpleNamespace(
        cache_ttl=MIN_SESSION_HISTORY_CACHE_TTL,
        _async_fetch_sessions_today=AsyncMock(return_value=[{"energy_kwh": 1.2}]),
    )

    naive_day = datetime(2025, 1, 1, 12, 0, 0)
    first = await coord._async_fetch_sessions_today(sn, day_local=naive_day)
    assert first == [{"energy_kwh": 1.2}]

    # Immediate second call should reuse cache without invoking session history again.
    second = await coord._async_fetch_sessions_today(sn, day_local=naive_day)
    assert second == first
    coord.session_history._async_fetch_sessions_today.assert_awaited_once()


def test_collect_site_metrics_serializes_dates(coordinator_factory):
    coord = coordinator_factory()

    class BadDate:
        def __init__(self, label: str) -> None:
            self._label = label

        def isoformat(self) -> str:
            raise ValueError("fail")

        def __str__(self) -> str:
            return self._label

    coord.site_name = "Garage"
    coord.last_success_utc = BadDate("success")
    coord.last_failure_utc = BadDate("failure")
    coord.backoff_ends_utc = BadDate("backoff")
    coord.last_failure_status = 500
    coord.last_failure_description = "boom"
    coord.last_failure_source = "http"
    coord.last_failure_response = '{"error":"boom"}'
    coord._backoff_until = coord_mod.time.monotonic() + 10

    metrics = coord.collect_site_metrics()
    assert metrics["last_success"] == "success"
    assert metrics["last_failure"] == "failure"
    assert metrics["backoff_ends_utc"] == "backoff"

    placeholders = coord._issue_translation_placeholders(metrics)
    assert placeholders == {
        "site_id": coord.site_id,
        "site_name": "Garage",
        "last_error": "boom",
        "last_status": "500",
    }


@pytest.mark.asyncio
async def test_async_update_data_http_error_creates_cloud_issue(
    coordinator_factory, mock_issue_registry, monkeypatch
):
    coord = coordinator_factory()
    headers = CIMultiDictProxy(CIMultiDict({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=503,
        message='{"error":{"displayMessage":"scheduled maintenance"}}',
        headers=headers,
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._http_errors = 2
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None
    assert coord.last_failure_description == "scheduled maintenance"
    assert any(issue[1] == ISSUE_CLOUD_ERRORS for issue in mock_issue_registry.created)


@pytest.mark.asyncio
async def test_async_update_data_network_dns_issue(
    coordinator_factory, mock_issue_registry, monkeypatch
):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(
        side_effect=aiohttp.ClientError("dns failure in name resolution")
    )
    coord._network_errors = 2
    coord._dns_failures = 1
    coord._schedule_backoff_timer = MagicMock()
    monkeypatch.setattr(coord, "_slow_interval_floor", lambda: 30)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None
    assert any(issue[1] == ISSUE_NETWORK_UNREACHABLE for issue in mock_issue_registry.created)
    assert any(issue[1] == ISSUE_DNS_RESOLUTION for issue in mock_issue_registry.created)


def test_sync_desired_charging_schedules_auto_resume(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    now = coord_mod.time.monotonic()
    coord._desired_charging = {sn: True}
    coord._auto_resume_attempts = {}
    coord.data = {
        sn: {
            "sn": sn,
            "charging": False,
            "plugged": True,
            "connector_status": coord_mod.SUSPENDED_EVSE_STATUS,
        }
    }
    created = []

    def fake_create_task(coro, *, name=None):
        created.append((coro, name))
        return None

    monkeypatch.setattr(coord.hass, "async_create_task", fake_create_task)

    coord._sync_desired_charging(coord.data)

    assert len(created) == 1
    coro, name = created[0]
    assert name == f"enphase_ev_auto_resume_{sn}"
    coro.close()
    assert coord._auto_resume_attempts[sn] >= now


def test_apply_lifetime_guard_confirms_resets(monkeypatch):
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.summary = SimpleNamespace(invalidate=MagicMock())
    coord._lifetime_guard = {}

    sn = "EV1"
    first = coord._apply_lifetime_guard(sn, 15000, {"lifetime_kwh": 12.0})
    assert first == pytest.approx(15.0)

    # Drop to trigger pending reset detection
    with monkeypatch.context() as ctx:
        ticker = deque([1_000.0, 1_005.0, 1_020.0])
        ctx.setattr(coord_mod.time, "monotonic", lambda: ticker[0])
        drop = coord._apply_lifetime_guard(sn, 2000, None)
        assert drop == pytest.approx(15.0)
        ticker.popleft()
        confirmed = coord._apply_lifetime_guard(sn, 2000, None)
        assert confirmed == pytest.approx(2.0)

    coord.summary.invalidate.assert_called_once()


def test_determine_polling_state_handles_options(hass):
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {"A": {"charging": False}}
    coord._fast_until = coord_mod.time.monotonic() + 5
    coord._streaming = True
    coord.update_interval = timedelta(seconds=90)
    coord.config_entry = SimpleNamespace(
        options={
        coord_mod.OPT_FAST_POLL_INTERVAL: "not-a-number",
        coord_mod.OPT_SLOW_POLL_INTERVAL: "75",
        coord_mod.OPT_FAST_WHILE_STREAMING: False,
        }
    )

    state = coord._determine_polling_state({"A": {"charging": True}})
    assert state["want_fast"] is True
    assert state["fast"] == coord_mod.DEFAULT_FAST_POLL_INTERVAL
    assert state["slow"] == 75


@pytest.mark.asyncio
async def test_async_resolve_charge_modes_uses_cache_and_handles_errors(monkeypatch, coordinator_factory):
    coord = coordinator_factory(serials=["EV1", "EV2"])
    coord._charge_mode_cache = {"EV1": ("IMMEDIATE", coord_mod.time.monotonic())}

    async def fake_get(sn: str):
        if sn == "EV2":
            return "SMART"
        raise RuntimeError("boom")

    coord._get_charge_mode = AsyncMock(side_effect=fake_get)
    result = await coord._async_resolve_charge_modes(["EV1", "EV2", "EV3"])
    assert result["EV1"] == "IMMEDIATE"
    assert result["EV2"] == "SMART"
    assert "EV3" not in result


def test_has_embedded_charge_mode_detects_nested():
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    payload = {
        "sch_d": {"info": [{"status": "enabled"}]},
    }
    assert coord._has_embedded_charge_mode(payload) is True
    assert coord._has_embedded_charge_mode({"foo": "bar"}) is False


@pytest.mark.asyncio
async def test_attempt_auto_refresh_updates_tokens(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "pw"
    tokens = coord_mod.AuthTokens("cookie", "sess", "token", 123)
    monkeypatch.setattr(
        coord_mod, "async_authenticate", AsyncMock(return_value=(tokens, None))
    )
    coord.client.update_credentials = MagicMock()
    coord._persist_tokens = MagicMock()

    assert await coord._attempt_auto_refresh() is True
    coord.client.update_credentials.assert_called_once()
    coord._persist_tokens.assert_called_once_with(tokens)


@pytest.mark.asyncio
async def test_handle_client_unauthorized_paths(
    coordinator_factory, mock_issue_registry
):
    coord = coordinator_factory()
    coord._attempt_auto_refresh = AsyncMock(return_value=True)
    assert await coord._handle_client_unauthorized() is True

    coord._attempt_auto_refresh = AsyncMock(return_value=False)
    coord._unauth_errors = 1
    with pytest.raises(coord_mod.ConfigEntryAuthFailed):
        await coord._handle_client_unauthorized()
    assert any(issue[1] == "reauth_required" for issue in mock_issue_registry.created)


@pytest.mark.asyncio
async def test_async_start_charging_handles_not_ready(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    coord.data[sn]["plugged"] = True
    coord.require_plugged = MagicMock()
    coord.pick_start_amps = MagicMock(return_value=32)
    coord.client.start_charging = AsyncMock(return_value={"status": "not_ready"})
    coord.async_request_refresh = AsyncMock()

    result = await coord.async_start_charging(sn, hold_seconds=10)
    assert result["status"] == "not_ready"
    coord.set_desired_charging(sn, False)


@pytest.mark.asyncio
async def test_async_stop_charging_respects_allow_flag(coordinator_factory):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    coord.require_plugged = MagicMock()
    coord.client.stop_charging = AsyncMock(return_value={"ok": True})
    coord.async_request_refresh = AsyncMock()

    await coord.async_stop_charging(sn, allow_unplugged=False)
    coord.require_plugged.assert_called_once_with(sn)


def test_schedule_amp_restart_replaces_existing_task(coordinator_factory, hass):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    stored = {}

    class DummyTask:
        def __init__(self):
            self._done = False
            self.callbacks = []

        def cancel(self):
            self._done = True

        def done(self):
            return self._done

        def add_done_callback(self, cb):
            self.callbacks.append(cb)

    def fake_task(coro, *, name=None):
        coro.close()
        task = DummyTask()
        stored[name] = task
        return task

    hass.async_create_task = fake_task
    coord.schedule_amp_restart(sn, delay=1)
    coord.schedule_amp_restart(sn, delay=2)
    assert len(coord._amp_restart_tasks) == 1


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_runs_sequence(monkeypatch):
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()
    monkeypatch.setattr(coord_mod.asyncio, "sleep", AsyncMock())

    await coord._async_restart_after_amp_change("EV1", "invalid")
    coord.async_stop_charging.assert_awaited()
    coord.async_start_charging.assert_awaited()


def test_persist_tokens_updates_entry(hass, config_entry):
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.config_entry = config_entry
    coord.hass = hass
    hass.config_entries.async_update_entry = MagicMock()
    tokens = coord_mod.AuthTokens("cookie", "sess", "token", 111)

    coord._persist_tokens(tokens)

    hass.config_entries.async_update_entry.assert_called_once()


def test_kick_fast_handles_invalid_seconds(coordinator_factory):
    coord = coordinator_factory()
    coord.kick_fast("invalid")
    assert coord._fast_until is not None


def test_record_actual_charging_triggers_fast(coordinator_factory):
    coord = coordinator_factory()
    coord.kick_fast = MagicMock()
    coord._record_actual_charging(SERIAL_ONE, True)
    coord._record_actual_charging(SERIAL_ONE, False)
    coord.kick_fast.assert_called_with(FAST_TOGGLE_POLL_HOLD_S)


def test_set_charging_expectation_handles_zero(coordinator_factory):
    coord = coordinator_factory()
    coord.set_charging_expectation(SERIAL_ONE, True, hold_for=0)
    assert SERIAL_ONE not in coord._pending_charging
    coord.set_charging_expectation(SERIAL_ONE, True, hold_for=10)
    assert SERIAL_ONE in coord._pending_charging


def test_clear_and_schedule_backoff_timer(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    cancelled = {"count": 0}

    def fake_cancel():
        cancelled["count"] += 1

    coord._backoff_cancel = fake_cancel
    coord._clear_backoff_timer()
    assert cancelled["count"] == 1

    created = {}

    coord.async_request_refresh = AsyncMock()

    def fake_async_call_later(_hass, delay, cb):
        created["callback"] = cb
        return lambda: created.setdefault("cancelled", True)

    monkeypatch.setattr(coord_mod, "async_call_later", fake_async_call_later)

    called = {}

    def fake_async_create_task(coro, *, name=None):
        called["coro"] = coro
        called["name"] = name
        return None

    monkeypatch.setattr(coord.hass, "async_create_task", fake_async_create_task)

    coord._schedule_backoff_timer(0)
    coro = called["coro"]
    coro.close()
    assert "coro" in called

    coord._schedule_backoff_timer(5)
    assert "callback" in created


def test_require_plugged_raises(monkeypatch):
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {"EV1": {"name": "Garage", "plugged": False}}
    with pytest.raises(ServiceValidationError):
        coord.require_plugged("EV1")


def test_ensure_serial_tracked_discovers(monkeypatch):
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.serials = set()
    coord._serial_order = []
    assert coord._ensure_serial_tracked(" 12345 ") is True
    assert "12345" in coord.serials
    assert coord._ensure_serial_tracked("") is False
