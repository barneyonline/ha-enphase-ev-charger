from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone

from custom_components.enphase_ev.binary_sensor import SiteCloudReachableBinarySensor
from custom_components.enphase_ev.button import (
    CancelPendingProfileChangeButton,
    RequestGridToggleOtpButton,
)
from custom_components.enphase_ev.entity import EnphaseBaseEntity
from custom_components.enphase_ev.number import (
    BatteryReserveNumber,
    BatteryShutdownLevelNumber,
)
from custom_components.enphase_ev.select import SystemProfileSelect
from custom_components.enphase_ev.sensor import (
    EnphaseCloudLatencySensor,
    EnphaseGridModeSensor,
    EnphaseGridControlStatusSensor,
    EnphaseSiteLastUpdateSensor,
    EnphaseSystemProfileStatusSensor,
    EnphaseTypeInventorySensor,
)
from custom_components.enphase_ev.switch import (
    ChargeFromGridScheduleSwitch,
    ChargeFromGridSwitch,
    SavingsUseBatteryAfterPeakSwitch,
    StormGuardSwitch,
)
from custom_components.enphase_ev.time import ChargeFromGridStartTimeEntity


def test_site_binary_sensor_has_type_false_is_unavailable() -> None:
    coord = SimpleNamespace(
        site_id="12345",
        has_type=lambda _key: False,
        last_success_utc=None,
        last_update_success=True,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        update_interval=None,
    )
    entity = SiteCloudReachableBinarySensor(coord)
    assert entity.available is False


def test_site_binary_sensor_device_info_falls_back_without_type_info() -> None:
    coord = SimpleNamespace(
        site_id="12345",
        last_success_utc=None,
        last_update_success=True,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        update_interval=None,
    )
    info = SiteCloudReachableBinarySensor(coord).device_info
    assert info["identifiers"] == {("enphase_ev", "type:12345:cloud")}


def test_site_device_info_fallbacks_without_type_device_info_provider() -> None:
    coord = SimpleNamespace(
        site_id="site-1",
        last_update_success=True,
        battery_profile_pending=True,
        battery_reserve_editable=True,
        battery_reserve_min=10,
        battery_reserve_max=100,
        battery_selected_backup_percentage=25,
        battery_shutdown_level_available=True,
        battery_shutdown_level=15,
        battery_shutdown_level_min=10,
        battery_shutdown_level_max=20,
        battery_controls_available=True,
        battery_profile_option_labels={"self-consumption": "Self-Consumption"},
        battery_profile_option_keys=["self-consumption"],
        battery_selected_profile="self-consumption",
        savings_use_battery_switch_available=True,
        savings_use_battery_after_peak=True,
        charge_from_grid_control_available=True,
        battery_charge_from_grid_enabled=True,
        charge_from_grid_schedule_available=True,
        battery_charge_from_grid_schedule_enabled=True,
        storm_guard_state="enabled",
        storm_evse_enabled=True,
    )

    assert CancelPendingProfileChangeButton(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:envoy")
    }
    assert RequestGridToggleOtpButton(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:envoy")
    }
    assert BatteryReserveNumber(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:encharge")
    }
    assert BatteryShutdownLevelNumber(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:encharge")
    }
    assert SystemProfileSelect(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:envoy")
    }
    assert SavingsUseBatteryAfterPeakSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:encharge")
    }
    assert ChargeFromGridSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:encharge")
    }
    assert ChargeFromGridScheduleSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:encharge")
    }
    assert StormGuardSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:envoy")
    }
    assert ChargeFromGridStartTimeEntity(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:encharge")
    }
    assert EnphaseGridControlStatusSensor(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:envoy")
    }
    assert EnphaseGridModeSensor(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-1:envoy")
    }


def test_system_profile_select_and_storm_guard_availability_type_checks() -> None:
    coord = SimpleNamespace(
        site_id="site-2",
        last_update_success=True,
        has_type=lambda _key: False,
        battery_controls_available=True,
        battery_profile_option_labels={"self-consumption": "Self-Consumption"},
        battery_profile_option_keys=["self-consumption"],
        battery_selected_profile="self-consumption",
        storm_guard_state="enabled",
        storm_evse_enabled=True,
    )
    assert SystemProfileSelect(coord).available is False
    assert StormGuardSwitch(coord).available is False


def test_base_entity_device_info_sets_via_device_from_type_identifier() -> None:
    class DummyEntity(EnphaseBaseEntity):
        pass

    coord = SimpleNamespace(
        data={
            "SN1": {
                "display_name": "Garage Charger",
                "model_name": "IQ EVSE",
            }
        },
        site_id="site-1",
        type_identifier=lambda _key: ("enphase_ev", "type:site-1:iqevse"),
    )
    entity = DummyEntity(coord, "SN1")
    info = entity.device_info
    assert info["via_device"] == ("enphase_ev", "type:site-1:iqevse")


