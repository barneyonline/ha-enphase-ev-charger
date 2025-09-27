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


def test_power_sensor_uses_lifetime_delta():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = "482522020944"
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 10.0,
            "last_reported_at": "2025-09-09T10:00:00Z[UTC]",
            "charging": True,
        },
    )

    sensor = EnphasePowerSensor(coord, sn)
    assert sensor.native_value == 0

    coord.data[sn]["lifetime_kwh"] = 10.6  # +0.6 kWh
    coord.data[sn]["last_reported_at"] = "2025-09-09T10:05:00Z[UTC]"
    val = sensor.native_value
    assert val == 7200
    assert sensor.extra_state_attributes["last_window_seconds"] == pytest.approx(300)

    # No new energy yet but still charging → hold last computed power
    coord.data[sn]["lifetime_kwh"] = 10.6
    coord.data[sn]["last_reported_at"] = "2025-09-09T10:06:00Z[UTC]"
    assert sensor.native_value == 7200


def test_power_sensor_zero_when_idle():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = "482522020944"
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 5.0,
            "last_reported_at": "2025-09-09T09:00:00Z",
            "charging": True,
        },
    )
    sensor = EnphasePowerSensor(coord, sn)
    assert sensor.native_value == 0

    coord.data[sn]["lifetime_kwh"] = 5.5
    coord.data[sn]["last_reported_at"] = "2025-09-09T09:05:00Z"
    assert sensor.native_value == 6000

    # Charging stops and no new energy → drop to 0
    coord.data[sn]["charging"] = False
    coord.data[sn]["last_reported_at"] = "2025-09-09T09:06:00Z"
    assert sensor.native_value == 0


def test_dlb_sensor_state_mapping():
    from custom_components.enphase_ev.sensor import EnphaseDynamicLoadBalancingSensor

    sn = "482522020944"
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "dlb_enabled": True,
        },
    )

    sensor = EnphaseDynamicLoadBalancingSensor(coord, sn)
    assert sensor.name == "Dynamic Loan Balancing"
    assert sensor.native_value == "enabled"

    coord.data[sn]["dlb_enabled"] = False
    assert sensor.native_value == "disabled"

    coord.data[sn].pop("dlb_enabled")
    assert sensor.native_value is None


def test_power_sensor_caps_max_output():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = "482522020944"
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 100.0,
            "last_reported_at": "2025-09-09T08:00:00Z",
            "charging": True,
        },
    )
    sensor = EnphasePowerSensor(coord, sn)
    assert sensor.native_value == 0

    coord.data[sn]["lifetime_kwh"] = 110.0  # 10 kWh in 5 minutes would exceed cap
    coord.data[sn]["last_reported_at"] = "2025-09-09T08:05:00Z"
    assert sensor.native_value == 19200


def test_power_sensor_fallback_window_when_timestamp_missing(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphasePowerSensor
    from homeassistant.util import dt as dt_util

    sn = "482522020944"
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 1.0,
            "charging": True,
        },
    )
    sensor = EnphasePowerSensor(coord, sn)

    # Seed state with deterministic now()
    anchor = datetime(2025, 9, 9, 7, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: anchor)
    monkeypatch.setattr(dt_util, "now", lambda: anchor)
    assert sensor.native_value == 0

    monkeypatch.setattr(dt_util, "utcnow", lambda: anchor + timedelta(minutes=5))
    monkeypatch.setattr(dt_util, "now", lambda: anchor + timedelta(minutes=5))
    coord.data[sn]["lifetime_kwh"] = 1.5
    coord.data[sn].pop("last_reported_at", None)
    assert sensor.native_value == 6000


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
