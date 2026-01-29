"""Extra coverage for rarely hit coordinator branches."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import logging

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from aiohttp.client_reqrep import RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from custom_components.enphase_ev import coordinator as coord_mod
from custom_components.enphase_ev.coordinator import (
    ChargeModeStartPreferences,
    ServiceValidationError,
)
from custom_components.enphase_ev.const import (
    CONF_COOKIE,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_SLOW_POLL_INTERVAL,
)
from custom_components.enphase_ev.api import AuthTokens
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

pytest.importorskip("homeassistant")


def _request_info() -> RequestInfo:
    return RequestInfo(
        url=URL("https://enphase.example/status"),
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL("https://enphase.example/status"),
    )


@pytest.fixture
def fake_summary(monkeypatch):
    """Provide a stub summary store that records calls."""

    class _Summary:
        def __init__(self):
            self.force_calls: list[dict] = []
            self._cache = None
            self._ttl = 0

        def prepare_refresh(self, *, want_fast: bool, target_interval: float):
            self.force_calls.append({"want_fast": want_fast, "target": target_interval})
            return False

        async def async_fetch(self, *, force: bool = False):
            return []

        def invalidate(self):
            self.force_calls.append({"invalidate": True})

    summary = _Summary()
    monkeypatch.setattr(
        coord_mod, "SummaryStore", lambda *_, **__: summary, raising=False
    )
    return summary


@pytest.mark.asyncio
async def test_async_update_data_invalid_http_status_blank_payload(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=799,
        message="   ",
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_description == "HTTP error"
    assert coord.last_failure_response == "   "


@pytest.mark.asyncio
async def test_async_update_data_http_error_plain_string(coordinator_factory):
    coord = coordinator_factory()
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=502,
        message='"temporary"',
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_description == "temporary"
    assert coord.last_failure_response == '"temporary"'


@pytest.mark.asyncio
async def test_async_update_data_reraises_config_entry_auth_failed(coordinator_factory):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(side_effect=ConfigEntryAuthFailed)

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_update_data_success_handles_edge_payloads(
    coordinator_factory, mock_issue_registry, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    coord = coordinator_factory()
    coord._unauth_errors = 2
    coord._has_successful_refresh = True
    coord._session_history_cache_ttl = None
    coord._schedule_session_enrichment = MagicMock()
    coord._async_enrich_sessions = AsyncMock(return_value={"ghost": []})

    class _View(SimpleNamespace):
        sessions: list | None = None
        needs_refresh = True
        blocked = False

    coord.session_history = SimpleNamespace(
        get_cache_view=lambda sn, day, now: _View(
            sessions=[], needs_refresh=True, blocked=False
        ),
        sum_energy=lambda sessions: 0.0,
    )

    data_ts = "1700000123456"
    obj1 = {
        "sn": RANDOM_SERIAL,
        "name": "Edge",
        "connectors": [{"safeLimitState": 1}],
        "pluggedIn": True,
        "charging": False,
        "faulted": False,
        "chargingLevel": 8,
        "session_d": {
            "e_c": 500.0,
            "miles": " ",
            "chargeLevel": " ",
            "charge_level": "oops",
        },
    }
    obj2 = {
        "sn": "AUX",
        "name": "Aux",
        "connectors": [{"safeLimitState": True}],
        "pluggedIn": True,
        "charging": False,
        "faulted": False,
        "session_d": {
            "e_c": 0,
            "miles": object(),
            "charge_level": True,
        },
    }
    obj3 = {
        "sn": "THIRD",
        "name": "Third",
        "connectors": [{}],
        "pluggedIn": True,
        "charging": False,
        "faulted": False,
        "session_d": {
            "e_c": 0,
            "charge_level": "oops",
        },
    }
    obj4 = {
        "sn": "FOURTH",
        "name": "Fourth",
        "connectors": [{}],
        "pluggedIn": True,
        "charging": False,
        "faulted": False,
        "session_d": {
            "e_c": 0,
            "chargingLevel": [],
        },
    }
    payload = {"evChargerData": [obj1, obj2, obj3, obj4], "ts": data_ts}
    coord.client.status = AsyncMock(return_value=payload)

    original_round = getattr(coord_mod, "round", round)

    def fake_round(value, ndigits=None):
        if isinstance(value, float) and abs(value - 0.5) < 0.01:
            raise ValueError("boom")
        if ndigits is None:
            return original_round(value)
        return original_round(value, ndigits)

    monkeypatch.setattr(coord_mod, "round", fake_round, raising=False)

    bad_interval = type("BadInterval", (), {"__str__": lambda self: "oops"})()

    summary_entries = [
        {},
        {
            "serialNumber": RANDOM_SERIAL,
            "chargeLevelDetails": {
                "min": "bad",
                "max": object(),
                "granularity": object(),
            },
            "reportingInterval": bad_interval,
            "operatingVoltage": object(),
            "networkConfig": [
                {"ipaddr": "10.0.0.10", "connectionStatus": "0"},
                {"ip": "10.0.0.11", "connectionStatus": "true"},
            ],
            "commissioningStatus": True,
            "dlbEnabled": "yes",
            "lastReportedAt": "2025-01-01T00:00:00Z",
            "lifeTimeConsumption": 5.5,
            "maxCurrent": 32,
            "phaseMode": "SINGLE",
            "status": "ONLINE",
            "activeConnection": " wifi ",
        },
        {
            "serialNumber": "NEW2",
            "networkConfig": '[\n  "ipaddr=,connectionStatus=0",\n  "ipaddr=192.0.2.1,connectionStatus=1"\n',
            "reportingInterval": "bad",
            "operatingVoltage": "bad",
        },
    ]

    summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=summary_entries),
        invalidate=MagicMock(),
    )
    coord.summary = summary

    original_as_local = coord_mod.dt_util.as_local

    now_calls = {"count": 0}

    def fake_now():
        now_calls["count"] += 1
        if now_calls["count"] == 1:
            raise RuntimeError("boom")
        return datetime(2025, 1, 1, tzinfo=timezone.utc)

    as_local_calls = {"count": 0}

    def fake_as_local(value):
        as_local_calls["count"] += 1
        if as_local_calls["count"] == 1:
            raise RuntimeError("fail")
        return original_as_local(value)

    monkeypatch.setattr(coord_mod.dt_util, "now", fake_now)
    monkeypatch.setattr(coord_mod.dt_util, "as_local", fake_as_local)

    coord.config_entry = SimpleNamespace(
        options={
            OPT_FAST_POLL_INTERVAL: "bad",
            OPT_SLOW_POLL_INTERVAL: "oops",
            OPT_FAST_WHILE_STREAMING: object(),
        },
        data={"entry_id": "123"},
        entry_id="123",
    )
    coord.async_set_update_interval = MagicMock(side_effect=RuntimeError("legacy"))

    result = await coord._async_update_data()

    assert any(issue[1] == "reauth_required" for issue in mock_issue_registry.deleted)
    assert any(
        "Coordinator refresh timings" in record.message for record in caplog.records
    )
    snapshot_data = {key: result[key] for key in sorted(result.keys())}
    assert set(snapshot_data) == {
        RANDOM_SERIAL,
        "AUX",
        "FOURTH",
        "NEW2",
        "THIRD",
    }
    main = snapshot_data[RANDOM_SERIAL]
    assert main["ip_address"] == "10.0.0.11"
    assert main["lifetime_kwh"] == 5.5
    assert main["status"] == "ONLINE"
    assert main["charge_mode"] == "IDLE"
    assert main["energy_today_sessions_kwh"] == 0.0
    assert main["safe_limit_state"] == 1
    assert RANDOM_SERIAL not in coord.last_set_amps
    aux = snapshot_data["AUX"]
    assert aux["last_reported_at"] is not None
    assert aux["session_energy_wh"] == 0.0
    new2 = snapshot_data["NEW2"]
    assert new2["ip_address"] == "192.0.2.1"
    assert new2["max_current"] is None


@pytest.mark.asyncio
async def test_async_update_data_resets_issues_and_network_config(
    coordinator_factory, mock_issue_registry, monkeypatch
):
    coord = coordinator_factory()
    sn = RANDOM_SERIAL
    coord._unauth_errors = 1
    coord._network_issue_reported = True
    coord._cloud_issue_reported = True
    coord._dns_issue_reported = True
    coord._backoff_cancel = lambda: None
    coord._has_successful_refresh = True

    original_as_local = coord_mod.dt_util.as_local
    calls = {"count": 0}

    def fake_as_local(value):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return original_as_local(value)

    monkeypatch.setattr(coord_mod.dt_util, "now", lambda: datetime(2025, 1, 1))
    monkeypatch.setattr(coord_mod.dt_util, "as_local", fake_as_local)

    coord.async_set_update_interval = MagicMock(side_effect=TypeError("legacy"))
    coord.config_entry = SimpleNamespace(
        options={}, data={"entry_id": "123"}, entry_id="123"
    )

    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *args, **kwargs: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda *_: 0.0,
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(
            return_value=[
                {
                    "serialNumber": sn,
                    "networkConfig": ["", {"ipaddr": "", "connectionStatus": "0"}],
                    "reportingInterval": "oops",
                }
            ]
        ),
        invalidate=MagicMock(),
    )
    payload = {
        "evChargerData": [
            {
                "sn": sn,
                "name": "EV",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "session_d": {"chargeLevel": "bad", "miles": "oops"},
            }
        ],
        "ts": "2025-01-01T00:00:00Z",
    }
    coord.client.status = AsyncMock(return_value=payload)

    result = await coord._async_update_data()

    assert any(issue[1] == "reauth_required" for issue in mock_issue_registry.deleted)
    assert coord._dns_issue_reported is False
    assert result[sn].get("ip_address") is None


@pytest.mark.asyncio
async def test_async_update_data_session_end_fix_handles_invalid_timestamp(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    sn = RANDOM_SERIAL
    coord._last_charging[sn] = True

    calls = {"count": 0}

    def fake_time():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return 123456

    monkeypatch.setattr(coord_mod.time, "time", fake_time)
    coord.client.status = AsyncMock(
        return_value={
            "ts": float("nan"),
            "evChargerData": [
                {
                    "sn": sn,
                    "name": "EV",
                    "connectors": [{}],
                    "session_d": {},
                    "pluggedIn": True,
                    "charging": False,
                }
            ],
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=MagicMock(),
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *args, **kwargs: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda sessions: 0.0,
    )

    await coord._async_update_data()
    assert coord._session_end_fix[sn] == 123456


@pytest.mark.asyncio
async def test_async_update_data_session_end_fix_default_branch(
    coordinator_factory,
):
    coord = coordinator_factory()
    sn = RANDOM_SERIAL
    coord._last_charging[sn] = True
    coord.client.status = AsyncMock(
        return_value={
            "ts": "bad",
            "evChargerData": [
                {
                    "sn": sn,
                    "name": "EV",
                    "connectors": [{}],
                    "session_d": {},
                    "pluggedIn": True,
                    "charging": False,
                }
            ],
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=MagicMock(),
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *args, **kwargs: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda sessions: 0.0,
    )

    await coord._async_update_data()
    assert coord._session_end_fix[sn]


@pytest.mark.asyncio
async def test_async_update_data_handles_invalid_global_timestamp(coordinator_factory):
    coord = coordinator_factory()
    sn = RANDOM_SERIAL
    coord.client.status = AsyncMock(
        return_value={
            "ts": "2025-13-99T00:00:00Z",
            "evChargerData": [
                {
                    "sn": sn,
                    "name": "EV",
                    "connectors": [{}],
                    "session_d": {},
                    "pluggedIn": True,
                    "charging": False,
                }
            ],
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=MagicMock(),
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *args, **kwargs: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda sessions: 0.0,
    )

    result = await coord._async_update_data()
    assert result[sn]["last_reported_at"] is None


def test_sync_desired_charging_handles_auto_resume_typeerror(coordinator_factory):
    coord = coordinator_factory()
    sn = RANDOM_SERIAL
    coord._desired_charging[sn] = True

    def fake_create_task(coro, *, name=None):
        if name is not None:
            coro.close()
            raise TypeError("legacy")
        coro.close()
        return None

    coord.hass.async_create_task = MagicMock(side_effect=fake_create_task)
    info = {
        "charging": False,
        "plugged": True,
        "connector_status": "SUSPENDED_EVSE",
    }
    coord._sync_desired_charging(
        {sn: info, "other": {"charging": False, "plugged": False}}
    )
    coord.hass.async_create_task.assert_called()


def test_sync_desired_charging_skips_when_unplugged(coordinator_factory):
    coord = coordinator_factory()
    sn = RANDOM_SERIAL
    coord._desired_charging[sn] = True
    coord.hass.async_create_task = MagicMock()
    coord._sync_desired_charging({sn: {"charging": False, "plugged": False}})
    coord.hass.async_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_async_update_data_covers_remaining_branches(
    coordinator_factory, mock_issue_registry
):
    coord = coordinator_factory()
    coord._unauth_errors = 1
    coord._has_successful_refresh = True
    coord._backoff_cancel = lambda: None

    class BadNumeric(float):
        def __int__(self):
            raise ValueError("bad int")

    payload = {
        "ts": "2025-01-01T00:00:00Z",
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "EV",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "session_d": {"chargeLevel": BadNumeric()},
            }
        ],
    }

    summary_entry = {
        "serialNumber": RANDOM_SERIAL,
        "networkConfig": {"ipaddr": ""},
        "reportingInterval": "",
    }

    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[summary_entry]),
        invalidate=lambda: None,
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *_, **__: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda *_: 0.0,
    )
    coord.client.status = AsyncMock(return_value=payload)
    coord.config_entry = SimpleNamespace(
        options={}, data={"entry_id": "123"}, entry_id="123"
    )
    coord.async_set_update_interval = AsyncMock(side_effect=RuntimeError("boom"))

    result = await coord._async_update_data()

    assert any(issue[1] == "reauth_required" for issue in mock_issue_registry.deleted)
    assert result[RANDOM_SERIAL].get("reporting_interval") is None
    assert result[RANDOM_SERIAL].get("ip_address") is None


@pytest.mark.asyncio
async def test_async_auto_resume_handles_snapshot_and_state(coordinator_factory):
    coord = coordinator_factory()
    coord.data = object()
    coord.pick_start_amps = lambda *args, **kwargs: 16
    coord._charge_mode_start_preferences = lambda sn: ChargeModeStartPreferences()
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    snapshot = {"plugged": True}
    await coord._async_auto_resume(RANDOM_SERIAL, snapshot)
    coord.client.start_charging.assert_awaited_once()


def test_apply_lifetime_guard_handles_invalid_samples(coordinator_factory):
    coord = coordinator_factory()

    class ExplodingFloat(float):
        def __new__(cls, value):
            return super().__new__(cls, value)

        def __float__(self):
            raise ValueError("boom")

    prev = {"lifetime_kwh": ExplodingFloat(5.0)}
    assert coord.energy._apply_lifetime_guard("sn", "bad", prev) is None

    coord.energy._lifetime_guard["sn"].last = None
    assert coord.energy._apply_lifetime_guard("sn", -1.0, None) == 0.0

    coord.energy._lifetime_guard["sn"].last = 5.0
    assert (
        coord.energy._apply_lifetime_guard("sn", 4.7, None) == coord.energy._lifetime_guard["sn"].last
    )


def test_determine_polling_state_handles_bad_options(coordinator_factory):
    coord = coordinator_factory()

    class WeirdOptions(dict):
        def get(self, key, default=None):  # noqa: D401
            if key == OPT_FAST_WHILE_STREAMING:
                raise ValueError("bad")
            return super().get(key, default)

    coord.config_entry = SimpleNamespace(
        options=WeirdOptions(
            {
                OPT_FAST_POLL_INTERVAL: "bogus",
                OPT_SLOW_POLL_INTERVAL: "oops",
            }
        ),
        data={},
        entry_id="1",
    )
    coord.update_interval = timedelta(seconds=15)
    state = coord._determine_polling_state({})
    assert state["want_fast"] in (True, False)


@pytest.mark.asyncio
async def test_async_resolve_charge_modes_skips_empty_serials(coordinator_factory):
    coord = coordinator_factory()
    coord._charge_mode_cache.clear()
    coord._get_charge_mode = AsyncMock(return_value="IDLE")
    result = await coord._async_resolve_charge_modes(["", RANDOM_SERIAL])
    assert result.get(RANDOM_SERIAL) == "IDLE"


def test_has_embedded_charge_mode_for_non_dict(coordinator_factory):
    coord = coordinator_factory()
    assert coord._has_embedded_charge_mode([]) is False


def test_persist_tokens_handles_missing_entry(coordinator_factory):
    coord = coordinator_factory()
    coord.config_entry = None
    coord._persist_tokens(AuthTokens(None, None, None, None))


def test_persist_tokens_drops_none_fields(coordinator_factory):
    coord = coordinator_factory()
    merged = {}
    coord.config_entry = SimpleNamespace(data={}, entry_id="1")

    def _update(entry, *, data):
        merged.update(data)

    coord.hass.config_entries.async_update_entry = _update
    coord._persist_tokens(
        AuthTokens(
            cookie=None, access_token="tok", session_id=None, token_expires_at=None
        )
    )
    assert merged.get("access_token") == "tok"
    assert merged.get(CONF_COOKIE) == ""


def test_set_charging_expectation_handles_invalid_hold(coordinator_factory):
    coord = coordinator_factory()
    coord.set_charging_expectation(RANDOM_SERIAL, True, hold_for="bad")
    assert RANDOM_SERIAL in coord._pending_charging


def test_slow_interval_floor_handles_bad_update_interval(coordinator_factory):
    coord = coordinator_factory()
    coord.config_entry = SimpleNamespace(options={})

    class BadInterval:
        def total_seconds(self):
            raise ValueError("boom")

    coord.__dict__["update_interval"] = BadInterval()
    assert coord._slow_interval_floor() >= 1


@pytest.mark.asyncio
async def test_schedule_backoff_timer_handles_invalid_now(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(coord_mod.dt_util, "utcnow", _raise)
    coord.hass.async_create_task = MagicMock(
        side_effect=lambda coro: (coro.close(), None)[1]
    )
    coord._schedule_backoff_timer(1)


def test_require_plugged_handles_bad_data(coordinator_factory):
    coord = coordinator_factory()
    coord.data = object()
    with pytest.raises(ServiceValidationError):
        coord.require_plugged(RANDOM_SERIAL)


def test_ensure_serial_tracked_invalid_inputs(coordinator_factory):
    coord = coordinator_factory()
    assert coord._ensure_serial_tracked(None) is False

    class BadSerial:
        def __str__(self):
            raise ValueError("boom")

    assert coord._ensure_serial_tracked(BadSerial()) is False

    coord.serials = {RANDOM_SERIAL}
    coord._serial_order = []
    assert coord._ensure_serial_tracked(RANDOM_SERIAL) is False
    assert RANDOM_SERIAL in coord._serial_order


def test_iter_serials_falls_back_to_sorted(coordinator_factory):
    coord = coordinator_factory()
    coord._serial_order = []
    coord.serials = {"B", "A"}
    ordered = coord.iter_serials()
    assert ordered[:2] == ["A", "B"]


def test_iter_serials_empty_when_site_only(coordinator_factory):
    coord = coordinator_factory()
    coord.serials = {"A"}
    coord._serial_order = ["A"]
    coord.site_only = True
    assert coord.iter_serials() == []


def test_coerce_amp_and_amp_limits(coordinator_factory):
    coord = coordinator_factory()
    assert coord._coerce_amp("   ") is None
    assert coord._coerce_amp([]) is None
    coord.data = object()
    min_amp, max_amp = coord._amp_limits(RANDOM_SERIAL)
    assert min_amp is None and max_amp is None


def test_pick_start_amps_handles_invalid_data(coordinator_factory):
    coord = coordinator_factory()
    coord.data = object()
    coord.last_set_amps.clear()
    assert coord.pick_start_amps(RANDOM_SERIAL, requested=None, fallback=16) == 16


def test_pick_start_amps_uses_fallback_default(coordinator_factory):
    coord = coordinator_factory()
    coord.data = {}
    coord.last_set_amps.clear()
    assert coord.pick_start_amps(RANDOM_SERIAL, requested=None, fallback="bad") == 32
