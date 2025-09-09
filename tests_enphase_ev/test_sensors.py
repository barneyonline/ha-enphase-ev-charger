from datetime import datetime, timezone, timedelta
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


def test_power_sensor_value():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = "482522020944"
    coord = _mk_coord_with(sn, {"sn": sn, "name": "Garage EV", "power_w": 3700})
    s = EnphasePowerSensor(coord, sn)
    assert s.native_value == 3700


def test_session_duration_minutes():
    from custom_components.enphase_ev.sensor import EnphaseSessionDurationSensor

    sn = "482522020944"
    now = datetime.now(timezone.utc)
    ten_min_ago = int((now - timedelta(minutes=10)).timestamp())

    coord = _mk_coord_with(sn, {"sn": sn, "name": "Garage EV", "session_start": ten_min_ago})
    s = EnphaseSessionDurationSensor(coord, sn)
    # Allow small drift
    assert 9 <= s.native_value <= 11
