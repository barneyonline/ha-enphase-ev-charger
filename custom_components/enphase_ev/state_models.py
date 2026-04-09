from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any

from .const import BATTERY_PROFILE_DEFAULT_RESERVE


@dataclass(slots=True)
class DiscoveryState:
    _discovery_snapshot_loaded: bool = False
    _discovery_snapshot_pending: bool = False
    _discovery_snapshot_save_cancel: Any = None
    _warmup_task: Any = None
    _warmup_in_progress: bool = False
    _warmup_last_error: str | None = None
    _restored_site_energy_channels: set[str] = field(default_factory=set)
    _restored_gateway_iq_energy_router_records: list[dict[str, object]] = field(
        default_factory=list
    )
    _topology_listeners: list[Any] = field(default_factory=list)
    _topology_snapshot_cache: object | None = None
    _gateway_inventory_summary_cache: dict[str, object] = field(default_factory=dict)
    _gateway_inventory_summary_source: tuple[object, ...] | None = None
    _microinverter_inventory_summary_cache: dict[str, object] = field(
        default_factory=dict
    )
    _microinverter_inventory_summary_source: tuple[object, ...] | None = None
    _heatpump_inventory_summary_cache: dict[str, object] = field(default_factory=dict)
    _heatpump_inventory_summary_source: tuple[object, ...] | None = None
    _heatpump_type_summaries_cache: dict[str, dict[str, object]] = field(
        default_factory=dict
    )
    _heatpump_type_summaries_source: tuple[object, ...] | None = None
    _gateway_iq_energy_router_records_cache: list[dict[str, object]] = field(
        default_factory=list
    )
    _gateway_iq_energy_router_records_source: tuple[object, ...] | None = None
    _gateway_iq_energy_router_records_by_key_cache: dict[str, dict[str, object]] = (
        field(default_factory=dict)
    )
    _topology_refresh_suppressed: int = 0
    _topology_refresh_pending: bool = False
    _site_energy_discovery_ready: bool = False
    _hems_inventory_ready: bool = False
    _devices_inventory_ready: bool = False
    _debug_summary_log_cache: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BatteryControlCapability:
    show: bool | None = None
    enabled: bool | None = None
    locked: bool | None = None
    show_day_schedule: bool | None = None
    schedule_supported: bool | None = None
    force_schedule_supported: bool | None = None
    force_schedule_opted: bool | None = None


@dataclass(slots=True)
class EndpointFamilyHealth:
    consecutive_failures: int = 0
    last_success_utc: datetime | None = None
    last_success_mono: float | None = None
    last_failure_utc: datetime | None = None
    last_status: int | None = None
    next_retry_mono: float | None = None
    next_retry_utc: datetime | None = None
    cooldown_active: bool = False
    support_state: str = "unknown"
    last_error: str | None = None


@dataclass(slots=True)
class RefreshHealthState:
    last_set_amps: dict[str, int] = field(default_factory=dict)
    _amp_restart_tasks: dict[str, Any] = field(default_factory=dict)
    last_success_utc: datetime | None = None
    latency_ms: int | None = None
    last_failure_utc: datetime | None = None
    last_failure_status: int | None = None
    last_failure_description: str | None = None
    last_failure_response: str | None = None
    last_failure_source: str | None = None
    last_failure_endpoint: str | None = None
    payload_using_stale: bool = False
    payload_failure_kind: str | None = None
    backoff_ends_utc: datetime | None = None
    _unauth_errors: int = 0
    _rate_limit_hits: int = 0
    _http_errors: int = 0
    _network_errors: int = 0
    _payload_errors: int = 0
    _cloud_issue_reported: bool = False
    _backoff_until: float | None = None
    _backoff_cancel: Any = None
    _last_error: str | None = None
    _streaming: bool = False
    _streaming_until: float | None = None
    _streaming_manual: bool = False
    _streaming_targets: dict[str, bool] = field(default_factory=dict)
    _streaming_stop_task: Any = None
    _network_issue_reported: bool = False
    _dns_failures: int = 0
    _dns_issue_reported: bool = False
    _scheduler_available: bool = True
    _scheduler_failures: int = 0
    _scheduler_last_error: str | None = None
    _scheduler_last_failure_utc: datetime | None = None
    _scheduler_backoff_until: float | None = None
    _scheduler_backoff_ends_utc: datetime | None = None
    _scheduler_issue_reported: bool = False
    _auth_settings_available: bool = True
    _auth_settings_failures: int = 0
    _auth_settings_last_error: str | None = None
    _auth_settings_last_failure_utc: datetime | None = None
    _auth_settings_backoff_until: float | None = None
    _auth_settings_backoff_ends_utc: datetime | None = None
    _auth_settings_issue_reported: bool = False
    _auth_refresh_task: Any = None
    _auth_refresh_rejected_until: float | None = None
    _auth_refresh_rejected_ends_utc: datetime | None = None
    _auth_refresh_last_success_mono: float | None = None
    _session_history_issue_reported: bool = False
    _site_energy_issue_reported: bool = False
    _payload_health: dict[str, dict[str, object]] = field(default_factory=dict)
    _phase_timings: dict[str, float] = field(default_factory=dict)
    _bootstrap_phase_timings: dict[str, float] = field(default_factory=dict)
    _warmup_phase_timings: dict[str, float] = field(default_factory=dict)
    _has_successful_refresh: bool = False
    _session_history_cache_shim: dict[tuple[str, str], tuple[float, list[dict]]] = (
        field(default_factory=dict)
    )
    _session_history_interval_min: int = 0
    _session_history_cache_ttl_value: float | None = None
    _session_history_day_retention: int = 0
    _operating_v: dict[str, int] = field(default_factory=dict)
    _fast_until: float | None = None
    _endpoint_family_health: dict[str, EndpointFamilyHealth] = field(
        default_factory=dict
    )
    _endpoint_manual_bypass_requested: bool = False
    _endpoint_manual_bypass_active: bool = False


