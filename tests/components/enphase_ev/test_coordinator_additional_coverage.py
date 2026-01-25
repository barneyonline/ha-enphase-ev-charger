"""Focused coverage tests for EnphaseCoordinator edge branches."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp.client_reqrep import RequestInfo
from homeassistant.helpers.update_coordinator import UpdateFailed
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

import builtins
from homeassistant.exceptions import ConfigEntryAuthFailed
from http import HTTPStatus

from custom_components.enphase_ev import coordinator as coord_mod
from custom_components.enphase_ev.coordinator import (
    FAST_TOGGLE_POLL_HOLD_S,
    EnphaseCoordinator,
    ServiceValidationError,
    ChargeModeStartPreferences,
)
from custom_components.enphase_ev.api import Unauthorized
from custom_components.enphase_ev.const import (
    GREEN_BATTERY_SETTING,
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
async def test_async_update_data_http_error_description(
    coordinator_factory, mock_issue_registry, monkeypatch
):
    coord = coordinator_factory()
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=502,
        message='{"error":{"description":"bad"}}',
        headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00"},
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._http_errors = 1
    coord._schedule_backoff_timer = MagicMock()
    monkeypatch.setattr(
        coord_mod.dt_util, "utcnow", lambda: datetime(2025, 1, 1, tzinfo=timezone.utc)
    )

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_description == "bad"
    assert any(issue[1] == ISSUE_CLOUD_ERRORS for issue in mock_issue_registry.created)


@pytest.mark.asyncio
async def test_async_update_data_http_error_trimmed_json(
    coordinator_factory, mock_issue_registry
):
    coord = coordinator_factory()
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=500,
        message='"{"error":{"displayMessage":"trimmed"}}"',
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_description == "trimmed"


@pytest.mark.asyncio
async def test_async_update_data_http_retry_after_invalid(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    coord._cloud_issue_reported = True
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=418,
        message="I'm a teapot",
        headers={"Retry-After": "invalid"},
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._cloud_issue_reported is False


@pytest.mark.asyncio
async def test_async_update_data_unauthorized_promotes_config_error(coordinator_factory):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(side_effect=Unauthorized())
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_update_data_http_status_phrase_fallback(coordinator_factory):
    coord = coordinator_factory()
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=429,
        message="  ",
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_description == HTTPStatus(429).phrase
def test_summary_compat_shims(coordinator_factory):
    coord = coordinator_factory()
    coord.summary = None

    assert coord._summary_cache is None
    coord._summary_cache = (0.0, [], 5.0)
    assert coord._summary_cache == (0.0, [], 5.0)

    coord._summary_ttl = 12.5
    assert coord._summary_ttl == 12.5

    dummy_summary = SimpleNamespace(_cache=None, ttl=0.0, _ttl=0.0)
    coord.summary = dummy_summary
    coord._summary_cache = (1.0, [{"sn": "1"}], 2.0)
    assert dummy_summary._cache == (1.0, [{"sn": "1"}], 2.0)
    coord._summary_ttl = 15.0
    assert dummy_summary._ttl == 15.0

    coord.__dict__.pop("session_history", None)
    coord._session_history_cache_ttl = None
    assert coord._session_history_cache_ttl is None

    coord.session_history = SimpleNamespace(cache_ttl=300)
    coord._session_history_cache_ttl = 120
    assert coord.session_history.cache_ttl == 120
    assert coord._session_history_cache_ttl == 120


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


@pytest.mark.asyncio
async def test_session_history_shims_without_manager(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    coord.__dict__.pop("session_history", None)

    day = datetime(2025, 5, 1, tzinfo=timezone.utc)
    sessions = await coord._async_enrich_sessions(["SN"], day, in_background=False)
    assert sessions == {}

    assert coord._sum_session_energy([{ "energy_kwh": 1.0 }, {"energy_kwh": "bad"}]) == pytest.approx(1.0)

    result = await coord._async_fetch_sessions_today("", day_local=day)
    assert result == []

    monkeypatch.setattr(coord_mod.dt_util, "now", lambda: datetime(2025, 5, 1, 10, 0, 0))
    assert await coord._async_fetch_sessions_today("SN", day_local=None) == []


def test_sum_session_energy_handles_conversion_error(coordinator_factory):
    coord = coordinator_factory()
    coord.__dict__.pop("session_history", None)

    class UnfriendlyInt(int):
        def __float__(self):
            raise ValueError("boom")

    total = coord._sum_session_energy([{"energy_kwh": UnfriendlyInt(5)}])
    assert total == 0.0


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


@pytest.mark.asyncio
async def test_async_update_data_network_error_clears_dns(
    coordinator_factory, mock_issue_registry, monkeypatch
):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(side_effect=aiohttp.ClientError("boom"))
    coord._schedule_backoff_timer = MagicMock()
    coord._network_errors = 3
    coord._dns_issue_reported = True

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._dns_issue_reported is False


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


@pytest.mark.asyncio
async def test_async_auto_resume_respects_preferences(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.pick_start_amps = MagicMock(return_value=24)
    prefs = ChargeModeStartPreferences(
        mode="SCHEDULED_CHARGING",
        include_level=True,
        strict=True,
        enforce_mode="SCHEDULED_CHARGING",
    )
    coord._charge_mode_start_preferences = MagicMock(return_value=prefs)
    coord._ensure_charge_mode = AsyncMock()
    coord.data = {sn: {"plugged": True}}

    await coord._async_auto_resume(sn, {"plugged": True})

    coord.client.start_charging.assert_awaited_once_with(
        sn, 24, include_level=True, strict_preference=True
    )
    coord._ensure_charge_mode.assert_awaited_once_with(sn, "SCHEDULED_CHARGING")


@pytest.mark.asyncio
async def test_async_auto_resume_aborts_when_unplugged(coordinator_factory):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    coord.client.start_charging = AsyncMock()
    coord.data = {sn: {"plugged": False}}

    await coord._async_auto_resume(sn, {"plugged": False})
    coord.client.start_charging.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_auto_resume_not_ready_breaks_loop(coordinator_factory):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    coord.client.start_charging = AsyncMock(return_value={"status": "not_ready"})
    coord.pick_start_amps = MagicMock(return_value=32)
    coord._charge_mode_start_preferences = MagicMock(
        return_value=ChargeModeStartPreferences(
            mode="SCHEDULED_CHARGING",
            include_level=True,
            strict=True,
            enforce_mode="SCHEDULED_CHARGING",
        )
    )
    await coord._async_auto_resume(sn, {"plugged": True})
    coord._charge_mode_start_preferences.assert_called()
    coord.client.start_charging.assert_awaited_once()


def test_apply_lifetime_guard_confirms_resets(monkeypatch):
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.summary = SimpleNamespace(invalidate=MagicMock())
    coord.energy._lifetime_guard = {}

    sn = "EV1"
    first = coord.energy._apply_lifetime_guard(sn, 15000, {"lifetime_kwh": 12.0})
    assert first == pytest.approx(15.0)

    # Drop to trigger pending reset detection
    with monkeypatch.context() as ctx:
        ticker = deque([1_000.0, 1_005.0, 1_020.0])
        ctx.setattr(coord_mod.time, "monotonic", lambda: ticker[0])
        drop = coord.energy._apply_lifetime_guard(sn, 2000, None)
        assert drop == pytest.approx(15.0)
        ticker.popleft()
        confirmed = coord.energy._apply_lifetime_guard(sn, 2000, None)
        assert confirmed == pytest.approx(2.0)

    coord.summary.invalidate.assert_called_once()


@pytest.mark.asyncio
async def test_async_update_data_success_enriches_payload(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory(serials=["SN1"])
    sn = "SN1"
    original_round = builtins.round

    def fake_round(value, ndigits=None):
        if value == 5.0 and ndigits == 3:
            raise ValueError("round boom")
        if ndigits is None:
            return original_round(value)
        return original_round(value, ndigits)

    monkeypatch.setattr(builtins, "round", fake_round)
    coord.last_set_amps[sn] = 32
    status_payload = {
        "evChargerData": [
            {},
            {
                "sn": sn,
                "name": "Garage",
                "charging": False,
                "pluggedIn": True,
                "faulted": False,
                "chargeMode": None,
                "chargingLevel": None,
                "session_d": {
                    "sessionId": "abc",
                    "start_time": 100,
                    "plg_in_at": "2025-10-30T05:00:00Z",
                    "plg_out_at": "2025-10-30T06:00:00Z",
                    "e_c": 100,
                    "miles": "5",
                    "sessionCost": "1.5",
                    "chargeProfileStackLevel": "1",
                },
                "connectors": [
                    {
                        "connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS,
                        "commissioned": "true",
                    }
                ],
                "sch_d": {
                    "status": "enabled",
                    "info": [{"type": "smart", "startTime": "1", "endTime": "2"}],
                },
                "lst_rpt_at": None,
                "session_energy_wh": "50",
                "commissioned": None,
                "operating_v": 240,
                "displayName": " Display ",
            },
        ],
        "ts": "2025-10-30T10:00:00Z[UTC]",
    }
    coord.client.status = AsyncMock(return_value=status_payload)
    coord._async_resolve_charge_modes = AsyncMock(return_value={sn: "SMART"})
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: True,
        async_fetch=AsyncMock(
            return_value=[
                {
                    "serialNumber": sn,
                    "maxCurrent": 48,
                    "chargeLevelDetails": {"min": "16", "max": "40"},
                    "phaseMode": "split",
                    "status": "online",
                    "activeConnection": "wifi",
                    "networkConfig": '[{"ipaddr":"1.2.3.4","connectionStatus":"1"}]',
                    "reportingInterval": "60",
                    "dlbEnabled": "true",
                    "commissioningStatus": True,
                    "lastReportedAt": "2025-10-30T09:59:00Z",
                    "operatingVoltage": "240",
                    "lifeTimeConsumption": 10000,
                    "firmwareVersion": "1.0",
                    "hardwareVersion": "revA",
                    "displayName": "Friendly",
                }
            ]
        ),
        invalidate=lambda: None,
    )

    class _DummyView:
        def __init__(self):
            self.sessions = [{"energy_kwh": 1.0}]
            self.needs_refresh = True
            self.blocked = False

    class _DummyHistory:
        cache_ttl = 60

        def get_cache_view(self, *_args, **_kwargs):
            return _DummyView()

        async def async_enrich(self, *_args, **_kwargs):
            return {sn: [{"energy_kwh": 1.0}]}

        def schedule_enrichment(self, *_args, **_kwargs):
            return None

        def sum_energy(self, sessions):
            return 2.0

    coord.session_history = _DummyHistory()

    result = await coord._async_update_data()
    assert sn in result
    entry = result[sn]
    assert entry["charge_mode"] == "SMART"
    assert entry["energy_today_sessions_kwh"] == 2.0


@pytest.mark.asyncio
async def test_async_update_data_handles_numeric_ts(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["SN2"])
    sn = "SN2"
    coord.last_set_amps[sn] = 16
    status_payload = {
        "evChargerData": [
            {
                "sn": sn,
                "name": "Driveway",
                "charging": True,
                "pluggedIn": True,
                "faulted": False,
                "connectors": [{"connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS}],
                "sch_d": {"status": "enabled", "info": [{}]},
                "session_d": {"start_time": 1700000000, "plg_in_at": None},
            }
        ],
        "ts": 1_700_000_000_000,
    }
    coord.client.status = AsyncMock(return_value=status_payload)
    coord._async_resolve_charge_modes = AsyncMock(return_value={})

    class _MiniHistory:
        cache_ttl = 60

        def get_cache_view(self, *_):
            return SimpleNamespace(sessions=[], needs_refresh=False, blocked=False)

        async def async_enrich(self, *_args, **_kwargs):
            return {sn: []}

        def schedule_enrichment(self, *_args, **_kwargs):
            return None

        def sum_energy(self, *_args, **_kwargs):
            return 0.0

    coord.session_history = _MiniHistory()
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=lambda: None,
    )

    result = await coord._async_update_data()
    assert "last_reported_at" in result[sn]


def test_determine_polling_state_handles_options(hass):
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {"A": {"charging": False}}
    coord._fast_until = coord_mod.time.monotonic() + 5
    coord._streaming = True
    coord._streaming_until = coord_mod.time.monotonic() + 5
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


def test_amp_helpers_and_expectation_management(coordinator_factory, monkeypatch):
    coord = coordinator_factory(serials=["EV1"])
    coord.data["EV1"].update({"min_amp": "10", "max_amp": "40", "plugged": False})

    assert coord._coerce_amp(" 15 ") == 15
    assert coord._amp_limits("EV1") == (10, 40)
    assert coord._apply_amp_limits("EV1", 5) == 10
    coord.set_last_set_amps("EV1", 50)
    assert coord.last_set_amps["EV1"] == 40

    with pytest.raises(ServiceValidationError):
        coord.require_plugged("EV1")

    coord.serials = None
    coord._serial_order = None
    assert coord._ensure_serial_tracked("  EV2  ") is True
    assert "EV2" in coord.iter_serials()

    coord.set_desired_charging("EV1", True)
    assert coord.get_desired_charging("EV1") is True
    coord.set_desired_charging("EV1", None)
    assert coord.get_desired_charging("EV1") is None

    coord.set_charging_expectation("EV1", True, hold_for=0)
    coord.set_charging_expectation("EV1", True, hold_for=2)
    assert coord._pending_charging["EV1"][0] is True
    coord._pending_charging.clear()

    coord.config_entry = SimpleNamespace(options={coord_mod.OPT_SLOW_POLL_INTERVAL: "bad"})
    assert coord._slow_interval_floor() >= coord_mod.DEFAULT_SLOW_POLL_INTERVAL

    coord.data["EV1"].update({"charging_level": "18"})
    assert coord.pick_start_amps("EV1", requested=None, fallback=30) == 40

    called = {"count": 0}

    def _cancel():
        called["count"] += 1
        raise RuntimeError("cancel fail")

    coord._backoff_cancel = _cancel
    coord._clear_backoff_timer()
    assert called["count"] == 1

    coord.hass.async_create_task = MagicMock(return_value=None)
    coord.async_request_refresh = MagicMock(return_value=None)
    coord._schedule_backoff_timer(0)

    coord._backoff_cancel = None
    coord._schedule_backoff_timer(1)


def test_charge_mode_preference_helpers(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    sn = "EV1"
    coord.data[sn]["charge_mode_pref"] = "MANUAL_CHARGING"
    prefs = coord._charge_mode_start_preferences(sn)
    assert prefs.include_level is True
    assert prefs.strict is True
    assert prefs.enforce_mode is None

    coord.data[sn]["charge_mode_pref"] = "SCHEDULED_CHARGING"
    prefs = coord._charge_mode_start_preferences(sn)
    assert prefs.include_level is True
    assert prefs.enforce_mode == "SCHEDULED_CHARGING"

    coord.data[sn]["charge_mode_pref"] = "GREEN_CHARGING"
    prefs = coord._charge_mode_start_preferences(sn)
    assert prefs.include_level is False
    assert prefs.strict is True

    coord.data[sn]["charge_mode_pref"] = None
    coord._charge_mode_cache[sn] = ("SMART", coord_mod.time.monotonic())
    assert coord._resolve_charge_mode_pref(sn) == "SMART"


def test_resolve_charge_mode_pref_handles_errors(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    sn = "EV1"

    class FaultyMapping:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    class BadStr:
        def __str__(self):
            raise ValueError("bad")

    coord.data = FaultyMapping()
    coord._charge_mode_cache[sn] = (BadStr(), coord_mod.time.monotonic())

    assert coord._resolve_charge_mode_pref(sn) is None


@pytest.mark.asyncio
async def test_ensure_charge_mode_updates_cache(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    sn = "EV1"
    coord.client.set_charge_mode = AsyncMock(return_value={"ok": True})
    await coord._ensure_charge_mode(sn, "SCHEDULED_CHARGING")
    coord.client.set_charge_mode.assert_awaited_once_with(sn, "SCHEDULED_CHARGING")
    assert coord._charge_mode_cache[sn][0] == "SCHEDULED_CHARGING"


@pytest.mark.asyncio
async def test_ensure_charge_mode_handles_errors(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    coord.client.set_charge_mode = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._ensure_charge_mode("EV1", "GREEN_CHARGING")


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
async def test_get_charge_mode_uses_cache(coordinator_factory):
    coord = coordinator_factory(serials=["SN1"])
    coord._charge_mode_cache["SN1"] = ("SMART", coord_mod.time.monotonic())
    assert await coord._get_charge_mode("SN1") == "SMART"

    coord._charge_mode_cache.clear()
    coord.client.charge_mode = AsyncMock(return_value=None)
    assert await coord._get_charge_mode("SN1") is None


@pytest.mark.asyncio
async def test_get_green_battery_setting_parses_and_caches(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    coord._green_battery_cache.clear()
    coord.client.green_charging_settings = AsyncMock(
        return_value=[
            "invalid",
            {"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": "false"}
        ]
    )
    result = await coord._get_green_battery_setting("EV1")
    assert result == (False, True)
    result_cached = await coord._get_green_battery_setting("EV1")
    assert result_cached == (False, True)
    coord.client.green_charging_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_green_battery_setting_handles_missing_setting(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    coord._green_battery_cache.clear()
    coord.client.green_charging_settings = AsyncMock(
        return_value=[{"chargerSettingName": "OTHER_SETTING", "enabled": True}]
    )
    result = await coord._get_green_battery_setting("EV1")
    assert result == (None, False)


@pytest.mark.asyncio
async def test_async_resolve_green_battery_settings_uses_cached_fallback(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1", "EV2", "EV3", "EV4"])
    now = coord_mod.time.monotonic()
    expired = now - coord_mod.GREEN_BATTERY_CACHE_TTL - 1
    coord._green_battery_cache["EV1"] = (True, True, now)
    coord._green_battery_cache["EV2"] = (False, True, expired)
    coord._green_battery_cache["EV3"] = (None, False, expired)
    coord._green_battery_cache["EV4"] = (True, True, expired)
    coord._get_green_battery_setting = AsyncMock(
        side_effect=[RuntimeError("boom"), None, (True, True)]
    )

    result = await coord._async_resolve_green_battery_settings(
        ["EV1", "EV2", "EV3", "EV4", ""]
    )

    assert result == {
        "EV1": (True, True),
        "EV2": (False, True),
        "EV3": (None, False),
        "EV4": (True, True),
    }


@pytest.mark.asyncio
async def test_get_green_battery_setting_coercion_paths(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    cases = [
        (None, None),
        (True, True),
        (0, False),
        ("true", True),
        ("maybe", None),
    ]
    for enabled, expected in cases:
        coord._green_battery_cache.clear()
        coord.client.green_charging_settings = AsyncMock(
            return_value=[
                {"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": enabled}
            ]
        )
        result = await coord._get_green_battery_setting("EV1")
        assert result == (expected, True)


@pytest.mark.asyncio
async def test_async_update_data_includes_green_battery_settings(
    coordinator_factory,
):
    coord = coordinator_factory(serials=[SERIAL_ONE])
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "chargeMode": "IDLE",
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.green_charging_settings = AsyncMock(
        return_value=[
            {"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": True}
        ]
    )
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["green_battery_supported"] is True
    assert result[SERIAL_ONE]["green_battery_enabled"] is True


@pytest.mark.asyncio
async def test_async_update_data_summary_supports_use_battery_overrides(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "chargeMode": "IDLE",
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.green_charging_settings = AsyncMock(
        return_value=[
            {"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": True}
        ]
    )
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(
        return_value=[{"serialNumber": SERIAL_ONE, "supportsUseBattery": False}]
    )
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["green_battery_supported"] is False
    assert "green_battery_enabled" not in result[SERIAL_ONE]


@pytest.mark.asyncio
async def test_handle_client_unauthorized_paths(
    coordinator_factory, mock_issue_registry
):
    coord = coordinator_factory()

    async def _auto_refresh_success() -> bool:
        return True

    coord._attempt_auto_refresh = _auto_refresh_success  # type: ignore[assignment]
    assert await coord._handle_client_unauthorized() is True

    async def _auto_refresh_failure() -> bool:
        return False

    coord._attempt_auto_refresh = _auto_refresh_failure  # type: ignore[assignment]
    coord._unauth_errors = 1
    with pytest.raises(coord_mod.ConfigEntryAuthFailed):
        await coord._handle_client_unauthorized()
    assert any(issue[1] == "reauth_required" for issue in mock_issue_registry.created)


def test_persist_tokens_updates_entry(coordinator_factory, config_entry):
    coord = coordinator_factory()
    coord.config_entry = config_entry

    def _fake_update_entry(entry, *, data=None, options=None):
        if data is not None:
            object.__setattr__(entry, "data", MappingProxyType(dict(data)))
        if options is not None:
            object.__setattr__(entry, "options", MappingProxyType(dict(options)))

    coord.hass.config_entries.async_update_entry = _fake_update_entry  # type: ignore[assignment]

    tokens = coord_mod.AuthTokens(cookie="c", session_id="s", access_token="t", token_expires_at=123)
    coord._persist_tokens(tokens)
    assert config_entry.data[coord_mod.CONF_COOKIE] == "c"
    assert config_entry.data[coord_mod.CONF_ACCESS_TOKEN] == "t"


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


def test_fast_poll_helpers(coordinator_factory):
    coord = coordinator_factory()
    before = coord_mod.time.monotonic()
    coord.kick_fast("bad")
    assert coord._fast_until > before

    coord._last_actual_charging = {"EV1": False}
    coord._record_actual_charging("EV1", True)
    coord._record_actual_charging("EV1", None)
    assert "EV1" not in coord._last_actual_charging


def test_streaming_active_expires(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_manual = True
    coord._streaming_targets = {"EV1": True}
    coord._streaming_until = coord_mod.time.monotonic() - 1

    assert coord._streaming_active() is False
    assert coord._streaming is False
    assert coord._streaming_manual is False
    assert coord._streaming_targets == {}


def test_streaming_active_without_expiry(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_manual = True
    coord._streaming_until = None

    assert coord._streaming_active() is True
    assert coord._streaming_manual is True


def test_streaming_response_ok_variants(coordinator_factory):
    coord = coordinator_factory()
    assert coord._streaming_response_ok("ok") is True
    assert coord._streaming_response_ok({"status": None}) is True


def test_streaming_duration_invalid_uses_default(coordinator_factory):
    coord = coordinator_factory()
    duration = coord._streaming_duration_s({"duration_s": "bad"})
    assert duration == coord_mod.STREAMING_DEFAULT_DURATION_S


@pytest.mark.asyncio
async def test_async_start_streaming_tracks_targets(coordinator_factory):
    coord = coordinator_factory()
    coord.client.start_live_stream = AsyncMock(
        return_value={"status": "accepted", "duration_s": 900}
    )
    await coord.async_start_streaming(serial="EV1", expected_state=True)

    assert coord._streaming is True
    assert coord._streaming_manual is False
    assert coord._streaming_targets["EV1"] is True
    assert coord._streaming_until is not None


@pytest.mark.asyncio
async def test_async_start_streaming_respects_manual_lock(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming_manual = True
    coord.client.start_live_stream = AsyncMock(return_value={"status": "accepted"})

    await coord.async_start_streaming(serial="EV1", expected_state=True)

    coord.client.start_live_stream.assert_not_awaited()
    assert coord._streaming_targets == {}


@pytest.mark.asyncio
async def test_async_start_streaming_existing_stream_handles_error(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord.client.start_live_stream = AsyncMock(side_effect=RuntimeError("boom"))

    await coord.async_start_streaming(serial="EV1", expected_state=False)

    assert coord._streaming_targets["EV1"] is False


@pytest.mark.asyncio
async def test_async_start_streaming_rejects_error(coordinator_factory):
    coord = coordinator_factory()
    coord.client.start_live_stream = AsyncMock(return_value={"status": "error"})
    await coord.async_start_streaming(serial="EV1", expected_state=True)

    assert coord._streaming is False
    assert coord._streaming_targets == {}


@pytest.mark.asyncio
async def test_async_start_streaming_manual_clears_targets(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming_targets = {"EV1": True}
    coord.client.start_live_stream = AsyncMock(
        return_value={"status": "accepted", "duration_s": 900}
    )

    await coord.async_start_streaming(manual=True)

    assert coord._streaming is True
    assert coord._streaming_manual is True
    assert coord._streaming_targets == {}


@pytest.mark.asyncio
async def test_async_stop_streaming_manual_clears_state(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = coord_mod.time.monotonic() + 60
    coord._streaming_manual = True
    coord._streaming_targets = {"EV1": True}
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "accepted"})

    await coord.async_stop_streaming(manual=True)

    coord.client.stop_live_stream.assert_awaited_once()
    assert coord._streaming is False
    assert coord._streaming_manual is False
    assert coord._streaming_targets == {}


@pytest.mark.asyncio
async def test_async_stop_streaming_skips_manual_lock(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord._streaming_manual = True
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "accepted"})

    await coord.async_stop_streaming(manual=False)

    coord.client.stop_live_stream.assert_not_awaited()
    assert coord._streaming is True


@pytest.mark.asyncio
async def test_async_stop_streaming_skips_inactive(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = False
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "accepted"})

    await coord.async_stop_streaming(manual=False)

    coord.client.stop_live_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_stop_streaming_handles_error(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord.client.stop_live_stream = AsyncMock(side_effect=RuntimeError("boom"))

    await coord.async_stop_streaming(manual=True)

    coord.client.stop_live_stream.assert_awaited_once()
    assert coord._streaming is False


def test_auto_streaming_stops_on_expected_state(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = coord_mod.time.monotonic() + 60
    coord._streaming_targets = {"EV1": True}

    called = {}

    def _capture(force=False):
        called["force"] = force

    coord._schedule_stream_stop = _capture  # type: ignore[assignment]
    coord._record_actual_charging("EV1", True)

    assert called["force"] is True
    assert coord._streaming is False
    assert coord._streaming_targets == {}


def test_schedule_stream_stop_skips_when_task_active(coordinator_factory, monkeypatch):
    coord = coordinator_factory()

    class DummyTask:
        def done(self):
            return False

    coord._streaming_stop_task = DummyTask()
    capture = MagicMock()
    monkeypatch.setattr(coord.hass, "async_create_task", capture)

    coord._schedule_stream_stop()

    capture.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_stream_stop_force_runs(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "accepted"})

    tasks: list[asyncio.Task] = []

    def _create_task(coro, name=None):
        if name is not None:
            coro.close()
            raise TypeError("no name")
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(coord.hass, "async_create_task", _create_task)

    coord._schedule_stream_stop(force=True)

    await tasks[0]
    coord.client.stop_live_stream.assert_awaited_once()
    assert coord._streaming is False


@pytest.mark.asyncio
async def test_schedule_stream_stop_runs_async_stop(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "accepted"})

    tasks: list[asyncio.Task] = []

    def _create_task(coro, name=None):
        if name is not None:
            coro.close()
            raise TypeError("no name")
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(coord.hass, "async_create_task", _create_task)

    coord._schedule_stream_stop(force=False)

    await tasks[0]
    coord.client.stop_live_stream.assert_awaited_once()
    assert coord._streaming is False


@pytest.mark.asyncio
async def test_schedule_stream_stop_force_handles_error(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord.client.stop_live_stream = AsyncMock(side_effect=RuntimeError("boom"))

    tasks: list[asyncio.Task] = []

    def _create_task(coro, name=None):
        if name is not None:
            coro.close()
            raise TypeError("no name")
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(coord.hass, "async_create_task", _create_task)

    coord._schedule_stream_stop(force=True)

    await tasks[0]
    coord.client.stop_live_stream.assert_awaited_once()
    assert coord._streaming is False


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


def test_persist_tokens_updates_entry_calls_hass_update(hass, config_entry):
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
