
import pytest

pytest.importorskip("pytest_asyncio")
from custom_components.enphase_ev.api import EnphaseEVClient

class StubClient(EnphaseEVClient):
    def __init__(self, site_id="9990001"):
        from aiohttp import ClientSession
        self.calls = []
        super().__init__(ClientSession(), site_id, "EAUTH", "COOKIE")

    async def _json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        return {"status": "ok"}

@pytest.mark.asyncio
async def test_api_builds_urls_correctly():
    c = StubClient(site_id="9990001")
    await c.status()
    await c.start_charging("499900000001", 32, connector_id=1)
    await c.stop_charging("499900000001")
    await c.trigger_message("499900000001", "MeterValues")

    methods_urls = [ (m,u) for (m,u,_) in c.calls ]
    # First call may fall back to alternative path; accept either
    assert methods_urls[0][0] == "GET"
    assert "/service/evse_controller/9990001/ev_chargers/status" in methods_urls[0][1] or "/service/evse_controller/9990001/ev_charger/status" in methods_urls[0][1]
    # Next three calls should be start/stop/trigger in order, regardless of fallback GETs
    start_call = methods_urls[-3]
    stop_call = methods_urls[-2]
    trig_call = methods_urls[-1]
    assert start_call[0] == "POST" and "/service/evse_controller/9990001/ev_chargers/499900000001/start_charging" in start_call[1]
    assert stop_call[0] == "PUT" and "/service/evse_controller/9990001/ev_chargers/499900000001/stop_charging" in stop_call[1]
    assert trig_call[0] == "POST" and "/service/evse_controller/9990001/ev_charger/499900000001/trigger_message" in trig_call[1]

    _, _, payload = c.calls[-3]
    assert payload == {"chargingLevel": 32, "connectorId": 1}
