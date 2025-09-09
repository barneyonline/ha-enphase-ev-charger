import pytest


@pytest.mark.asyncio
async def test_power_uses_operating_voltage_when_present(monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_NOMINAL_VOLTAGE,
    )

    cfg = {
        CONF_SITE_ID: "1234567",
        CONF_SERIALS: ["555555555555"],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    coord = EnphaseCoordinator(object(), cfg)

    sn = "555555555555"

    status_payload = {
        "evChargerData": [
            {
                "sn": sn,
                "name": "Garage EV",
                "connected": True,
                "pluggedIn": True,
                "charging": True,
                # Estimate from amps when power missing
                "chargingLevel": 16,
                "connectors": [{"connectorStatusType": "CHARGING", "dlbActive": False}],
            }
        ],
        "ts": 1725600423,
    }

    summary_list = [
        {
            "serialNumber": sn,
            "operatingVoltage": "230",
            "lifeTimeConsumption": 10000,
        }
    ]

    class StubClient:
        async def status(self):
            return status_payload

        async def summary_v2(self):
            return summary_list

        async def charge_mode(self, sn: str):
            return None

    coord.client = StubClient()

    data = await coord._async_update_data()
    assert data[sn]["power_w"] == 16 * 230


def test_energy_today_sensor_name_and_value():
    from custom_components.enphase_ev.sensor import EnphaseSessionEnergySensor
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    # Minimal coordinator stub with daily kWh present
    sn = "499900000001"
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: {"sn": sn, "name": "IQ EV Charger", "session_kwh": 3.25}}
    coord.serials = {sn}

    ent = EnphaseSessionEnergySensor(coord, sn)
    assert ent.name == "Energy Today"
    assert ent.native_value == 3.25
