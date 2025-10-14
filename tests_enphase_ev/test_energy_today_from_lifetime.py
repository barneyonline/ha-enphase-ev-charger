import datetime as _dt

from tests_enphase_ev.random_ids import RANDOM_SERIAL


def test_energy_today_from_lifetime_monotonic(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: {"sn": sn, "name": "IQ EV Charger", "lifetime_kwh": 100.0}}
    coord.serials = {sn}

    # Freeze time to a specific day
    day1 = _dt.datetime(2025, 9, 9, 10, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day1)

    ent = EnphaseEnergyTodaySensor(coord, sn)

    # First read establishes baseline; should be 0.0
    assert ent.native_value == 0.0

    # Increase lifetime by 1.5 kWh → today should reflect delta
    coord.data[sn]["lifetime_kwh"] = 101.5
    assert ent.native_value == 1.5

    # Minor jitter down should not decrease today's value
    coord.data[sn]["lifetime_kwh"] = 101.49
    assert ent.native_value == 1.5

    # Next day: baseline resets and value starts from 0 again
    day2 = _dt.datetime(2025, 9, 10, 0, 1, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day2)
    coord.data[sn]["lifetime_kwh"] = 103.0
    assert ent.native_value == 0.0
    coord.data[sn]["lifetime_kwh"] = 104.2
    assert ent.native_value == 1.2
