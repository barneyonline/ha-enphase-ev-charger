from datetime import datetime, timedelta, timezone
from datetime import time as dt_time

import pytest

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

pytest.importorskip("homeassistant")
from homeassistant.components.sensor import SensorStateClass


def _mk_coord_with(sn: str, payload: dict):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: payload}
    coord.serials = {sn}
    coord.last_set_amps = {}
    coord.site_id = "site"
    coord.last_update_success = True
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
    coord.pick_start_amps = lambda s: 30
    coord.last_set_amps[sn] = 30

    sensor = EnphaseChargingLevelSensor(coord, sn)
    assert sensor.native_value == 30


def test_charging_level_attributes_include_limits():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging_level": 32,
            "min_amp": " 6 ",
            "max_amp": 40,
            "max_current": "48",
            "amp_granularity": "2",
        },
    )

    sensor = EnphaseChargingLevelSensor(coord, sn)
    attrs = sensor.extra_state_attributes
    assert attrs == {
        "min_amp": 6,
        "max_amp": 40,
        "max_current": 48,
        "amp_granularity": 2,
        "safe_limit_state": None,
        "safe_limit_active": False,
    }

    coord.data[sn]["min_amp"] = "bad"
    coord.data[sn]["max_amp"] = None
    coord.data[sn]["max_current"] = ""
    coord.data[sn]["amp_granularity"] = "bad"
    attrs = sensor.extra_state_attributes
    assert attrs == {
        "min_amp": None,
        "max_amp": None,
        "max_current": None,
        "amp_granularity": None,
        "safe_limit_state": None,
        "safe_limit_active": False,
    }


def test_charging_level_invalid_value_falls_back():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor
    from custom_components.enphase_ev.const import SAFE_LIMIT_AMPS

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging_level": "bad",
        },
    )
    coord.pick_start_amps = lambda s: 18

    sensor = EnphaseChargingLevelSensor(coord, sn)
    assert sensor.native_value == 18

    coord.data[sn]["safe_limit_state"] = 1
    coord.data[sn]["charging_level"] = 32
    assert sensor.native_value == SAFE_LIMIT_AMPS

    coord.data[sn]["safe_limit_state"] = True
    assert sensor.native_value == SAFE_LIMIT_AMPS

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord.data[sn]["safe_limit_state"] = BadStr()
    assert sensor.native_value == 32

    coord.data[sn]["safe_limit_state"] = 0
    assert sensor.native_value == 32


def test_charging_level_includes_safe_limit_state_attributes():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging_level": 32,
            "safe_limit_state": 1,
        },
    )

    sensor = EnphaseChargingLevelSensor(coord, sn)
    attrs = sensor.extra_state_attributes
    assert attrs["safe_limit_state"] == 1
    assert attrs["safe_limit_active"] is True


def test_electrical_phase_sensor_formats_state_and_attributes():
    from custom_components.enphase_ev.sensor import EnphaseElectricalPhaseSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "phase_mode": "3",
            "dlb_enabled": "true",
            "dlb_active": 1,
        },
    )

    sensor = EnphaseElectricalPhaseSensor(coord, sn)
    assert sensor.native_value == "Three Phase"
    attrs = sensor.extra_state_attributes
    assert attrs["phase_mode_raw"] == "3"
    assert attrs["dlb_enabled"] is True
    assert attrs["dlb_active"] is True

    coord.data[sn]["phase_mode"] = " "
    coord.data[sn]["dlb_enabled"] = None
    coord.data[sn]["dlb_active"] = None
    assert sensor.native_value is None
    attrs = sensor.extra_state_attributes
    assert attrs["phase_mode_raw"] is None
    assert attrs["dlb_enabled"] is None
    assert attrs["dlb_active"] is None


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

    coord.data[sn]["charging"] = False
    coord.data[sn]["last_reported_at"] = "2025-09-09T09:06:00Z"
    assert sensor.native_value == 0


