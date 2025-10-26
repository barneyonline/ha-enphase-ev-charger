from datetime import datetime, timedelta, timezone

import pytest

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

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

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "session_start": None,
        },
    )
    coord.set_last_set_amps = lambda s, a: None  # no-op
    coord.last_set_amps[sn] = 30

    s = EnphaseChargingLevelSensor(coord, sn)
    assert s.native_value == 30


def test_power_sensor_uses_lifetime_delta():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL
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

    sn = RANDOM_SERIAL
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

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "dlb_enabled": True,
        },
    )

    sensor = EnphaseDynamicLoadBalancingSensor(coord, sn)
    assert sensor.name == "Dynamic Load Balancing"
    assert sensor.native_value == "enabled"
    assert sensor.icon == "mdi:lightning-bolt"

    coord.data[sn]["dlb_enabled"] = False
    assert sensor.native_value == "disabled"
    assert sensor.icon == "mdi:lightning-bolt-outline"

    coord.data[sn].pop("dlb_enabled")
    assert sensor.native_value is None


def test_connection_sensor_strips_whitespace():
    from custom_components.enphase_ev.sensor import EnphaseConnectionSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connection": " ethernet ",
        },
    )

    sensor = EnphaseConnectionSensor(coord, sn)
    assert sensor.native_value == " ethernet ".strip()

    coord.data[sn]["connection"] = ""
    assert sensor.native_value is None


def test_ip_sensor_handles_blank_values():
    from custom_components.enphase_ev.sensor import EnphaseIpAddressSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "ip_address": " 192.168.1.184 ",
        },
    )

    sensor = EnphaseIpAddressSensor(coord, sn)
    assert sensor.native_value == "192.168.1.184"

    coord.data[sn]["ip_address"] = ""
    assert sensor.native_value is None

    coord.data[sn]["ip_address"] = None
    assert sensor.native_value is None


def test_reporting_interval_sensor_coerces_ints():
    from custom_components.enphase_ev.sensor import EnphaseReportingIntervalSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "reporting_interval": " 300 ",
        },
    )

    sensor = EnphaseReportingIntervalSensor(coord, sn)
    assert sensor.native_value == 300

    coord.data[sn]["reporting_interval"] = 150
    assert sensor.native_value == 150

    coord.data[sn]["reporting_interval"] = "not-int"
    assert sensor.native_value is None


def test_power_sensor_caps_max_output():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL
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

    sn = RANDOM_SERIAL
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


