from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from datetime import timezone as _tz
from http import HTTPStatus
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import aiohttp
from email.utils import parsedate_to_datetime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AuthTokens,
    AuthSettingsUnavailable,
    EnlightenAuthInvalidCredentials,
    EnlightenAuthMFARequired,
    EnlightenAuthUnavailable,
    EnphaseEVClient,
    InvalidPayloadError,
    SchedulerUnavailable,
    Unauthorized,
    async_authenticate,
    is_scheduler_unavailable_error,
)
from .const import (
    AUTH_APP_SETTING,
    AUTH_RFID_SETTING,
    CONF_ACCESS_TOKEN,
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_EMAIL,
    CONF_INCLUDE_INVERTERS,
    CONF_PASSWORD,
    CONF_REMEMBER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_TYPE_KEYS,
    CONF_SERIALS,
    CONF_SESSION_ID,
    CONF_SITE_ID,
    CONF_SITE_ONLY,
    CONF_SITE_NAME,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_API_TIMEOUT,
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLOW_POLL_INTERVAL,
    DOMAIN,
    GREEN_BATTERY_SETTING,
    ISSUE_NETWORK_UNREACHABLE,
    ISSUE_DNS_RESOLUTION,
    ISSUE_CLOUD_ERRORS,
    ISSUE_SCHEDULER_UNAVAILABLE,
    ISSUE_SESSION_HISTORY_UNAVAILABLE,
    ISSUE_SITE_ENERGY_UNAVAILABLE,
    ISSUE_AUTH_SETTINGS_UNAVAILABLE,
    ISSUE_BATTERY_PROFILE_PENDING,
    OPT_API_TIMEOUT,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
    OPT_SESSION_HISTORY_INTERVAL,
    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
    SAFE_LIMIT_AMPS,
)
from .device_types import (
    member_is_retired,
    normalize_type_key,
    parse_type_identifier,
    sanitize_member,
    type_display_label,
    type_identifier,
)
from .device_info_helpers import _is_redundant_model_id
from .energy import EnergyManager
from .evse_timeseries import EVSETimeseriesManager
from .log_redaction import (
    redact_identifier,
    redact_site_id,
    redact_text,
    truncate_identifier,
)
from .session_history import (
    MIN_SESSION_HISTORY_CACHE_TTL,
    SESSION_HISTORY_CACHE_DAY_RETENTION,
    SESSION_HISTORY_CONCURRENCY,
    SESSION_HISTORY_FAILURE_BACKOFF_S,
    SessionHistoryManager,
)
from .summary import SummaryStore
from .voltage import (
    coerce_nominal_voltage,
    preferred_operating_voltage,
    resolve_nominal_voltage_for_hass,
)

_LOGGER = logging.getLogger(__name__)
GREEN_BATTERY_CACHE_TTL = 300.0
AUTH_SETTINGS_CACHE_TTL = 300.0
STORM_GUARD_CACHE_TTL = 300.0
STORM_ALERT_CACHE_TTL = 60.0
STORM_GUARD_PENDING_HOLD_S = 90.0
GRID_CONTROL_CHECK_CACHE_TTL = 60.0
GRID_CONTROL_CHECK_STALE_AFTER_S = 180.0
DRY_CONTACT_SETTINGS_CACHE_TTL = 300.0
DRY_CONTACT_SETTINGS_FAILURE_CACHE_TTL = 15.0
DRY_CONTACT_SETTINGS_STALE_AFTER_S = 900.0
EVSE_FEATURE_FLAGS_CACHE_TTL = 1800.0
BATTERY_SITE_SETTINGS_CACHE_TTL = 300.0
HEMS_SUPPORT_PREFLIGHT_CACHE_TTL = 15.0
BATTERY_SETTINGS_CACHE_TTL = 300.0
BATTERY_BACKUP_HISTORY_CACHE_TTL = 300.0
BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL = 60.0
DEVICES_INVENTORY_CACHE_TTL = 300.0
HEMS_DEVICES_STALE_AFTER_S = 90.0
# HEMS heat-pump status/power can lag the Enphase app by only a few seconds.
# Keep these caches short so we do not hold stale or empty telemetry for minutes.
HEMS_DEVICES_CACHE_TTL = 15.0
HEATPUMP_POWER_CACHE_TTL = 15.0
HEATPUMP_POWER_FAILURE_BACKOFF_S = 900.0
SYSTEM_DASHBOARD_DIAGNOSTIC_TYPES: tuple[str, ...] = (
    "envoys",
    "meters",
    "enpowers",
    "encharges",
    "modems",
    "inverters",
)
SYSTEM_DASHBOARD_TYPE_KEY_MAP: dict[str, str] = {
    "envoys": "envoy",
    "meters": "envoy",
    "enpowers": "envoy",
    "encharges": "encharge",
    "inverters": "microinverter",
    "modems": "modem",
}
SAVINGS_OPERATION_MODE_SUBTYPE = "prioritize-energy"
BATTERY_PROFILE_PENDING_TIMEOUT_S = 900.0
BATTERY_PROFILE_WRITE_DEBOUNCE_S = 2.0
BATTERY_SETTINGS_WRITE_DEBOUNCE_S = 2.0
DISCOVERY_SNAPSHOT_STORE_VERSION = 1
DISCOVERY_SNAPSHOT_SAVE_DELAY_S = 1.0


@dataclass(frozen=True)
class CoordinatorTopologySnapshot:
    charger_serials: tuple[str, ...]
    battery_serials: tuple[str, ...]
    inverter_serials: tuple[str, ...]
    active_type_keys: tuple[str, ...]
    gateway_iq_router_keys: tuple[str, ...]
    inventory_ready: bool


BATTERY_PROFILE_LABELS = {
    "self-consumption": "Self-Consumption",
    "cost_savings": "Savings",
    "backup_only": "Full Backup",
}
BATTERY_PROFILE_DEFAULT_RESERVE = {
    "self-consumption": 20,
    "cost_savings": 20,
    "backup_only": 100,
}
BATTERY_MIN_SOC_FALLBACK = 5
BATTERY_GRID_MODE_LABELS = {
    "importexport": "Import and Export",
    "importonly": "Import Only",
    "exportonly": "Export Only",
}
BATTERY_GRID_MODE_PERMISSIONS = {
    "importexport": (True, True),
    "importonly": (True, False),
    "exportonly": (False, True),
}
BATTERY_STATUS_SEVERITY = {
    "normal": 0,
    "unknown": 1,
    "warning": 2,
    "error": 3,
}

ACTIVE_CONNECTOR_STATUSES = {"CHARGING", "FINISHING", "SUSPENDED"}
ACTIVE_SUSPENDED_PREFIXES = ("SUSPENDED_EV",)
SUSPENDED_EVSE_STATUS = "SUSPENDED_EVSE"
FAST_TOGGLE_POLL_HOLD_S = 60
AMP_RESTART_DELAY_S = 30.0
STREAMING_DEFAULT_DURATION_S = 900.0
STORM_ALERT_INACTIVE_STATUSES = frozenset(
    {
        "opted-out",
        "inactive",
        "expired",
        "cleared",
        "ended",
        "resolved",
    }
)


@dataclass(slots=True)
class ChargerState:
    sn: str
    name: str | None
    connected: bool
    plugged: bool
    charging: bool
    faulted: bool
    connector_status: str | None
    session_kwh: float | None
    session_start: int | None


@dataclass(slots=True)
class ChargeModeStartPreferences:
    mode: str | None = None
    include_level: bool | None = None
    strict: bool = False
    enforce_mode: str | None = None


class EnphaseCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, config, config_entry=None):
        self.hass = hass
        self.config_entry = config_entry
        self.site_id = str(config[CONF_SITE_ID])
        raw_serials = config.get(CONF_SERIALS) or []
        self.serials: set[str] = set()
        self._serial_order: list[str] = []
        if isinstance(raw_serials, (list, tuple, set)):
            normalized_serials: list[str] = []
            for sn in raw_serials:
                if sn is None:
                    continue
                try:
                    normalized = str(sn).strip()
                except Exception:
                    continue
                if not normalized:
                    continue
                normalized_serials.append(normalized)
            self.serials.update(normalized_serials)
            self._serial_order.extend(list(dict.fromkeys(normalized_serials)))
        else:
            if raw_serials is not None:
                try:
                    normalized = str(raw_serials).strip()
                except Exception:
                    normalized = ""
                if normalized:
                    self.serials = {normalized}
                    self._serial_order.append(normalized)
        self._configured_serials: set[str] = set(self.serials)
        raw_site_only = config.get(CONF_SITE_ONLY, None)
        if raw_site_only is None and config_entry is not None:
            raw_site_only = config_entry.options.get(CONF_SITE_ONLY)
        self.site_only = bool(raw_site_only)
        self.include_inverters = bool(config.get(CONF_INCLUDE_INVERTERS, True))
        raw_selected_type_keys = config.get(CONF_SELECTED_TYPE_KEYS, None)
        self._selected_type_keys: set[str] | None = None
        if raw_selected_type_keys is not None:
            if isinstance(raw_selected_type_keys, (list, tuple, set)):
                iterable = raw_selected_type_keys
                selected_keys: set[str] = set()
                for key in iterable:
                    normalized = normalize_type_key(key)
                    if normalized:
                        selected_keys.add(normalized)
                self._selected_type_keys = selected_keys
            elif isinstance(raw_selected_type_keys, str):
                iterable = [raw_selected_type_keys]
                selected_keys = set()
                for key in iterable:
                    normalized = normalize_type_key(key)
                    if normalized:
                        selected_keys.add(normalized)
                if selected_keys:
                    self._selected_type_keys = selected_keys

        self.site_name = config.get(CONF_SITE_NAME)
        self._email = config.get(CONF_EMAIL)
        self._remember_password = bool(config.get(CONF_REMEMBER_PASSWORD))
        self._stored_password = config.get(CONF_PASSWORD)
        cookie = config.get(CONF_COOKIE, "") or ""
        access_token = config.get(CONF_EAUTH) or config.get(CONF_ACCESS_TOKEN)
        self._tokens = AuthTokens(
            cookie=cookie,
            session_id=config.get(CONF_SESSION_ID),
            access_token=access_token,
            token_expires_at=config.get(CONF_TOKEN_EXPIRES_AT),
        )
        timeout = (
            int(config_entry.options.get(OPT_API_TIMEOUT, DEFAULT_API_TIMEOUT))
            if config_entry
            else DEFAULT_API_TIMEOUT
        )
        self.client = EnphaseEVClient(
            async_get_clientsession(hass),
            self.site_id,
            self._tokens.access_token,
            self._tokens.cookie,
            timeout=timeout,
        )
        set_reauth_cb = getattr(self.client, "set_reauth_callback", None)
        if callable(set_reauth_cb):
            result = set_reauth_cb(self._handle_client_unauthorized)
            if inspect.isawaitable(result):
                self.hass.async_create_task(result)
        from .schedule_sync import ScheduleSync

        self.schedule_sync = ScheduleSync(hass, self, config_entry)
        entry_id = getattr(config_entry, "entry_id", self.site_id)
        self._discovery_snapshot_store = Store(
            hass,
            DISCOVERY_SNAPSHOT_STORE_VERSION,
            f"{DOMAIN}.discovery_snapshot.{entry_id}",
        )
        self._discovery_snapshot_loaded = False
        self._discovery_snapshot_pending = False
        self._discovery_snapshot_save_cancel: Callable[[], None] | None = None
        self._warmup_task: asyncio.Task | None = None
        self._warmup_in_progress = False
        self._warmup_last_error: str | None = None
        self._restored_site_energy_channels: set[str] = set()
        self._restored_gateway_iq_energy_router_records: list[dict[str, object]] = []
        self._topology_listeners: list[Callable[[], None]] = []
        self._topology_snapshot_cache = CoordinatorTopologySnapshot(
            charger_serials=(),
            battery_serials=(),
            inverter_serials=(),
            active_type_keys=(),
            gateway_iq_router_keys=(),
            inventory_ready=False,
        )
        self._gateway_inventory_summary_cache: dict[str, object] = {}
        self._gateway_inventory_summary_source: tuple[object, ...] | None = None
        self._microinverter_inventory_summary_cache: dict[str, object] = {}
        self._microinverter_inventory_summary_source: tuple[object, ...] | None = None
        self._heatpump_inventory_summary_cache: dict[str, object] = {}
        self._heatpump_inventory_summary_source: tuple[object, ...] | None = None
        self._heatpump_type_summaries_cache: dict[str, dict[str, object]] = {}
        self._heatpump_type_summaries_source: tuple[object, ...] | None = None
        self._gateway_iq_energy_router_records_cache: list[dict[str, object]] = []
        self._gateway_iq_energy_router_records_source: tuple[object, ...] | None = None
        self._gateway_iq_energy_router_records_by_key_cache: dict[
            str, dict[str, object]
        ] = {}
        self._topology_refresh_suppressed = 0
        self._topology_refresh_pending = False
        self._site_energy_discovery_ready = False
        self._hems_inventory_ready = False
        self._debug_summary_log_cache: dict[str, object] = {}
        self._refresh_lock = asyncio.Lock()
        # Nominal voltage for estimated power when API omits voltage; user-configurable
        self._nominal_v = resolve_nominal_voltage_for_hass(hass)
        if config_entry is not None:
            configured_nominal = coerce_nominal_voltage(
                config_entry.options.get(OPT_NOMINAL_VOLTAGE)
            )
            if configured_nominal is not None:
                self._nominal_v = configured_nominal
        # Options: allow dynamic fast/slow polling
        slow = None
        if config_entry is not None:
            slow = int(
                config_entry.options.get(
                    OPT_SLOW_POLL_INTERVAL,
                    config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                )
            )
        interval = slow or config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        self._configured_slow_poll_interval = max(1, int(interval))
        self.last_set_amps: dict[str, int] = {}
        self._amp_restart_tasks: dict[str, asyncio.Task] = {}
        self.last_success_utc = None
        self.latency_ms: int | None = None
        self.last_failure_utc = None
        self.last_failure_status: int | None = None
        self.last_failure_description: str | None = None
        self.last_failure_response: str | None = None
        self.last_failure_source: str | None = None
        self.backoff_ends_utc = None
        self._unauth_errors = 0
        self._rate_limit_hits = 0
        self._http_errors = 0
        self._network_errors = 0
        self._payload_errors = 0
        self._cloud_issue_reported = False
        self._backoff_until: float | None = None
        self._backoff_cancel: Callable[[], None] | None = None
        self._last_error: str | None = None
        self._streaming: bool = False
        self._streaming_until: float | None = None
        self._streaming_manual: bool = False
        self._streaming_targets: dict[str, bool] = {}
        self._streaming_stop_task: asyncio.Task | None = None
        self._network_issue_reported = False
        self._dns_failures = 0
        self._dns_issue_reported = False
        self._scheduler_available = True
        self._scheduler_failures = 0
        self._scheduler_last_error: str | None = None
        self._scheduler_last_failure_utc: datetime | None = None
        self._scheduler_backoff_until: float | None = None
        self._scheduler_backoff_ends_utc: datetime | None = None
        self._scheduler_issue_reported = False
        self._auth_settings_available = True
        self._auth_settings_failures = 0
        self._auth_settings_last_error: str | None = None
        self._auth_settings_last_failure_utc: datetime | None = None
        self._auth_settings_backoff_until: float | None = None
        self._auth_settings_backoff_ends_utc: datetime | None = None
        self._auth_settings_issue_reported = False
        self._session_history_issue_reported = False
        self._site_energy_issue_reported = False
        self._devices_inventory_cache_until: float | None = None
        self._devices_inventory_payload: dict[str, object] | None = None
        self._system_dashboard_cache_until: float | None = None
        self._system_dashboard_devices_tree_raw: dict[str, object] | None = None
        self._system_dashboard_devices_tree_payload: dict[str, object] | None = None
        self._system_dashboard_devices_details_raw: dict[
            str, dict[str, dict[str, object]]
        ] = {}
        self._system_dashboard_devices_details_payloads: dict[
            str, dict[str, object]
        ] = {}
        self._system_dashboard_hierarchy_index: dict[str, dict[str, object]] = {}
        self._system_dashboard_hierarchy_summary: dict[str, object] = {}
        self._system_dashboard_type_summaries: dict[str, dict[str, object]] = {}
        self._hems_support_preflight_cache_until: float | None = None
        self._hems_devices_cache_until: float | None = None
        self._hems_devices_payload: dict[str, object] | None = None
        self._hems_devices_last_success_mono: float | None = None
        self._hems_devices_last_success_utc: datetime | None = None
        self._hems_devices_using_stale = False
        self._devices_inventory_ready: bool = False
        self._current_power_consumption_w: float | None = None
        self._current_power_consumption_sample_utc: datetime | None = None
        self._current_power_consumption_reported_units: str | None = None
        self._current_power_consumption_reported_precision: int | None = None
        self._current_power_consumption_source: str | None = None
        self._heatpump_power_w: float | None = None
        self._heatpump_power_sample_utc: datetime | None = None
        self._heatpump_power_start_utc: datetime | None = None
        self._heatpump_power_device_uid: str | None = None
        self._heatpump_power_source: str | None = None
        self._heatpump_power_cache_until: float | None = None
        self._heatpump_power_backoff_until: float | None = None
        self._heatpump_power_last_error: str | None = None
        self._heatpump_power_selection_marker: (
            tuple[tuple[str, str, str, str], ...] | None
        ) = None
        self._inverters_inventory_payload: dict[str, object] | None = None
        self._inverter_status_payload: dict[str, object] | None = None
        self._inverter_production_payload: dict[str, object] | None = None
        self._inverter_data: dict[str, dict[str, object]] = {}
        self._inverter_order: list[str] = []
        self._inverter_panel_info: dict[str, object] | None = None
        self._inverter_status_type_counts: dict[str, int] = {}
        self._inverter_summary_counts: dict[str, int] = {
            "total": 0,
            "normal": 0,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
        }
        self._inverter_model_counts: dict[str, int] = {}
        self._type_device_buckets: dict[str, dict[str, object]] = {}
        self._type_device_order: list[str] = []
        self.summary = SummaryStore(lambda: self.client, logger=_LOGGER)
        self.energy = EnergyManager(
            client_provider=lambda: self.client,
            site_id=self.site_id,
            logger=_LOGGER,
            summary_invalidator=self.summary.invalidate,
        )
        self.evse_timeseries = EVSETimeseriesManager(
            hass,
            lambda: self.client,
            logger=_LOGGER,
        )
        self._session_history_cache_shim: dict[
            tuple[str, str], tuple[float, list[dict]]
        ] = {}
        self._session_history_interval_min = DEFAULT_SESSION_HISTORY_INTERVAL_MIN
        if config_entry is not None:
            try:
                configured_interval = int(
                    config_entry.options.get(
                        OPT_SESSION_HISTORY_INTERVAL,
                        DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
                    )
                )
                if configured_interval > 0:
                    self._session_history_interval_min = configured_interval
            except Exception:
                self._session_history_interval_min = (
                    DEFAULT_SESSION_HISTORY_INTERVAL_MIN
                )
        self._session_history_cache_ttl_value = max(
            MIN_SESSION_HISTORY_CACHE_TTL, self._session_history_interval_min * 60
        )
        self._session_history_day_retention = SESSION_HISTORY_CACHE_DAY_RETENTION
        # Per-serial operating voltage learned from summary v2; used for power estimation
        self._operating_v: dict[str, int] = {}
        # Temporary fast polling window after user actions (start/stop/etc.)
        self._fast_until: float | None = None
        # Cache charge mode results to avoid extra API calls every poll
        self._charge_mode_cache: dict[str, tuple[str, float]] = {}
        # Cache green charging battery settings (enabled, supported, timestamp)
        self._green_battery_cache: dict[str, tuple[bool | None, bool, float]] = {}
        # Cache charger authentication settings (app_enabled, rfid_enabled, app_supported, rfid_supported, timestamp)
        self._auth_settings_cache: dict[
            str, tuple[bool | None, bool | None, bool, bool, float]
        ] = {}
        # Cache Storm Guard state and EVSE preference for charge-to-100%
        self._storm_guard_state: str | None = None
        self._storm_evse_enabled: bool | None = None
        self._storm_guard_pending_state: str | None = None
        self._storm_guard_pending_expires_mono: float | None = None
        self._storm_alert_active: bool | None = None
        self._storm_alert_critical_override: bool | None = None
        self._storm_alerts: list[dict[str, object]] = []
        self._storm_guard_cache_until: float | None = None
        self._storm_alert_cache_until: float | None = None
        self._grid_control_check_cache_until: float | None = None
        self._grid_control_check_last_success_mono: float | None = None
        self._grid_control_check_failures: int = 0
        self._grid_control_check_payload: dict[str, object] | None = None
        self._grid_control_disable: bool | None = None
        self._grid_control_active_download: bool | None = None
        self._grid_control_sunlight_backup_system_check: bool | None = None
        self._grid_control_grid_outage_check: bool | None = None
        self._grid_control_user_initiated_toggle: bool | None = None
        self._grid_control_supported: bool | None = None
        self._dry_contact_settings_cache_until: float | None = None
        self._dry_contact_settings_last_success_mono: float | None = None
        self._dry_contact_settings_failures: int = 0
        self._dry_contact_settings_payload: dict[str, object] | None = None
        self._dry_contact_settings_supported: bool | None = None
        self._dry_contact_settings_entries: list[dict[str, object]] = []
        self._dry_contact_unmatched_settings: list[dict[str, object]] = []
        self._evse_feature_flags_cache_until: float | None = None
        self._evse_feature_flags_payload: dict[str, object] | None = None
        self._evse_site_feature_flags: dict[str, object] = {}
        self._evse_feature_flags_by_serial: dict[str, dict[str, object]] = {}
        # Cache BatteryConfig site settings and profile details.
        self._battery_site_settings_cache_until: float | None = None
        self._battery_show_production: bool | None = None
        self._battery_show_consumption: bool | None = None
        self._battery_show_charge_from_grid: bool | None = None
        self._battery_show_savings_mode: bool | None = None
        self._battery_show_storm_guard: bool | None = None
        self._battery_show_full_backup: bool | None = None
        self._battery_show_battery_backup_percentage: bool | None = None
        self._battery_is_charging_modes_enabled: bool | None = None
        self._battery_has_encharge: bool | None = None
        self._battery_has_enpower: bool | None = None
        self._battery_country_code: str | None = None
        self._battery_region: str | None = None
        self._battery_locale: str | None = None
        self._battery_timezone: str | None = None
        self._battery_feature_details: dict[str, object] = {}
        self._battery_user_is_owner: bool | None = None
        self._battery_user_is_installer: bool | None = None
        self._battery_site_status_code: str | None = None
        self._battery_site_status_text: str | None = None
        self._battery_site_status_severity: str | None = None
        self._battery_profile: str | None = None
        self._battery_backup_percentage: int | None = None
        self._battery_operation_mode_sub_type: str | None = None
        self._battery_supports_mqtt: bool | None = None
        self._battery_polling_interval_s: int | None = None
        self._battery_cfg_control_show: bool | None = None
        self._battery_cfg_control_enabled: bool | None = None
        self._battery_cfg_control_schedule_supported: bool | None = None
        self._battery_cfg_control_force_schedule_supported: bool | None = None
        self._battery_profile_evse_device: dict[str, object] | None = None
        self._battery_use_battery_for_self_consumption: bool | None = None
        self._battery_profile_devices: list[dict[str, object]] = []
        self._battery_pending_profile: str | None = None
        self._battery_pending_reserve: int | None = None
        self._battery_pending_sub_type: str | None = None
        self._battery_pending_requested_at: datetime | None = None
        self._battery_pending_require_exact_settings: bool = True
        self._battery_profile_reserve_memory: dict[str, int] = dict(
            BATTERY_PROFILE_DEFAULT_RESERVE
        )
        self._battery_profile_issue_reported = False
        self._battery_profile_write_lock = asyncio.Lock()
        self._battery_profile_last_write_mono: float | None = None
        self._battery_settings_write_lock = asyncio.Lock()
        self._battery_settings_last_write_mono: float | None = None
        self._battery_settings_cache_until: float | None = None
        self._battery_grid_mode: str | None = None
        self._battery_hide_charge_from_grid: bool | None = None
        self._battery_envoy_supports_vls: bool | None = None
        self._battery_charge_from_grid: bool | None = None
        self._battery_charge_from_grid_schedule_enabled: bool | None = None
        self._battery_charge_begin_time: int | None = None
        self._battery_charge_end_time: int | None = None
        self._battery_cfg_schedule_limit: int | None = None
        self._battery_cfg_schedule_id: str | None = None
        self._battery_cfg_schedule_days: list[int] | None = None
        self._battery_cfg_schedule_timezone: str | None = None
        self._battery_schedules_payload: dict[str, object] | None = None
        self._battery_accepted_itc_disclaimer: str | None = None
        self._battery_very_low_soc: int | None = None
        self._battery_very_low_soc_min: int | None = None
        self._battery_very_low_soc_max: int | None = None
        self._battery_site_settings_payload: dict[str, object] | None = None
        self._battery_profile_payload: dict[str, object] | None = None
        self._battery_settings_payload: dict[str, object] | None = None
        self._battery_status_payload: dict[str, object] | None = None
        self._battery_backup_history_payload: dict[str, object] | None = None
        self._battery_backup_history_events: list[dict[str, object]] = []
        self._battery_backup_history_cache_until: float | None = None
        self._battery_storage_data: dict[str, dict[str, object]] = {}
        self._battery_storage_order: list[str] = []
        self._battery_aggregate_charge_pct: float | None = None
        self._battery_aggregate_status: str | None = None
        self._battery_aggregate_status_details: dict[str, object] = {}
        # Track charging transitions and a fixed session end timestamp so
        # session duration does not grow after charging stops
        self._last_charging: dict[str, bool] = {}
        # Track raw cloud-reported charging state for fast toggle detection
        self._last_actual_charging: dict[str, bool | None] = {}
        # Pending expectations for charger state while waiting for backend to catch up
        self._pending_charging: dict[str, tuple[bool, float]] = {}
        # Remember user-requested charging intent and resume attempts
        self._desired_charging: dict[str, bool] = {}
        self._auto_resume_attempts: dict[str, float] = {}
        self._session_end_fix: dict[str, int] = {}
        self._phase_timings: dict[str, float] = {}
        self._bootstrap_phase_timings: dict[str, float] = {}
        self._warmup_phase_timings: dict[str, float] = {}
        self._has_successful_refresh = False
        super_kwargs = {
            "name": DOMAIN,
            "update_interval": timedelta(seconds=interval),
        }
        if config_entry is not None:
            super_kwargs["config_entry"] = config_entry
        super().__init__(
            hass,
            _LOGGER,
            **super_kwargs,
        )
        self.config_entry = config_entry
        self.session_history = SessionHistoryManager(
            hass,
            lambda: self.client,
            cache_ttl=self._session_history_cache_ttl_value,
            failure_backoff=SESSION_HISTORY_FAILURE_BACKOFF_S,
            concurrency=SESSION_HISTORY_CONCURRENCY,
            data_supplier=lambda: self.data,
            publish_callback=self.async_set_updated_data,
            logger=_LOGGER,
        )

    def __setattr__(self, name, value):
        if name == "_async_fetch_sessions_today" and hasattr(self, "session_history"):
            object.__setattr__(self, name, value)
            self.session_history.set_fetch_override(value)
            return
        super().__setattr__(name, value)

    def __getattr__(self, name: str):
        if name == "energy":
            energy = EnergyManager(
                client_provider=lambda: getattr(self, "client", None),
                site_id=str(getattr(self, "site_id", "")),
                logger=_LOGGER,
                summary_invalidator=getattr(
                    getattr(self, "summary", None), "invalidate", None
                ),
            )
            self.__dict__["energy"] = energy
            return energy
        if name == "evse_timeseries":
            manager = EVSETimeseriesManager(
                self.__dict__.get("hass"),
                lambda: self.__dict__.get("client"),
                logger=_LOGGER,
            )
            self.__dict__["evse_timeseries"] = manager
            return manager
        raise AttributeError(f"{type(self).__name__} has no attribute {name!r}")

    async def _async_setup(self) -> None:
        """Prepare lightweight state before the first refresh."""
        self._phase_timings = {}

    @property
    def phase_timings(self) -> dict[str, float]:
        """Return the most recent phase timings."""
        return dict(self._phase_timings)

    @property
    def bootstrap_phase_timings(self) -> dict[str, float]:
        """Return the most recent bootstrap timings."""
        return dict(getattr(self, "_bootstrap_phase_timings", {}) or {})

    @property
    def warmup_phase_timings(self) -> dict[str, float]:
        """Return the most recent startup warmup timings."""
        return dict(getattr(self, "_warmup_phase_timings", {}) or {})

    @property
    def _summary_cache(self) -> tuple[float, list[dict], float] | None:
        """Legacy access to the summary cache tuple."""
        summary = getattr(self, "summary", None)
        if summary is None:
            return getattr(self, "_compat_summary_cache", None)
        return getattr(summary, "_cache", None)

    @_summary_cache.setter
    def _summary_cache(self, value: tuple[float, list[dict], float] | None) -> None:
        summary = getattr(self, "summary", None)
        if summary is None:
            self.__dict__["_compat_summary_cache"] = value
            return
        setattr(summary, "_cache", value)

    @property
    def _summary_ttl(self) -> float:
        """Legacy access to the current summary TTL."""
        summary = getattr(self, "summary", None)
        if summary is None:
            return getattr(self, "_compat_summary_ttl", 0.0)
        return summary.ttl

    @_summary_ttl.setter
    def _summary_ttl(self, value: float) -> None:
        summary = getattr(self, "summary", None)
        if summary is None:
            self.__dict__["_compat_summary_ttl"] = value
            return
        self.summary._ttl = value

    @property
    def _session_history_cache_ttl(self) -> float | None:
        """Expose the session history TTL for diagnostics/tests."""
        if hasattr(self, "session_history"):
            return self.session_history.cache_ttl
        return getattr(self, "_session_history_cache_ttl_value", None)

    @_session_history_cache_ttl.setter
    def _session_history_cache_ttl(self, value: float | None) -> None:
        self._session_history_cache_ttl_value = value
        if hasattr(self, "session_history"):
            self.session_history.cache_ttl = value

    @property
    def nominal_voltage(self) -> int:
        """Configured/default nominal voltage used when API voltage is missing."""
        return int(self._nominal_v)

    def preferred_nominal_voltage(self) -> int:
        """Best available nominal voltage (API-derived when available)."""
        discovered = self._preferred_operating_voltage()
        if discovered is not None:
            return discovered
        return int(self._nominal_v)

    def _preferred_operating_voltage(self) -> int | None:
        return preferred_operating_voltage(self._operating_v.values())

    def _seed_nominal_voltage_option_from_api(self) -> None:
        if self.config_entry is None:
            return

        options = dict(getattr(self.config_entry, "options", {}) or {})
        configured = coerce_nominal_voltage(options.get(OPT_NOMINAL_VOLTAGE))
        if configured is not None:
            self._nominal_v = configured
            return

        discovered = self._preferred_operating_voltage()
        if discovered is None:
            return

        options[OPT_NOMINAL_VOLTAGE] = discovered
        self._nominal_v = discovered
        try:
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=options
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to persist API-derived nominal voltage", exc_info=True
            )

    def _schedule_session_enrichment(
        self,
        serials: Iterable[str],
        day_local: datetime,
    ) -> None:
        """Compat shim delegating to the session history manager."""
        if hasattr(self, "session_history"):
            self.session_history.schedule_enrichment(serials, day_local)

    async def _async_enrich_sessions(
        self,
        serials: Iterable[str],
        day_local: datetime,
        *,
        in_background: bool,
    ) -> dict[str, list[dict]]:
        """Compat shim delegating to the session history manager."""
        if hasattr(self, "session_history"):
            return await self.session_history.async_enrich(
                serials, day_local, in_background=in_background
            )
        return {}

    def _sum_session_energy(self, sessions: list[dict]) -> float:
        """Compat shim delegating to the session history manager."""
        if hasattr(self, "session_history"):
            return self.session_history.sum_energy(sessions)
        total = 0.0
        for entry in sessions or []:
            val = entry.get("energy_kwh")
            if isinstance(val, (int, float)):
                try:
                    total += float(val)
                except Exception:  # noqa: BLE001
                    continue
        return round(total, 2)

    @staticmethod
    def _session_history_day(payload: dict, day_local_default: datetime) -> datetime:
        if payload.get("charging"):
            return day_local_default
        for key in ("session_end", "session_start"):
            ts_raw = payload.get(key)
            if ts_raw is None:
                continue
            try:
                ts_val = float(ts_raw)
            except Exception:
                ts_val = None
            if ts_val is None:
                continue
            try:
                dt_val = datetime.fromtimestamp(ts_val, tz=_tz.utc)
            except Exception:
                continue
            try:
                return dt_util.as_local(dt_val)
            except Exception:
                return dt_val
        return day_local_default

    async def _async_fetch_sessions_today(
        self,
        sn: str,
        *,
        day_local: datetime | None = None,
    ) -> list[dict]:
        """Compat shim delegating to the session history manager."""
        if not sn:
            return []
        day_ref = day_local
        if day_ref is None:
            day_ref = dt_util.now()
        try:
            local_dt = dt_util.as_local(day_ref)
        except Exception:
            if day_ref.tzinfo is None:
                day_ref = day_ref.replace(tzinfo=_tz.utc)
            local_dt = dt_util.as_local(day_ref)
        day_key = local_dt.strftime("%Y-%m-%d")
        cache_key = (str(sn), day_key)
        tracked_serials = set(self.iter_serials())
        tracked_serials.add(str(sn))
        self._prune_session_history_cache_shim(
            active_serials=tracked_serials,
            keep_day_keys={day_key},
        )
        cached = self._session_history_cache_shim.get(cache_key)
        ttl = self._session_history_cache_ttl or MIN_SESSION_HISTORY_CACHE_TTL
        if cached:
            cached_ts, cached_sessions = cached
            if time.monotonic() - cached_ts < ttl:
                return cached_sessions
        if hasattr(self, "session_history"):
            sessions = await self.session_history._async_fetch_sessions_today(
                sn, day_local=local_dt
            )
        else:
            sessions = []
        self._set_session_history_cache_shim_entry(str(sn), day_key, sessions)
        return sessions

    @staticmethod
    def _normalize_serials(serials: Iterable[str] | None) -> set[str]:
        normalized: set[str] = set()
        if serials is None:
            return normalized
        for serial in serials:
            if serial is None:
                continue
            try:
                sn = str(serial).strip()
            except Exception:  # noqa: BLE001
                continue
            if sn:
                normalized.add(sn)
        return normalized

    def _retained_session_history_days(
        self, keep_day_keys: Iterable[str] | None = None
    ) -> set[str]:
        retained = {
            str(day_key).strip()
            for day_key in keep_day_keys or ()
            if day_key is not None and str(day_key).strip()
        }
        try:
            now_local = dt_util.as_local(dt_util.now())
        except Exception:
            now_local = datetime.now(tz=_tz.utc)
        day_retention = max(1, int(getattr(self, "_session_history_day_retention", 1)))
        for day_offset in range(day_retention):
            retained.add((now_local - timedelta(days=day_offset)).strftime("%Y-%m-%d"))
        return retained

    def _prune_session_history_cache_shim(
        self,
        *,
        active_serials: Iterable[str] | None,
        keep_day_keys: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(getattr(self, "_session_history_cache_shim", None), dict):
            self._session_history_cache_shim = {}
            return

        active_set = (
            None if active_serials is None else self._normalize_serials(active_serials)
        )
        retained_days = self._retained_session_history_days(keep_day_keys)
        self._session_history_cache_shim = {
            (sn, day_key): entry
            for (sn, day_key), entry in self._session_history_cache_shim.items()
            if day_key in retained_days and (active_set is None or sn in active_set)
        }

    def _set_session_history_cache_shim_entry(
        self,
        serial: str,
        day_key: str,
        sessions: list[dict],
    ) -> None:
        self._session_history_cache_shim[(serial, day_key)] = (
            time.monotonic(),
            sessions,
        )
        keep_serials = self._normalize_serials(self.iter_serials())
        keep_serials.add(serial)
        self._prune_session_history_cache_shim(
            active_serials=keep_serials,
            keep_day_keys={day_key},
        )

    def _prune_serial_runtime_state(self, active_serials: Iterable[str]) -> set[str]:
        keep_serials = self._normalize_serials(active_serials)
        keep_serials.update(
            self._normalize_serials(getattr(self, "_configured_serials", ()))
        )

        if isinstance(getattr(self, "serials", None), set):
            self.serials.intersection_update(keep_serials)
        else:
            self.serials = set(keep_serials)

        serial_order = getattr(self, "_serial_order", None)
        if isinstance(serial_order, list):
            self._serial_order = [sn for sn in serial_order if sn in keep_serials]
        else:
            self._serial_order = [sn for sn in keep_serials]

        for attr_name in (
            "last_set_amps",
            "_operating_v",
            "_charge_mode_cache",
            "_green_battery_cache",
            "_auth_settings_cache",
            "_evse_feature_flags_by_serial",
            "_last_charging",
            "_last_actual_charging",
            "_pending_charging",
            "_desired_charging",
            "_auto_resume_attempts",
            "_session_end_fix",
            "_streaming_targets",
        ):
            cache = getattr(self, attr_name, None)
            if not isinstance(cache, dict):
                continue
            for key in list(cache):
                key_sn = str(key).strip()
                if key_sn not in keep_serials:
                    cache.pop(key, None)

        return keep_serials

    def _prune_runtime_caches(
        self,
        *,
        active_serials: Iterable[str],
        keep_day_keys: Iterable[str] | None = None,
    ) -> None:
        keep_serials = self._prune_serial_runtime_state(active_serials)
        self._prune_session_history_cache_shim(
            active_serials=keep_serials,
            keep_day_keys=keep_day_keys,
        )
        session_manager = getattr(self, "session_history", None)
        if session_manager is not None and hasattr(session_manager, "prune"):
            session_manager.prune(
                active_serials=keep_serials,
                keep_day_keys=keep_day_keys,
            )

    def cleanup_runtime_state(self) -> None:
        """Release runtime caches/listeners to make unload deterministic."""
        if self._warmup_task is not None:
            self._warmup_task.cancel()
            self._warmup_task = None
        if self._discovery_snapshot_save_cancel is not None:
            self._discovery_snapshot_save_cancel()
            self._discovery_snapshot_save_cancel = None
        session_manager = getattr(self, "session_history", None)
        if session_manager is not None and hasattr(session_manager, "clear"):
            session_manager.clear()
        self._session_history_cache_shim.clear()
        self._prune_runtime_caches(active_serials=(), keep_day_keys=())
        self._topology_listeners.clear()

    @callback
    def async_add_topology_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Listen for topology-only changes."""
        self._topology_listeners.append(update_callback)

        @callback
        def _remove_listener() -> None:
            if update_callback in self._topology_listeners:
                self._topology_listeners.remove(update_callback)

        return _remove_listener

    def topology_snapshot(self) -> CoordinatorTopologySnapshot:
        """Return the latest cached topology snapshot."""
        return self._topology_snapshot_cache

    def gateway_inventory_summary(self) -> dict[str, object]:
        source = self._gateway_inventory_summary_marker()
        summary = getattr(self, "_gateway_inventory_summary_cache", {}) or {}
        if not summary or source != self._gateway_inventory_summary_source:
            summary = self._build_gateway_inventory_summary()
            self._gateway_inventory_summary_cache = summary
            self._gateway_inventory_summary_source = source
        return dict(summary)

    def microinverter_inventory_summary(self) -> dict[str, object]:
        source = self._microinverter_inventory_summary_marker()
        summary = getattr(self, "_microinverter_inventory_summary_cache", {}) or {}
        if not summary or source != self._microinverter_inventory_summary_source:
            summary = self._build_microinverter_inventory_summary()
            self._microinverter_inventory_summary_cache = summary
            self._microinverter_inventory_summary_source = source
        return dict(summary)

    def heatpump_inventory_summary(self) -> dict[str, object]:
        source = self._heatpump_inventory_summary_marker()
        summary = getattr(self, "_heatpump_inventory_summary_cache", {}) or {}
        if not summary or source != self._heatpump_inventory_summary_source:
            summary = self._build_heatpump_inventory_summary()
            self._heatpump_inventory_summary_cache = summary
            self._heatpump_inventory_summary_source = source
        return dict(summary)

    def heatpump_type_summary(self, device_type: str) -> dict[str, object]:
        try:
            normalized = str(device_type).strip().upper()
        except Exception:  # noqa: BLE001
            normalized = ""
        source = self._heatpump_inventory_summary_marker()
        summaries = getattr(self, "_heatpump_type_summaries_cache", {}) or {}
        if source != self._heatpump_type_summaries_source or (
            normalized and normalized not in summaries
        ):
            summaries = self._build_heatpump_type_summaries()
            self._heatpump_type_summaries_cache = summaries
            self._heatpump_type_summaries_source = source
        summary = summaries.get(normalized, {})
        return dict(summary) if isinstance(summary, dict) else {}

    def gateway_iq_energy_router_summary_records(self) -> list[dict[str, object]]:
        source = self._gateway_iq_energy_router_records_marker()
        records = getattr(self, "_gateway_iq_energy_router_records_cache", [])
        if not records or source != self._gateway_iq_energy_router_records_source:
            records = self._gateway_iq_energy_router_summary_records(
                self.gateway_iq_energy_router_records()
            )
            self._gateway_iq_energy_router_records_cache = records
            self._gateway_iq_energy_router_records_source = source
            self._gateway_iq_energy_router_records_by_key_cache = {
                record["key"]: record
                for record in records
                if isinstance(record, dict) and isinstance(record.get("key"), str)
            }
        return [dict(record) for record in records if isinstance(record, dict)]

    @staticmethod
    def _router_record_key(record: object) -> str | None:
        if not isinstance(record, dict):
            return None
        key = record.get("key")
        if key is None:
            return None
        try:
            key_text = str(key).strip()
        except Exception:  # noqa: BLE001
            return None
        return key_text or None

    def gateway_iq_energy_router_record(
        self, router_key: object
    ) -> dict[str, object] | None:
        try:
            key = str(router_key).strip()
        except Exception:  # noqa: BLE001
            return None
        if not key:
            return None
        self.gateway_iq_energy_router_summary_records()
        record = getattr(
            self, "_gateway_iq_energy_router_records_by_key_cache", {}
        ).get(key)
        return dict(record) if isinstance(record, dict) else None

    def _current_topology_snapshot(self) -> CoordinatorTopologySnapshot:
        router_records = self.gateway_iq_energy_router_summary_records()
        router_keys = tuple(
            key
            for key in (self._router_record_key(record) for record in router_records)
            if key
        )
        return CoordinatorTopologySnapshot(
            charger_serials=tuple(self.iter_serials()),
            battery_serials=tuple(self.iter_battery_serials()),
            inverter_serials=tuple(self.iter_inverter_serials()),
            active_type_keys=tuple(self.iter_type_keys()),
            gateway_iq_router_keys=router_keys,
            inventory_ready=bool(
                getattr(self, "_devices_inventory_ready", False)
                or getattr(self, "_hems_inventory_ready", False)
            ),
        )

    @callback
    def _notify_topology_listeners(self) -> None:
        for listener in list(self._topology_listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Topology listener failed for site %s",
                    redact_site_id(self.site_id),
                    exc_info=True,
                )

    @callback
    def _refresh_cached_topology(self) -> bool:
        if self._topology_refresh_suppressed > 0:
            self._topology_refresh_pending = True
            return False
        try:
            self._rebuild_inventory_summary_caches()
            snapshot = self._current_topology_snapshot()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Skipping topology cache rebuild for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            return False
        if snapshot == self._topology_snapshot_cache:
            return False
        self._topology_snapshot_cache = snapshot
        self._debug_log_summary_if_changed(
            "topology",
            "Discovery topology summary updated",
            self._debug_topology_summary(snapshot),
        )
        self._notify_topology_listeners()
        return True

    @callback
    def _begin_topology_refresh_batch(self) -> None:
        self._topology_refresh_suppressed += 1

    @callback
    def _end_topology_refresh_batch(self) -> bool:
        if self._topology_refresh_suppressed > 0:
            self._topology_refresh_suppressed -= 1
        if self._topology_refresh_suppressed > 0 or not self._topology_refresh_pending:
            return False
        self._topology_refresh_pending = False
        return self._refresh_cached_topology()

    @staticmethod
    def _snapshot_compatible_value(value: object) -> object:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            out: dict[str, object] = {}
            for key, item in value.items():
                try:
                    key_text = str(key)
                except Exception:  # noqa: BLE001
                    continue
                out[key_text] = EnphaseCoordinator._snapshot_compatible_value(item)
            return out
        if isinstance(value, (list, tuple, set)):
            return [
                EnphaseCoordinator._snapshot_compatible_value(item) for item in value
            ]
        try:
            return str(value)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _snapshot_bool(value: object) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1", "yes", "y", "enabled", "on"):
                return True
            if normalized in ("false", "0", "no", "n", "disabled", "off"):
                return False
        return None

    def site_energy_channel_known(self, flow_key: str) -> bool:
        try:
            key = str(flow_key).strip()
        except Exception:  # noqa: BLE001
            return False
        if not key:
            return False
        if key in self._live_site_energy_channels():
            return True
        if self._site_energy_discovery_ready:
            return False
        return key in self._restored_site_energy_channels

    def _live_site_energy_channels(self) -> set[str]:
        channels: set[str] = set()
        energy = getattr(self, "energy", None)
        if energy is None:
            return channels
        flows = getattr(energy, "site_energy", None)
        if isinstance(flows, dict):
            for key in flows:
                try:
                    key_text = str(key).strip()
                except Exception:  # noqa: BLE001
                    continue
                if key_text:
                    channels.add(key_text)
        meta = getattr(energy, "site_energy_meta", None)
        if isinstance(meta, dict):
            bucket_lengths = meta.get("bucket_lengths")
            if isinstance(bucket_lengths, dict):
                for key, value in bucket_lengths.items():
                    try:
                        key_text = str(key).strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if not key_text:
                        continue
                    try:
                        if int(value) <= 0:
                            continue
                    except Exception:
                        if not value:
                            continue
                    mapped = {
                        "heatpump": "heat_pump",
                        "water_heater": "water_heater",
                        "evse": "evse_charging",
                        "solar_production": "solar_production",
                        "consumption": "consumption",
                        "grid_import": "grid_import",
                        "grid_export": "grid_export",
                        "battery_charge": "battery_charge",
                        "battery_discharge": "battery_discharge",
                    }.get(key_text, key_text)
                    channels.add(mapped)
        return channels

    def _gateway_router_discovery_ready(self) -> bool:
        return bool(getattr(self, "_hems_inventory_ready", False))

    def _live_gateway_iq_energy_router_records(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for member in self._hems_group_members("gateway"):
            if not isinstance(member, dict):
                continue
            raw_type = member.get("device-type")
            if raw_type is None:
                raw_type = member.get("device_type")
            if raw_type is None:
                continue
            try:
                type_text = str(raw_type).strip().upper()
            except Exception:  # noqa: BLE001
                continue
            if type_text != "IQ_ENERGY_ROUTER":
                continue
            records.append(dict(member))
        return records

    def gateway_iq_energy_router_records(self) -> list[dict[str, object]]:
        records = self._live_gateway_iq_energy_router_records()
        if records:
            return records
        if self._gateway_router_discovery_ready():
            return []
        return [
            dict(item)
            for item in self._restored_gateway_iq_energy_router_records
            if isinstance(item, dict)
        ]

    def _capture_discovery_snapshot(self) -> dict[str, object]:
        site_energy_channels = self._live_site_energy_channels()
        if not site_energy_channels and not self._site_energy_discovery_ready:
            site_energy_channels = set(self._restored_site_energy_channels)
        router_records = self._live_gateway_iq_energy_router_records()
        if not router_records and not self._gateway_router_discovery_ready():
            router_records = [
                dict(item)
                for item in self._restored_gateway_iq_energy_router_records
                if isinstance(item, dict)
            ]
        snapshot = {
            "serial_order": self.iter_serials(),
            "type_device_order": list(getattr(self, "_type_device_order", []) or []),
            "type_device_buckets": self._snapshot_compatible_value(
                dict(getattr(self, "_type_device_buckets", {}) or {})
            ),
            "battery_storage_order": list(
                getattr(self, "_battery_storage_order", []) or []
            ),
            "battery_storage_data": self._snapshot_compatible_value(
                dict(getattr(self, "_battery_storage_data", {}) or {})
            ),
            "inverter_order": list(getattr(self, "_inverter_order", []) or []),
            "inverter_data": self._snapshot_compatible_value(
                dict(getattr(self, "_inverter_data", {}) or {})
            ),
            "battery_has_encharge": getattr(self, "_battery_has_encharge", None),
            "battery_has_enpower": getattr(self, "_battery_has_enpower", None),
            "site_energy_channels": sorted(site_energy_channels),
            "gateway_iq_energy_router_records": self._snapshot_compatible_value(
                router_records
            ),
        }
        return snapshot

    def _apply_discovery_snapshot(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            return

        serial_order = snapshot.get("serial_order")
        if isinstance(serial_order, list):
            for serial in serial_order:
                if serial is None:
                    continue
                try:
                    text = str(serial).strip()
                except Exception:  # noqa: BLE001
                    continue
                if text:
                    self._ensure_serial_tracked(text)

        grouped = snapshot.get("type_device_buckets")
        ordered = snapshot.get("type_device_order")
        if isinstance(grouped, dict):
            normalized_grouped: dict[str, dict[str, object]] = {}
            for raw_key, raw_bucket in grouped.items():
                type_key = normalize_type_key(raw_key)
                if not type_key or not isinstance(raw_bucket, dict):
                    continue
                bucket = dict(raw_bucket)
                members = bucket.get("devices")
                if isinstance(members, list):
                    bucket["devices"] = [
                        dict(member) for member in members if isinstance(member, dict)
                    ]
                else:
                    bucket["devices"] = []
                try:
                    count = int(bucket.get("count", len(bucket["devices"])) or 0)
                except Exception:  # noqa: BLE001
                    count = len(bucket["devices"])
                bucket["count"] = max(count, len(bucket["devices"]))
                normalized_grouped[type_key] = bucket
            ordered_keys = (
                [normalize_type_key(key) for key in ordered if normalize_type_key(key)]
                if isinstance(ordered, list)
                else list(normalized_grouped.keys())
            )
            if normalized_grouped:
                self._set_type_device_buckets(
                    normalized_grouped, ordered_keys, authoritative=False
                )

        battery_order = snapshot.get("battery_storage_order")
        battery_data = snapshot.get("battery_storage_data")
        if isinstance(battery_order, list) and isinstance(battery_data, dict):
            self._battery_storage_order = [
                str(item).strip() for item in battery_order if str(item).strip()
            ]
            self._battery_storage_data = {
                str(key).strip(): dict(value)
                for key, value in battery_data.items()
                if str(key).strip() and isinstance(value, dict)
            }

        inverter_order = snapshot.get("inverter_order")
        inverter_data = snapshot.get("inverter_data")
        if isinstance(inverter_order, list) and isinstance(inverter_data, dict):
            self._inverter_order = [
                str(item).strip() for item in inverter_order if str(item).strip()
            ]
            self._inverter_data = {
                str(key).strip(): dict(value)
                for key, value in inverter_data.items()
                if str(key).strip() and isinstance(value, dict)
            }

        has_encharge = self._snapshot_bool(snapshot.get("battery_has_encharge"))
        if has_encharge is not None:
            self._battery_has_encharge = has_encharge
        has_enpower = self._snapshot_bool(snapshot.get("battery_has_enpower"))
        if has_enpower is not None:
            self._battery_has_enpower = has_enpower

        restored_channels = snapshot.get("site_energy_channels")
        if isinstance(restored_channels, list):
            self._restored_site_energy_channels = {
                str(item).strip() for item in restored_channels if str(item).strip()
            }

        restored_router_records = snapshot.get("gateway_iq_energy_router_records")
        if isinstance(restored_router_records, list):
            self._restored_gateway_iq_energy_router_records = [
                dict(item) for item in restored_router_records if isinstance(item, dict)
            ]
        self._refresh_cached_topology()

    async def async_restore_discovery_state(self) -> None:
        if self._discovery_snapshot_loaded:
            return
        self._discovery_snapshot_loaded = True
        self._devices_inventory_ready = False
        self._hems_inventory_ready = False
        self._site_energy_discovery_ready = False
        try:
            snapshot = await self._discovery_snapshot_store.async_load()
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to load discovery snapshot for site %s",
                redact_site_id(self.site_id),
                exc_info=True,
            )
            return
        self._apply_discovery_snapshot(snapshot)

    async def _async_save_discovery_snapshot(self) -> None:
        self._discovery_snapshot_pending = False
        snapshot = self._capture_discovery_snapshot()
        try:
            await self._discovery_snapshot_store.async_save(snapshot)
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to save discovery snapshot for site %s",
                redact_site_id(self.site_id),
                exc_info=True,
            )

    def _schedule_discovery_snapshot_save(self) -> None:
        self._discovery_snapshot_pending = True
        if self._discovery_snapshot_save_cancel is not None:
            return

        @callback
        def _run(_now: datetime) -> None:
            self._discovery_snapshot_save_cancel = None
            if not self._discovery_snapshot_pending:
                return
            self.hass.async_create_task(self._async_save_discovery_snapshot())

        self._discovery_snapshot_save_cancel = async_call_later(
            self.hass, DISCOVERY_SNAPSHOT_SAVE_DELAY_S, _run
        )

    def startup_migrations_ready(self) -> bool:
        return bool(getattr(self, "_devices_inventory_ready", False))

    def _sync_site_energy_discovery_state(self) -> None:
        energy = getattr(self, "energy", None)
        if energy is None:
            return
        if getattr(energy, "_site_energy_cache_ts", None) is not None:
            self._site_energy_discovery_ready = True

    def _publish_internal_state_update(self) -> None:
        current = self.data if isinstance(self.data, dict) else {}
        self.async_set_updated_data(dict(current))

    async def _async_run_refresh_call(
        self,
        timing_key: str,
        log_label: str,
        callback_factory: Callable[[], object],
    ) -> tuple[str, float | None]:
        started = time.monotonic()
        try:
            result = callback_factory()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Skipping %s refresh for site %s: %s",
                log_label,
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
        return timing_key, round(time.monotonic() - started, 3)

    async def _async_run_refresh_calls(
        self,
        phase_timings: dict[str, float],
        *,
        calls: tuple[tuple[str, str, Callable[[], object]], ...],
        stage_key: str | None = None,
        defer_topology: bool = False,
    ) -> None:
        if defer_topology:
            self._begin_topology_refresh_batch()

        group_started = time.monotonic()
        try:
            results = await asyncio.gather(
                *(
                    self._async_run_refresh_call(
                        timing_key, log_label, callback_factory
                    )
                    for timing_key, log_label, callback_factory in calls
                )
            )
        finally:
            if defer_topology:
                self._end_topology_refresh_batch()

        for timing_key, duration in results:
            if duration is not None:
                phase_timings[timing_key] = duration
        if stage_key is not None:
            phase_timings[f"{stage_key}_s"] = round(time.monotonic() - group_started, 3)

    async def _async_run_ordered_refresh_calls(
        self,
        phase_timings: dict[str, float],
        *,
        calls: tuple[tuple[str, str, Callable[[], object]], ...],
        stage_key: str | None = None,
        defer_topology: bool = False,
    ) -> None:
        if defer_topology:
            self._begin_topology_refresh_batch()

        group_started = time.monotonic()
        try:
            for timing_key, log_label, callback_factory in calls:
                key, duration = await self._async_run_refresh_call(
                    timing_key,
                    log_label,
                    callback_factory,
                )
                if duration is not None:
                    phase_timings[key] = duration
        finally:
            if defer_topology:
                self._end_topology_refresh_batch()

        if stage_key is not None:
            phase_timings[f"{stage_key}_s"] = round(time.monotonic() - group_started, 3)

    async def _async_run_staged_refresh_calls(
        self,
        phase_timings: dict[str, float],
        *,
        parallel_calls: tuple[tuple[str, str, Callable[[], object]], ...] = (),
        ordered_calls: tuple[tuple[str, str, Callable[[], object]], ...] = (),
        stage_key: str | None = None,
        defer_topology: bool = False,
    ) -> None:
        if not parallel_calls and not ordered_calls:
            if stage_key is not None:
                phase_timings[f"{stage_key}_s"] = 0.0
            return

        if defer_topology:
            self._begin_topology_refresh_batch()

        group_started = time.monotonic()
        try:
            if parallel_calls:
                await self._async_run_refresh_calls(
                    phase_timings,
                    calls=parallel_calls,
                )
            if ordered_calls:
                await self._async_run_ordered_refresh_calls(
                    phase_timings,
                    calls=ordered_calls,
                )
        finally:
            if defer_topology:
                self._end_topology_refresh_batch()

        if stage_key is not None:
            phase_timings[f"{stage_key}_s"] = round(time.monotonic() - group_started, 3)

    async def async_ensure_system_dashboard_diagnostics(self) -> None:
        if (
            self._system_dashboard_type_summaries
            or self._system_dashboard_hierarchy_summary
        ):
            return
        await self._async_refresh_system_dashboard(force=True)

    async def _async_refresh_hems_support_preflight(
        self, *, force: bool = False
    ) -> None:
        if getattr(self.client, "hems_site_supported", None) is not None:
            return

        now = time.monotonic()
        if not force and self._hems_support_preflight_cache_until is not None:
            if now < self._hems_support_preflight_cache_until:
                return

        fetcher = getattr(self.client, "system_dashboard_summary", None)
        if not callable(fetcher):
            self._hems_support_preflight_cache_until = (
                now + HEMS_SUPPORT_PREFLIGHT_CACHE_TTL
            )
            return

        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "HEMS support preflight failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            self._hems_support_preflight_cache_until = (
                now + HEMS_SUPPORT_PREFLIGHT_CACHE_TTL
            )
            return

        if isinstance(payload, dict):
            is_hems = self._coerce_optional_bool(payload.get("is_hems"))
            if is_hems is not None:
                self.client._hems_site_supported = is_hems  # noqa: SLF001

        self._hems_support_preflight_cache_until = (
            now + HEMS_SUPPORT_PREFLIGHT_CACHE_TTL
        )

    async def _async_startup_warmup_runner(self) -> None:
        warmup_timings: dict[str, float] = {}
        self._warmup_in_progress = True
        self._warmup_last_error = None
        warmup_data = (
            {sn: dict(payload) for sn, payload in self.data.items()}
            if isinstance(self.data, dict)
            else {}
        )
        try:
            await self._async_run_staged_refresh_calls(
                warmup_timings,
                stage_key="discovery",
                defer_topology=True,
                parallel_calls=(
                    (
                        "battery_site_settings_s",
                        "battery site settings",
                        lambda: self._async_refresh_battery_site_settings(),
                    ),
                ),
                ordered_calls=(
                    (
                        "battery_status_s",
                        "battery status",
                        lambda: self._async_refresh_battery_status(),
                    ),
                    (
                        "devices_inventory_s",
                        "device inventory",
                        lambda: self._async_refresh_devices_inventory(),
                    ),
                    (
                        "hems_devices_s",
                        "HEMS inventory",
                        lambda: self._async_refresh_hems_devices(),
                    ),
                    (
                        "inverters_s",
                        "inverters",
                        lambda: self._async_refresh_inverters(),
                    ),
                ),
            )
            await self._async_run_refresh_calls(
                warmup_timings,
                stage_key="state",
                calls=(
                    (
                        "battery_backup_history_s",
                        "battery backup history",
                        lambda: self._async_refresh_battery_backup_history(),
                    ),
                    (
                        "battery_settings_s",
                        "battery settings",
                        lambda: self._async_refresh_battery_settings(),
                    ),
                    (
                        "battery_schedules_s",
                        "battery schedules",
                        lambda: self._async_refresh_battery_schedules(),
                    ),
                    (
                        "storm_guard_s",
                        "storm guard",
                        lambda: self._async_refresh_storm_guard_profile(),
                    ),
                    (
                        "storm_alert_s",
                        "storm alert",
                        lambda: self._async_refresh_storm_alert(),
                    ),
                    (
                        "grid_control_check_s",
                        "grid control",
                        lambda: self._async_refresh_grid_control_check(),
                    ),
                    (
                        "dry_contact_settings_s",
                        "dry contact settings",
                        lambda: self._async_refresh_dry_contact_settings(),
                    ),
                    (
                        "evse_feature_flags_s",
                        "EVSE feature flags",
                        lambda: self._async_refresh_evse_feature_flags(),
                    ),
                    (
                        "current_power_s",
                        "current power consumption",
                        lambda: self._async_refresh_current_power_consumption(),
                    ),
                ),
            )
            heatpump_started = time.monotonic()
            try:
                await self._async_refresh_heatpump_power()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Skipping heat pump power refresh for site %s during warmup: %s",
                    redact_site_id(self.site_id),
                    redact_text(err, site_ids=(self.site_id,)),
                )
            warmup_timings["heatpump_power_s"] = round(
                time.monotonic() - heatpump_started, 3
            )
            await self._async_run_refresh_calls(
                warmup_timings,
                stage_key="energy",
                calls=(
                    (
                        "site_energy_s",
                        "site energy",
                        lambda: self._async_refresh_site_energy_for_warmup(),
                    ),
                    (
                        "evse_timeseries_s",
                        "EVSE timeseries",
                        lambda: self._async_refresh_evse_timeseries_for_warmup(
                            working_data=warmup_data
                        ),
                    ),
                    (
                        "sessions_s",
                        "session state",
                        lambda: self._async_refresh_session_state_for_warmup(
                            working_data=warmup_data
                        ),
                    ),
                    (
                        "secondary_evse_state_s",
                        "secondary EVSE state",
                        lambda: self._async_refresh_secondary_evse_state_for_warmup(
                            working_data=warmup_data
                        ),
                    ),
                ),
            )
            if warmup_data:
                self.async_set_updated_data(warmup_data)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self._warmup_last_error = (
                redact_text(err, site_ids=(self.site_id,)) or err.__class__.__name__
            )
            _LOGGER.debug(
                "Startup warmup failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
                exc_info=True,
            )
        finally:
            self._warmup_in_progress = False
            self._warmup_phase_timings = warmup_timings
            self._schedule_discovery_snapshot_save()

    async def async_start_startup_warmup(self) -> None:
        if self._warmup_task is not None and not self._warmup_task.done():
            return
        try:
            self._warmup_task = self.hass.async_create_task(
                self._async_startup_warmup_runner(),
                name=f"{DOMAIN}_warmup_{self.site_id}",
            )
        except TypeError:
            self._warmup_task = self.hass.async_create_task(
                self._async_startup_warmup_runner()
            )

    async def _async_refresh_site_energy_for_warmup(self) -> None:
        await self.energy._async_refresh_site_energy()
        self._sync_site_energy_discovery_state()
        self._sync_site_energy_issue()

    async def _async_refresh_evse_timeseries_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]] | None = None,
    ) -> None:
        try:
            day_local = dt_util.as_local(dt_util.now())
        except Exception:
            day_local = datetime.now(tz=_tz.utc)
        await self.evse_timeseries.async_refresh(day_local=day_local)
        target = working_data
        if target is None and isinstance(self.data, dict) and self.data:
            target = {sn: dict(payload) for sn, payload in self.data.items()}
        if target:
            self.evse_timeseries.merge_charger_payloads(target, day_local=day_local)
            if working_data is None:
                self.async_set_updated_data(target)

    async def _async_refresh_session_state_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]] | None = None,
    ) -> None:
        target = working_data if working_data is not None else self.data
        if not isinstance(target, dict) or not target:
            return
        try:
            day_ref = dt_util.as_local(dt_util.now())
        except Exception:
            day_ref = datetime.now(tz=_tz.utc)
        updates = await self._async_enrich_sessions(
            target.keys(),
            day_ref,
            in_background=False,
        )
        if not updates:
            return
        merged = (
            target
            if working_data is not None
            else {sn: dict(payload) for sn, payload in target.items()}
        )
        for sn, sessions in updates.items():
            payload = merged.get(sn)
            if payload is None:
                continue
            payload["energy_today_sessions"] = sessions
            payload["energy_today_sessions_kwh"] = self._sum_session_energy(sessions)
        if working_data is None:
            self.async_set_updated_data(merged)
        self._sync_session_history_issue()

    async def _async_refresh_secondary_evse_state_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]] | None = None,
    ) -> None:
        target = working_data if working_data is not None else self.data
        if not isinstance(target, dict) or not target:
            return
        serials = [sn for sn in self.iter_serials() if sn]
        if not serials:
            return
        charge_modes = await self._async_resolve_charge_modes(serials)
        green_settings = await self._async_resolve_green_battery_settings(serials)
        auth_settings = await self._async_resolve_auth_settings(serials)
        merged = (
            target
            if working_data is not None
            else {sn: dict(payload) for sn, payload in target.items()}
        )
        for sn in serials:
            payload = merged.get(sn)
            if payload is None:
                continue
            if charge_modes.get(sn):
                payload["charge_mode_pref"] = charge_modes[sn]
            if green_settings.get(sn) is not None:
                enabled, supported = green_settings[sn]
                payload["green_battery_supported"] = supported
                if supported:
                    payload["green_battery_enabled"] = enabled
            if auth_settings.get(sn) is not None:
                (
                    app_enabled,
                    rfid_enabled,
                    app_supported,
                    rfid_supported,
                ) = auth_settings[sn]
                payload["app_auth_supported"] = app_supported
                payload["rfid_auth_supported"] = rfid_supported
                payload["app_auth_enabled"] = app_enabled
                payload["rfid_auth_enabled"] = rfid_enabled
                if app_supported or rfid_supported:
                    values = [
                        value
                        for value in (app_enabled, rfid_enabled)
                        if value is not None
                    ]
                    payload["auth_required"] = any(values) if values else None
        if working_data is None:
            self.async_set_updated_data(merged)

    def _parse_devices_inventory_payload(
        self, payload: object
    ) -> tuple[bool, dict[str, dict[str, object]], list[str]]:
        if isinstance(payload, list):
            result = payload
        elif isinstance(payload, dict):
            result = payload.get("result")
        else:
            return False, {}, []
        if not isinstance(result, list):
            return False, {}, []

        grouped: dict[str, dict[str, object]] = {}
        seen_per_type: dict[str, set[str]] = {}
        ordered_keys: list[str] = []

        def _clean_text(value: object) -> str | None:
            if value is None:
                return None
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                return None
            return text or None

        def _dry_contact_member_dedupe_key(
            raw_type: object,
            member: dict[str, object],
            member_index: int,
        ) -> str:
            serial = _clean_text(
                member.get("serial_number")
                if member.get("serial_number") is not None
                else (
                    member.get("serial")
                    if member.get("serial") is not None
                    else (
                        member.get("serialNumber")
                        if member.get("serialNumber") is not None
                        else member.get("device_sn")
                    )
                )
            )
            if serial is not None:
                return f"sn:{serial}"

            source_type = _clean_text(raw_type)
            identity_parts: list[str] = []
            for key in (
                "device_uid",
                "device-uid",
                "uid",
                "contact_id",
                "contactId",
                "id",
                "channel_type",
                "channelType",
                "meter_type",
            ):
                value = _clean_text(member.get(key))
                if value is None:
                    continue
                identity_parts.append(f"{key}:{value}")
            if identity_parts:
                if source_type is not None:
                    identity_parts.insert(0, f"source:{source_type}")
                return "|".join(identity_parts)

            fingerprint_parts: list[str] = []
            for key in sorted(member):
                value = member.get(key)
                if value is None or not isinstance(value, (str, int, float, bool)):
                    continue
                fingerprint_parts.append(f"{key}:{value}")
            if fingerprint_parts:
                fingerprint = "|".join(fingerprint_parts)
                if source_type is not None:
                    return f"source:{source_type}|{fingerprint}|idx:{member_index}"
                return f"{fingerprint}|idx:{member_index}"

            if source_type is not None:
                return f"source:{source_type}|idx:{member_index}"
            return f"idx:{member_index}:dry_contact"

        for bucket in result:
            if not isinstance(bucket, dict):
                continue
            raw_type = bucket.get("type")
            if raw_type is None:
                raw_type = bucket.get("deviceType")
            if raw_type is None:
                raw_type = bucket.get("device_type")
            type_key = normalize_type_key(raw_type)
            devices = bucket.get("devices")
            if not isinstance(devices, list):
                devices = bucket.get("items")
            if not isinstance(devices, list):
                devices = bucket.get("members")
            if not type_key or not isinstance(devices, list):
                continue
            if type_key not in grouped:
                grouped[type_key] = {
                    "type_key": type_key,
                    "type_label": type_display_label(type_key),
                    "count": 0,
                    "devices": [],
                }
                seen_per_type[type_key] = set()
                ordered_keys.append(type_key)
            members: list[dict[str, object]] = grouped[type_key]["devices"]  # type: ignore[assignment]
            seen_keys = seen_per_type[type_key]
            for member_index, member in enumerate(devices):
                if not isinstance(member, dict):
                    continue
                if member_is_retired(member):
                    continue
                sanitized = sanitize_member(member)
                if not sanitized:
                    continue
                if type_key == "dry_contact":
                    dedupe_key = _dry_contact_member_dedupe_key(
                        raw_type, sanitized, member_index
                    )
                else:
                    serial = sanitized.get("serial_number")
                    name = sanitized.get("name")
                    if isinstance(serial, str) and serial.strip():
                        dedupe_key = f"sn:{serial.strip()}"
                    elif isinstance(name, str) and name.strip():
                        dedupe_key = f"name:{name.strip()}"
                    else:
                        dedupe_key = f"idx:{len(members)}:{type_key}"
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                members.append(sanitized)

        valid = True
        for type_key, bucket in grouped.items():
            members = bucket.get("devices")
            count = len(members) if isinstance(members, list) else 0
            bucket["count"] = count
            bucket["type_label"] = bucket.get("type_label") or type_display_label(
                type_key
            )
            if type_key == "encharge" and isinstance(members, list):
                name_counts: dict[str, int] = {}
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    raw_name = member.get("name")
                    if raw_name is None:
                        continue
                    try:
                        name_text = str(raw_name).strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if not name_text:
                        continue
                    name_counts[name_text] = name_counts.get(name_text, 0) + 1
                if name_counts:
                    bucket["model_counts"] = dict(name_counts)
                    summary = self._format_inverter_model_summary(name_counts)
                    if isinstance(summary, str) and summary.strip():
                        bucket["model_summary"] = summary

        return valid, dict(grouped), list(dict.fromkeys(ordered_keys))

    def _set_type_device_buckets(
        self,
        grouped: dict[str, dict[str, object]],
        ordered_keys: list[str],
        *,
        authoritative: bool = True,
    ) -> None:
        normalized_order = [
            key
            for key in ordered_keys
            if key in grouped
            and isinstance(grouped[key].get("devices"), list)
            and int(grouped[key].get("count", 0)) > 0
        ]
        self._type_device_buckets = {
            key: value
            for key, value in grouped.items()
            if int(value.get("count", 0)) > 0
        }
        self._type_device_order = normalized_order
        if authoritative:
            self._devices_inventory_ready = True

    @staticmethod
    def _devices_inventory_buckets(payload: object) -> list[dict[str, object]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        result = payload.get("result")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        wrapped = payload.get("value")
        if isinstance(wrapped, dict):
            wrapped_result = wrapped.get("result")
            if isinstance(wrapped_result, list):
                return [item for item in wrapped_result if isinstance(item, dict)]
        return []

    @staticmethod
    def _hems_devices_groups(payload: object) -> list[dict[str, object]]:
        """Return grouped HEMS members from the dedicated HEMS inventory payload."""

        if not isinstance(payload, dict):
            return []
        result = payload.get("result")
        if isinstance(result, dict):
            devices = result.get("devices")
            if isinstance(devices, list):
                return [grouped for grouped in devices if isinstance(grouped, dict)]
            if isinstance(devices, dict):
                return [devices]
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        hems_devices = (
            data.get("hems-devices")
            if data.get("hems-devices") is not None
            else data.get("hems_devices")
        )
        if not isinstance(hems_devices, dict):
            return []
        return [hems_devices]

    @classmethod
    def _legacy_hems_devices_groups(cls, payload: object) -> list[dict[str, object]]:
        """Return grouped HEMS members from legacy devices.json payloads."""

        groups: list[dict[str, object]] = []
        for bucket in cls._devices_inventory_buckets(payload):
            bucket_type = cls._hems_bucket_type(
                bucket.get("type")
                if bucket.get("type") is not None
                else (
                    bucket.get("deviceType")
                    if bucket.get("deviceType") is not None
                    else bucket.get("device_type")
                )
            )
            if bucket_type != "hemsdevices":
                continue
            grouped_devices = bucket.get("devices")
            if not isinstance(grouped_devices, list):
                continue
            groups.extend(
                grouped for grouped in grouped_devices if isinstance(grouped, dict)
            )
        return groups

    def _hems_grouped_devices(self) -> list[dict[str, object]]:
        """Return HEMS grouped devices using dedicated inventory first."""

        groups = self._hems_devices_groups(getattr(self, "_hems_devices_payload", None))
        if groups:
            return groups
        return self._legacy_hems_devices_groups(
            getattr(self, "_devices_inventory_payload", None)
        )

    @staticmethod
    def _normalize_hems_member(member: dict[str, object]) -> dict[str, object]:
        """Normalize dedicated and legacy HEMS member key variants."""

        normalized: dict[str, object] = dict(member)
        alias_pairs = (
            ("device-type", "device_type"),
            ("deviceType", "device_type"),
            ("device-uid", "device_uid"),
            ("deviceUid", "device_uid"),
            ("last-report", "last_report"),
            ("lastReport", "last_report"),
            ("last-reported", "last_reported"),
            ("lastReported", "last_reported"),
            ("last-reported-at", "last_reported_at"),
            ("lastReportedAt", "last_reported_at"),
            ("firmware-version", "firmware_version"),
            ("firmwareVersion", "firmware_version"),
            ("software-version", "software_version"),
            ("softwareVersion", "software_version"),
            ("hardware-version", "hardware_version"),
            ("hardwareVersion", "hardware_version"),
            ("hardware-sku", "hardware_sku"),
            ("hardwareSku", "hardware_sku"),
            ("part-number", "part_number"),
            ("partNumber", "part_number"),
            ("hems-device-id", "hems_device_id"),
            ("hems-device-facet-id", "hems_device_facet_id"),
            ("pairing-status", "pairing_status"),
            ("device-state", "device_state"),
            ("iqer-uid", "iqer_uid"),
            ("ip-address", "ip_address"),
            ("created-at", "created_at"),
            ("fvt-time", "fvt_time"),
        )
        for source, dest in alias_pairs:
            if dest not in normalized and source in normalized:
                normalized[dest] = normalized[source]
        if "status_text" not in normalized and "statusText" in normalized:
            normalized["status_text"] = normalized.get("statusText")
        if "serial_number" not in normalized and "serial" in normalized:
            normalized["serial_number"] = normalized.get("serial")
        if "uid" not in normalized and "device_uid" in normalized:
            normalized["uid"] = normalized.get("device_uid")
        return normalized

    @staticmethod
    def _normalize_heatpump_member(member: dict[str, object]) -> dict[str, object]:
        return EnphaseCoordinator._normalize_hems_member(member)

    def _extract_hems_group_members(
        self,
        groups: list[dict[str, object]],
        requested_keys: set[str],
    ) -> tuple[bool, list[dict[str, object]]]:
        """Return whether any requested group was present and its normalized members."""

        members: list[dict[str, object]] = []
        seen_keys: set[str] = set()
        found_group = False
        for grouped in groups:
            for group_key in requested_keys:
                if group_key in grouped:
                    found_group = True
                raw_members = grouped.get(group_key)
                if not isinstance(raw_members, list):
                    continue
                for raw_member in raw_members:
                    if not isinstance(raw_member, dict):
                        continue
                    if member_is_retired(raw_member):
                        continue
                    normalized = self._normalize_hems_member(raw_member)
                    if not normalized:
                        continue
                    dedupe = (
                        self._type_member_text(
                            normalized, "device_uid", "uid", "serial_number", "name"
                        )
                        or f"idx:{len(members)}"
                    )
                    if dedupe in seen_keys:
                        continue
                    seen_keys.add(dedupe)
                    members.append(normalized)
        return found_group, members

    def _hems_group_members(self, *group_keys: str) -> list[dict[str, object]]:
        """Return normalized HEMS members, preferring dedicated data per group."""

        requested_keys = {key for key in group_keys if key}
        dedicated_found, dedicated_members = self._extract_hems_group_members(
            self._hems_devices_groups(getattr(self, "_hems_devices_payload", None)),
            requested_keys,
        )
        if dedicated_found:
            return dedicated_members
        _legacy_found, legacy_members = self._extract_hems_group_members(
            self._legacy_hems_devices_groups(
                getattr(self, "_devices_inventory_payload", None)
            ),
            requested_keys,
        )
        return legacy_members

    @staticmethod
    def _hems_bucket_type(raw_type: object) -> str | None:
        normalized = normalize_type_key(raw_type)
        if normalized:
            return normalized.replace("_", "")
        try:
            text = str(raw_type).strip().lower()
        except Exception:
            return None
        if not text:
            return None
        return "".join(ch for ch in text if ch.isalnum())

    @staticmethod
    def _heatpump_member_device_type(member: dict[str, object] | None) -> str | None:
        if not isinstance(member, dict):
            return None
        raw = (
            member.get("device_type")
            if member.get("device_type") is not None
            else member.get("device-type")
        )
        if raw is None:
            return None
        try:
            text = str(raw).strip()
        except Exception:
            return None
        return text.upper() if text else None

    @staticmethod
    def _heatpump_worst_status_text(status_counts: dict[str, int]) -> str | None:
        if int(status_counts.get("error", 0) or 0) > 0:
            return "Error"
        if int(status_counts.get("warning", 0) or 0) > 0:
            return "Warning"
        if int(status_counts.get("not_reporting", 0) or 0) > 0:
            return "Not Reporting"
        if int(status_counts.get("unknown", 0) or 0) > 0:
            return "Unknown"
        if int(status_counts.get("normal", 0) or 0) > 0:
            return "Normal"
        return None

    def _merge_heatpump_type_bucket(self) -> None:
        """Merge HEMS heat-pump members into the canonical heatpump bucket."""

        ready_before = bool(getattr(self, "_devices_inventory_ready", False))
        buckets = dict(getattr(self, "_type_device_buckets", {}) or {})
        ordered = list(getattr(self, "_type_device_order", []) or [])
        key = "heatpump"

        members_out = self._hems_group_members("heat-pump", "heat_pump", "heatpump")
        if members_out:
            status_counts: dict[str, int] = {
                "total": len(members_out),
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            }
            device_type_counts: dict[str, int] = {}
            model_counts: dict[str, int] = {}
            firmware_counts: dict[str, int] = {}
            latest_reported: datetime | None = None
            latest_reported_device: dict[str, object] | None = None
            overall_status_text: str | None = None

            for member in members_out:
                device_type = self._heatpump_member_device_type(member) or "UNKNOWN"
                device_type_counts[device_type] = (
                    device_type_counts.get(device_type, 0) + 1
                )
                status_text = self._heatpump_status_text(member)
                normalized_status = self._normalize_inverter_status(status_text)
                status_counts[normalized_status] = (
                    int(status_counts.get(normalized_status, 0)) + 1
                )
                if device_type == "HEAT_PUMP" and status_text:
                    overall_status_text = status_text

                model = self._type_member_text(
                    member,
                    "model",
                    "model_id",
                    "sku_id",
                    "part_number",
                    "hardware_sku",
                )
                if model:
                    model_counts[model] = model_counts.get(model, 0) + 1
                firmware = self._type_member_text(
                    member,
                    "firmware_version",
                    "sw_version",
                    "software_version",
                    "application_version",
                )
                if firmware:
                    firmware_counts[firmware] = firmware_counts.get(firmware, 0) + 1

                parsed_last = self._parse_inverter_last_report(
                    self._type_member_text(
                        member,
                        "last_report",
                        "last_reported",
                        "last_reported_at",
                    )
                )
                if parsed_last is not None and (
                    latest_reported is None or parsed_last > latest_reported
                ):
                    latest_reported = parsed_last
                    latest_reported_device = {
                        "device_type": device_type,
                        "device_uid": self._type_member_text(
                            member, "device_uid", "uid", "serial_number"
                        ),
                        "name": self._type_member_text(member, "name"),
                        "status": status_text,
                    }

            if not overall_status_text:
                overall_status_text = self._heatpump_worst_status_text(status_counts)

            buckets[key] = {
                "type_key": key,
                "type_label": "Heat Pump",
                "count": len(members_out),
                "devices": members_out,
                "status_counts": status_counts,
                "status_summary": self._format_inverter_status_summary(status_counts),
                "device_type_counts": device_type_counts,
                "model_counts": model_counts,
                "model_summary": self._format_inverter_model_summary(model_counts),
                "firmware_counts": firmware_counts,
                "firmware_summary": self._format_inverter_model_summary(
                    firmware_counts
                ),
                "overall_status_text": overall_status_text,
                "latest_reported_utc": (
                    latest_reported.isoformat() if latest_reported is not None else None
                ),
                "latest_reported_device": latest_reported_device,
            }
            if key not in ordered:
                if "iqevse" in ordered:
                    ordered.insert(ordered.index("iqevse") + 1, key)
                else:
                    ordered.append(key)
        else:
            buckets.pop(key, None)
            ordered = [item for item in ordered if item != key]

        self._set_type_device_buckets(buckets, ordered)
        if not ready_before:
            self._devices_inventory_ready = False
        self._refresh_cached_topology()

    @staticmethod
    def _summary_text(value: object) -> str | None:
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @classmethod
    def _summary_identity(cls, value: object) -> str | None:
        text = cls._summary_text(value)
        if not text:
            return None
        normalized = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        return normalized or None

    def _summary_type_bucket_source(self, type_key: object) -> dict[str, object] | None:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        buckets = getattr(self, "_type_device_buckets", None)
        if not isinstance(buckets, dict):
            return None
        bucket = buckets.get(normalized)
        return bucket if isinstance(bucket, dict) else None

    def _gateway_inventory_summary_marker(self) -> tuple[object, ...]:
        dashboard_payloads = getattr(
            self, "_system_dashboard_devices_details_raw", None
        )
        dashboard_envoy = (
            dashboard_payloads.get("envoy")
            if isinstance(dashboard_payloads, dict)
            else None
        )
        return (
            id(self._summary_type_bucket_source("envoy")),
            id(dashboard_envoy),
        )

    def _microinverter_inventory_summary_marker(self) -> tuple[object, ...]:
        return (id(self._summary_type_bucket_source("microinverter")),)

    def _heatpump_inventory_summary_marker(self) -> tuple[object, ...]:
        return (
            id(self._summary_type_bucket_source("heatpump")),
            id(getattr(self, "_hems_devices_payload", None)),
            bool(getattr(self, "_hems_devices_using_stale", False)),
            getattr(self, "_hems_devices_last_success_utc", None),
            getattr(self, "_hems_devices_last_success_mono", None),
        )

    def _gateway_iq_energy_router_records_marker(self) -> tuple[object, ...]:
        return (
            id(getattr(self, "_hems_devices_payload", None)),
            id(getattr(self, "_devices_inventory_payload", None)),
            id(getattr(self, "_restored_gateway_iq_energy_router_records", None)),
        )

    @staticmethod
    def _heatpump_status_text(member: dict[str, object] | None) -> str | None:
        if not isinstance(member, dict):
            return None
        status_text = (
            member.get("statusText")
            if member.get("statusText") is not None
            else member.get("status_text")
        )
        text = EnphaseCoordinator._summary_text(status_text)
        if text:
            return text
        raw = EnphaseCoordinator._summary_text(member.get("status"))
        if not raw:
            return None
        return raw.replace("_", " ").replace("-", " ").title()

    @classmethod
    def _gateway_iq_energy_router_summary_records(
        cls, members: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        key_counts: dict[str, int] = {}
        for member in members:
            index = len(records) + 1
            base_key = None
            for key in ("device-uid", "device_uid", "uid"):
                base_key = cls._summary_identity(member.get(key))
                if base_key:
                    break
            if base_key is None:
                name_identity = cls._summary_identity(member.get("name"))
                base_key = (
                    f"name_{name_identity}" if name_identity else f"index_{index}"
                )
            key_counts[base_key] = key_counts.get(base_key, 0) + 1
            key = base_key
            if key_counts[base_key] > 1:
                key = f"{base_key}_{key_counts[base_key]}"
            records.append(
                {
                    "key": key,
                    "index": index,
                    "name": cls._summary_text(member.get("name"))
                    or f"IQ Energy Router_{index}",
                    "member": dict(member),
                }
            )
        return records

    def _build_gateway_inventory_summary(self) -> dict[str, object]:
        bucket = self.type_bucket("envoy") or {}
        members_raw = bucket.get("devices")
        members = (
            [item for item in members_raw if isinstance(item, dict)]
            if isinstance(members_raw, list)
            else []
        )
        dashboard_envoy = self.system_dashboard_envoy_detail()
        if not members and isinstance(dashboard_envoy, dict):
            members = [dict(dashboard_envoy)]
        try:
            total_devices = int(bucket.get("count", len(members)) or 0)
        except Exception:
            total_devices = len(members)
        total_devices = max(total_devices, len(members))
        status_counts: dict[str, int] = {
            "normal": 0,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
            "unknown": 0,
        }
        model_counts: dict[str, int] = {}
        firmware_counts: dict[str, int] = {}
        property_keys: set[str] = set()
        connected_devices = 0
        disconnected_devices = 0
        latest_reported: datetime | None = None
        latest_reported_device: dict[str, object] | None = None
        without_last_report_count = 0

        for member in members:
            property_keys.update(str(key) for key in member.keys())
            status_source = None
            for key in ("statusText", "status_text", "status"):
                if member.get(key) is not None:
                    status_source = member.get(key)
                    break
            status = self._normalize_inverter_status(status_source)
            status_counts[status] = status_counts.get(status, 0) + 1

            connected = member.get("connected")
            if isinstance(connected, str):
                normalized_connected = connected.strip().lower()
                if normalized_connected in {"true", "1", "yes", "y"}:
                    connected = True
                elif normalized_connected in {"false", "0", "no", "n"}:
                    connected = False
                else:
                    connected = None
            elif isinstance(connected, (int, float)):
                connected = connected != 0
            elif not isinstance(connected, bool):
                connected = None
            if connected is None:
                if status == "normal":
                    connected = True
                elif status == "not_reporting":
                    connected = False
            if connected is True:
                connected_devices += 1
            elif connected is False:
                disconnected_devices += 1

            model_name = self._type_member_text(
                member, "model", "model_name", "part_number", "device_type"
            )
            if model_name:
                model_counts[model_name] = model_counts.get(model_name, 0) + 1
            firmware_version = self._type_member_text(
                member, "firmware_version", "sw_version", "software_version"
            )
            if firmware_version:
                firmware_counts[firmware_version] = (
                    firmware_counts.get(firmware_version, 0) + 1
                )

            parsed_last_report = None
            for key in (
                "last_report",
                "last_reported",
                "last_reported_at",
                "last-report",
            ):
                parsed_last_report = self._parse_inverter_last_report(member.get(key))
                if parsed_last_report is not None:
                    break
            if parsed_last_report is None:
                without_last_report_count += 1
                continue
            if latest_reported is None or parsed_last_report > latest_reported:
                latest_reported = parsed_last_report
                latest_reported_device = {
                    "name": self._summary_text(member.get("name")),
                    "serial_number": self._summary_text(member.get("serial_number")),
                    "status": self._summary_text(status_source),
                }

        unknown_connection_devices = max(
            0, total_devices - connected_devices - disconnected_devices
        )
        status_summary = (
            f"Normal {status_counts.get('normal', 0)} | "
            f"Warning {status_counts.get('warning', 0)} | "
            f"Error {status_counts.get('error', 0)} | "
            f"Not Reporting {status_counts.get('not_reporting', 0)} | "
            f"Unknown {status_counts.get('unknown', 0)}"
            if total_devices > 0
            else None
        )
        if latest_reported is None and isinstance(dashboard_envoy, dict):
            fallback_last = None
            for key in ("last_report", "last_interval_end_date"):
                fallback_last = self._parse_inverter_last_report(
                    dashboard_envoy.get(key)
                )
                if fallback_last is not None:
                    break
            if fallback_last is not None:
                latest_reported = fallback_last
                latest_reported_device = {
                    "name": self._summary_text(dashboard_envoy.get("name"))
                    or "IQ Gateway",
                    "serial_number": self._summary_text(
                        dashboard_envoy.get("serial_number")
                    ),
                    "status": self._summary_text(
                        dashboard_envoy.get("statusText")
                        if dashboard_envoy.get("statusText") is not None
                        else dashboard_envoy.get("status")
                    ),
                }
        return {
            "total_devices": total_devices,
            "connected_devices": connected_devices,
            "disconnected_devices": disconnected_devices,
            "unknown_connection_devices": unknown_connection_devices,
            "without_last_report_count": without_last_report_count,
            "status_counts": status_counts,
            "status_summary": status_summary,
            "model_counts": model_counts,
            "model_summary": self._format_inverter_model_summary(model_counts),
            "firmware_counts": firmware_counts,
            "firmware_summary": self._format_inverter_model_summary(firmware_counts),
            "latest_reported": latest_reported,
            "latest_reported_utc": (
                latest_reported.isoformat() if latest_reported is not None else None
            ),
            "latest_reported_device": latest_reported_device,
            "property_keys": sorted(property_keys),
        }

    def _build_microinverter_inventory_summary(self) -> dict[str, object]:
        bucket = self.type_bucket("microinverter") or {}
        members = bucket.get("devices")
        safe_members = (
            [dict(item) for item in members if isinstance(item, dict)]
            if isinstance(members, list)
            else []
        )
        status_counts_raw = bucket.get("status_counts")
        status_counts: dict[str, int] = {}
        has_status_counts = isinstance(status_counts_raw, dict)
        if isinstance(status_counts_raw, dict):
            for key in (
                "total",
                "normal",
                "warning",
                "error",
                "not_reporting",
                "unknown",
            ):
                try:
                    status_counts[key] = int(status_counts_raw.get(key, 0) or 0)
                except Exception:
                    status_counts[key] = 0
        try:
            total_inverters = int(bucket.get("count", len(safe_members)) or 0)
        except Exception:
            total_inverters = len(safe_members)
        if status_counts.get("total", 0) > 0:
            total_inverters = max(total_inverters, int(status_counts.get("total", 0)))
        not_reporting = max(0, int(status_counts.get("not_reporting", 0)))
        unknown = max(0, int(status_counts.get("unknown", 0)))
        if not has_status_counts:
            unknown = total_inverters
        elif (
            total_inverters > 0
            and int(status_counts.get("total", 0) or 0) <= 0
            and max(
                0,
                int(status_counts.get("normal", 0) or 0)
                + int(status_counts.get("warning", 0) or 0)
                + int(status_counts.get("error", 0) or 0)
                + not_reporting
                + unknown,
            )
            == 0
        ):
            unknown = total_inverters
        known_status_total = not_reporting + unknown
        if known_status_total > total_inverters:
            unknown = max(0, unknown - (known_status_total - total_inverters))
        reporting = max(0, total_inverters - not_reporting - unknown)
        latest_reported = self._parse_inverter_last_report(
            bucket.get("latest_reported_utc")
            if bucket.get("latest_reported_utc") is not None
            else bucket.get("latest_reported")
        )
        latest_reported_device = (
            dict(bucket.get("latest_reported_device"))
            if isinstance(bucket.get("latest_reported_device"), dict)
            else None
        )
        if latest_reported is None:
            for member in safe_members:
                parsed_last = self._parse_inverter_last_report(
                    member.get("last_report")
                )
                if parsed_last is None:
                    continue
                if latest_reported is None or parsed_last > latest_reported:
                    latest_reported = parsed_last
                    latest_reported_device = {
                        "serial_number": self._summary_text(
                            member.get("serial_number")
                        ),
                        "name": self._summary_text(member.get("name")),
                        "status": self._summary_text(
                            member.get("statusText")
                            if member.get("statusText") is not None
                            else member.get("status")
                        ),
                    }
        snapshot: dict[str, object] = {
            "total_inverters": total_inverters,
            "reporting_inverters": reporting,
            "not_reporting_inverters": not_reporting,
            "unknown_inverters": unknown,
            "status_counts": status_counts,
            "status_summary": bucket.get("status_summary"),
            "model_summary": bucket.get("model_summary"),
            "firmware_summary": bucket.get("firmware_summary"),
            "array_summary": bucket.get("array_summary"),
            "panel_info": (
                dict(bucket.get("panel_info"))
                if isinstance(bucket.get("panel_info"), dict)
                else None
            ),
            "status_type_counts": (
                dict(bucket.get("status_type_counts"))
                if isinstance(bucket.get("status_type_counts"), dict)
                else None
            ),
            "latest_reported": latest_reported,
            "latest_reported_utc": (
                latest_reported.isoformat() if latest_reported is not None else None
            ),
            "latest_reported_device": latest_reported_device,
            "production_start_date": bucket.get("production_start_date"),
            "production_end_date": bucket.get("production_end_date"),
        }
        connectivity_state = bucket.get("connectivity_state")
        if not isinstance(connectivity_state, str) or not connectivity_state.strip():
            connectivity_state = "degraded"
            if total_inverters <= 0:
                connectivity_state = None
            elif reporting >= total_inverters:
                connectivity_state = "online"
            elif reporting == 0 and not_reporting > 0:
                connectivity_state = "offline"
            elif reporting > 0 and reporting < total_inverters:
                connectivity_state = "degraded"
            elif unknown >= total_inverters:
                connectivity_state = "unknown"
        snapshot["connectivity_state"] = connectivity_state
        return snapshot

    def _build_heatpump_inventory_summary(self) -> dict[str, object]:
        bucket = self.type_bucket("heatpump") or {}
        members = bucket.get("devices")
        safe_members = (
            [dict(item) for item in members if isinstance(item, dict)]
            if isinstance(members, list)
            else []
        )
        status_counts_raw = bucket.get("status_counts")
        status_counts: dict[str, int] | None = None
        if isinstance(status_counts_raw, dict):
            parsed_counts = {
                "total": 0,
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            }
            try:
                for key in parsed_counts:
                    parsed_counts[key] = int(status_counts_raw.get(key, 0) or 0)
                status_counts = parsed_counts
            except Exception:
                status_counts = None
        if status_counts is None:
            status_counts = {
                "total": len(safe_members),
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            }
            for member in safe_members:
                status_key = self._normalize_inverter_status(
                    self._heatpump_status_text(member)
                )
                status_counts[status_key] = int(status_counts.get(status_key, 0)) + 1
        try:
            total_devices = int(bucket.get("count", len(safe_members)) or 0)
        except Exception:
            total_devices = len(safe_members)
        total_devices = max(total_devices, len(safe_members))
        status_counts["total"] = max(
            int(status_counts.get("total", 0) or 0), total_devices
        )
        latest_reported = self._parse_inverter_last_report(
            bucket.get("latest_reported_utc")
            if bucket.get("latest_reported_utc") is not None
            else bucket.get("latest_reported")
        )
        latest_reported_device = (
            dict(bucket.get("latest_reported_device"))
            if isinstance(bucket.get("latest_reported_device"), dict)
            else None
        )
        without_last_report_count = 0
        if latest_reported is None:
            for member in safe_members:
                member_last = None
                for key in (
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                    "last-report",
                ):
                    member_last = self._parse_inverter_last_report(member.get(key))
                    if member_last is not None:
                        break
                if member_last is None:
                    without_last_report_count += 1
                    continue
                if latest_reported is None or member_last > latest_reported:
                    latest_reported = member_last
                    latest_reported_device = {
                        "device_type": self._heatpump_member_device_type(member),
                        "name": self._summary_text(member.get("name")),
                        "device_uid": self._type_member_text(
                            member, "device_uid", "device-uid", "uid"
                        ),
                        "status": self._heatpump_status_text(member),
                    }
        overall_status_text = self._summary_text(bucket.get("overall_status_text"))
        if not overall_status_text:
            for member in safe_members:
                if self._heatpump_member_device_type(member) != "HEAT_PUMP":
                    continue
                overall_status_text = self._heatpump_status_text(member)
                if overall_status_text:
                    break
        if not overall_status_text:
            overall_status_text = self._heatpump_worst_status_text(status_counts)
        device_type_counts: dict[str, int] = {}
        if isinstance(bucket.get("device_type_counts"), dict):
            for key, value in bucket.get("device_type_counts", {}).items():
                if key is None:
                    continue
                try:
                    count = int(value)
                except Exception:
                    continue
                if count > 0:
                    device_type_counts[str(key)] = count
        else:
            for member in safe_members:
                device_type = self._heatpump_member_device_type(member) or "UNKNOWN"
                device_type_counts[device_type] = (
                    device_type_counts.get(device_type, 0) + 1
                )
        status_summary = bucket.get("status_summary")
        if not isinstance(status_summary, str) or not status_summary.strip():
            status_summary = self._format_inverter_status_summary(status_counts)
        hems_last_success_utc = getattr(self, "_hems_devices_last_success_utc", None)
        if not isinstance(hems_last_success_utc, datetime):
            hems_last_success_utc = None
        hems_last_success_mono = getattr(self, "_hems_devices_last_success_mono", None)
        hems_last_success_age_s: float | None = None
        if isinstance(hems_last_success_mono, (int, float)):
            age = time.monotonic() - float(hems_last_success_mono)
            if age >= 0:
                hems_last_success_age_s = round(age, 1)
        return {
            "total_devices": total_devices,
            "members": safe_members,
            "status_counts": status_counts,
            "status_summary": status_summary,
            "device_type_counts": device_type_counts,
            "model_summary": bucket.get("model_summary"),
            "firmware_summary": bucket.get("firmware_summary"),
            "latest_reported": latest_reported,
            "latest_reported_utc": (
                latest_reported.isoformat() if latest_reported is not None else None
            ),
            "latest_reported_device": latest_reported_device,
            "without_last_report_count": without_last_report_count,
            "overall_status_text": overall_status_text,
            "hems_data_stale": bool(getattr(self, "_hems_devices_using_stale", False)),
            "hems_last_success_utc": (
                hems_last_success_utc.isoformat()
                if hems_last_success_utc is not None
                else None
            ),
            "hems_last_success_age_s": hems_last_success_age_s,
        }

    def _build_heatpump_type_summaries(self) -> dict[str, dict[str, object]]:
        snapshot = self._build_heatpump_inventory_summary()
        members = [
            member for member in snapshot.get("members", []) if isinstance(member, dict)
        ]
        summaries: dict[str, dict[str, object]] = {}
        for device_type in sorted(
            {
                self._heatpump_member_device_type(member)
                for member in members
                if self._heatpump_member_device_type(member)
            }
        ):
            type_members = [
                member
                for member in members
                if self._heatpump_member_device_type(member) == device_type
            ]
            counts = {
                "total": len(type_members),
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            }
            latest_reported: datetime | None = None
            latest_device: dict[str, object] | None = None
            status_texts: list[str] = []
            for member in type_members:
                status_text = self._heatpump_status_text(member)
                if status_text:
                    status_texts.append(status_text)
                status_key = self._normalize_inverter_status(status_text)
                counts[status_key] = int(counts.get(status_key, 0)) + 1
                parsed_last = None
                for key in (
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                    "last-report",
                ):
                    parsed_last = self._parse_inverter_last_report(member.get(key))
                    if parsed_last is not None:
                        break
                if parsed_last is not None and (
                    latest_reported is None or parsed_last > latest_reported
                ):
                    latest_reported = parsed_last
                    latest_device = {
                        "name": self._summary_text(member.get("name")),
                        "device_uid": self._type_member_text(
                            member, "device_uid", "device-uid", "uid"
                        ),
                        "status": status_text,
                    }
            unique_statuses = list(dict.fromkeys(status_texts))
            if len(unique_statuses) == 1:
                native_status = unique_statuses[0]
            else:
                native_status = self._heatpump_worst_status_text(counts)
            summaries[device_type] = {
                "device_type": device_type,
                "members": type_members,
                "member_count": len(type_members),
                "status_counts": counts,
                "status_summary": self._format_inverter_status_summary(counts),
                "native_status": native_status,
                "latest_reported": latest_reported,
                "latest_reported_utc": (
                    latest_reported.isoformat() if latest_reported is not None else None
                ),
                "latest_reported_device": latest_device,
                "hems_data_stale": snapshot.get("hems_data_stale"),
                "hems_last_success_utc": snapshot.get("hems_last_success_utc"),
                "hems_last_success_age_s": snapshot.get("hems_last_success_age_s"),
            }
        return summaries

    @callback
    def _rebuild_inventory_summary_caches(self) -> None:
        gateway_source = self._gateway_inventory_summary_marker()
        micro_source = self._microinverter_inventory_summary_marker()
        heatpump_source = self._heatpump_inventory_summary_marker()
        router_source = self._gateway_iq_energy_router_records_marker()
        gateway_summary = self._build_gateway_inventory_summary()
        micro_summary = self._build_microinverter_inventory_summary()
        heatpump_summary = self._build_heatpump_inventory_summary()
        heatpump_type_summaries = self._build_heatpump_type_summaries()
        router_records = self._gateway_iq_energy_router_summary_records(
            self.gateway_iq_energy_router_records()
        )
        self._gateway_inventory_summary_cache = gateway_summary
        self._gateway_inventory_summary_source = gateway_source
        self._microinverter_inventory_summary_cache = micro_summary
        self._microinverter_inventory_summary_source = micro_source
        self._heatpump_inventory_summary_cache = heatpump_summary
        self._heatpump_inventory_summary_source = heatpump_source
        self._heatpump_type_summaries_cache = heatpump_type_summaries
        self._heatpump_type_summaries_source = heatpump_source
        self._gateway_iq_energy_router_records_cache = router_records
        self._gateway_iq_energy_router_records_source = router_source
        self._gateway_iq_energy_router_records_by_key_cache = {
            record["key"]: record
            for record in router_records
            if isinstance(record, dict) and isinstance(record.get("key"), str)
        }

    async def _async_refresh_devices_inventory(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._devices_inventory_cache_until:
            if now < self._devices_inventory_cache_until:
                return
        fetcher = getattr(self.client, "devices_inventory", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Device inventory fetch failed: %s",
                redact_text(err, site_ids=(self.site_id,)),
            )
            return
        valid, grouped, ordered = self._parse_devices_inventory_payload(payload)
        if not valid:
            _LOGGER.debug(
                "Device inventory payload shape was invalid: %s",
                self._debug_render_summary(self._debug_payload_shape(payload)),
            )
            return
        summary = self._debug_devices_inventory_summary(grouped, ordered)
        has_active_members = False
        for bucket in grouped.values():
            try:
                if int(bucket.get("count", 0)) > 0:
                    has_active_members = True
                    break
            except Exception:
                continue
        if not has_active_members:
            _LOGGER.debug(
                "Device inventory refresh returned no active members; keeping previous type mapping: %s",
                self._debug_render_summary(summary),
            )
            self._devices_inventory_cache_until = now + DEVICES_INVENTORY_CACHE_TTL
            return
        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._devices_inventory_payload = redacted_payload
        else:
            self._devices_inventory_payload = {"value": redacted_payload}
        self._set_type_device_buckets(grouped, ordered)
        self._merge_heatpump_type_bucket()
        self._devices_inventory_cache_until = now + DEVICES_INVENTORY_CACHE_TTL
        self._debug_log_summary_if_changed(
            "devices_inventory",
            "Device inventory discovery summary",
            summary,
        )

    async def _async_refresh_hems_devices(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._hems_devices_cache_until:
            if now < self._hems_devices_cache_until:
                return
        await self._async_refresh_hems_support_preflight(force=force)
        if getattr(self.client, "hems_site_supported", None) is False:
            self._hems_devices_payload = None
            self._hems_devices_using_stale = False
            self._hems_inventory_ready = True
            self._merge_heatpump_type_bucket()
            self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
            self._debug_log_summary_if_changed(
                "hems_inventory",
                "HEMS discovery summary",
                self._debug_hems_inventory_summary(),
            )
            return
        fetcher = getattr(self.client, "hems_devices", None)
        if not callable(fetcher):
            return
        previous_payload = getattr(self, "_hems_devices_payload", None)

        def _previous_payload_reusable() -> bool:
            if previous_payload is None:
                return False
            last_success = getattr(self, "_hems_devices_last_success_mono", None)
            if not isinstance(last_success, (int, float)):
                return False
            age = now - float(last_success)
            if age < 0:
                return True
            return age < HEMS_DEVICES_STALE_AFTER_S

        try:
            payload = await fetcher(refresh_data=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "HEMS device inventory fetch failed: %s",
                redact_text(err, site_ids=(self.site_id,)),
            )
            if getattr(self.client, "hems_site_supported", None) is False:
                self._hems_devices_payload = None
                self._hems_devices_using_stale = False
                self._merge_heatpump_type_bucket()
                self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
            elif _previous_payload_reusable():
                self._hems_devices_payload = previous_payload
                self._hems_devices_using_stale = True
                self._merge_heatpump_type_bucket()
                self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
            elif previous_payload is not None:
                self._hems_devices_payload = None
                self._hems_devices_using_stale = False
                self._merge_heatpump_type_bucket()
                self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
            self._debug_log_summary_if_changed(
                "hems_inventory",
                "HEMS discovery summary",
                self._debug_hems_inventory_summary(),
            )
            return
        if payload is None:
            if getattr(self.client, "hems_site_supported", None) is False:
                self._hems_devices_payload = None
                self._hems_devices_using_stale = False
                self._hems_inventory_ready = True
                self._merge_heatpump_type_bucket()
                self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
                self._debug_log_summary_if_changed(
                    "hems_inventory",
                    "HEMS discovery summary",
                    self._debug_hems_inventory_summary(),
                )
                return
            if _previous_payload_reusable():
                self._hems_devices_payload = previous_payload
                self._hems_devices_using_stale = True
                self._merge_heatpump_type_bucket()
                self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
                self._debug_log_summary_if_changed(
                    "hems_inventory",
                    "HEMS discovery summary",
                    self._debug_hems_inventory_summary(),
                )
                return
            self._hems_devices_payload = None
            self._hems_devices_using_stale = False
            self._merge_heatpump_type_bucket()
            self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
            self._debug_log_summary_if_changed(
                "hems_inventory",
                "HEMS discovery summary",
                self._debug_hems_inventory_summary(),
            )
            return
        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._hems_devices_payload = redacted_payload
        else:
            self._hems_devices_payload = {"value": redacted_payload}
        self._hems_devices_last_success_mono = now
        self._hems_devices_last_success_utc = dt_util.utcnow()
        self._hems_devices_using_stale = False
        self._hems_inventory_ready = True
        self._merge_heatpump_type_bucket()
        self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
        self._debug_log_summary_if_changed(
            "hems_inventory",
            "HEMS discovery summary",
            self._debug_hems_inventory_summary(),
        )

    @staticmethod
    def _copy_diagnostics_value(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: EnphaseCoordinator._copy_diagnostics_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [EnphaseCoordinator._copy_diagnostics_value(item) for item in value]
        return value

    @staticmethod
    def _debug_truncate_identifier(value: object) -> str | None:
        """Return a short, non-reversible debug identifier."""
        return truncate_identifier(value)

    @staticmethod
    def _debug_sorted_keys(value: object) -> list[str]:
        """Return sorted string keys from a mapping."""

        if not isinstance(value, dict):
            return []
        keys: set[str] = set()
        for key in value:
            try:
                key_text = str(key).strip()
            except Exception:  # noqa: BLE001
                continue
            if key_text:
                keys.add(key_text)
        return sorted(keys)

    @classmethod
    def _debug_field_keys(cls, members: object) -> list[str]:
        """Return sorted field keys present across a list of mappings."""

        if not isinstance(members, list):
            return []
        keys: set[str] = set()
        for member in members:
            if not isinstance(member, dict):
                continue
            keys.update(cls._debug_sorted_keys(member))
        return sorted(keys)

    @classmethod
    def _debug_payload_shape(cls, payload: object) -> dict[str, object]:
        """Return a payload-shape summary suitable for debug logging."""

        if isinstance(payload, dict):
            shape: dict[str, object] = {
                "kind": "dict",
                "keys": cls._debug_sorted_keys(payload),
            }
            for key in ("result", "data", "devices", "items", "members"):
                nested = payload.get(key)
                if isinstance(nested, list):
                    shape[f"{key}_length"] = len(nested)
                    field_keys = cls._debug_field_keys(nested)
                    if field_keys:
                        shape[f"{key}_field_keys"] = field_keys
                elif isinstance(nested, dict):
                    shape[f"{key}_keys"] = cls._debug_sorted_keys(nested)
            return shape
        if isinstance(payload, list):
            return {
                "kind": "list",
                "length": len(payload),
                "field_keys": cls._debug_field_keys(payload),
            }
        if payload is None:
            return {"kind": "none"}
        return {"kind": type(payload).__name__}

    @staticmethod
    def _debug_render_summary(summary: object) -> str:
        """Serialize a debug summary into stable compact JSON."""

        try:
            return json.dumps(summary, sort_keys=True, ensure_ascii=True)
        except Exception:  # noqa: BLE001
            return str(summary)

    def _debug_log_summary_if_changed(
        self,
        cache_key: str,
        label: str,
        summary: object,
    ) -> None:
        """Log a debug summary only when it changes."""

        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        cached = self._debug_summary_log_cache.get(cache_key)
        if cached == summary:
            return
        self._debug_summary_log_cache[cache_key] = self._copy_diagnostics_value(summary)
        _LOGGER.debug("%s: %s", label, self._debug_render_summary(summary))

    def _debug_devices_inventory_summary(
        self,
        grouped: dict[str, dict[str, object]],
        ordered_keys: list[str],
    ) -> dict[str, object]:
        """Return a sanitized summary of device inventory discovery."""

        types: dict[str, dict[str, object]] = {}
        for type_key in ordered_keys:
            bucket = grouped.get(type_key)
            if not isinstance(bucket, dict):
                continue
            members = bucket.get("devices")
            count = self._coerce_int(bucket.get("count"), default=0)
            summary: dict[str, object] = {
                "count": max(
                    count,
                    len(members) if isinstance(members, list) else 0,
                ),
                "field_keys": self._debug_field_keys(members),
            }
            status_counts = bucket.get("status_counts")
            if isinstance(status_counts, dict) and status_counts:
                summary["status_counts"] = {
                    str(key): self._coerce_int(value, default=0)
                    for key, value in status_counts.items()
                }
            device_type_counts = bucket.get("device_type_counts")
            if isinstance(device_type_counts, dict) and device_type_counts:
                summary["device_type_counts"] = {
                    str(key): self._coerce_int(value, default=0)
                    for key, value in device_type_counts.items()
                }
            types[type_key] = summary
        return {
            "ordered_type_keys": list(ordered_keys),
            "type_count": len(types),
            "types": types,
        }

    def _debug_hems_inventory_summary(self) -> dict[str, object]:
        """Return a sanitized summary of HEMS discovery data."""

        grouped_devices = self._hems_grouped_devices()
        group_keys: set[str] = set()
        for grouped in grouped_devices:
            if not isinstance(grouped, dict):
                continue
            for key, value in grouped.items():
                if isinstance(value, list):
                    try:
                        key_text = str(key).strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if key_text:
                        group_keys.add(key_text)

        gateway_members = self._hems_group_members("gateway")
        heatpump_members = self._hems_group_members(
            "heat-pump", "heat_pump", "heatpump"
        )
        heatpump_summary = self._build_heatpump_inventory_summary()
        router_records = self.gateway_iq_energy_router_summary_records()
        device_type_counts = heatpump_summary.get("device_type_counts")
        status_counts = heatpump_summary.get("status_counts")

        return {
            "site_supported": getattr(self.client, "hems_site_supported", None),
            "using_stale": bool(getattr(self, "_hems_devices_using_stale", False)),
            "group_keys": sorted(group_keys),
            "gateway_member_count": len(gateway_members),
            "gateway_field_keys": self._debug_field_keys(gateway_members),
            "heatpump_member_count": self._coerce_int(
                heatpump_summary.get("total_devices"), default=len(heatpump_members)
            ),
            "heatpump_field_keys": self._debug_field_keys(heatpump_members),
            "heatpump_device_type_counts": (
                dict(device_type_counts) if isinstance(device_type_counts, dict) else {}
            ),
            "heatpump_status_counts": (
                dict(status_counts) if isinstance(status_counts, dict) else {}
            ),
            "router_count": len(router_records),
        }

    def _debug_system_dashboard_summary(
        self,
        tree_payload: dict[str, object] | None,
        details_payloads: dict[str, dict[str, dict[str, object]]],
        type_summaries: dict[str, dict[str, object]],
        hierarchy_summary: dict[str, object],
    ) -> dict[str, object]:
        """Return a sanitized summary of system dashboard discovery."""

        types: dict[str, dict[str, object]] = {}
        for canonical_type, payloads_by_source in details_payloads.items():
            records = self._system_dashboard_detail_records(
                payloads_by_source, *sorted(payloads_by_source)
            )
            raw_type_summary = type_summaries.get(canonical_type, {})
            type_summary = (
                raw_type_summary if isinstance(raw_type_summary, dict) else {}
            )
            summary: dict[str, object] = {
                "sources": sorted(payloads_by_source),
                "record_count": len(records),
                "field_keys": self._debug_field_keys(records),
            }
            hierarchy = type_summary.get("hierarchy")
            if isinstance(hierarchy, dict):
                summary["hierarchy_count"] = self._coerce_int(
                    hierarchy.get("count"), default=0
                )
            counts = type_summary.get("counts_by_type")
            if isinstance(counts, dict) and counts:
                summary["counts_by_type"] = {
                    str(key): self._coerce_int(value, default=0)
                    for key, value in counts.items()
                }
            status_counts = type_summary.get("status_counts")
            if isinstance(status_counts, dict) and status_counts:
                summary["status_counts"] = {
                    str(key): self._coerce_int(value, default=0)
                    for key, value in status_counts.items()
                }
            types[canonical_type] = summary

        hierarchy_counts = hierarchy_summary.get("counts_by_type")
        return {
            "tree_keys": self._debug_sorted_keys(tree_payload),
            "hierarchy_total_nodes": self._coerce_int(
                hierarchy_summary.get("total_nodes"), default=0
            ),
            "hierarchy_counts_by_type": (
                {
                    str(key): self._coerce_int(value, default=0)
                    for key, value in hierarchy_counts.items()
                }
                if isinstance(hierarchy_counts, dict)
                else {}
            ),
            "types": types,
        }

    def _debug_evse_feature_flag_summary(self) -> dict[str, object]:
        """Return a sanitized summary of EVSE feature-flag discovery."""

        charger_flag_keys: set[str] = set()
        for flags in getattr(self, "_evse_feature_flags_by_serial", {}).values():
            if not isinstance(flags, dict):
                continue
            charger_flag_keys.update(self._debug_sorted_keys(flags))
        payload = getattr(self, "_evse_feature_flags_payload", None)
        meta = payload.get("meta") if isinstance(payload, dict) else None
        error = payload.get("error") if isinstance(payload, dict) else None
        return {
            "site_flag_keys": sorted(
                str(key) for key in getattr(self, "_evse_site_feature_flags", {}).keys()
            ),
            "charger_count": len(
                getattr(self, "_evse_feature_flags_by_serial", {}) or {}
            ),
            "charger_flag_keys": sorted(charger_flag_keys),
            "meta_keys": self._debug_sorted_keys(meta),
            "error_keys": self._debug_sorted_keys(error),
        }

    def _debug_topology_summary(
        self, snapshot: CoordinatorTopologySnapshot
    ) -> dict[str, object]:
        """Return a sanitized summary of active discovery-driven topology."""

        return {
            "inventory_ready": bool(snapshot.inventory_ready),
            "charger_count": len(snapshot.charger_serials),
            "battery_count": len(snapshot.battery_serials),
            "inverter_count": len(snapshot.inverter_serials),
            "active_type_keys": list(snapshot.active_type_keys),
            "gateway_iq_router_count": len(snapshot.gateway_iq_router_keys),
            "site_energy_channels": sorted(self._live_site_energy_channels()),
        }

    @staticmethod
    def _dashboard_key_token(key: object) -> str:
        text = EnphaseCoordinator._coerce_optional_text(key)
        if not text:
            return ""
        return "".join(ch if ch.isalnum() else "_" for ch in text.lower()).strip("_")

    @classmethod
    def _dashboard_key_matches(cls, key: object, *candidates: str) -> bool:
        token = cls._dashboard_key_token(key)
        if not token:
            return False
        candidate_tokens = {
            cls._dashboard_key_token(candidate) for candidate in candidates if candidate
        }
        if token in candidate_tokens:
            return True
        return any(
            token.startswith(candidate)
            or token.endswith(candidate)
            or candidate in token
            for candidate in candidate_tokens
            if candidate and len(candidate) >= 3
        )

    @staticmethod
    def _dashboard_simple_value(value: object) -> object | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, dict):
            out: dict[str, object] = {}
            for key, item in value.items():
                simplified = EnphaseCoordinator._dashboard_simple_value(item)
                if simplified is not None:
                    out[str(key)] = simplified
            return out or None
        if isinstance(value, list):
            out = [
                simplified
                for item in value
                if (simplified := EnphaseCoordinator._dashboard_simple_value(item))
                is not None
            ]
            return out or None
        return EnphaseCoordinator._coerce_optional_text(value)

    @classmethod
    def _iter_dashboard_mappings(cls, value: object) -> Iterable[dict[str, object]]:
        if isinstance(value, dict):
            yield value
            for item in value.values():
                yield from cls._iter_dashboard_mappings(item)
            return
        if isinstance(value, list):
            for item in value:
                yield from cls._iter_dashboard_mappings(item)

    @classmethod
    def _dashboard_first_value(cls, payload: object, *keys: str) -> object | None:
        for mapping in cls._iter_dashboard_mappings(payload):
            for key, value in mapping.items():
                if cls._dashboard_key_matches(key, *keys):
                    return value
        return None

    @classmethod
    def _dashboard_first_mapping(
        cls, payload: object, *keys: str
    ) -> dict[str, object] | None:
        value = cls._dashboard_first_value(payload, *keys)
        if isinstance(value, dict):
            return dict(value)
        return None

    @classmethod
    def _dashboard_field(
        cls, payload: object, *keys: str, default: object | None = None
    ) -> object | None:
        value = cls._dashboard_first_value(payload, *keys)
        simplified = cls._dashboard_simple_value(value)
        if simplified is None:
            return default
        return simplified

    @classmethod
    def _dashboard_field_map(
        cls,
        payload: object,
        fields: dict[str, tuple[str, ...]],
    ) -> dict[str, object]:
        out: dict[str, object] = {}
        for output_key, candidate_keys in fields.items():
            value = cls._dashboard_field(payload, *candidate_keys)
            if value is not None:
                out[output_key] = value
        return out

    @classmethod
    def _dashboard_aliases(cls, payload: dict[str, object]) -> list[str]:
        aliases: list[str] = []
        seen: set[str] = set()
        for key in (
            "device_uid",
            "device-uid",
            "uid",
            "iqer_uid",
            "iqer-uid",
            "hems_device_id",
            "hems-device-id",
            "serial_number",
            "serialNumber",
            "serial",
            "device_id",
            "deviceId",
            "id",
        ):
            value = cls._coerce_optional_text(payload.get(key))
            if not value or value in seen:
                continue
            seen.add(value)
            aliases.append(value)
        return aliases

    @classmethod
    def _dashboard_primary_id(cls, payload: dict[str, object]) -> str | None:
        for key in (
            "device_uid",
            "device-uid",
            "uid",
            "iqer_uid",
            "iqer-uid",
            "hems_device_id",
            "hems-device-id",
            "serial_number",
            "serialNumber",
            "serial",
            "device_id",
            "deviceId",
            "id",
        ):
            value = cls._coerce_optional_text(payload.get(key))
            if value:
                return value
        return None

    @classmethod
    def _dashboard_parent_id(cls, payload: dict[str, object]) -> str | None:
        for key in (
            "parent_uid",
            "parentUid",
            "parent_device_uid",
            "parentDeviceUid",
            "parent_id",
            "parentId",
            "parent",
        ):
            value = cls._coerce_optional_text(payload.get(key))
            if value:
                return value
        return None

    @classmethod
    def _dashboard_raw_type(
        cls, payload: dict[str, object], fallback_type: str | None = None
    ) -> str | None:
        for key in (
            "type",
            "device_type",
            "deviceType",
            "channel_type",
            "channelType",
            "meter_type",
        ):
            value = cls._coerce_optional_text(payload.get(key))
            if value:
                return value
        return fallback_type

    @classmethod
    def _system_dashboard_type_key(cls, raw_type: object) -> str | None:
        text = cls._coerce_optional_text(raw_type)
        if text:
            token = "".join(ch if ch.isalnum() else "_" for ch in text.lower()).strip(
                "_"
            )
            if token in SYSTEM_DASHBOARD_TYPE_KEY_MAP:
                return SYSTEM_DASHBOARD_TYPE_KEY_MAP[token]
        return normalize_type_key(raw_type)

    @classmethod
    def _system_dashboard_detail_records(
        cls,
        payloads: dict[str, object],
        *source_types: str,
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        seen: set[tuple[str | None, str | None, str | None]] = set()
        for source_type in source_types:
            payload = payloads.get(source_type)
            if not isinstance(payload, dict):
                continue
            items = payload.get(source_type)
            if isinstance(items, list):
                source_items = items
            elif isinstance(items, dict):
                nested_items = (
                    items.get("devices")
                    if isinstance(items.get("devices"), list)
                    else (
                        items.get("members")
                        if isinstance(items.get("members"), list)
                        else (
                            items.get("items")
                            if isinstance(items.get("items"), list)
                            else None
                        )
                    )
                )
                source_items = (
                    nested_items if isinstance(nested_items, list) else [items]
                )
            else:
                nested_items = (
                    payload.get("devices")
                    if isinstance(payload.get("devices"), list)
                    else (
                        payload.get("members")
                        if isinstance(payload.get("members"), list)
                        else (
                            payload.get("items")
                            if isinstance(payload.get("items"), list)
                            else None
                        )
                    )
                )
                source_items = (
                    nested_items if isinstance(nested_items, list) else [payload]
                )
            for item in source_items:
                if not isinstance(item, dict):
                    continue
                record = dict(item)
                dedupe_key = (
                    cls._coerce_optional_text(record.get("serial_number")),
                    cls._coerce_optional_text(record.get("device_uid")),
                    cls._coerce_optional_text(record.get("id")),
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                records.append(record)
        return records

    @classmethod
    def _system_dashboard_meter_kind(cls, payload: dict[str, object]) -> str | None:
        for value in (
            payload.get("meter_type"),
            payload.get("config_type"),
            payload.get("channel_type"),
            payload.get("name"),
        ):
            text = cls._coerce_optional_text(value)
            if not text:
                continue
            normalized = "".join(ch if ch.isalnum() else "_" for ch in text.lower())
            if "production" in normalized or normalized in ("prod", "pv", "solar"):
                return "production"
            if "consumption" in normalized or normalized in (
                "cons",
                "load",
                "site_load",
            ):
                return "consumption"
        return None

    @classmethod
    def _system_dashboard_battery_detail_subset(
        cls,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        if not isinstance(payload, dict):
            return {}
        allowed = (
            "phase",
            "operation_mode",
            "app_version",
            "sw_version",
            "rssi_subghz",
            "rssi_24ghz",
            "rssi_dbm",
            "led_status",
            "alarm_id",
        )
        out: dict[str, object] = {}
        for key in allowed:
            value = payload.get(key)
            if value is not None:
                out[key] = value
        return out

    @classmethod
    def _dashboard_node_entry(
        cls,
        payload: dict[str, object],
        *,
        fallback_type: str | None = None,
        parent_uid: str | None = None,
    ) -> dict[str, object] | None:
        device_uid = cls._dashboard_primary_id(payload)
        if not device_uid:
            return None
        raw_type = cls._dashboard_raw_type(payload, fallback_type)
        type_key = cls._system_dashboard_type_key(raw_type)
        entry: dict[str, object] = {"device_uid": device_uid}
        if type_key:
            entry["type_key"] = type_key
        if raw_type:
            entry["source_type"] = raw_type
        parent = cls._dashboard_parent_id(payload) or parent_uid
        if parent:
            entry["parent_uid"] = parent
        name = cls._coerce_optional_text(
            payload.get("name")
            if payload.get("name") is not None
            else payload.get("display_name")
        )
        if name:
            entry["name"] = name
        serial = cls._coerce_optional_text(
            payload.get("serial_number")
            if payload.get("serial_number") is not None
            else (
                payload.get("serialNumber")
                if payload.get("serialNumber") is not None
                else payload.get("serial")
            )
        )
        if serial:
            entry["serial_number"] = serial
        return entry

    @classmethod
    def _dashboard_child_containers(
        cls, payload: dict[str, object]
    ) -> list[tuple[object, str | None]]:
        out: list[tuple[object, str | None]] = []
        next_type = cls._dashboard_raw_type(payload)
        for key, value in payload.items():
            if cls._dashboard_key_matches(
                key,
                "children",
                "child_nodes",
                "devices",
                "members",
                "items",
                "nodes",
                "result",
                "data",
                "envoy",
                "envoys",
                "meter",
                "meters",
                "enpower",
                "enpowers",
                "encharge",
                "encharges",
                "modem",
                "modems",
                "inverter",
                "inverters",
            ) and isinstance(value, (dict, list)):
                out.append((value, next_type))
        return out

    @classmethod
    def _index_dashboard_nodes(
        cls,
        payload: object,
        *,
        fallback_type: str | None = None,
        parent_uid: str | None = None,
        index: dict[str, dict[str, object]] | None = None,
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, dict[str, object]]:
        out = index if isinstance(index, dict) else {}
        aliases = alias_index if isinstance(alias_index, dict) else {}
        if isinstance(payload, list):
            for item in payload:
                cls._index_dashboard_nodes(
                    item,
                    fallback_type=fallback_type,
                    parent_uid=parent_uid,
                    index=out,
                    alias_index=aliases,
                )
            return out
        if not isinstance(payload, dict):
            return out

        entry = cls._dashboard_node_entry(
            payload,
            fallback_type=fallback_type,
            parent_uid=parent_uid,
        )
        next_parent = parent_uid
        next_type = fallback_type
        if entry is not None:
            entry_aliases = cls._dashboard_aliases(payload)
            device_uid = next(
                (
                    canonical_uid
                    for alias in entry_aliases
                    if (canonical_uid := aliases.get(alias)) is not None
                ),
                str(entry["device_uid"]),
            )
            existing = out.get(device_uid, {"device_uid": device_uid})
            for key, value in entry.items():
                if value is None:
                    continue
                existing[key] = value
            existing["device_uid"] = device_uid
            out[device_uid] = existing
            for alias in entry_aliases:
                aliases[alias] = device_uid
            next_parent = device_uid
            next_type = cls._coerce_optional_text(entry.get("source_type")) or next_type

        for child_payload, child_type in cls._dashboard_child_containers(payload):
            cls._index_dashboard_nodes(
                child_payload,
                fallback_type=child_type or next_type,
                parent_uid=next_parent,
                index=out,
                alias_index=aliases,
            )
        return out

    @classmethod
    def _system_dashboard_hierarchy_summary_from_index(
        cls,
        index: dict[str, dict[str, object]],
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, object]:
        counts_by_type: dict[str, int] = {}
        child_counts: dict[str, int] = {}
        relationships: list[dict[str, object]] = []
        aliases = alias_index if isinstance(alias_index, dict) else {}
        for device_uid, entry in index.items():
            type_key = cls._coerce_optional_text(entry.get("type_key"))
            if type_key:
                counts_by_type[type_key] = counts_by_type.get(type_key, 0) + 1
            parent_uid = cls._coerce_optional_text(entry.get("parent_uid"))
            if parent_uid:
                parent_uid = aliases.get(parent_uid, parent_uid)
            if parent_uid:
                child_counts[parent_uid] = child_counts.get(parent_uid, 0) + 1
            relationships.append(
                {
                    "device_uid": device_uid,
                    "parent_uid": parent_uid,
                    "type_key": type_key,
                    "source_type": cls._coerce_optional_text(entry.get("source_type")),
                    "name": cls._coerce_optional_text(entry.get("name")),
                    "serial_number": cls._coerce_optional_text(
                        entry.get("serial_number")
                    ),
                }
            )
        for relationship in relationships:
            relationship["child_count"] = child_counts.get(
                str(relationship["device_uid"]), 0
            )
        relationships.sort(
            key=lambda item: (
                str(item.get("type_key") or ""),
                str(item.get("name") or ""),
                str(item.get("device_uid") or ""),
            )
        )
        return {
            "total_nodes": len(index),
            "counts_by_type": counts_by_type,
            "relationships": relationships,
        }

    @classmethod
    def _system_dashboard_type_hierarchy(
        cls,
        type_key: str,
        index: dict[str, dict[str, object]],
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, object]:
        aliases = alias_index if isinstance(alias_index, dict) else {}
        relationships = [
            {
                "device_uid": device_uid,
                "parent_uid": (
                    aliases.get(parent_uid, parent_uid)
                    if (
                        parent_uid := cls._coerce_optional_text(entry.get("parent_uid"))
                    )
                    else None
                ),
                "name": cls._coerce_optional_text(entry.get("name")),
                "serial_number": cls._coerce_optional_text(entry.get("serial_number")),
                "source_type": cls._coerce_optional_text(entry.get("source_type")),
                "child_count": sum(
                    1
                    for candidate in index.values()
                    if cls._coerce_optional_text(candidate.get("parent_uid"))
                    == device_uid
                ),
            }
            for device_uid, entry in index.items()
            if cls._coerce_optional_text(entry.get("type_key")) == type_key
        ]
        relationships.sort(
            key=lambda item: (
                str(item.get("name") or ""),
                str(item.get("device_uid") or ""),
            )
        )
        return {"count": len(relationships), "relationships": relationships}

    @classmethod
    def _system_dashboard_meter_summaries(
        cls, payloads: dict[str, object]
    ) -> list[dict[str, object]]:
        meters: list[dict[str, object]] = []
        seen: set[tuple[str | None, str | None]] = set()
        for record in cls._system_dashboard_detail_records(payloads, "meters", "meter"):
            name = cls._coerce_optional_text(record.get("name"))
            meter_kind = cls._system_dashboard_meter_kind(record)
            meter_type = (
                cls._coerce_optional_text(record.get("meter_type")) or meter_kind
            )
            dedupe_key = (name, meter_type)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            meter_summary = {
                "name": name,
                "meter_type": meter_type,
                "status": cls._dashboard_field(record, "status", "status_text"),
                "meter_state": cls._coerce_optional_text(record.get("meter_state")),
                "config_type": cls._coerce_optional_text(record.get("config_type")),
            }
            config_payload = cls._dashboard_first_mapping(
                record,
                "configuration",
                "meter_config",
                "meter_configuration",
            )
            if isinstance(config_payload, dict):
                config = cls._dashboard_field_map(
                    config_payload,
                    {
                        "phase": ("phase", "phase_mode", "phase_configuration"),
                        "wiring": ("wiring", "wiring_type"),
                        "mode": ("mode", "config_mode", "meter_mode"),
                        "role": ("role", "measurement", "measurement_type"),
                        "enabled": ("enabled", "is_enabled"),
                    },
                )
                if config:
                    meter_summary["config"] = config
            cleaned = {
                key: value for key, value in meter_summary.items() if value is not None
            }
            if cleaned:
                meters.append(cleaned)
        meters.sort(
            key=lambda item: (
                str(item.get("name") or ""),
                str(item.get("meter_type") or ""),
            )
        )
        return meters

    @classmethod
    def _system_dashboard_envoy_summary(
        cls,
        payloads: dict[str, object],
        index: dict[str, dict[str, object]],
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, object]:
        modem_records = cls._system_dashboard_detail_records(
            payloads, "modems", "modem"
        )
        modem_source = (
            modem_records[0]
            if modem_records
            else cls._dashboard_first_mapping(payloads, "modem", "cellular", "sim")
        )
        envoy_records = cls._system_dashboard_detail_records(
            payloads, "envoys", "envoy"
        )
        envoy_source = envoy_records[0] if envoy_records else payloads
        network_source = cls._dashboard_first_mapping(
            envoy_source,
            "network",
            "network_config",
            "gateway_network",
            "gateway_config",
        )
        tunnel_source = cls._dashboard_first_mapping(envoy_source, "tunnel", "vpn")
        controller_records = cls._system_dashboard_detail_records(
            payloads, "enpowers", "enpower"
        )
        controller_source = (
            controller_records[0]
            if controller_records
            else cls._dashboard_first_mapping(
                payloads,
                "controller",
                "system_controller",
                "enpower",
            )
        )
        summary = {
            "modem": cls._dashboard_field_map(
                modem_source or payloads,
                {
                    "signal": (
                        "signal",
                        "signal_strength",
                        "signal_level",
                        "sig_str",
                    ),
                    "rssi": ("rssi",),
                    "sim_plan_expiry": (
                        "sim_plan_expiry",
                        "plan_expiry",
                        "plan_expiry_date",
                        "plan_end",
                        "sim_expiry",
                    ),
                },
            ),
            "network": cls._dashboard_field_map(
                network_source or envoy_source,
                {
                    "status": ("status", "state", "link_state"),
                    "mode": ("mode", "network_mode", "config_mode"),
                    "type": ("type", "network_type", "connection_type"),
                    "dhcp": ("dhcp", "is_dhcp"),
                    "enabled": ("enabled", "is_enabled"),
                },
            ),
            "tunnel": cls._dashboard_field_map(
                tunnel_source or payloads,
                {
                    "status": ("status", "state"),
                    "type": ("type", "tunnel_type"),
                    "enabled": ("enabled", "is_enabled"),
                    "connected": ("connected", "is_connected"),
                    "healthy": ("healthy", "is_healthy"),
                },
            ),
            "controller": cls._dashboard_field_map(
                controller_source or payloads,
                {
                    "earth_type": (
                        "earth_type",
                        "earthType",
                        "system_earth_type",
                    ),
                    "status": ("status", "state"),
                    "operation_mode": ("operation_mode", "mode"),
                },
            ),
            "meters": cls._system_dashboard_meter_summaries(payloads),
            "hierarchy": cls._system_dashboard_type_hierarchy(
                "envoy", index, alias_index
            ),
        }
        return {
            key: value for key, value in summary.items() if value not in ({}, [], None)
        }

    @classmethod
    def _system_dashboard_encharge_summary(
        cls,
        payloads: dict[str, object],
        index: dict[str, dict[str, object]],
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, object]:
        records = cls._system_dashboard_detail_records(
            payloads, "encharges", "encharge"
        )
        first_record = records[0] if records else payloads
        connectivity_source = cls._dashboard_first_mapping(
            first_record,
            "connectivity",
            "network",
            "wireless",
        )
        software_source = cls._dashboard_first_mapping(
            first_record,
            "software",
            "app",
            "application",
        )
        operation_source = cls._dashboard_first_mapping(
            first_record,
            "operation_mode",
            "operation",
            "mode",
        )
        summary = {
            "connectivity": cls._dashboard_field_map(
                connectivity_source or first_record,
                {
                    "signal": (
                        "signal",
                        "signal_strength",
                        "signal_level",
                        "sig_str",
                    ),
                    "rssi": ("rssi",),
                    "rssi_subghz": ("rssi_subghz",),
                    "rssi_24ghz": ("rssi_24ghz",),
                    "rssi_dbm": ("rssi_dbm",),
                    "status": ("status", "state"),
                },
            ),
            "software": cls._dashboard_field_map(
                software_source or first_record,
                {
                    "app_version": ("app_version", "appVersion", "version"),
                    "firmware": ("firmware", "fw_version", "sw_version"),
                    "sw_version": ("sw_version",),
                },
            ),
            "operation_mode": cls._dashboard_field_map(
                operation_source or first_record,
                {
                    "mode": ("operation_mode", "operationMode", "mode"),
                    "state": ("status", "state"),
                },
            ),
            "batteries": [
                cls._system_dashboard_battery_detail_subset(record)
                | {
                    key: value
                    for key, value in {
                        "name": cls._coerce_optional_text(record.get("name")),
                        "serial_number": cls._coerce_optional_text(
                            record.get("serial_number")
                        ),
                        "status": cls._coerce_optional_text(record.get("status")),
                        "status_text": cls._coerce_optional_text(
                            record.get("statusText")
                        ),
                        "soc": cls._coerce_optional_text(record.get("soc")),
                    }.items()
                    if value is not None
                }
                for record in records
                if cls._system_dashboard_battery_detail_subset(record)
                or cls._coerce_optional_text(record.get("serial_number"))
                or cls._coerce_optional_text(record.get("name"))
            ],
            "hierarchy": cls._system_dashboard_type_hierarchy(
                "encharge", index, alias_index
            ),
        }
        return {
            key: value for key, value in summary.items() if value not in ({}, [], None)
        }

    @classmethod
    def _system_dashboard_microinverter_summary(
        cls,
        payloads: dict[str, object],
        index: dict[str, dict[str, object]],
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, object]:
        summary_payload = (
            cls._dashboard_first_mapping(payloads, "inverters", "inverter") or {}
        )
        if not isinstance(summary_payload, dict):
            return {}
        nested_payload = summary_payload.get("inverters")
        if isinstance(nested_payload, dict):
            summary_payload = nested_payload
        total = cls._coerce_optional_int(summary_payload.get("total"))
        not_reporting = cls._coerce_optional_int(summary_payload.get("not_reporting"))
        plc_comm = cls._coerce_optional_int(summary_payload.get("plc_comm"))
        items = summary_payload.get("items")
        if isinstance(items, list):
            model_counts = {
                cls._coerce_optional_text(item.get("name"))
                or f"item_{index}": (cls._coerce_optional_int(item.get("count")) or 0)
                for index, item in enumerate(items, start=1)
                if isinstance(item, dict)
            }
        else:
            model_counts = {}
        reporting = None
        if total is not None:
            reporting = max(0, total - int(not_reporting or 0))
        connectivity = None
        if total is not None:
            if int(total) <= 0:
                connectivity = None
            elif int(not_reporting or 0) <= 0:
                connectivity = "online"
            elif int(not_reporting or 0) >= int(total):
                connectivity = "offline"
            else:
                connectivity = "degraded"
        summary = {
            "total_inverters": total,
            "reporting_inverters": reporting,
            "not_reporting_inverters": not_reporting,
            "plc_comm_inverters": plc_comm,
            "model_counts": model_counts or None,
            "model_summary": cls._format_inverter_model_summary(model_counts),
            "connectivity": connectivity,
            "hierarchy": cls._system_dashboard_type_hierarchy(
                "microinverter", index, alias_index
            ),
        }
        return {key: value for key, value in summary.items() if value is not None}

    def _build_system_dashboard_summaries(
        self,
        tree_payload: dict[str, object] | None,
        details_payloads: dict[str, dict[str, object]],
    ) -> tuple[
        dict[str, dict[str, object]], dict[str, object], dict[str, dict[str, object]]
    ]:
        hierarchy_index: dict[str, dict[str, object]] = {}
        hierarchy_aliases: dict[str, str] = {}
        if isinstance(tree_payload, dict):
            hierarchy_index = self._index_dashboard_nodes(
                tree_payload, alias_index=hierarchy_aliases
            )
        for type_key, payloads_by_source in details_payloads.items():
            for source_type, payload in payloads_by_source.items():
                self._index_dashboard_nodes(
                    payload,
                    fallback_type=source_type or type_key,
                    index=hierarchy_index,
                    alias_index=hierarchy_aliases,
                )
        hierarchy_summary = self._system_dashboard_hierarchy_summary_from_index(
            hierarchy_index,
            hierarchy_aliases,
        )
        type_summaries: dict[str, dict[str, object]] = {}
        envoy_payloads: dict[str, object] = {}
        for key in ("envoy", "modem"):
            payloads = details_payloads.get(key, {})
            if isinstance(payloads, dict):
                envoy_payloads.update(payloads)
        if envoy_summary := self._system_dashboard_envoy_summary(
            envoy_payloads,
            hierarchy_index,
            hierarchy_aliases,
        ):
            type_summaries["envoy"] = envoy_summary
        if encharge_summary := self._system_dashboard_encharge_summary(
            details_payloads.get("encharge", {}),
            hierarchy_index,
            hierarchy_aliases,
        ):
            type_summaries["encharge"] = encharge_summary
        if microinverter_summary := self._system_dashboard_microinverter_summary(
            details_payloads.get("microinverter", {}),
            hierarchy_index,
            hierarchy_aliases,
        ):
            type_summaries["microinverter"] = microinverter_summary
        return type_summaries, hierarchy_summary, hierarchy_index

    async def _async_refresh_system_dashboard(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._system_dashboard_cache_until:
            if now < self._system_dashboard_cache_until:
                return
        fetch_tree = getattr(self.client, "devices_tree", None)
        fetch_details = getattr(self.client, "devices_details", None)
        if not callable(fetch_tree) and not callable(fetch_details):
            return

        tree_payload = getattr(self, "_system_dashboard_devices_tree_raw", None)
        details_payloads = {
            canonical_type: {
                source_type: dict(payload)
                for source_type, payload in payloads_by_source.items()
                if isinstance(payload, dict)
            }
            for canonical_type, payloads_by_source in getattr(
                self, "_system_dashboard_devices_details_raw", {}
            ).items()
            if isinstance(payloads_by_source, dict)
        }
        if callable(fetch_tree):
            try:
                payload = await fetch_tree()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "System dashboard devices-tree fetch failed: %s",
                    redact_text(err, site_ids=(self.site_id,)),
                )
            else:
                if isinstance(payload, dict):
                    tree_payload = payload

        if callable(fetch_details):
            for source_type in SYSTEM_DASHBOARD_DIAGNOSTIC_TYPES:
                try:
                    payload = await fetch_details(source_type)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "System dashboard devices_details fetch failed for type %s: %s",
                        source_type,
                        err,
                    )
                    continue
                if not isinstance(payload, dict):
                    continue
                canonical_type = self._system_dashboard_type_key(source_type)
                if canonical_type is None:
                    continue
                details_payloads.setdefault(canonical_type, {})[source_type] = payload

        type_summaries, hierarchy_summary, hierarchy_index = (
            self._build_system_dashboard_summaries(tree_payload, details_payloads)
        )
        self._system_dashboard_devices_tree_raw = (
            dict(tree_payload) if isinstance(tree_payload, dict) else None
        )
        self._system_dashboard_devices_details_raw = {
            canonical_type: {
                source_type: dict(payload)
                for source_type, payload in payloads_by_source.items()
                if isinstance(payload, dict)
            }
            for canonical_type, payloads_by_source in details_payloads.items()
            if isinstance(payloads_by_source, dict)
        }

        if isinstance(self._system_dashboard_devices_tree_raw, dict):
            redacted_tree = self._redact_battery_payload(tree_payload)
            self._system_dashboard_devices_tree_payload = (
                redacted_tree
                if isinstance(redacted_tree, dict)
                else {"value": redacted_tree}
            )
        else:
            self._system_dashboard_devices_tree_payload = None

        redacted_details: dict[str, dict[str, object]] = {}
        for (
            canonical_type,
            payloads_by_source,
        ) in self._system_dashboard_devices_details_raw.items():
            redacted_details[canonical_type] = {}
            for source_type, payload in payloads_by_source.items():
                redacted_payload = self._redact_battery_payload(payload)
                redacted_details[canonical_type][source_type] = (
                    redacted_payload
                    if isinstance(redacted_payload, dict)
                    else {"value": redacted_payload}
                )
        self._system_dashboard_devices_details_payloads = redacted_details
        self._system_dashboard_type_summaries = type_summaries
        self._system_dashboard_hierarchy_summary = hierarchy_summary
        self._system_dashboard_hierarchy_index = hierarchy_index
        self._system_dashboard_cache_until = now + DEVICES_INVENTORY_CACHE_TTL
        self._debug_log_summary_if_changed(
            "system_dashboard",
            "System dashboard discovery summary",
            self._debug_system_dashboard_summary(
                self._system_dashboard_devices_tree_raw,
                self._system_dashboard_devices_details_raw,
                type_summaries,
                hierarchy_summary,
            ),
        )

    @staticmethod
    def _coerce_int(value: object, *, default: int = 0) -> int:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(str(value).strip()))
            except (TypeError, ValueError):
                return default

    @staticmethod
    def _normalize_iso_date(value: object) -> str | None:
        """Normalize a YYYY-MM-DD date string."""
        if value is None:
            return None
        try:
            cleaned = str(value).strip()
        except Exception:
            return None
        if not cleaned:
            return None
        try:
            return datetime.strptime(cleaned, "%Y-%m-%d").date().isoformat()
        except Exception:
            return None

    def _inverter_start_date(self) -> str | None:
        """Return query start date for inverter lifetime totals."""
        start_date: str | None = None
        energy = getattr(self, "energy", None)
        if energy is not None:
            meta = getattr(energy, "_site_energy_meta", None)
            if isinstance(meta, dict):
                start_date = self._normalize_iso_date(meta.get("start_date"))
        if start_date:
            return start_date

        existing_starts: list[str] = []
        existing = getattr(self, "_inverter_data", None)
        if isinstance(existing, dict):
            for payload in existing.values():
                if not isinstance(payload, dict):
                    continue
                normalized = self._normalize_iso_date(
                    payload.get("lifetime_query_start_date")
                )
                if normalized:
                    existing_starts.append(normalized)
        if existing_starts:
            return min(existing_starts)
        return None

    def _site_local_current_date(self) -> str:
        """Return current date in site-local timezone when available."""
        inventory_payload = getattr(self, "_devices_inventory_payload", None)
        if isinstance(inventory_payload, dict):
            direct = self._normalize_iso_date(inventory_payload.get("curr_date_site"))
            if direct:
                return direct
            result = inventory_payload.get("result")
            if isinstance(result, list):
                for item in result:
                    if not isinstance(item, dict):
                        continue
                    candidate = self._normalize_iso_date(item.get("curr_date_site"))
                    if candidate:
                        return candidate

        tz_name = getattr(self, "_battery_timezone", None)
        if isinstance(tz_name, str) and tz_name.strip():
            try:
                return datetime.now(ZoneInfo(tz_name.strip())).date().isoformat()
            except Exception:
                pass

        try:
            return dt_util.now().date().isoformat()
        except Exception:
            return datetime.now(tz=_tz.utc).date().isoformat()

    @staticmethod
    def _format_inverter_model_summary(model_counts: dict[str, int]) -> str | None:
        clean: dict[str, int] = {}
        for model, count in (model_counts or {}).items():
            name = str(model).strip()
            if not name:
                continue
            try:
                count_int = int(count)
            except (TypeError, ValueError):
                continue
            if count_int <= 0:
                continue
            clean[name] = count_int
        if not clean:
            return None
        ordered = sorted(clean.items(), key=lambda item: (-item[1], item[0]))
        return ", ".join(f"{name} x{count}" for name, count in ordered)

    @staticmethod
    def _format_inverter_status_summary(summary_counts: dict[str, int]) -> str:
        normal = int(summary_counts.get("normal", 0))
        warning = int(summary_counts.get("warning", 0))
        error = int(summary_counts.get("error", 0))
        not_reporting = int(summary_counts.get("not_reporting", 0))
        summary = (
            f"Normal {normal} | Warning {warning} | "
            f"Error {error} | Not Reporting {not_reporting}"
        )
        unknown = int(summary_counts.get("unknown", 0))
        if unknown > 0:
            summary = f"{summary} | Unknown {unknown}"
        return summary

    @staticmethod
    def _normalize_inverter_status(value: object) -> str:
        if value is None:
            return "unknown"
        try:
            normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        except Exception:
            return "unknown"
        if not normalized:
            return "unknown"
        if any(token in normalized for token in ("fault", "error", "critical")):
            return "error"
        if "warn" in normalized:
            return "warning"
        if any(
            token in normalized
            for token in ("not_reporting", "offline", "disconnected")
        ):
            return "not_reporting"
        if any(
            token in normalized
            for token in ("normal", "online", "connected", "ok", "recommended")
        ):
            return "normal"
        return "unknown"

    @staticmethod
    def _inverter_connectivity_state(summary_counts: dict[str, int]) -> str | None:
        total = int(summary_counts.get("total", 0))
        not_reporting = int(summary_counts.get("not_reporting", 0))
        unknown = int(summary_counts.get("unknown", 0))
        reporting = max(0, total - not_reporting - unknown)
        if total <= 0:
            return None
        if reporting >= total:
            return "online"
        if reporting == 0 and unknown > 0 and not_reporting <= 0:
            return "unknown"
        if reporting > 0:
            return "degraded"
        return "offline"

    @staticmethod
    def _parse_inverter_last_report(value: object) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=_tz.utc)
        epoch_value: float | None = None
        if isinstance(value, (int, float)):
            epoch_value = float(value)
        else:
            try:
                text = str(value).strip()
            except Exception:
                return None
            if not text:
                return None
            if text.endswith("[UTC]"):
                text = text[:-5]
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt_value = datetime.fromisoformat(text)
                return dt_value if dt_value.tzinfo else dt_value.replace(tzinfo=_tz.utc)
            except Exception:
                try:
                    epoch_value = float(text)
                except Exception:
                    return None
        if epoch_value is None:
            return None
        if epoch_value > 1_000_000_000_000:
            epoch_value /= 1000.0
        try:
            return datetime.fromtimestamp(epoch_value, tz=_tz.utc)
        except Exception:
            return None

    def _merge_microinverter_type_bucket(self) -> None:
        """Merge inverter summary/details into the type-device buckets."""
        ready_before = bool(getattr(self, "_devices_inventory_ready", False))
        buckets = dict(getattr(self, "_type_device_buckets", {}) or {})
        ordered = list(getattr(self, "_type_device_order", []) or [])
        key = "microinverter"

        devices_out: list[dict[str, object]] = []
        for serial in self.iter_inverter_serials():
            payload = self.inverter_data(serial)
            if not isinstance(payload, dict):
                continue
            devices_out.append(
                {
                    "name": payload.get("name"),
                    "serial_number": payload.get("serial_number"),
                    "sku_id": payload.get("sku_id"),
                    "status": payload.get("status"),
                    "statusText": payload.get("status_text"),
                    "last_report": payload.get("last_report"),
                    "array_name": payload.get("array_name"),
                    "warranty_end_date": payload.get("warranty_end_date"),
                    "device_id": payload.get("device_id"),
                    "inverter_id": payload.get("inverter_id"),
                    "fw1": payload.get("fw1"),
                    "fw2": payload.get("fw2"),
                }
            )

        if devices_out:
            model_counts = dict(self._inverter_model_counts)
            model_summary = self._format_inverter_model_summary(model_counts)
            summary_counts = dict(self._inverter_summary_counts)
            status_counts = {
                "total": int(summary_counts.get("total", len(devices_out))),
                "normal": int(summary_counts.get("normal", 0)),
                "warning": int(summary_counts.get("warning", 0)),
                "error": int(summary_counts.get("error", 0)),
                "not_reporting": int(summary_counts.get("not_reporting", 0)),
                "unknown": int(summary_counts.get("unknown", 0)),
            }
            latest_reported: datetime | None = None
            latest_reported_device: dict[str, object] | None = None
            array_counts: dict[str, int] = {}
            firmware_counts: dict[str, int] = {}
            for member in devices_out:
                parsed_last = self._parse_inverter_last_report(
                    member.get("last_report")
                )
                if parsed_last is not None and (
                    latest_reported is None or parsed_last > latest_reported
                ):
                    latest_reported = parsed_last
                    latest_reported_device = {
                        "serial_number": member.get("serial_number"),
                        "name": member.get("name"),
                        "status": (
                            member.get("statusText")
                            if member.get("statusText") is not None
                            else member.get("status")
                        ),
                    }
                raw_array = member.get("array_name")
                if raw_array is not None:
                    try:
                        array_name = str(raw_array).strip()
                    except Exception:
                        array_name = ""
                    if array_name:
                        array_counts[array_name] = array_counts.get(array_name, 0) + 1
                raw_firmware = member.get("fw1") or member.get("fw2")
                if raw_firmware is not None:
                    try:
                        firmware = str(raw_firmware).strip()
                    except Exception:
                        firmware = ""
                    if firmware:
                        firmware_counts[firmware] = firmware_counts.get(firmware, 0) + 1
            array_summary = self._format_inverter_model_summary(array_counts)
            firmware_summary = self._format_inverter_model_summary(firmware_counts)
            buckets[key] = {
                "type_key": key,
                "type_label": "Microinverters",
                "count": len(devices_out),
                "devices": devices_out,
                "model_counts": model_counts,
                "model_summary": model_summary or "Microinverters",
                "status_counts": status_counts,
                "status_summary": self._format_inverter_status_summary(summary_counts),
                "connectivity_state": self._inverter_connectivity_state(status_counts),
                "reporting_count": max(
                    0,
                    int(status_counts.get("total", len(devices_out)))
                    - int(status_counts.get("not_reporting", 0))
                    - int(status_counts.get("unknown", 0)),
                ),
                "latest_reported_utc": (
                    latest_reported.isoformat() if latest_reported is not None else None
                ),
                "latest_reported_device": latest_reported_device,
                "array_counts": array_counts,
                "array_summary": array_summary,
                "firmware_counts": firmware_counts,
                "firmware_summary": firmware_summary,
            }
            panel_info = getattr(self, "_inverter_panel_info", None)
            if isinstance(panel_info, dict):
                buckets[key]["panel_info"] = dict(panel_info)
            production_payload = getattr(self, "_inverter_production_payload", None)
            if isinstance(production_payload, dict):
                start_date = self._normalize_iso_date(
                    production_payload.get("start_date")
                )
                end_date = self._normalize_iso_date(production_payload.get("end_date"))
                if start_date:
                    buckets[key]["production_start_date"] = start_date
                if end_date:
                    buckets[key]["production_end_date"] = end_date
            status_type_counts = getattr(self, "_inverter_status_type_counts", None)
            if isinstance(status_type_counts, dict) and status_type_counts:
                buckets[key]["status_type_counts"] = dict(status_type_counts)
            if key not in ordered:
                ordered.append(key)
        else:
            buckets.pop(key, None)
            ordered = [item for item in ordered if item != key]

        self._set_type_device_buckets(buckets, ordered)
        if not ready_before:
            # Preserve unknown-inventory behavior for non-inverter type gating.
            self._devices_inventory_ready = False

    async def _async_refresh_inverters(self) -> None:
        """Refresh inverter metadata/status/production and build serial snapshots."""
        if not self.include_inverters:
            self._inverters_inventory_payload = None
            self._inverter_status_payload = None
            self._inverter_production_payload = None
            self._inverter_data = {}
            self._inverter_order = []
            self._inverter_panel_info = None
            self._inverter_status_type_counts = {}
            self._inverter_model_counts = {}
            self._inverter_summary_counts = {
                "total": 0,
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
            }
            self._merge_microinverter_type_bucket()
            self._merge_heatpump_type_bucket()
            return

        fetch_inventory = getattr(self.client, "inverters_inventory", None)
        fetch_status = getattr(self.client, "inverter_status", None)
        fetch_production = getattr(self.client, "inverter_production", None)
        if not callable(fetch_inventory) or not callable(fetch_status):
            return

        async def _fetch_inventory_page(offset: int) -> dict[str, object] | None:
            try:
                payload = await fetch_inventory(
                    limit=1000,
                    offset=offset,
                    search="",
                )
            except TypeError:
                if offset != 0:
                    return None
                payload = await fetch_inventory()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Inverters inventory fetch failed for site %s: %s",
                    redact_site_id(self.site_id),
                    redact_text(err, site_ids=(self.site_id,)),
                )
                return None
            if not isinstance(payload, dict):
                return None
            return payload

        inventory_payload = await _fetch_inventory_page(0)
        if inventory_payload is None:
            return

        inverters_raw = inventory_payload.get("inverters")
        if not isinstance(inverters_raw, list):
            inverters_raw = []
        inverters_list = [item for item in inverters_raw if isinstance(item, dict)]
        total_expected = self._coerce_int(
            inventory_payload.get("total"), default=len(inverters_list)
        )
        if total_expected > len(inverters_list):
            merged = list(inverters_list)
            next_offset = len(merged)
            while next_offset < total_expected:
                next_payload = await _fetch_inventory_page(next_offset)
                if next_payload is None:
                    break
                next_raw = next_payload.get("inverters")
                if not isinstance(next_raw, list):
                    break
                next_items = [item for item in next_raw if isinstance(item, dict)]
                if not next_items:
                    break
                merged.extend(next_items)
                total_candidate = self._coerce_int(
                    next_payload.get("total"), default=total_expected
                )
                if total_candidate > total_expected:
                    total_expected = total_candidate
                page_size = len(next_items)
                next_offset += page_size
            inventory_payload = dict(inventory_payload)
            inventory_payload["inverters"] = merged
            inverters_list = merged

        try:
            status_payload = await fetch_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Inverter status fetch failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            status_payload = {}
        if not isinstance(status_payload, dict):
            status_payload = {}

        start_date = self._inverter_start_date()
        end_date = self._site_local_current_date()
        production_payload: dict[str, object] = {}
        if callable(fetch_production) and start_date is not None:
            try:
                production_payload = await fetch_production(
                    start_date=start_date, end_date=end_date
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Inverter production fetch failed for site %s: %s",
                    redact_site_id(self.site_id),
                    redact_text(err, site_ids=(self.site_id,)),
                )
                production_payload = {}
        elif start_date is None:
            _LOGGER.debug(
                "Skipping inverter production fetch for site %s: start date unknown",
                redact_site_id(self.site_id),
            )
        if not isinstance(production_payload, dict):
            production_payload = {}
        production_raw = production_payload.get("production")
        if not isinstance(production_raw, dict):
            production_raw = {}

        status_by_serial: dict[str, dict[str, object]] = {}
        for inverter_id, payload in status_payload.items():
            if not isinstance(payload, dict):
                continue
            serial = str(payload.get("serialNum") or "").strip()
            if not serial:
                continue
            item = dict(payload)
            item["inverter_id"] = str(inverter_id)
            status_by_serial[serial] = item

        previous_data = getattr(self, "_inverter_data", None)
        if not isinstance(previous_data, dict):
            previous_data = {}

        inverter_data: dict[str, dict[str, object]] = {}
        inverter_order: list[str] = []
        model_counts: dict[str, int] = {}
        status_type_counts: dict[str, int] = {}
        derived_status_counts: dict[str, int] = {
            "normal": 0,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
        }
        for item in inverters_list:
            if member_is_retired(item):
                continue
            serial = str(item.get("serial_number") or "").strip()
            if not serial:
                continue
            previous_item = previous_data.get(serial)
            if not isinstance(previous_item, dict):
                previous_item = {}
            status_item = status_by_serial.get(serial, {})
            inverter_id = (
                str(
                    status_item.get("inverter_id")
                    or previous_item.get("inverter_id")
                    or ""
                ).strip()
                or None
            )
            production_wh = None
            if inverter_id:
                try:
                    raw_val = production_raw.get(inverter_id)
                    production_wh = float(raw_val) if raw_val is not None else None
                except (TypeError, ValueError):
                    production_wh = None
            prev_wh: float | None = None
            try:
                raw_prev_wh = previous_item.get("lifetime_production_wh")
                prev_wh = float(raw_prev_wh) if raw_prev_wh is not None else None
            except (TypeError, ValueError):
                prev_wh = None
            if production_wh is None or production_wh < 0:
                production_wh = prev_wh
            elif prev_wh is not None and production_wh < prev_wh:
                production_wh = prev_wh

            query_start = self._normalize_iso_date(production_payload.get("start_date"))
            query_end = self._normalize_iso_date(production_payload.get("end_date"))
            if query_start is None:
                query_start = self._normalize_iso_date(
                    previous_item.get("lifetime_query_start_date")
                )
            if query_end is None:
                query_end = self._normalize_iso_date(
                    previous_item.get("lifetime_query_end_date")
                )
            if query_start is None:
                query_start = start_date
            if query_end is None:
                query_end = end_date

            model_name = str(item.get("name") or "").strip()
            if model_name:
                model_counts[model_name] = model_counts.get(model_name, 0) + 1
            status_type = status_item.get("type")
            if status_type is not None:
                try:
                    status_type_text = str(status_type).strip()
                except Exception:
                    status_type_text = ""
                if status_type_text:
                    status_type_counts[status_type_text] = (
                        status_type_counts.get(status_type_text, 0) + 1
                    )
            status_bucket = self._normalize_inverter_status(
                status_item.get("statusCode")
                if status_item.get("statusCode") is not None
                else (
                    status_item.get("status")
                    if status_item.get("status") is not None
                    else item.get("status")
                )
            )
            if status_bucket in derived_status_counts:
                derived_status_counts[status_bucket] += 1
            inverter_data[serial] = {
                "serial_number": serial,
                "name": item.get("name"),
                "array_name": item.get("array_name"),
                "sku_id": item.get("sku_id"),
                "part_num": item.get("part_num"),
                "sku": item.get("sku"),
                "status": item.get("status"),
                "status_text": item.get("statusText"),
                "last_report": item.get("last_report"),
                "fw1": item.get("fw1"),
                "fw2": item.get("fw2"),
                "warranty_end_date": item.get("warranty_end_date"),
                "inverter_id": inverter_id,
                "device_id": status_item.get(
                    "deviceId", previous_item.get("device_id")
                ),
                "inverter_type": status_item.get(
                    "type", previous_item.get("inverter_type")
                ),
                "status_code": status_item.get(
                    "statusCode", previous_item.get("status_code")
                ),
                "show_sig_str": status_item.get(
                    "show_sig_str", previous_item.get("show_sig_str")
                ),
                "emu_version": status_item.get(
                    "emu_version", previous_item.get("emu_version")
                ),
                "issi": status_item.get("issi", previous_item.get("issi")),
                "rssi": status_item.get("rssi", previous_item.get("rssi")),
                "lifetime_production_wh": production_wh,
                "lifetime_query_start_date": query_start,
                "lifetime_query_end_date": query_end,
            }
            inverter_order.append(serial)

        total_count = len(inverter_data)
        normal_count = int(
            derived_status_counts.get("normal")
            or self._coerce_int(inventory_payload.get("normal_count"), default=0)
        )
        warning_count = int(
            derived_status_counts.get("warning")
            or self._coerce_int(inventory_payload.get("warning_count"), default=0)
        )
        error_count = int(
            derived_status_counts.get("error")
            or self._coerce_int(inventory_payload.get("error_count"), default=0)
        )
        not_reporting_count = int(
            derived_status_counts.get("not_reporting")
            or self._coerce_int(inventory_payload.get("not_reporting"), default=0)
        )
        normal_count = max(0, normal_count)
        warning_count = max(0, warning_count)
        error_count = max(0, error_count)
        not_reporting_count = max(0, not_reporting_count)
        counts = {
            "normal": normal_count,
            "warning": warning_count,
            "error": error_count,
            "not_reporting": not_reporting_count,
        }
        known_total = sum(counts.values())
        if known_total > total_count:
            overflow = known_total - total_count
            for key in ("not_reporting", "error", "warning", "normal"):
                if overflow <= 0:
                    break
                reducible = min(counts[key], overflow)
                counts[key] -= reducible
                overflow -= reducible
        known_total = sum(counts.values())
        unknown_count = max(0, total_count - known_total)

        summary_counts = {
            "total": total_count,
            "normal": counts["normal"],
            "warning": counts["warning"],
            "error": counts["error"],
            "not_reporting": counts["not_reporting"],
            "unknown": unknown_count,
        }
        panel_info_out: dict[str, object] | None = None
        panel_info_raw = inventory_payload.get("panel_info")
        if isinstance(panel_info_raw, dict):
            panel_info_out = {}
            for key, value in panel_info_raw.items():
                if value is None:
                    continue
                if isinstance(value, (str, int, float, bool)):
                    if isinstance(value, str):
                        value = value.strip()
                        if not value:
                            continue
                    panel_info_out[str(key)] = value
            if not panel_info_out:
                panel_info_out = None

        self._inverters_inventory_payload = inventory_payload
        self._inverter_status_payload = status_payload
        self._inverter_production_payload = production_payload
        self._inverter_data = inverter_data
        self._inverter_order = inverter_order
        self._inverter_panel_info = panel_info_out
        self._inverter_status_type_counts = status_type_counts
        self._inverter_model_counts = model_counts
        self._inverter_summary_counts = summary_counts
        self._merge_microinverter_type_bucket()
        self._merge_heatpump_type_bucket()

    def _heatpump_primary_device_uid(self) -> str | None:
        members = self._type_bucket_members("heatpump")
        if not members:
            return None
        preferred_types = ("HEAT_PUMP", "ENERGY_METER", "SG_READY_GATEWAY")
        for preferred in preferred_types:
            for member in members:
                if self._heatpump_member_device_type(member) != preferred:
                    continue
                uid = self._type_member_text(member, "device_uid")
                if uid:
                    return uid
        for member in members:
            uid = self._type_member_text(member, "device_uid")
            if uid:
                return uid
        return None

    def _heatpump_power_candidate_device_uids(self) -> list[str | None]:
        candidates: list[str | None] = []
        seen: set[str] = set()

        def _add(uid: str | None) -> None:
            if uid is None:
                return
            if uid in seen:
                return
            seen.add(uid)
            candidates.append(uid)

        _add(self._heatpump_primary_device_uid())
        for member in self._type_bucket_members("heatpump"):
            _add(self._type_member_text(member, "device_uid"))
        candidates.append(None)
        return candidates

    @staticmethod
    def _heatpump_latest_power_sample(payload: object) -> tuple[int, float] | None:
        if not isinstance(payload, dict):
            return None
        values = payload.get("heat_pump_consumption")
        if not isinstance(values, list):
            return None
        for index in range(len(values) - 1, -1, -1):
            raw_value = values[index]
            if raw_value is None:
                continue
            try:
                value = float(raw_value)
            except Exception:
                continue
            if value != value or value in (float("inf"), float("-inf")):
                continue
            return index, value
        return None

    def _heatpump_member_for_uid(self, uid: object) -> dict[str, object] | None:
        uid_text = self._coerce_optional_text(uid)
        if not uid_text:
            return None
        for member in self._type_bucket_members("heatpump"):
            for key in ("device_uid", "uid", "serial_number", "serial"):
                member_uid = self._type_member_text(member, key)
                if member_uid == uid_text:
                    return member
        return None

    @classmethod
    def _heatpump_member_aliases(cls, member: dict[str, object] | None) -> list[str]:
        if not isinstance(member, dict):
            return []
        aliases: list[str] = []
        seen: set[str] = set()
        for key in (
            "device_uid",
            "uid",
            "serial_number",
            "serial",
            "hems_device_id",
            "hems_device_facet_id",
            "device_id",
            "id",
        ):
            alias = cls._type_member_text(member, key)
            if not alias or alias in seen:
                continue
            seen.add(alias)
            aliases.append(alias)
        return aliases

    @classmethod
    def _heatpump_member_primary_id(
        cls, member: dict[str, object] | None
    ) -> str | None:
        aliases = cls._heatpump_member_aliases(member)
        return aliases[0] if aliases else None

    @classmethod
    def _heatpump_member_parent_id(cls, member: dict[str, object] | None) -> str | None:
        if not isinstance(member, dict):
            return None
        return cls._type_member_text(
            member,
            "parent_uid",
            "parentUid",
            "parent_device_uid",
            "parentDeviceUid",
            "parent_id",
            "parentId",
            "parent",
        )

    def _heatpump_member_alias_map(self) -> dict[str, str]:
        alias_map: dict[str, str] = {}
        for member in self._type_bucket_members("heatpump"):
            primary = self._heatpump_member_primary_id(member)
            if not primary:
                continue
            for alias in self._heatpump_member_aliases(member):
                alias_map[alias] = primary
        return alias_map

    def _heatpump_power_inventory_marker(self) -> tuple[tuple[str, str, str, str], ...]:
        alias_map = self._heatpump_member_alias_map()
        marker_rows: list[tuple[str, str, str, str]] = []
        for index, member in enumerate(self._type_bucket_members("heatpump")):
            primary_id = self._heatpump_member_primary_id(member)
            if not primary_id:
                primary_id = f"idx:{index}"
            parent_id = self._heatpump_member_parent_id(member)
            if parent_id:
                parent_id = alias_map.get(parent_id, parent_id)
            status_text = self._heatpump_status_text(member)
            marker_rows.append(
                (
                    primary_id,
                    parent_id or "",
                    self._heatpump_member_device_type(member) or "",
                    status_text.casefold() if isinstance(status_text, str) else "",
                )
            )
        marker_rows.sort()
        return tuple(marker_rows)

    def _heatpump_power_fetch_plan(
        self,
    ) -> tuple[list[str | None], bool, tuple[tuple[str, str, str, str], ...]]:
        marker = self._heatpump_power_inventory_marker()
        candidates = self._heatpump_power_candidate_device_uids()
        compare_all = (
            self._heatpump_power_selection_marker != marker
            or self._heatpump_power_device_uid not in candidates
        )

        ordered: list[str | None] = []
        seen: set[str | None] = set()

        def _add(uid: str | None) -> None:
            if uid in seen:
                return
            seen.add(uid)
            ordered.append(uid)

        if compare_all:
            for candidate in candidates:
                _add(candidate)
            return ordered, True, marker

        _add(self._heatpump_power_device_uid)
        for candidate in candidates:
            _add(candidate)
        return ordered, False, marker

    def _heatpump_power_candidate_is_recommended(self, uid: str | None) -> bool:
        members = self._type_bucket_members("heatpump")
        if not members:
            return False
        alias_map = self._heatpump_member_alias_map()
        candidate = self._heatpump_member_for_uid(uid) if uid else None
        candidate_primary = alias_map.get(uid, uid) if uid else None
        candidate_parent = self._heatpump_member_parent_id(candidate)
        if candidate_parent:
            candidate_parent = alias_map.get(candidate_parent, candidate_parent)

        recommended_members = [
            member
            for member in members
            if isinstance(self._heatpump_status_text(member), str)
            and self._heatpump_status_text(member).casefold() == "recommended"
        ]
        if not recommended_members:
            return False

        any_parent_link = False
        for member in recommended_members:
            member_primary = self._heatpump_member_primary_id(member)
            if member_primary:
                member_primary = alias_map.get(member_primary, member_primary)
            member_parent = self._heatpump_member_parent_id(member)
            if member_parent:
                member_parent = alias_map.get(member_parent, member_parent)
            if member_parent or candidate_parent:
                any_parent_link = True
            if candidate_primary and member_primary == candidate_primary:
                return True
            if candidate_primary and member_parent == candidate_primary:
                return True
            if candidate_parent and member_primary == candidate_parent:
                return True
            if candidate_parent and member_parent == candidate_parent:
                return True

        if not any_parent_link:
            return True
        return False

    def _heatpump_power_candidate_type_rank(
        self,
        payload: dict[str, object],
        requested_uid: str | None,
        *,
        is_recommended: bool,
    ) -> int:
        if not is_recommended:
            return 0
        member = self._heatpump_member_for_uid(
            self._type_member_text(payload, "device_uid", "uid") or requested_uid
        )
        device_type = self._heatpump_member_device_type(member)
        if device_type == "ENERGY_METER":
            return 3
        if device_type == "HEAT_PUMP":
            return 2
        if device_type == "SG_READY_GATEWAY":
            return 1
        return 0

    def _heatpump_power_selection_key(
        self,
        payload: dict[str, object],
        *,
        requested_uid: str | None,
        sample: tuple[int, float] | None,
    ) -> tuple[int, int, int, float, int, int]:
        payload_uid = self._type_member_text(payload, "device_uid", "uid")
        resolved_uid = payload_uid or requested_uid
        is_recommended = (
            1 if self._heatpump_power_candidate_is_recommended(resolved_uid) else 0
        )
        type_rank = self._heatpump_power_candidate_type_rank(
            payload,
            requested_uid,
            is_recommended=bool(is_recommended),
        )
        sample_value = sample[1] if sample is not None else float("-inf")
        sample_index = sample[0] if sample is not None else -1
        return (
            1 if sample is not None else 0,
            is_recommended,
            type_rank,
            sample_value,
            1 if resolved_uid else 0,
            sample_index,
        )

    async def _async_refresh_heatpump_power(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            self._heatpump_power_w = None
            self._heatpump_power_sample_utc = None
            self._heatpump_power_start_utc = None
            self._heatpump_power_device_uid = None
            self._heatpump_power_source = None
            self._heatpump_power_cache_until = None
            self._heatpump_power_backoff_until = None
            self._heatpump_power_last_error = None
            self._heatpump_power_selection_marker = None
            return
        if not force and self._heatpump_power_cache_until is not None:
            if now < self._heatpump_power_cache_until:
                return
        if not force and self._heatpump_power_backoff_until is not None:
            if now < self._heatpump_power_backoff_until:
                return
        await self._async_refresh_hems_support_preflight(force=force)
        if getattr(self.client, "hems_site_supported", None) is False:
            self._heatpump_power_w = None
            self._heatpump_power_sample_utc = None
            self._heatpump_power_start_utc = None
            self._heatpump_power_device_uid = None
            self._heatpump_power_source = None
            self._heatpump_power_cache_until = now + HEATPUMP_POWER_CACHE_TTL
            self._heatpump_power_backoff_until = None
            self._heatpump_power_last_error = None
            self._heatpump_power_selection_marker = None
            return

        fetcher = getattr(self.client, "hems_power_timeseries", None)
        if not callable(fetcher):
            return

        site_date = self._site_local_current_date()
        candidate_uids, compare_all, marker = self._heatpump_power_fetch_plan()
        payload: dict[str, object] | None = None
        sample: tuple[int, float] | None = None
        requested_uid: str | None = None
        selected_key: tuple[int, int, int, float, int, int] | None = None
        last_error: Exception | None = None
        for candidate_uid in candidate_uids:
            try:
                current_payload = await fetcher(
                    device_uid=candidate_uid,
                    site_date=site_date,
                )
            except Exception as err:  # noqa: BLE001
                last_error = err
                _LOGGER.debug(
                    "Heat pump power fetch failed (requested_device_uid=%s): %s",
                    self._debug_truncate_identifier(candidate_uid) or "[redacted]",
                    err,
                )
                continue
            if not isinstance(current_payload, dict):
                continue
            current_sample = self._heatpump_latest_power_sample(current_payload)
            if not compare_all:
                if payload is None:
                    payload = current_payload
                    requested_uid = candidate_uid
                if current_sample is None:
                    continue
                payload = current_payload
                requested_uid = candidate_uid
                sample = current_sample
                selected_key = self._heatpump_power_selection_key(
                    current_payload,
                    requested_uid=candidate_uid,
                    sample=current_sample,
                )
                break
            current_key = self._heatpump_power_selection_key(
                current_payload,
                requested_uid=candidate_uid,
                sample=current_sample,
            )
            if selected_key is None or current_key > selected_key:
                payload = current_payload
                requested_uid = candidate_uid
                sample = current_sample
                selected_key = current_key

        if payload is None and last_error is not None:
            self._heatpump_power_last_error = (
                redact_text(last_error, site_ids=(self.site_id,))
                or last_error.__class__.__name__
            )
            self._heatpump_power_backoff_until = now + HEATPUMP_POWER_FAILURE_BACKOFF_S
            self._heatpump_power_cache_until = None
            return

        self._heatpump_power_cache_until = now + HEATPUMP_POWER_CACHE_TTL
        self._heatpump_power_backoff_until = None
        self._heatpump_power_last_error = None
        if payload is not None:
            self._heatpump_power_selection_marker = marker
        self._heatpump_power_w = None
        self._heatpump_power_sample_utc = None
        self._heatpump_power_start_utc = None
        self._heatpump_power_device_uid = requested_uid
        self._heatpump_power_source = "hems_power_timeseries"

        if not isinstance(payload, dict):
            return

        payload_uid = self._type_member_text(payload, "device_uid", "uid")
        if payload_uid:
            self._heatpump_power_device_uid = payload_uid
        if self._heatpump_power_device_uid:
            self._heatpump_power_source = (
                f"hems_power_timeseries:{self._heatpump_power_device_uid}"
            )
        if sample is None:
            return
        sample_index, sample_value = sample
        self._heatpump_power_w = sample_value

        start_utc = self._parse_inverter_last_report(payload.get("start_date"))
        self._heatpump_power_start_utc = start_utc
        interval_minutes = self._coerce_optional_int(payload.get("interval_minutes"))
        if (
            start_utc is not None
            and interval_minutes is not None
            and interval_minutes > 0
            and sample_index is not None
        ):
            try:
                self._heatpump_power_sample_utc = start_utc + timedelta(
                    minutes=interval_minutes * sample_index
                )
            except Exception:
                self._heatpump_power_sample_utc = None
        elif sample_index is not None:
            self._heatpump_power_sample_utc = dt_util.utcnow()

    def _clear_current_power_consumption(self) -> None:
        self._current_power_consumption_w = None
        self._current_power_consumption_sample_utc = None
        self._current_power_consumption_reported_units = None
        self._current_power_consumption_reported_precision = None
        self._current_power_consumption_source = None

    async def _async_refresh_current_power_consumption(self) -> None:
        fetcher = getattr(self.client, "latest_power", None)
        if not callable(fetcher):
            self._clear_current_power_consumption()
            return

        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            self._clear_current_power_consumption()
            _LOGGER.debug(
                "Skipping current power consumption refresh for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            return

        if not isinstance(payload, dict):
            self._clear_current_power_consumption()
            return

        value = payload.get("value")
        try:
            numeric = float(value)
        except Exception:  # noqa: BLE001
            self._clear_current_power_consumption()
            return
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            self._clear_current_power_consumption()
            return

        sampled_at = None
        sample_time = payload.get("time")
        if sample_time is not None:
            try:
                sample_seconds = float(sample_time)
                if sample_seconds > 10**12:
                    sample_seconds /= 1000.0
                sampled_at = datetime.fromtimestamp(sample_seconds, tz=_tz.utc)
            except Exception:  # noqa: BLE001
                sampled_at = None

        units = payload.get("units")
        if units is not None:
            try:
                units = str(units).strip()
            except Exception:  # noqa: BLE001
                units = None
            if not units:
                units = None

        precision_raw = payload.get("precision")
        precision = None
        if precision_raw is not None:
            try:
                precision = int(precision_raw)
            except Exception:  # noqa: BLE001
                precision = None

        self._current_power_consumption_w = numeric
        self._current_power_consumption_sample_utc = sampled_at
        self._current_power_consumption_reported_units = units
        self._current_power_consumption_reported_precision = precision
        self._current_power_consumption_source = "app-api:get_latest_power"

    def iter_type_keys(self) -> list[str]:
        type_order = getattr(self, "_type_device_order", None)
        if isinstance(type_order, list):
            return [key for key in type_order if self._type_is_selected(key)]
        return []

    def _type_is_selected(self, type_key: object) -> bool:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return False
        selected = getattr(self, "_selected_type_keys", None)
        if selected is None:
            return True
        return normalized in selected

    def has_type(self, type_key: object) -> bool:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return False
        buckets = getattr(self, "_type_device_buckets", None)
        if not isinstance(buckets, dict):
            return False
        bucket = buckets.get(normalized)
        if not isinstance(bucket, dict):
            return False
        try:
            return int(bucket.get("count", 0)) > 0
        except Exception:
            return False

    def has_type_for_entities(self, type_key: object) -> bool:
        """Return whether a type should gate entity creation/availability.

        Before devices-inventory has been parsed at least once, return True to
        avoid suppressing site entities during transient or unsupported
        inventory fetch conditions.
        """
        normalized = normalize_type_key(type_key)
        if not normalized:
            return False
        if not self._type_is_selected(normalized):
            return False
        if not getattr(self, "_devices_inventory_ready", False):
            return True
        if self.has_type(normalized):
            return True
        # BatteryConfig site settings are a separate capability source from
        # devices.json and are the authoritative battery-family signal on some
        # regional deployments where the inventory bucket is missing or delayed.
        if normalized == "encharge":
            return getattr(self, "_battery_has_encharge", None) is True
        return False

    def type_bucket(self, type_key: object) -> dict[str, object] | None:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        buckets = getattr(self, "_type_device_buckets", None)
        if not isinstance(buckets, dict):
            return None
        bucket = buckets.get(normalized)
        if not isinstance(bucket, dict):
            return None
        members = bucket.get("devices")
        if isinstance(members, list):
            members_out = [dict(item) for item in members if isinstance(item, dict)]
        else:
            members_out = []
        out = {
            "type_key": normalized,
            "type_label": bucket.get("type_label") or type_display_label(normalized),
            "count": bucket.get("count", len(members_out)),
            "devices": members_out,
        }
        for key, value in bucket.items():
            if key in out or key == "devices":
                continue
            if isinstance(value, dict):
                out[key] = dict(value)
            elif isinstance(value, list):
                out[key] = list(value)
            else:
                out[key] = value
        return out

    def type_label(self, type_key: object) -> str | None:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        buckets = getattr(self, "_type_device_buckets", None)
        bucket = buckets.get(normalized) if isinstance(buckets, dict) else None
        if isinstance(bucket, dict):
            label = bucket.get("type_label")
            if isinstance(label, str) and label.strip():
                return label
        return type_display_label(normalized)

    def type_identifier(self, type_key: object) -> tuple[str, str] | None:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        if not self.has_type(normalized):
            return None
        return type_identifier(self.site_id, normalized)

    def _type_bucket_members(self, type_key: object) -> list[dict[str, object]]:
        bucket = self.type_bucket(type_key)
        if not isinstance(bucket, dict):
            return []
        members = bucket.get("devices")
        if not isinstance(members, list):
            return []
        return [dict(item) for item in members if isinstance(item, dict)]

    @staticmethod
    def _type_member_text(member: dict[str, object] | None, *keys: str) -> str | None:
        if not isinstance(member, dict):
            return None
        for key in keys:
            value = member.get(key)
            if value is None:
                continue
            try:
                text = str(value).strip()
            except Exception:
                continue
            if text:
                return text
        return None

    def _type_summary_from_values(self, values: Iterable[object]) -> str | None:
        counts: dict[str, int] = {}
        for value in values:
            if value is None:
                continue
            try:
                text = str(value).strip()
            except Exception:
                continue
            if not text:
                continue
            counts[text] = counts.get(text, 0) + 1
        return self._format_inverter_model_summary(counts)

    def _type_member_summary(
        self,
        members: Iterable[dict[str, object]],
        *keys: str,
    ) -> str | None:
        values: list[str] = []
        for member in members:
            value = self._type_member_text(member, *keys)
            if value:
                values.append(value)
        return self._type_summary_from_values(values)

    @staticmethod
    def _iq_type_device_name(type_key: str) -> str | None:
        return {
            "envoy": "IQ Gateway",
            "encharge": "IQ Battery",
            "iqevse": "IQ EV Charger",
            "heatpump": "Heat Pump",
            "microinverter": "IQ Microinverters",
            "generator": "IQ Generator",
        }.get(type_key)

    def _type_member_single_value(
        self, members: Iterable[dict[str, object]], *keys: str
    ) -> str | None:
        values: list[str] = []
        for member in members:
            value = self._type_member_text(member, *keys)
            if value:
                values.append(value)
        if not values:
            return None
        unique_values = list(dict.fromkeys(values))
        if len(unique_values) == 1:
            return unique_values[0]
        return None

    @staticmethod
    def _normalize_mac(value: object) -> str | None:
        if value is None:
            return None
        try:
            text = str(value).strip().lower()
        except Exception:
            return None
        if not text:
            return None

        def _all_hex(chars: str) -> bool:
            return bool(chars) and all(ch in "0123456789abcdef" for ch in chars)

        def _compact_to_colon_hex(compact: str) -> str:
            return ":".join(compact[idx : idx + 2] for idx in range(0, 12, 2))

        if ":" in text or "-" in text:
            parts = [part for part in text.replace("-", ":").split(":") if part]
            if len(parts) != 6:
                return None
            normalized_parts: list[str] = []
            for part in parts:
                if len(part) == 1:
                    part = f"0{part}"
                if len(part) != 2 or not _all_hex(part):
                    return None
                normalized_parts.append(part)
            return ":".join(normalized_parts)

        if "." in text:
            groups = [group for group in text.split(".") if group]
            if len(groups) != 3:
                return None
            if any(len(group) != 4 or not _all_hex(group) for group in groups):
                return None
            return _compact_to_colon_hex("".join(groups))

        if len(text) == 12 and _all_hex(text):
            return _compact_to_colon_hex(text)

        return None

    def _envoy_controller_mac(self) -> str | None:
        controller = self._envoy_system_controller_member()
        if not isinstance(controller, dict):
            return None
        for key in (
            "mac",
            "mac_address",
            "macAddress",
            "eth0_mac",
            "ethernet_mac",
            "wifi_mac",
            "wireless_mac",
        ):
            normalized = self._normalize_mac(controller.get(key))
            if normalized:
                return normalized
        return None

    @staticmethod
    def _envoy_member_kind(member: dict[str, object]) -> str | None:
        channel_type = EnphaseCoordinator._type_member_text(
            member,
            "channel_type",
            "channelType",
            "meter_type",
        )
        if channel_type:
            normalized = "".join(
                ch if ch.isalnum() else "_" for ch in channel_type.lower()
            )
            if (
                normalized in ("enpower", "system_controller", "systemcontroller")
                or "enpower" in normalized
                or "system_controller" in normalized
                or normalized.startswith("systemcontroller")
            ):
                return "controller"
            if "production" in normalized or normalized in ("prod", "pv", "solar"):
                return "production"
            if "consumption" in normalized or normalized in (
                "cons",
                "load",
                "site_load",
            ):
                return "consumption"
        name = (EnphaseCoordinator._type_member_text(member, "name") or "").lower()
        if "system controller" in name:
            return "controller"
        if "controller" in name and "meter" not in name:
            return "controller"
        if "production" in name:
            return "production"
        if "consumption" in name:
            return "consumption"
        return None

    def _envoy_system_controller_member(self) -> dict[str, object] | None:
        for member in self._type_bucket_members("envoy"):
            if self._envoy_member_kind(member) == "controller":
                return member
        return None

    def _heatpump_primary_member(self) -> dict[str, object] | None:
        members = self._type_bucket_members("heatpump")
        for member in members:
            if self._heatpump_member_device_type(member) == "HEAT_PUMP":
                return member
        if members:
            return members[0]
        return None

    def type_device_name(self, type_key: object) -> str | None:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        canonical_iq_name = self._iq_type_device_name(normalized)
        if canonical_iq_name:
            return canonical_iq_name
        bucket = self.type_bucket(normalized)
        if not bucket:
            return None
        label = bucket.get("type_label")
        if not isinstance(label, str) or not label.strip():
            return None
        return label.strip()

    def type_device_model(self, type_key: object) -> str | None:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        if normalized == "envoy":
            member = self._envoy_system_controller_member()
            controller_name = self._type_member_text(
                member,
                "name",
                "model",
                "channel_type",
                "sku_id",
                "model_id",
            )
            if controller_name:
                return controller_name
            return self.type_device_name(normalized) or self.type_label(normalized)
        if normalized == "heatpump":
            primary_member = self._heatpump_primary_member()
            primary_model = self._type_member_text(
                primary_member,
                "model",
                "sku_id",
                "model_id",
                "part_num",
                "part_number",
                "hardware_sku",
                "name",
            )
            if primary_model:
                return primary_model
            members = self._type_bucket_members(normalized)
            summary_model = self._type_member_summary(
                members,
                "model",
                "sku_id",
                "model_id",
                "part_num",
                "part_number",
                "hardware_sku",
                "name",
            )
            if summary_model:
                return summary_model
            return self.type_device_name(normalized) or self.type_label(normalized)
        members = self._type_bucket_members(normalized)
        model = self._type_member_single_value(
            members,
            "model",
            "sku_id",
            "model_id",
            "part_num",
            "part_number",
            "channel_type",
            "name",
        )
        if model:
            return model
        return self.type_device_name(normalized) or self.type_label(normalized)

    def type_device_serial_number(self, type_key: object) -> str | None:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        if normalized == "envoy":
            serial_keys = ("serial_number", "serial", "serialNumber", "device_sn")
            controller = self._envoy_system_controller_member()
            return self._type_member_text(controller, *serial_keys)
        if normalized == "heatpump":
            primary = self._heatpump_primary_member()
            serial = self._type_member_text(
                primary,
                "serial_number",
                "serial",
                "serialNumber",
                "device_sn",
                "uid",
                "device_uid",
            )
            if serial:
                return serial
            return self._type_member_single_value(
                self._type_bucket_members(normalized),
                "serial_number",
                "serial",
                "serialNumber",
                "device_sn",
                "uid",
                "device_uid",
            )
        if normalized in ("encharge", "microinverter", "iqevse", "generator"):
            return self._type_member_single_value(
                self._type_bucket_members(normalized),
                "serial_number",
                "serial",
                "serialNumber",
                "device_sn",
            )
        return None

    def type_device_model_id(self, type_key: object) -> str | None:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        model_id_keys = (
            "sku_id",
            "model_id",
            "sku",
            "modelId",
            "part_num",
            "part_number",
        )
        if normalized == "envoy":
            controller = self._envoy_system_controller_member()
            model_id = self._type_member_text(controller, *model_id_keys)
        elif normalized == "heatpump":
            primary = self._heatpump_primary_member()
            model_id = self._type_member_text(
                primary,
                *model_id_keys,
                "hardware_sku",
            )
            if not model_id:
                model_id = self._type_member_single_value(
                    self._type_bucket_members(normalized),
                    *model_id_keys,
                    "hardware_sku",
                )
            if not model_id:
                model_id = self._type_member_summary(
                    self._type_bucket_members(normalized),
                    *model_id_keys,
                    "hardware_sku",
                )
        elif normalized in ("encharge", "microinverter", "iqevse", "generator"):
            model_id = self._type_member_single_value(
                self._type_bucket_members(normalized),
                *model_id_keys,
            )
        else:
            return None
        if _is_redundant_model_id(self.type_device_model(type_key), model_id):
            return None
        return model_id

    def type_device_sw_version(self, type_key: object) -> str | None:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        sw_keys = (
            "envoy_sw_version",
            "sw_version",
            "firmware_version",
            "software_version",
            "system_version",
            "application_version",
        )
        if normalized == "envoy":
            controller = self._envoy_system_controller_member()
            return self._type_member_text(controller, *sw_keys)
        if normalized == "heatpump":
            primary = self._heatpump_primary_member()
            sw_version = self._type_member_text(primary, *sw_keys)
            if sw_version:
                return sw_version
            sw_version = self._type_member_single_value(
                self._type_bucket_members(normalized), *sw_keys
            )
            if sw_version:
                return sw_version
            return self._type_member_summary(
                self._type_bucket_members(normalized), *sw_keys
            )
        if normalized in ("encharge", "iqevse", "generator"):
            return self._type_member_single_value(
                self._type_bucket_members(normalized),
                *sw_keys,
            )
        if normalized == "microinverter":
            return self._type_member_single_value(
                self._type_bucket_members(normalized),
                "fw1",
                "fw2",
                *sw_keys,
            )
        return None

    def type_device_hw_version(self, type_key: object) -> str | None:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        if normalized == "envoy":
            controller = self._envoy_system_controller_member()
            return self._type_member_text(
                controller,
                "hw_version",
                "hardware_version",
                "hardwareVersion",
            )
        if normalized == "heatpump":
            primary = self._heatpump_primary_member()
            hw_version = self._type_member_text(
                primary,
                "hw_version",
                "hardware_version",
                "hardwareVersion",
                "hardware_sku",
                "part_num",
                "part_number",
                "sku_id",
            )
            if hw_version:
                return hw_version
            hw_version = self._type_member_single_value(
                self._type_bucket_members(normalized),
                "hw_version",
                "hardware_version",
                "hardwareVersion",
                "hardware_sku",
                "part_num",
                "part_number",
                "sku_id",
            )
            if hw_version:
                return hw_version
            return self._type_member_summary(
                self._type_bucket_members(normalized),
                "hw_version",
                "hardware_version",
                "hardwareVersion",
                "hardware_sku",
                "part_num",
                "part_number",
                "sku_id",
            )
        if normalized in ("microinverter", "encharge", "iqevse", "generator"):
            return self._type_member_single_value(
                self._type_bucket_members(normalized),
                "hw_version",
                "hardware_version",
                "hardwareVersion",
                "part_num",
                "part_number",
                "sku_id",
            )
        return None

    def type_device_info(self, type_key: object):
        from homeassistant.helpers.entity import DeviceInfo

        normalized = normalize_type_key(type_key)
        if not normalized:
            return None
        identifier = self.type_identifier(type_key)
        if identifier is None:
            return None
        label = self.type_label(type_key) or "Device"
        name = self.type_device_name(type_key) or label
        model = self.type_device_model(type_key) or label
        info_kwargs: dict[str, object] = {
            "identifiers": {identifier},
            "manufacturer": "Enphase",
            "model": model,
            "name": name,
        }
        serial_number = self.type_device_serial_number(type_key)
        if serial_number:
            info_kwargs["serial_number"] = serial_number
        model_id = self.type_device_model_id(type_key)
        if model_id:
            info_kwargs["model_id"] = model_id
        sw_version = self.type_device_sw_version(type_key)
        if sw_version:
            info_kwargs["sw_version"] = sw_version
        hw_summary = self.type_device_hw_version(type_key)
        if hw_summary:
            info_kwargs["hw_version"] = hw_summary
        if normalized == "envoy":
            controller_mac = self._envoy_controller_mac()
            if controller_mac:
                from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

                info_kwargs["connections"] = {(CONNECTION_NETWORK_MAC, controller_mac)}
        return DeviceInfo(**info_kwargs)

    def iter_inverter_serials(self) -> list[str]:
        """Return currently active inverter serials in a stable order."""
        order = getattr(self, "_inverter_order", None) or []
        data = getattr(self, "_inverter_data", None)
        if not isinstance(data, dict):
            return []
        serials = [str(sn) for sn in order if sn in data]
        serials.extend(str(sn) for sn in data.keys())
        return [sn for sn in dict.fromkeys(serials) if sn]

    def inverter_data(self, serial: str) -> dict[str, object] | None:
        """Return normalized inverter snapshot for a serial."""
        data = getattr(self, "_inverter_data", None)
        if not isinstance(data, dict):
            return None
        try:
            key = str(serial).strip()
        except Exception:
            return None
        if not key:
            return None
        payload = data.get(key)
        if not isinstance(payload, dict):
            return None
        return dict(payload)

    @staticmethod
    def parse_type_identifier(identifier: object) -> tuple[str, str] | None:
        return parse_type_identifier(identifier)

    def collect_site_metrics(self) -> dict[str, object]:
        """Return a snapshot of site-level metrics for diagnostics."""

        def _iso(dt: datetime | None) -> str | None:
            if not dt:
                return None
            try:
                return dt.isoformat()
            except Exception:
                return str(dt)

        backoff_until = self._backoff_until or 0.0
        backoff_active = bool(backoff_until and backoff_until > time.monotonic())
        scheduler_backoff_active = self._scheduler_backoff_active()
        type_keys = self.iter_type_keys()
        type_counts: dict[str, int] = {}
        for key in type_keys:
            bucket = self.type_bucket(key)
            if not bucket:
                continue
            try:
                type_counts[key] = int(bucket.get("count", 0))
            except Exception:
                type_counts[key] = 0
        metrics: dict[str, object] = {
            "site_id": self.site_id,
            "site_name": self.site_name,
            "last_success": _iso(self.last_success_utc),
            "last_error": getattr(self, "_last_error", None),
            "last_failure": _iso(self.last_failure_utc),
            "last_failure_status": getattr(self, "last_failure_status", None),
            "last_failure_description": getattr(self, "last_failure_description", None),
            "last_failure_source": getattr(self, "last_failure_source", None),
            "last_failure_response": getattr(self, "last_failure_response", None),
            "latency_ms": self.latency_ms,
            "backoff_active": backoff_active,
            "backoff_until_monotonic": self._backoff_until,
            "backoff_ends_utc": _iso(self.backoff_ends_utc),
            "network_errors": getattr(self, "_network_errors", 0),
            "http_errors": getattr(self, "_http_errors", 0),
            "rate_limit_hits": getattr(self, "_rate_limit_hits", 0),
            "dns_errors": getattr(self, "_dns_failures", 0),
            "phase_timings": self.phase_timings,
            "bootstrap_phase_timings": self.bootstrap_phase_timings,
            "warmup_phase_timings": self.warmup_phase_timings,
            "warmup_in_progress": getattr(self, "_warmup_in_progress", False),
            "warmup_last_error": getattr(self, "_warmup_last_error", None),
            "type_device_keys": type_keys,
            "type_device_counts": type_counts,
            "inverters_enabled": bool(getattr(self, "include_inverters", True)),
            "inverters_count": len(getattr(self, "_inverter_data", {}) or {}),
            "inverters_summary_counts": dict(
                getattr(self, "_inverter_summary_counts", {}) or {}
            ),
            "inverters_model_counts": dict(
                getattr(self, "_inverter_model_counts", {}) or {}
            ),
            "session_cache_ttl_s": getattr(self, "_session_history_cache_ttl", None),
            "scheduler_available": self.scheduler_available,
            "scheduler_failures": getattr(self, "_scheduler_failures", 0),
            "scheduler_last_error": getattr(self, "_scheduler_last_error", None),
            "scheduler_last_failure": _iso(
                getattr(self, "_scheduler_last_failure_utc", None)
            ),
            "scheduler_backoff_active": scheduler_backoff_active,
            "scheduler_backoff_ends_utc": _iso(
                getattr(self, "_scheduler_backoff_ends_utc", None)
            ),
            "auth_settings_available": self.auth_settings_available,
            "auth_settings_failures": getattr(self, "_auth_settings_failures", 0),
            "auth_settings_last_error": getattr(
                self, "_auth_settings_last_error", None
            ),
            "auth_settings_last_failure": _iso(
                getattr(self, "_auth_settings_last_failure_utc", None)
            ),
            "auth_settings_backoff_active": self._auth_settings_backoff_active(),
            "auth_settings_backoff_ends_utc": _iso(
                getattr(self, "_auth_settings_backoff_ends_utc", None)
            ),
            "battery_profile": getattr(self, "_battery_profile", None),
            "battery_profile_label": self._battery_profile_label(
                getattr(self, "_battery_profile", None)
            ),
            "battery_backup_percentage": getattr(
                self, "_battery_backup_percentage", None
            ),
            "battery_supports_mqtt": getattr(self, "_battery_supports_mqtt", None),
            "battery_operation_mode_sub_type": getattr(
                self, "_battery_operation_mode_sub_type", None
            ),
            "battery_profile_polling_interval_s": getattr(
                self, "_battery_polling_interval_s", None
            ),
            "battery_cfg_control_show": getattr(
                self, "_battery_cfg_control_show", None
            ),
            "battery_cfg_control_enabled": getattr(
                self, "_battery_cfg_control_enabled", None
            ),
            "battery_cfg_control_schedule_supported": getattr(
                self, "_battery_cfg_control_schedule_supported", None
            ),
            "battery_cfg_control_force_schedule_supported": getattr(
                self, "_battery_cfg_control_force_schedule_supported", None
            ),
            "battery_profile_evse_device": getattr(
                self, "_battery_profile_evse_device", None
            ),
            "battery_use_battery_for_self_consumption": getattr(
                self, "_battery_use_battery_for_self_consumption", None
            ),
            "battery_profile_pending": self.battery_profile_pending,
            "battery_pending_profile": getattr(self, "_battery_pending_profile", None),
            "battery_pending_reserve": getattr(self, "_battery_pending_reserve", None),
            "battery_pending_sub_type": getattr(
                self, "_battery_pending_sub_type", None
            ),
            "battery_pending_requested_at": _iso(
                getattr(self, "_battery_pending_requested_at", None)
            ),
            "battery_pending_age_s": self.battery_pending_age_seconds,
            "battery_pending_timeout_s": int(BATTERY_PROFILE_PENDING_TIMEOUT_S),
            "battery_profile_options": self.battery_profile_option_labels,
            "battery_show_charge_from_grid": getattr(
                self, "_battery_show_charge_from_grid", None
            ),
            "battery_show_savings_mode": getattr(
                self, "_battery_show_savings_mode", None
            ),
            "battery_show_storm_guard": getattr(
                self, "_battery_show_storm_guard", None
            ),
            "battery_show_production": getattr(self, "_battery_show_production", None),
            "battery_show_consumption": getattr(
                self, "_battery_show_consumption", None
            ),
            "battery_show_full_backup": getattr(
                self, "_battery_show_full_backup", None
            ),
            "battery_show_backup_percentage": getattr(
                self, "_battery_show_battery_backup_percentage", None
            ),
            "battery_has_encharge": getattr(self, "_battery_has_encharge", None),
            "battery_has_enpower": getattr(self, "_battery_has_enpower", None),
            "battery_country_code": getattr(self, "_battery_country_code", None),
            "battery_region": getattr(self, "_battery_region", None),
            "battery_locale": getattr(self, "_battery_locale", None),
            "battery_timezone": getattr(self, "_battery_timezone", None),
            "battery_feature_details": getattr(self, "_battery_feature_details", None),
            "battery_user_is_owner": getattr(self, "_battery_user_is_owner", None),
            "battery_user_is_installer": getattr(
                self, "_battery_user_is_installer", None
            ),
            "battery_site_status_code": getattr(
                self, "_battery_site_status_code", None
            ),
            "battery_site_status_text": getattr(
                self, "_battery_site_status_text", None
            ),
            "battery_site_status_severity": getattr(
                self, "_battery_site_status_severity", None
            ),
            "battery_charging_modes_enabled": getattr(
                self, "_battery_is_charging_modes_enabled", None
            ),
            "battery_status_aggregate_charge_pct": getattr(
                self, "_battery_aggregate_charge_pct", None
            ),
            "battery_status_aggregate_state": getattr(
                self, "_battery_aggregate_status", None
            ),
            "battery_status_storage_count": len(
                getattr(self, "_battery_storage_data", {}) or {}
            ),
            "battery_status_storage_order": list(
                getattr(self, "_battery_storage_order", []) or []
            ),
            "battery_status_details": dict(
                getattr(self, "_battery_aggregate_status_details", {}) or {}
            ),
            "battery_backup_history_count": len(
                getattr(self, "_battery_backup_history_events", []) or []
            ),
            "battery_write_in_progress": bool(
                getattr(self, "_battery_profile_write_lock", None)
                and self._battery_profile_write_lock.locked()
            ),
            "battery_grid_mode": getattr(self, "_battery_grid_mode", None),
            "battery_mode_display": self.battery_mode_display,
            "battery_charge_from_grid_allowed": self.battery_charge_from_grid_allowed,
            "battery_discharge_to_grid_allowed": self.battery_discharge_to_grid_allowed,
            "battery_hide_charge_from_grid": getattr(
                self, "_battery_hide_charge_from_grid", None
            ),
            "battery_envoy_supports_vls": getattr(
                self, "_battery_envoy_supports_vls", None
            ),
            "battery_charge_from_grid": getattr(
                self, "_battery_charge_from_grid", None
            ),
            "battery_charge_from_grid_schedule_enabled": getattr(
                self, "_battery_charge_from_grid_schedule_enabled", None
            ),
            "battery_charge_begin_time": getattr(
                self, "_battery_charge_begin_time", None
            ),
            "battery_charge_end_time": getattr(self, "_battery_charge_end_time", None),
            "battery_cfg_schedule_limit": getattr(
                self, "_battery_cfg_schedule_limit", None
            ),
            "battery_schedules_payload": getattr(
                self, "_battery_schedules_payload", None
            ),
            "battery_accepted_itc_disclaimer": getattr(
                self, "_battery_accepted_itc_disclaimer", None
            ),
            "battery_very_low_soc": getattr(self, "_battery_very_low_soc", None),
            "battery_very_low_soc_min": getattr(
                self, "_battery_very_low_soc_min", None
            ),
            "battery_very_low_soc_max": getattr(
                self, "_battery_very_low_soc_max", None
            ),
            "battery_settings_write_in_progress": bool(
                getattr(self, "_battery_settings_write_lock", None)
                and self._battery_settings_write_lock.locked()
            ),
            "storm_guard_state": getattr(self, "_storm_guard_state", None),
            "storm_evse_enabled": getattr(self, "_storm_evse_enabled", None),
            "storm_alert_active": getattr(self, "_storm_alert_active", None),
            "storm_alert_critical_override": getattr(
                self, "_storm_alert_critical_override", None
            ),
            "storm_alert_count": len(getattr(self, "_storm_alerts", []) or []),
            "evse_feature_flags_available": bool(
                getattr(self, "_evse_feature_flags_payload", None)
            ),
            "evse_feature_flag_site_keys": sorted(
                str(key) for key in getattr(self, "_evse_site_feature_flags", {}).keys()
            ),
            "evse_feature_flag_charger_count": len(
                getattr(self, "_evse_feature_flags_by_serial", {}) or {}
            ),
            "grid_control_supported": self.grid_control_supported,
            "grid_toggle_allowed": self.grid_toggle_allowed,
            "grid_toggle_pending": self.grid_toggle_pending,
            "grid_toggle_blocked_reasons": self.grid_toggle_blocked_reasons,
            "grid_control_disable": self.grid_control_disable,
            "grid_control_active_download": self.grid_control_active_download,
            "grid_control_sunlight_backup_system_check": self.grid_control_sunlight_backup_system_check,
            "grid_control_grid_outage_check": self.grid_control_grid_outage_check,
            "grid_control_user_initiated_toggle": self.grid_control_user_initiated_toggle,
            "grid_control_fetch_failures": getattr(
                self, "_grid_control_check_failures", 0
            ),
            "grid_control_data_stale": self.grid_control_supported is None,
            "dry_contact_settings_supported": self.dry_contact_settings_supported,
            "dry_contact_settings_contact_count": len(
                getattr(self, "_dry_contact_settings_entries", []) or []
            ),
            "dry_contact_settings_unmatched_count": len(
                getattr(self, "_dry_contact_unmatched_settings", []) or []
            ),
            "dry_contact_settings_fetch_failures": getattr(
                self, "_dry_contact_settings_failures", 0
            ),
            "dry_contact_settings_data_stale": self.dry_contact_settings_supported
            is None,
            "hems_devices_data_stale": bool(
                getattr(self, "_hems_devices_using_stale", False)
            ),
        }
        grid_last_success = getattr(self, "_grid_control_check_last_success_mono", None)
        if isinstance(grid_last_success, (int, float)):
            age = time.monotonic() - float(grid_last_success)
            if age >= 0:
                metrics["grid_control_last_success_age_s"] = round(age, 1)
        dry_contacts_last_success = getattr(
            self, "_dry_contact_settings_last_success_mono", None
        )
        if isinstance(dry_contacts_last_success, (int, float)):
            age = time.monotonic() - float(dry_contacts_last_success)
            if age >= 0:
                metrics["dry_contact_settings_last_success_age_s"] = round(age, 1)
        hems_last_success = getattr(self, "_hems_devices_last_success_mono", None)
        if isinstance(hems_last_success, (int, float)):
            age = time.monotonic() - float(hems_last_success)
            if age >= 0:
                metrics["hems_devices_last_success_age_s"] = round(age, 1)
        metrics["hems_devices_last_success_utc"] = _iso(
            getattr(self, "_hems_devices_last_success_utc", None)
        )
        session_manager = getattr(self, "session_history", None)
        if session_manager is not None:
            metrics["session_history_available"] = getattr(
                session_manager, "service_available", None
            )
            metrics["session_history_failures"] = getattr(
                session_manager, "service_failures", None
            )
            metrics["session_history_last_error"] = getattr(
                session_manager, "service_last_error", None
            )
            metrics["session_history_last_failure"] = _iso(
                getattr(session_manager, "service_last_failure_utc", None)
            )
            metrics["session_history_backoff_active"] = getattr(
                session_manager, "service_backoff_active", None
            )
            metrics["session_history_backoff_ends_utc"] = _iso(
                getattr(session_manager, "service_backoff_ends_utc", None)
            )
        energy_manager = getattr(self, "energy", None)
        if energy_manager is not None:
            metrics["site_energy_available"] = getattr(
                energy_manager, "service_available", None
            )
            metrics["site_energy_failures"] = getattr(
                energy_manager, "service_failures", None
            )
            metrics["site_energy_last_error"] = getattr(
                energy_manager, "service_last_error", None
            )
            metrics["site_energy_last_failure"] = _iso(
                getattr(energy_manager, "service_last_failure_utc", None)
            )
            metrics["site_energy_backoff_active"] = getattr(
                energy_manager, "service_backoff_active", None
            )
            metrics["site_energy_backoff_ends_utc"] = _iso(
                getattr(energy_manager, "service_backoff_ends_utc", None)
            )
        evse_timeseries = getattr(self, "evse_timeseries", None)
        if evse_timeseries is not None:
            metrics["evse_timeseries_available"] = getattr(
                evse_timeseries, "service_available", None
            )
            metrics["evse_timeseries_failures"] = getattr(
                evse_timeseries, "service_failures", None
            )
            metrics["evse_timeseries_last_error"] = getattr(
                evse_timeseries, "service_last_error", None
            )
            metrics["evse_timeseries_last_failure"] = _iso(
                getattr(evse_timeseries, "service_last_failure_utc", None)
            )
            metrics["evse_timeseries_backoff_active"] = getattr(
                evse_timeseries, "service_backoff_active", None
            )
            metrics["evse_timeseries_backoff_ends_utc"] = _iso(
                getattr(evse_timeseries, "service_backoff_ends_utc", None)
            )
        site_energy_age = None
        site_flows = {}
        site_meta = {}
        if energy_manager is not None:
            site_energy_age = getattr(energy_manager, "site_energy_cache_age", None)
            site_flows = getattr(energy_manager, "site_energy", None) or {}
            site_meta = getattr(energy_manager, "site_energy_meta", None) or {}
        if site_flows or site_energy_age is not None or site_meta:
            metrics["site_energy"] = {
                "flows": sorted(list(site_flows.keys())),
                "cache_age_s": (
                    round(site_energy_age, 3) if site_energy_age is not None else None
                ),
                "start_date": site_meta.get("start_date"),
                "last_report_date": _iso(site_meta.get("last_report_date")),
                "update_pending": site_meta.get("update_pending"),
                "interval_minutes": site_meta.get("interval_minutes"),
            }
        if evse_timeseries is not None:
            metrics["evse_timeseries"] = evse_timeseries.diagnostics()
        firmware_catalog_manager = getattr(self, "firmware_catalog_manager", None)
        status_snapshot = getattr(firmware_catalog_manager, "status_snapshot", None)
        if callable(status_snapshot):
            try:
                status = status_snapshot()
            except Exception:  # noqa: BLE001
                status = {}
            if isinstance(status, dict):
                metrics["firmware_catalog_last_fetch_utc"] = status.get(
                    "last_fetch_utc"
                )
                metrics["firmware_catalog_last_success_utc"] = status.get(
                    "last_success_utc"
                )
                metrics["firmware_catalog_last_error"] = status.get("last_error")
                metrics["firmware_catalog_using_stale"] = status.get("using_stale")
                metrics["firmware_catalog_generated_at"] = status.get(
                    "catalog_generated_at"
                )
                metrics["firmware_catalog_source_age_seconds"] = status.get(
                    "catalog_source_age_seconds"
                )
        return metrics

    def charge_mode_cache_snapshot(self) -> dict[str, str]:
        """Return scheduler mode cache keyed by charger serial."""

        return {
            str(sn): str(value[0])
            for sn, value in self._charge_mode_cache.items()
            if value and value[0]
        }

    def session_history_diagnostics(self) -> dict[str, object]:
        """Return session-history cache and service diagnostics."""

        session_manager = getattr(self, "session_history", None)
        if session_manager is None:
            return {
                "cache_ttl_seconds": None,
                "cache_keys": 0,
                "interval_minutes": None,
                "in_progress": 0,
            }
        return {
            "cache_ttl_seconds": session_manager.cache_ttl,
            "cache_keys": session_manager.cache_key_count,
            "interval_minutes": getattr(self, "_session_history_interval_min", None),
            "in_progress": session_manager.in_progress,
        }

    def evse_timeseries_diagnostics(self) -> dict[str, object]:
        """Return EVSE-timeseries cache and service diagnostics."""

        manager = getattr(self, "evse_timeseries", None)
        if manager is None:
            return {
                "cache_ttl_seconds": None,
                "daily_cache_days": [],
                "daily_cache_age_seconds": {},
                "lifetime_cache_age_seconds": None,
                "lifetime_serial_count": 0,
            }
        return manager.diagnostics()

    def scheduler_diagnostics(self) -> dict[str, object]:
        """Return scheduler availability and failure diagnostics."""

        backoff_ends = getattr(self, "_scheduler_backoff_ends_utc", None)
        if isinstance(backoff_ends, datetime):
            backoff_ends = backoff_ends.isoformat()
        return {
            "available": self.scheduler_available,
            "last_error": self.scheduler_last_error,
            "failures": getattr(self, "_scheduler_failures", None),
            "backoff_until_monotonic": getattr(self, "_scheduler_backoff_until", None),
            "backoff_ends_utc": backoff_ends,
        }

    def battery_diagnostics_payloads(self) -> dict[str, object]:
        """Return battery-related payload snapshots used by diagnostics."""

        return {
            "site_settings_payload": getattr(
                self, "_battery_site_settings_payload", None
            ),
            "profile_payload": getattr(self, "_battery_profile_payload", None),
            "settings_payload": getattr(self, "_battery_settings_payload", None),
            "status_payload": getattr(self, "_battery_status_payload", None),
            "grid_control_check_payload": getattr(
                self, "_grid_control_check_payload", None
            ),
            "dry_contacts_payload": getattr(
                self, "_dry_contact_settings_payload", None
            ),
            "backup_history_payload": getattr(
                self, "_battery_backup_history_payload", None
            ),
            "hems_devices_payload": getattr(self, "_hems_devices_payload", None),
            "devices_inventory_payload": getattr(
                self, "_devices_inventory_payload", None
            ),
        }

    def evse_diagnostics_payloads(self) -> dict[str, object]:
        """Return EVSE capability snapshots used by diagnostics."""

        charger_feature_flags = [
            {"serial": serial, "flags": dict(flags)}
            for serial, flags in sorted(
                getattr(self, "_evse_feature_flags_by_serial", {}).items()
            )
        ]
        charger_support_sources = []
        for serial, snapshot in sorted((self.data or {}).items()):
            if not isinstance(snapshot, dict):
                continue
            sources = {
                key: snapshot.get(f"{key}_source")
                for key in (
                    "charge_mode_supported",
                    "charging_amps_supported",
                    "storm_guard_supported",
                    "auth_feature_supported",
                    "rfid_feature_supported",
                    "plug_and_charge_supported",
                )
                if snapshot.get(f"{key}_source") is not None
            }
            if not sources:
                continue
            charger_support_sources.append({"serial": serial, "sources": sources})
        payload = getattr(self, "_evse_feature_flags_payload", None)
        payload_meta = payload.get("meta") if isinstance(payload, dict) else None
        payload_error = payload.get("error") if isinstance(payload, dict) else None
        return {
            "feature_flags_meta": (
                payload_meta if isinstance(payload_meta, dict) else None
            ),
            "feature_flags_error": (
                payload_error if isinstance(payload_error, dict) else None
            ),
            "site_feature_flags": dict(
                getattr(self, "_evse_site_feature_flags", {}) or {}
            ),
            "charger_feature_flags": charger_feature_flags,
            "charger_support_sources": charger_support_sources,
            "timeseries": self.evse_timeseries_diagnostics(),
        }

    def inverter_diagnostics_payloads(self) -> dict[str, object]:
        """Return inverter-related payload snapshots used by diagnostics."""

        bucket_snapshot = self.type_bucket("microinverter")
        return {
            "enabled": bool(getattr(self, "include_inverters", True)),
            "summary_counts": getattr(self, "_inverter_summary_counts", None),
            "model_counts": getattr(self, "_inverter_model_counts", None),
            "status_type_counts": getattr(self, "_inverter_status_type_counts", None),
            "panel_info": getattr(self, "_inverter_panel_info", None),
            "inventory_payload": getattr(self, "_inverters_inventory_payload", None),
            "status_payload": getattr(self, "_inverter_status_payload", None),
            "production_payload": getattr(self, "_inverter_production_payload", None),
            "bucket_snapshot": bucket_snapshot,
        }

    def _system_dashboard_raw_payloads(
        self, canonical_type: str
    ) -> dict[str, dict[str, object]]:
        payloads = getattr(self, "_system_dashboard_devices_details_raw", None)
        if not isinstance(payloads, dict):
            return {}
        raw = payloads.get(canonical_type)
        if not isinstance(raw, dict):
            return {}
        return {
            str(source_type): dict(payload)
            for source_type, payload in raw.items()
            if isinstance(payload, dict)
        }

    def system_dashboard_envoy_detail(self) -> dict[str, object] | None:
        """Return the primary dashboard gateway detail record when available."""

        records = self._system_dashboard_detail_records(
            self._system_dashboard_raw_payloads("envoy"),
            "envoys",
            "envoy",
        )
        if not records:
            return None
        record = records[0]
        out: dict[str, object] = {}
        for key in (
            "status",
            "statusText",
            "connected",
            "last_report",
            "last_interval_end_date",
            "envoy_sw_version",
            "ap_mode",
            "sku_id",
        ):
            value = record.get(key)
            if value is not None:
                out[key] = value
        return out or None

    def system_dashboard_meter_detail(
        self, meter_kind: str
    ) -> dict[str, object] | None:
        """Return a sanitized dashboard meter record for the requested kind."""

        for record in self._system_dashboard_detail_records(
            self._system_dashboard_raw_payloads("envoy"),
            "meters",
            "meter",
        ):
            if self._system_dashboard_meter_kind(record) != meter_kind:
                continue
            out: dict[str, object] = {}
            for key in (
                "name",
                "serial_number",
                "channel_type",
                "status",
                "statusText",
                "last_report",
                "meter_state",
                "config_type",
                "meter_type",
            ):
                value = record.get(key)
                if value is not None:
                    out[key] = value
            return out or None
        return None

    def system_dashboard_battery_detail(self, serial: str) -> dict[str, object] | None:
        """Return sanitized dashboard battery detail fields for a battery."""

        snapshots = getattr(self, "_battery_storage_data", None)
        snapshot = snapshots.get(serial) if isinstance(snapshots, dict) else None
        candidates: set[str] = set()
        for value in (
            serial,
            snapshot.get("serial_number") if isinstance(snapshot, dict) else None,
            snapshot.get("identity") if isinstance(snapshot, dict) else None,
            snapshot.get("battery_id") if isinstance(snapshot, dict) else None,
            snapshot.get("id") if isinstance(snapshot, dict) else None,
        ):
            text = self._coerce_optional_text(value)
            if text:
                candidates.add(text)
        if not candidates:
            return None
        for record in self._system_dashboard_detail_records(
            self._system_dashboard_raw_payloads("encharge"),
            "encharges",
            "encharge",
        ):
            record_serial = self._coerce_optional_text(record.get("serial_number"))
            record_id = self._coerce_optional_text(record.get("id"))
            if record_serial not in candidates and record_id not in candidates:
                continue
            detail = self._system_dashboard_battery_detail_subset(record)
            return detail or None
        return None

    def system_dashboard_diagnostics(self) -> dict[str, object]:
        """Return cached system dashboard diagnostics payloads and summaries."""

        devices_tree_payload = getattr(
            self, "_system_dashboard_devices_tree_payload", None
        )
        devices_details_payloads = getattr(
            self, "_system_dashboard_devices_details_payloads", None
        )
        hierarchy_summary = getattr(self, "_system_dashboard_hierarchy_summary", None)
        type_summaries = getattr(self, "_system_dashboard_type_summaries", None)
        out: dict[str, object] = {
            "devices_tree_payload": (
                self._copy_diagnostics_value(devices_tree_payload)
                if isinstance(devices_tree_payload, dict)
                else None
            ),
            "devices_details_payloads": (
                self._copy_diagnostics_value(devices_details_payloads)
                if isinstance(devices_details_payloads, dict)
                else {}
            ),
            "hierarchy_summary": (
                self._copy_diagnostics_value(hierarchy_summary)
                if isinstance(hierarchy_summary, dict)
                else {}
            ),
            "type_summaries": (
                self._copy_diagnostics_value(type_summaries)
                if isinstance(type_summaries, dict)
                else {}
            ),
        }
        return out

    def _issue_translation_placeholders(
        self, metrics: dict[str, object]
    ) -> dict[str, str]:
        placeholders: dict[str, str] = {"site_id": str(self.site_id)}
        site_name = metrics.get("site_name")
        if site_name:
            placeholders["site_name"] = str(site_name)
        last_error = metrics.get("last_error") or metrics.get(
            "last_failure_description"
        )
        if last_error:
            placeholders["last_error"] = str(last_error)
        status = metrics.get("last_failure_status")
        if status is not None:
            placeholders["last_status"] = str(status)
        return placeholders

    def _issue_context(self) -> tuple[dict[str, object], dict[str, str]]:
        metrics = self.collect_site_metrics()
        return metrics, self._issue_translation_placeholders(metrics)

    async def _async_update_data(self) -> dict:
        t0 = time.monotonic()
        phase_timings: dict[str, float] = {}
        fallback_data: dict[str, dict] = {}
        first_refresh = not self._has_successful_refresh
        if isinstance(self.data, dict):
            try:
                fallback_data = dict(self.data)
            except Exception:
                fallback_data = self.data

        if self.site_only or not self.serials:
            self._backoff_until = None
            self._clear_backoff_timer()
            ir.async_delete_issue(self.hass, DOMAIN, "reauth_required")
            if self._network_issue_reported:
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NETWORK_UNREACHABLE)
                self._network_issue_reported = False
            if self._cloud_issue_reported:
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CLOUD_ERRORS)
                self._cloud_issue_reported = False
            if self._dns_issue_reported:
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_DNS_RESOLUTION)
                self._dns_issue_reported = False
            self._unauth_errors = 0
            self._rate_limit_hits = 0
            self._http_errors = 0
            self._network_errors = 0
            self._dns_failures = 0
            self._last_error = None
            self.backoff_ends_utc = None
            self._has_successful_refresh = True
            site_energy_start = time.monotonic()
            await self.energy._async_refresh_site_energy()
            self._sync_site_energy_discovery_state()
            self._sync_site_energy_issue()
            phase_timings["site_energy_s"] = round(
                time.monotonic() - site_energy_start, 3
            )
            if not first_refresh:
                await self._async_run_staged_refresh_calls(
                    phase_timings,
                    defer_topology=True,
                    parallel_calls=(
                        (
                            "battery_site_settings_s",
                            "battery site settings",
                            lambda: self._async_refresh_battery_site_settings(),
                        ),
                        (
                            "battery_backup_history_s",
                            "battery backup history",
                            lambda: self._async_refresh_battery_backup_history(),
                        ),
                        (
                            "battery_settings_s",
                            "battery settings",
                            lambda: self._async_refresh_battery_settings(),
                        ),
                        (
                            "battery_schedules_s",
                            "battery schedules",
                            lambda: self._async_refresh_battery_schedules(),
                        ),
                        (
                            "storm_guard_s",
                            "storm guard",
                            lambda: self._async_refresh_storm_guard_profile(),
                        ),
                        (
                            "storm_alert_s",
                            "storm alert",
                            lambda: self._async_refresh_storm_alert(),
                        ),
                        (
                            "grid_control_check_s",
                            "grid control",
                            lambda: self._async_refresh_grid_control_check(),
                        ),
                        (
                            "dry_contact_settings_s",
                            "dry contact settings",
                            lambda: self._async_refresh_dry_contact_settings(),
                        ),
                        (
                            "current_power_s",
                            "current power consumption",
                            lambda: self._async_refresh_current_power_consumption(),
                        ),
                    ),
                    ordered_calls=(
                        (
                            "battery_status_s",
                            "battery status",
                            lambda: self._async_refresh_battery_status(),
                        ),
                        (
                            "devices_inventory_s",
                            "device inventory",
                            lambda: self._async_refresh_devices_inventory(),
                        ),
                        (
                            "hems_devices_s",
                            "HEMS inventory",
                            lambda: self._async_refresh_hems_devices(),
                        ),
                        (
                            "inverters_s",
                            "inverters",
                            lambda: self._async_refresh_inverters(),
                        ),
                    ),
                )
                heatpump_started = time.monotonic()
                try:
                    await self._async_refresh_heatpump_power()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Skipping heat pump power refresh: %s",
                        redact_text(err, site_ids=(self.site_id,)),
                    )
                phase_timings["heatpump_power_s"] = round(
                    time.monotonic() - heatpump_started, 3
                )
            self._prune_runtime_caches(active_serials=(), keep_day_keys=())
            self._sync_battery_profile_pending_issue()
            self.last_success_utc = dt_util.utcnow()
            self.latency_ms = int((time.monotonic() - t0) * 1000)
            phase_timings["total_s"] = round(time.monotonic() - t0, 3)
            self._phase_timings = phase_timings.copy()
            if first_refresh:
                self._bootstrap_phase_timings = phase_timings.copy()
            self._refresh_cached_topology()
            self._schedule_discovery_snapshot_save()
            return {}

        # Helper to normalize epoch-like inputs to seconds
        def _sec(v):
            try:
                iv = int(v)
                # Convert ms -> s if too large
                if iv > 10**12:
                    iv = iv // 1000
                return iv
            except Exception:
                return None

        def _extract_description(raw: str | None) -> str | None:
            """Best-effort extraction of a descriptive message from error payloads."""

            if not raw:
                return None
            text = str(raw).strip()

            def _search(obj):
                if isinstance(obj, dict):
                    for key in (
                        "description",
                        "code_description",
                        "codeDescription",
                        "displayMessage",
                        "message",
                        "detail",
                        "error_description",
                        "errorDescription",
                        "errorMessage",
                    ):
                        val = obj.get(key)
                        if isinstance(val, str) and val.strip():
                            return val.strip()
                    # Dive into common nested containers
                    for key in ("error", "details", "data"):
                        nested = obj.get(key)
                        result = _search(nested)
                        if result:
                            return result
                elif isinstance(obj, list):
                    for item in obj:
                        result = _search(item)
                        if result:
                            return result
                elif isinstance(obj, str):
                    if obj.strip():
                        return obj.strip()
                return None

            candidates = [text]
            trimmed = text.strip("\"'")
            if trimmed != text:
                candidates.append(trimmed)
            for candidate in candidates:
                try:
                    parsed = json.loads(candidate)
                except Exception:
                    continue
                description = _search(parsed)
                if description:
                    return description
            return None

        # Handle backoff window
        if self._backoff_until and time.monotonic() < self._backoff_until:
            raise UpdateFailed("In backoff due to rate limiting or server errors")

        try:
            status_start = time.monotonic()
            data = await self.client.status()
            phase_timings["status_s"] = round(time.monotonic() - status_start, 3)
            self._unauth_errors = 0
            ir.async_delete_issue(self.hass, DOMAIN, "reauth_required")
        except ConfigEntryAuthFailed:
            raise
        except Unauthorized as err:
            raise ConfigEntryAuthFailed from err
        except InvalidPayloadError as err:
            reason = (err.summary or str(err) or "Invalid JSON response").strip()
            self._last_error = reason
            self._network_errors = 0
            self._http_errors = 0
            self._payload_errors += 1
            jitter = random.uniform(1.0, 2.5)
            backoff_multiplier = 2 ** min(self._payload_errors - 1, 3)
            slow_floor = self._slow_interval_floor()
            backoff = max(slow_floor, slow_floor * backoff_multiplier * jitter)
            self._backoff_until = time.monotonic() + backoff
            self._schedule_backoff_timer(backoff)
            if self._payload_errors >= 2 and not self._cloud_issue_reported:
                metrics, placeholders = self._issue_context()
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    ISSUE_CLOUD_ERRORS,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_CLOUD_ERRORS,
                    translation_placeholders=placeholders,
                    data={"site_metrics": metrics},
                )
                self._cloud_issue_reported = True
            now_utc = dt_util.utcnow()
            self.last_failure_utc = now_utc
            self.last_failure_status = None
            self.last_failure_description = reason
            self.last_failure_response = reason
            self.last_failure_source = "payload"
            raise UpdateFailed(f"Invalid API payload: {reason}")
        except aiohttp.ClientResponseError as err:
            url = None
            try:
                url = getattr(err.request_info, "real_url", None)
            except Exception:
                url = None
            if is_scheduler_unavailable_error(err.message, err.status, url):
                self._note_scheduler_unavailable(
                    err, status=err.status, raw_payload=err.message
                )
                self._phase_timings = phase_timings.copy()
                return fallback_data
            # Respect Retry-After and create a warning issue on repeated 429
            self._last_error = f"HTTP {err.status}"
            self._network_errors = 0
            self._payload_errors = 0
            self._http_errors += 1
            retry_after = err.headers.get("Retry-After") if err.headers else None
            delay = 0
            if retry_after:
                try:
                    delay = int(retry_after)
                except Exception:
                    retry_dt = None
                    try:
                        retry_dt = parsedate_to_datetime(str(retry_after))
                    except Exception:
                        retry_dt = None
                    if retry_dt is not None:
                        if retry_dt.tzinfo is None:
                            retry_dt = retry_dt.replace(tzinfo=_tz.utc)
                        retry_dt = retry_dt.astimezone(_tz.utc)
                        now_utc = dt_util.utcnow()
                        delay = max(
                            0,
                            (retry_dt - now_utc).total_seconds(),
                        )
                    else:
                        delay = 0
            # Exponential backoff anchored to configured slow poll interval
            jitter = random.uniform(1.0, 3.0)
            backoff_multiplier = 2 ** min(self._http_errors - 1, 3)
            slow_floor = self._slow_interval_floor()
            backoff = max(delay, slow_floor * backoff_multiplier * jitter)
            self._backoff_until = time.monotonic() + backoff
            self._schedule_backoff_timer(backoff)
            if err.status == 429:
                self._rate_limit_hits += 1
                if self._rate_limit_hits >= 2:
                    metrics, placeholders = self._issue_context()
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        "rate_limited",
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="rate_limited",
                        translation_placeholders=placeholders,
                        data={"site_metrics": metrics},
                    )
            else:
                is_server_error = 500 <= err.status < 600
                if is_server_error:
                    if self._http_errors >= 2 and not self._cloud_issue_reported:
                        metrics, placeholders = self._issue_context()
                        ir.async_create_issue(
                            self.hass,
                            DOMAIN,
                            ISSUE_CLOUD_ERRORS,
                            is_fixable=False,
                            severity=ir.IssueSeverity.WARNING,
                            translation_key=ISSUE_CLOUD_ERRORS,
                            translation_placeholders=placeholders,
                            data={"site_metrics": metrics},
                        )
                        self._cloud_issue_reported = True
                elif self._cloud_issue_reported:
                    ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CLOUD_ERRORS)
                    self._cloud_issue_reported = False
            raw_payload = redact_text(err.message, site_ids=(self.site_id,))
            description = _extract_description(raw_payload)
            reason = redact_text(err.message, site_ids=(self.site_id,))
            if not reason:
                reason = err.__class__.__name__
            now_utc = dt_util.utcnow()
            self.last_failure_utc = now_utc
            self.last_failure_status = err.status
            if description is None:
                try:
                    description = HTTPStatus(int(err.status)).phrase
                except Exception:
                    description = "HTTP error"
            self.last_failure_description = description
            self.last_failure_response = (
                raw_payload if raw_payload is not None else (reason or None)
            )
            self.last_failure_source = "http"
            raise UpdateFailed(f"Cloud error {err.status}: {reason}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            msg = redact_text(err, site_ids=(self.site_id,))
            if not msg:
                msg = err.__class__.__name__
            self._last_error = msg
            self._network_errors += 1
            self._payload_errors = 0
            msg_lower = msg.lower()
            dns_failure = any(
                token in msg_lower
                for token in (
                    "dns",
                    "name or service not known",
                    "temporary failure in name resolution",
                    "resolv",
                )
            )
            if dns_failure:
                self._dns_failures += 1
            else:
                self._dns_failures = 0
                if self._dns_issue_reported:
                    ir.async_delete_issue(self.hass, DOMAIN, ISSUE_DNS_RESOLUTION)
                    self._dns_issue_reported = False
            backoff_multiplier = 2 ** min(self._network_errors - 1, 3)
            jitter = random.uniform(1.0, 2.5)
            slow_floor = self._slow_interval_floor()
            backoff = max(slow_floor, slow_floor * backoff_multiplier * jitter)
            self._backoff_until = time.monotonic() + backoff
            self._schedule_backoff_timer(backoff)
            if self._network_errors >= 3 and not self._network_issue_reported:
                metrics, placeholders = self._issue_context()
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    ISSUE_NETWORK_UNREACHABLE,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_NETWORK_UNREACHABLE,
                    translation_placeholders=placeholders,
                    data={"site_metrics": metrics},
                )
                self._network_issue_reported = True
            if dns_failure and self._dns_failures >= 2 and not self._dns_issue_reported:
                metrics, placeholders = self._issue_context()
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    ISSUE_DNS_RESOLUTION,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_DNS_RESOLUTION,
                    translation_placeholders=placeholders,
                    data={"site_metrics": metrics},
                )
                self._dns_issue_reported = True
            now_utc = dt_util.utcnow()
            self.last_failure_utc = now_utc
            self.last_failure_status = None
            self.last_failure_description = msg
            self.last_failure_response = None
            self.last_failure_source = "network"
            raise UpdateFailed(f"Error communicating with API: {msg}")
        finally:
            self.latency_ms = int((time.monotonic() - t0) * 1000)

        # Success path: reset counters, record last success
        if self._unauth_errors:
            # Clear any outstanding reauth issues on success
            ir.async_delete_issue(self.hass, DOMAIN, "reauth_required")
        self._unauth_errors = 0
        self._rate_limit_hits = 0
        self._http_errors = 0
        self._payload_errors = 0
        if self._network_issue_reported:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NETWORK_UNREACHABLE)
            self._network_issue_reported = False
        self._network_errors = 0
        if self._cloud_issue_reported:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CLOUD_ERRORS)
            self._cloud_issue_reported = False
        self._backoff_until = None
        self._clear_backoff_timer()
        self._last_error = None
        if self._dns_issue_reported:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_DNS_RESOLUTION)
            self._dns_issue_reported = False
        self._dns_failures = 0
        self.last_success_utc = dt_util.utcnow()

        if not first_refresh:
            await self._async_run_staged_refresh_calls(
                phase_timings,
                defer_topology=True,
                parallel_calls=(
                    (
                        "battery_site_settings_s",
                        "battery site settings",
                        lambda: self._async_refresh_battery_site_settings(),
                    ),
                    (
                        "battery_backup_history_s",
                        "battery backup history",
                        lambda: self._async_refresh_battery_backup_history(),
                    ),
                    (
                        "battery_settings_s",
                        "battery settings",
                        lambda: self._async_refresh_battery_settings(),
                    ),
                    (
                        "battery_schedules_s",
                        "battery schedules",
                        lambda: self._async_refresh_battery_schedules(),
                    ),
                    (
                        "storm_guard_s",
                        "storm guard",
                        lambda: self._async_refresh_storm_guard_profile(),
                    ),
                    (
                        "storm_alert_s",
                        "storm alert",
                        lambda: self._async_refresh_storm_alert(),
                    ),
                    (
                        "grid_control_check_s",
                        "grid control",
                        lambda: self._async_refresh_grid_control_check(),
                    ),
                    (
                        "dry_contact_settings_s",
                        "dry contact settings",
                        lambda: self._async_refresh_dry_contact_settings(),
                    ),
                    (
                        "current_power_s",
                        "current power consumption",
                        lambda: self._async_refresh_current_power_consumption(),
                    ),
                ),
                ordered_calls=(
                    (
                        "battery_status_s",
                        "battery status",
                        lambda: self._async_refresh_battery_status(),
                    ),
                    (
                        "devices_inventory_s",
                        "device inventory",
                        lambda: self._async_refresh_devices_inventory(),
                    ),
                    (
                        "hems_devices_s",
                        "HEMS inventory",
                        lambda: self._async_refresh_hems_devices(),
                    ),
                ),
            )

        prev_data = self.data if isinstance(self.data, dict) else {}
        self._has_successful_refresh = True
        out: dict[str, dict] = {}
        arr = data.get("evChargerData") or []
        data_ts = data.get("ts")
        records: list[tuple[str, dict]] = []
        charge_mode_candidates: list[str] = []
        for obj in arr:
            sn = str(obj.get("sn") or "")
            if not sn:
                continue
            self._ensure_serial_tracked(sn)
            records.append((sn, obj))
            if not self._has_embedded_charge_mode(obj):
                charge_mode_candidates.append(sn)

        charge_modes: dict[str, str | None] = {}
        if (
            not first_refresh
            and not charge_mode_candidates
            and records
            and not self.scheduler_available
            and not self._scheduler_backoff_active()
        ):
            try:
                await self._get_charge_mode(records[0][0])
            except Exception:
                pass
        if not first_refresh and charge_mode_candidates:
            unique_candidates = list(dict.fromkeys(charge_mode_candidates))
            charge_start = time.monotonic()
            if unique_candidates:
                charge_modes = await self._async_resolve_charge_modes(unique_candidates)
            phase_timings["charge_mode_s"] = round(time.monotonic() - charge_start, 3)

        green_settings: dict[str, tuple[bool | None, bool]] = {}
        if not first_refresh and records:
            green_start = time.monotonic()
            green_settings = await self._async_resolve_green_battery_settings(
                [sn for sn, _obj in records]
            )
            phase_timings["green_settings_s"] = round(time.monotonic() - green_start, 3)

        auth_settings: dict[str, tuple[bool | None, bool | None, bool, bool]] = {}
        if not first_refresh and records:
            auth_serials = [sn for sn, _obj in records]
            auth_start = time.monotonic()
            if auth_serials:
                auth_settings = await self._async_resolve_auth_settings(auth_serials)
            phase_timings["auth_settings_s"] = round(time.monotonic() - auth_start, 3)

        def _as_bool(v):
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return v != 0
            if isinstance(v, str):
                return v.strip().lower() in ("true", "1", "yes", "y")
            return False

        def _as_optional_bool(v):
            if v is None:
                return None
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return v != 0
            if isinstance(v, str):
                normalized = v.strip().lower()
                if normalized in ("true", "1", "yes", "y", "enabled", "enable", "on"):
                    return True
                if normalized in (
                    "false",
                    "0",
                    "no",
                    "n",
                    "disabled",
                    "disable",
                    "off",
                ):
                    return False
            return None

        def _support_value_and_source(
            runtime_value: bool | None,
            feature_flag_value: bool | None,
        ) -> tuple[bool | None, str]:
            if runtime_value is not None:
                return runtime_value, "runtime"
            if feature_flag_value is not None:
                return feature_flag_value, "feature_flag"
            return None, "unknown"

        def _as_float(v, *, precision: int | None = None):
            if v is None:
                return None
            if isinstance(v, (int, float)):
                val = float(v)
            elif isinstance(v, str):
                s = v.strip()
                if not s:
                    return None
                try:
                    val = float(s)
                except Exception:
                    return None
            else:
                return None
            if precision is not None:
                try:
                    return round(val, precision)
                except Exception:
                    return val
            return val

        def _as_int(v):
            if isinstance(v, bool) or v is None:
                return None
            if isinstance(v, (int, float)):
                try:
                    return int(v)
                except Exception:
                    return None
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    return None
                try:
                    return int(float(s))
                except Exception:
                    return None
            return None

        def _as_text(v):
            if v is None:
                return None
            try:
                text = str(v).strip()
            except Exception:
                return None
            return text or None

        def _as_int_list(v):
            if not isinstance(v, list):
                return None
            out: list[int] = []
            for item in v:
                coerced = _as_int(item)
                if coerced is None:
                    continue
                out.append(coerced)
            return out or None

        for sn, obj in records:
            conn0 = (obj.get("connectors") or [{}])[0]
            has_embedded_charge_mode = self._has_embedded_charge_mode(obj)
            raw_safe_limit = conn0.get("safeLimitState")
            if raw_safe_limit is None:
                raw_safe_limit = conn0.get("safe_limit_state")
            if raw_safe_limit is None:
                raw_safe_limit = obj.get("safeLimitState")
            if isinstance(raw_safe_limit, bool):
                safe_limit_state = int(raw_safe_limit)
            else:
                safe_limit_state = _as_int(raw_safe_limit)
            charging_level = None
            for key in ("chargingLevel", "charging_level", "charginglevel"):
                if key in obj and obj.get(key) is not None:
                    charging_level = obj.get(key)
                    break
            if charging_level is None:
                charging_level = self.last_set_amps.get(sn)
            # On initial load or after restart, seed the local last_set_amps
            # so UI controls (number entity) reflect the current setpoint
            # instead of defaulting to 0/min.
            safe_limit_active = (
                safe_limit_state is not None and int(safe_limit_state) != 0
            )
            safe_limit_level = _as_int(charging_level)
            skip_seed = safe_limit_active and safe_limit_level == SAFE_LIMIT_AMPS
            if (
                sn not in self.last_set_amps
                and charging_level is not None
                and not skip_seed
            ):
                try:
                    self.set_last_set_amps(sn, int(charging_level))
                except Exception:
                    pass
            sch = obj.get("sch_d") or {}
            sch_info0 = (sch.get("info") or [{}])[0]
            sess = obj.get("session_d") or {}
            smart_ev = obj.get("smartEV")
            if not isinstance(smart_ev, dict):
                smart_ev = {}
            # Derive last reported if not provided by API
            last_rpt = (
                obj.get("lst_rpt_at")
                or obj.get("lastReportedAt")
                or obj.get("last_reported_at")
            )
            if not last_rpt and data_ts is not None:
                try:
                    # Handle ISO string, seconds, or milliseconds epoch
                    if isinstance(data_ts, str):
                        if data_ts.endswith("Z[UTC]") or data_ts.endswith("Z"):
                            # Strip [UTC] if present; HA will display local time
                            s = data_ts.replace("[UTC]", "").replace("Z", "")
                            last_rpt = (
                                datetime.fromisoformat(s)
                                .replace(tzinfo=_tz.utc)
                                .isoformat()
                            )
                        elif data_ts.isdigit():
                            v = int(data_ts)
                            if v > 10**12:
                                v = v // 1000
                            last_rpt = datetime.fromtimestamp(v, tz=_tz.utc).isoformat()
                    elif isinstance(data_ts, (int, float)):
                        v = int(data_ts)
                        if v > 10**12:
                            v = v // 1000
                        last_rpt = datetime.fromtimestamp(v, tz=_tz.utc).isoformat()
                except Exception:
                    last_rpt = None

            # Commissioned key variations
            commissioned_val = obj.get("commissioned")
            if commissioned_val is None:
                commissioned_val = obj.get("isCommissioned") or conn0.get(
                    "commissioned"
                )

            connector_status = obj.get("connectorStatusType") or conn0.get(
                "connectorStatusType"
            )
            connector_status_info = conn0.get("connectorStatusInfo")
            connector_status_norm = None
            suspended_by_evse = False
            if isinstance(connector_status, str):
                connector_status_norm = connector_status.strip().upper()
            charging_now_flag = _as_bool(obj.get("charging"))
            if connector_status_norm:
                if connector_status_norm == SUSPENDED_EVSE_STATUS:
                    suspended_by_evse = True
                    charging_now_flag = False
                elif connector_status_norm in ACTIVE_CONNECTOR_STATUSES or any(
                    connector_status_norm.startswith(prefix)
                    for prefix in ACTIVE_SUSPENDED_PREFIXES
                ):
                    charging_now_flag = True
            actual_charging_flag = charging_now_flag
            self._record_actual_charging(sn, actual_charging_flag)
            pending_expectation = self._pending_charging.get(sn)
            if pending_expectation:
                target_state, expires_at = pending_expectation
                now_mono = time.monotonic()
                if actual_charging_flag == target_state or now_mono > expires_at:
                    self._pending_charging.pop(sn, None)
                else:
                    charging_now_flag = target_state

            # Charge mode: use cached/parallel fetch; fall back to derived values
            charge_mode_pref = charge_modes.get(sn)
            charge_mode = charge_mode_pref
            if not charge_mode:
                charge_mode = (
                    obj.get("chargeMode")
                    or obj.get("chargingMode")
                    or (obj.get("sch_d") or {}).get("mode")
                )
                if not charge_mode:
                    if charging_now_flag:
                        charge_mode = "IMMEDIATE"
                    elif sch_info0.get("type") or sch.get("status"):
                        charge_mode = str(
                            sch_info0.get("type") or sch.get("status")
                        ).upper()
                    else:
                        charge_mode = "IDLE"

            green_setting = green_settings.get(sn)
            green_enabled: bool | None = None
            green_supported: bool | None = None
            if green_setting is not None:
                green_enabled, green_supported = green_setting

            auth_setting = auth_settings.get(sn)
            app_auth_enabled: bool | None = None
            rfid_auth_enabled: bool | None = None
            app_auth_supported = False
            rfid_auth_supported = False
            auth_required: bool | None = None
            if auth_setting is not None:
                (
                    app_auth_enabled,
                    rfid_auth_enabled,
                    app_auth_supported,
                    rfid_auth_supported,
                ) = auth_setting
                if app_auth_supported or rfid_auth_supported:
                    values = [
                        value
                        for value in (app_auth_enabled, rfid_auth_enabled)
                        if value is not None
                    ]
                    if values:
                        auth_required = any(values)

            charge_mode_support, charge_mode_support_source = _support_value_and_source(
                (
                    True
                    if charge_mode_pref is not None or has_embedded_charge_mode
                    else None
                ),
                self.evse_feature_flag_enabled("evse_charging_mode", sn),
            )
            charging_amps_hint = self.evse_feature_flag_enabled(
                "max_current_config_support", sn
            )
            if charging_amps_hint is None:
                charging_amps_hint = self.evse_feature_flag_enabled(
                    "evse_charge_level_control", sn
                )
            charging_amps_support, charging_amps_support_source = (
                _support_value_and_source(
                    True if charging_level is not None else None,
                    charging_amps_hint,
                )
            )
            storm_guard_support, storm_guard_support_source = _support_value_and_source(
                (
                    True
                    if self._storm_guard_state is not None
                    or self._storm_evse_enabled is not None
                    else None
                ),
                self.evse_feature_flag_enabled("evse_storm_guard", sn),
            )
            auth_feature_support, auth_feature_support_source = (
                _support_value_and_source(
                    app_auth_supported if auth_setting is not None else None,
                    self.evse_feature_flag_enabled("evse_authentication", sn),
                )
            )
            rfid_feature_support, rfid_feature_support_source = (
                _support_value_and_source(
                    rfid_auth_supported if auth_setting is not None else None,
                    self.evse_feature_flag_enabled("iqevse_rfid", sn),
                )
            )
            plug_and_charge_support, plug_and_charge_support_source = (
                _support_value_and_source(
                    None,
                    self.evse_feature_flag_enabled("plug_and_charge", sn),
                )
            )

            # Determine a stable session end when not charging
            charging_now = charging_now_flag
            if (
                sn in self._last_charging
                and self._last_charging.get(sn)
                and not charging_now
            ):
                # Transition charging -> not charging: capture a fixed end time
                try:
                    if isinstance(data_ts, (int, float)) or (
                        isinstance(data_ts, str) and data_ts.isdigit()
                    ):
                        val = _sec(data_ts)
                        if val is not None:
                            self._session_end_fix[sn] = val
                        else:
                            self._session_end_fix[sn] = int(time.time())
                    else:
                        self._session_end_fix[sn] = int(time.time())
                except Exception:
                    self._session_end_fix[sn] = int(time.time())
            elif charging_now:
                # Clear fixed end when charging resumes
                self._session_end_fix.pop(sn, None)
            self._last_charging[sn] = charging_now

            session_end = None
            if not charging_now:
                # Prefer fixed end captured at stop; fall back to plug-out timestamp
                session_end = self._session_end_fix.get(sn)
                if session_end is None and sess.get("plg_out_at") is not None:
                    session_end = _sec(sess.get("plg_out_at"))

            # Session energy normalization: many deployments report Wh in e_c
            session_energy_wh = _as_float(sess.get("e_c"))
            ses_kwh = session_energy_wh
            if isinstance(ses_kwh, (int, float)):
                try:
                    if ses_kwh > 200:
                        ses_kwh = round(float(ses_kwh) / 1000.0, 2)
                    else:
                        ses_kwh = round(float(ses_kwh), 2)
                except Exception:
                    ses_kwh = session_energy_wh
            else:
                ses_kwh = sess.get("e_c")

            display_name = obj.get("displayName") or obj.get("name")
            if display_name is not None:
                try:
                    display_name = str(display_name)
                except Exception:
                    display_name = None
            session_charge_level = None
            for key in (
                "chargeLevel",
                "charge_level",
                "chargingLevel",
                "charging_level",
            ):
                raw_level = sess.get(key)
                if raw_level is not None:
                    session_charge_level = _as_int(raw_level)
                    break
            raw_miles = sess.get("miles")
            session_miles = _as_float(raw_miles, precision=3)
            if session_miles is None:
                session_miles = raw_miles

            session_cost = None
            for key in ("session_cost", "sessionCost"):
                session_cost = _as_float(sess.get(key), precision=3)
                if session_cost is not None:
                    break
            schedule_days = _as_int_list(sch_info0.get("days"))
            schedule_remind = _as_optional_bool(sch_info0.get("remindFlag"))
            if schedule_remind is None:
                schedule_remind = _as_optional_bool(sch_info0.get("reminderEnabled"))
            session_auth_status = _as_int(
                sess.get("auth_status")
                if sess.get("auth_status") is not None
                else sess.get("authStatus")
            )
            session_auth_type = _as_text(
                sess.get("auth_type")
                if sess.get("auth_type") is not None
                else sess.get("authType")
            )
            session_auth_identifier = _as_text(
                sess.get("auth_id")
                if sess.get("auth_id") is not None
                else sess.get("authIdentifier")
            )
            session_auth_token = _as_text(
                sess.get("auth_token")
                if sess.get("auth_token") is not None
                else sess.get("authToken")
            )

            entry = {
                "sn": sn,
                "name": obj.get("name"),
                "display_name": display_name,
                "connected": _as_bool(obj.get("connected")),
                "plugged": _as_bool(obj.get("pluggedIn")),
                "charging": charging_now_flag,
                "faulted": _as_bool(obj.get("faulted")),
                "connector_status": connector_status,
                "connector_reason": conn0.get("connectorStatusReason"),
                "connector_status_info": connector_status_info,
                "safe_limit_state": safe_limit_state,
                "dlb_active": (
                    _as_bool(conn0.get("dlbActive"))
                    if conn0.get("dlbActive") is not None
                    else None
                ),
                "suspended_by_evse": suspended_by_evse,
                "session_energy_wh": session_energy_wh,
                "session_kwh": ses_kwh,
                "session_miles": session_miles,
                # Normalize session start epoch if needed
                "session_start": _sec(
                    sess.get("start_time")
                    if sess.get("start_time") is not None
                    else sess.get("strt_chrg")
                ),
                "session_end": session_end,
                "session_plug_in_at": sess.get("plg_in_at"),
                "session_plug_out_at": sess.get("plg_out_at"),
                "session_auth_status": session_auth_status,
                "session_auth_type": session_auth_type,
                "session_auth_identifier": session_auth_identifier,
                "session_auth_token_present": bool(session_auth_token),
                "last_reported_at": last_rpt,
                "offline_since": obj.get("offlineAt"),
                "commissioned": _as_bool(commissioned_val),
                "schedule_status": sch.get("status"),
                "schedule_type": sch_info0.get("type") or sch.get("status"),
                "schedule_start": sch_info0.get("startTime"),
                "schedule_end": sch_info0.get("endTime"),
                "schedule_slot_id": _as_text(sch_info0.get("id")),
                "schedule_days": schedule_days,
                "schedule_reminder_enabled": schedule_remind,
                "schedule_reminder_min": _as_int(sch_info0.get("remindTime")),
                "charge_mode": charge_mode,
                # Expose scheduler preference explicitly for entities that care
                "charge_mode_pref": charge_mode_pref,
                "charging_level": charging_level,
                "storm_guard_state": self._storm_guard_state,
                "storm_evse_enabled": self._storm_evse_enabled,
                "session_charge_level": session_charge_level,
                "session_cost": session_cost,
                "off_grid_state": _as_text(obj.get("offGrid")),
                "ev_manufacturer_name": _as_text(obj.get("evManufacturerName")),
                "ev_details_set": _as_optional_bool(obj.get("isEVDetailsSet")),
                "smart_ev_has_token": _as_optional_bool(smart_ev.get("hasToken")),
                "smart_ev_has_details": _as_optional_bool(smart_ev.get("hasEVDetails")),
                "operating_v": self._operating_v.get(sn),
                "nominal_v": self._nominal_v,
                "charge_mode_supported": charge_mode_support,
                "charge_mode_supported_source": charge_mode_support_source,
                "charging_amps_supported": charging_amps_support,
                "charging_amps_supported_source": charging_amps_support_source,
                "storm_guard_supported": storm_guard_support,
                "storm_guard_supported_source": storm_guard_support_source,
                "auth_feature_supported": auth_feature_support,
                "auth_feature_supported_source": auth_feature_support_source,
                "rfid_feature_supported": rfid_feature_support,
                "rfid_feature_supported_source": rfid_feature_support_source,
                "plug_and_charge_supported": plug_and_charge_support,
                "plug_and_charge_supported_source": plug_and_charge_support_source,
            }
            if green_supported is not None:
                entry["green_battery_supported"] = green_supported
                if green_supported:
                    entry["green_battery_enabled"] = green_enabled
            if auth_setting is not None:
                entry["app_auth_supported"] = app_auth_supported
                entry["rfid_auth_supported"] = rfid_auth_supported
                entry["app_auth_enabled"] = app_auth_enabled
                entry["rfid_auth_enabled"] = rfid_auth_enabled
                entry["auth_required"] = auth_required

            out[sn] = entry

        self._sync_desired_charging(out)

        polling_state = self._determine_polling_state(out)
        summary_force = self.summary.prepare_refresh(
            want_fast=polling_state["want_fast"],
            target_interval=float(polling_state["target"]),
        )

        # Enrich with summary v2 data
        summary_start = time.monotonic()
        summary = await self.summary.async_fetch(force=summary_force)
        phase_timings["summary_s"] = round(time.monotonic() - summary_start, 3)
        if summary:
            for item in summary:
                sn = str(item.get("serialNumber") or "")
                if not sn:
                    continue
                self._ensure_serial_tracked(sn)
                cur = out.setdefault(sn, {})
                cur.setdefault("nominal_v", self._nominal_v)
                prev_sn = prev_data.get(sn) if isinstance(prev_data, dict) else None
                # Max current capability and phase/status
                cur["max_current"] = item.get("maxCurrent")
                cld = item.get("chargeLevelDetails") or {}
                try:
                    cur["min_amp"] = (
                        int(str(cld.get("min"))) if cld.get("min") is not None else None
                    )
                except Exception:
                    cur["min_amp"] = None
                try:
                    cur["max_amp"] = (
                        int(str(cld.get("max"))) if cld.get("max") is not None else None
                    )
                except Exception:
                    cur["max_amp"] = None
                try:
                    cur["amp_granularity"] = (
                        int(str(cld.get("granularity")))
                        if cld.get("granularity") is not None
                        else None
                    )
                except Exception:
                    cur["amp_granularity"] = None
                if any(
                    cur.get(key) is not None
                    for key in (
                        "charging_level",
                        "min_amp",
                        "max_amp",
                        "max_current",
                        "amp_granularity",
                    )
                ):
                    cur["charging_amps_supported"] = True
                    cur["charging_amps_supported_source"] = "runtime"
                cur["phase_mode"] = item.get("phaseMode")
                cur["status"] = item.get("status")
                supports_use_battery = _as_optional_bool(item.get("supportsUseBattery"))
                if supports_use_battery is not None:
                    cur["green_battery_supported"] = supports_use_battery
                    if not supports_use_battery:
                        cur.pop("green_battery_enabled", None)
                default_charge_level = cld.get("defaultChargeLevel")
                if default_charge_level is not None:
                    cur["default_charge_level"] = default_charge_level
                conn = item.get("activeConnection")
                if isinstance(conn, str):
                    conn = conn.strip()
                if conn:
                    cur["connection"] = conn
                net_cfg = item.get("networkConfig")
                ip_addr = None
                mac_addr = None
                interface_count = 0
                entries: list = []
                if isinstance(net_cfg, dict):
                    entries = [net_cfg]
                elif isinstance(net_cfg, list):
                    entries = net_cfg
                elif isinstance(net_cfg, str):
                    raw = net_cfg.strip()
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        parsed = []
                        raw_body = raw.strip("[]\n ")
                        for line in raw_body.splitlines():
                            line = line.strip().strip(",")
                            if line.startswith('"') and line.endswith('"'):
                                line = line[1:-1]
                            if line:
                                parsed.append(line)
                    entries = parsed if isinstance(parsed, list) else []
                for entry in entries:
                    parts: dict[str, object] = {}
                    if isinstance(entry, dict):
                        parts = entry
                    elif isinstance(entry, str):
                        for piece in entry.split(","):
                            if "=" in piece:
                                k, v = piece.split("=", 1)
                                parts[k.strip()] = v.strip()
                    if not parts:
                        continue
                    interface_count += 1
                    candidate = parts.get("ipaddr") or parts.get("ip")
                    candidate_mac = parts.get("mac_addr")
                    if candidate_mac is None:
                        candidate_mac = parts.get("mac")
                    if candidate_mac is not None and mac_addr is None:
                        mac_addr = candidate_mac
                    if candidate and ip_addr is None:
                        ip_addr = candidate
                    if candidate and str(parts.get("connectionStatus")) in (
                        "1",
                        "true",
                        "True",
                    ):
                        ip_addr = candidate
                        if candidate_mac is not None:
                            mac_addr = candidate_mac
                        break
                if isinstance(ip_addr, str) and not ip_addr:
                    ip_addr = None
                if isinstance(mac_addr, str) and not mac_addr:
                    mac_addr = None
                if ip_addr:
                    cur["ip_address"] = str(ip_addr)
                if mac_addr:
                    cur["mac_address"] = str(mac_addr)
                if interface_count > 0:
                    cur["network_interface_count"] = interface_count
                interval = item.get("reportingInterval")
                if interval is not None:
                    try:
                        cur["reporting_interval"] = int(str(interval))
                    except Exception:
                        pass
                if item.get("dlbEnabled") is not None:
                    cur["dlb_enabled"] = _as_bool(item.get("dlbEnabled"))
                # Commissioning: prefer explicit commissioningStatus from summary
                if item.get("commissioningStatus") is not None:
                    cur["commissioned"] = bool(item.get("commissioningStatus"))
                if item.get("commissioningStatus") is not None:
                    cur["commissioning_status"] = _as_int(
                        item.get("commissioningStatus")
                    )
                # Last reported: prefer summary if present
                if item.get("lastReportedAt"):
                    cur["last_reported_at"] = item.get("lastReportedAt")
                if item.get("timezone") is not None:
                    cur["charger_timezone"] = _as_text(item.get("timezone"))
                if item.get("isConnected") is not None:
                    cur["is_connected"] = _as_optional_bool(item.get("isConnected"))
                if item.get("isLocallyConnected") is not None:
                    cur["is_locally_connected"] = _as_optional_bool(
                        item.get("isLocallyConnected")
                    )
                if item.get("hoControl") is not None:
                    cur["ho_control"] = _as_optional_bool(item.get("hoControl"))
                if item.get("warrantyStartDate") is not None:
                    cur["warranty_start_date"] = item.get("warrantyStartDate")
                if item.get("warrantyDueDate") is not None:
                    cur["warranty_due_date"] = item.get("warrantyDueDate")
                if item.get("warrantyPeriod") is not None:
                    cur["warranty_period_years"] = _as_int(item.get("warrantyPeriod"))
                if item.get("createdAt") is not None:
                    cur["created_at"] = item.get("createdAt")
                if item.get("breakerRating") is not None:
                    cur["breaker_rating"] = _as_int(item.get("breakerRating"))
                if item.get("ratedCurrent") is not None:
                    cur["rated_current"] = _as_int(item.get("ratedCurrent"))
                if item.get("gridType") is not None:
                    cur["grid_type"] = _as_int(item.get("gridType"))
                if item.get("phaseCount") is not None:
                    cur["phase_count"] = _as_int(item.get("phaseCount"))
                if item.get("wifiConfig") is not None:
                    cur["wifi_config"] = _as_text(item.get("wifiConfig"))
                if item.get("cellularConfig") is not None:
                    cur["cellular_config"] = _as_text(item.get("cellularConfig"))
                if item.get("defaultRoute") is not None:
                    cur["default_route"] = _as_text(item.get("defaultRoute"))
                if item.get("wiringConfiguration") is not None:
                    cur["wiring_configuration"] = item.get("wiringConfiguration")
                fval = item.get("functionalValDetails")
                if isinstance(fval, dict):
                    if fval.get("state") is not None:
                        cur["functional_validation_state"] = _as_int(fval.get("state"))
                    if fval.get("lastUpdatedTimestamp") is not None:
                        cur["functional_validation_updated_at"] = _sec(
                            fval.get("lastUpdatedTimestamp")
                        )
                gateway_connectivity = item.get("gatewayConnectivityDetails")
                if isinstance(gateway_connectivity, list):
                    cur["gateway_connection_count"] = len(gateway_connectivity)
                    connected_count = 0
                    for gateway in gateway_connectivity:
                        if not isinstance(gateway, dict):
                            continue
                        if _as_int(gateway.get("gwConnStatus")) == 0:
                            connected_count += 1
                    cur["gateway_connected_count"] = connected_count
                # Capture operating voltage for better power estimation
                ov = item.get("operatingVoltage")
                if ov is not None:
                    try:
                        self._operating_v[sn] = int(round(float(str(ov))))
                    except Exception:
                        pass
                # Expose operating voltage in the mapped data when known
                if self._operating_v.get(sn) is not None:
                    cur["operating_v"] = self._operating_v.get(sn)
                # Lifetime energy for Energy Dashboard (kWh) with glitch guard
                if item.get("lifeTimeConsumption") is not None:
                    filtered = self.energy._apply_lifetime_guard(
                        sn,
                        item.get("lifeTimeConsumption"),
                        prev_sn,
                    )
                    if filtered is not None:
                        cur["lifetime_kwh"] = filtered
                for key_src, key_dst in (
                    ("firmwareVersion", "firmware_version"),
                    ("systemVersion", "system_version"),
                    ("applicationVersion", "application_version"),
                    ("processorBoardVersion", "processor_board_version"),
                    ("powerBoardVersion", "power_board_version"),
                    ("kernelVersion", "kernel_version"),
                    ("bootloaderVersion", "bootloader_version"),
                ):
                    val = item.get(key_src)
                    if val is not None and key_dst not in cur:
                        cur[key_dst] = val
                # Optional device metadata if provided by summary v2
                for key_src, key_dst in (
                    ("firmwareVersion", "sw_version"),
                    ("systemVersion", "sw_version"),
                    ("applicationVersion", "sw_version"),
                    ("softwareVersion", "sw_version"),
                    ("processorBoardVersion", "hw_version"),
                    ("powerBoardVersion", "hw_version"),
                    ("hwVersion", "hw_version"),
                    ("hardwareVersion", "hw_version"),
                    ("modelId", "model_id"),
                    ("sku", "model_id"),
                    ("model", "model_name"),
                    ("modelName", "model_name"),
                    ("partNumber", "part_number"),
                    ("kernelVersion", "kernel_version"),
                    ("bootloaderVersion", "bootloader_version"),
                ):
                    val = item.get(key_src)
                    if val is not None and key_dst not in cur:
                        cur[key_dst] = val
                # Prefer displayName from summary v2 for user-facing names
                if item.get("displayName"):
                    cur["display_name"] = str(item.get("displayName"))
            self._seed_nominal_voltage_option_from_api()
        # Attach session history using cached data, deferring expensive fetches when possible
        sessions_start = time.monotonic()
        try:
            day_ref = dt_util.now()
        except Exception:
            day_ref = datetime.now(tz=_tz.utc)
        try:
            day_local_default = dt_util.as_local(day_ref)
        except Exception:
            if day_ref.tzinfo is None:
                day_ref = day_ref.replace(tzinfo=_tz.utc)
            day_local_default = dt_util.as_local(day_ref)

        now_mono = time.monotonic()
        immediate_by_day: dict[str, list[str]] = {}
        background_by_day: dict[str, list[str]] = {}
        day_locals: dict[str, datetime] = {}
        for sn, cur in out.items():
            history_day = self._session_history_day(cur, day_local_default)
            day_key = history_day.strftime("%Y-%m-%d")
            day_locals.setdefault(day_key, history_day)
            view = self.session_history.get_cache_view(sn, day_key, now_mono)
            sessions_cached = view.sessions or []
            cur["energy_today_sessions"] = sessions_cached
            cur["energy_today_sessions_kwh"] = self._sum_session_energy(sessions_cached)
            if not view.needs_refresh or view.blocked:
                continue
            target = background_by_day if first_refresh else immediate_by_day
            target.setdefault(day_key, []).append(sn)
        # Prune after day-keys are known so historical session-day entries in use
        # by current chargers are retained for normal TTL behavior.
        self._prune_runtime_caches(active_serials=out.keys(), keep_day_keys=day_locals)

        for day_key, serials in immediate_by_day.items():
            updates = await self._async_enrich_sessions(
                serials,
                day_locals.get(day_key, day_local_default),
                in_background=False,
            )
            for sn, sessions in updates.items():
                cur = out.get(sn)
                if cur is None:
                    continue
                cur["energy_today_sessions"] = sessions
                cur["energy_today_sessions_kwh"] = self._sum_session_energy(sessions)
        for day_key, serials in background_by_day.items():
            self._schedule_session_enrichment(
                serials, day_locals.get(day_key, day_local_default)
            )
        phase_timings["sessions_s"] = round(time.monotonic() - sessions_start, 3)
        self._sync_session_history_issue()

        if not first_refresh:
            await self._async_run_refresh_calls(
                phase_timings,
                defer_topology=True,
                calls=(
                    (
                        "evse_timeseries_s",
                        "EVSE timeseries",
                        lambda: self.evse_timeseries.async_refresh(
                            day_local=day_local_default
                        ),
                    ),
                    (
                        "site_energy_s",
                        "site energy",
                        lambda: self.energy._async_refresh_site_energy(),
                    ),
                    (
                        "inverters_s",
                        "inverters",
                        lambda: self._async_refresh_inverters(),
                    ),
                ),
            )
            try:
                self.evse_timeseries.merge_charger_payloads(
                    out, day_local=day_local_default
                )
            except Exception:
                pass
            self._sync_site_energy_discovery_state()
            self._sync_site_energy_issue()
            self._sync_battery_profile_pending_issue()
            heatpump_power_start = time.monotonic()
            try:
                await self._async_refresh_heatpump_power()
            except Exception:  # noqa: BLE001
                pass
            phase_timings["heatpump_power_s"] = round(
                time.monotonic() - heatpump_power_start, 3
            )

        # Dynamic poll rate: fast while any charging, within a fast window, or streaming
        if self.config_entry is not None:
            target = polling_state["target"]
            if (
                not self.update_interval
                or int(self.update_interval.total_seconds()) != target
            ):
                new_interval = timedelta(seconds=target)
                self.update_interval = new_interval
                # Older cores require async_set_update_interval for dynamic changes
                if hasattr(self, "async_set_update_interval"):
                    try:
                        self.async_set_update_interval(new_interval)
                    except Exception:
                        pass

        phase_timings["total_s"] = round(time.monotonic() - t0, 3)
        self._phase_timings = phase_timings
        if first_refresh:
            self._bootstrap_phase_timings = phase_timings.copy()
        self._refresh_cached_topology()
        self._schedule_discovery_snapshot_save()
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Coordinator refresh timings for site %s: %s",
                redact_site_id(self.site_id),
                phase_timings,
            )

        return out

    def _sync_desired_charging(self, data: dict[str, dict]) -> None:
        """Align desired charging state with backend data and auto-resume when needed."""
        if not data:
            return
        now = time.monotonic()
        for sn, info in data.items():
            sn_str = str(sn)
            charging = bool(info.get("charging"))
            desired = self._desired_charging.get(sn_str)
            if desired is None:
                self._desired_charging[sn_str] = charging
                desired = charging
            if charging:
                self._auto_resume_attempts.pop(sn_str, None)
                continue
            if not desired:
                continue
            if not info.get("plugged"):
                continue
            status_raw = info.get("connector_status")
            status_norm = ""
            if isinstance(status_raw, str):
                status_norm = status_raw.strip().upper()
            if status_norm != SUSPENDED_EVSE_STATUS:
                continue
            mode_raw = info.get("charge_mode_pref") or info.get("charge_mode")
            mode = ""
            if mode_raw is not None:
                try:
                    mode = str(mode_raw).strip().upper()
                except Exception:
                    mode = ""
            if mode == "GREEN_CHARGING":
                _LOGGER.debug(
                    "Skipping auto-resume for charger %s because mode is GREEN_CHARGING",
                    redact_identifier(sn_str),
                )
                continue
            last_attempt = self._auto_resume_attempts.get(sn_str)
            if last_attempt is not None and (now - last_attempt) < 120:
                continue
            self._auto_resume_attempts[sn_str] = now
            _LOGGER.debug(
                "Scheduling auto-resume for charger %s after connector reported %s",
                redact_identifier(sn_str),
                status_norm or "unknown",
            )
            snapshot = dict(info)
            task_name = f"enphase_ev_auto_resume_{sn_str}"
            try:
                self.hass.async_create_task(
                    self._async_auto_resume(sn_str, snapshot),
                    name=task_name,
                )
            except TypeError:
                # Older cores do not support the name kwarg
                self.hass.async_create_task(self._async_auto_resume(sn_str, snapshot))

    async def _async_auto_resume(self, sn: str, snapshot: dict | None = None) -> None:
        """Attempt to resume charging automatically after a cloud-side suspension."""
        sn_str = str(sn)
        try:
            current = (self.data or {}).get(sn_str, {})
        except Exception:  # noqa: BLE001
            current = {}
        plugged_snapshot = None
        if isinstance(snapshot, dict):
            plugged_snapshot = snapshot.get("plugged")
        plugged = (
            plugged_snapshot if plugged_snapshot is not None else current.get("plugged")
        )
        if not plugged:
            _LOGGER.debug(
                "Auto-resume aborted for charger %s because it is not plugged in",
                redact_identifier(sn_str),
            )
            return
        amps = self.pick_start_amps(sn_str)
        prefs = self._charge_mode_start_preferences(sn_str)
        try:
            result = await self.client.start_charging(
                sn_str,
                amps,
                include_level=prefs.include_level,
                strict_preference=prefs.strict,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Auto-resume start_charging failed for charger %s: %s",
                redact_identifier(sn_str),
                redact_text(
                    err,
                    site_ids=(self.site_id,),
                    identifiers=(sn_str,),
                ),
            )
            return
        self.set_last_set_amps(sn_str, amps)
        if isinstance(result, dict) and result.get("status") == "not_ready":
            _LOGGER.debug(
                "Auto-resume start_charging for charger %s returned not_ready; will retry later",
                redact_identifier(sn_str),
            )
            return
        if prefs.enforce_mode:
            await self._ensure_charge_mode(sn_str, prefs.enforce_mode)
        _LOGGER.info(
            "Auto-resume start_charging issued for charger %s after suspension",
            redact_identifier(sn_str),
        )
        self.set_charging_expectation(sn_str, True, hold_for=120)
        self.kick_fast(120)
        await self.async_request_refresh()

    def _determine_polling_state(self, data: dict[str, dict]) -> dict[str, object]:
        charging_now = any(v.get("charging") for v in data.values()) if data else False
        want_fast = charging_now
        now_mono = time.monotonic()
        if self._fast_until and now_mono < self._fast_until:
            want_fast = True
        fast_stream_enabled = True
        if self.config_entry is not None:
            try:
                fast_stream_enabled = bool(
                    self.config_entry.options.get(OPT_FAST_WHILE_STREAMING, True)
                )
            except Exception:
                fast_stream_enabled = True
        if self._streaming_active() and fast_stream_enabled:
            want_fast = True
        fast_opt = None
        if self.config_entry is not None:
            fast_opt = self.config_entry.options.get(OPT_FAST_POLL_INTERVAL)
        fast_configured = fast_opt is not None
        try:
            fast = int(fast_opt) if fast_opt is not None else DEFAULT_FAST_POLL_INTERVAL
        except Exception:
            fast = DEFAULT_FAST_POLL_INTERVAL
            fast_configured = False
        fast = max(1, fast)
        slow_default = getattr(
            self,
            "_configured_slow_poll_interval",
            DEFAULT_SCAN_INTERVAL,
        )
        slow_opt = None
        if self.config_entry is not None:
            slow_opt = self.config_entry.options.get(OPT_SLOW_POLL_INTERVAL)
        try:
            if slow_opt is not None:
                slow = int(slow_opt)
            else:
                slow = int(slow_default)
        except Exception:
            slow = int(slow_default)
        slow = max(1, slow)
        target = slow
        if want_fast:
            target = fast
        return {
            "charging_now": charging_now,
            "want_fast": want_fast,
            "fast": fast,
            "slow": slow,
            "target": target,
            "fast_configured": fast_configured,
        }

    async def _async_resolve_charge_modes(
        self, serials: Iterable[str]
    ) -> dict[str, str | None]:
        """Resolve charge modes concurrently for the provided serial numbers."""
        results: dict[str, str | None] = {}
        pending: dict[str, asyncio.Task[str | None]] = {}
        now = time.monotonic()
        if self._scheduler_backoff_active():
            for sn in dict.fromkeys(serials):
                if not sn:
                    continue
                cached = self._charge_mode_cache.get(sn)
                if cached and (now - cached[1] < 300):
                    results[sn] = cached[0]
            return results
        for sn in dict.fromkeys(serials):
            if not sn:
                continue
            cached = self._charge_mode_cache.get(sn)
            if cached and (now - cached[1] < 300):
                results[sn] = cached[0]
                continue
            pending[sn] = asyncio.create_task(self._get_charge_mode(sn))

        if pending:
            responses = await asyncio.gather(*pending.values(), return_exceptions=True)
            for sn, response in zip(pending.keys(), responses, strict=False):
                if isinstance(response, Exception):
                    _LOGGER.debug(
                        "Charge mode lookup failed for %s: %s",
                        redact_identifier(sn),
                        redact_text(
                            response,
                            site_ids=(self.site_id,),
                            identifiers=(sn,),
                        ),
                    )
                    continue
                if response:
                    results[sn] = response

        return results

    def _has_embedded_charge_mode(self, obj: dict) -> bool:
        """Check whether the status payload already exposes a charge mode."""
        if not isinstance(obj, dict):
            return False
        for key in ("chargeMode", "chargingMode", "charge_mode"):
            val = obj.get(key)
            if val is not None:
                return True
        sch = obj.get("sch_d")
        if isinstance(sch, dict):
            if sch.get("mode") or sch.get("status"):
                return True
            info = sch.get("info")
            if isinstance(info, list):
                for entry in info:
                    if isinstance(entry, dict) and (
                        entry.get("type") or entry.get("mode") or entry.get("status")
                    ):
                        return True
        return False

    async def _attempt_auto_refresh(self) -> bool:
        """Attempt to refresh authentication using stored credentials."""
        if not self._email or not self._remember_password or not self._stored_password:
            return False

        async with self._refresh_lock:
            session = async_get_clientsession(self.hass)
            try:
                tokens, _ = await async_authenticate(
                    session, self._email, self._stored_password
                )
            except EnlightenAuthInvalidCredentials:
                _LOGGER.warning(
                    "Stored Enlighten credentials were rejected; reauthenticate via the integration options"
                )
                return False
            except EnlightenAuthMFARequired:
                _LOGGER.warning(
                    "Enphase account requires multi-factor authentication; complete MFA in the browser and reauthenticate"
                )
                return False
            except EnlightenAuthUnavailable:
                _LOGGER.debug(
                    "Auth service unavailable while refreshing tokens; will retry later"
                )
                return False
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Unexpected error refreshing Enlighten auth: %s",
                    redact_text(err),
                )
                return False

            self._tokens = tokens
            self.client.update_credentials(
                eauth=tokens.access_token,
                cookie=tokens.cookie,
            )
            self._persist_tokens(tokens)
            return True

    async def _handle_client_unauthorized(self) -> bool:
        """Handle client Unauthorized responses and retry when possible."""

        self._last_error = "unauthorized"
        self._unauth_errors += 1
        if await self._attempt_auto_refresh():
            self._unauth_errors = 0
            ir.async_delete_issue(self.hass, DOMAIN, "reauth_required")
            return True

        if self._unauth_errors >= 2:
            metrics, placeholders = self._issue_context()
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "reauth_required",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="reauth_required",
                translation_placeholders=placeholders,
                data={"site_metrics": metrics},
            )

        raise ConfigEntryAuthFailed

    async def async_start_charging(
        self,
        sn: str,
        *,
        requested_amps: int | float | str | None = None,
        connector_id: int | None = 1,
        hold_seconds: float = 90.0,
        allow_unplugged: bool = False,
        fallback_amps: int | float | str | None = None,
    ) -> object:
        """Start charging with coordinator safeguards and auth retry."""
        sn_str = str(sn)
        if not allow_unplugged:
            self.require_plugged(sn_str)
        try:
            data = (self.data or {}).get(sn_str, {})
        except Exception:
            data = {}
        if data.get("auth_required") is True:
            display = data.get("display_name") or data.get("name") or sn_str
            _LOGGER.warning(
                "Start charging requested for %s but session authentication is required; "
                "charging will begin after app/RFID auth completes.",
                redact_identifier(display),
            )
        fallback = fallback_amps if fallback_amps is not None else 32
        amps = self.pick_start_amps(sn_str, requested_amps, fallback=fallback)
        connector = connector_id if connector_id is not None else 1
        prefs = self._charge_mode_start_preferences(sn_str)

        result = await self.client.start_charging(
            sn_str,
            amps,
            connector,
            include_level=prefs.include_level,
            strict_preference=prefs.strict,
        )
        self.set_last_set_amps(sn_str, amps)
        if isinstance(result, dict) and result.get("status") == "not_ready":
            self.set_desired_charging(sn_str, False)
            return result

        await self.async_start_streaming(
            manual=False, serial=sn_str, expected_state=True
        )
        self.set_desired_charging(sn_str, True)
        self.set_charging_expectation(sn_str, True, hold_for=hold_seconds)
        self.kick_fast(int(hold_seconds))
        if prefs.enforce_mode:
            await self._ensure_charge_mode(sn_str, prefs.enforce_mode)
        await self.async_request_refresh()
        return result

    async def async_stop_charging(
        self,
        sn: str,
        *,
        hold_seconds: float = 90.0,
        fast_seconds: int = 60,
        allow_unplugged: bool = True,
    ) -> object:
        """Stop charging with coordinator safeguards and auth retry."""
        sn_str = str(sn)
        prefs = self._charge_mode_start_preferences(sn_str)
        if not allow_unplugged:
            self.require_plugged(sn_str)

        result = await self.client.stop_charging(sn_str)
        await self.async_start_streaming(
            manual=False, serial=sn_str, expected_state=False
        )
        self.set_desired_charging(sn_str, False)
        self.set_charging_expectation(sn_str, False, hold_for=hold_seconds)
        self.kick_fast(fast_seconds)
        if prefs.enforce_mode == "SCHEDULED_CHARGING":
            await self._ensure_charge_mode(sn_str, prefs.enforce_mode)
        await self.async_request_refresh()
        return result

    def schedule_amp_restart(self, sn: str, delay: float = AMP_RESTART_DELAY_S) -> None:
        """Stop an active session and restart with the new amps after a delay."""
        sn_str = str(sn)
        existing = self._amp_restart_tasks.pop(sn_str, None)
        if existing and not existing.done():
            existing.cancel()
        try:
            task = self.hass.async_create_task(
                self._async_restart_after_amp_change(sn_str, delay),
                name=f"enphase_ev_amp_restart_{sn_str}",
            )
        except TypeError:
            task = self.hass.async_create_task(
                self._async_restart_after_amp_change(sn_str, delay)
            )
        self._amp_restart_tasks[sn_str] = task

        def _cleanup(_):
            stored = self._amp_restart_tasks.get(sn_str)
            if stored is task:
                self._amp_restart_tasks.pop(sn_str, None)

        task.add_done_callback(_cleanup)

    async def _async_restart_after_amp_change(self, sn: str, delay: float) -> None:
        """Stop, wait, and restart charging so the new amps apply immediately."""
        sn_str = str(sn)
        try:
            delay_s = max(0.0, float(delay))
        except Exception:  # noqa: BLE001
            delay_s = AMP_RESTART_DELAY_S

        fast_seconds = max(60, int(delay_s) if delay_s else 60)
        stop_hold = max(90.0, delay_s)

        try:
            await self.async_stop_charging(
                sn_str,
                hold_seconds=stop_hold,
                fast_seconds=fast_seconds,
                allow_unplugged=True,
            )
        except asyncio.CancelledError:  # pragma: no cover - task cancellation path
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Amp restart stop failed for charger %s: %s",
                redact_identifier(sn_str),
                redact_text(
                    err,
                    site_ids=(self.site_id,),
                    identifiers=(sn_str,),
                ),
            )
            return

        if delay_s:
            try:
                await asyncio.sleep(delay_s)
            except asyncio.CancelledError:  # pragma: no cover - task cancellation path
                raise
            except Exception:  # noqa: BLE001
                return

        try:
            await self.async_start_charging(sn_str)
        except asyncio.CancelledError:  # pragma: no cover - task cancellation path
            raise
        except ServiceValidationError as err:
            reason = "validation error"
            key = getattr(err, "translation_key", "") or ""
            if "charger_not_plugged" in key:
                reason = "not plugged in"
            elif "auth_required" in key:
                reason = "authentication required"
            _LOGGER.debug(
                "Amp restart aborted for charger %s because %s",
                redact_identifier(sn_str),
                reason,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Amp restart start_charging failed for charger %s: %s",
                redact_identifier(sn_str),
                redact_text(
                    err,
                    site_ids=(self.site_id,),
                    identifiers=(sn_str,),
                ),
            )

    async def async_trigger_ocpp_message(self, sn: str, message: str) -> object:
        """Trigger an OCPP message with auth retry and fast follow-up poll."""
        sn_str = str(sn)

        result = await self.client.trigger_message(sn_str, message)
        self.kick_fast(60)
        await self.async_request_refresh()
        return result

    def _persist_tokens(self, tokens: AuthTokens) -> None:
        if not self.config_entry:
            return
        merged = dict(self.config_entry.data)
        updates = {
            CONF_COOKIE: tokens.cookie or "",
            CONF_EAUTH: tokens.access_token,
            CONF_ACCESS_TOKEN: tokens.access_token,
            CONF_SESSION_ID: tokens.session_id,
            CONF_TOKEN_EXPIRES_AT: tokens.token_expires_at,
        }
        for key, value in updates.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        self.hass.config_entries.async_update_entry(self.config_entry, data=merged)

    def kick_fast(self, seconds: int = 60) -> None:
        """Force fast polling for a short window after user actions."""
        try:
            sec = int(seconds)
        except Exception:
            sec = 60
        self._fast_until = time.monotonic() + max(1, sec)

    def _streaming_active(self) -> bool:
        """Return whether a live stream is currently active."""
        if not self._streaming:
            return False
        if self._streaming_until is None:
            return True
        now = time.monotonic()
        if now >= self._streaming_until:
            self._clear_streaming_state()
            return False
        return True

    def _clear_streaming_state(self) -> None:
        """Reset live streaming flags."""
        self._streaming = False
        self._streaming_until = None
        self._streaming_manual = False
        self._streaming_targets.clear()

    def _streaming_response_ok(self, response: object) -> bool:
        if not isinstance(response, dict):
            return True
        status = response.get("status")
        if status is None:
            return True
        status_norm = str(status).strip().lower()
        return status_norm in ("accepted", "ok", "success")

    def _streaming_duration_s(self, response: object) -> float:
        duration = STREAMING_DEFAULT_DURATION_S
        if isinstance(response, dict):
            raw = response.get("duration_s")
            if raw is not None:
                try:
                    duration = float(raw)
                except Exception:
                    duration = STREAMING_DEFAULT_DURATION_S
        return max(1.0, duration)

    async def async_start_streaming(
        self,
        *,
        manual: bool = False,
        serial: str | None = None,
        expected_state: bool | None = None,
    ) -> None:
        """Request a live stream and track any follow-up expectations."""
        was_active = self._streaming_active()
        if not manual and self._streaming_manual:
            return
        response = None
        start_ok = False
        try:
            response = await self.client.start_live_stream()
        except Exception as err:  # noqa: BLE001
            if not was_active:
                _LOGGER.debug("Live stream start failed: %s", redact_text(err))
                return
        else:
            start_ok = self._streaming_response_ok(response)
            if not start_ok and not was_active:
                _LOGGER.debug(
                    "Live stream start rejected: %s",
                    redact_text(response, site_ids=(self.site_id,)),
                )
                return

        if start_ok:
            duration = self._streaming_duration_s(response)
            self._streaming = True
            self._streaming_until = time.monotonic() + duration

        if manual:
            self._streaming_manual = True
            self._streaming_targets.clear()
        else:
            if (self._streaming_active() or was_active) and serial is not None:
                if expected_state is not None:
                    self._streaming_targets[str(serial)] = bool(expected_state)

    async def async_stop_streaming(self, *, manual: bool = False) -> None:
        """Stop the live stream and clear streaming flags."""
        active = self._streaming_active()
        if not manual and self._streaming_manual:
            return
        if not manual and not active:
            return
        try:
            await self.client.stop_live_stream()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Live stream stop failed: %s", redact_text(err))
        self._clear_streaming_state()

    def _schedule_stream_stop(self, *, force: bool = False) -> None:
        existing = self._streaming_stop_task
        if existing and not existing.done():
            return

        async def _runner() -> None:
            if force:
                try:
                    await self.client.stop_live_stream()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Live stream stop failed: %s", redact_text(err))
                self._clear_streaming_state()
            else:
                await self.async_stop_streaming()

        try:
            task = self.hass.async_create_task(_runner(), name="enphase_ev_stop_stream")
        except TypeError:
            task = self.hass.async_create_task(_runner())
        self._streaming_stop_task = task

        def _cleanup(_task: asyncio.Task) -> None:
            if self._streaming_stop_task is _task:
                self._streaming_stop_task = None

        task.add_done_callback(_cleanup)

    def _record_actual_charging(self, sn: str, charging: bool | None) -> None:
        """Track raw charging transitions to extend fast polling on toggles."""
        sn_str = str(sn)
        if charging is None:
            self._last_actual_charging.pop(sn_str, None)
            return
        previous = self._last_actual_charging.get(sn_str)
        if previous is not None and previous != charging:
            self.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        self._last_actual_charging[sn_str] = charging
        if not self._streaming_manual and self._streaming_active():
            expected = self._streaming_targets.get(sn_str)
            if expected is not None and charging == expected:
                self._streaming_targets.pop(sn_str, None)
                if not self._streaming_targets:
                    self._streaming = False
                    self._streaming_until = None
                    self._schedule_stream_stop(force=True)

    def set_charging_expectation(
        self,
        sn: str,
        should_charge: bool,
        hold_for: float = 90.0,
    ) -> None:
        """Temporarily pin the reported charging state while waiting for cloud updates."""
        sn_str = str(sn)
        try:
            hold = float(hold_for)
        except Exception:
            hold = 90.0
        if hold <= 0:
            self._pending_charging.pop(sn_str, None)
            return
        expires = time.monotonic() + hold
        self._pending_charging[sn_str] = (bool(should_charge), expires)

    def _slow_interval_floor(self) -> int:
        slow_floor = DEFAULT_SLOW_POLL_INTERVAL
        if self.config_entry is not None:
            try:
                slow_opt = self.config_entry.options.get(
                    OPT_SLOW_POLL_INTERVAL, DEFAULT_SLOW_POLL_INTERVAL
                )
                slow_floor = max(slow_floor, int(slow_opt))
            except Exception:
                slow_floor = max(slow_floor, DEFAULT_SLOW_POLL_INTERVAL)
        if self.update_interval:
            try:
                slow_floor = max(slow_floor, int(self.update_interval.total_seconds()))
            except Exception:
                pass
        return max(1, slow_floor)

    def _clear_backoff_timer(self) -> None:
        if self._backoff_cancel:
            try:
                self._backoff_cancel()
            except Exception:
                pass
            self._backoff_cancel = None
        self.backoff_ends_utc = None

    def _scheduler_backoff_active(self) -> bool:
        """Return True when scheduler requests are in backoff."""
        backoff_until = getattr(self, "_scheduler_backoff_until", None)
        return bool(backoff_until and time.monotonic() < backoff_until)

    def scheduler_backoff_active(self) -> bool:
        """Public wrapper for scheduler backoff state."""

        return self._scheduler_backoff_active()

    def _scheduler_backoff_delay(self) -> float:
        slow_floor = float(self._slow_interval_floor())
        backoff_multiplier = 2 ** min(self._scheduler_failures - 1, 3)
        return max(30.0, min(600.0, slow_floor * backoff_multiplier))

    @property
    def scheduler_available(self) -> bool:
        """Return True when scheduler-dependent features are usable."""
        return bool(
            getattr(self, "_scheduler_available", True)
            and not self._scheduler_backoff_active()
        )

    @property
    def scheduler_last_error(self) -> str | None:
        return getattr(self, "_scheduler_last_error", None)

    def _mark_scheduler_available(self) -> None:
        if getattr(self, "_scheduler_available", True) and not getattr(
            self, "_scheduler_issue_reported", False
        ):
            return
        self._scheduler_available = True
        self._scheduler_failures = 0
        self._scheduler_last_error = None
        self._scheduler_last_failure_utc = None
        self._scheduler_backoff_until = None
        self._scheduler_backoff_ends_utc = None
        if getattr(self, "_scheduler_issue_reported", False):
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_SCHEDULER_UNAVAILABLE)
            self._scheduler_issue_reported = False

    def mark_scheduler_available(self) -> None:
        """Public wrapper used by entities and helper managers."""

        self._mark_scheduler_available()

    def _note_scheduler_unavailable(
        self,
        err: Exception | str | None = None,
        *,
        status: int | None = None,
        raw_payload: str | None = None,
    ) -> None:
        """Record scheduler outage and raise a repair issue."""
        reason = redact_text(err, site_ids=(self.site_id,)) if err else ""
        if not reason:
            reason = "Scheduler unavailable"
        self._scheduler_available = False
        self._scheduler_failures += 1
        self._scheduler_last_error = reason
        self._scheduler_last_failure_utc = dt_util.utcnow()
        delay = self._scheduler_backoff_delay()
        self._scheduler_backoff_until = time.monotonic() + delay
        try:
            self._scheduler_backoff_ends_utc = dt_util.utcnow() + timedelta(
                seconds=delay
            )
        except Exception:
            self._scheduler_backoff_ends_utc = None
        self._last_error = reason
        self.last_failure_utc = self._scheduler_last_failure_utc
        self.last_failure_status = status
        self.last_failure_description = "Scheduler unavailable"
        self.last_failure_response = (
            redact_text(raw_payload, site_ids=(self.site_id,))
            if raw_payload
            else reason
        )
        self.last_failure_source = "scheduler"
        if not self._scheduler_issue_reported:
            metrics, placeholders = self._issue_context()
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_SCHEDULER_UNAVAILABLE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_SCHEDULER_UNAVAILABLE,
                translation_placeholders=placeholders,
                data={"site_metrics": metrics},
            )
            self._scheduler_issue_reported = True

    def note_scheduler_unavailable(
        self,
        err: Exception | str | None = None,
        *,
        status: int | None = None,
        raw_payload: str | None = None,
    ) -> None:
        """Public wrapper used by entities and helper managers."""

        self._note_scheduler_unavailable(err, status=status, raw_payload=raw_payload)

    def _auth_settings_backoff_active(self) -> bool:
        """Return True when auth settings requests are in backoff."""
        backoff_until = getattr(self, "_auth_settings_backoff_until", None)
        return bool(backoff_until and time.monotonic() < backoff_until)

    def _auth_settings_backoff_delay(self) -> float:
        slow_floor = float(self._slow_interval_floor())
        backoff_multiplier = 2 ** min(self._auth_settings_failures - 1, 3)
        return max(30.0, min(600.0, slow_floor * backoff_multiplier))

    @property
    def auth_settings_available(self) -> bool:
        """Return True when auth settings features are usable."""
        return bool(
            getattr(self, "_auth_settings_available", True)
            and not self._auth_settings_backoff_active()
        )

    @property
    def auth_settings_last_error(self) -> str | None:
        return getattr(self, "_auth_settings_last_error", None)

    @staticmethod
    def _normalize_battery_profile_key(value: object) -> str | None:
        if value is None:
            return None
        try:
            normalized = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        return normalized or None

    @staticmethod
    def _battery_profile_label(profile: str | None) -> str | None:
        if not profile:
            return None
        if profile in BATTERY_PROFILE_LABELS:
            return BATTERY_PROFILE_LABELS[profile]
        try:
            return str(profile).replace("_", " ").replace("-", " ").title()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_battery_sub_type(value: object) -> str | None:
        if value is None:
            return None
        try:
            normalized = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        return normalized or None

    @staticmethod
    def _coerce_optional_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _coerce_optional_float(value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except Exception:  # noqa: BLE001
                return None
        if isinstance(value, str):
            try:
                cleaned = value.strip().replace(",", "")
            except Exception:  # noqa: BLE001
                return None
            if not cleaned:
                return None
            try:
                return float(cleaned)
            except Exception:  # noqa: BLE001
                return None
        return None

    @staticmethod
    def _coerce_optional_kwh(value: object) -> float | None:
        coerced = EnphaseCoordinator._coerce_optional_float(value)
        if coerced is None:
            return None
        try:
            return round(coerced, 2)
        except Exception:  # noqa: BLE001
            return coerced

    @staticmethod
    def _coerce_optional_text(value: object) -> str | None:
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @staticmethod
    def _normalize_battery_grid_mode(value: object) -> str | None:
        if value is None:
            return None
        try:
            raw = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        return raw

    @staticmethod
    def _battery_grid_mode_key(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            normalized = str(value).strip().lower().replace("-", "").replace("_", "")
        except Exception:  # noqa: BLE001
            return None
        return normalized or None

    @staticmethod
    def _battery_grid_mode_label(mode: str | None) -> str | None:
        if not mode:
            return None
        key = EnphaseCoordinator._battery_grid_mode_key(mode)
        if key in BATTERY_GRID_MODE_LABELS:
            return BATTERY_GRID_MODE_LABELS[key]
        try:
            return (
                str(mode).replace("_", " ").replace("-", " ").replace("  ", " ").title()
            )
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_minutes_of_day(value: object) -> int | None:
        if value is None:
            return None
        try:
            minutes = int(str(value).strip())
        except Exception:  # noqa: BLE001
            return None
        if minutes < 0 or minutes >= 24 * 60:
            return None
        return minutes

    @staticmethod
    def _minutes_of_day_to_time(value: int | None) -> dt_time | None:
        if value is None:
            return None
        normalized = EnphaseCoordinator._normalize_minutes_of_day(value)
        if normalized is None:
            return None
        hours = normalized // 60
        minutes = normalized % 60
        return dt_time(hour=hours, minute=minutes)

    @staticmethod
    def _time_to_minutes_of_day(value: dt_time | None) -> int | None:
        if value is None:
            return None
        try:
            return int(value.hour) * 60 + int(value.minute)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _redact_battery_payload(value: object) -> object:
        """Return a diagnostics-safe copy of BatteryConfig payloads."""

        sensitive = {
            "email",
            "authorization",
            "cookie",
            "token",
            "access_token",
            "refresh_token",
            "xsrf_token",
            "x_xsrf_token",
            "session_id",
            "userid",
            "user_id",
            "username",
            "device_link",
            "interface_ip",
            "ip_addr",
            "gateway_ip_addr",
            "default_route",
            "mac_addr",
        }
        if isinstance(value, dict):
            out: dict[str, object] = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text.strip().lower().replace("-", "_") in sensitive:
                    out[key_text] = "[redacted]"
                else:
                    out[key_text] = EnphaseCoordinator._redact_battery_payload(item)
            return out
        if isinstance(value, list):
            return [EnphaseCoordinator._redact_battery_payload(item) for item in value]
        return value

    @property
    def battery_pending_age_seconds(self) -> int | None:
        requested_at = getattr(self, "_battery_pending_requested_at", None)
        if requested_at is None:
            return None
        try:
            now = dt_util.utcnow()
            age = int((now - requested_at).total_seconds())
        except Exception:
            return None
        return age if age >= 0 else 0

    @property
    def battery_profile(self) -> str | None:
        return getattr(self, "_battery_profile", None)

    @property
    def battery_profile_pending(self) -> bool:
        return getattr(self, "_battery_pending_profile", None) is not None

    @property
    def battery_pending_requested_at(self) -> datetime | None:
        return getattr(self, "_battery_pending_requested_at", None)

    @property
    def battery_effective_backup_percentage(self) -> int | None:
        return getattr(self, "_battery_backup_percentage", None)

    @property
    def battery_effective_operation_mode_sub_type(self) -> str | None:
        return getattr(self, "_battery_operation_mode_sub_type", None)

    @property
    def battery_selected_profile(self) -> str | None:
        return getattr(self, "_battery_pending_profile", None) or getattr(
            self, "_battery_profile", None
        )

    @property
    def battery_selected_backup_percentage(self) -> int | None:
        return (
            getattr(self, "_battery_pending_reserve", None)
            if getattr(self, "_battery_pending_reserve", None) is not None
            else getattr(self, "_battery_backup_percentage", None)
        )

    @property
    def battery_selected_operation_mode_sub_type(self) -> str | None:
        return (
            getattr(self, "_battery_pending_sub_type", None)
            if getattr(self, "_battery_pending_profile", None) == "cost_savings"
            else getattr(self, "_battery_operation_mode_sub_type", None)
        )

    @property
    def battery_pending_profile(self) -> str | None:
        return getattr(self, "_battery_pending_profile", None)

    @property
    def battery_pending_backup_percentage(self) -> int | None:
        return getattr(self, "_battery_pending_reserve", None)

    @property
    def battery_pending_operation_mode_sub_type(self) -> str | None:
        return getattr(self, "_battery_pending_sub_type", None)

    @property
    def battery_has_encharge(self) -> bool | None:
        return getattr(self, "_battery_has_encharge", None)

    @property
    def battery_is_charging_modes_enabled(self) -> bool | None:
        return getattr(self, "_battery_is_charging_modes_enabled", None)

    @property
    def battery_show_battery_backup_percentage(self) -> bool | None:
        return getattr(self, "_battery_show_battery_backup_percentage", None)

    @property
    def battery_show_storm_guard(self) -> bool | None:
        return getattr(self, "_battery_show_storm_guard", None)

    @property
    def battery_show_production(self) -> bool | None:
        return getattr(self, "_battery_show_production", None)

    @property
    def battery_show_consumption(self) -> bool | None:
        return getattr(self, "_battery_show_consumption", None)

    @property
    def battery_has_enpower(self) -> bool | None:
        return getattr(self, "_battery_has_enpower", None)

    @property
    def battery_country_code(self) -> str | None:
        return getattr(self, "_battery_country_code", None)

    @property
    def battery_region(self) -> str | None:
        return getattr(self, "_battery_region", None)

    @property
    def battery_locale(self) -> str | None:
        return getattr(self, "_battery_locale", None)

    @property
    def battery_timezone(self) -> str | None:
        return getattr(self, "_battery_timezone", None)

    @property
    def battery_feature_details(self) -> dict[str, object]:
        details = getattr(self, "_battery_feature_details", None)
        if not isinstance(details, dict):
            return {}
        return dict(details)

    @property
    def battery_user_is_owner(self) -> bool | None:
        return getattr(self, "_battery_user_is_owner", None)

    @property
    def battery_user_is_installer(self) -> bool | None:
        return getattr(self, "_battery_user_is_installer", None)

    @property
    def battery_site_status_code(self) -> str | None:
        return getattr(self, "_battery_site_status_code", None)

    @property
    def battery_site_status_text(self) -> str | None:
        return getattr(self, "_battery_site_status_text", None)

    @property
    def battery_site_status_severity(self) -> str | None:
        return getattr(self, "_battery_site_status_severity", None)

    @property
    def battery_aggregate_charge_pct(self) -> float | None:
        value = getattr(self, "_battery_aggregate_charge_pct", None)
        if value is None:
            return None
        try:
            return float(value)
        except Exception:  # noqa: BLE001
            return None

    @property
    def battery_aggregate_status(self) -> str | None:
        value = getattr(self, "_battery_aggregate_status", None)
        if value is None:
            return None
        try:
            text = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @property
    def battery_aggregate_status_details(self) -> dict[str, object]:
        details = getattr(self, "_battery_aggregate_status_details", None)
        if not isinstance(details, dict):
            return {}
        return dict(details)

    @property
    def battery_status_payload(self) -> dict[str, object] | None:
        payload = getattr(self, "_battery_status_payload", None)
        if not isinstance(payload, dict):
            return None
        return dict(payload)

    @property
    def battery_backup_history_events(self) -> list[dict[str, object]]:
        events = getattr(self, "_battery_backup_history_events", None)
        if not isinstance(events, list):
            return []
        out: list[dict[str, object]] = []
        for item in events:
            if isinstance(item, dict):
                out.append(dict(item))
        return out

    @property
    def battery_status_summary(self) -> dict[str, object]:
        details = dict(getattr(self, "_battery_aggregate_status_details", {}) or {})
        details["aggregate_charge_pct"] = self.battery_aggregate_charge_pct
        details["aggregate_status"] = self.battery_aggregate_status
        details["battery_order"] = self.iter_battery_serials()
        return details

    @property
    def battery_profile_option_keys(self) -> list[str]:
        options: list[str] = []
        if getattr(self, "_battery_show_charge_from_grid", None):
            options.append("self-consumption")
        if getattr(self, "_battery_show_savings_mode", None):
            options.append("cost_savings")
        if getattr(self, "_battery_show_full_backup", None):
            options.append("backup_only")
        current = getattr(self, "_battery_profile", None)
        if current:
            options.append(current)
        pending = getattr(self, "_battery_pending_profile", None)
        if pending:
            options.append(pending)
        return [item for item in dict.fromkeys(options) if item]

    @property
    def battery_profile_option_labels(self) -> dict[str, str]:
        labels: dict[str, str] = {}
        for key in self.battery_profile_option_keys:
            label = self._battery_profile_label(key)
            if label:
                labels[key] = label
        return labels

    @property
    def battery_profile_display(self) -> str | None:
        return self._battery_profile_label(self.battery_selected_profile)

    @property
    def battery_effective_profile_display(self) -> str | None:
        return self._battery_profile_label(self._battery_profile)

    @property
    def battery_controls_available(self) -> bool:
        if getattr(self, "_battery_has_encharge", None) is False:
            return False
        if getattr(self, "_battery_profile", None) is not None:
            return True
        return bool(self.battery_profile_option_keys)

    @property
    def savings_use_battery_after_peak(self) -> bool | None:
        profile = self.battery_selected_profile
        if profile != "cost_savings":
            return None
        subtype = self.battery_selected_operation_mode_sub_type
        if subtype is None:
            return False
        return subtype == SAVINGS_OPERATION_MODE_SUBTYPE

    @property
    def savings_use_battery_switch_available(self) -> bool:
        if not self.battery_controls_available:
            return False
        if getattr(self, "_battery_show_savings_mode", None) is False:
            return False
        return self.battery_selected_profile == "cost_savings"

    @property
    def battery_reserve_editable(self) -> bool:
        if not self.battery_controls_available:
            return False
        # Prefer cfgControl.show (used by Enlighten app) over the
        # legacy showBatteryBackupPercentage flag which is unreliable
        # on EMEA sites.  Use cfgControl whenever present; fall back
        # to legacy only when the field is absent.
        cfg_show = getattr(self, "_battery_cfg_control_show", None)
        if cfg_show is not None:
            if cfg_show is False:
                return False
        elif getattr(self, "_battery_show_battery_backup_percentage", None) is False:
            return False
        owner = self.battery_user_is_owner
        installer = self.battery_user_is_installer
        if owner is False and installer is False:
            return False
        profile = self.battery_selected_profile
        if profile is None:
            return False
        return profile != "backup_only"

    @property
    def battery_reserve_min(self) -> int:
        profile = self.battery_selected_profile
        if profile == "backup_only":
            return 100
        return self._battery_min_soc_floor()

    @property
    def battery_reserve_max(self) -> int:
        return 100

    @property
    def battery_grid_mode(self) -> str | None:
        return getattr(self, "_battery_grid_mode", None)

    @property
    def battery_mode_display(self) -> str | None:
        return self._battery_grid_mode_label(self.battery_grid_mode)

    @property
    def battery_charge_from_grid_allowed(self) -> bool | None:
        key = self._battery_grid_mode_key(self.battery_grid_mode)
        permissions = BATTERY_GRID_MODE_PERMISSIONS.get(key or "")
        if permissions is None:
            return None
        return permissions[0]

    @property
    def battery_discharge_to_grid_allowed(self) -> bool | None:
        key = self._battery_grid_mode_key(self.battery_grid_mode)
        permissions = BATTERY_GRID_MODE_PERMISSIONS.get(key or "")
        if permissions is None:
            return None
        return permissions[1]

    @property
    def charge_from_grid_control_available(self) -> bool:
        if getattr(self, "_battery_has_encharge", None) is False:
            return False
        # Prefer cfgControl.show/enabled (used by Enlighten app) over
        # the legacy hideChargeFromGrid flag which is unreliable on
        # EMEA sites.  Use cfgControl whenever present; fall back to
        # legacy only when both fields are absent.
        cfg_show = getattr(self, "_battery_cfg_control_show", None)
        cfg_enabled = getattr(self, "_battery_cfg_control_enabled", None)
        if cfg_show is not None or cfg_enabled is not None:
            if cfg_show is False or cfg_enabled is False:
                return False
        else:
            if getattr(self, "_battery_hide_charge_from_grid", None) is True:
                return False
        owner = self.battery_user_is_owner
        installer = self.battery_user_is_installer
        if owner is False and installer is False:
            return False
        return getattr(self, "_battery_charge_from_grid", None) is not None

    @property
    def battery_charge_from_grid_enabled(self) -> bool | None:
        return getattr(self, "_battery_charge_from_grid", None)

    @property
    def battery_charge_from_grid_schedule_enabled(self) -> bool | None:
        return getattr(self, "_battery_charge_from_grid_schedule_enabled", None)

    @property
    def charge_from_grid_schedule_supported(self) -> bool:
        if not self.charge_from_grid_control_available:
            return False
        begin = getattr(self, "_battery_charge_begin_time", None)
        end = getattr(self, "_battery_charge_end_time", None)
        return begin is not None and end is not None

    @property
    def charge_from_grid_schedule_available(self) -> bool:
        if not self.charge_from_grid_schedule_supported:
            return False
        return self.battery_charge_from_grid_enabled is True

    @property
    def battery_charge_from_grid_start_time(self) -> dt_time | None:
        return self._minutes_of_day_to_time(
            getattr(self, "_battery_charge_begin_time", None)
        )

    @property
    def battery_charge_from_grid_end_time(self) -> dt_time | None:
        return self._minutes_of_day_to_time(
            getattr(self, "_battery_charge_end_time", None)
        )

    @property
    def battery_cfg_schedule_limit(self) -> int | None:
        """Return the CFG schedule charge limit (max SoC %) from /schedules."""
        return getattr(self, "_battery_cfg_schedule_limit", None)

    @property
    def battery_shutdown_level(self) -> int | None:
        return getattr(self, "_battery_very_low_soc", None)

    @property
    def battery_shutdown_level_min(self) -> int:
        value = getattr(self, "_battery_very_low_soc_min", None)
        if value is None:
            return self._battery_min_soc_floor()
        return int(value)

    @property
    def battery_shutdown_level_max(self) -> int:
        value = getattr(self, "_battery_very_low_soc_max", None)
        return value if value is not None else 100

    @property
    def battery_shutdown_level_available(self) -> bool:
        if getattr(self, "_battery_envoy_supports_vls", None) is False:
            return False
        if getattr(self, "_battery_very_low_soc", None) is None:
            return False
        return True

    def _battery_min_soc_floor(self) -> int:
        value = self._coerce_optional_int(
            getattr(self, "_battery_very_low_soc_min", None)
        )
        if value is None:
            return BATTERY_MIN_SOC_FALLBACK
        return max(0, min(100, int(value)))

    def _grid_control_is_stale(self) -> bool:
        raw_supported = getattr(self, "_grid_control_supported", None)
        if raw_supported is None:
            return True
        last_success = getattr(self, "_grid_control_check_last_success_mono", None)
        if not isinstance(last_success, (int, float)):
            return False
        age = time.monotonic() - float(last_success)
        if age < 0:
            return False
        return age >= GRID_CONTROL_CHECK_STALE_AFTER_S

    @property
    def grid_control_supported(self) -> bool | None:
        raw_supported = getattr(self, "_grid_control_supported", None)
        if raw_supported is None:
            return None
        if self._grid_control_is_stale():
            return None
        return raw_supported

    @property
    def grid_control_disable(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        return getattr(self, "_grid_control_disable", None)

    @property
    def grid_control_active_download(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        return getattr(self, "_grid_control_active_download", None)

    @property
    def grid_control_sunlight_backup_system_check(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        return getattr(self, "_grid_control_sunlight_backup_system_check", None)

    @property
    def grid_control_grid_outage_check(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        return getattr(self, "_grid_control_grid_outage_check", None)

    @property
    def grid_control_user_initiated_toggle(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        return getattr(self, "_grid_control_user_initiated_toggle", None)

    @property
    def grid_toggle_pending(self) -> bool:
        return self.grid_control_user_initiated_toggle is True

    @property
    def grid_toggle_blocked_reasons(self) -> list[str]:
        if self.grid_control_supported is not True:
            return []
        reasons: list[str] = []
        if self.grid_control_disable is True:
            reasons.append("disable_grid_control")
        if self.grid_control_active_download is True:
            reasons.append("active_download")
        if self.grid_control_sunlight_backup_system_check is True:
            reasons.append("sunlight_backup_system_check")
        if self.grid_control_grid_outage_check is True:
            reasons.append("grid_outage_check")
        return reasons

    @property
    def grid_toggle_allowed(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        if self.grid_toggle_pending:
            return False
        flags = [
            self.grid_control_disable,
            self.grid_control_active_download,
            self.grid_control_sunlight_backup_system_check,
            self.grid_control_grid_outage_check,
        ]
        if any(value is True for value in flags):
            return False
        if any(value is None for value in flags):
            return None
        return True

    def _dry_contact_settings_is_stale(self) -> bool:
        raw_supported = getattr(self, "_dry_contact_settings_supported", None)
        if raw_supported is None:
            return True
        last_success = getattr(self, "_dry_contact_settings_last_success_mono", None)
        if not isinstance(last_success, (int, float)):
            return False
        age = time.monotonic() - float(last_success)
        if age < 0:
            return False
        return age >= DRY_CONTACT_SETTINGS_STALE_AFTER_S

    @property
    def dry_contact_settings_supported(self) -> bool | None:
        raw_supported = getattr(self, "_dry_contact_settings_supported", None)
        if raw_supported is None:
            return None
        if self._dry_contact_settings_is_stale():
            return None
        return raw_supported

    def dry_contact_settings_entries(self) -> list[dict[str, object]]:
        entries = getattr(self, "_dry_contact_settings_entries", [])
        if not isinstance(entries, list):
            return []
        return [
            self._copy_dry_contact_settings_entry(entry)
            for entry in entries
            if isinstance(entry, dict)
        ]

    def dry_contact_unmatched_settings(self) -> list[dict[str, object]]:
        entries = getattr(self, "_dry_contact_unmatched_settings", [])
        if not isinstance(entries, list):
            return []
        return [
            self._copy_dry_contact_settings_entry(entry)
            for entry in entries
            if isinstance(entry, dict)
        ]

    @staticmethod
    def _normalize_grid_mode_value(value: object) -> str | None:
        text = EnphaseCoordinator._coerce_optional_text(value)
        if text is None:
            return None
        upper = text.upper()
        if "OFF_GRID" in upper:
            return "off_grid"
        if "ON_GRID" in upper:
            return "on_grid"
        return None

    @property
    def grid_mode_raw_states(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        data = self.data if isinstance(self.data, dict) else {}
        for payload in data.values():
            if not isinstance(payload, dict):
                continue
            raw = self._coerce_optional_text(payload.get("off_grid_state"))
            if not raw:
                continue
            if raw in seen:
                continue
            seen.add(raw)
            out.append(raw)
        return sorted(out)

    @property
    def grid_mode(self) -> str | None:
        raw_states = self.grid_mode_raw_states
        if not raw_states:
            return None
        normalized = {
            mode
            for mode in (self._normalize_grid_mode_value(state) for state in raw_states)
            if mode is not None
        }
        if len(normalized) == 1:
            return next(iter(normalized))
        return "unknown"

    def _raise_grid_validation(
        self,
        key: str,
        *,
        placeholders: dict[str, object] | None = None,
        message: str | None = None,
    ) -> None:
        kwargs: dict[str, object] = {
            "translation_domain": DOMAIN,
            "translation_key": f"exceptions.{key}",
            "translation_placeholders": placeholders,
        }
        if message is None:
            raise ServiceValidationError(**kwargs)
        raise ServiceValidationError(message, **kwargs)

    def _grid_envoy_serial(self) -> str | None:
        bucket = self.type_bucket("envoy")
        if not isinstance(bucket, dict):
            return None
        devices = bucket.get("devices")
        if not isinstance(devices, list):
            return None
        for device in devices:
            if not isinstance(device, dict):
                continue
            serial = self._coerce_optional_text(device.get("serial_number"))
            if serial:
                return serial
        return None

    async def _async_assert_grid_toggle_allowed(self) -> None:
        await self._async_refresh_grid_control_check(force=True)
        if self.grid_control_supported is not True:
            self._raise_grid_validation("grid_control_unavailable")
        if self.grid_toggle_allowed is True:
            return
        reasons = self.grid_toggle_blocked_reasons
        reasons_text = ", ".join(reasons) if reasons else "unknown"
        self._raise_grid_validation(
            "grid_control_blocked",
            placeholders={"reasons": reasons_text},
        )

    @property
    def storm_guard_state(self) -> str | None:
        return self._storm_guard_state

    @property
    def storm_guard_update_pending(self) -> bool:
        self._sync_storm_guard_pending()
        return getattr(self, "_storm_guard_pending_state", None) is not None

    @property
    def storm_evse_enabled(self) -> bool | None:
        return self._storm_evse_enabled

    @property
    def storm_alert_active(self) -> bool | None:
        return self._storm_alert_active

    @property
    def storm_alert_critical_override(self) -> bool | None:
        return getattr(self, "_storm_alert_critical_override", None)

    @property
    def storm_alerts(self) -> list[dict[str, object]]:
        alerts = getattr(self, "_storm_alerts", None)
        if not isinstance(alerts, list):
            return []
        out: list[dict[str, object]] = []
        for item in alerts:
            if isinstance(item, dict):
                out.append(dict(item))
        return out

    @property
    def heatpump_power_w(self) -> float | None:
        value = getattr(self, "_heatpump_power_w", None)
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    @property
    def heatpump_power_sample_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_power_sample_utc", None)
        if isinstance(value, datetime):
            return value
        return None

    @property
    def heatpump_power_start_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_power_start_utc", None)
        if isinstance(value, datetime):
            return value
        return None

    @property
    def heatpump_power_device_uid(self) -> str | None:
        value = getattr(self, "_heatpump_power_device_uid", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_power_source(self) -> str | None:
        value = getattr(self, "_heatpump_power_source", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_power_last_error(self) -> str | None:
        value = getattr(self, "_heatpump_power_last_error", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def current_power_consumption_w(self) -> float | None:
        value = getattr(self, "_current_power_consumption_w", None)
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    @property
    def current_power_consumption_sample_utc(self) -> datetime | None:
        value = getattr(self, "_current_power_consumption_sample_utc", None)
        if isinstance(value, datetime):
            return value
        return None

    @property
    def current_power_consumption_reported_units(self) -> str | None:
        value = getattr(self, "_current_power_consumption_reported_units", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def current_power_consumption_reported_precision(self) -> int | None:
        value = getattr(self, "_current_power_consumption_reported_precision", None)
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    @property
    def current_power_consumption_source(self) -> str | None:
        value = getattr(self, "_current_power_consumption_source", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def battery_supports_mqtt(self) -> bool | None:
        return getattr(self, "_battery_supports_mqtt", None)

    @property
    def battery_profile_polling_interval(self) -> int | None:
        return getattr(self, "_battery_polling_interval_s", None)

    @property
    def battery_profile_evse_device(self) -> dict[str, object] | None:
        value = getattr(self, "_battery_profile_evse_device", None)
        if isinstance(value, dict):
            return dict(value)
        return None

    @property
    def battery_cfg_control_show(self) -> bool | None:
        return getattr(self, "_battery_cfg_control_show", None)

    @property
    def battery_cfg_control_enabled(self) -> bool | None:
        return getattr(self, "_battery_cfg_control_enabled", None)

    @property
    def battery_cfg_control_schedule_supported(self) -> bool | None:
        return getattr(self, "_battery_cfg_control_schedule_supported", None)

    @property
    def battery_cfg_control_force_schedule_supported(self) -> bool | None:
        return getattr(self, "_battery_cfg_control_force_schedule_supported", None)

    @property
    def battery_use_battery_for_self_consumption(self) -> bool | None:
        return getattr(self, "_battery_use_battery_for_self_consumption", None)

    def _mark_auth_settings_available(self) -> None:
        if self._auth_settings_available and not self._auth_settings_issue_reported:
            return
        self._auth_settings_available = True
        self._auth_settings_failures = 0
        self._auth_settings_last_error = None
        self._auth_settings_last_failure_utc = None
        self._auth_settings_backoff_until = None
        self._auth_settings_backoff_ends_utc = None
        if self._auth_settings_issue_reported:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_AUTH_SETTINGS_UNAVAILABLE)
            self._auth_settings_issue_reported = False

    def mark_auth_settings_available(self) -> None:
        """Public wrapper used by entities."""

        self._mark_auth_settings_available()

    def _note_auth_settings_unavailable(
        self,
        err: Exception | str | None = None,
    ) -> None:
        """Record auth settings outage and raise a repair issue."""
        reason = redact_text(err, site_ids=(self.site_id,)) if err else ""
        if not reason:
            reason = "Auth settings unavailable"
        self._auth_settings_available = False
        self._auth_settings_failures += 1
        self._auth_settings_last_error = reason
        self._auth_settings_last_failure_utc = dt_util.utcnow()
        delay = self._auth_settings_backoff_delay()
        self._auth_settings_backoff_until = time.monotonic() + delay
        try:
            self._auth_settings_backoff_ends_utc = dt_util.utcnow() + timedelta(
                seconds=delay
            )
        except Exception:
            self._auth_settings_backoff_ends_utc = None
        if not self._auth_settings_issue_reported:
            metrics, placeholders = self._issue_context()
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_AUTH_SETTINGS_UNAVAILABLE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_AUTH_SETTINGS_UNAVAILABLE,
                translation_placeholders=placeholders,
                data={"site_metrics": metrics},
            )
            self._auth_settings_issue_reported = True

    def note_auth_settings_unavailable(
        self,
        err: Exception | str | None = None,
    ) -> None:
        """Public wrapper used by entities."""

        self._note_auth_settings_unavailable(err)

    def _sync_session_history_issue(self) -> None:
        manager = getattr(self, "session_history", None)
        if manager is None:
            return
        available = getattr(manager, "service_available", True)
        if available:
            if self._session_history_issue_reported:
                ir.async_delete_issue(
                    self.hass, DOMAIN, ISSUE_SESSION_HISTORY_UNAVAILABLE
                )
                self._session_history_issue_reported = False
            return
        if not self._session_history_issue_reported:
            metrics, placeholders = self._issue_context()
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_SESSION_HISTORY_UNAVAILABLE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_SESSION_HISTORY_UNAVAILABLE,
                translation_placeholders=placeholders,
                data={"site_metrics": metrics},
            )
            self._session_history_issue_reported = True

    def _sync_site_energy_issue(self) -> None:
        energy = getattr(self, "energy", None)
        if energy is None:
            return
        available = getattr(energy, "service_available", True)
        if available:
            if self._site_energy_issue_reported:
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_SITE_ENERGY_UNAVAILABLE)
                self._site_energy_issue_reported = False
            return
        if not self._site_energy_issue_reported:
            metrics, placeholders = self._issue_context()
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_SITE_ENERGY_UNAVAILABLE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_SITE_ENERGY_UNAVAILABLE,
                translation_placeholders=placeholders,
                data={"site_metrics": metrics},
            )
            self._site_energy_issue_reported = True

    def _sync_battery_profile_pending_issue(self) -> None:
        """Raise/clear repair issue when a BatteryConfig profile change stalls."""

        pending_profile = getattr(self, "_battery_pending_profile", None)
        requested_at = getattr(self, "_battery_pending_requested_at", None)
        age_s = self.battery_pending_age_seconds
        pending_overdue = bool(
            pending_profile
            and requested_at is not None
            and age_s is not None
            and age_s >= int(BATTERY_PROFILE_PENDING_TIMEOUT_S)
        )
        if not pending_overdue:
            if self._battery_profile_issue_reported:
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_BATTERY_PROFILE_PENDING)
                self._battery_profile_issue_reported = False
            return
        if self._battery_profile_issue_reported:
            return
        metrics, placeholders = self._issue_context()
        placeholders["pending_timeout_minutes"] = str(
            int(BATTERY_PROFILE_PENDING_TIMEOUT_S // 60)
        )
        if age_s is not None:
            placeholders["pending_age_minutes"] = str(max(1, age_s // 60))
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_BATTERY_PROFILE_PENDING,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_BATTERY_PROFILE_PENDING,
            translation_placeholders=placeholders,
            data={"site_metrics": metrics},
        )
        self._battery_profile_issue_reported = True

    def _schedule_backoff_timer(self, delay: float) -> None:
        if delay <= 0:
            self._clear_backoff_timer()
            self._backoff_until = None
            self.backoff_ends_utc = None
            self.hass.async_create_task(self.async_request_refresh())
            return
        self._clear_backoff_timer()
        try:
            self.backoff_ends_utc = dt_util.utcnow() + timedelta(seconds=delay)
        except Exception:
            self.backoff_ends_utc = None

        async def _resume(_now: datetime) -> None:
            self._backoff_cancel = None
            self._backoff_until = None
            self.backoff_ends_utc = None
            await self.async_request_refresh()

        self._backoff_cancel = async_call_later(self.hass, delay, _resume)

    def set_last_set_amps(self, sn: str, amps: int) -> None:
        safe = self._apply_amp_limits(str(sn), amps)
        self.last_set_amps[str(sn)] = safe

    def require_plugged(self, sn: str) -> None:
        """Raise a translated validation error when the EV is unplugged."""
        try:
            data = (self.data or {}).get(str(sn), {})
        except Exception:
            data = {}
        plugged = data.get("plugged")
        if plugged is True:
            return
        display = data.get("display_name") or data.get("name") or sn
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="exceptions.charger_not_plugged",
            translation_placeholders={"name": str(display)},
        )

    def _ensure_serial_tracked(self, serial: str) -> bool:
        """Record a charger serial that appears in runtime data.

        Returns True when the serial was newly discovered.
        """
        if not hasattr(self, "serials") or self.serials is None:
            self.serials = set()
        if not hasattr(self, "_serial_order") or self._serial_order is None:
            self._serial_order = []
        if serial is None:
            return False
        try:
            sn = str(serial).strip()
        except Exception:
            return False
        if not sn:
            return False
        if sn not in self.serials:
            self.serials.add(sn)
            if sn not in self._serial_order:
                self._serial_order.append(sn)
            _LOGGER.info(
                "Discovered Enphase charger serial=%s during update",
                redact_identifier(sn),
            )
            return True
        if sn not in self._serial_order:
            self._serial_order.append(sn)
        return False

    def iter_serials(self) -> list[str]:
        """Return charger serials in a stable order for entity setup."""
        if getattr(self, "site_only", False):
            return []
        ordered: list[str] = []
        serial_order = getattr(self, "_serial_order", None)
        known_serials = getattr(self, "serials", None)
        if serial_order:
            ordered.extend(serial_order)
        elif known_serials:
            # Fallback for legacy configs where order could not be preserved
            ordered.extend(sorted(known_serials))
        source = self.data if isinstance(self.data, dict) else {}
        if isinstance(source, dict):
            ordered.extend(str(sn) for sn in source.keys())
        # Deduplicate while preserving order
        return [sn for sn in dict.fromkeys(ordered) if sn]

    def iter_battery_serials(self) -> list[str]:
        """Return active battery identities in a stable order."""

        order = getattr(self, "_battery_storage_order", None)
        snapshots = getattr(self, "_battery_storage_data", None)
        if not isinstance(order, list) or not isinstance(snapshots, dict):
            return []
        out: list[str] = []
        for item in order:
            try:
                key = str(item).strip()
            except Exception:  # noqa: BLE001
                continue
            if not key or key not in snapshots:
                continue
            out.append(key)
        return out

    def battery_storage(self, serial: str) -> dict[str, object] | None:
        """Return normalized battery snapshot for an active battery identity."""

        snapshots = getattr(self, "_battery_storage_data", None)
        if not isinstance(snapshots, dict):
            return None
        try:
            key = str(serial).strip()
        except Exception:  # noqa: BLE001
            return None
        if not key:
            return None
        payload = snapshots.get(key)
        if not isinstance(payload, dict):
            return None
        out = dict(payload)
        detail = self.system_dashboard_battery_detail(key)
        if isinstance(detail, dict):
            for detail_key, detail_value in detail.items():
                if detail_value is None:
                    continue
                out[detail_key] = detail_value
        return out

    def get_desired_charging(self, sn: str) -> bool | None:
        """Return the user-requested charging state when known."""
        return self._desired_charging.get(str(sn))

    def set_desired_charging(self, sn: str, desired: bool | None) -> None:
        """Persist the user-requested charging state for auto-resume logic."""
        sn_str = str(sn)
        if desired is None:
            self._desired_charging.pop(sn_str, None)
            return
        self._desired_charging[sn_str] = bool(desired)

    @staticmethod
    def _coerce_amp(value) -> int | None:
        """Convert mixed-type amp values into ints, preserving None."""
        if value is None:
            return None
        try:
            if isinstance(value, str):
                stripped = value.strip()
                if not stripped:
                    return None
                return int(float(stripped))
            if isinstance(value, (int, float)):
                return int(float(value))
        except Exception:
            return None
        return None

    def _amp_limits(self, sn: str) -> tuple[int | None, int | None]:
        data: dict | None = None
        try:
            data = (self.data or {}).get(str(sn))
        except Exception:
            data = None
        data = data or {}
        min_amp = self._coerce_amp(data.get("min_amp"))
        max_amp = self._coerce_amp(data.get("max_amp"))
        if min_amp is not None and max_amp is not None and max_amp < min_amp:
            # If backend reported inverted bounds, prefer the stricter (min).
            max_amp = min_amp
        return min_amp, max_amp

    def _apply_amp_limits(self, sn: str, amps: int | float | str | None) -> int:
        value = self._coerce_amp(amps)
        if value is None:
            value = 32
        min_amp, max_amp = self._amp_limits(sn)
        if max_amp is not None and value > max_amp:
            value = max_amp
        if min_amp is not None and value < min_amp:
            value = min_amp
        return value

    def pick_start_amps(
        self, sn: str, requested: int | float | str | None = None, fallback: int = 32
    ) -> int:
        """Return a safe charging amp target honoring device limits."""
        sn_str = str(sn)
        candidates: list[int | float | str | None] = []
        if requested is not None:
            candidates.append(requested)
        candidates.append(self.last_set_amps.get(sn_str))
        try:
            data = (self.data or {}).get(sn_str)
        except Exception:
            data = None
        data = data or {}
        for key in ("charging_level", "session_charge_level"):
            if key in data:
                candidates.append(data.get(key))
        candidates.append(fallback)
        for candidate in candidates:
            coerced = self._coerce_amp(candidate)
            if coerced is not None:
                return self._apply_amp_limits(sn_str, coerced)
        return self._apply_amp_limits(sn_str, fallback)

    async def _get_charge_mode(self, sn: str) -> str | None:
        """Return charge mode using a 300s cache to reduce API calls."""
        now = time.monotonic()
        cached = self._charge_mode_cache.get(sn)
        if cached and (now - cached[1] < 300):
            return cached[0]
        try:
            mode = await self.client.charge_mode(sn)
        except SchedulerUnavailable as err:
            self._note_scheduler_unavailable(err)
            return None
        except Exception:
            mode = None
        if mode:
            self._mark_scheduler_available()
            self._charge_mode_cache[sn] = (mode, now)
        return mode

    async def _get_green_battery_setting(
        self, sn: str
    ) -> tuple[bool | None, bool] | None:
        """Return green charging battery setting using a short cache."""

        now = time.monotonic()
        cached = self._green_battery_cache.get(sn)
        if cached and (now - cached[2] < GREEN_BATTERY_CACHE_TTL):
            return cached[0], cached[1]
        try:
            settings = await self.client.green_charging_settings(sn)
        except SchedulerUnavailable as err:
            self._note_scheduler_unavailable(err)
            return None
        except Exception:
            return None
        self._mark_scheduler_available()

        enabled: bool | None = None
        supported = False

        def _as_bool(value) -> bool | None:
            if value is None:
                return None
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in ("true", "1", "yes", "y"):
                    return True
                if normalized in ("false", "0", "no", "n"):
                    return False
            return None

        if isinstance(settings, list):
            for item in settings:
                if not isinstance(item, dict):
                    continue
                if item.get("chargerSettingName") != GREEN_BATTERY_SETTING:
                    continue
                supported = True
                enabled = _as_bool(item.get("enabled"))
                break

        self._green_battery_cache[sn] = (enabled, supported, now)
        return enabled, supported

    async def _get_auth_settings(
        self, sn: str
    ) -> tuple[bool | None, bool | None, bool, bool] | None:
        """Return session authentication settings using a short cache."""

        now = time.monotonic()
        cached = self._auth_settings_cache.get(sn)
        if cached and (now - cached[4] < AUTH_SETTINGS_CACHE_TTL):
            return cached[0], cached[1], cached[2], cached[3]
        if self._auth_settings_backoff_active():
            if cached:
                return cached[0], cached[1], cached[2], cached[3]
            return None
        try:
            settings = await self.client.charger_auth_settings(sn)
        except AuthSettingsUnavailable as err:
            self._note_auth_settings_unavailable(err)
            return None
        except Exception:
            return None

        app_enabled: bool | None = None
        rfid_enabled: bool | None = None
        app_supported = False
        rfid_supported = False

        def _coerce(value) -> bool | None:
            if value is None:
                return False
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in ("true", "1", "yes", "y", "enabled", "enable"):
                    return True
                if normalized in (
                    "false",
                    "0",
                    "no",
                    "n",
                    "disabled",
                    "disable",
                    "",
                ):
                    return False
            return None

        if isinstance(settings, list):
            for item in settings:
                if not isinstance(item, dict):
                    continue
                key = item.get("key")
                raw = item.get("value")
                if raw is None:
                    raw = item.get("reqValue")
                if key == AUTH_APP_SETTING:
                    app_supported = True
                    app_enabled = _coerce(raw)
                elif key == AUTH_RFID_SETTING:
                    rfid_supported = True
                    rfid_enabled = _coerce(raw)

        if not app_supported and not rfid_supported:
            return None

        self._mark_auth_settings_available()
        self._auth_settings_cache[sn] = (
            app_enabled,
            rfid_enabled,
            app_supported,
            rfid_supported,
            now,
        )
        return app_enabled, rfid_enabled, app_supported, rfid_supported

    def set_charge_mode_cache(self, sn: str, mode: str) -> None:
        """Update cache when user changes mode via select."""
        self._charge_mode_cache[str(sn)] = (str(mode), time.monotonic())

    def set_green_battery_cache(
        self, sn: str, enabled: bool, supported: bool = True
    ) -> None:
        """Update cache when user changes green charging battery setting."""
        self._green_battery_cache[str(sn)] = (
            bool(enabled),
            bool(supported),
            time.monotonic(),
        )

    def set_app_auth_cache(self, sn: str, enabled: bool) -> None:
        """Update cache when user changes app authentication."""
        sn_str = str(sn)
        now = time.monotonic()
        cached = self._auth_settings_cache.get(sn_str)
        if cached:
            _, rfid_enabled, _app_supported, rfid_supported, _ts = cached
            self._auth_settings_cache[sn_str] = (
                bool(enabled),
                rfid_enabled,
                True,
                rfid_supported,
                now,
            )
            return
        self._auth_settings_cache[sn_str] = (bool(enabled), None, True, False, now)

    def evse_feature_flag(self, key: str, sn: str | None = None) -> object | None:
        """Return a parsed EVSE feature flag for the site or charger."""

        key_text = str(key).strip()
        if not key_text:
            return None
        if sn:
            serial_flags = getattr(self, "_evse_feature_flags_by_serial", {}) or {}
            raw = serial_flags.get(str(sn), {}).get(key_text)
            if raw is not None:
                return raw
        return (getattr(self, "_evse_site_feature_flags", {}) or {}).get(key_text)

    def evse_feature_flag_enabled(self, key: str, sn: str | None = None) -> bool | None:
        """Return a feature flag coerced to a tri-state boolean."""

        return self._coerce_optional_bool(self.evse_feature_flag(key, sn))

    @staticmethod
    def _coerce_evse_feature_flags_map(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {}
        out: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            try:
                key = str(raw_key).strip()
            except Exception:
                continue
            if not key:
                continue
            out[key] = raw_value
        return out

    def _parse_evse_feature_flags_payload(self, payload: object) -> None:
        """Cache site and charger feature flags from the EVSE management payload."""

        self._evse_site_feature_flags = {}
        self._evse_feature_flags_by_serial = {}
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        site_flags: dict[str, object] = {}
        charger_flags: dict[str, dict[str, object]] = {}
        for raw_key, raw_value in data.items():
            try:
                key = str(raw_key).strip()
            except Exception:
                continue
            if not key:
                continue
            if isinstance(raw_value, dict):
                flags = self._coerce_evse_feature_flags_map(raw_value)
                if flags:
                    charger_flags[key] = flags
                continue
            site_flags[key] = raw_value
        self._evse_site_feature_flags = site_flags
        self._evse_feature_flags_by_serial = charger_flags

    async def _async_refresh_evse_feature_flags(self, *, force: bool = False) -> None:
        """Refresh EVSE feature flags used for capability gating."""

        now = time.monotonic()
        if not force and self._evse_feature_flags_cache_until:
            if now < self._evse_feature_flags_cache_until:
                return
        fetcher = getattr(self.client, "evse_feature_flags", None)
        if not callable(fetcher):
            self._evse_feature_flags_payload = None
            self._evse_site_feature_flags = {}
            self._evse_feature_flags_by_serial = {}
            return
        country = getattr(self, "_battery_country_code", None)
        try:
            payload = await fetcher(country=country)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "EVSE feature flags fetch failed: %s",
                redact_text(err, site_ids=(self.site_id,)),
            )
            self._evse_feature_flags_cache_until = now + 60.0
            return
        if not isinstance(payload, dict):
            self._evse_feature_flags_payload = None
            self._evse_site_feature_flags = {}
            self._evse_feature_flags_by_serial = {}
            self._evse_feature_flags_cache_until = now + 60.0
            _LOGGER.debug(
                "EVSE feature flags payload shape was invalid: %s",
                self._debug_render_summary(self._debug_payload_shape(payload)),
            )
            return
        self._evse_feature_flags_payload = dict(payload)
        self._parse_evse_feature_flags_payload(payload)
        self._evse_feature_flags_cache_until = now + EVSE_FEATURE_FLAGS_CACHE_TTL
        self._debug_log_summary_if_changed(
            "evse_feature_flags",
            "EVSE feature flag summary",
            self._debug_evse_feature_flag_summary(),
        )

    @staticmethod
    def _coerce_optional_bool(value) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1", "yes", "y", "enabled", "enable", "on"):
                return True
            if normalized in ("false", "0", "no", "n", "disabled", "disable", "off"):
                return False
        return None

    @staticmethod
    def _parse_percent_value(value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except Exception:  # noqa: BLE001
                return None
        if not isinstance(value, str):
            return None
        try:
            cleaned = value.strip().replace("%", "")
        except Exception:  # noqa: BLE001
            return None
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_battery_status_text(value: object) -> str | None:
        if value is None:
            return None
        try:
            text = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        if not text:
            return None
        normalized = " ".join(text.replace("_", " ").replace("-", " ").split())
        if not normalized:
            return None
        tokens = [token for token in normalized.split(" ") if token]
        token_set = set(tokens)
        if "normal" in token_set and "not" not in token_set:
            return "normal"
        if any(
            token in token_set
            for token in ("error", "fault", "critical", "failed", "alarm")
        ):
            return "error"
        if any(token in token_set for token in ("warning", "warn", "degraded")):
            return "warning"
        if "abnormal" in token_set:
            return "warning"
        if "not reporting" in normalized:
            return "warning"
        if "not" in token_set and "normal" in token_set:
            return "warning"
        return "unknown"

    @staticmethod
    def _battery_status_severity_value(status: str | None) -> int:
        if status is None:
            return BATTERY_STATUS_SEVERITY["unknown"]
        return BATTERY_STATUS_SEVERITY.get(status, BATTERY_STATUS_SEVERITY["unknown"])

    @staticmethod
    def _battery_storage_key(payload: dict[str, object]) -> str | None:
        serial = EnphaseCoordinator._coerce_optional_text(payload.get("serial_number"))
        if serial:
            return serial
        storage_id = EnphaseCoordinator._coerce_optional_int(payload.get("id"))
        if storage_id is not None:
            return f"id_{storage_id}"
        return None

    @staticmethod
    def _normalize_battery_id(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if not value.is_integer():
                return None
            return str(int(value))
        if not isinstance(value, str):
            return None
        try:
            cleaned = value.strip().replace(",", "")
        except Exception:  # noqa: BLE001
            return None
        if not cleaned:
            return None
        if cleaned[0] in ("+", "-"):
            sign = cleaned[0]
            digits = cleaned[1:]
        else:
            sign = ""
            digits = cleaned
        if not digits.isdigit():
            return None
        return f"{sign}{digits}"

    def _parse_battery_status_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            self._battery_storage_data = {}
            self._battery_storage_order = []
            self._battery_aggregate_charge_pct = None
            self._battery_aggregate_status = None
            self._battery_aggregate_status_details = {}
            self._refresh_cached_topology()
            return

        storages = payload.get("storages")
        storage_items = storages if isinstance(storages, list) else []
        snapshots: dict[str, dict[str, object]] = {}
        order: list[str] = []
        status_map: dict[str, str] = {}
        raw_status_map: dict[str, str | None] = {}
        status_text_map: dict[str, str | None] = {}
        total_available_energy = 0.0
        total_capacity = 0.0
        contributing_count = 0
        missing_energy_capacity_keys: list[str] = []
        excluded_count = 0
        worst_key: str | None = None
        worst_status: str | None = None
        worst_severity = BATTERY_STATUS_SEVERITY["normal"]

        for item in storage_items:
            if not isinstance(item, dict):
                continue
            raw_payload: dict[str, object] = {}
            for raw_key, raw_value in item.items():
                raw_payload[str(raw_key)] = raw_value

            excluded = self._coerce_optional_bool(raw_payload.get("excluded")) is True
            if excluded:
                excluded_count += 1

            key = self._battery_storage_key(raw_payload)
            if not key:
                continue
            if excluded:
                continue

            charge_pct = self._parse_percent_value(raw_payload.get("current_charge"))
            available_energy = self._coerce_optional_kwh(
                raw_payload.get("available_energy")
            )
            max_capacity = self._coerce_optional_kwh(raw_payload.get("max_capacity"))
            status_raw = self._coerce_optional_text(raw_payload.get("status"))
            status_text = self._coerce_optional_text(raw_payload.get("statusText"))
            normalized_status_raw = self._normalize_battery_status_text(status_raw)
            normalized_status_text = self._normalize_battery_status_text(status_text)
            normalized_status = normalized_status_raw
            if normalized_status in (
                None,
                "unknown",
            ) and normalized_status_text not in (None, "unknown"):
                normalized_status = normalized_status_text
            if normalized_status is None:
                normalized_status = "unknown"
            severity = self._battery_status_severity_value(normalized_status)

            snapshot = dict(raw_payload)
            normalized_battery_id = self._normalize_battery_id(raw_payload.get("id"))
            if normalized_battery_id is not None:
                snapshot["id"] = normalized_battery_id
                snapshot["battery_id"] = normalized_battery_id
            snapshot["identity"] = key
            snapshot["current_charge_pct"] = charge_pct
            snapshot["available_energy_kwh"] = available_energy
            snapshot["max_capacity_kwh"] = max_capacity
            snapshot["status_normalized"] = normalized_status
            snapshot["status_text"] = status_text
            snapshots[key] = snapshot
            order.append(key)

            status_map[key] = normalized_status
            raw_status_map[key] = status_raw
            status_text_map[key] = status_text

            if (
                max_capacity is not None
                and max_capacity > 0
                and available_energy is not None
            ):
                total_capacity += max_capacity
                total_available_energy += available_energy
                contributing_count += 1
            else:
                missing_energy_capacity_keys.append(key)

            if severity > worst_severity:
                worst_severity = severity
                worst_status = normalized_status
                worst_key = key
            elif worst_status is None:
                worst_status = normalized_status
                worst_key = key

        aggregate_charge = None
        site_current_charge = self._parse_percent_value(payload.get("current_charge"))
        aggregate_charge_source = "unknown"
        included_count = len(snapshots)
        can_compute_weighted = (
            included_count > 0
            and contributing_count == included_count
            and total_capacity > 0
        )
        if can_compute_weighted:
            aggregate_charge = round((total_available_energy / total_capacity) * 100, 1)
            aggregate_charge_source = "computed"
        elif site_current_charge is not None:
            # Avoid publishing a subset-weighted SOC when any included battery
            # lacks energy/capacity fields; use authoritative site SOC instead.
            aggregate_charge = site_current_charge
            aggregate_charge_source = "site_current_charge"

        aggregate_status = worst_status or ("normal" if snapshots else "unknown")

        self._battery_storage_data = snapshots
        self._battery_storage_order = list(dict.fromkeys(order))
        self._battery_aggregate_charge_pct = aggregate_charge
        self._battery_aggregate_status = aggregate_status
        self._battery_aggregate_status_details = {
            "status": aggregate_status,
            "worst_storage_key": worst_key,
            "worst_status": worst_status,
            "per_battery_status": status_map,
            "per_battery_status_raw": raw_status_map,
            "per_battery_status_text": status_text_map,
            "included_count": included_count,
            "contributing_count": contributing_count,
            "aggregate_charge_source": aggregate_charge_source,
            "missing_energy_capacity_keys": list(
                dict.fromkeys(missing_energy_capacity_keys)
            ),
            "excluded_count": excluded_count,
            "available_energy_kwh": round(total_available_energy, 2),
            "max_capacity_kwh": round(total_capacity, 2),
            "site_current_charge_pct": site_current_charge,
            "site_available_energy_kwh": self._coerce_optional_kwh(
                payload.get("available_energy")
            ),
            "site_max_capacity_kwh": self._coerce_optional_kwh(
                payload.get("max_capacity")
            ),
            "site_available_power_kw": self._coerce_optional_float(
                payload.get("available_power")
            ),
            "site_max_power_kw": self._coerce_optional_float(payload.get("max_power")),
            "site_total_micros": self._coerce_optional_int(payload.get("total_micros")),
            "site_active_micros": self._coerce_optional_int(
                payload.get("active_micros")
            ),
            "site_inactive_micros": self._coerce_optional_int(
                payload.get("inactive_micros")
            ),
            "site_included_count": self._coerce_optional_int(
                payload.get("included_count")
            ),
            "site_excluded_count": self._coerce_optional_int(
                payload.get("excluded_count")
            ),
        }
        self._refresh_cached_topology()

    @staticmethod
    def _normalize_storm_guard_state(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return "enabled" if value else "disabled"
        if isinstance(value, (int, float)):
            return "enabled" if value != 0 else "disabled"
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("enabled", "disabled"):
                return normalized
            if normalized in ("true", "1", "yes", "y", "on"):
                return "enabled"
            if normalized in ("false", "0", "no", "n", "off"):
                return "disabled"
        return None

    def _clear_storm_guard_pending(self) -> None:
        self._storm_guard_pending_state = None
        self._storm_guard_pending_expires_mono = None

    def _set_storm_guard_pending(self, target_state: str) -> None:
        normalized = self._normalize_storm_guard_state(target_state)
        if normalized is None:
            self._clear_storm_guard_pending()
            return
        self._storm_guard_pending_state = normalized
        self._storm_guard_pending_expires_mono = (
            time.monotonic() + STORM_GUARD_PENDING_HOLD_S
        )

    def _sync_storm_guard_pending(self, effective_state: str | None = None) -> None:
        pending_state = getattr(self, "_storm_guard_pending_state", None)
        if pending_state is None:
            return
        if effective_state is None:
            effective_state = self._normalize_storm_guard_state(
                getattr(self, "_storm_guard_state", None)
            )
        else:
            effective_state = self._normalize_storm_guard_state(effective_state)
        if effective_state == pending_state:
            self._clear_storm_guard_pending()
            return
        expires_at = getattr(self, "_storm_guard_pending_expires_mono", None)
        if expires_at is None:
            self._clear_storm_guard_pending()
            return
        if time.monotonic() >= float(expires_at):
            self._clear_storm_guard_pending()

    def _clear_battery_pending(self) -> None:
        self._battery_pending_profile = None
        self._battery_pending_reserve = None
        self._battery_pending_sub_type = None
        self._battery_pending_requested_at = None
        self._battery_pending_require_exact_settings = True
        self._sync_battery_profile_pending_issue()

    def _set_battery_pending(
        self,
        *,
        profile: str,
        reserve: int,
        sub_type: str | None,
        require_exact_settings: bool = True,
    ) -> None:
        self._battery_pending_profile = profile
        self._battery_pending_reserve = reserve
        self._battery_pending_sub_type = (
            self._normalize_battery_sub_type(sub_type)
            if profile == "cost_savings"
            else None
        )
        self._battery_pending_requested_at = dt_util.utcnow()
        self._battery_pending_require_exact_settings = bool(require_exact_settings)
        self._sync_battery_profile_pending_issue()

    def _assert_battery_profile_write_allowed(self) -> None:
        lock = getattr(self, "_battery_profile_write_lock", None)
        if lock is not None and lock.locked():
            raise ServiceValidationError(
                "Another battery profile update is already in progress."
            )
        owner = self.battery_user_is_owner
        installer = self.battery_user_is_installer
        if owner is False and installer is False:
            raise ServiceValidationError(
                "Battery profile updates are not permitted for this account."
            )

        now = time.monotonic()
        last = getattr(self, "_battery_profile_last_write_mono", None)
        if (
            last is not None
            and now >= last
            and (now - last) < BATTERY_PROFILE_WRITE_DEBOUNCE_S
        ):
            raise ServiceValidationError(
                "Battery profile update requested too quickly. Please wait and try again."
            )

    def _normalize_battery_reserve_for_profile(self, profile: str, reserve: int) -> int:
        if profile == "backup_only":
            return 100
        min_reserve = self._battery_min_soc_floor()
        bounded = max(min_reserve, min(100, int(reserve)))
        return bounded

    def _effective_profile_matches_pending(self) -> bool:
        pending_profile = self._battery_pending_profile
        if not pending_profile:
            return False
        if self._battery_profile != pending_profile:
            return False
        if not getattr(self, "_battery_pending_require_exact_settings", True):
            return True
        if self._battery_pending_reserve is not None:
            if self._battery_backup_percentage != self._battery_pending_reserve:
                return False
        if pending_profile == "cost_savings":
            pending_subtype = self._normalize_battery_sub_type(
                self._battery_pending_sub_type
            )
            effective_subtype = self._normalize_battery_sub_type(
                self._battery_operation_mode_sub_type
            )
            if pending_subtype == SAVINGS_OPERATION_MODE_SUBTYPE:
                if effective_subtype != SAVINGS_OPERATION_MODE_SUBTYPE:
                    return False
            elif pending_subtype is None:
                # OFF/default savings payload omits subtype; backend may echo
                # an implementation-specific non-prioritize value.
                if effective_subtype == SAVINGS_OPERATION_MODE_SUBTYPE:
                    return False
            elif pending_subtype != effective_subtype:
                return False
        return True

    def _remember_battery_reserve(
        self, profile: str | None, reserve: int | None
    ) -> None:
        if not profile or reserve is None:
            return
        normalized = self._normalize_battery_profile_key(profile)
        if not normalized:
            return
        if normalized not in BATTERY_PROFILE_DEFAULT_RESERVE:
            return
        self._battery_profile_reserve_memory[normalized] = int(reserve)

    def _parse_battery_profile_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload

        profile = self._normalize_battery_profile_key(data.get("profile"))
        reserve = self._coerce_optional_int(data.get("batteryBackupPercentage"))
        subtype = self._normalize_battery_sub_type(data.get("operationModeSubType"))
        polling_interval = self._coerce_optional_int(data.get("pollingInterval"))
        supports_mqtt = self._coerce_optional_bool(data.get("supportsMqtt"))
        evse_storm_enabled = self._coerce_optional_bool(data.get("evseStormEnabled"))
        storm_state = self._normalize_storm_guard_state(data.get("stormGuardState"))
        cfg_control = data.get("cfgControl")
        if isinstance(cfg_control, dict):
            self._battery_cfg_control_show = self._coerce_optional_bool(
                cfg_control.get("show")
            )
            self._battery_cfg_control_enabled = self._coerce_optional_bool(
                cfg_control.get("enabled")
            )
            self._battery_cfg_control_schedule_supported = self._coerce_optional_bool(
                cfg_control.get("scheduleSupported")
            )
            self._battery_cfg_control_force_schedule_supported = (
                self._coerce_optional_bool(cfg_control.get("forceScheduleSupported"))
            )
        devices: list[dict[str, object]] = []
        profile_evse_device: dict[str, object] | None = None
        raw_devices = data.get("devices")
        if isinstance(raw_devices, dict):
            iq_evse = raw_devices.get("iqEvse")
            if isinstance(iq_evse, list):
                for item in iq_evse:
                    if not isinstance(item, dict):
                        continue
                    uuid = item.get("uuid")
                    if uuid is None:
                        continue
                    try:
                        uuid_text = str(uuid)
                    except Exception:  # noqa: BLE001
                        continue
                    devices.append(
                        {
                            "uuid": uuid_text,
                            "chargeMode": item.get("chargeMode"),
                            "enable": self._coerce_optional_bool(item.get("enable")),
                        }
                    )
                    if profile_evse_device is None:
                        profile_evse_device = {
                            "uuid": uuid_text,
                            "device_name": item.get("deviceName"),
                            "profile": self._normalize_battery_profile_key(
                                item.get("profile")
                            )
                            or item.get("profile"),
                            "profile_config": item.get("profileConfig"),
                            "enable": self._coerce_optional_bool(item.get("enable")),
                            "status": self._coerce_optional_int(item.get("status")),
                            "charge_mode": item.get("chargeMode"),
                            "charge_mode_status": item.get("chargeModeStatus"),
                            "updated_at": self._coerce_optional_int(
                                item.get("updatedAt")
                            ),
                        }

        if profile is not None:
            self._battery_profile = profile
        if reserve is not None:
            normalized_reserve = self._normalize_battery_reserve_for_profile(
                profile or self._battery_profile or "self-consumption",
                reserve,
            )
            self._battery_backup_percentage = normalized_reserve
            self._remember_battery_reserve(
                profile or self._battery_profile, normalized_reserve
            )
        if subtype is not None:
            self._battery_operation_mode_sub_type = subtype
        elif profile != "cost_savings":
            self._battery_operation_mode_sub_type = None
        if supports_mqtt is not None:
            self._battery_supports_mqtt = supports_mqtt
        if polling_interval is not None and polling_interval > 0:
            self._battery_polling_interval_s = polling_interval
        if storm_state is not None:
            self._storm_guard_state = storm_state
        self._sync_storm_guard_pending(storm_state)
        if evse_storm_enabled is not None:
            self._storm_evse_enabled = evse_storm_enabled
        if devices:
            self._battery_profile_devices = devices
        elif profile is not None:
            self._battery_profile_devices = []
        if profile_evse_device is not None:
            self._battery_profile_evse_device = profile_evse_device

        if self._effective_profile_matches_pending():
            self._clear_battery_pending()

    def _parse_battery_settings_payload(
        self, payload: object, *, clear_missing_schedule_times: bool = False
    ) -> None:
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload

        grid_mode = self._normalize_battery_grid_mode(data.get("batteryGridMode"))
        if grid_mode is not None:
            self._battery_grid_mode = grid_mode
        hide_cfg = self._coerce_optional_bool(data.get("hideChargeFromGrid"))
        if hide_cfg is not None:
            self._battery_hide_charge_from_grid = hide_cfg
        supports_vls = self._coerce_optional_bool(data.get("envoySupportsVls"))
        if supports_vls is not None:
            self._battery_envoy_supports_vls = supports_vls
        charge_from_grid = self._coerce_optional_bool(data.get("chargeFromGrid"))
        if charge_from_grid is not None:
            self._battery_charge_from_grid = charge_from_grid
        schedule_enabled = self._coerce_optional_bool(
            data.get("chargeFromGridScheduleEnabled")
        )
        if schedule_enabled is not None:
            self._battery_charge_from_grid_schedule_enabled = schedule_enabled
        begin = self._normalize_minutes_of_day(data.get("chargeBeginTime"))
        if begin is not None:
            self._battery_charge_begin_time = begin
        elif clear_missing_schedule_times:
            self._battery_charge_begin_time = None
        end = self._normalize_minutes_of_day(data.get("chargeEndTime"))
        if end is not None:
            self._battery_charge_end_time = end
        elif clear_missing_schedule_times:
            self._battery_charge_end_time = None
        accepted = data.get("acceptedItcDisclaimer")
        if accepted is not None:
            try:
                self._battery_accepted_itc_disclaimer = str(accepted)
            except Exception:  # noqa: BLE001
                self._battery_accepted_itc_disclaimer = None
        very_low_soc = self._coerce_optional_int(data.get("veryLowSoc"))
        if very_low_soc is not None:
            self._battery_very_low_soc = very_low_soc
        very_low_soc_min = self._coerce_optional_int(data.get("veryLowSocMin"))
        if very_low_soc_min is not None:
            self._battery_very_low_soc_min = very_low_soc_min
        very_low_soc_max = self._coerce_optional_int(data.get("veryLowSocMax"))
        if very_low_soc_max is not None:
            self._battery_very_low_soc_max = very_low_soc_max
        settings_profile = self._normalize_battery_profile_key(data.get("profile"))
        if settings_profile is not None:
            self._battery_profile = settings_profile
        settings_reserve = self._coerce_optional_int(
            data.get("batteryBackupPercentage")
        )
        if settings_reserve is not None:
            self._battery_backup_percentage = (
                self._normalize_battery_reserve_for_profile(
                    settings_profile or self._battery_profile or "self-consumption",
                    settings_reserve,
                )
            )
            self._remember_battery_reserve(
                settings_profile or self._battery_profile,
                self._battery_backup_percentage,
            )
        settings_subtype = self._normalize_battery_sub_type(
            data.get("operationModeSubType")
        )
        if settings_subtype is not None:
            self._battery_operation_mode_sub_type = settings_subtype
        elif (settings_profile or self._battery_profile) != "cost_savings":
            self._battery_operation_mode_sub_type = None
        storm_state = self._normalize_storm_guard_state(data.get("stormGuardState"))
        if storm_state is not None:
            self._storm_guard_state = storm_state
        self._sync_storm_guard_pending(storm_state)
        raw_devices = data.get("devices")
        if isinstance(raw_devices, dict):
            iq_evse = raw_devices.get("iqEvse")
            if isinstance(iq_evse, dict):
                use_battery = self._coerce_optional_bool(
                    iq_evse.get("useBatteryFrSelfConsumption")
                )
                if use_battery is not None:
                    self._battery_use_battery_for_self_consumption = use_battery

        if self._effective_profile_matches_pending():
            self._clear_battery_pending()

    def _parse_battery_site_settings(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload

        def _as_text(value) -> str | None:
            if value is None:
                return None
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                return None
            return text or None

        self._battery_show_production = self._coerce_optional_bool(
            data.get("showProduction")
        )
        self._battery_show_consumption = self._coerce_optional_bool(
            data.get("showConsumption")
        )
        self._battery_show_charge_from_grid = self._coerce_optional_bool(
            data.get("showChargeFromGrid")
        )
        self._battery_show_savings_mode = self._coerce_optional_bool(
            data.get("showSavingsMode")
        )
        self._battery_show_storm_guard = self._coerce_optional_bool(
            data.get("showStormGuard")
        )
        self._battery_show_full_backup = self._coerce_optional_bool(
            data.get("showFullBackup")
        )
        self._battery_show_battery_backup_percentage = self._coerce_optional_bool(
            data.get("showBatteryBackupPercentage")
        )
        self._battery_is_charging_modes_enabled = self._coerce_optional_bool(
            data.get("isChargingModesEnabled")
        )
        self._battery_has_encharge = self._coerce_optional_bool(data.get("hasEncharge"))
        self._battery_has_enpower = self._coerce_optional_bool(data.get("hasEnpower"))
        self._battery_country_code = _as_text(data.get("countryCode"))
        self._battery_region = _as_text(data.get("region"))
        self._battery_locale = _as_text(data.get("locale"))
        self._battery_timezone = _as_text(data.get("timezone"))
        grid_mode = self._normalize_battery_grid_mode(data.get("batteryGridMode"))
        if grid_mode is not None:
            self._battery_grid_mode = grid_mode
        raw_feature_details = data.get("featureDetails")
        feature_details: dict[str, object] = {}
        if isinstance(raw_feature_details, dict):
            for key, value in raw_feature_details.items():
                key_text = _as_text(key)
                if not key_text:
                    continue
                normalized_bool = self._coerce_optional_bool(value)
                if normalized_bool is not None:
                    feature_details[key_text] = normalized_bool
                    continue
                if isinstance(value, (str, int, float)):
                    feature_details[key_text] = value
        self._battery_feature_details = feature_details
        user_details = data.get("userDetails")
        if isinstance(user_details, dict):
            owner = self._coerce_optional_bool(user_details.get("isOwner"))
            installer = self._coerce_optional_bool(user_details.get("isInstaller"))
            if owner is not None:
                self._battery_user_is_owner = owner
            if installer is not None:
                self._battery_user_is_installer = installer
        site_status = data.get("siteStatus")
        if isinstance(site_status, dict):
            self._battery_site_status_code = _as_text(site_status.get("code"))
            self._battery_site_status_text = _as_text(site_status.get("text"))
            self._battery_site_status_severity = _as_text(site_status.get("severity"))

    @staticmethod
    def _copy_dry_contact_settings_entry(entry: dict[str, object]) -> dict[str, object]:
        copied: dict[str, object] = {}
        for key, value in entry.items():
            if isinstance(value, dict):
                copied[key] = dict(value)
            elif isinstance(value, list):
                copied[key] = [
                    dict(item) if isinstance(item, dict) else item for item in value
                ]
            else:
                copied[key] = value
        return copied

    @classmethod
    def _normalize_dry_contact_schedule_windows(
        cls, value: object
    ) -> list[dict[str, object]]:
        if isinstance(value, list):
            candidates = [item for item in value if isinstance(item, dict)]
        elif isinstance(value, dict):
            candidates = [value]
        else:
            return []
        windows: list[dict[str, object]] = []
        seen: set[tuple[str | None, str | None]] = set()
        for item in candidates:
            start = cls._coerce_optional_text(
                item.get("start")
                if item.get("start") is not None
                else (
                    item.get("startTime")
                    if item.get("startTime") is not None
                    else (
                        item.get("begin")
                        if item.get("begin") is not None
                        else (
                            item.get("beginTime")
                            if item.get("beginTime") is not None
                            else (
                                item.get("from")
                                if item.get("from") is not None
                                else item.get("windowStart")
                            )
                        )
                    )
                )
            )
            end = cls._coerce_optional_text(
                item.get("end")
                if item.get("end") is not None
                else (
                    item.get("endTime")
                    if item.get("endTime") is not None
                    else (
                        item.get("finish")
                        if item.get("finish") is not None
                        else (
                            item.get("finishTime")
                            if item.get("finishTime") is not None
                            else (
                                item.get("to")
                                if item.get("to") is not None
                                else item.get("windowEnd")
                            )
                        )
                    )
                )
            )
            if start is None and end is None:
                continue
            key = (start, end)
            if key in seen:
                continue
            seen.add(key)
            window: dict[str, object] = {}
            if start is not None:
                window["start"] = start
            if end is not None:
                window["end"] = end
            windows.append(window)
        return windows

    @classmethod
    def _dry_contact_settings_looks_like_entry(cls, value: object) -> bool:
        if not isinstance(value, dict):
            return False
        keys = (
            "serial_number",
            "serial",
            "serialNumber",
            "device_uid",
            "device-uid",
            "deviceUid",
            "uid",
            "contact_id",
            "contactId",
            "id",
            "channel_type",
            "channelType",
            "meter_type",
            "name",
            "displayName",
            "configuredName",
            "overrideSupported",
            "overrideActive",
            "controlMode",
            "pollingInterval",
            "pollingIntervalSeconds",
            "socThreshold",
            "socThresholdMin",
            "socThresholdMax",
            "scheduleWindows",
            "schedule_windows",
            "schedule",
            "schedules",
            "windows",
        )
        return any(key in value for key in keys)

    @classmethod
    def _dry_contact_identity_candidates(
        cls, value: dict[str, object]
    ) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []

        def _add(identity_key: str, raw_value: object) -> None:
            text = cls._coerce_optional_text(raw_value)
            if text is None:
                return
            candidates.append((identity_key, text.casefold()))

        _add(
            "serial_number",
            (
                value.get("serial_number")
                if value.get("serial_number") is not None
                else (
                    value.get("serial")
                    if value.get("serial") is not None
                    else value.get("serialNumber")
                )
            ),
        )
        _add(
            "device_uid",
            (
                value.get("device_uid")
                if value.get("device_uid") is not None
                else (
                    value.get("device-uid")
                    if value.get("device-uid") is not None
                    else (
                        value.get("deviceUid")
                        if value.get("deviceUid") is not None
                        else (
                            value.get("iqer_uid")
                            if value.get("iqer_uid") is not None
                            else value.get("iqer-uid")
                        )
                    )
                )
            ),
        )
        _add("uid", value.get("uid"))
        _add(
            "contact_id",
            (
                value.get("contact_id")
                if value.get("contact_id") is not None
                else (
                    value.get("contactId")
                    if value.get("contactId") is not None
                    else value.get("id")
                )
            ),
        )
        _add(
            "channel_type",
            (
                value.get("channel_type")
                if value.get("channel_type") is not None
                else (
                    value.get("channelType")
                    if value.get("channelType") is not None
                    else (
                        value.get("meter_type")
                        if value.get("meter_type") is not None
                        else value.get("type")
                    )
                )
            ),
        )
        _add(
            "name",
            (
                value.get("configured_name")
                if value.get("configured_name") is not None
                else (
                    value.get("display_name")
                    if value.get("display_name") is not None
                    else (
                        value.get("name")
                        if value.get("name") is not None
                        else (
                            value.get("displayName")
                            if value.get("displayName") is not None
                            else (
                                value.get("configuredName")
                                if value.get("configuredName") is not None
                                else value.get("label")
                            )
                        )
                    )
                )
            ),
        )
        return candidates

    @classmethod
    def _dry_contact_identity_map(cls, value: dict[str, object]) -> dict[str, str]:
        return dict(cls._dry_contact_identity_candidates(value))

    @staticmethod
    def _dry_contact_member_dedupe_key(
        identities: dict[str, str], index: int
    ) -> tuple[tuple[str, str], ...]:
        for keys in (
            ("device_uid", "contact_id"),
            ("device_uid", "channel_type"),
            ("uid", "contact_id"),
            ("uid", "channel_type"),
            ("contact_id", "channel_type"),
            ("serial_number", "channel_type"),
            ("serial_number", "contact_id"),
            ("contact_id",),
            ("channel_type",),
            ("serial_number",),
            ("device_uid",),
            ("uid",),
            ("name",),
        ):
            if all(identities.get(key) is not None for key in keys):
                return tuple((key, identities[key]) for key in keys)
        return (("idx", str(index)),)

    @staticmethod
    def _dry_contact_match_conflicts(
        member_identities: dict[str, str],
        entry_identities: dict[str, str],
    ) -> bool:
        for key in (
            "contact_id",
            "channel_type",
            "serial_number",
            "device_uid",
            "uid",
        ):
            member_value = member_identities.get(key)
            entry_value = entry_identities.get(key)
            if member_value is None or entry_value is None:
                continue
            if member_value != entry_value:
                return True
        return False

    @classmethod
    def _dry_contact_member_is_dry_contact(cls, member: object) -> bool:
        if not isinstance(member, dict):
            return False
        for key in ("channel_type", "channelType", "meter_type", "type", "name"):
            value = cls._coerce_optional_text(member.get(key))
            if value is None:
                continue
            compact = (
                value.casefold().replace("-", "").replace("_", "").replace(" ", "")
            )
            if compact in {"nc1", "nc2", "no1", "no2"}:
                return True
            if "drycontact" in compact:
                return True
            if "relay" in compact and any(token in compact for token in ("nc", "no")):
                return True
        return False

    def _dry_contact_members_for_settings(self) -> list[dict[str, object]]:
        members: list[dict[str, object]] = []
        seen_keys: set[tuple[tuple[str, str], ...]] = set()

        def _append_member(raw_member: object) -> None:
            if not isinstance(raw_member, dict):
                return
            if member_is_retired(raw_member):
                return
            sanitized = sanitize_member(raw_member)
            if not sanitized:
                return
            identities = self._dry_contact_identity_map(sanitized)
            key = self._dry_contact_member_dedupe_key(identities, len(members))
            if key in seen_keys:
                return
            seen_keys.add(key)
            members.append(sanitized)

        buckets = getattr(self, "_type_device_buckets", None)
        envoy_bucket = buckets.get("envoy", {}) if isinstance(buckets, dict) else {}
        envoy_members = (
            envoy_bucket.get("devices") if isinstance(envoy_bucket, dict) else None
        )
        if isinstance(envoy_members, list):
            for member in envoy_members:
                if self._dry_contact_member_is_dry_contact(member):
                    _append_member(member)

        dry_bucket = buckets.get("dry_contact", {}) if isinstance(buckets, dict) else {}
        dry_members = (
            dry_bucket.get("devices") if isinstance(dry_bucket, dict) else None
        )
        if isinstance(dry_members, list):
            for member in dry_members:
                _append_member(member)

        return members

    def _match_dry_contact_settings(
        self,
        members: Iterable[dict[str, object]],
        *,
        settings_entries: list[dict[str, object]] | None = None,
    ) -> tuple[list[dict[str, object] | None], list[dict[str, object]]]:
        members_list = [dict(member) for member in members if isinstance(member, dict)]
        member_identity_maps = [
            self._dry_contact_identity_map(member) for member in members_list
        ]
        index_by_key: dict[str, dict[str, list[int]]] = {
            key: {}
            for key in (
                "contact_id",
                "channel_type",
                "serial_number",
                "device_uid",
                "uid",
                "name",
            )
        }
        for index, identities in enumerate(member_identity_maps):
            for key, mapping in index_by_key.items():
                value = identities.get(key)
                if value is None:
                    continue
                mapping.setdefault(value, []).append(index)

        entries = [
            self._copy_dry_contact_settings_entry(entry)
            for entry in (
                settings_entries
                if settings_entries is not None
                else getattr(self, "_dry_contact_settings_entries", [])
            )
            if isinstance(entry, dict)
        ]
        matches: list[dict[str, object] | None] = [None] * len(members_list)
        unmatched: list[dict[str, object]] = []
        used_member_indexes: set[int] = set()

        for entry in entries:
            entry_identities = self._dry_contact_identity_map(entry)
            matched_member_index: int | None = None
            for key in (
                "contact_id",
                "channel_type",
                "serial_number",
                "device_uid",
                "uid",
                "name",
            ):
                value = entry_identities.get(key)
                if value is None:
                    continue
                candidate_indexes = [
                    index
                    for index in index_by_key[key].get(value, [])
                    if index not in used_member_indexes
                ]
                if len(candidate_indexes) != 1:
                    continue
                candidate_index = candidate_indexes[0]
                if self._dry_contact_match_conflicts(
                    member_identity_maps[candidate_index], entry_identities
                ):
                    continue
                matched_member_index = candidate_index
                break
            if matched_member_index is None:
                unmatched.append(entry)
                continue
            used_member_indexes.add(matched_member_index)
            matches[matched_member_index] = entry
        return matches, unmatched

    def dry_contact_settings_matches(
        self, members: Iterable[dict[str, object]]
    ) -> tuple[list[dict[str, object] | None], list[dict[str, object]]]:
        matches, unmatched = self._match_dry_contact_settings(members)
        return (
            [
                (
                    self._copy_dry_contact_settings_entry(entry)
                    if isinstance(entry, dict)
                    else None
                )
                for entry in matches
            ],
            [
                self._copy_dry_contact_settings_entry(entry)
                for entry in unmatched
                if isinstance(entry, dict)
            ],
        )

    def _parse_dry_contact_settings_payload(self, payload: object) -> None:
        self._dry_contact_settings_entries = []
        self._dry_contact_unmatched_settings = []
        if not isinstance(payload, dict):
            self._dry_contact_settings_supported = False
            return

        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload

        raw_entries: list[dict[str, object]] = []
        visited: set[int] = set()

        def _visit(node: object, depth: int = 0) -> None:
            if depth > 4:
                return
            if isinstance(node, dict):
                node_id = id(node)
                if node_id in visited:
                    return
                visited.add(node_id)
                if self._dry_contact_settings_looks_like_entry(node):
                    raw_entries.append(node)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        _visit(value, depth + 1)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, (dict, list)):
                        _visit(item, depth + 1)

        _visit(data)

        entries: list[dict[str, object]] = []
        seen_signatures: set[tuple[object, ...]] = set()
        for entry in raw_entries:
            normalized: dict[str, object] = {}
            serial_number = self._coerce_optional_text(
                entry.get("serial_number")
                if entry.get("serial_number") is not None
                else (
                    entry.get("serial")
                    if entry.get("serial") is not None
                    else (
                        entry.get("serialNumber")
                        if entry.get("serialNumber") is not None
                        else entry.get("deviceSerial")
                    )
                )
            )
            if serial_number is not None:
                normalized["serial_number"] = serial_number
            device_uid = self._coerce_optional_text(
                entry.get("device_uid")
                if entry.get("device_uid") is not None
                else (
                    entry.get("device-uid")
                    if entry.get("device-uid") is not None
                    else (
                        entry.get("deviceUid")
                        if entry.get("deviceUid") is not None
                        else (
                            entry.get("iqer_uid")
                            if entry.get("iqer_uid") is not None
                            else entry.get("iqer-uid")
                        )
                    )
                )
            )
            if device_uid is not None:
                normalized["device_uid"] = device_uid
            uid = self._coerce_optional_text(entry.get("uid"))
            if uid is not None:
                normalized["uid"] = uid
            contact_id = self._coerce_optional_text(
                entry.get("contact_id")
                if entry.get("contact_id") is not None
                else (
                    entry.get("contactId")
                    if entry.get("contactId") is not None
                    else entry.get("id")
                )
            )
            if contact_id is not None:
                normalized["contact_id"] = contact_id
            channel_type = self._coerce_optional_text(
                entry.get("channel_type")
                if entry.get("channel_type") is not None
                else (
                    entry.get("channelType")
                    if entry.get("channelType") is not None
                    else (
                        entry.get("meter_type")
                        if entry.get("meter_type") is not None
                        else entry.get("type")
                    )
                )
            )
            if channel_type is not None:
                normalized["channel_type"] = channel_type
            configured_name = self._coerce_optional_text(
                entry.get("configured_name")
                if entry.get("configured_name") is not None
                else (
                    entry.get("configuredName")
                    if entry.get("configuredName") is not None
                    else (
                        entry.get("display_name")
                        if entry.get("display_name") is not None
                        else (
                            entry.get("displayName")
                            if entry.get("displayName") is not None
                            else (
                                entry.get("name")
                                if entry.get("name") is not None
                                else entry.get("label")
                            )
                        )
                    )
                )
            )
            if configured_name is not None:
                normalized["configured_name"] = configured_name
            override_supported = self._coerce_optional_bool(
                entry.get("override_supported")
                if entry.get("override_supported") is not None
                else (
                    entry.get("overrideSupported")
                    if entry.get("overrideSupported") is not None
                    else (
                        entry.get("isOverrideSupported")
                        if entry.get("isOverrideSupported") is not None
                        else (
                            entry.get("supportsOverride")
                            if entry.get("supportsOverride") is not None
                            else (
                                entry.get("allowOverride")
                                if entry.get("allowOverride") is not None
                                else entry.get("canOverride")
                            )
                        )
                    )
                )
            )
            if override_supported is not None:
                normalized["override_supported"] = override_supported
            override_active = self._coerce_optional_bool(
                entry.get("override_active")
                if entry.get("override_active") is not None
                else (
                    entry.get("overrideActive")
                    if entry.get("overrideActive") is not None
                    else (
                        entry.get("override")
                        if entry.get("override") is not None
                        else (
                            entry.get("isOverrideActive")
                            if entry.get("isOverrideActive") is not None
                            else entry.get("manualOverride")
                        )
                    )
                )
            )
            if override_active is not None:
                normalized["override_active"] = override_active
            control_mode = self._coerce_optional_text(
                entry.get("control_mode")
                if entry.get("control_mode") is not None
                else (
                    entry.get("controlMode")
                    if entry.get("controlMode") is not None
                    else (
                        entry.get("mode")
                        if entry.get("mode") is not None
                        else entry.get("operatingMode")
                    )
                )
            )
            if control_mode is not None:
                normalized["control_mode"] = control_mode
            polling_interval = self._coerce_optional_int(
                entry.get("polling_interval_seconds")
                if entry.get("polling_interval_seconds") is not None
                else (
                    entry.get("pollingIntervalSeconds")
                    if entry.get("pollingIntervalSeconds") is not None
                    else (
                        entry.get("pollingInterval")
                        if entry.get("pollingInterval") is not None
                        else entry.get("polling_interval")
                    )
                )
            )
            if polling_interval is not None:
                normalized["polling_interval_seconds"] = polling_interval
            soc_threshold = self._coerce_optional_int(
                entry.get("soc_threshold")
                if entry.get("soc_threshold") is not None
                else (
                    entry.get("socThreshold")
                    if entry.get("socThreshold") is not None
                    else (
                        entry.get("thresholdSoc")
                        if entry.get("thresholdSoc") is not None
                        else (
                            entry.get("targetSoc")
                            if entry.get("targetSoc") is not None
                            else (
                                entry.get("setPointSoc")
                                if entry.get("setPointSoc") is not None
                                else entry.get("soc")
                            )
                        )
                    )
                )
            )
            if soc_threshold is not None:
                normalized["soc_threshold"] = soc_threshold
            soc_threshold_min = self._coerce_optional_int(
                entry.get("soc_threshold_min")
                if entry.get("soc_threshold_min") is not None
                else (
                    entry.get("socThresholdMin")
                    if entry.get("socThresholdMin") is not None
                    else (
                        entry.get("minimumSoc")
                        if entry.get("minimumSoc") is not None
                        else (
                            entry.get("minSoc")
                            if entry.get("minSoc") is not None
                            else entry.get("minSocThreshold")
                        )
                    )
                )
            )
            if soc_threshold_min is not None:
                normalized["soc_threshold_min"] = soc_threshold_min
            soc_threshold_max = self._coerce_optional_int(
                entry.get("soc_threshold_max")
                if entry.get("soc_threshold_max") is not None
                else (
                    entry.get("socThresholdMax")
                    if entry.get("socThresholdMax") is not None
                    else (
                        entry.get("maximumSoc")
                        if entry.get("maximumSoc") is not None
                        else (
                            entry.get("maxSoc")
                            if entry.get("maxSoc") is not None
                            else entry.get("maxSocThreshold")
                        )
                    )
                )
            )
            if soc_threshold_max is not None:
                normalized["soc_threshold_max"] = soc_threshold_max
            schedule_windows = self._normalize_dry_contact_schedule_windows(
                entry.get("schedule_windows")
                if entry.get("schedule_windows") is not None
                else (
                    entry.get("scheduleWindows")
                    if entry.get("scheduleWindows") is not None
                    else (
                        entry.get("schedule")
                        if entry.get("schedule") is not None
                        else (
                            entry.get("schedules")
                            if entry.get("schedules") is not None
                            else (
                                entry.get("windows")
                                if entry.get("windows") is not None
                                else entry.get("window")
                            )
                        )
                    )
                )
            )
            if not schedule_windows:
                fallback_start = self._coerce_optional_text(
                    entry.get("scheduleStart")
                    if entry.get("scheduleStart") is not None
                    else (
                        entry.get("schedule_start")
                        if entry.get("schedule_start") is not None
                        else (
                            entry.get("windowStart")
                            if entry.get("windowStart") is not None
                            else entry.get("startTime")
                        )
                    )
                )
                fallback_end = self._coerce_optional_text(
                    entry.get("scheduleEnd")
                    if entry.get("scheduleEnd") is not None
                    else (
                        entry.get("schedule_end")
                        if entry.get("schedule_end") is not None
                        else (
                            entry.get("windowEnd")
                            if entry.get("windowEnd") is not None
                            else entry.get("endTime")
                        )
                    )
                )
                if fallback_start is not None or fallback_end is not None:
                    schedule_window: dict[str, object] = {}
                    if fallback_start is not None:
                        schedule_window["start"] = fallback_start
                    if fallback_end is not None:
                        schedule_window["end"] = fallback_end
                    schedule_windows = [schedule_window]
            if schedule_windows:
                normalized["schedule_windows"] = schedule_windows
            if not normalized:
                continue
            signature = (
                normalized.get("serial_number"),
                normalized.get("device_uid"),
                normalized.get("uid"),
                normalized.get("contact_id"),
                normalized.get("channel_type"),
                normalized.get("configured_name"),
                normalized.get("override_supported"),
                normalized.get("override_active"),
                normalized.get("control_mode"),
                normalized.get("polling_interval_seconds"),
                normalized.get("soc_threshold"),
                normalized.get("soc_threshold_min"),
                normalized.get("soc_threshold_max"),
                tuple(
                    (
                        window.get("start") if isinstance(window, dict) else None,
                        window.get("end") if isinstance(window, dict) else None,
                    )
                    for window in normalized.get("schedule_windows", [])
                    if isinstance(window, dict)
                ),
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            entries.append(normalized)

        matches, unmatched = self._match_dry_contact_settings(
            self._dry_contact_members_for_settings(),
            settings_entries=entries,
        )
        _ = matches
        self._dry_contact_settings_entries = entries
        self._dry_contact_unmatched_settings = unmatched
        self._dry_contact_settings_supported = True

    def _parse_grid_control_check_payload(self, payload: object) -> None:
        keys = (
            "disableGridControl",
            "activeDownload",
            "sunlightBackupSystemCheck",
            "gridOutageCheck",
            "userInitiatedGridToggle",
        )
        if not isinstance(payload, dict):
            self._grid_control_supported = False
            self._grid_control_disable = None
            self._grid_control_active_download = None
            self._grid_control_sunlight_backup_system_check = None
            self._grid_control_grid_outage_check = None
            self._grid_control_user_initiated_toggle = None
            return

        data = payload.get("data")
        if isinstance(data, dict):
            payload = data

        has_any = any(key in payload for key in keys)
        if not has_any:
            self._grid_control_supported = False
            self._grid_control_disable = None
            self._grid_control_active_download = None
            self._grid_control_sunlight_backup_system_check = None
            self._grid_control_grid_outage_check = None
            self._grid_control_user_initiated_toggle = None
            return

        self._grid_control_supported = True
        self._grid_control_disable = self._coerce_optional_bool(
            payload.get("disableGridControl")
        )
        self._grid_control_active_download = self._coerce_optional_bool(
            payload.get("activeDownload")
        )
        self._grid_control_sunlight_backup_system_check = self._coerce_optional_bool(
            payload.get("sunlightBackupSystemCheck")
        )
        self._grid_control_grid_outage_check = self._coerce_optional_bool(
            payload.get("gridOutageCheck")
        )
        self._grid_control_user_initiated_toggle = self._coerce_optional_bool(
            payload.get("userInitiatedGridToggle")
        )

    def _battery_profile_devices_payload(self) -> list[dict[str, object]] | None:
        if not self._battery_profile_devices:
            return None
        payload: list[dict[str, object]] = []
        seen_uuids: set[str] = set()
        for item in self._battery_profile_devices:
            uuid = item.get("uuid")
            if uuid is None:
                continue
            try:
                uuid_text = str(uuid).strip()
            except Exception:  # noqa: BLE001
                continue
            if not uuid_text or uuid_text in seen_uuids:
                continue
            seen_uuids.add(uuid_text)
            entry: dict[str, object] = {
                "uuid": uuid_text,
                "deviceType": "iqEvse",
            }
            enabled = self._coerce_optional_bool(item.get("enable"))
            if enabled is not None:
                entry["enable"] = enabled
            charge_mode = item.get("chargeMode")
            if charge_mode is not None:
                entry["chargeMode"] = str(charge_mode)
            payload.append(entry)
        return payload or None

    def _target_reserve_for_profile(self, profile: str) -> int:
        if profile == "backup_only":
            return 100
        remembered = self._battery_profile_reserve_memory.get(profile)
        if remembered is not None:
            return self._normalize_battery_reserve_for_profile(profile, remembered)
        default = BATTERY_PROFILE_DEFAULT_RESERVE.get(profile, 20)
        return self._normalize_battery_reserve_for_profile(profile, default)

    def _current_savings_sub_type(self) -> str | None:
        selected_subtype = self.battery_selected_operation_mode_sub_type
        if selected_subtype is None:
            return None
        if selected_subtype == SAVINGS_OPERATION_MODE_SUBTYPE:
            return SAVINGS_OPERATION_MODE_SUBTYPE
        return None

    async def _async_apply_battery_profile(
        self,
        *,
        profile: str,
        reserve: int,
        sub_type: str | None = None,
        require_exact_pending_match: bool = True,
    ) -> None:
        self._assert_battery_profile_write_allowed()
        normalized_profile = self._normalize_battery_profile_key(profile)
        if not normalized_profile:
            raise ServiceValidationError("Battery profile is unavailable.")
        normalized_reserve = self._normalize_battery_reserve_for_profile(
            normalized_profile, reserve
        )
        normalized_sub_type = (
            self._normalize_battery_sub_type(sub_type)
            if normalized_profile == "cost_savings"
            else None
        )
        async with self._battery_profile_write_lock:
            self._battery_profile_last_write_mono = time.monotonic()
            try:
                await self.client.set_battery_profile(
                    profile=normalized_profile,
                    battery_backup_percentage=normalized_reserve,
                    operation_mode_sub_type=normalized_sub_type,
                    devices=self._battery_profile_devices_payload(),
                )
            except aiohttp.ClientResponseError as err:
                if err.status == HTTPStatus.FORBIDDEN:
                    owner = self.battery_user_is_owner
                    installer = self.battery_user_is_installer
                    if owner is False and installer is False:
                        raise ServiceValidationError(
                            "Battery profile updates are not permitted for this account."
                        ) from err
                    raise ServiceValidationError(
                        "Battery profile update was rejected by Enphase (HTTP 403 Forbidden)."
                    ) from err
                if err.status == HTTPStatus.UNAUTHORIZED:
                    raise ServiceValidationError(
                        "Battery profile update could not be authenticated. Reauthenticate and try again."
                    ) from err
                raise
        self._remember_battery_reserve(normalized_profile, normalized_reserve)
        self._set_battery_pending(
            profile=normalized_profile,
            reserve=normalized_reserve,
            sub_type=normalized_sub_type,
            require_exact_settings=require_exact_pending_match,
        )
        self._storm_guard_cache_until = None
        self._battery_settings_cache_until = None
        self.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self.async_request_refresh()

    def _assert_battery_settings_write_allowed(self) -> None:
        lock = getattr(self, "_battery_settings_write_lock", None)
        if lock is not None and lock.locked():
            raise ServiceValidationError(
                "Another battery settings update is already in progress."
            )
        owner = self.battery_user_is_owner
        installer = self.battery_user_is_installer
        if owner is False and installer is False:
            raise ServiceValidationError(
                "Battery settings updates are not permitted for this account."
            )
        now = time.monotonic()
        last = getattr(self, "_battery_settings_last_write_mono", None)
        if (
            last is not None
            and now >= last
            and (now - last) < BATTERY_SETTINGS_WRITE_DEBOUNCE_S
        ):
            raise ServiceValidationError(
                "Battery settings update requested too quickly. Please wait and try again."
            )

    def _current_charge_from_grid_schedule_window(self) -> tuple[int, int]:
        begin = self._normalize_minutes_of_day(self._battery_charge_begin_time)
        end = self._normalize_minutes_of_day(self._battery_charge_end_time)
        if begin is None:
            begin = 120
        if end is None:
            end = 300
        return begin, end

    def _battery_itc_disclaimer_value(self) -> str:
        current = getattr(self, "_battery_accepted_itc_disclaimer", None)
        if current:
            return current
        return dt_util.utcnow().isoformat()

    async def _async_apply_battery_settings(self, payload: dict[str, object]) -> None:
        if not isinstance(payload, dict) or not payload:
            raise ServiceValidationError("Battery settings payload is unavailable.")
        self._assert_battery_settings_write_allowed()
        async with self._battery_settings_write_lock:
            self._battery_settings_last_write_mono = time.monotonic()
            try:
                await self.client.set_battery_settings(payload)
            except aiohttp.ClientResponseError as err:
                if err.status == HTTPStatus.FORBIDDEN:
                    raise ServiceValidationError(
                        "Battery settings update was rejected by Enphase (HTTP 403 Forbidden)."
                    ) from err
                if err.status == HTTPStatus.UNAUTHORIZED:
                    raise ServiceValidationError(
                        "Battery settings update could not be authenticated. Reauthenticate and try again."
                    ) from err
                raise
        self._parse_battery_settings_payload(payload)
        self._battery_settings_cache_until = None
        self.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self.async_request_refresh()

    async def _async_refresh_battery_status(self, *, force: bool = False) -> None:
        _ = force
        fetcher = getattr(self.client, "battery_status", None)
        if not callable(fetcher):
            return
        payload = await fetcher()
        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._battery_status_payload = redacted_payload
        else:
            self._battery_status_payload = {"value": redacted_payload}
        self._parse_battery_status_payload(payload)

    def _backup_history_tzinfo(self) -> _tz | ZoneInfo:
        tz_name = getattr(self, "_battery_timezone", None)
        if isinstance(tz_name, str) and tz_name.strip():
            try:
                return ZoneInfo(tz_name.strip())
            except Exception:  # noqa: BLE001
                pass
        default_tz = getattr(dt_util, "DEFAULT_TIME_ZONE", None)
        if default_tz is not None:
            return default_tz
        return _tz.utc

    def _parse_battery_backup_history_payload(
        self,
        payload: object,
    ) -> list[dict[str, object]] | None:
        if not isinstance(payload, dict):
            return None
        histories = payload.get("histories")
        if not isinstance(histories, list):
            return None
        total_records = self._coerce_int(payload.get("total_records"), default=-1)
        total_backup = self._coerce_int(payload.get("total_backup"), default=-1)
        events: list[dict[str, object]] = []
        for item in histories:
            if not isinstance(item, dict):
                continue
            try:
                duration = int(item.get("duration"))
            except (TypeError, ValueError):
                continue
            if duration <= 0:
                continue
            start_raw = item.get("start_time")
            if start_raw is None:
                continue
            try:
                start_text = str(start_raw).strip()
            except Exception:  # noqa: BLE001
                continue
            if not start_text:
                continue
            start = dt_util.parse_datetime(start_text)
            if start is None:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=self._backup_history_tzinfo())
            end = start + timedelta(seconds=duration)
            events.append(
                {
                    "start": start,
                    "end": end,
                    "duration_seconds": duration,
                }
            )
        events.sort(key=lambda item: item["start"])
        if total_records >= 0 and total_records != len(events):
            _LOGGER.debug(
                "Battery backup history total_records mismatch for site %s (payload=%s parsed=%s)",
                redact_site_id(self.site_id),
                total_records,
                len(events),
            )
        if total_backup >= 0:
            parsed_total_backup = sum(int(item["duration_seconds"]) for item in events)
            if total_backup != parsed_total_backup:
                _LOGGER.debug(
                    "Battery backup history total_backup mismatch for site %s (payload=%s parsed=%s)",
                    redact_site_id(self.site_id),
                    total_backup,
                    parsed_total_backup,
                )
        return events

    async def _async_refresh_battery_backup_history(
        self, *, force: bool = False
    ) -> None:
        now = time.monotonic()
        if not force and self._battery_backup_history_cache_until:
            if now < self._battery_backup_history_cache_until:
                return
        fetcher = getattr(self.client, "battery_backup_history", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Battery backup history fetch failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            self._battery_backup_history_cache_until = (
                now + BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL
            )
            return
        parsed = self._parse_battery_backup_history_payload(payload)
        if parsed is None:
            _LOGGER.debug(
                "Battery backup history payload was invalid for site %s",
                redact_site_id(self.site_id),
            )
            self._battery_backup_history_cache_until = (
                now + BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL
            )
            return
        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._battery_backup_history_payload = redacted_payload
        else:
            self._battery_backup_history_payload = {"value": redacted_payload}
        self._battery_backup_history_events = parsed
        self._battery_backup_history_cache_until = (
            now + BATTERY_BACKUP_HISTORY_CACHE_TTL
        )

    async def _async_refresh_battery_settings(self, *, force: bool = False) -> None:
        now = time.monotonic()
        pending_profile = getattr(self, "_battery_pending_profile", None)
        if not force and not pending_profile and self._battery_settings_cache_until:
            if now < self._battery_settings_cache_until:
                return
        fetcher = getattr(self.client, "battery_settings_details", None)
        if not callable(fetcher):
            return
        payload = await fetcher()
        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._battery_settings_payload = redacted_payload
        else:
            self._battery_settings_payload = {"value": redacted_payload}
        self._parse_battery_settings_payload(payload, clear_missing_schedule_times=True)
        self._battery_settings_cache_until = now + BATTERY_SETTINGS_CACHE_TTL

    async def _async_refresh_battery_schedules(self) -> None:
        """Fetch battery schedules from the newer /schedules endpoint.

        Overrides legacy chargeBeginTime/chargeEndTime with accurate data
        and populates the CFG schedule limit (max SoC).
        """

        fetcher = getattr(self.client, "battery_schedules", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Battery schedules fetch failed: %s",
                redact_text(err, site_ids=(self.site_id,)),
            )
            return
        if not isinstance(payload, dict):
            return

        redacted = self._redact_battery_payload(payload)
        if isinstance(redacted, dict):
            self._battery_schedules_payload = redacted
        else:
            self._battery_schedules_payload = {"value": redacted}

        self._parse_battery_schedules_payload(payload)

    def _parse_battery_schedules_payload(self, payload: object) -> None:
        """Parse the /schedules response and update schedule times + limit.

        The payload has ``cfg``, ``dtg``, and ``rbd`` families.  Each has
        a ``details`` list with ``startTime`` (HH:MM), ``endTime`` (HH:MM),
        ``limit`` (int, 0-100), ``days`` (list[int]), and ``isEnabled`` (bool).

        We use the first enabled CFG schedule to override the legacy
        chargeBeginTime / chargeEndTime and to populate the charge limit.
        """

        self._battery_cfg_schedule_limit = None
        self._battery_cfg_schedule_id = None
        self._battery_cfg_schedule_days = None
        self._battery_cfg_schedule_timezone = None

        if not isinstance(payload, dict):
            return

        cfg = payload.get("cfg")
        if not isinstance(cfg, dict):
            return
        details = cfg.get("details")
        if not isinstance(details, list) or not details:
            return

        # Find the first enabled CFG schedule (or the first one if none enabled)
        chosen = None
        for entry in details:
            if not isinstance(entry, dict):
                continue
            if chosen is None:
                chosen = entry
            if entry.get("isEnabled") is True:
                chosen = entry
                break
        if chosen is None:
            return

        start_str = chosen.get("startTime")
        end_str = chosen.get("endTime")
        limit = chosen.get("limit")
        schedule_id = chosen.get("scheduleId")
        days = chosen.get("days")

        if isinstance(start_str, str) and ":" in start_str:
            try:
                parts = start_str.split(":")
                minutes = int(parts[0]) * 60 + int(parts[1])
                self._battery_charge_begin_time = minutes
            except (ValueError, IndexError):
                pass

        if isinstance(end_str, str) and ":" in end_str:
            try:
                parts = end_str.split(":")
                minutes = int(parts[0]) * 60 + int(parts[1])
                self._battery_charge_end_time = minutes
            except (ValueError, IndexError):
                pass

        if schedule_id is not None:
            self._battery_cfg_schedule_id = str(schedule_id)
        if isinstance(days, list):
            self._battery_cfg_schedule_days = [int(d) for d in days]

        # Store timezone from the schedule entry first, then top-level payload.
        tz = chosen.get("timezone")
        if not isinstance(tz, str) or not tz.strip():
            tz = payload.get("timezone") if isinstance(payload, dict) else None
        if isinstance(tz, str) and tz.strip():
            self._battery_cfg_schedule_timezone = tz.strip()

        if isinstance(limit, (int, float)):
            self._battery_cfg_schedule_limit = int(limit)

    async def _async_refresh_battery_site_settings(
        self, *, force: bool = False
    ) -> None:
        now = time.monotonic()
        if not force and self._battery_site_settings_cache_until:
            if now < self._battery_site_settings_cache_until:
                return
        fetcher = getattr(self.client, "battery_site_settings", None)
        if not callable(fetcher):
            return
        payload = await fetcher()
        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._battery_site_settings_payload = redacted_payload
        else:
            self._battery_site_settings_payload = {"value": redacted_payload}
        self._parse_battery_site_settings(payload)
        self._battery_site_settings_cache_until = now + BATTERY_SITE_SETTINGS_CACHE_TTL

    async def _async_refresh_grid_control_check(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._grid_control_check_cache_until:
            if now < self._grid_control_check_cache_until:
                return
        fetcher = getattr(self.client, "grid_control_check", None)
        if not callable(fetcher):
            self._grid_control_supported = None
            self._grid_control_disable = None
            self._grid_control_active_download = None
            self._grid_control_sunlight_backup_system_check = None
            self._grid_control_grid_outage_check = None
            self._grid_control_user_initiated_toggle = None
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            self._grid_control_check_failures += 1
            last_success = getattr(self, "_grid_control_check_last_success_mono", None)
            if (
                not isinstance(last_success, (int, float))
                or (now - float(last_success)) >= GRID_CONTROL_CHECK_STALE_AFTER_S
            ):
                self._grid_control_supported = None
                self._grid_control_disable = None
                self._grid_control_active_download = None
                self._grid_control_sunlight_backup_system_check = None
                self._grid_control_grid_outage_check = None
                self._grid_control_user_initiated_toggle = None
            self._grid_control_check_cache_until = now + 15.0
            _LOGGER.debug(
                "Grid control check fetch failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            return
        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._grid_control_check_payload = redacted_payload
        else:
            self._grid_control_check_payload = {"value": redacted_payload}
        self._parse_grid_control_check_payload(payload)
        self._grid_control_check_failures = 0
        self._grid_control_check_last_success_mono = now
        self._grid_control_check_cache_until = now + GRID_CONTROL_CHECK_CACHE_TTL

    async def _async_refresh_dry_contact_settings(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._dry_contact_settings_cache_until:
            if now < self._dry_contact_settings_cache_until:
                return
        fetcher = getattr(self.client, "dry_contacts_settings", None)
        if not callable(fetcher):
            self._dry_contact_settings_supported = None
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            self._dry_contact_settings_failures += 1
            last_success = getattr(
                self, "_dry_contact_settings_last_success_mono", None
            )
            if (
                not isinstance(last_success, (int, float))
                or (now - float(last_success)) >= DRY_CONTACT_SETTINGS_STALE_AFTER_S
            ):
                self._dry_contact_settings_supported = None
            self._dry_contact_settings_cache_until = (
                now + DRY_CONTACT_SETTINGS_FAILURE_CACHE_TTL
            )
            _LOGGER.debug(
                "Dry contact settings fetch failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            return
        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._dry_contact_settings_payload = redacted_payload
        else:
            self._dry_contact_settings_payload = {"value": redacted_payload}
        self._parse_dry_contact_settings_payload(payload)
        self._dry_contact_settings_failures = 0
        self._dry_contact_settings_last_success_mono = now
        self._dry_contact_settings_cache_until = now + DRY_CONTACT_SETTINGS_CACHE_TTL

    async def async_set_system_profile(self, profile_key: str) -> None:
        profile = self._normalize_battery_profile_key(profile_key)
        if not profile:
            raise ServiceValidationError("Battery profile is unavailable.")
        if profile not in self.battery_profile_option_keys:
            raise ServiceValidationError("Selected battery profile is not supported.")
        reserve = self._target_reserve_for_profile(profile)
        sub_type = (
            self._current_savings_sub_type() if profile == "cost_savings" else None
        )
        await self._async_apply_battery_profile(
            profile=profile,
            reserve=reserve,
            sub_type=sub_type,
            require_exact_pending_match=False,
        )

    async def async_set_battery_reserve(self, reserve: int) -> None:
        profile = self.battery_selected_profile
        if not profile:
            raise ServiceValidationError("Battery profile is unavailable.")
        if profile == "backup_only":
            raise ServiceValidationError("Full Backup reserve is fixed at 100%.")
        normalized = self._normalize_battery_reserve_for_profile(profile, reserve)
        sub_type = (
            self._current_savings_sub_type() if profile == "cost_savings" else None
        )
        await self._async_apply_battery_profile(
            profile=profile,
            reserve=normalized,
            sub_type=sub_type,
        )

    async def async_set_savings_use_battery_after_peak(self, enabled: bool) -> None:
        profile = self.battery_selected_profile
        if profile != "cost_savings":
            raise ServiceValidationError("Savings profile must be active.")
        reserve = self.battery_selected_backup_percentage
        if reserve is None:
            reserve = self._target_reserve_for_profile("cost_savings")
        sub_type = SAVINGS_OPERATION_MODE_SUBTYPE if enabled else None
        await self._async_apply_battery_profile(
            profile="cost_savings",
            reserve=reserve,
            sub_type=sub_type,
        )

    async def async_cancel_pending_profile_change(self) -> None:
        if not self.battery_profile_pending:
            self._clear_battery_pending()
            return
        self._assert_battery_profile_write_allowed()
        async with self._battery_profile_write_lock:
            self._battery_profile_last_write_mono = time.monotonic()
            await self.client.cancel_battery_profile_update()
        self._clear_battery_pending()
        self._storm_guard_cache_until = None
        self.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self.async_request_refresh()

    async def async_set_charge_from_grid(self, enabled: bool) -> None:
        if not self.charge_from_grid_control_available:
            raise ServiceValidationError("Charge from grid setting is unavailable.")
        payload: dict[str, object] = {"chargeFromGrid": bool(enabled)}
        if enabled:
            start, end = self._current_charge_from_grid_schedule_window()
            payload["acceptedItcDisclaimer"] = self._battery_itc_disclaimer_value()
            payload["chargeBeginTime"] = start
            payload["chargeEndTime"] = end
            payload["chargeFromGridScheduleEnabled"] = bool(
                self._battery_charge_from_grid_schedule_enabled
            )
        await self._async_apply_battery_settings(payload)

    async def async_set_charge_from_grid_schedule_enabled(self, enabled: bool) -> None:
        if not self.charge_from_grid_schedule_supported:
            raise ServiceValidationError("Charge from grid schedule is unavailable.")
        if self.battery_charge_from_grid_enabled is not True:
            raise ServiceValidationError("Charge from grid must be enabled first.")
        start, end = self._current_charge_from_grid_schedule_window()
        if start == end:
            raise ServiceValidationError(
                "Charge-from-grid schedule start and end times must be different."
            )
        payload: dict[str, object] = {
            "chargeFromGrid": True,
            "chargeFromGridScheduleEnabled": bool(enabled),
            "chargeBeginTime": start,
            "chargeEndTime": end,
            "acceptedItcDisclaimer": self._battery_itc_disclaimer_value(),
        }
        await self._async_apply_battery_settings(payload)

    async def async_set_charge_from_grid_schedule_time(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
    ) -> None:
        if not self.charge_from_grid_schedule_supported:
            raise ServiceValidationError("Charge from grid schedule is unavailable.")
        if self.battery_charge_from_grid_enabled is not True:
            raise ServiceValidationError("Charge from grid must be enabled first.")
        current_start, current_end = self._current_charge_from_grid_schedule_window()
        next_start = (
            self._time_to_minutes_of_day(start) if start is not None else current_start
        )
        next_end = self._time_to_minutes_of_day(end) if end is not None else current_end
        if next_start is None or next_end is None:
            raise ServiceValidationError("Charge-from-grid schedule time is invalid.")
        if next_start == next_end:
            raise ServiceValidationError(
                "Charge-from-grid schedule start and end times must be different."
            )

        # Use the newer /schedules API when a CFG schedule ID is known.
        # The legacy batterySettings endpoint ignores time changes on EMEA sites.
        schedule_id = getattr(self, "_battery_cfg_schedule_id", None)
        if schedule_id and hasattr(self.client, "delete_battery_schedule"):
            self._assert_battery_settings_write_allowed()
            async with self._battery_settings_write_lock:
                self._battery_settings_last_write_mono = time.monotonic()
                start_hhmm = f"{next_start // 60:02d}:{next_start % 60:02d}"
                end_hhmm = f"{next_end // 60:02d}:{next_end % 60:02d}"
                limit = getattr(self, "_battery_cfg_schedule_limit", None)
                if limit is None:
                    limit = 100
                days = getattr(self, "_battery_cfg_schedule_days", None) or [
                    1,
                    2,
                    3,
                    4,
                    5,
                    6,
                    7,
                ]
                tz = getattr(self, "_battery_cfg_schedule_timezone", None) or "UTC"
                # Save originals for restore-on-failure.
                orig_start = self._battery_charge_begin_time
                orig_end = self._battery_charge_end_time
                orig_start_hhmm = (
                    f"{orig_start // 60:02d}:{orig_start % 60:02d}"
                    if orig_start is not None
                    else "02:00"
                )
                orig_end_hhmm = (
                    f"{orig_end // 60:02d}:{orig_end % 60:02d}"
                    if orig_end is not None
                    else "05:00"
                )
                await self.client.delete_battery_schedule(schedule_id)
                # XSRF token is single-use; clear it so create acquires a fresh one.
                self.client._bp_xsrf_token = None
                try:
                    await self.client.create_battery_schedule(
                        schedule_type="CFG",
                        start_time=start_hhmm,
                        end_time=end_hhmm,
                        limit=limit,
                        days=days,
                        timezone=tz,
                    )
                except Exception as err:  # noqa: BLE001
                    # Attempt to restore the original schedule.
                    try:
                        self.client._bp_xsrf_token = None
                        await self.client.create_battery_schedule(
                            schedule_type="CFG",
                            start_time=orig_start_hhmm,
                            end_time=orig_end_hhmm,
                            limit=limit,
                            days=days,
                            timezone=tz,
                        )
                        _LOGGER.info(
                            "Restored original CFG schedule after failed update"
                        )
                    except Exception as restore_err:  # noqa: BLE001
                        _LOGGER.error(
                            "Failed to restore original CFG schedule: %s", restore_err
                        )
                    raise ServiceValidationError(
                        f"Failed to create updated CFG schedule: {err}"
                    ) from err
            self._battery_charge_begin_time = next_start
            self._battery_charge_end_time = next_end
            self._battery_cfg_schedule_id = None  # cleared until next poll
            self._battery_settings_cache_until = None
            self.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
            await self.async_request_refresh()
            return

        # No existing CFG schedule, but the /schedules API is available on this
        # site — create one.
        if (
            hasattr(self.client, "create_battery_schedule")
            and getattr(self, "_battery_schedules_payload", None) is not None
        ):
            self._assert_battery_settings_write_allowed()
            async with self._battery_settings_write_lock:
                self._battery_settings_last_write_mono = time.monotonic()
                start_hhmm = f"{next_start // 60:02d}:{next_start % 60:02d}"
                end_hhmm = f"{next_end // 60:02d}:{next_end % 60:02d}"
                days = [1, 2, 3, 4, 5, 6, 7]
                tz = getattr(self, "_battery_cfg_schedule_timezone", None) or "UTC"
                _LOGGER.info(
                    "Creating new CFG schedule: %s-%s limit=100 tz=%s",
                    start_hhmm,
                    end_hhmm,
                    tz,
                )
                await self.client.create_battery_schedule(
                    schedule_type="CFG",
                    start_time=start_hhmm,
                    end_time=end_hhmm,
                    limit=100,
                    days=days,
                    timezone=tz,
                )
            self._battery_charge_begin_time = next_start
            self._battery_charge_end_time = next_end
            self._battery_cfg_schedule_id = None  # cleared until next poll
            self._battery_settings_cache_until = None
            self.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
            await self.async_request_refresh()
            return

        # Fallback: legacy batterySettings PUT (works on non-EMEA sites)
        payload: dict[str, object] = {
            "chargeFromGrid": True,
            "chargeFromGridScheduleEnabled": bool(
                self._battery_charge_from_grid_schedule_enabled
            ),
            "chargeBeginTime": next_start,
            "chargeEndTime": next_end,
            "acceptedItcDisclaimer": self._battery_itc_disclaimer_value(),
        }
        await self._async_apply_battery_settings(payload)

    async def async_set_cfg_schedule_limit(self, limit: int) -> None:
        """Update the CFG schedule charge limit (max SoC %) via delete+create."""
        if not hasattr(self.client, "create_battery_schedule"):
            raise ServiceValidationError(
                "Schedule API not available on this client version."
            )
        if (
            getattr(self, "_battery_cfg_schedule_id", None) is None
            or getattr(self, "_battery_cfg_schedule_limit", None) is None
            or getattr(self, "_battery_charge_begin_time", None) is None
            or getattr(self, "_battery_charge_end_time", None) is None
        ):
            raise ServiceValidationError(
                "No existing charge-from-grid schedule is available."
            )
        self._assert_battery_settings_write_allowed()
        async with self._battery_settings_write_lock:
            self._battery_settings_last_write_mono = time.monotonic()
            current_start, current_end = (
                self._current_charge_from_grid_schedule_window()
            )
            start_hhmm = f"{current_start // 60:02d}:{current_start % 60:02d}"
            end_hhmm = f"{current_end // 60:02d}:{current_end % 60:02d}"
            days = getattr(self, "_battery_cfg_schedule_days", None) or [
                1,
                2,
                3,
                4,
                5,
                6,
                7,
            ]
            tz = getattr(self, "_battery_cfg_schedule_timezone", None) or "UTC"
            schedule_id = self._battery_cfg_schedule_id
            orig_limit = self._battery_cfg_schedule_limit
            await self.client.delete_battery_schedule(schedule_id)
            # XSRF token is single-use; clear it so create acquires a fresh one.
            self.client._bp_xsrf_token = None
            try:
                await self.client.create_battery_schedule(
                    schedule_type="CFG",
                    start_time=start_hhmm,
                    end_time=end_hhmm,
                    limit=limit,
                    days=days,
                    timezone=tz,
                )
            except Exception as err:  # noqa: BLE001
                # Attempt to restore the original schedule.
                try:
                    self.client._bp_xsrf_token = None
                    await self.client.create_battery_schedule(
                        schedule_type="CFG",
                        start_time=start_hhmm,
                        end_time=end_hhmm,
                        limit=orig_limit,
                        days=days,
                        timezone=tz,
                    )
                    _LOGGER.info(
                        "Restored original CFG schedule after failed limit update"
                    )
                except Exception as restore_err:  # noqa: BLE001
                    _LOGGER.error(
                        "Failed to restore original CFG schedule: %s", restore_err
                    )
                raise ServiceValidationError(
                    f"Failed to create updated CFG schedule: {err}"
                ) from err
        self._battery_cfg_schedule_limit = limit
        self._battery_cfg_schedule_id = None  # cleared until next poll
        self._battery_settings_cache_until = None
        self.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self.async_request_refresh()

    async def async_request_grid_toggle_otp(self) -> None:
        await self._async_assert_grid_toggle_allowed()
        requester = getattr(self.client, "request_grid_toggle_otp", None)
        if not callable(requester):
            self._raise_grid_validation("grid_control_unavailable")
        try:
            await requester()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Grid toggle OTP request failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            self._raise_grid_validation("grid_control_unavailable")

    async def async_set_grid_mode(self, mode: str, otp: str) -> None:
        try:
            normalized_mode = str(mode).strip().lower()
        except Exception:
            normalized_mode = ""
        if normalized_mode not in {"on_grid", "off_grid"}:
            self._raise_grid_validation("grid_mode_invalid")

        otp_text = str(otp).strip() if otp is not None else ""
        if not otp_text:
            self._raise_grid_validation("grid_otp_required")
        if len(otp_text) != 4 or not otp_text.isdigit():
            self._raise_grid_validation("grid_otp_invalid_format")

        await self._async_assert_grid_toggle_allowed()

        validator = getattr(self.client, "validate_grid_toggle_otp", None)
        if not callable(validator):
            self._raise_grid_validation("grid_control_unavailable")
        try:
            valid = await validator(otp_text)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Grid toggle OTP validation call failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            self._raise_grid_validation("grid_control_unavailable")
        if valid is not True:
            self._raise_grid_validation("grid_otp_invalid")

        envoy_serial = self._grid_envoy_serial()
        if envoy_serial is None:
            self._raise_grid_validation("grid_envoy_serial_missing")

        grid_state = 2 if normalized_mode == "on_grid" else 1
        setter = getattr(self.client, "set_grid_state", None)
        if not callable(setter):
            self._raise_grid_validation("grid_control_unavailable")
        try:
            await setter(envoy_serial, grid_state)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Grid mode set request failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(
                    err,
                    site_ids=(self.site_id,),
                    identifiers=(envoy_serial,),
                ),
            )
            self._raise_grid_validation("grid_control_unavailable")

        old_state = (
            "OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD"
            if normalized_mode == "on_grid"
            else "OPER_RELAY_CLOSED"
        )
        new_state = (
            "OPER_RELAY_CLOSED"
            if normalized_mode == "on_grid"
            else "OPER_RELAY_OFFGRID_AC_GRID_PRESENT"
        )
        logger = getattr(self.client, "log_grid_change", None)
        if callable(logger):
            try:
                await logger(envoy_serial, old_state, new_state)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Grid toggle audit log failed for site %s: %s",
                    redact_site_id(self.site_id),
                    redact_text(
                        err,
                        site_ids=(self.site_id,),
                        identifiers=(envoy_serial,),
                    ),
                )

        self.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self.async_request_refresh()
        await self._async_refresh_grid_control_check(force=True)

    async def async_set_grid_connection(
        self, enabled: bool, *, otp: str | None = None
    ) -> None:
        if not otp:
            self._raise_grid_validation("grid_otp_required")
        mode = "on_grid" if bool(enabled) else "off_grid"
        await self.async_set_grid_mode(mode, otp)

    async def async_set_battery_shutdown_level(self, level: int) -> None:
        if not self.battery_shutdown_level_available:
            raise ServiceValidationError("Battery shutdown level is unavailable.")
        try:
            normalized = int(level)
        except Exception as err:  # noqa: BLE001
            raise ServiceValidationError("Battery shutdown level is invalid.") from err
        min_level = self.battery_shutdown_level_min
        max_level = self.battery_shutdown_level_max
        if normalized < min_level or normalized > max_level:
            raise ServiceValidationError(
                f"Battery shutdown level must be between {min_level} and {max_level}."
            )
        await self._async_apply_battery_settings({"veryLowSoc": normalized})

    def _parse_storm_guard_profile(
        self, payload: object
    ) -> tuple[str | None, bool | None]:
        if not isinstance(payload, dict):
            return None, None
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload
        state = self._normalize_storm_guard_state(data.get("stormGuardState"))
        evse = self._coerce_optional_bool(data.get("evseStormEnabled"))
        return state, evse

    def _parse_storm_alert(self, payload: object) -> bool | None:
        if not isinstance(payload, dict):
            return None
        self._storm_alert_critical_override = self._coerce_optional_bool(
            payload.get("criticalAlertsOverride")
        )
        alerts = payload.get("stormAlerts")
        normalized_alerts: list[dict[str, object]] = []
        derived_alert_active: bool | None = None
        if isinstance(alerts, list):
            derived_alert_active = False
            for alert in alerts:
                alert_active = False
                if isinstance(alert, dict):
                    normalized: dict[str, object] = {}
                    for key in (
                        "id",
                        "name",
                        "source",
                        "status",
                        "active",
                        "critical",
                        "type",
                        "severity",
                        "message",
                        "startTime",
                        "endTime",
                        "region",
                    ):
                        if key in alert and alert.get(key) is not None:
                            normalized[key] = alert.get(key)
                    if not normalized:
                        for key, value in alert.items():
                            if isinstance(value, (str, int, float, bool)):
                                normalized[str(key)] = value
                    if normalized:
                        normalized_alerts.append(normalized)
                    else:
                        normalized_alerts.append({"active": True})
                    alert_active = self._storm_alert_is_active(alert)
                elif alert is not None:
                    try:
                        normalized_alerts.append({"value": str(alert)})
                    except Exception:  # noqa: BLE001
                        normalized_alerts.append({"active": True})
                    alert_active = True
                if alert_active:
                    derived_alert_active = True
        self._storm_alerts = normalized_alerts
        critical_active = self._coerce_optional_bool(payload.get("criticalAlertActive"))
        if derived_alert_active is None:
            return critical_active
        if critical_active is None:
            return derived_alert_active
        return critical_active or derived_alert_active

    def _storm_alert_status_is_inactive(self, status: str | None) -> bool:
        if status is None:
            return False
        return status in STORM_ALERT_INACTIVE_STATUSES

    def _storm_alert_is_active(self, alert: dict[str, object]) -> bool:
        explicit_active = self._coerce_optional_bool(alert.get("active"))
        if explicit_active is not None:
            return explicit_active
        status = self._coerce_optional_text(alert.get("status"))
        if status:
            normalized_status = status.strip().lower().replace("_", "-")
            if normalized_status:
                return not self._storm_alert_status_is_inactive(normalized_status)
        return True

    async def _async_refresh_storm_guard_profile(self, *, force: bool = False) -> None:
        now = time.monotonic()
        pending_profile = getattr(self, "_battery_pending_profile", None)
        if not force and not pending_profile and self._storm_guard_cache_until:
            if now < self._storm_guard_cache_until:
                return
        locale = None
        try:
            locale = getattr(self.hass.config, "language", None)
        except Exception:  # noqa: BLE001
            locale = None
        fetcher = getattr(self.client, "storm_guard_profile", None)
        if not callable(fetcher):
            return
        payload = await fetcher(locale=locale)
        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._battery_profile_payload = redacted_payload
        else:
            self._battery_profile_payload = {"value": redacted_payload}
        self._parse_battery_profile_payload(payload)
        state, evse = self._parse_storm_guard_profile(payload)
        if state is not None:
            self._storm_guard_state = state
        self._sync_storm_guard_pending(state)
        if evse is not None:
            self._storm_evse_enabled = evse
        self._storm_guard_cache_until = now + STORM_GUARD_CACHE_TTL

    async def _async_refresh_storm_alert(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._storm_alert_cache_until:
            if now < self._storm_alert_cache_until:
                return
        fetcher = getattr(self.client, "storm_guard_alert", None)
        if not callable(fetcher):
            return
        payload = await fetcher()
        active = self._parse_storm_alert(payload)
        if active is not None:
            self._storm_alert_active = active
        self._storm_alert_cache_until = now + STORM_ALERT_CACHE_TTL

    async def async_opt_out_all_storm_alerts(self) -> None:
        await self._async_refresh_storm_alert(force=True)

        actionable: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for alert in self.storm_alerts:
            if not isinstance(alert, dict):
                continue
            alert_id = self._coerce_optional_text(alert.get("id"))
            if not alert_id or alert_id in seen_ids:
                continue
            if not self._storm_alert_is_active(alert):
                continue
            name = self._coerce_optional_text(alert.get("name")) or "Storm Alert"
            actionable.append((alert_id, name))
            seen_ids.add(alert_id)

        if not actionable:
            return

        opt_out = getattr(self.client, "opt_out_storm_alert", None)
        if not callable(opt_out):
            raise ServiceValidationError("Storm Alert opt-out is unavailable.")

        failures: list[tuple[str, Exception]] = []
        for alert_id, name in actionable:
            try:
                await opt_out(alert_id=alert_id, name=name)
            except Exception as err:  # noqa: BLE001
                failures.append((alert_id, err))
                _LOGGER.warning(
                    "Storm Alert opt-out failed for site %s alert %s: %s",
                    redact_site_id(self.site_id),
                    redact_identifier(alert_id),
                    redact_text(
                        err,
                        site_ids=(self.site_id,),
                        identifiers=(alert_id,),
                    ),
                )

        refresh_err: Exception | None = None
        self._storm_alert_cache_until = None
        try:
            await self._async_refresh_storm_alert(force=True)
            self.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
            await self.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            refresh_err = err
            _LOGGER.warning(
                "Storm Alert opt-out refresh failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )

        if failures:
            raise ServiceValidationError(
                f"Storm Alert opt-out failed for {len(failures)} alert(s)."
            )
        if refresh_err is not None:
            raise refresh_err

    async def async_set_storm_guard_enabled(self, enabled: bool) -> None:
        await self._async_refresh_storm_guard_profile(force=True)
        if self._storm_evse_enabled is None:
            raise ServiceValidationError("Storm Guard settings are unavailable.")
        target_state = "enabled" if enabled else "disabled"
        self._set_storm_guard_pending(target_state)
        try:
            await self.client.set_storm_guard(
                enabled=bool(enabled),
                evse_enabled=bool(self._storm_evse_enabled),
            )
        except aiohttp.ClientResponseError as err:
            self._clear_storm_guard_pending()
            if err.status == HTTPStatus.FORBIDDEN:
                owner = self.battery_user_is_owner
                installer = self.battery_user_is_installer
                if owner is False and installer is False:
                    raise ServiceValidationError(
                        "Storm Guard updates are not permitted for this account."
                    ) from err
                raise ServiceValidationError(
                    "Storm Guard update was rejected by Enphase (HTTP 403 Forbidden)."
                ) from err
            if err.status == HTTPStatus.UNAUTHORIZED:
                raise ServiceValidationError(
                    "Storm Guard update could not be authenticated. Reauthenticate and try again."
                ) from err
            raise
        except Exception:
            self._clear_storm_guard_pending()
            raise
        self._storm_guard_cache_until = None
        self._sync_storm_guard_pending(self._storm_guard_state)

    async def async_set_storm_evse_enabled(self, enabled: bool) -> None:
        await self._async_refresh_storm_guard_profile(force=True)
        if self._storm_guard_state is None:
            raise ServiceValidationError("Storm Guard settings are unavailable.")
        try:
            await self.client.set_storm_guard(
                enabled=self._storm_guard_state == "enabled",
                evse_enabled=bool(enabled),
            )
        except aiohttp.ClientResponseError as err:
            if err.status == HTTPStatus.FORBIDDEN:
                owner = self.battery_user_is_owner
                installer = self.battery_user_is_installer
                if owner is False and installer is False:
                    raise ServiceValidationError(
                        "Storm Guard updates are not permitted for this account."
                    ) from err
                raise ServiceValidationError(
                    "Storm Guard update was rejected by Enphase (HTTP 403 Forbidden)."
                ) from err
            if err.status == HTTPStatus.UNAUTHORIZED:
                raise ServiceValidationError(
                    "Storm Guard update could not be authenticated. Reauthenticate and try again."
                ) from err
            raise
        self._storm_evse_enabled = bool(enabled)
        self._storm_guard_cache_until = time.monotonic() + STORM_GUARD_CACHE_TTL

    async def _async_resolve_green_battery_settings(
        self, serials: Iterable[str]
    ) -> dict[str, tuple[bool | None, bool]]:
        """Resolve green charging battery settings concurrently."""
        results: dict[str, tuple[bool | None, bool]] = {}
        pending: dict[str, asyncio.Task[tuple[bool | None, bool] | None]] = {}
        now = time.monotonic()
        if self._scheduler_backoff_active():
            for sn in dict.fromkeys(serials):
                if not sn:
                    continue
                cached = self._green_battery_cache.get(sn)
                if cached and (now - cached[2] < GREEN_BATTERY_CACHE_TTL):
                    results[sn] = (cached[0], cached[1])
            return results
        for sn in dict.fromkeys(serials):
            if not sn:
                continue
            cached = self._green_battery_cache.get(sn)
            if cached and (now - cached[2] < GREEN_BATTERY_CACHE_TTL):
                results[sn] = (cached[0], cached[1])
                continue
            pending[sn] = asyncio.create_task(self._get_green_battery_setting(sn))

        if pending:
            responses = await asyncio.gather(*pending.values(), return_exceptions=True)
            for sn, response in zip(pending.keys(), responses, strict=False):
                if isinstance(response, Exception):
                    _LOGGER.debug(
                        "Green battery setting lookup failed for %s: %s",
                        redact_identifier(sn),
                        redact_text(
                            response,
                            site_ids=(self.site_id,),
                            identifiers=(sn,),
                        ),
                    )
                    cached = self._green_battery_cache.get(sn)
                    if cached:
                        results[sn] = (cached[0], cached[1])
                    continue
                if response is None:
                    cached = self._green_battery_cache.get(sn)
                    if cached:
                        results[sn] = (cached[0], cached[1])
                    continue
                results[sn] = response

        return results

    async def _async_resolve_auth_settings(
        self, serials: Iterable[str]
    ) -> dict[str, tuple[bool | None, bool | None, bool, bool]]:
        """Resolve session authentication settings concurrently."""
        results: dict[str, tuple[bool | None, bool | None, bool, bool]] = {}
        pending: dict[
            str,
            asyncio.Task[tuple[bool | None, bool | None, bool, bool] | None],
        ] = {}
        now = time.monotonic()
        if self._auth_settings_backoff_active():
            for sn in dict.fromkeys(serials):
                if not sn:
                    continue
                cached = self._auth_settings_cache.get(sn)
                if cached and (now - cached[4] < AUTH_SETTINGS_CACHE_TTL):
                    results[sn] = cached[0], cached[1], cached[2], cached[3]
            return results
        for sn in dict.fromkeys(serials):
            if not sn:
                continue
            cached = self._auth_settings_cache.get(sn)
            if cached and (now - cached[4] < AUTH_SETTINGS_CACHE_TTL):
                results[sn] = cached[0], cached[1], cached[2], cached[3]
                continue
            pending[sn] = asyncio.create_task(self._get_auth_settings(sn))

        if pending:
            responses = await asyncio.gather(*pending.values(), return_exceptions=True)
            for sn, response in zip(pending.keys(), responses, strict=False):
                if isinstance(response, Exception):
                    _LOGGER.debug(
                        "Auth settings lookup failed for %s: %s",
                        redact_identifier(sn),
                        redact_text(
                            response,
                            site_ids=(self.site_id,),
                            identifiers=(sn,),
                        ),
                    )
                    cached = self._auth_settings_cache.get(sn)
                    if cached:
                        results[sn] = cached[0], cached[1], cached[2], cached[3]
                    continue
                if response is None:
                    cached = self._auth_settings_cache.get(sn)
                    if cached:
                        results[sn] = cached[0], cached[1], cached[2], cached[3]
                    continue
                results[sn] = response

        return results

    def _resolve_charge_mode_pref(self, sn: str) -> str | None:
        """Return the preferred charge mode recorded for a charger."""

        sn_str = str(sn)
        try:
            data = (self.data or {}).get(sn_str)
        except Exception:
            data = None
        data = data or {}
        candidates: list[str | None] = [
            data.get("charge_mode_pref"),
            data.get("charge_mode"),
        ]
        cached = self._charge_mode_cache.get(sn_str)
        if cached:
            candidates.append(cached[0])
        for raw in candidates:
            if raw is None:
                continue
            try:
                value = str(raw).strip()
            except Exception:
                continue
            if value:
                return value.upper()
        return None

    def _charge_mode_start_preferences(self, sn: str) -> ChargeModeStartPreferences:
        """Return payload preferences based on the configured charge mode."""

        mode = self._resolve_charge_mode_pref(sn)
        include_level: bool | None = None
        strict = False
        enforce_mode: str | None = None
        if mode == "MANUAL_CHARGING":
            include_level = True
            strict = True
        elif mode == "SCHEDULED_CHARGING":
            include_level = True
            strict = True
            enforce_mode = "SCHEDULED_CHARGING"
        elif mode == "GREEN_CHARGING":
            include_level = False
            strict = True
        return ChargeModeStartPreferences(
            mode=mode,
            include_level=include_level,
            strict=strict,
            enforce_mode=enforce_mode,
        )

    async def _ensure_charge_mode(self, sn: str, target_mode: str) -> None:
        """Force the charge mode preference via the scheduler API."""

        sn_str = str(sn)
        try:
            await self.client.set_charge_mode(sn_str, target_mode)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to enforce %s charge mode for charger %s: %s",
                target_mode,
                redact_identifier(sn_str),
                redact_text(
                    err,
                    site_ids=(self.site_id,),
                    identifiers=(sn_str,),
                ),
            )
            return
        self.set_charge_mode_cache(sn_str, target_mode)
