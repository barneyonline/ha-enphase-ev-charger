import pytest


@pytest.mark.asyncio
async def test_charging_amps_number_reads_and_sets(monkeypatch):
    from custom_components.enphase_ev.number import ChargingAmpsNumber
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )

    cfg = {
        CONF_SITE_ID: "3381244",
        CONF_SERIALS: ["482522020944"],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    coord = EnphaseCoordinator(object(), cfg)
    sn = "482522020944"
    # Populate coordinator data with min/max
    coord.data = {sn: {"name": "Garage EV", "charging_level": None, "min_amp": 6, "max_amp": 40}}
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.calls = []
        async def start_charging(self, s, amps, connector_id=1):
            self.calls.append((s, amps, connector_id))
            return {"status": "ok"}

    coord.client = StubClient()

    # Avoid debouncer refresh
    async def _noop():
        return None
    coord.async_request_refresh = _noop  # type: ignore

    ent = ChargingAmpsNumber(coord, sn)
    # Unknown -> uses last_set_amps (0)
    assert ent.native_value == 0.0
    assert ent.native_min_value == 6.0
    assert ent.native_max_value == 40.0

    await ent.async_set_native_value(24)
    assert coord.client.calls[-1] == (sn, 24, 1)
    # Last set amps updated
    assert coord.last_set_amps[sn] == 24


@pytest.mark.asyncio
async def test_charging_switch_turn_on_off(monkeypatch):
    from custom_components.enphase_ev.switch import ChargingSwitch
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )

    cfg = {
        CONF_SITE_ID: "3381244",
        CONF_SERIALS: ["482522020944"],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    coord = EnphaseCoordinator(object(), cfg)
    sn = "482522020944"
    coord.data = {sn: {"name": "Garage EV", "charging": False}}
    coord.last_set_amps = {sn: 32}

    class StubClient:
        def __init__(self):
            self.start_calls = []
            self.stop_calls = []
        async def start_charging(self, s, amps, connector_id=1):
            self.start_calls.append((s, amps, connector_id))
            return {"status": "ok"}
        async def stop_charging(self, s):
            self.stop_calls.append(s)
            return {"status": "ok"}

    coord.client = StubClient()

    async def _noop():
        return None
    coord.async_request_refresh = _noop  # type: ignore

    sw = ChargingSwitch(coord, sn)
    assert sw.is_on is False

    await sw.async_turn_on()
    assert coord.client.start_calls[-1] == (sn, 32, 1)

    await sw.async_turn_off()
    assert coord.client.stop_calls[-1] == sn