def test_storm_guard_state_sensor_normalizes():
    from custom_components.enphase_ev.sensor import EnphaseStormGuardStateSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "storm_guard_state": "enabled",
        },
    )
    sensor = EnphaseStormGuardStateSensor(coord, sn)
    assert sensor.native_value == "enabled"
    assert sensor.available is True

    coord.data[sn]["storm_guard_state"] = "Disabled"
    assert sensor.native_value == "disabled"

    coord.data[sn]["storm_guard_state"] = 1
    assert sensor.native_value == "enabled"

    coord.data[sn]["storm_guard_state"] = 0
    assert sensor.native_value == "disabled"

    coord.data[sn]["storm_guard_state"] = "on"
    assert sensor.native_value == "enabled"

    coord.data[sn]["storm_guard_state"] = "off"
    assert sensor.native_value == "disabled"

    coord.data[sn]["storm_guard_state"] = True
    assert sensor.native_value == "enabled"

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord.data[sn]["storm_guard_state"] = BadStr()
    assert sensor.native_value is None

    coord.data[sn]["storm_guard_state"] = "mystery"
    assert sensor.native_value is None

    coord.data[sn]["storm_guard_state"] = None
    assert sensor.native_value is None
    assert sensor.available is False


def test_storm_alert_sensor_states():
    from types import SimpleNamespace
    from custom_components.enphase_ev.sensor import EnphaseStormAlertSensor
    from homeassistant.helpers.entity import EntityCategory

    coord = SimpleNamespace(
        site_id="site",
        storm_alert_active=None,
        storm_alert_critical_override=None,
        storm_alerts=[],
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )
    sensor = EnphaseStormAlertSensor(coord)
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["storm_alert_active"] is None

    coord.storm_alert_active = True
    coord.storm_alert_critical_override = True
    coord.storm_alerts = [{"type": "wind"}]
    assert sensor.native_value == "active"
    assert sensor.extra_state_attributes["storm_alert_active"] is True
    assert sensor.extra_state_attributes["critical_alert_override"] is True
    assert sensor.extra_state_attributes["storm_alert_count"] == 1

    coord.storm_alert_active = False
    coord.storm_alerts = "bad"
    assert sensor.native_value == "inactive"
    assert sensor.extra_state_attributes["storm_alert_active"] is False
    assert sensor.extra_state_attributes["storm_alert_count"] == 0


def test_battery_overall_charge_sensor_states():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryOverallChargeSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_aggregate_charge_pct=47.8,
        battery_status_summary={
            "aggregate_status": "normal",
            "aggregate_charge_source": "computed",
            "included_count": 2,
            "contributing_count": 2,
            "missing_energy_capacity_keys": [],
            "excluded_count": 0,
            "available_energy_kwh": 4.75,
            "max_capacity_kwh": 10.0,
            "site_current_charge_pct": 48.0,
            "site_available_energy_kwh": 4.75,
            "site_max_capacity_kwh": 10.0,
            "site_available_power_kw": 7.68,
            "site_max_power_kw": 7.68,
            "site_total_micros": 12,
            "site_active_micros": 12,
            "site_inactive_micros": 0,
            "site_included_count": 2,
            "site_excluded_count": 0,
            "battery_order": ["BAT-1", "BAT-2"],
        },
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )

    sensor = EnphaseBatteryOverallChargeSensor(coord)
    assert sensor.available is True
    assert sensor.native_value == 47.8
    attrs = sensor.extra_state_attributes
    assert attrs["aggregate_status"] == "normal"
    assert attrs["aggregate_charge_source"] == "computed"
    assert attrs["included_count"] == 2
    assert attrs["contributing_count"] == 2

    coord.battery_aggregate_charge_pct = None
    assert sensor.available is False


def test_battery_overall_status_sensor_states():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryOverallStatusSensor
    from homeassistant.helpers.entity import EntityCategory

    coord = SimpleNamespace(
        site_id="site",
        battery_aggregate_status="warning",
        battery_status_summary={
            "aggregate_charge_pct": 30.0,
            "aggregate_charge_source": "computed",
            "included_count": 2,
            "contributing_count": 2,
            "missing_energy_capacity_keys": [],
            "excluded_count": 1,
            "worst_storage_key": "BAT-2",
            "worst_status": "warning",
            "per_battery_status": {"BAT-1": "normal", "BAT-2": "warning"},
            "per_battery_status_raw": {"BAT-1": "normal", "BAT-2": "warning"},
            "per_battery_status_text": {"BAT-1": "Normal", "BAT-2": "Warning"},
            "battery_order": ["BAT-1", "BAT-2"],
        },
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )

    sensor = EnphaseBatteryOverallStatusSensor(coord)
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.available is True
    assert sensor.native_value == "warning"
    attrs = sensor.extra_state_attributes
    assert attrs["aggregate_charge_source"] == "computed"
    assert attrs["worst_storage_key"] == "BAT-2"
    assert attrs["per_battery_status"]["BAT-2"] == "warning"

    coord.battery_aggregate_status = None
    assert sensor.available is False


