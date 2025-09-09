import aiohttp
import pytest


@pytest.mark.asyncio
async def test_backoff_on_429(monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from homeassistant.helpers.update_coordinator import UpdateFailed

    cfg = {
        CONF_SITE_ID: "3381244",
        CONF_SERIALS: ["482522020944"],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    from custom_components.enphase_ev import coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    coord = EnphaseCoordinator(object(), cfg)

    class StubRespErr(aiohttp.ClientResponseError):
        def __init__(self, status, headers=None):
            req = aiohttp.RequestInfo(url=aiohttp.client.URL("https://example"), method="GET", headers={}, real_url=aiohttp.client.URL("https://example"))
            super().__init__(request_info=req, history=(), status=status, message="", headers=headers or {})

    class StubClient:
        async def status(self):
            raise StubRespErr(429, headers={"Retry-After": "1"})

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None


@pytest.mark.asyncio
async def test_dynamic_poll_switch(monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_FAST_POLL_INTERVAL,
        OPT_SLOW_POLL_INTERVAL,
    )
    # no extra imports

    cfg = {
        CONF_SITE_ID: "3381244",
        CONF_SERIALS: ["482522020944"],
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
    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    coord = EnphaseCoordinator(object(), cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload
        async def status(self):
            return self._payload

    # Charging -> fast
    payload_charging = {
        "evChargerData": [
            {
                "sn": "482522020944",
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_charging)
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 5

    # Idle -> slow
    payload_idle = {
        "evChargerData": [
            {
                "sn": "482522020944",
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_idle)
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 20


@pytest.mark.asyncio
async def test_streaming_prefers_fast(monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_FAST_POLL_INTERVAL,
        OPT_SLOW_POLL_INTERVAL,
        OPT_FAST_WHILE_STREAMING,
    )
    # no extra imports

    cfg = {
        CONF_SITE_ID: "3381244",
        CONF_SERIALS: ["482522020944"],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    options = {OPT_FAST_POLL_INTERVAL: 6, OPT_SLOW_POLL_INTERVAL: 22, OPT_FAST_WHILE_STREAMING: True}
    class DummyEntry:
        def __init__(self, options):
            self.options = options
        def async_on_unload(self, cb):
            return None
    entry = DummyEntry(options)
    from custom_components.enphase_ev import coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    coord = EnphaseCoordinator(object(), cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload
        async def status(self):
            return self._payload

    payload_idle = {
        "evChargerData": [
            {
                "sn": "482522020944",
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
