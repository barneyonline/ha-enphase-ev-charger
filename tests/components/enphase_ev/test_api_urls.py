from unittest.mock import MagicMock

import pytest

from custom_components.enphase_ev.api import EnphaseEVClient
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


class StubClient(EnphaseEVClient):
    def __init__(self, site_id=RANDOM_SITE_ID):
        self.calls = []
        super().__init__(MagicMock(), site_id, "EAUTH", "COOKIE")

    async def _json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_api_builds_urls_correctly():
    c = StubClient(site_id=RANDOM_SITE_ID)
    await c.status()
    await c.battery_status()
    await c.grid_control_check()
    await c.inverters_inventory()
    await c.inverter_status()
    await c.inverter_production(start_date="2022-01-01", end_date="2026-01-01")
    await c.start_charging(RANDOM_SERIAL, 32, connector_id=1)
    await c.stop_charging(RANDOM_SERIAL)
    await c.trigger_message(RANDOM_SERIAL, "MeterValues")

    methods_urls = [(method, url) for (method, url, _) in c.calls]
    # First call should hit the primary status endpoint
    assert methods_urls[0][0] == "GET"
    assert (
        f"/service/evse_controller/{RANDOM_SITE_ID}/ev_chargers/status"
        in methods_urls[0][1]
    )
    assert methods_urls[1][0] == "GET"
    assert f"/pv/settings/{RANDOM_SITE_ID}/battery_status.json" in methods_urls[1][1]
    assert methods_urls[2][0] == "GET"
    assert f"/app-api/{RANDOM_SITE_ID}/grid_control_check.json" in methods_urls[2][1]
    assert methods_urls[3][0] == "GET"
    assert f"/app-api/{RANDOM_SITE_ID}/inverters.json" in methods_urls[3][1]
    assert methods_urls[4][0] == "GET"
    assert f"/systems/{RANDOM_SITE_ID}/inverter_status_x.json" in methods_urls[4][1]
    assert methods_urls[5][0] == "GET"
    assert f"/systems/{RANDOM_SITE_ID}/inverter_data_x/energy.json" in methods_urls[5][1]
    # Final three calls should be start/stop/trigger in order, regardless of fallback GETs
    start_call = methods_urls[-3]
    stop_call = methods_urls[-2]
    trig_call = methods_urls[-1]
    assert start_call[0] == "POST"
    assert (
        f"/service/evse_controller/{RANDOM_SITE_ID}/ev_chargers/{RANDOM_SERIAL}/start_charging"
        in start_call[1]
    )
    assert stop_call[0] == "PUT"
    assert (
        f"/service/evse_controller/{RANDOM_SITE_ID}/ev_chargers/{RANDOM_SERIAL}/stop_charging"
        in stop_call[1]
    )
    assert trig_call[0] == "POST"
    assert (
        f"/service/evse_controller/{RANDOM_SITE_ID}/ev_charger/{RANDOM_SERIAL}/trigger_message"
        in trig_call[1]
    )

    _, _, payload = c.calls[-3]
    assert payload == {"chargingLevel": 32, "connectorId": 1}