def test_lifetime_energy_accepts_resets(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseLifetimeEnergySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    payload = {"sn": sn, "name": "Garage EV", "lifetime_kwh": 200.5}
    coord = _mk_coord_with(sn, payload)

    sensor = EnphaseLifetimeEnergySensor(coord, sn)
    assert sensor.native_value == pytest.approx(200.5)

    # Normal increase is accepted
    coord.data[sn]["lifetime_kwh"] = 200.75
    assert sensor.native_value == pytest.approx(200.75)

    # Minor jitter below tolerance is clamped to the stored total
    coord.data[sn]["lifetime_kwh"] = 200.74
    assert sensor.native_value == pytest.approx(200.75)

    # A genuine reset should be propagated and tracked
    reset_time = datetime(2025, 9, 9, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: reset_time)
    coord.data[sn]["lifetime_kwh"] = 0
    assert sensor.native_value == pytest.approx(0)
    attrs = sensor.extra_state_attributes
    assert attrs["last_reset_value"] == pytest.approx(0)
    assert attrs["last_reset_at"] is not None

    # Subsequent increases continue updating the state
    coord.data[sn]["lifetime_kwh"] = 0.4
    assert sensor.native_value == pytest.approx(0.4)

    # Minor jitter near the new baseline remains clamped
    coord.data[sn]["lifetime_kwh"] = 0.38
    assert sensor.native_value == pytest.approx(0.4)

    # Subsequent increases continue updating the state
    coord.data[sn]["lifetime_kwh"] = 0.9
    assert sensor.native_value == pytest.approx(0.9)


def test_session_duration_minutes():
    from custom_components.enphase_ev.sensor import EnphaseSessionDurationSensor

    sn = RANDOM_SERIAL
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

    sn = RANDOM_SERIAL
    # Numeric 1 -> Single Phase
    coord = _mk_coord_with(sn, {"sn": sn, "name": "Garage EV", "phase_mode": 1})
    s = EnphasePhaseModeSensor(coord, sn)
    assert s.native_value == "Single Phase"

    # Numeric 3 -> Three Phase
    coord2 = _mk_coord_with(sn, {"sn": sn, "name": "Garage EV", "phase_mode": 3})
    s2 = EnphasePhaseModeSensor(coord2, sn)
    assert s2.native_value == "Three Phase"

    # Non-numeric -> unchanged
    coord3 = _mk_coord_with(
        sn, {"sn": sn, "name": "Garage EV", "phase_mode": "Balanced"}
    )
    s3 = EnphasePhaseModeSensor(coord3, sn)
    assert s3.native_value == "Balanced"


def test_sensor_entity_categories():
    from custom_components.enphase_ev.sensor import (
        EnphaseConnectorStatusSensor,
        EnphaseMaxAmpSensor,
        EnphaseMinAmpSensor,
        EnphasePhaseModeSensor,
    )
    from homeassistant.helpers.entity import EntityCategory

    sn = RANDOM_SERIAL
    diag_payload = {"sn": sn, "name": "Garage EV"}
    min_amp_sensor = EnphaseMinAmpSensor(_mk_coord_with(sn, diag_payload), sn)
    max_amp_sensor = EnphaseMaxAmpSensor(_mk_coord_with(sn, diag_payload), sn)
    phase_mode_sensor = EnphasePhaseModeSensor(_mk_coord_with(sn, diag_payload), sn)
    connector_sensor = EnphaseConnectorStatusSensor(
        _mk_coord_with(sn, {"sn": sn, "name": "Garage EV", "connector_status": "AVAILABLE"}),
        sn,
    )

    assert min_amp_sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert max_amp_sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert phase_mode_sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert connector_sensor.entity_category is None


def test_connector_status_reports_reason_attribute():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "AVAILABLE",
            "connector_reason": "INSUFFICIENT_SOLAR",
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {"status_reason": "INSUFFICIENT_SOLAR"}


def test_connector_status_reason_absent_returns_empty_attributes():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "AVAILABLE",
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {}


def test_power_and_energy_handle_lifetime_reset(monkeypatch):
    from custom_components.enphase_ev.sensor import (
        EnphaseEnergyTodaySensor,
        EnphaseLifetimeEnergySensor,
        EnphasePowerSensor,
    )
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    base_time = datetime(2025, 9, 9, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: base_time)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_time)

    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 120.0,
            "last_reported_at": "2025-09-09T10:00:00Z",
            "charging": True,
        },
    )

    energy_today = EnphaseEnergyTodaySensor(coord, sn)
    lifetime = EnphaseLifetimeEnergySensor(coord, sn)
    power = EnphasePowerSensor(coord, sn)

    assert lifetime.native_value == pytest.approx(120.0)
    assert energy_today.native_value == 0.0
    assert power.native_value == 0

    # After new consumption, power should reflect windowed delta
    advance_1 = base_time + timedelta(minutes=5)
    monkeypatch.setattr(dt_util, "now", lambda: advance_1)
    monkeypatch.setattr(dt_util, "utcnow", lambda: advance_1)
    coord.data[sn]["lifetime_kwh"] = 120.4
    coord.data[sn]["last_reported_at"] = "2025-09-09T10:05:00Z"
    first_power = power.native_value
    assert first_power > 0

    # Simulate a reset down to a small value while idle
    reset_time = base_time + timedelta(minutes=10)
    monkeypatch.setattr(dt_util, "now", lambda: reset_time)
    monkeypatch.setattr(dt_util, "utcnow", lambda: reset_time)
    coord.data[sn]["charging"] = False
    coord.data[sn]["lifetime_kwh"] = 3.0
    coord.data[sn]["last_reported_at"] = "2025-09-09T10:10:00Z"

    assert lifetime.native_value == pytest.approx(3.0)
    assert energy_today.native_value == 0.0
    assert power.native_value == 0
    lifetime_attrs = lifetime.extra_state_attributes
    assert lifetime_attrs["last_reset_at"] is not None

    # Once charging resumes, deltas should be measured from the new baseline
    resume_time = base_time + timedelta(minutes=15)
    monkeypatch.setattr(dt_util, "now", lambda: resume_time)
    monkeypatch.setattr(dt_util, "utcnow", lambda: resume_time)
    coord.data[sn]["charging"] = True
    coord.data[sn]["lifetime_kwh"] = 3.2
    coord.data[sn]["last_reported_at"] = "2025-09-09T10:15:00Z"

    resumed_power = power.native_value
    assert resumed_power > 0
    assert resumed_power != first_power
    assert energy_today.native_value == pytest.approx(0.2, abs=1e-3)


