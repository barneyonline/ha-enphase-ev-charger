from __future__ import annotations

import pytest

try:
    from homeassistant.exceptions import ServiceValidationError
except ImportError:  # pragma: no cover - older HA cores
    from homeassistant.exceptions import HomeAssistantError

    class ServiceValidationError(HomeAssistantError):
        """Fallback for environments lacking ServiceValidationError."""

        def __init__(
            self,
            message: str | None = None,
            *,
            translation_domain: str | None = None,
            translation_key: str | None = None,
            translation_placeholders: dict[str, object] | None = None,
            **_: object,
        ) -> None:
            super().__init__(message)
            self.translation_domain = translation_domain
            self.translation_key = translation_key
            self.translation_placeholders = translation_placeholders

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


@pytest.mark.asyncio
async def test_start_stop_buttons_press(hass, monkeypatch):
    from custom_components.enphase_ev.button import StartChargeButton, StopChargeButton
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_SCAN_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    sn = RANDOM_SERIAL
    coord.data = {
        sn: {
            "name": "Garage EV",
            "charging": False,
            "max_amp": 16,
            "min_amp": 6,
            "plugged": True,
        }
    }
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.start_calls = []
            self.stop_calls = []
            self.stream_start_calls = 0

        async def start_charging(
            self,
            s,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((s, amps, connector_id))
            return {"status": "ok"}

        async def stop_charging(self, s):
            self.stop_calls.append(s)
            return {"status": "ok"}

        async def start_live_stream(self):
            self.stream_start_calls += 1
            return {"status": "accepted", "duration_s": 900}

    coord.client = StubClient()

    # Avoid debouncer refresh
    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore

    start_btn = StartChargeButton(coord, sn)
    stop_btn = StopChargeButton(coord, sn)

    # Start button clamps to device max when no prior setpoint exists
    await start_btn.async_press()
    assert coord.client.start_calls[-1] == (sn, 16, 1)
    assert coord.last_set_amps[sn] == 16
    # Stop button calls API
    await stop_btn.async_press()
    assert coord.client.stop_calls[-1] == sn


@pytest.mark.asyncio
async def test_start_button_requires_plugged(hass, monkeypatch):
    from custom_components.enphase_ev.button import StartChargeButton
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_SCAN_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    sn = RANDOM_SERIAL
    coord.data = {
        sn: {
            "name": "Garage EV",
            "charging": False,
            "max_amp": 16,
            "min_amp": 6,
            "plugged": False,
        }
    }
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.start_calls = []

        async def start_charging(
            self,
            s,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((s, amps, connector_id))
            return {"status": "ok"}

    coord.client = StubClient()

    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore

    start_btn = StartChargeButton(coord, sn)

    with pytest.raises(ServiceValidationError):
        await start_btn.async_press()
    assert coord.client.start_calls == []


@pytest.mark.asyncio
async def test_start_button_requires_authentication(hass, monkeypatch):
    from custom_components.enphase_ev.button import StartChargeButton
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_SCAN_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    sn = RANDOM_SERIAL
    coord.data = {
        sn: {
            "name": "Garage EV",
            "charging": False,
            "max_amp": 16,
            "min_amp": 6,
            "plugged": True,
            "auth_required": True,
        }
    }
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.start_calls = []

        async def start_charging(
            self,
            s,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((s, amps, connector_id))
            return {"status": "ok"}

    coord.client = StubClient()

    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore

    start_btn = StartChargeButton(coord, sn)

    with pytest.raises(ServiceValidationError) as err:
        await start_btn.async_press()
    assert coord.client.start_calls == []
    assert getattr(err.value, "translation_key", "") == "exceptions.auth_required"


@pytest.mark.asyncio
async def test_start_button_skips_expectation_when_not_ready(hass, monkeypatch):
    from custom_components.enphase_ev.button import StartChargeButton
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_SCAN_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    sn = RANDOM_SERIAL
    coord.data = {
        sn: {
            "name": "Garage EV",
            "charging": False,
            "max_amp": 16,
            "min_amp": 6,
            "plugged": True,
        }
    }
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.start_calls = []

        async def start_charging(
            self,
            s,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((s, amps, connector_id))
            return {"status": "not_ready"}

    coord.client = StubClient()

    expectation_calls = []

    def _record_expectation(sn_arg, state, hold_for=0):
        expectation_calls.append((sn_arg, state, hold_for))

    coord.set_charging_expectation = _record_expectation  # type: ignore

    flags = {"kick": False, "refresh": False}

    def _kick_fast(duration):
        flags["kick"] = True

    async def _refresh():
        flags["refresh"] = True

    coord.kick_fast = _kick_fast  # type: ignore
    coord.async_request_refresh = _refresh  # type: ignore

    start_btn = StartChargeButton(coord, sn)
    await start_btn.async_press()

    assert coord.client.start_calls == [(sn, 16, 1)]
    assert expectation_calls == []
    assert flags["kick"] is False
    assert flags["refresh"] is False


@pytest.mark.asyncio
async def test_kick_fast_window(hass, monkeypatch):
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

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self, options):
            self.options = options

        def async_on_unload(self, cb):
            return None

    options = {OPT_FAST_POLL_INTERVAL: 5, OPT_SLOW_POLL_INTERVAL: 20}
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

    # Idle payload (would normally be slow)
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

    # Trigger fast window explicitly
    coord.kick_fast(60)
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 5


@pytest.mark.asyncio
async def test_button_platform_async_setup_entry_filters_known_serials(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.button import (
        StartChargeButton,
        StopChargeButton,
        async_setup_entry,
    )
    from custom_components.enphase_ev.const import DOMAIN

    coord = coordinator_factory(serials=["5555"])
    added: list[list[object]] = []
    listeners: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_listener(callback, *, context=None):
        listeners.append(callback)

        def _remove():
            listeners.remove(callback)

        return _remove

    coord.async_add_listener = capture_listener  # type: ignore[attr-defined]
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    await async_setup_entry(hass, config_entry, capture_add)
    assert len(added) == 1
    start_entity, stop_entity = added[0]
    assert isinstance(start_entity, StartChargeButton)
    assert isinstance(stop_entity, StopChargeButton)
    assert start_entity._sn == stop_entity._sn == "5555"
    assert len(listeners) == 1

    added.clear()
    listeners[0]()
    assert added == []

    coord._ensure_serial_tracked("6666")
    coord.data["6666"] = {"sn": "6666", "name": "Aux Charger"}
    listeners[0]()

    assert len(added) == 1
    serials = {entity._sn for entity in added[0]}
    assert serials == {"6666"}
