from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("homeassistant")


def _mk_coord_with(sn: str, payload: dict):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    # minimal hass stub for coordinator init path that doesn't use hass features
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    # Patch attributes directly for testing entity properties
    coord.data = {sn: payload}
    coord.serials = {sn}
    coord.last_set_amps = {}
    return coord


def test_charging_level_fallback():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sn = "499900000001"
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

    sn = "499900000001"
    coord = _mk_coord_with(sn, {"sn": sn, "name": "Garage EV", "power_w": 3700})
    s = EnphasePowerSensor(coord, sn)
    assert s.native_value == 3700


def test_session_duration_minutes():
    from custom_components.enphase_ev.sensor import EnphaseSessionDurationSensor

    sn = "499900000001"
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

    sn = "499900000001"
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
