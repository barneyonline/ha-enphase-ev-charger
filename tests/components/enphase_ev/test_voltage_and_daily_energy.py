from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SERIAL_ALT


def test_power_derived_from_energy_today(monkeypatch):
    import datetime as _dt

    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL_ALT
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {
        sn: {"sn": sn, "name": "Garage EV", "lifetime_kwh": 10.0, "operating_v": 230}
    }
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
    sn = RANDOM_SERIAL
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: {"sn": sn, "name": "IQ EV Charger", "lifetime_kwh": 10.0}}
    coord.serials = {sn}

    # Freeze to deterministic date
    monkeypatch.setattr(
        dt_util,
        "now",
        lambda: _dt.datetime(2025, 9, 9, 10, 0, 0, tzinfo=_dt.timezone.utc),
    )

    ent = EnphaseEnergyTodaySensor(coord, sn)
    assert ent.name == "Energy Today"
    # First read establishes baseline → 0.0 today
    assert ent.native_value == 0.0


def test_energy_today_sensor_session_attributes(monkeypatch):
    from datetime import timezone as _tz

    import datetime as _dt

    from homeassistant.const import UnitOfLength
    from homeassistant.util import dt as dt_util
    from homeassistant.util.unit_conversion import DistanceConverter

    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    # Ensure localized timestamps remain deterministic within the test
    monkeypatch.setattr(
        dt_util, "as_local", lambda dt: dt.replace(tzinfo=_tz.utc)  # type: ignore[override]
    )
    base_utc = _dt.datetime(2025, 10, 24, 20, 0, 0, tzinfo=_tz.utc)
    monkeypatch.setattr(
        dt_util,
        "utcnow",
        lambda: base_utc,
    )

    sn = RANDOM_SERIAL_ALT

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {sn}
            self.site_id = "site"
            self.last_update_success = True

    coord = DummyCoord()
    coord.data[sn] = {
        "sn": sn,
        "name": "Garage EV",
        "lifetime_kwh": 12.0,
        "session_plug_in_at": "2025-10-24T20:00:00.000Z[UTC]",
        "session_plug_out_at": "2025-10-24T22:30:15.000Z[UTC]",
        "session_energy_wh": 3561.53,
        "session_miles": 14.35368,
        "session_cost": 4.75,
        "session_charge_level": 32,
    }

    ent = EnphaseEnergyTodaySensor(coord, sn)

    class DummyUnits:
        length_unit = UnitOfLength.KILOMETERS

    class DummyConfig:
        units = DummyUnits()

    class DummyHass:
        config = DummyConfig()

    ent.hass = DummyHass()  # type: ignore[assignment]

    assert ent.native_value == round(3561.53 / 1000.0, 3)
    attrs = ent.extra_state_attributes
    assert attrs["plugged_in_at"] == "2025-10-24T20:00:00+00:00"
    assert attrs["plugged_out_at"] == "2025-10-24T22:30:15+00:00"
    assert attrs["energy_consumed_wh"] == 3561.53
    expected_km = round(
        DistanceConverter.convert(
            14.35368, UnitOfLength.MILES, UnitOfLength.KILOMETERS
        ),
        3,
    )
    assert attrs["range_added"] == expected_km
    assert attrs["session_cost"] == 4.75
    assert attrs["session_charge_level"] == 32


def test_energy_today_sensor_session_attributes_kwh(monkeypatch):
    import datetime as _dt

    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {sn}
            self.site_id = "site"
            self.last_update_success = True

    coord = DummyCoord()
    base_time = _dt.datetime(2025, 10, 25, 9, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: base_time)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_time)
    coord.data[sn] = {
        "sn": sn,
        "name": "Garage EV",
        "session_kwh": 3.75,
        "session_energy_wh": 3.75,
    }

    ent = EnphaseEnergyTodaySensor(coord, sn)
    attrs = ent.extra_state_attributes
    assert attrs["energy_consumed_kwh"] == 3.75
    assert attrs["energy_consumed_wh"] == 3750.0


def test_energy_today_sensor_falls_back_to_lifetime(monkeypatch):
    import datetime as _dt

    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: {"sn": sn, "name": "Garage EV", "lifetime_kwh": 15.0}}
    coord.serials = {sn}

    monkeypatch.setattr(
        dt_util,
        "now",
        lambda: _dt.datetime(2025, 10, 25, 10, 0, 0, tzinfo=_dt.timezone.utc),
    )

    ent = EnphaseEnergyTodaySensor(coord, sn)
    assert ent.native_value == 0.0
    coord.data[sn]["lifetime_kwh"] = 15.5
    assert ent.native_value == 0.5


def test_energy_today_sensor_status_reset(monkeypatch):
    import datetime as _dt

    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {sn}
            self.site_id = "site"
            self.last_update_success = True

    coord = DummyCoord()
    base_time = _dt.datetime(2025, 10, 25, 8, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: base_time)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_time)
    coord.data[sn] = {
        "sn": sn,
        "name": "Garage EV",
        "session_kwh": 3.0,
        "session_energy_wh": 3000.0,
    }

    ent = EnphaseEnergyTodaySensor(coord, sn)
    assert ent.native_value == 3.0

    # Simulate end of session/reset to zero
    next_time = base_time + _dt.timedelta(minutes=5)
    monkeypatch.setattr(dt_util, "now", lambda: next_time)
    monkeypatch.setattr(dt_util, "utcnow", lambda: next_time)
    coord.data[sn]["session_kwh"] = 0.0
    coord.data[sn]["session_energy_wh"] = 0.0

    assert ent.native_value == 0.0
    attrs = ent.extra_state_attributes
    assert "last_reset_at" not in attrs


def test_energy_today_rollover_without_session_timestamps(monkeypatch):
    import datetime as _dt

    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {sn}
            self.site_id = "site"
            self.last_update_success = True

    coord = DummyCoord()
    yesterday = _dt.datetime(2025, 11, 1, 23, 30, 0, tzinfo=_dt.timezone.utc)
    today = yesterday + _dt.timedelta(days=1)
    monkeypatch.setattr(dt_util, "now", lambda: yesterday)
    monkeypatch.setattr(dt_util, "utcnow", lambda: yesterday)
    coord.data[sn] = {
        "sn": sn,
        "name": "Garage EV",
        "session_energy_wh": 29704.0,
        "charging": False,
    }

    ent = EnphaseEnergyTodaySensor(coord, sn)
    assert ent.native_value == round(29704.0 / 1000.0, 3)

    monkeypatch.setattr(dt_util, "now", lambda: today)
    monkeypatch.setattr(dt_util, "utcnow", lambda: today)
    assert ent.native_value == 0.0

    later_today = today + _dt.timedelta(hours=2)
    monkeypatch.setattr(dt_util, "now", lambda: later_today)
    monkeypatch.setattr(dt_util, "utcnow", lambda: later_today)
    coord.data[sn]["session_energy_wh"] = 1500.0
    assert ent.native_value == round(1500.0 / 1000.0, 3)