@dataclass(slots=True)
class InventoryState:
    _inverters_inventory_cache_until: float | None = None
    _devices_inventory_cache_until: float | None = None
    _devices_inventory_payload: dict[str, object] | None = None
    _status_payload_cache: dict[str, object] | None = None
    _system_dashboard_cache_until: float | None = None
    _system_dashboard_devices_tree_raw: dict[str, object] | None = None
    _system_dashboard_devices_tree_payload: dict[str, object] | None = None
    _system_dashboard_devices_details_raw: dict[str, dict[str, dict[str, object]]] = (
        field(default_factory=dict)
    )
    _system_dashboard_devices_details_payloads: dict[str, dict[str, object]] = field(
        default_factory=dict
    )
    _system_dashboard_hierarchy_index: dict[str, dict[str, object]] = field(
        default_factory=dict
    )
    _system_dashboard_hierarchy_summary: dict[str, object] = field(default_factory=dict)
    _system_dashboard_type_summaries: dict[str, dict[str, object]] = field(
        default_factory=dict
    )
    _inverters_inventory_payload: dict[str, object] | None = None
    _inverter_status_cache_until: float | None = None
    _inverter_status_payload: dict[str, object] | None = None
    _inverter_production_cache_until: float | None = None
    _inverter_production_payload: dict[str, object] | None = None
    _inverter_data: dict[str, dict[str, object]] = field(default_factory=dict)
    _inverter_order: list[str] = field(default_factory=list)
    _inverter_panel_info: dict[str, object] | None = None
    _inverter_status_type_counts: dict[str, int] = field(default_factory=dict)
    _inverter_summary_counts: dict[str, int] = field(
        default_factory=lambda: {
            "total": 0,
            "normal": 0,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
        }
    )
    _inverter_model_counts: dict[str, int] = field(default_factory=dict)
    _type_device_buckets: dict[str, dict[str, object]] = field(default_factory=dict)
    _type_device_order: list[str] = field(default_factory=list)
    _inverter_production_cache_key: tuple[str, str] | None = None


