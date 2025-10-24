import asyncio
import time
from datetime import datetime, timezone
import aiohttp
import pytest

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


@pytest.mark.asyncio
async def test_backoff_on_429(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

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
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(hass, cfg)

    class StubRespErr(aiohttp.ClientResponseError):
        def __init__(self, status, headers=None):
            req = aiohttp.RequestInfo(
                url=aiohttp.client.URL("https://example"),
                method="GET",
                headers={},
                real_url=aiohttp.client.URL("https://example"),
            )
            super().__init__(
                request_info=req,
                history=(),
                status=status,
                message="",
                headers=headers or {},
            )

    class StubClient:
        async def status(self):
            raise StubRespErr(429, headers={"Retry-After": "1"})

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None


@pytest.mark.asyncio
async def test_http_error_issue(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        ISSUE_CLOUD_ERRORS,
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
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(hass, cfg)

    class StubRespErr(aiohttp.ClientResponseError):
        def __init__(self, status):
            req = aiohttp.RequestInfo(
                url=aiohttp.client.URL("https://example"),
                method="GET",
                headers={},
                real_url=aiohttp.client.URL("https://example"),
            )
            super().__init__(
                request_info=req,
                history=(),
                status=status,
                message="",
                headers={},
            )

    class FailingClient:
        async def status(self):
            raise StubRespErr(503)

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

    assert any(issue_id == ISSUE_CLOUD_ERRORS for _, issue_id, _ in created)

    class SuccessClient:
        async def status(self):
            return {"evChargerData": []}

    coord.client = SuccessClient()
    coord._backoff_until = None
    await coord._async_update_data()

    assert any(issue_id == ISSUE_CLOUD_ERRORS for _, issue_id in deleted)


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

    # Idle -> slow
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

    # Newer firmware reports additional suspended variants that should still
    # be treated as an active charging session
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
    assert coord.data[RANDOM_SERIAL]["charging"] is True


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