def test_energy_today_sessions_attribute(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    base_time = datetime(2025, 10, 16, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: base_time)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_time)

    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 250.0,
        },
    )
    energy_today = EnphaseEnergyTodaySensor(coord, sn)

    # Baseline established on first read
    assert energy_today.native_value == 0.0

    sessions = [
        {
            "session_id": "session-1",
            "start": "2025-10-16T01:00:00+00:00",
            "end": "2025-10-16T02:00:00+00:00",
            "auth_type": None,
            "auth_identifier": None,
            "auth_token": None,
            "active_charge_time_s": 3600,
            "active_charge_time_overlap_s": 3600,
            "energy_kwh_total": 5.5,
            "energy_kwh": 5.5,
            "miles_added": 20.0,
            "session_cost": 1.23,
            "avg_cost_per_kwh": 0.22,
            "cost_calculated": True,
            "manual_override": False,
            "session_cost_state": "COST_CALCULATED",
            "charge_profile_stack_level": 0,
        },
        {
            "session_id": "session-2",
            "start": "2025-10-16T05:15:00+00:00",
            "end": "2025-10-16T06:45:00+00:00",
            "auth_type": "RFID",
            "auth_identifier": "user-123",
            "auth_token": "token-abc",
            "active_charge_time_s": 5400,
            "active_charge_time_overlap_s": 5400,
            "energy_kwh_total": 3.2,
            "energy_kwh": 3.2,
            "miles_added": 12.5,
            "session_cost": 0.75,
            "avg_cost_per_kwh": 0.23,
            "cost_calculated": True,
            "manual_override": True,
            "session_cost_state": "COST_CALCULATED",
            "charge_profile_stack_level": 4,
        },
    ]

    coord.data[sn]["energy_today_sessions"] = sessions
    coord.data[sn]["energy_today_sessions_kwh"] = 8.7

    # Value now follows summed session energy
    assert energy_today.native_value == pytest.approx(8.7)

    attrs = energy_today.extra_state_attributes
    assert "sessions_today_total_kwh" not in attrs
    assert "sessions_today_count" not in attrs
    assert "sessions_today" not in attrs

    # A new day should reset the accumulated value when session totals drop
    coord.data[sn]["energy_today_sessions"] = [
        {
            "session_id": "session-new",
            "energy_kwh_total": 0.0,
            "energy_kwh": 0.0,
        }
    ]
    coord.data[sn]["energy_today_sessions_kwh"] = 0.0
    assert energy_today.native_value == 0.0