@dataclass(slots=True)
class HeatpumpState:
    _hems_support_preflight_cache_until: float | None = None
    _hems_devices_cache_until: float | None = None
    _hems_devices_payload: dict[str, object] | None = None
    _hems_devices_last_success_mono: float | None = None
    _hems_devices_last_success_utc: datetime | None = None
    _hems_devices_using_stale: bool = False
    _heatpump_runtime_diagnostics_cache_until: float | None = None
    _show_livestream_payload: dict[str, object] | None = None
    _heatpump_events_payloads: list[dict[str, object]] = field(default_factory=list)
    _heatpump_runtime_diagnostics_error: str | None = None
    _heatpump_runtime_state: dict[str, object] | None = None
    _heatpump_runtime_state_cache_until: float | None = None
    _heatpump_runtime_state_backoff_until: float | None = None
    _heatpump_runtime_state_last_error: str | None = None
    _heatpump_runtime_state_last_success_mono: float | None = None
    _heatpump_runtime_state_last_success_utc: datetime | None = None
    _heatpump_runtime_state_using_stale: bool = False
    _heatpump_daily_consumption: dict[str, object] | None = None
    _heatpump_daily_consumption_cache_until: float | None = None
    _heatpump_daily_consumption_backoff_until: float | None = None
    _heatpump_daily_consumption_last_error: str | None = None
    _heatpump_daily_consumption_cache_key: tuple[str, str] | None = None
    _heatpump_daily_consumption_last_success_mono: float | None = None
    _heatpump_daily_consumption_last_success_utc: datetime | None = None
    _heatpump_daily_consumption_using_stale: bool = False
    _current_power_consumption_w: float | None = None
    _current_power_consumption_sample_utc: datetime | None = None
    _current_power_consumption_reported_units: str | None = None
    _current_power_consumption_reported_precision: int | None = None
    _current_power_consumption_source: str | None = None
    _heatpump_power_w: float | None = None
    _heatpump_power_sample_utc: datetime | None = None
    _heatpump_power_start_utc: datetime | None = None
    _heatpump_power_device_uid: str | None = None
    _heatpump_power_source: str | None = None
    _heatpump_power_cache_until: float | None = None
    _heatpump_power_backoff_until: float | None = None
    _heatpump_power_last_error: str | None = None
    _heatpump_power_last_success_mono: float | None = None
    _heatpump_power_last_success_utc: datetime | None = None
    _heatpump_power_using_stale: bool = False
    _heatpump_power_selection_marker: tuple[tuple[str, str, str, str], ...] | None = (
        None
    )
    _heatpump_power_snapshot: dict[str, object] | None = None


