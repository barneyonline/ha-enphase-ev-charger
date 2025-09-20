import pytest


@pytest.mark.asyncio
async def test_power_estimates_from_amps_when_missing(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_FAST_POLL_INTERVAL,
        OPT_NOMINAL_VOLTAGE,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: "3381244",
        CONF_SERIALS: ["482522020944"],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    options = {OPT_NOMINAL_VOLTAGE: 240, OPT_FAST_POLL_INTERVAL: 5, OPT_SLOW_POLL_INTERVAL: 20}

    class DummyEntry:
        def __init__(self, options):
            self.options = options
        def async_on_unload(self, cb):
            return None

    entry = DummyEntry(options)

    from custom_components.enphase_ev import coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload
        async def status(self):
            return self._payload

    payload = {
        "evChargerData": [
            {
                "sn": "482522020944",
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
                # No power keys here; coordinator should estimate from chargingLevel
                "chargingLevel": 16,
            }
        ],
        "ts": 1725600423,
    }
    coord.client = StubClient(payload)
    out = await coord._async_update_data()
    sn = "482522020944"
    assert out[sn]["power_w"] == 16 * 240