def test_battery_storage_charge_sensor_snapshot():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryStorageChargeSensor

    snapshot = {
        "identity": "BAT-1",
        "name": "IQ Battery 5P",
        "serial_number": "BAT-1",
        "current_charge_pct": 48.0,
        "status": "normal",
    }
    coord = SimpleNamespace(
        site_id="site",
        last_update_success=True,
        battery_storage=lambda _serial: snapshot,
        type_device_info=lambda _key: None,
    )

    sensor = EnphaseBatteryStorageChargeSensor(coord, "BAT-1")
    assert sensor.available is True
    assert sensor.name == "IQ Battery 5P"
    assert sensor.native_value == 48.0
    assert sensor.extra_state_attributes["serial_number"] == "BAT-1"


def test_battery_storage_charge_sensor_edge_paths():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryStorageChargeSensor

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord = SimpleNamespace(
        site_id="site",
        last_update_success=True,
        battery_storage="not-callable",
        type_device_info=lambda _key: None,
    )
    sensor = EnphaseBatteryStorageChargeSensor(coord, "BAT-EDGE")
    assert sensor.available is False
    assert sensor.native_value is None

    coord.battery_storage = lambda _serial: {"name": None, "serial_number": BadStr()}
    assert sensor.name == "BAT-EDGE"
    assert sensor.native_value is None

    coord.battery_storage = lambda _serial: {
        "name": "Edge Battery",
        "current_charge_pct": BadStr(),
    }
    assert sensor.name == "Edge Battery"
    assert sensor.native_value is None
    info = sensor.device_info
    assert info["name"] == "Battery"

    coord.battery_storage = lambda _serial: ["bad"]
    assert sensor.available is False

    coord.has_type_for_entities = lambda _key: False
    assert sensor.available is False

    expected_info = {"identifiers": {("enphase_ev", "type:site:encharge")}}
    coord.type_device_info = lambda _key: expected_info
    assert sensor.device_info == expected_info


def test_battery_overall_sensors_unavailable_paths():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryOverallChargeSensor,
        EnphaseBatteryOverallStatusSensor,
    )

    coord = SimpleNamespace(
        site_id="site",
        battery_aggregate_charge_pct=None,
        battery_aggregate_status=None,
        battery_status_summary={},
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=False,
    )
    charge = EnphaseBatteryOverallChargeSensor(coord)
    status = EnphaseBatteryOverallStatusSensor(coord)
    assert charge.available is False
    assert status.available is False
    assert charge.native_value is None

    class BadFloat:
        def __float__(self):
            raise ValueError("boom")

    coord.last_update_success = True
    coord.battery_aggregate_charge_pct = BadFloat()
    assert charge.native_value is None


def test_battery_mode_sensor_states():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryModeSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_grid_mode="ImportExport",
        battery_mode_display="Import and Export",
        battery_charge_from_grid_allowed=True,
        battery_discharge_to_grid_allowed=True,
        battery_charge_from_grid_enabled=True,
        battery_charge_from_grid_schedule_enabled=True,
        battery_charge_from_grid_start_time=dt_time(2, 0),
        battery_charge_from_grid_end_time=dt_time(5, 0),
        battery_shutdown_level=15,
        battery_shutdown_level_min=10,
        battery_shutdown_level_max=25,
        battery_use_battery_for_self_consumption=True,
        _battery_hide_charge_from_grid=False,
        _battery_envoy_supports_vls=True,
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )
    sensor = EnphaseBatteryModeSensor(coord)
    assert sensor.available is True
    assert sensor.native_value == "Import and Export"
    attrs = sensor.extra_state_attributes
    assert attrs["mode_raw"] == "ImportExport"
    assert attrs["charge_from_grid_allowed"] is True
    assert attrs["discharge_to_grid_allowed"] is True
    assert attrs["charge_from_grid_start_time"] == "02:00:00"
    assert attrs["shutdown_level"] == 15
    assert attrs["use_battery_for_self_consumption"] is True

    coord.battery_grid_mode = None
    assert sensor.available is False