@dataclass(slots=True)
class EVSEState:
    _charge_mode_cache: dict[str, tuple[str, float]] = field(default_factory=dict)
    _green_battery_cache: dict[str, tuple[bool | None, bool, float]] = field(
        default_factory=dict
    )
    _charger_config_cache: dict[str, tuple[dict[str, object], float]] = field(
        default_factory=dict
    )
    _charger_config_backoff_until: dict[str, float] = field(default_factory=dict)
    _auth_settings_cache: dict[
        str, tuple[bool | None, bool | None, bool, bool, float]
    ] = field(default_factory=dict)
    _evse_feature_flags_cache_until: float | None = None
    _evse_feature_flags_payload: dict[str, object] | None = None
    _evse_site_feature_flags: dict[str, object] = field(default_factory=dict)
    _evse_feature_flags_by_serial: dict[str, dict[str, object]] = field(
        default_factory=dict
    )
    _last_charging: dict[str, bool] = field(default_factory=dict)
    _last_actual_charging: dict[str, bool | None] = field(default_factory=dict)
    _pending_charging: dict[str, tuple[bool, float]] = field(default_factory=dict)
    _desired_charging: dict[str, bool] = field(default_factory=dict)
    _auto_resume_attempts: dict[str, float] = field(default_factory=dict)
    _session_end_fix: dict[str, int] = field(default_factory=dict)
    _evse_power_snapshots: dict[str, dict[str, object]] = field(default_factory=dict)
    _evse_transition_snapshots: dict[str, list[dict[str, object]]] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class BatteryState:
    _storm_guard_state: str | None = None
    _storm_evse_enabled: bool | None = None
    _storm_guard_pending_state: str | None = None
    _storm_guard_pending_expires_mono: float | None = None
    _storm_alert_active: bool | None = None
    _storm_alert_critical_override: bool | None = None
    _storm_alerts: list[dict[str, object]] = field(default_factory=list)
    _storm_guard_cache_until: float | None = None
    _storm_alert_cache_until: float | None = None
    _grid_control_check_cache_until: float | None = None
    _grid_control_check_last_success_mono: float | None = None
    _grid_control_check_failures: int = 0
    _grid_control_check_payload: dict[str, object] | None = None
    _grid_control_disable: bool | None = None
    _grid_control_active_download: bool | None = None
    _grid_control_sunlight_backup_system_check: bool | None = None
    _grid_control_grid_outage_check: bool | None = None
    _grid_control_user_initiated_toggle: bool | None = None
    _grid_control_supported: bool | None = None
    _dry_contact_settings_cache_until: float | None = None
    _dry_contact_settings_last_success_mono: float | None = None
    _dry_contact_settings_failures: int = 0
    _dry_contact_settings_payload: dict[str, object] | None = None
    _dry_contact_settings_supported: bool | None = None
    _dry_contact_settings_entries: list[dict[str, object]] = field(default_factory=list)
    _dry_contact_unmatched_settings: list[dict[str, object]] = field(
        default_factory=list
    )
    _battery_site_settings_cache_until: float | None = None
    _battery_show_production: bool | None = None
    _battery_show_consumption: bool | None = None
    _battery_show_charge_from_grid: bool | None = None
    _battery_show_savings_mode: bool | None = None
    _battery_show_ai_optimisation_mode: bool | None = None
    _battery_is_emea: bool | None = None
    _battery_show_ai_opti_savings_mode: bool | None = None
    _battery_show_storm_guard: bool | None = None
    _battery_show_full_backup: bool | None = None
    _battery_show_battery_backup_percentage: bool | None = None
    _battery_is_charging_modes_enabled: bool | None = None
    _battery_has_encharge: bool | None = None
    _battery_has_acb: bool | None = None
    _battery_has_enpower: bool | None = None
    _battery_country_code: str | None = None
    _battery_region: str | None = None
    _battery_locale: str | None = None
    _battery_timezone: str | None = None
    _battery_feature_details: dict[str, object] = field(default_factory=dict)
    _battery_user_is_owner: bool | None = None
    _battery_user_is_installer: bool | None = None
    _battery_site_status_code: str | None = None
    _battery_site_status_text: str | None = None
    _battery_site_status_severity: str | None = None
    _battery_profile: str | None = None
    _battery_backup_percentage: int | None = None
    _battery_backup_percentage_min: int | None = None
    _battery_backup_percentage_max: int | None = None
    _battery_operation_mode_sub_type: str | None = None
    _battery_supports_mqtt: bool | None = None
    _battery_polling_interval_s: int | None = None
    _battery_dtg_control: BatteryControlCapability | None = None
    _battery_cfg_control_show: bool | None = None
    _battery_cfg_control_enabled: bool | None = None
    _battery_cfg_control_schedule_supported: bool | None = None
    _battery_cfg_control_force_schedule_supported: bool | None = None
    _battery_cfg_control: BatteryControlCapability | None = None
    _battery_rbd_control: BatteryControlCapability | None = None
    _battery_system_task: bool | None = None
    _battery_profile_evse_device: dict[str, object] | None = None
    _battery_use_battery_for_self_consumption: bool | None = None
    _battery_profile_devices: list[dict[str, object]] = field(default_factory=list)
    _battery_pending_profile: str | None = None
    _battery_pending_reserve: int | None = None
    _battery_pending_sub_type: str | None = None
    _battery_pending_requested_at: datetime | None = None
    _battery_pending_require_exact_settings: bool = True
    _battery_backend_profile_update_pending: bool | None = None
    _battery_backend_not_pending_observed_at: datetime | None = None
    _battery_profile_reserve_memory: dict[str, int] = field(
        default_factory=lambda: dict(BATTERY_PROFILE_DEFAULT_RESERVE)
    )
    _battery_profile_issue_reported: bool = False
    _battery_profile_write_lock: Any = None
    _battery_profile_last_write_mono: float | None = None
    _battery_settings_write_lock: Any = None
    _battery_settings_last_write_mono: float | None = None
    _battery_settings_cache_until: float | None = None
    _battery_grid_mode: str | None = None
    _battery_hide_charge_from_grid: bool | None = None
    _battery_envoy_supports_vls: bool | None = None
    _battery_charge_from_grid: bool | None = None
    _battery_charge_from_grid_schedule_enabled: bool | None = None
    _battery_charge_begin_time: int | None = None
    _battery_charge_end_time: int | None = None
    _battery_cfg_schedule_limit: int | None = None
    _battery_cfg_schedule_id: str | None = None
    _battery_cfg_schedule_days: list[int] | None = None
    _battery_cfg_schedule_timezone: str | None = None
    _battery_cfg_schedule_status: str | None = None
    _battery_cfg_schedule_enabled: bool | None = None
    _battery_dtg_begin_time: int | None = None
    _battery_dtg_end_time: int | None = None
    _battery_dtg_control_begin_time: int | None = None
    _battery_dtg_control_end_time: int | None = None
    _battery_dtg_schedule_limit: int | None = None
    _battery_dtg_schedule_id: str | None = None
    _battery_dtg_schedule_days: list[int] | None = None
    _battery_dtg_schedule_timezone: str | None = None
    _battery_dtg_schedule_status: str | None = None
    _battery_dtg_schedule_enabled: bool | None = None
    _battery_rbd_begin_time: int | None = None
    _battery_rbd_end_time: int | None = None
    _battery_rbd_control_begin_time: int | None = None
    _battery_rbd_control_end_time: int | None = None
    _battery_rbd_schedule_limit: int | None = None
    _battery_rbd_schedule_id: str | None = None
    _battery_rbd_schedule_days: list[int] | None = None
    _battery_rbd_schedule_timezone: str | None = None
    _battery_rbd_schedule_status: str | None = None
    _battery_rbd_schedule_enabled: bool | None = None
    _battery_schedules_payload: dict[str, object] | None = None
    _battery_accepted_itc_disclaimer: str | None = None
    _battery_very_low_soc: int | None = None
    _battery_very_low_soc_min: int | None = None
    _battery_very_low_soc_max: int | None = None
    _battery_site_settings_payload: dict[str, object] | None = None
    _battery_profile_payload: dict[str, object] | None = None
    _battery_settings_payload: dict[str, object] | None = None
    _battery_status_cache_until: float | None = None
    _battery_status_payload: dict[str, object] | None = None
    _battery_backup_history_payload: dict[str, object] | None = None
    _battery_backup_history_events: list[dict[str, object]] = field(
        default_factory=list
    )
    _battery_backup_history_cache_until: float | None = None
    _battery_storage_data: dict[str, dict[str, object]] = field(default_factory=dict)
    _battery_storage_order: list[str] = field(default_factory=list)
    _battery_aggregate_charge_pct: float | None = None
    _battery_aggregate_status: str | None = None
    _battery_aggregate_status_details: dict[str, object] = field(default_factory=dict)
    _battery_summary_sample_utc: datetime | None = None
    _ac_battery_devices_cache_until: float | None = None
    _ac_battery_devices_payload: dict[str, object] | None = None
    _ac_battery_devices_html_payload: dict[str, object] | None = None
    _ac_battery_telemetry_cache_until: float | None = None
    _ac_battery_telemetry_payloads: dict[str, object] = field(default_factory=dict)
    _ac_battery_events_payloads: dict[str, object] = field(default_factory=dict)
    _ac_battery_data: dict[str, dict[str, object]] = field(default_factory=dict)
    _ac_battery_order: list[str] = field(default_factory=list)
    _ac_battery_aggregate_status: str | None = None
    _ac_battery_aggregate_status_details: dict[str, object] = field(
        default_factory=dict
    )
    _ac_battery_power_w: float | None = None
    _ac_battery_summary_sample_utc: datetime | None = None
    _ac_battery_selected_sleep_min_soc: int | None = None
    _ac_battery_sleep_state: str | None = None
    _ac_battery_control_pending: bool = False
    _ac_battery_last_command: dict[str, object] | None = None


