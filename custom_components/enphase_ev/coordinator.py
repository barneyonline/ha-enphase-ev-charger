from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
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
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ServiceValidationError,
)  # noqa: F401
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AuthTokens,
    EnlightenAuthInvalidCredentials,
    EnlightenAuthMFARequired,
    EnlightenAuthUnavailable,
    EnphaseEVClient,
    InvalidPayloadError,
    Unauthorized,
    async_authenticate,
    is_scheduler_unavailable_error,
)
from .const import (
    BATTERY_MIN_SOC_FALLBACK,
    CONF_ACCESS_TOKEN,
    CONF_COOKIE,
    DEFAULT_CHARGE_LEVEL_SETTING,
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
    DEFAULT_SCAN_INTERVAL,
    DRY_CONTACT_SETTINGS_STALE_AFTER_S,
    DOMAIN,
    GRID_CONTROL_CHECK_STALE_AFTER_S,
    OPT_API_TIMEOUT,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
    OPT_SESSION_HISTORY_INTERVAL,
    PHASE_SWITCH_CONFIG_SETTING,
    SAVINGS_OPERATION_MODE_SUBTYPE,
    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
    SAFE_LIMIT_AMPS,
)
from .battery_runtime import BatteryRuntime
from .coordinator_diagnostics import CoordinatorDiagnostics
from .device_types import (
    normalize_type_key,
    parse_type_identifier,
    type_display_label,
    type_identifier,
)
from .device_info_helpers import _is_redundant_model_id
from .energy import EnergyManager
from .evse_timeseries import EVSETimeseriesManager
from .evse_runtime import (
    AMP_RESTART_DELAY_S,
    SUSPENDED_EVSE_STATUS,
    ChargeModeStartPreferences,
    EvseRuntime,
)
from .heatpump_runtime import HeatpumpRuntime
from .inventory_runtime import CoordinatorTopologySnapshot, InventoryRuntime
from .log_redaction import (
    redact_site_id,
    redact_text,
    truncate_identifier,
)
from .parsing_helpers import (
    coerce_optional_bool,
    coerce_optional_float,
    coerce_optional_text,
    heatpump_member_device_type,
    heatpump_status_text,
    parse_inverter_last_report,
    type_member_text,
)
from .session_history import (
    MIN_SESSION_HISTORY_CACHE_TTL,
    SESSION_HISTORY_CACHE_DAY_RETENTION,
    SESSION_HISTORY_CONCURRENCY,
    SESSION_HISTORY_FAILURE_BACKOFF_S,
    SessionHistoryManager,
)
from .summary import SummaryStore
from .refresh_plan import (
    FOLLOWUP_PLAN,
    HEATPUMP_FOLLOWUP_PLAN,
    RefreshPlan,
    SITE_ONLY_FOLLOWUP_PLAN,
    bind_refresh_plan,
    post_session_followup_plan,
    warmup_plan,
)
from .service_validation import raise_translated_service_validation
from .state_models import (
    BatteryControlCapability,
    BatteryState,
    DiscoveryState,
    EVSEState,
    HeatpumpState,
    InventoryState,
    RefreshHealthState,
    install_state_descriptors,
)
from .voltage import (
    coerce_nominal_voltage,
    preferred_operating_voltage,
    resolve_nominal_voltage_for_hass,
)