def test_battery_mode_sensor_unavailable_when_coordinator_unavailable():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryModeSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_grid_mode="ImportExport",
        battery_mode_display="Import and Export",
        battery_charge_from_grid_allowed=True,
        battery_discharge_to_grid_allowed=True,
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=False,
    )

    assert EnphaseBatteryModeSensor(coord).available is False


def test_system_profile_status_sensor_states():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseSystemProfileStatusSensor

    coord = SimpleNamespace(
        site_id="site",
        storm_alert_active=None,
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
        battery_controls_available=True,
        battery_profile_pending=False,
        battery_profile="self-consumption",
        battery_effective_profile_display="Self-Consumption",
        battery_effective_backup_percentage=20,
        battery_effective_operation_mode_sub_type=None,
        battery_pending_profile=None,
        battery_pending_backup_percentage=None,
        battery_pending_operation_mode_sub_type=None,
        battery_pending_requested_at=None,
        battery_selected_profile="self-consumption",
        battery_profile_display="Self-Consumption",
        battery_selected_backup_percentage=20,
        battery_selected_operation_mode_sub_type=None,
        battery_profile_option_keys=["self-consumption", "cost_savings"],
        battery_profile_option_labels={
            "self-consumption": "Self-Consumption",
            "cost_savings": "Savings",
        },
        battery_supports_mqtt=True,
        battery_profile_polling_interval=60,
        battery_cfg_control_show=True,
        battery_cfg_control_enabled=True,
        battery_cfg_control_schedule_supported=True,
        battery_cfg_control_force_schedule_supported=False,
        battery_show_production=True,
        battery_show_consumption=True,
        battery_show_storm_guard=True,
        battery_show_battery_backup_percentage=True,
        battery_has_encharge=True,
        battery_has_enpower=False,
        battery_is_charging_modes_enabled=True,
        battery_country_code="US",
        battery_region="CA",
        battery_locale="en-US",
        battery_timezone="America/Los_Angeles",
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_site_status_code="normal",
        battery_site_status_text="Normal",
        battery_site_status_severity="info",
        battery_feature_details={"HEMS_EV_Custom_Schedule": True},
        battery_profile_evse_device={"uuid": "evse-1"},
    )
    sensor = EnphaseSystemProfileStatusSensor(coord)
    assert sensor.available is True
    assert sensor.native_value == "Self-Consumption"

    coord.battery_profile_pending = True
    coord.battery_pending_profile = "cost_savings"
    coord.battery_pending_backup_percentage = 25
    coord.battery_pending_operation_mode_sub_type = "prioritize-energy"
    coord.battery_pending_requested_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    coord.battery_selected_profile = "cost_savings"
    coord.battery_profile_display = "Savings"
    coord.battery_selected_backup_percentage = 25
    coord.battery_selected_operation_mode_sub_type = "prioritize-energy"
    assert sensor.native_value == "pending"
    attrs = sensor.extra_state_attributes
    assert attrs["pending"] is True
    assert attrs["requested_profile"] == "cost_savings"
    assert attrs["requested_profile_label"] == "Savings"
    assert attrs["supports_mqtt"] is True
    assert attrs["cfg_control_show"] is True
    assert attrs["site_country_code"] == "US"
    assert attrs["feature_details"] == {"HEMS_EV_Custom_Schedule": True}
    assert attrs["evse_profile"]["uuid"] == "evse-1"

    coord.last_update_success = False
    assert sensor.available is False


