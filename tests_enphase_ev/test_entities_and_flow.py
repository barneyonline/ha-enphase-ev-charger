import pytest


def test_power_sensor_device_class():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor
    class Dummy:
        data = {}
    ent = EnphasePowerSensor(Dummy(), "4825")
    assert ent.device_class == "power"


@pytest.mark.asyncio
async def test_config_flow_form():
    from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow
    flow = EnphaseEVConfigFlow()
    flow.hass = object()
    res = await flow.async_step_user()
    assert res["type"].name == "FORM"
    assert res["step_id"] == "user"