def test_energy_today_restore_extra_data_roundtrip():
    from custom_components.enphase_ev.sensor import (
        EnphaseEnergyTodaySensor,
        _EnergyTodayRestoreData,
    )

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(sn, {"sn": sn, "name": "Garage EV"})

    ent = EnphaseEnergyTodaySensor(coord, sn)
    ent._baseline_kwh = 12.5
    ent._baseline_day = "2025-10-27"
    ent._last_total = 345.6
    ent._last_reset_at = "2025-10-27T01:02:03+00:00"

    extra = ent.extra_restore_state_data
    assert extra is not None
    payload = extra.as_dict()
    assert payload["baseline_kwh"] == 12.5
    assert payload["baseline_day"] == "2025-10-27"
    assert payload["last_total_kwh"] == 345.6
    assert payload["last_reset_at"] == "2025-10-27T01:02:03+00:00"

    restored = _EnergyTodayRestoreData.from_dict(payload)
    assert restored.baseline_kwh == 12.5
    assert restored.baseline_day == "2025-10-27"
    assert restored.last_total_kwh == 345.6
    assert restored.last_reset_at == "2025-10-27T01:02:03+00:00"


@pytest.mark.asyncio
async def test_energy_today_restores_state_from_extra_data(monkeypatch):
    from datetime import datetime, timezone

    from homeassistant.helpers.update_coordinator import CoordinatorEntity
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.sensor import (
        EnphaseEnergyTodaySensor,
        _EnergyTodayRestoreData,
    )

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(sn, {"sn": sn, "name": "Garage EV", "lifetime_kwh": 15.0})
    ent = EnphaseEnergyTodaySensor(coord, sn)

    base_time = datetime(2025, 10, 27, 8, 0, 0, tzinfo=timezone.utc)
    today_str = base_time.strftime("%Y-%m-%d")
    monkeypatch.setattr(dt_util, "now", lambda: base_time)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_time)

    class _FakeState:
        def __init__(self):
            self.state = "1.234"
            self.attributes = {
                "baseline_kwh": 99.0,
                "baseline_day": today_str,
                "last_total_kwh": 88.8,
                "last_reset_at": "from-state",
            }

    async def _fake_last_state(self):
        return _FakeState()

    async def _fake_last_extra(self):
        return _EnergyTodayRestoreData(
            baseline_kwh=12.0,
            baseline_day=today_str,
            last_total_kwh=None,
            last_reset_at=None,
        )

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        EnphaseEnergyTodaySensor, "async_get_last_state", _fake_last_state
    )
    monkeypatch.setattr(
        EnphaseEnergyTodaySensor, "async_get_last_extra_data", _fake_last_extra
    )
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)

    await ent.async_added_to_hass()

    assert ent._baseline_kwh == pytest.approx(12.0)
    assert ent._baseline_day == today_str
    assert ent._last_total == pytest.approx(88.8)
    assert ent._last_reset_at == "from-state"
    assert ent._last_value == pytest.approx(1.234)


def test_energy_today_session_reset_tracking(monkeypatch):
    from datetime import datetime, timezone

    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "energy_today_sessions": [{"session_id": "s1"}],
            "energy_today_sessions_kwh": 5.0,
        },
    )

    ent = EnphaseEnergyTodaySensor(coord, sn)
    ent._last_value = 5.0
    ent._last_reset_at = None

    # Small drop should be treated as jitter and keep previous value
    coord.data[sn]["energy_today_sessions_kwh"] = 4.98
    small_drop = ent._value_from_sessions(coord.data[sn])
    assert small_drop == pytest.approx(5.0)
    assert ent._last_reset_at is None

    # Large drop should register a reset and record timestamp
    base_time = datetime(2025, 10, 27, 9, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_time)
    coord.data[sn]["energy_today_sessions_kwh"] = 0.0
    reset_value = ent._value_from_sessions(coord.data[sn])
    assert reset_value == pytest.approx(0.0)
    assert ent._last_reset_at == base_time.isoformat()