def test_last_session_sensor_tracks_session_and_persists(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    start = datetime(2025, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "session_kwh": 4.25,
            "session_start": int(start.timestamp()),
            "session_end": int(end.timestamp()),
            "charging": False,
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.state_class == SensorStateClass.TOTAL
    monkeypatch.setattr(dt_util, "utcnow", lambda: end)
    assert sensor.native_value == pytest.approx(4.25)
    attrs = sensor.extra_state_attributes
    assert attrs["session_duration_min"] == 120

    coord.data[sn]["session_kwh"] = None
    coord.data[sn]["session_start"] = None
    coord.data[sn]["session_end"] = None
    assert sensor.native_value == pytest.approx(4.25)


def test_last_session_sensor_uses_history_when_available(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "energy_today_sessions": [
                {
                    "session_id": "a1",
                    "start": "2025-10-01T01:00:00+00:00",
                    "end": "2025-10-01T02:00:00+00:00",
                    "energy_kwh_total": 6.5,
                    "active_charge_time_s": 3600,
                    "avg_cost_per_kwh": 0.25,
                    "cost_calculated": True,
                    "session_cost_state": "calculated",
                    "manual_override": False,
                    "charge_profile_stack_level": 2,
                }
            ],
            "energy_today_sessions_kwh": 6.5,
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(6.5)
    attrs = sensor.extra_state_attributes
    assert attrs["energy_consumed_kwh"] == pytest.approx(6.5)
    assert attrs["energy_consumed_wh"] == pytest.approx(6500.0)
    assert attrs["session_id"] == "a1"
    assert attrs["session_started_at"] == "2025-10-01T01:00:00+00:00"
    assert attrs["session_ended_at"] == "2025-10-01T02:00:00+00:00"
    assert attrs["active_charge_time_s"] == 3600
    assert attrs["avg_cost_per_kwh"] == pytest.approx(0.25)
    assert attrs["cost_calculated"] is True
    assert attrs["session_cost_state"] == "calculated"
    assert attrs["manual_override"] is False
    assert attrs["charge_profile_stack_level"] == 2


def test_last_session_prefers_history_when_idle(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging": False,
            "session_kwh": 1.25,
            "session_start": "2025-10-01T03:00:00+00:00",
            "session_end": "2025-10-01T03:30:00+00:00",
            "energy_today_sessions": [
                {
                    "session_id": "old",
                    "start": "2025-10-01T01:00:00+00:00",
                    "end": "2025-10-01T02:00:00+00:00",
                    "energy_kwh_total": 2.0,
                },
                {
                    "session_id": "history-123",
                    "start": "2025-10-01T03:00:00+00:00",
                    "end": "2025-10-01T04:00:00+00:00",
                    "energy_kwh_total": 6.5,
                    "session_cost": 2.2,
                    "avg_cost_per_kwh": 0.34,
                },
            ],
            "energy_today_sessions_kwh": 8.5,
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(6.5)
    attrs = sensor.extra_state_attributes
    assert attrs["energy_consumed_kwh"] == pytest.approx(6.5)
    assert attrs["session_id"] == "history-123"
    assert attrs["session_cost"] == pytest.approx(2.2)
    assert attrs["avg_cost_per_kwh"] == pytest.approx(0.34)


def test_last_session_merges_history_metadata(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging": True,
            "session_kwh": 1.25,
            "session_start": "2025-10-01T03:00:00+00:00",
            "session_end": None,
            "energy_today_sessions": [
                {
                    "session_id": "old",
                    "start": "2025-10-01T01:00:00+00:00",
                    "end": "2025-10-01T02:00:00+00:00",
                    "energy_kwh_total": 2.0,
                },
                {
                    "session_id": "history-123",
                    "start": "2025-10-01T03:00:00+00:00",
                    "end": "2025-10-01T04:00:00+00:00",
                    "energy_kwh_total": 4.5,
                    "session_cost": 1.2,
                    "avg_cost_per_kwh": 0.24,
                    "manual_override": True,
                    "session_cost_state": "COST_CALCULATED",
                    "charge_profile_stack_level": 3,
                },
            ],
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(1.25)
    attrs = sensor.extra_state_attributes
    assert attrs["energy_consumed_kwh"] == pytest.approx(1.25)
    assert attrs["session_id"] == "history-123"
    assert attrs["session_started_at"] == "2025-10-01T03:00:00+00:00"
    assert attrs["session_ended_at"] == "2025-10-01T04:00:00+00:00"
    assert attrs["session_cost"] == pytest.approx(1.2)
    assert attrs["avg_cost_per_kwh"] == pytest.approx(0.24)
    assert attrs["manual_override"] is True
    assert attrs["session_cost_state"] == "COST_CALCULATED"
    assert attrs["charge_profile_stack_level"] == 3


def test_last_session_attributes_convert_units_and_duration(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.const import UnitOfLength
    from homeassistant.util import dt as dt_util
    from homeassistant.util.unit_conversion import DistanceConverter

    sn = RANDOM_SERIAL
    plug_in = "2025-10-24T20:00:00.000Z[UTC]"
    plug_out = "2025-10-24T22:30:15.000Z[UTC]"
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "session_energy_wh": 3561.53,
            "session_plug_in_at": plug_in,
            "session_plug_out_at": plug_out,
            "session_start": datetime(2025, 10, 24, 20, 0, 0, tzinfo=timezone.utc).timestamp(),
            "session_end": datetime(2025, 10, 24, 22, 30, 15, tzinfo=timezone.utc).timestamp(),
            "session_miles": 14.35368,
            "session_cost": 4.75,
            "session_charge_level": 32,
        },
    )

    base_utc = datetime(2025, 10, 24, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_utc)
    monkeypatch.setattr(
        dt_util,
        "as_local",
        lambda dt: dt.replace(tzinfo=timezone.utc),  # type: ignore[override]
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)

    class DummyUnits:
        length_unit = UnitOfLength.KILOMETERS

    class DummyConfig:
        units = DummyUnits()

    class DummyHass:
        config = DummyConfig()

    sensor.hass = DummyHass()  # type: ignore[assignment]

    assert sensor.native_value == round(3561.53 / 1000.0, 3)
    attrs = sensor.extra_state_attributes
    assert attrs["plugged_in_at"] == "2025-10-24T20:00:00+00:00"
    assert attrs["plugged_out_at"] == "2025-10-24T22:30:15+00:00"
    assert attrs["energy_consumed_wh"] == pytest.approx(3561.53)
    assert attrs["session_started_at"] == "2025-10-24T20:00:00+00:00"
    assert attrs["session_ended_at"] == "2025-10-24T22:30:15+00:00"
    expected_km = round(
        DistanceConverter.convert(
            14.35368, UnitOfLength.MILES, UnitOfLength.KILOMETERS
        ),
        3,
    )
    assert attrs["range_added"] == expected_km
    assert attrs["session_cost"] == 4.75
    assert attrs["session_charge_level"] == 32
    assert attrs["session_duration_min"] == 150


def test_last_session_restore_data_roundtrip():
    from custom_components.enphase_ev.sensor import _LastSessionRestoreData

    restored = _LastSessionRestoreData.from_dict(
        {
            "last_session_kwh": "2.5",
            "last_session_wh": "2500",
            "last_session_start": "100",
            "last_session_end": "200",
            "session_key": "abc",
            "last_duration_min": "45",
        }
    )
    assert restored.last_session_kwh == pytest.approx(2.5)
    assert restored.last_session_wh == pytest.approx(2500)
    assert restored.last_session_start == pytest.approx(100)
    assert restored.last_session_end == pytest.approx(200)
    assert restored.session_key == "abc"
    assert restored.last_duration_min == 45

    empty = _LastSessionRestoreData.from_dict(None)
    assert empty.last_session_kwh is None
    assert empty.session_key is None


def test_last_reported_sensor_exposes_reporting_interval(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseLastReportedSensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "last_reported_at": "2025-09-07T11:38:31Z[UTC]",
            "reporting_interval": " 300 ",
            "connection": "wifi",
            "ip_address": "192.0.2.10",
            "mac_address": "00:11:22:33:44:55",
            "network_interface_count": "2",
            "operating_v": "240",
            "firmware_version": "25.37.1.14",
            "system_version": "25.37.1.14",
            "application_version": "25.37.1.5",
            "sw_version": "25.37.1.14",
            "hw_version": "2.0.713.0",
            "processor_board_version": "2.0.713.0",
            "power_board_version": "25.28.9.0",
            "kernel_version": "6.6.23",
            "bootloader_version": "2024.04",
            "default_route": "interface=mlan0",
            "wifi_config": "status=connected",
            "cellular_config": "status=disconnected",
            "warranty_start_date": "2025-01-01T00:00:00Z[UTC]",
            "warranty_due_date": "2030-01-01T00:00:00Z[UTC]",
            "warranty_period_years": "5",
            "created_at": "2025-01-01T00:00:00Z[UTC]",
            "breaker_rating": "48",
            "rated_current": "32",
            "grid_type": "2",
            "phase_count": "1",
            "commissioning_status": "1",
            "is_connected": "true",
            "is_locally_connected": 0,
            "ho_control": True,
            "gateway_connection_count": "2",
            "gateway_connected_count": "1",
            "functional_validation_state": "1",
            "functional_validation_updated_at": 1_714_550_000,
            "charger_timezone": "Region/City",
        },
    )
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)

    sensor = EnphaseLastReportedSensor(coord, sn)
    assert sensor.native_value is not None

    attrs = sensor.extra_state_attributes
    assert attrs["reporting_interval"] == 300
    assert attrs["connection"] == "wifi"
    assert attrs["ip_address"] == "192.0.2.10"
    assert attrs["mac_address"] == "00:11:22:33:44:55"
    assert attrs["network_interface_count"] == 2
    assert attrs["operating_voltage"] == 240
    assert attrs["is_connected"] is True
    assert attrs["is_locally_connected"] is False
    assert attrs["functional_validation_updated_at"] is not None

    coord.data[sn]["reporting_interval"] = 150
    assert sensor.extra_state_attributes["reporting_interval"] == 150

    coord.data[sn]["reporting_interval"] = "not-int"
    assert sensor.extra_state_attributes["reporting_interval"] is None


def test_last_reported_sensor_handles_missing_and_invalid_values():
    from custom_components.enphase_ev.sensor import EnphaseLastReportedSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "last_reported_at": None,
        },
    )

    sensor = EnphaseLastReportedSensor(coord, sn)
    assert sensor.native_value is None

    coord.data[sn]["last_reported_at"] = "not-a-timestamp"
    assert sensor.native_value is None


