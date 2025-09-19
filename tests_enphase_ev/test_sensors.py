from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("homeassistant")


def _mk_coord_with(sn: str, payload: dict):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    # minimal hass-free coordinator stub for entity property tests
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: payload}
    coord.serials = {sn}
    coord.last_set_amps = {}
    return coord


def test_charging_level_fallback():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sn = "482522020944"
    coord = _mk_coord_with(sn, {
        "sn": sn,
        "name": "Garage EV",
        "session_start": None,
    })
    coord.set_last_set_amps = lambda s, a: None  # no-op
    coord.last_set_amps[sn] = 30

    s = EnphaseChargingLevelSensor(coord, sn)
    assert s.native_value == 30


def test_power_sensor_value(monkeypatch):
    import datetime as _dt
    from homeassistant.util import dt as dt_util
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = "482522020944"
    coord = _mk_coord_with(sn, {"sn": sn, "name": "Garage EV", "lifetime_kwh": 10.0})
    s = EnphasePowerSensor(coord, sn)

    # First read at t0 seeds state → 0 W
    t0 = _dt.datetime(2025, 9, 9, 10, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: t0)
    assert s.native_value == 0

    # After 120s, +0.24 kWh → 7200 W
    t1 = t0 + _dt.timedelta(seconds=120)
    monkeypatch.setattr(dt_util, "now", lambda: t1)
    coord.data[sn]["lifetime_kwh"] = 10.24
    assert s.native_value == 7200


def test_lifetime_energy_filters_resets():
    from custom_components.enphase_ev.sensor import EnphaseLifetimeEnergySensor

    sn = "482522020944"
    payload = {"sn": sn, "name": "Garage EV", "lifetime_kwh": 200.5}
    coord = _mk_coord_with(sn, payload)

    sensor = EnphaseLifetimeEnergySensor(coord, sn)
    assert sensor.native_value == pytest.approx(200.5)

    # A cloud glitch may momentarily return 0 – keep previous total
    coord.data[sn]["lifetime_kwh"] = 0
    assert sensor.native_value == pytest.approx(200.5)

    # Normal increase is accepted
    coord.data[sn]["lifetime_kwh"] = 200.75
    assert sensor.native_value == pytest.approx(200.75)

    # Minor jitter below tolerance is clamped to the stored total
    coord.data[sn]["lifetime_kwh"] = 200.74
    assert sensor.native_value == pytest.approx(200.75)

    # Subsequent increases continue updating the state
    coord.data[sn]["lifetime_kwh"] = 201.1
    assert sensor.native_value == pytest.approx(201.1)


def test_session_duration_minutes():
    from custom_components.enphase_ev.sensor import EnphaseSessionDurationSensor

    sn = "482522020944"
    now = datetime.now(timezone.utc)
    ten_min_ago = int((now - timedelta(minutes=10)).timestamp())

    # While charging: duration should be computed against 'now'
    coord = _mk_coord_with(
        sn,
        {"sn": sn, "name": "Garage EV", "session_start": ten_min_ago, "charging": True},
    )
    s = EnphaseSessionDurationSensor(coord, sn)
    # Allow small drift
    assert 9 <= s.native_value <= 11


def test_phase_mode_mapping():
    from custom_components.enphase_ev.sensor import EnphasePhaseModeSensor

    sn = "482522020944"
    # Numeric 1 -> Single Phase
    coord = _mk_coord_with(sn, {"sn": sn, "name": "Garage EV", "phase_mode": 1})
    s = EnphasePhaseModeSensor(coord, sn)
    assert s.native_value == "Single Phase"

    # Numeric 3 -> Three Phase
    coord2 = _mk_coord_with(sn, {"sn": sn, "name": "Garage EV", "phase_mode": 3})
    s2 = EnphasePhaseModeSensor(coord2, sn)
    assert s2.native_value == "Three Phase"

    # Non-numeric -> unchanged
    coord3 = _mk_coord_with(sn, {"sn": sn, "name": "Garage EV", "phase_mode": "Balanced"})
    s3 = EnphasePhaseModeSensor(coord3, sn)
    assert s3.native_value == "Balanced"
