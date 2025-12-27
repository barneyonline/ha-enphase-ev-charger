import datetime as _dt

import pytest


@pytest.mark.asyncio
async def test_power_restore_continues_from_last_sample(monkeypatch):
    from homeassistant.helpers.update_coordinator import CoordinatorEntity
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = "555555555555"

    # Prepare a fixed day and a prior sample at t0
    t0 = _dt.datetime(2025, 9, 9, 10, 0, 0, tzinfo=_dt.timezone.utc)
    today_str = t0.strftime("%Y-%m-%d")
    last_ts = t0.timestamp()

    # Build coordinator stub and initial data
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {
        sn: {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 10.5,
            "operating_v": 230,
            "charging": True,
        }
    }
    coord.serials = {sn}
    coord.site_id = "1234567"
    coord.last_update_success = True

    ent = EnphasePowerSensor(coord, sn)

    # Provide a fake last state via monkeypatch without relying on hass restore cache
    class _FakeState:
        def __init__(self, state: str, attrs: dict):
            self.state = state
            self.attributes = attrs

    async def _fake_get_last_state(self):
        return _FakeState(
            "3600",
            {
                "baseline_kwh": 10.0,
                "baseline_day": today_str,
                "last_energy_today_kwh": 0.5,
                "last_ts": last_ts,
            },
        )

    # Avoid calling the CoordinatorEntity async_added_to_hass implementation
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(EnphasePowerSensor, "async_get_last_state", _fake_get_last_state)
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)

    await ent.async_added_to_hass()

    # Advance time by 60s and increase lifetime by 0.1 kWh → 6000 W
    t1 = t0 + _dt.timedelta(seconds=60)
    monkeypatch.setattr(dt_util, "now", lambda: t1)
    coord.data[sn]["lifetime_kwh"] = 10.6

    assert ent.native_value == 6000
