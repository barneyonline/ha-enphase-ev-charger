import aiohttp
import pytest
from aiohttp import client_reqrep
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from tests_enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


class DummyEntry:
    def __init__(self, options, data):
        self.options = options
        self.data = data

    def async_on_unload(self, cb):
        return None


class FailingClient:
    def __init__(self, status):
        self._status = status

    async def status(self):
        request_info = client_reqrep.RequestInfo(
            url=URL("https://example.com/status"),
            method="GET",
            headers=CIMultiDictProxy(CIMultiDict()),
            real_url=URL("https://example.com/status"),
        )
        raise aiohttp.ClientResponseError(
            request_info=request_info,
            history=(),
            status=self._status,
            message="Internal Server Error",
            headers={},
        )


@pytest.mark.asyncio
async def test_server_error_backoff_respects_configured_slow_interval(hass, monkeypatch):
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
    from homeassistant.helpers.update_coordinator import UpdateFailed

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 45,
    }
    options = {OPT_FAST_POLL_INTERVAL: 10, OPT_SLOW_POLL_INTERVAL: 120}
    entry = DummyEntry(options, dict(cfg))

    # Freeze time.monotonic so backoff duration is deterministic
    monotonic_val = [1000.0]

    def fake_monotonic():
        return monotonic_val[0]

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.time.monotonic", fake_monotonic
    )

    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)
    coord.client = FailingClient(status=500)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    first_backoff = coord._backoff_until - monotonic_val[0]
    assert first_backoff >= 120

    # Fast-forward beyond backoff window and repeat to ensure exponential growth
    monotonic_val[0] = coord._backoff_until + 1
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    second_backoff = coord._backoff_until - monotonic_val[0]
    assert second_backoff >= 240