STATE_MODELS = {
    "discovery_state": DiscoveryState,
    "refresh_state": RefreshHealthState,
    "inventory_state": InventoryState,
    "heatpump_state": HeatpumpState,
    "evse_state": EVSEState,
    "battery_state": BatteryState,
}


class StateBackedAttribute:
    """Descriptor that stores coordinator fields on grouped runtime state."""

    def __init__(self, state_model_name: str, attribute_name: str) -> None:
        self._state_model_name = state_model_name
        self._attribute_name = attribute_name

    def __get__(self, instance: object, owner: type | None = None) -> Any:
        if instance is None:
            return self
        state_model = getattr(instance, "__dict__", {}).get(self._state_model_name)
        if state_model is not None:
            return getattr(state_model, self._attribute_name)
        if self._attribute_name in getattr(instance, "__dict__", {}):
            return instance.__dict__[self._attribute_name]
        raise AttributeError(
            f"{type(instance).__name__} has no attribute {self._attribute_name!r}"
        )

    def __set__(self, instance: object, value: Any) -> None:
        state_model = getattr(instance, "__dict__", {}).get(self._state_model_name)
        if state_model is not None:
            setattr(state_model, self._attribute_name, value)
            return
        instance.__dict__[self._attribute_name] = value


def install_state_descriptors(target_cls: type) -> None:
    """Install explicit coordinator descriptors for grouped runtime fields."""

    for model_name, model_type in STATE_MODELS.items():
        for model_field in fields(model_type):
            if model_field.name in target_cls.__dict__:
                continue
            setattr(
                target_cls,
                model_field.name,
                StateBackedAttribute(model_name, model_field.name),
            )