def test_type_device_entities_use_provided_type_device_info() -> None:
    provided = {"identifiers": {("enphase_ev", "type:site-x:envoy")}}
    coord = SimpleNamespace(
        site_id="site-x",
        last_update_success=True,
        has_type=lambda _key: True,
        type_label=lambda _key: "Gateway",
        type_bucket=lambda _key: {"count": "bad", "devices": "bad"},
        type_device_info=lambda _key: provided,
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        latency_ms=None,
        battery_controls_available=False,
        battery_profile=None,
    )

    assert SiteCloudReachableBinarySensor(coord).device_info is provided
    assert CancelPendingProfileChangeButton(coord).device_info is provided
    assert RequestGridToggleOtpButton(coord).device_info is provided
    assert BatteryReserveNumber(coord).device_info is provided
    assert BatteryShutdownLevelNumber(coord).device_info is provided
    assert SystemProfileSelect(coord).device_info is provided
    assert SavingsUseBatteryAfterPeakSwitch(coord).device_info is provided
    assert ChargeFromGridSwitch(coord).device_info is provided
    assert ChargeFromGridScheduleSwitch(coord).device_info is provided
    assert StormGuardSwitch(coord).device_info is provided
    assert ChargeFromGridStartTimeEntity(coord).device_info is provided
    assert EnphaseGridControlStatusSensor(coord).device_info is provided
    assert EnphaseGridModeSensor(coord).device_info is provided
    assert EnphaseTypeInventorySensor(coord, "envoy").device_info is provided
    assert EnphaseSiteLastUpdateSensor(coord).device_info is provided
    assert EnphaseCloudLatencySensor(coord).device_info is provided

    # Cover error/fallback handling in inventory/status sensors.
    inventory = EnphaseTypeInventorySensor(coord, "envoy")
    assert inventory.native_value == 0
    assert inventory.extra_state_attributes["devices"] == []
    assert inventory.available is True

    system_profile = EnphaseSystemProfileStatusSensor(coord)
    assert system_profile.available is False
    assert EnphaseSiteLastUpdateSensor(coord).extra_state_attributes == {}


def test_site_and_type_inventory_device_info_fallback_identifiers() -> None:
    coord = SimpleNamespace(
        site_id="site-fallback",
        last_update_success=True,
        has_type=lambda _key: True,
        type_device_info=lambda _key: None,
        type_label=lambda _key: "Gateway",
        type_bucket=lambda _key: {"count": 1, "devices": []},
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        latency_ms=None,
    )
    assert EnphaseTypeInventorySensor(coord, "envoy").device_info["identifiers"] == {
        ("enphase_ev", "type:site-fallback:envoy")
    }
    assert EnphaseSiteLastUpdateSensor(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-fallback:cloud")
    }
    assert EnphaseCloudLatencySensor(coord).device_info["identifiers"] == {
        ("enphase_ev", "type:site-fallback:cloud")
    }


def test_site_sensor_attributes_and_latency_attrs_paths() -> None:
    coord = SimpleNamespace(
        site_id="site-attrs",
        last_update_success=True,
        has_type=lambda _key: True,
        type_label=lambda _key: "Gateway",
        type_bucket=lambda _key: {"count": 1, "devices": []},
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        latency_ms=10,
    )
    assert EnphaseSiteLastUpdateSensor(coord).extra_state_attributes == {}
    assert EnphaseCloudLatencySensor(coord).extra_state_attributes == {}


def test_grid_control_status_device_info_prefers_enpower_then_envoy() -> None:
    enpower_info = {"identifiers": {("enphase_ev", "type:site-grid:enpower")}}
    envoy_info = {"identifiers": {("enphase_ev", "type:site-grid:envoy")}}
    coord = SimpleNamespace(
        site_id="site-grid",
        last_success_utc=None,
        last_update_success=True,
        grid_control_supported=True,
        grid_toggle_pending=False,
        grid_toggle_allowed=True,
        grid_toggle_blocked_reasons=[],
        grid_control_disable=False,
        grid_control_active_download=False,
        grid_control_sunlight_backup_system_check=False,
        grid_control_grid_outage_check=False,
        grid_control_user_initiated_toggle=False,
        type_device_info=lambda key: enpower_info if key == "enpower" else envoy_info,
    )
    sensor = EnphaseGridControlStatusSensor(coord)
    assert sensor.device_info is enpower_info

    coord.type_device_info = lambda key: None if key == "enpower" else envoy_info
    assert sensor.device_info is envoy_info


def test_site_sensor_type_gate_and_last_success_attrs() -> None:
    coord = SimpleNamespace(
        site_id="site-gate",
        last_update_success=True,
        has_type=lambda _key: False,
        type_label=lambda _key: "Gateway",
        type_bucket=lambda _key: {"count": 1, "devices": []},
        type_device_info=lambda _key: None,
        last_success_utc=datetime.now(timezone.utc),
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        backoff_ends_utc=None,
        latency_ms=None,
    )
    sensor = EnphaseSiteLastUpdateSensor(coord)
    assert sensor.available is False
    coord.has_type = lambda _key: True
    attrs = sensor.extra_state_attributes
    assert "last_success_utc" in attrs