_LOGGER = logging.getLogger(__name__)
EVSE_FEATURE_FLAGS_CACHE_TTL = 1800.0
DEVICES_INVENTORY_CACHE_TTL = 300.0
HEMS_DEVICES_STALE_AFTER_S = 90.0
# HEMS heat-pump status/power can lag the Enphase app by only a few seconds.
# Keep these caches short so we do not hold stale or empty telemetry for minutes.
HEMS_DEVICES_CACHE_TTL = 15.0
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
DISCOVERY_SNAPSHOT_STORE_VERSION = 1
DISCOVERY_SNAPSHOT_SAVE_DELAY_S = 1.0
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
_SERVICE_VALIDATION_ERROR_COMPAT = ServiceValidationError


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
        object.__setattr__(
            self,
            "discovery_state",
            DiscoveryState(
                _topology_snapshot_cache=CoordinatorTopologySnapshot(
                    charger_serials=(),
                    battery_serials=(),
                    inverter_serials=(),
                    active_type_keys=(),
                    gateway_iq_router_keys=(),
                    inventory_ready=False,
                )
            ),
        )
        object.__setattr__(self, "refresh_state", RefreshHealthState())
        object.__setattr__(self, "inventory_state", InventoryState())
        object.__setattr__(self, "heatpump_state", HeatpumpState())
        object.__setattr__(self, "evse_state", EVSEState())
        object.__setattr__(
            self,
            "battery_state",
            BatteryState(
                _battery_profile_write_lock=asyncio.Lock(),
                _battery_settings_write_lock=asyncio.Lock(),
            ),
        )
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
        self.evse_runtime = EvseRuntime(self)
        self.battery_runtime = BatteryRuntime(self)
        self.heatpump_runtime = HeatpumpRuntime(self)
        self.inventory_runtime = InventoryRuntime(self)
        self.diagnostics = CoordinatorDiagnostics(self)

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
        if name == "diagnostics":
            diagnostics = CoordinatorDiagnostics(self)
            self.__dict__["diagnostics"] = diagnostics
            return diagnostics
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
        self.evse_runtime.schedule_session_enrichment(serials, day_local)

    async def _async_enrich_sessions(
        self,
        serials: Iterable[str],
        day_local: datetime,
        *,
        in_background: bool,
    ) -> dict[str, list[dict]]:
        return await self.evse_runtime.async_enrich_sessions(
            serials,
            day_local,
            in_background=in_background,
        )

    def _sum_session_energy(self, sessions: list[dict]) -> float:
        return self.evse_runtime.sum_session_energy(sessions)

    @staticmethod
    def _session_history_day(payload: dict, day_local_default: datetime) -> datetime:
        return EvseRuntime.session_history_day(payload, day_local_default)

    async def _async_fetch_sessions_today(
        self,
        sn: str,
        *,
        day_local: datetime | None = None,
    ) -> list[dict]:
        return await self.evse_runtime.async_fetch_sessions_today(
            sn,
            day_local=day_local,
        )

    @staticmethod
    def _normalize_serials(serials: Iterable[str] | None) -> set[str]:
        return EvseRuntime.normalize_serials(serials)

    def _retained_session_history_days(
        self, keep_day_keys: Iterable[str] | None = None
    ) -> set[str]:
        return self.evse_runtime.retained_session_history_days(keep_day_keys)

    def _prune_session_history_cache_shim(
        self,
        *,
        active_serials: Iterable[str] | None,
        keep_day_keys: Iterable[str] | None = None,
    ) -> None:
        self.evse_runtime.prune_session_history_cache_shim(
            active_serials=active_serials,
            keep_day_keys=keep_day_keys,
        )

    def _set_session_history_cache_shim_entry(
        self,
        serial: str,
        day_key: str,
        sessions: list[dict],
    ) -> None:
        self.evse_runtime.set_session_history_cache_shim_entry(
            serial,
            day_key,
            sessions,
        )

    def _prune_serial_runtime_state(self, active_serials: Iterable[str]) -> set[str]:
        return self.evse_runtime.prune_serial_runtime_state(active_serials)

    def _prune_runtime_caches(
        self,
        *,
        active_serials: Iterable[str],
        keep_day_keys: Iterable[str] | None = None,
    ) -> None:
        self.evse_runtime.prune_runtime_caches(
            active_serials=active_serials,
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
        return self.inventory_runtime.topology_snapshot()

    def gateway_inventory_summary(self) -> dict[str, object]:
        return self.inventory_runtime.gateway_inventory_summary()

    def microinverter_inventory_summary(self) -> dict[str, object]:
        return self.inventory_runtime.microinverter_inventory_summary()

    def heatpump_inventory_summary(self) -> dict[str, object]:
        return self.inventory_runtime.heatpump_inventory_summary()

    def heatpump_type_summary(self, device_type: str) -> dict[str, object]:
        return self.inventory_runtime.heatpump_type_summary(device_type)

    def gateway_iq_energy_router_summary_records(self) -> list[dict[str, object]]:
        return self.inventory_runtime.gateway_iq_energy_router_summary_records()

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
        return self.inventory_runtime.gateway_iq_energy_router_record(router_key)

    def _current_topology_snapshot(self) -> CoordinatorTopologySnapshot:
        return self.inventory_runtime._current_topology_snapshot()

    @callback
    def _notify_topology_listeners(self) -> None:
        self.inventory_runtime._notify_topology_listeners()

    @callback
    def _refresh_cached_topology(self) -> bool:
        return self.inventory_runtime._refresh_cached_topology()

    @callback
    def _begin_topology_refresh_batch(self) -> None:
        self.inventory_runtime._begin_topology_refresh_batch()

    @callback
    def _end_topology_refresh_batch(self) -> bool:
        return self.inventory_runtime._end_topology_refresh_batch()

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

    async def _async_run_refresh_plan(
        self,
        phase_timings: dict[str, float],
        *,
        plan: RefreshPlan,
    ) -> None:
        bound_plan = bind_refresh_plan(self, plan)
        for stage in bound_plan.stages:
            await self._async_run_staged_refresh_calls(
                phase_timings,
                stage_key=stage.stage_key,
                defer_topology=stage.defer_topology,
                parallel_calls=stage.parallel_calls,
                ordered_calls=stage.ordered_calls,
            )

    async def async_ensure_system_dashboard_diagnostics(self) -> None:
        await self.inventory_runtime.async_ensure_system_dashboard_diagnostics()

    async def async_ensure_battery_status_diagnostics(self) -> None:
        """Ensure battery-status payloads exist for diagnostics exports."""

        if isinstance(getattr(self, "_battery_status_payload", None), dict):
            return
        await self._async_refresh_battery_status(force=True)

    async def _async_refresh_hems_support_preflight(
        self, *, force: bool = False
    ) -> None:
        await self.heatpump_runtime.async_refresh_hems_support_preflight(force=force)

    async def async_ensure_heatpump_runtime_diagnostics(
        self, *, force: bool = False
    ) -> None:
        await self.heatpump_runtime.async_ensure_heatpump_runtime_diagnostics(
            force=force
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
            await self._async_run_refresh_plan(
                warmup_timings,
                plan=warmup_plan(warmup_data),
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
        charger_config = await self._async_resolve_charger_config(
            serials,
            keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
        )
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
            config_values = charger_config.get(sn)
            if isinstance(config_values, dict):
                if PHASE_SWITCH_CONFIG_SETTING in config_values:
                    payload["phase_switch_config"] = config_values[
                        PHASE_SWITCH_CONFIG_SETTING
                    ]
                if DEFAULT_CHARGE_LEVEL_SETTING in config_values:
                    payload["default_charge_level"] = config_values[
                        DEFAULT_CHARGE_LEVEL_SETTING
                    ]
        if working_data is None:
            self.async_set_updated_data(merged)

    def _parse_devices_inventory_payload(
        self, payload: object
    ) -> tuple[bool, dict[str, dict[str, object]], list[str]]:
        return self.inventory_runtime._parse_devices_inventory_payload(payload)

    def _set_type_device_buckets(
        self,
        grouped: dict[str, dict[str, object]],
        ordered_keys: list[str],
        *,
        authoritative: bool = True,
    ) -> None:
        self.inventory_runtime._set_type_device_buckets(
            grouped,
            ordered_keys,
            authoritative=authoritative,
        )

    @staticmethod
    def _devices_inventory_buckets(payload: object) -> list[dict[str, object]]:
        return InventoryRuntime._devices_inventory_buckets(payload)

    @staticmethod
    def _hems_devices_groups(payload: object) -> list[dict[str, object]]:
        return InventoryRuntime._hems_devices_groups(payload)

    @classmethod
    def _legacy_hems_devices_groups(cls, payload: object) -> list[dict[str, object]]:
        return InventoryRuntime._legacy_hems_devices_groups(payload)

    def _hems_grouped_devices(self) -> list[dict[str, object]]:
        return self.inventory_runtime._hems_grouped_devices()

    @staticmethod
    def _normalize_hems_member(member: dict[str, object]) -> dict[str, object]:
        return InventoryRuntime._normalize_hems_member(member)

    @staticmethod
    def _normalize_heatpump_member(member: dict[str, object]) -> dict[str, object]:
        return InventoryRuntime._normalize_heatpump_member(member)

    def _extract_hems_group_members(
        self,
        groups: list[dict[str, object]],
        requested_keys: set[str],
    ) -> tuple[bool, list[dict[str, object]]]:
        return self.inventory_runtime._extract_hems_group_members(
            groups,
            requested_keys,
        )

    def _hems_group_members(self, *group_keys: str) -> list[dict[str, object]]:
        return self.inventory_runtime._hems_group_members(*group_keys)

    @staticmethod
    def _hems_bucket_type(raw_type: object) -> str | None:
        return InventoryRuntime._hems_bucket_type(raw_type)

    @staticmethod
    def _heatpump_member_device_type(member: dict[str, object] | None) -> str | None:
        return heatpump_member_device_type(member)

    @staticmethod
    def _heatpump_worst_status_text(status_counts: dict[str, int]) -> str | None:
        return InventoryRuntime._heatpump_worst_status_text(status_counts)

    def _merge_heatpump_type_bucket(self) -> None:
        self.inventory_runtime._merge_heatpump_type_bucket()

    @staticmethod
    def _summary_text(value: object) -> str | None:
        return InventoryRuntime._summary_text(value)

    @classmethod
    def _summary_identity(cls, value: object) -> str | None:
        return InventoryRuntime._summary_identity(value)

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
        return heatpump_status_text(member)

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
        self.inventory_runtime._rebuild_inventory_summary_caches()

    async def _async_refresh_devices_inventory(self, *, force: bool = False) -> None:
        await self.inventory_runtime._async_refresh_devices_inventory(force=force)

    async def _async_refresh_hems_devices(self, *, force: bool = False) -> None:
        await self.inventory_runtime._async_refresh_hems_devices(force=force)

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
        await self.inventory_runtime._async_refresh_system_dashboard(force=force)

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
    def coerce_int(value: object, *, default: int = 0) -> int:
        return EnphaseCoordinator._coerce_int(value, default=default)

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

    def _site_timezone_name(self) -> str:
        """Return the site timezone name when available."""

        tz_name = getattr(self, "_battery_timezone", None)
        if isinstance(tz_name, str) and tz_name.strip():
            try:
                ZoneInfo(tz_name.strip())
            except Exception:
                pass
            else:
                return tz_name.strip()
        return "UTC"

    @staticmethod
    def _format_inverter_model_summary(model_counts: dict[str, int]) -> str | None:
        return InventoryRuntime._format_inverter_model_summary(model_counts)

    @staticmethod
    def _format_inverter_status_summary(summary_counts: dict[str, int]) -> str:
        return InventoryRuntime._format_inverter_status_summary(summary_counts)

    @staticmethod
    def _normalize_inverter_status(value: object) -> str:
        return InventoryRuntime._normalize_inverter_status(value)

    @staticmethod
    def _inverter_connectivity_state(summary_counts: dict[str, int]) -> str | None:
        return InventoryRuntime._inverter_connectivity_state(summary_counts)

    @staticmethod
    def _parse_inverter_last_report(value: object) -> datetime | None:
        return parse_inverter_last_report(value)

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
        await self.inventory_runtime._async_refresh_inverters()

    def _heatpump_primary_device_uid(self) -> str | None:
        return self.heatpump_runtime._heatpump_primary_device_uid()

    def _heatpump_runtime_device_uid(self) -> str | None:
        return self.heatpump_runtime._heatpump_runtime_device_uid()

    async def _async_refresh_heatpump_runtime_state(
        self, *, force: bool = False
    ) -> None:
        await self.heatpump_runtime.async_refresh_heatpump_runtime_state(force=force)

    async def _async_refresh_heatpump_daily_consumption(
        self, *, force: bool = False
    ) -> None:
        await self.heatpump_runtime.async_refresh_heatpump_daily_consumption(
            force=force
        )

    def _heatpump_daily_window(self) -> tuple[str, str, str, tuple[str, str]] | None:
        return self.heatpump_runtime._heatpump_daily_window()

    @staticmethod
    def _sum_optional_values(values: object) -> float | None:
        if not isinstance(values, list):
            return None
        total = 0.0
        found = False
        for item in values:
            if item is None:
                continue
            try:
                numeric = float(item)
            except Exception:
                continue
            if numeric != numeric or numeric in (float("inf"), float("-inf")):
                continue
            total += numeric
            found = True
        return total if found else None

    def _build_heatpump_daily_consumption_snapshot(
        self, payload: object
    ) -> dict[str, object] | None:
        return self.heatpump_runtime._build_heatpump_daily_consumption_snapshot(payload)

    def _heatpump_power_candidate_device_uids(self) -> list[str | None]:
        return self.heatpump_runtime._heatpump_power_candidate_device_uids()

    @staticmethod
    def _heatpump_latest_power_sample(payload: object) -> tuple[int, float] | None:
        return HeatpumpRuntime._heatpump_latest_power_sample(payload)

    @staticmethod
    def _infer_heatpump_interval_minutes(
        start_utc: datetime | None,
        bucket_count: int,
        now_utc: datetime,
    ) -> int | None:
        return HeatpumpRuntime._infer_heatpump_interval_minutes(
            start_utc, bucket_count, now_utc
        )

    def _heatpump_member_for_uid(self, uid: object) -> dict[str, object] | None:
        return self.heatpump_runtime._heatpump_member_for_uid(uid)

    @classmethod
    def _heatpump_member_aliases(cls, member: dict[str, object] | None) -> list[str]:
        return HeatpumpRuntime._heatpump_member_aliases(member)

    @classmethod
    def _heatpump_member_primary_id(
        cls, member: dict[str, object] | None
    ) -> str | None:
        return HeatpumpRuntime._heatpump_member_primary_id(member)

    @classmethod
    def _heatpump_member_parent_id(cls, member: dict[str, object] | None) -> str | None:
        return HeatpumpRuntime._heatpump_member_parent_id(member)

    def _heatpump_member_alias_map(self) -> dict[str, str]:
        return self.heatpump_runtime._heatpump_member_alias_map()

    def _heatpump_power_inventory_marker(self) -> tuple[tuple[str, str, str, str], ...]:
        return self.heatpump_runtime._heatpump_power_inventory_marker()

    def _heatpump_power_fetch_plan(
        self,
    ) -> tuple[list[str | None], bool, tuple[tuple[str, str, str, str], ...]]:
        return self.heatpump_runtime._heatpump_power_fetch_plan()

    def _heatpump_power_candidate_is_recommended(self, uid: str | None) -> bool:
        return self.heatpump_runtime._heatpump_power_candidate_is_recommended(uid)

    def _heatpump_power_candidate_type_rank(
        self,
        payload: dict[str, object],
        requested_uid: str | None,
        *,
        is_recommended: bool,
    ) -> int:
        return self.heatpump_runtime._heatpump_power_candidate_type_rank(
            payload,
            requested_uid,
            is_recommended=is_recommended,
        )

    def _heatpump_power_selection_key(
        self,
        payload: dict[str, object],
        *,
        requested_uid: str | None,
        sample: tuple[int, float] | None,
    ) -> tuple[int, int, int, int, float, int, int]:
        return self.heatpump_runtime._heatpump_power_selection_key(
            payload,
            requested_uid=requested_uid,
            sample=sample,
        )

    async def _async_refresh_heatpump_power(self, *, force: bool = False) -> None:
        await self.heatpump_runtime.async_refresh_heatpump_power(force=force)

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
        return type_member_text(member, *keys)

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

    def _envoy_member_looks_like_gateway(self, member: dict[str, object]) -> bool:
        if self._envoy_member_kind(member) in (
            "production",
            "consumption",
            "controller",
        ):
            return False
        if any(
            member.get(key) is not None
            for key in (
                "envoy_sw_version",
                "ap_mode",
                "supportsEntrez",
                "show_connection_details",
                "ip",
                "ip_address",
            )
        ):
            return True
        name = (self._type_member_text(member, "name") or "").lower()
        return "gateway" in name

    def _envoy_primary_gateway_member(self) -> dict[str, object] | None:
        for member in self._type_bucket_members("envoy"):
            if self._envoy_member_looks_like_gateway(member):
                return member
        return None

    def _heatpump_primary_member(self) -> dict[str, object] | None:
        return self.heatpump_runtime._heatpump_primary_member()

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
            if member is None:
                member = self._envoy_primary_gateway_member()
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
            if controller is None:
                controller = self._envoy_primary_gateway_member()
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
            if controller is None:
                controller = self._envoy_primary_gateway_member()
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
            if controller is None:
                controller = self._envoy_primary_gateway_member()
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
            if controller is None:
                controller = self._envoy_primary_gateway_member()
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
        return self.inventory_runtime.iter_inverter_serials()

    def inverter_data(self, serial: str) -> dict[str, object] | None:
        return self.inventory_runtime.inverter_data(serial)

    @staticmethod
    def parse_type_identifier(identifier: object) -> tuple[str, str] | None:
        return parse_type_identifier(identifier)

    def collect_site_metrics(self) -> dict[str, object]:
        return self.diagnostics.collect_site_metrics()

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
        last_failure_utc = getattr(session_manager, "service_last_failure_utc", None)
        return {
            "available": getattr(session_manager, "service_available", None),
            "using_stale": getattr(session_manager, "service_using_stale", None),
            "failures": getattr(session_manager, "service_failures", None),
            "last_error": getattr(session_manager, "service_last_error", None),
            "last_failure_utc": (
                last_failure_utc.isoformat()
                if isinstance(last_failure_utc, datetime)
                else None
            ),
            "last_payload_signature": getattr(
                session_manager,
                "_service_last_payload_signature",
                None,
            ),
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

    def heatpump_runtime_diagnostics(self) -> dict[str, object]:
        return self.heatpump_runtime.heatpump_runtime_diagnostics()

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
        return self.inventory_runtime.inverter_diagnostics_payloads()

    def _system_dashboard_raw_payloads(
        self, canonical_type: str
    ) -> dict[str, dict[str, object]]:
        return self.inventory_runtime._system_dashboard_raw_payloads(canonical_type)

    def system_dashboard_envoy_detail(self) -> dict[str, object] | None:
        return self.inventory_runtime.system_dashboard_envoy_detail()

    def system_dashboard_meter_detail(
        self, meter_kind: str
    ) -> dict[str, object] | None:
        return self.inventory_runtime.system_dashboard_meter_detail(meter_kind)

    def system_dashboard_battery_detail(self, serial: str) -> dict[str, object] | None:
        return self.inventory_runtime.system_dashboard_battery_detail(serial)

    def system_dashboard_diagnostics(self) -> dict[str, object]:
        return self.inventory_runtime.system_dashboard_diagnostics()

    def _issue_translation_placeholders(
        self, metrics: dict[str, object]
    ) -> dict[str, str]:
        return self.diagnostics.issue_translation_placeholders(metrics)

    def _issue_context(self) -> tuple[dict[str, object], dict[str, str]]:
        return self.diagnostics.issue_context()

    def _payload_health_state(self, name: str) -> dict[str, object]:
        return self.diagnostics.payload_health_state(name)

    def _mark_payload_endpoint_success(
        self,
        name: str,
        *,
        success_mono: float | None = None,
        success_utc: datetime | None = None,
    ) -> None:
        self.diagnostics.mark_payload_endpoint_success(
            name,
            success_mono=success_mono,
            success_utc=success_utc,
        )

    def _note_payload_endpoint_failure(
        self,
        name: str,
        *,
        error: str,
        signature: dict[str, object] | None = None,
        using_stale: bool = False,
    ) -> None:
        self.diagnostics.note_payload_endpoint_failure(
            name,
            error=error,
            signature=signature,
            using_stale=using_stale,
        )

    def _payload_endpoint_reusable(self, name: str, stale_after_s: float) -> bool:
        return self.diagnostics.payload_endpoint_reusable(name, stale_after_s)

    def _status_stale_window_s(self) -> float:
        return self.diagnostics.status_stale_window_s()

    def payload_health_diagnostics(self) -> dict[str, object]:
        return self.diagnostics.payload_health_diagnostics()

    async def _async_update_data(self) -> dict:
        t0 = time.monotonic()
        phase_timings: dict[str, float] = {}
        fallback_data: dict[str, dict] = {}
        status_used_stale = False
        first_refresh = not self._has_successful_refresh
        if isinstance(self.data, dict):
            try:
                fallback_data = dict(self.data)
            except Exception:
                fallback_data = self.data

        if self.site_only or not self.serials:
            self._backoff_until = None
            self._clear_backoff_timer()
            self.diagnostics.clear_reauth_issue()
            self.diagnostics.clear_network_issue()
            self.diagnostics.clear_cloud_issue()
            self.diagnostics.clear_dns_issue()
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
                await self._async_run_refresh_plan(
                    phase_timings,
                    plan=SITE_ONLY_FOLLOWUP_PLAN,
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
            if isinstance(data, dict):
                self._status_payload_cache = dict(data)
            self._mark_payload_endpoint_success(
                "status",
                success_mono=time.monotonic(),
                success_utc=dt_util.utcnow(),
            )
            self.last_failure_endpoint = None
            self.payload_using_stale = False
            self.payload_failure_kind = None
            self._unauth_errors = 0
            self.diagnostics.clear_reauth_issue()
        except ConfigEntryAuthFailed:
            raise
        except Unauthorized as err:
            raise ConfigEntryAuthFailed from err
        except InvalidPayloadError as err:
            reason = (err.summary or str(err) or "Invalid JSON response").strip()
            signature = err.signature_dict()
            can_reuse_status = (
                not first_refresh
                and isinstance(self._status_payload_cache, dict)
                and self._payload_endpoint_reusable(
                    "status", self._status_stale_window_s()
                )
            )
            self._last_error = reason
            self.last_failure_endpoint = err.endpoint
            self.payload_failure_kind = err.failure_kind
            self.last_failure_utc = dt_util.utcnow()
            self.last_failure_status = None
            self.last_failure_description = reason
            self.last_failure_response = reason
            self.last_failure_source = "payload"
            self._network_errors = 0
            self._http_errors = 0
            if can_reuse_status:
                self.payload_using_stale = True
                self._note_payload_endpoint_failure(
                    "status",
                    error=reason,
                    signature=signature,
                    using_stale=True,
                )
                phase_timings["status_s"] = round(time.monotonic() - status_start, 3)
                data = dict(self._status_payload_cache)
                status_used_stale = True
                self._payload_errors = 0
                self._backoff_until = None
                self._clear_backoff_timer()
                self.diagnostics.clear_cloud_issue()
            else:
                self.payload_using_stale = False
                self._note_payload_endpoint_failure(
                    "status",
                    error=reason,
                    signature=signature,
                    using_stale=False,
                )
                self._payload_errors += 1
                jitter = random.uniform(1.0, 2.5)
                backoff_multiplier = 2 ** min(self._payload_errors - 1, 3)
                slow_floor = self._slow_interval_floor()
                backoff = max(slow_floor, slow_floor * backoff_multiplier * jitter)
                self._backoff_until = time.monotonic() + backoff
                self._schedule_backoff_timer(backoff)
                if self._payload_errors >= 2:
                    self.diagnostics.report_cloud_issue()
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
                    self.diagnostics.create_rate_limited_issue()
            else:
                is_server_error = 500 <= err.status < 600
                if is_server_error:
                    if self._http_errors >= 2:
                        self.diagnostics.report_cloud_issue()
                else:
                    self.diagnostics.clear_cloud_issue()
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
            self.last_failure_endpoint = str(url) if url is not None else None
            self.payload_using_stale = False
            self.payload_failure_kind = None
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
                self.diagnostics.clear_dns_issue()
            backoff_multiplier = 2 ** min(self._network_errors - 1, 3)
            jitter = random.uniform(1.0, 2.5)
            slow_floor = self._slow_interval_floor()
            backoff = max(slow_floor, slow_floor * backoff_multiplier * jitter)
            self._backoff_until = time.monotonic() + backoff
            self._schedule_backoff_timer(backoff)
            if self._network_errors >= 3:
                self.diagnostics.report_network_issue()
            if dns_failure and self._dns_failures >= 2:
                self.diagnostics.report_dns_issue()
            now_utc = dt_util.utcnow()
            self.last_failure_utc = now_utc
            self.last_failure_status = None
            self.last_failure_description = msg
            self.last_failure_response = None
            self.last_failure_source = "network"
            self.last_failure_endpoint = None
            self.payload_using_stale = False
            self.payload_failure_kind = None
            raise UpdateFailed(f"Error communicating with API: {msg}")
        finally:
            self.latency_ms = int((time.monotonic() - t0) * 1000)

        # Success path: reset counters, record last success
        if self._unauth_errors:
            # Clear any outstanding reauth issues on success
            self.diagnostics.clear_reauth_issue()
        self._unauth_errors = 0
        self._rate_limit_hits = 0
        self._http_errors = 0
        self._payload_errors = 0
        self.diagnostics.clear_network_issue()
        self._network_errors = 0
        self.diagnostics.clear_cloud_issue()
        self._backoff_until = None
        self._clear_backoff_timer()
        self._last_error = None
        self.diagnostics.clear_dns_issue()
        self._dns_failures = 0
        if not status_used_stale:
            self.last_success_utc = dt_util.utcnow()
            self.payload_using_stale = False
            self.payload_failure_kind = None
            self.last_failure_endpoint = None
        else:
            self._last_error = self.last_failure_description

        if not first_refresh:
            await self._async_run_refresh_plan(
                phase_timings,
                plan=FOLLOWUP_PLAN,
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

        charger_config: dict[str, dict[str, object]] = {}
        if not first_refresh and records:
            config_start = time.monotonic()
            charger_config = await self._async_resolve_charger_config(
                [sn for sn, _obj in records],
                keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
            )
            phase_timings["charger_config_s"] = round(
                time.monotonic() - config_start, 3
            )

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

            # Keep preference state stable when scheduler lookups temporarily omit it.
            charge_mode_pref = self._normalize_charge_mode_preference(
                charge_modes.get(sn)
            )
            if charge_mode_pref is None:
                charge_mode_pref = self._cached_charge_mode_preference(sn)
            if charge_mode_pref is None:
                charge_mode_pref = self._battery_profile_charge_mode_preference(sn)

            charge_mode = self._normalize_effective_charge_mode(
                obj.get("chargeMode")
                or obj.get("chargingMode")
                or (obj.get("sch_d") or {}).get("mode")
            )
            if charge_mode is None:
                if charge_mode_pref:
                    charge_mode = charge_mode_pref
                elif charging_now_flag:
                    charge_mode = "IMMEDIATE"
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
            config_values = charger_config.get(sn) or {}

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
            if PHASE_SWITCH_CONFIG_SETTING in config_values:
                entry["phase_switch_config"] = config_values[
                    PHASE_SWITCH_CONFIG_SETTING
                ]
            if DEFAULT_CHARGE_LEVEL_SETTING in config_values:
                entry["default_charge_level"] = config_values[
                    DEFAULT_CHARGE_LEVEL_SETTING
                ]
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
                    gateway_details: list[dict[str, int]] = []
                    gateway_last_connection_at: int | None = None
                    for gateway in gateway_connectivity:
                        if not isinstance(gateway, dict):
                            continue
                        gateway_status = _as_int(gateway.get("gwConnStatus"))
                        gateway_failure_reason = _as_int(
                            gateway.get("gwConnFailureReason")
                        )
                        gateway_last_conn_time = _sec(gateway.get("lastConnTime"))
                        if gateway_status == 0:
                            connected_count += 1
                        detail: dict[str, int] = {}
                        if gateway_status is not None:
                            detail["status"] = gateway_status
                        if gateway_failure_reason is not None:
                            detail["failure_reason"] = gateway_failure_reason
                        if gateway_last_conn_time is not None:
                            detail["last_connection_at"] = gateway_last_conn_time
                            if (
                                gateway_last_connection_at is None
                                or gateway_last_conn_time > gateway_last_connection_at
                            ):
                                gateway_last_connection_at = gateway_last_conn_time
                        if detail:
                            gateway_details.append(detail)
                    cur["gateway_connected_count"] = connected_count
                    if gateway_details:
                        cur["gateway_connectivity_details"] = gateway_details
                    if gateway_last_connection_at is not None:
                        cur["gateway_last_connection_at"] = gateway_last_connection_at
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
            await self._async_run_refresh_plan(
                phase_timings,
                plan=post_session_followup_plan(day_local_default),
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
            await self._async_run_refresh_plan(
                phase_timings,
                plan=HEATPUMP_FOLLOWUP_PLAN,
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
        self.evse_runtime.sync_desired_charging(data)

    async def _async_auto_resume(self, sn: str, snapshot: dict | None = None) -> None:
        await self.evse_runtime.async_auto_resume(sn, snapshot)

    def _determine_polling_state(self, data: dict[str, dict]) -> dict[str, object]:
        return self.evse_runtime.determine_polling_state(data)

    async def _async_resolve_charge_modes(
        self, serials: Iterable[str]
    ) -> dict[str, str | None]:
        return await self.evse_runtime.async_resolve_charge_modes(serials)

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
            self.diagnostics.clear_reauth_issue()
            return True

        if self._unauth_errors >= 2:
            self.diagnostics.create_reauth_issue()

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
        return await self.evse_runtime.async_start_charging(
            sn,
            requested_amps=requested_amps,
            connector_id=connector_id,
            hold_seconds=hold_seconds,
            allow_unplugged=allow_unplugged,
            fallback_amps=fallback_amps,
        )

    async def async_stop_charging(
        self,
        sn: str,
        *,
        hold_seconds: float = 90.0,
        fast_seconds: int = 60,
        allow_unplugged: bool = True,
    ) -> object:
        return await self.evse_runtime.async_stop_charging(
            sn,
            hold_seconds=hold_seconds,
            fast_seconds=fast_seconds,
            allow_unplugged=allow_unplugged,
        )

    def schedule_amp_restart(self, sn: str, delay: float = AMP_RESTART_DELAY_S) -> None:
        self.evse_runtime.schedule_amp_restart(sn, delay)

    async def _async_restart_after_amp_change(self, sn: str, delay: float) -> None:
        await self.evse_runtime.async_restart_after_amp_change(sn, delay)

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
        self.evse_runtime.kick_fast(seconds)

    def _streaming_active(self) -> bool:
        return self.evse_runtime.streaming_active()

    def _clear_streaming_state(self) -> None:
        self.evse_runtime.clear_streaming_state()

    def _streaming_response_ok(self, response: object) -> bool:
        return EvseRuntime.streaming_response_ok(response)

    def _streaming_duration_s(self, response: object) -> float:
        return EvseRuntime.streaming_duration_s(response)

    async def async_start_streaming(
        self,
        *,
        manual: bool = False,
        serial: str | None = None,
        expected_state: bool | None = None,
    ) -> None:
        await self.evse_runtime.async_start_streaming(
            manual=manual,
            serial=serial,
            expected_state=expected_state,
        )

    async def async_stop_streaming(self, *, manual: bool = False) -> None:
        await self.evse_runtime.async_stop_streaming(manual=manual)

    def _schedule_stream_stop(self, *, force: bool = False) -> None:
        self.evse_runtime.schedule_stream_stop(force=force)

    def _record_actual_charging(self, sn: str, charging: bool | None) -> None:
        self.evse_runtime.record_actual_charging(sn, charging)

    def set_charging_expectation(
        self,
        sn: str,
        should_charge: bool,
        hold_for: float = 90.0,
    ) -> None:
        self.evse_runtime.set_charging_expectation(sn, should_charge, hold_for)

    def _slow_interval_floor(self) -> int:
        return self.evse_runtime.slow_interval_floor()

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
        self.diagnostics.clear_scheduler_issue()

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
        self.diagnostics.report_scheduler_issue()

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
        return BatteryRuntime.normalize_battery_profile_key(value)

    @staticmethod
    def _battery_profile_label(profile: str | None) -> str | None:
        return BatteryRuntime.battery_profile_label(profile)

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
    def normalize_battery_sub_type(value: object) -> str | None:
        return EnphaseCoordinator._normalize_battery_sub_type(value)

    @staticmethod
    def _coerce_optional_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def coerce_optional_int(value: object) -> int | None:
        return EnphaseCoordinator._coerce_optional_int(value)

    @staticmethod
    def _coerce_optional_float(value: object) -> float | None:
        return coerce_optional_float(value)

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
        return coerce_optional_text(value)

    @staticmethod
    def coerce_optional_text(value: object) -> str | None:
        return EnphaseCoordinator._coerce_optional_text(value)

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
    def normalize_minutes_of_day(value: object) -> int | None:
        return EnphaseCoordinator._normalize_minutes_of_day(value)

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
    def time_to_minutes_of_day(value: dt_time | None) -> int | None:
        return EnphaseCoordinator._time_to_minutes_of_day(value)

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

    def redact_battery_payload(self, value: object) -> object:
        return self._redact_battery_payload(value)

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
            if getattr(self, "_battery_pending_profile", None)
            in {"cost_savings", "ai_optimisation"}
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
    def battery_write_access_confirmed(self) -> bool:
        owner = self.battery_user_is_owner
        installer = self.battery_user_is_installer
        return owner is True or installer is True

    @staticmethod
    def _battery_control_to_dict(
        value: BatteryControlCapability | None,
    ) -> dict[str, bool | None] | None:
        if value is None:
            return None
        return {
            "show": value.show,
            "enabled": value.enabled,
            "locked": value.locked,
            "show_day_schedule": value.show_day_schedule,
            "schedule_supported": value.schedule_supported,
            "force_schedule_supported": value.force_schedule_supported,
            "force_schedule_opted": value.force_schedule_opted,
        }

    @staticmethod
    def _battery_control_field(
        value: BatteryControlCapability | None, field_name: str
    ) -> bool | None:
        if value is None:
            return None
        return getattr(value, field_name)

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
        if getattr(self, "_battery_show_ai_opti_savings_mode", None):
            options.append("ai_optimisation")
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
    def battery_system_task(self) -> bool | None:
        return getattr(self, "_battery_system_task", None)

    @property
    def battery_profile_selection_available(self) -> bool:
        if not self.battery_controls_available:
            return False
        if self.battery_system_task is True:
            return False
        owner = self.battery_user_is_owner
        installer = self.battery_user_is_installer
        return not (owner is False and installer is False)

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
        if not self.battery_profile_selection_available:
            return False
        if getattr(self, "_battery_show_savings_mode", None) is False:
            return False
        return self.battery_selected_profile == "cost_savings"

    @property
    def battery_reserve_editable(self) -> bool:
        if not self.battery_profile_selection_available:
            return False
        rbd_control = getattr(self, "_battery_rbd_control", None)
        rbd_show = self._battery_control_field(rbd_control, "show")
        if rbd_show is not None:
            if rbd_show is False:
                return False
        elif getattr(self, "_battery_show_battery_backup_percentage", None) is False:
            return False
        if self._battery_control_field(rbd_control, "locked") is True:
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
        value = self._coerce_optional_int(
            getattr(self, "_battery_backup_percentage_min", None)
        )
        if value is None:
            return self._battery_min_soc_floor()
        return max(0, min(100, int(value)))

    @property
    def battery_reserve_max(self) -> int:
        profile = self.battery_selected_profile
        if profile == "backup_only":
            return 100
        value = self._coerce_optional_int(
            getattr(self, "_battery_backup_percentage_max", None)
        )
        if value is None:
            return 100
        return max(0, min(100, int(value)))

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
        if self.battery_system_task is True:
            return False
        # Prefer cfgControl.show (used by Enlighten app) over the
        # legacy hideChargeFromGrid flag which is unreliable on EMEA
        # sites. cfgControl.enabled appears to reflect current toggle
        # state on some homeowner payloads, so it is not authoritative
        # for control availability.
        cfg_show = self.battery_cfg_control_show
        if cfg_show is not None:
            if cfg_show is False:
                return False
        else:
            if getattr(self, "_battery_hide_charge_from_grid", None) is True:
                return False
        if self.battery_cfg_control_locked is True:
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
        show_day_schedule = self.battery_cfg_control_show_day_schedule
        if show_day_schedule is False:
            return False
        schedule_supported = self.battery_cfg_control_schedule_supported
        if schedule_supported is not None:
            return schedule_supported
        begin = getattr(self, "_battery_charge_begin_time", None)
        end = getattr(self, "_battery_charge_end_time", None)
        return begin is not None and end is not None

    @property
    def charge_from_grid_schedule_available(self) -> bool:
        if not self.charge_from_grid_schedule_supported:
            return False
        return self.battery_charge_from_grid_enabled is True

    @property
    def charge_from_grid_force_schedule_supported(self) -> bool:
        if not self.charge_from_grid_control_available:
            return False
        force_schedule_supported = self.battery_cfg_control_force_schedule_supported
        if force_schedule_supported is not None:
            return force_schedule_supported
        if getattr(self, "_battery_cfg_schedule_limit", None) is not None:
            return True
        if getattr(self, "_battery_cfg_schedule_id", None) is not None:
            return True
        if (
            getattr(self, "_battery_charge_from_grid_schedule_enabled", None)
            is not None
        ):
            return True
        return True

    @property
    def charge_from_grid_force_schedule_available(self) -> bool:
        if not self.charge_from_grid_force_schedule_supported:
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
    def battery_cfg_schedule_status(self) -> str | None:
        """Return the CFG schedule sync status (``pending`` or ``active``)."""
        return getattr(self, "_battery_cfg_schedule_status", None)

    @property
    def battery_cfg_schedule_pending(self) -> bool:
        """Return True if a CFG schedule change is pending Envoy sync."""
        return self.battery_cfg_schedule_status == "pending"

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
            self.battery_runtime._copy_dry_contact_settings_entry(entry)
            for entry in entries
            if isinstance(entry, dict)
        ]

    def dry_contact_unmatched_settings(self) -> list[dict[str, object]]:
        entries = getattr(self, "_dry_contact_unmatched_settings", [])
        if not isinstance(entries, list):
            return []
        return [
            self.battery_runtime._copy_dry_contact_settings_entry(entry)
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
        self.raise_grid_validation(
            key,
            placeholders=placeholders,
            message=message,
        )

    def raise_grid_validation(
        self,
        key: str,
        *,
        placeholders: dict[str, object] | None = None,
        message: str | None = None,
    ) -> None:
        raise_translated_service_validation(
            translation_domain=DOMAIN,
            translation_key=f"exceptions.{key}",
            translation_placeholders=placeholders,
            message=message,
        )

    def _grid_envoy_serial(self) -> str | None:
        return self.battery_runtime.grid_envoy_serial()

    async def _async_assert_grid_toggle_allowed(self) -> None:
        await self.battery_runtime.async_assert_grid_toggle_allowed()

    @property
    def storm_guard_state(self) -> str | None:
        return self._storm_guard_state

    @property
    def storm_guard_update_pending(self) -> bool:
        pending_state = getattr(self, "_storm_guard_pending_state", None)
        if pending_state is None:
            return False
        effective_state = self._normalize_storm_guard_state(
            getattr(self, "_storm_guard_state", None)
        )
        if effective_state == pending_state:
            self._storm_guard_pending_state = None
            self._storm_guard_pending_expires_mono = None
            return False
        expires_at = getattr(self, "_storm_guard_pending_expires_mono", None)
        if expires_at is None or time.monotonic() >= float(expires_at):
            self._storm_guard_pending_state = None
            self._storm_guard_pending_expires_mono = None
            return False
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
    def heatpump_runtime_state(self) -> dict[str, object]:
        return self.heatpump_runtime.heatpump_runtime_state

    @property
    def heatpump_runtime_state_last_error(self) -> str | None:
        return self.heatpump_runtime.heatpump_runtime_state_last_error

    @property
    def heatpump_daily_consumption(self) -> dict[str, object]:
        return self.heatpump_runtime.heatpump_daily_consumption

    @property
    def heatpump_daily_consumption_last_error(self) -> str | None:
        return self.heatpump_runtime.heatpump_daily_consumption_last_error

    @property
    def heatpump_power_w(self) -> float | None:
        return self.heatpump_runtime.heatpump_power_w

    @property
    def heatpump_power_sample_utc(self) -> datetime | None:
        return self.heatpump_runtime.heatpump_power_sample_utc

    @property
    def heatpump_power_start_utc(self) -> datetime | None:
        return self.heatpump_runtime.heatpump_power_start_utc

    @property
    def heatpump_power_device_uid(self) -> str | None:
        return self.heatpump_runtime.heatpump_power_device_uid

    @property
    def heatpump_power_source(self) -> str | None:
        return self.heatpump_runtime.heatpump_power_source

    @property
    def heatpump_power_last_error(self) -> str | None:
        return self.heatpump_runtime.heatpump_power_last_error

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
    def battery_dtg_control(self) -> dict[str, bool | None] | None:
        value = getattr(self, "_battery_dtg_control", None)
        return self._battery_control_to_dict(value)

    @property
    def battery_cfg_control(self) -> dict[str, bool | None] | None:
        value = getattr(self, "_battery_cfg_control", None)
        return self._battery_control_to_dict(value)

    @property
    def battery_rbd_control(self) -> dict[str, bool | None] | None:
        value = getattr(self, "_battery_rbd_control", None)
        return self._battery_control_to_dict(value)

    @property
    def battery_cfg_control_show(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        field = self._battery_control_field(value, "show")
        if field is not None:
            return field
        return getattr(self, "_battery_cfg_control_show", None)

    @property
    def battery_cfg_control_enabled(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        field = self._battery_control_field(value, "enabled")
        if field is not None:
            return field
        return getattr(self, "_battery_cfg_control_enabled", None)

    @property
    def battery_cfg_control_locked(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        return self._battery_control_field(value, "locked")

    @property
    def battery_cfg_control_show_day_schedule(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        return self._battery_control_field(value, "show_day_schedule")

    @property
    def battery_cfg_control_schedule_supported(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        field = self._battery_control_field(value, "schedule_supported")
        if field is not None:
            return field
        return getattr(self, "_battery_cfg_control_schedule_supported", None)

    @property
    def battery_cfg_control_force_schedule_supported(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        field = self._battery_control_field(value, "force_schedule_supported")
        if field is not None:
            return field
        return getattr(self, "_battery_cfg_control_force_schedule_supported", None)

    @property
    def battery_cfg_control_force_schedule_opted(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        return self._battery_control_field(value, "force_schedule_opted")

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
        self.diagnostics.clear_auth_settings_issue()

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
        self.diagnostics.report_auth_settings_issue()

    def note_auth_settings_unavailable(
        self,
        err: Exception | str | None = None,
    ) -> None:
        """Public wrapper used by entities."""

        self._note_auth_settings_unavailable(err)

    def _sync_session_history_issue(self) -> None:
        self.diagnostics.sync_session_history_issue()

    def _sync_site_energy_issue(self) -> None:
        self.diagnostics.sync_site_energy_issue()

    def _sync_battery_profile_pending_issue(self) -> None:
        """Raise/clear repair issue when a BatteryConfig profile change stalls."""
        self.diagnostics.sync_battery_profile_pending_issue()

    def sync_battery_profile_pending_issue(self) -> None:
        self._sync_battery_profile_pending_issue()

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
        self.evse_runtime.set_last_set_amps(sn, amps)

    def require_plugged(self, sn: str) -> None:
        self.evse_runtime.require_plugged(sn)

    def _ensure_serial_tracked(self, serial: str) -> bool:
        return self.evse_runtime.ensure_serial_tracked(serial)

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
        return self.evse_runtime.get_desired_charging(sn)

    def set_desired_charging(self, sn: str, desired: bool | None) -> None:
        self.evse_runtime.set_desired_charging(sn, desired)

    @staticmethod
    def _coerce_amp(value) -> int | None:
        return EvseRuntime.coerce_amp(value)

    def _amp_limits(self, sn: str) -> tuple[int | None, int | None]:
        return self.evse_runtime.amp_limits(sn)

    def _apply_amp_limits(self, sn: str, amps: int | float | str | None) -> int:
        return self.evse_runtime.apply_amp_limits(sn, amps)

    def pick_start_amps(
        self, sn: str, requested: int | float | str | None = None, fallback: int = 32
    ) -> int:
        return self.evse_runtime.pick_start_amps(sn, requested, fallback)

    async def _get_charge_mode(self, sn: str) -> str | None:
        return await self.evse_runtime.async_get_charge_mode(sn)

    async def _get_green_battery_setting(
        self, sn: str
    ) -> tuple[bool | None, bool] | None:
        return await self.evse_runtime.async_get_green_battery_setting(sn)

    async def _get_auth_settings(
        self, sn: str
    ) -> tuple[bool | None, bool | None, bool, bool] | None:
        return await self.evse_runtime.async_get_auth_settings(sn)

    def set_charge_mode_cache(self, sn: str, mode: str) -> None:
        self.evse_runtime.set_charge_mode_cache(sn, mode)

    def set_green_battery_cache(
        self, sn: str, enabled: bool, supported: bool = True
    ) -> None:
        self.evse_runtime.set_green_battery_cache(sn, enabled, supported)

    def set_app_auth_cache(self, sn: str, enabled: bool) -> None:
        self.evse_runtime.set_app_auth_cache(sn, enabled)

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
        return coerce_optional_bool(value)

    @staticmethod
    def coerce_optional_bool(value: object) -> bool | None:
        return EnphaseCoordinator._coerce_optional_bool(value)

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
        self.battery_runtime.parse_battery_status_payload(payload)

    @staticmethod
    def _normalize_storm_guard_state(value) -> str | None:
        return BatteryRuntime.normalize_storm_guard_state(value)

    def _clear_storm_guard_pending(self) -> None:
        self.battery_runtime.clear_storm_guard_pending()

    def _set_storm_guard_pending(self, target_state: str) -> None:
        self.battery_runtime.set_storm_guard_pending(target_state)

    def _sync_storm_guard_pending(self, effective_state: str | None = None) -> None:
        self.battery_runtime.sync_storm_guard_pending(effective_state)

    def _clear_battery_pending(self) -> None:
        self.battery_runtime.clear_battery_pending()

    def _set_battery_pending(
        self,
        *,
        profile: str,
        reserve: int,
        sub_type: str | None,
        require_exact_settings: bool = True,
    ) -> None:
        self.battery_runtime.set_battery_pending(
            profile=profile,
            reserve=reserve,
            sub_type=sub_type,
            require_exact_settings=require_exact_settings,
        )

    def _assert_battery_profile_write_allowed(self) -> None:
        self.battery_runtime.assert_battery_profile_write_allowed()

    def _normalize_battery_reserve_for_profile(self, profile: str, reserve: int) -> int:
        return self.battery_runtime.normalize_battery_reserve_for_profile(
            profile, reserve
        )

    def _effective_profile_matches_pending(self) -> bool:
        return self.battery_runtime.effective_profile_matches_pending()

    def _remember_battery_reserve(
        self, profile: str | None, reserve: int | None
    ) -> None:
        self.battery_runtime.remember_battery_reserve(profile, reserve)

    def parse_battery_profile_payload(self, payload: object) -> None:
        self.battery_runtime.parse_battery_profile_payload(payload)

    def _parse_battery_settings_payload(
        self, payload: object, *, clear_missing_schedule_times: bool = False
    ) -> None:
        self.battery_runtime.parse_battery_settings_payload(
            payload,
            clear_missing_schedule_times=clear_missing_schedule_times,
        )

    def parse_battery_settings_payload(
        self, payload: object, *, clear_missing_schedule_times: bool = False
    ) -> None:
        self.battery_runtime.parse_battery_settings_payload(
            payload,
            clear_missing_schedule_times=clear_missing_schedule_times,
        )

    def parse_battery_site_settings_payload(self, payload: object) -> None:
        self.battery_runtime.parse_battery_site_settings_payload(payload)

    def dry_contact_settings_matches(
        self, members: Iterable[dict[str, object]]
    ) -> tuple[list[dict[str, object] | None], list[dict[str, object]]]:
        return self.battery_runtime.dry_contact_settings_matches(list(members))

    def _parse_dry_contact_settings_payload(self, payload: object) -> None:
        self.battery_runtime.parse_dry_contact_settings_payload(payload)

    def parse_dry_contact_settings_payload(self, payload: object) -> None:
        self.battery_runtime.parse_dry_contact_settings_payload(payload)

    def _parse_grid_control_check_payload(self, payload: object) -> None:
        self.battery_runtime.parse_grid_control_check_payload(payload)

    def parse_grid_control_check_payload(self, payload: object) -> None:
        self.battery_runtime.parse_grid_control_check_payload(payload)

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

    def battery_profile_devices_payload(self) -> list[dict[str, object]] | None:
        return self._battery_profile_devices_payload()

    def _target_reserve_for_profile(self, profile: str) -> int:
        return self.battery_runtime.target_reserve_for_profile(profile)

    def _current_savings_sub_type(self) -> str | None:
        return self.battery_runtime.current_savings_sub_type()

    async def _async_apply_battery_profile(
        self,
        *,
        profile: str,
        reserve: int,
        sub_type: str | None = None,
        require_exact_pending_match: bool = True,
    ) -> None:
        await self.battery_runtime.async_apply_battery_profile(
            profile=profile,
            reserve=reserve,
            sub_type=sub_type,
            require_exact_pending_match=require_exact_pending_match,
        )

    def _assert_battery_settings_write_allowed(self) -> None:
        self.battery_runtime.assert_battery_settings_write_allowed()

    def _current_charge_from_grid_schedule_window(self) -> tuple[int, int]:
        return self.battery_runtime.current_charge_from_grid_schedule_window()

    def _battery_itc_disclaimer_value(self) -> str:
        return self.battery_runtime.battery_itc_disclaimer_value()

    async def _async_apply_battery_settings(self, payload: dict[str, object]) -> None:
        await self.battery_runtime.async_apply_battery_settings(payload)

    def _raise_schedule_update_validation_error(
        self, err: aiohttp.ClientResponseError
    ) -> None:
        self.battery_runtime.raise_schedule_update_validation_error(err)

    async def _async_update_battery_schedule(
        self,
        schedule_id: str,
        *,
        start_time: str,
        end_time: str,
        limit: int,
        days: list[int],
        timezone: str,
    ) -> None:
        await self.battery_runtime.async_update_battery_schedule(
            schedule_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            days=days,
            timezone=timezone,
        )

    async def _async_refresh_battery_status(self, *, force: bool = False) -> None:
        await self.battery_runtime.async_refresh_battery_status(force=force)

    def parse_battery_status_payload(self, payload: object) -> None:
        self.battery_runtime.parse_battery_status_payload(payload)

    def _backup_history_tzinfo(self) -> _tz | ZoneInfo:
        return self.battery_runtime.backup_history_tzinfo()

    def _parse_battery_backup_history_payload(
        self,
        payload: object,
    ) -> list[dict[str, object]] | None:
        return self.battery_runtime.parse_battery_backup_history_payload(payload)

    async def _async_refresh_battery_backup_history(
        self, *, force: bool = False
    ) -> None:
        await self.battery_runtime.async_refresh_battery_backup_history(force=force)

    async def _async_refresh_battery_settings(self, *, force: bool = False) -> None:
        await self.battery_runtime.async_refresh_battery_settings(force=force)

    async def _async_refresh_battery_schedules(self) -> None:
        await self.battery_runtime.async_refresh_battery_schedules()

    def parse_battery_schedules_payload(self, payload: object) -> None:
        self.battery_runtime.parse_battery_schedules_payload(payload)

    def _parse_battery_schedules_payload(self, payload: object) -> None:
        self.battery_runtime.parse_battery_schedules_payload(payload)

    async def _async_refresh_battery_site_settings(
        self, *, force: bool = False
    ) -> None:
        await self.battery_runtime.async_refresh_battery_site_settings(force=force)

    async def _async_refresh_grid_control_check(self, *, force: bool = False) -> None:
        await self.battery_runtime.async_refresh_grid_control_check(force=force)

    async def async_refresh_grid_control_check(self, *, force: bool = False) -> None:
        await self._async_refresh_grid_control_check(force=force)

    async def _async_refresh_dry_contact_settings(self, *, force: bool = False) -> None:
        await self.battery_runtime.async_refresh_dry_contact_settings(force=force)

    async def async_set_system_profile(self, profile_key: str) -> None:
        await self.battery_runtime.async_set_system_profile(profile_key)

    async def async_set_battery_reserve(self, reserve: int) -> None:
        await self.battery_runtime.async_set_battery_reserve(reserve)

    async def async_set_savings_use_battery_after_peak(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_savings_use_battery_after_peak(enabled)

    async def async_cancel_pending_profile_change(self) -> None:
        await self.battery_runtime.async_cancel_pending_profile_change()

    async def async_set_charge_from_grid(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_charge_from_grid(enabled)

    async def async_set_charge_from_grid_schedule_enabled(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_charge_from_grid_schedule_enabled(enabled)

    async def async_set_charge_from_grid_schedule_time(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
    ) -> None:
        await self.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=start,
            end=end,
        )

    async def async_set_cfg_schedule_limit(self, limit: int) -> None:
        await self.battery_runtime.async_set_cfg_schedule_limit(limit)

    async def async_update_cfg_schedule(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
        limit: int | None = None,
    ) -> None:
        await self.battery_runtime.async_update_cfg_schedule(
            start=start,
            end=end,
            limit=limit,
        )

    async def async_request_grid_toggle_otp(self) -> None:
        await self.battery_runtime.async_request_grid_toggle_otp()

    async def async_set_grid_mode(self, mode: str, otp: str) -> None:
        await self.battery_runtime.async_set_grid_mode(mode, otp)

    async def async_set_grid_connection(
        self, enabled: bool, *, otp: str | None = None
    ) -> None:
        await self.battery_runtime.async_set_grid_connection(enabled, otp=otp)

    async def async_set_battery_shutdown_level(self, level: int) -> None:
        await self.battery_runtime.async_set_battery_shutdown_level(level)

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
        return self.battery_runtime.parse_storm_alert(payload)

    def _storm_alert_status_is_inactive(self, status: str | None) -> bool:
        return self.battery_runtime.storm_alert_status_is_inactive(status)

    def _storm_alert_is_active(self, alert: dict[str, object]) -> bool:
        return self.battery_runtime.storm_alert_is_active(alert)

    async def _async_refresh_storm_guard_profile(self, *, force: bool = False) -> None:
        await self.battery_runtime.async_refresh_storm_guard_profile(force=force)

    async def async_refresh_storm_guard_profile(self, *, force: bool = False) -> None:
        await self._async_refresh_storm_guard_profile(force=force)

    async def _async_refresh_storm_alert(self, *, force: bool = False) -> None:
        await self.battery_runtime.async_refresh_storm_alert(force=force)

    async def async_refresh_storm_alert(self, *, force: bool = False) -> None:
        await self._async_refresh_storm_alert(force=force)

    async def async_opt_out_all_storm_alerts(self) -> None:
        await self.battery_runtime.async_opt_out_all_storm_alerts()

    async def async_set_storm_guard_enabled(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_storm_guard_enabled(enabled)

    async def async_set_storm_evse_enabled(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_storm_evse_enabled(enabled)

    async def _async_resolve_green_battery_settings(
        self, serials: Iterable[str]
    ) -> dict[str, tuple[bool | None, bool]]:
        return await self.evse_runtime.async_resolve_green_battery_settings(serials)

    async def _async_resolve_auth_settings(
        self, serials: Iterable[str]
    ) -> dict[str, tuple[bool | None, bool | None, bool, bool]]:
        return await self.evse_runtime.async_resolve_auth_settings(serials)

    async def _async_resolve_charger_config(
        self,
        serials: Iterable[str],
        *,
        keys: Iterable[str],
    ) -> dict[str, dict[str, object]]:
        return await self.evse_runtime.async_resolve_charger_config(
            serials,
            keys=keys,
        )

    def _resolve_charge_mode_pref(self, sn: str) -> str | None:
        return self.evse_runtime.resolve_charge_mode_pref(sn)

    def _cached_charge_mode_preference(
        self, sn: str, *, now: float | None = None
    ) -> str | None:
        return self.evse_runtime.cached_charge_mode_preference(sn, now=now)

    def _battery_profile_charge_mode_preference(self, sn: str) -> str | None:
        return self.evse_runtime.battery_profile_charge_mode_preference(sn)

    @staticmethod
    def _normalize_charge_mode_preference(value: object) -> str | None:
        return EvseRuntime.normalize_charge_mode_preference(value)

    def _normalize_effective_charge_mode(self, value: object) -> str | None:
        return self.evse_runtime.normalize_effective_charge_mode(value)

    def _charge_mode_start_preferences(self, sn: str) -> ChargeModeStartPreferences:
        return self.evse_runtime.charge_mode_start_preferences(sn)

    async def _ensure_charge_mode(self, sn: str, target_mode: str) -> None:
        await self.evse_runtime.async_ensure_charge_mode(sn, target_mode)


install_state_descriptors(EnphaseCoordinator)