def test_last_reported_sensor_attribute_edge_cases(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseLastReportedSensor
    from homeassistant.util import dt as dt_util

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "last_reported_at": "2025-09-07T11:38:31Z[UTC]",
            "reporting_interval": None,
            "is_connected": "off",
            "is_locally_connected": None,
            "created_at": None,
            "warranty_start_date": {},
            "warranty_due_date": "2025-01-01T00:00:00",
            "functional_validation_updated_at": " ",
            "wifi_config": BadStr(),
        },
    )
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)

    sensor = EnphaseLastReportedSensor(coord, sn)
    attrs = sensor.extra_state_attributes
    assert attrs["reporting_interval"] is None
    assert attrs["is_connected"] is False
    assert attrs["is_locally_connected"] is None
    assert attrs["created_at"] is None
    assert attrs["warranty_start_date"] is None
    assert attrs["warranty_due_date"] == "2025-01-01T00:00:00+00:00"
    assert attrs["functional_validation_updated_at"] is None
    assert attrs["wifi_config"] is None

    coord.data[sn]["created_at"] = "invalid"
    assert sensor.extra_state_attributes["created_at"] is None
    coord.data[sn]["is_connected"] = "maybe"
    assert sensor.extra_state_attributes["is_connected"] is None


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

    coord.data[sn]["lifetime_kwh"] = 110.0
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

    coord.data[sn]["lifetime_kwh"] = 200.75
    assert sensor.native_value == pytest.approx(200.75)

    coord.data[sn]["lifetime_kwh"] = 200.74
    assert sensor.native_value == pytest.approx(200.75)

    reset_time = datetime(2025, 9, 9, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: reset_time)
    coord.data[sn]["lifetime_kwh"] = 0
    assert sensor.native_value == pytest.approx(0)
    attrs = sensor.extra_state_attributes
    assert attrs["last_reset_value"] == pytest.approx(0)
    assert attrs["last_reset_at"] is not None

    coord.data[sn]["lifetime_kwh"] = 0.4
    assert sensor.native_value == pytest.approx(0.4)

    coord.data[sn]["lifetime_kwh"] = 0.38
    assert sensor.native_value == pytest.approx(0.4)

    coord.data[sn]["lifetime_kwh"] = 0.9
    assert sensor.native_value == pytest.approx(0.9)


