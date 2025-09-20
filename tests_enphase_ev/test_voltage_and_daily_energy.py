

def test_power_derived_from_energy_today(monkeypatch):
    import datetime as _dt

    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = "555555555555"
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: {"sn": sn, "name": "Garage EV", "lifetime_kwh": 10.0, "operating_v": 230}}
    coord.serials = {sn}

    ent = EnphasePowerSensor(coord, sn)

    # Freeze time at t0 and seed baseline → first read returns 0
    t0 = _dt.datetime(2025, 9, 9, 10, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: t0)
    assert ent.native_value == 0

    # After 120 seconds, lifetime increases by 0.24 kWh → 0.24*3_600_000/120 = 7200 W
    t1 = t0 + _dt.timedelta(seconds=120)
    monkeypatch.setattr(dt_util, "now", lambda: t1)
    coord.data[sn]["lifetime_kwh"] = 10.24
    assert ent.native_value == 7200


def test_energy_today_sensor_name_and_value(monkeypatch):
    import datetime as _dt

    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    # Minimal coordinator stub with lifetime kWh present
    sn = "482522020944"
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: {"sn": sn, "name": "IQ EV Charger", "lifetime_kwh": 10.0}}
    coord.serials = {sn}

    # Freeze to deterministic date
    monkeypatch.setattr(dt_util, "now", lambda: _dt.datetime(2025, 9, 9, 10, 0, 0, tzinfo=_dt.timezone.utc))

    ent = EnphaseEnergyTodaySensor(coord, sn)
    assert ent.name == "Energy Today"
    # First read establishes baseline → 0.0 today
    assert ent.native_value == 0.0
