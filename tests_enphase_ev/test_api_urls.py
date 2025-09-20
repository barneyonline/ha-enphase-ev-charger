from unittest.mock import MagicMock

import pytest

from custom_components.enphase_ev.api import EnphaseEVClient


class StubClient(EnphaseEVClient):
    def __init__(self, site_id="3381244"):
        self.calls = []
        super().__init__(MagicMock(), site_id, "EAUTH", "COOKIE")

    async def _json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        return {"status": "ok"}

@pytest.mark.asyncio
async def test_api_builds_urls_correctly():
    c = StubClient(site_id="3381244")
    await c.status()
    await c.start_charging("482522020944", 32, connector_id=1)
    await c.stop_charging("482522020944")
    await c.trigger_message("482522020944", "MeterValues")

    methods_urls = [(method, url) for (method, url, _) in c.calls]
    # First call may fall back to alternative path; accept either
    assert methods_urls[0][0] == "GET"
    assert any(
        fragment in methods_urls[0][1]
        for fragment in (
            "/service/evse_controller/3381244/ev_chargers/status",
            "/service/evse_controller/3381244/ev_charger/status",
        )
    )
    # Next three calls should be start/stop/trigger in order, regardless of fallback GETs
    start_call = methods_urls[-3]
    stop_call = methods_urls[-2]
    trig_call = methods_urls[-1]
    assert start_call[0] == "POST"
    assert (
        "/service/evse_controller/3381244/ev_chargers/482522020944/start_charging"
        in start_call[1]
    )
    assert stop_call[0] == "PUT"
    assert (
        "/service/evse_controller/3381244/ev_chargers/482522020944/stop_charging"
        in stop_call[1]
    )
    assert trig_call[0] == "POST"
    assert (
        "/service/evse_controller/3381244/ev_charger/482522020944/trigger_message"
        in trig_call[1]
    )

    _, _, payload = c.calls[-3]
    assert payload == {"chargingLevel": 32, "connectorId": 1}