def test_status_sensor_exposes_attributes(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseStatusSensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "status": "CONNECTED",
            "faulted": True,
            "commissioned": False,
            "suspended_by_evse": True,
            "offline_since": "2025-01-02T03:04:05Z",
        },
    )
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    sensor = EnphaseStatusSensor(coord, sn)
    assert sensor.native_value == "CONNECTED"
    attrs = sensor.extra_state_attributes
    assert attrs["commissioned"] is False
    assert attrs["charger_problem"] is True
    assert attrs["suspended_by_evse"] is True
    assert attrs["offline_since"] == "2025-01-02T03:04:05+00:00"


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
            "connector_status_info": "  see manual ",
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {
        "status_reason": "INSUFFICIENT_SOLAR",
        "connector_status_info": "see manual",
        "suspended_by_evse": None,
    }


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
    assert sensor.extra_state_attributes == {
        "status_reason": None,
        "connector_status_info": None,
        "suspended_by_evse": None,
    }


def test_connector_status_reason_numeric_gets_stringified():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "SUSPENDED",
            "connector_reason": 123,
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {
        "status_reason": "123",
        "connector_status_info": None,
        "suspended_by_evse": None,
    }


def test_connector_status_reason_handles_non_string_value():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    sentinel = BadStr()
    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "FAULTED",
            "connector_reason": sentinel,
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {
        "status_reason": sentinel,
        "connector_status_info": None,
        "suspended_by_evse": None,
    }


def test_connector_status_reports_suspended_by_evse():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "SUSPENDED_EVSE",
            "suspended_by_evse": True,
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes["suspended_by_evse"] is True

    class BadBool:
        def __bool__(self):
            raise ValueError("boom")

    coord.data[sn]["suspended_by_evse"] = BadBool()
    assert sensor.extra_state_attributes["suspended_by_evse"] is None


def test_charge_mode_sensor_attributes():
    from custom_components.enphase_ev.sensor import EnphaseChargeModeSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charge_mode": "IMMEDIATE",
            "charge_mode_pref": "SCHEDULED_CHARGING",
            "schedule_status": 1,
            "schedule_type": "CUSTOM",
            "schedule_slot_id": "slot-1",
            "schedule_start": "23:00",
            "schedule_end": "06:00",
            "schedule_days": [1, 2, 3],
            "schedule_reminder_enabled": "true",
            "schedule_reminder_min": 10,
            "green_battery_supported": True,
            "green_battery_enabled": False,
        },
    )
    sensor = EnphaseChargeModeSensor(coord, sn)
    attrs = sensor.extra_state_attributes
    assert attrs["preferred_mode"] == "SCHEDULED_CHARGING"
    assert attrs["effective_mode"] == "IMMEDIATE"
    assert attrs["schedule_slot_id"] == "slot-1"
    assert attrs["schedule_days"] == [1, 2, 3]
    assert attrs["schedule_reminder_enabled"] is True
    assert attrs["green_battery_supported"] is True
    assert attrs["green_battery_enabled"] is False

    coord.data[sn]["schedule_reminder_enabled"] = "off"
    coord.data[sn]["green_battery_supported"] = 1
    coord.data[sn]["green_battery_enabled"] = "disabled"
    attrs = sensor.extra_state_attributes
    assert attrs["schedule_reminder_enabled"] is False
    assert attrs["green_battery_supported"] is True
    assert attrs["green_battery_enabled"] is False

    coord.data[sn]["schedule_reminder_enabled"] = None
    assert sensor.extra_state_attributes["schedule_reminder_enabled"] is None
    coord.data[sn]["schedule_reminder_enabled"] = "maybe"
    assert sensor.extra_state_attributes["schedule_reminder_enabled"] is None


def test_last_session_sensor_exposes_auth_metadata(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging": False,
            "session_auth_status": 1,
            "session_auth_type": "APP",
            "session_auth_identifier": "user@example.com",
            "session_auth_token_present": True,
            "energy_today_sessions": [
                {
                    "session_id": "history-1",
                    "start": "2025-10-01T03:00:00+00:00",
                    "end": "2025-10-01T04:00:00+00:00",
                    "energy_kwh_total": 2.5,
                    "auth_type": "RFID",
                    "auth_identifier": "tag-123",
                    "auth_token": "present",
                }
            ],
        },
    )
    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(2.5)
    attrs = sensor.extra_state_attributes
    assert attrs["session_auth_status"] == 1
    assert attrs["session_auth_type"] == "RFID"
    assert attrs["session_auth_identifier"] == "tag-123"
    assert attrs["session_auth_token_present"] is True
