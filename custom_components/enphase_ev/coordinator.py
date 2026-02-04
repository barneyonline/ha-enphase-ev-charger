from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as _tz
from http import HTTPStatus
from typing import Callable, Iterable

import aiohttp
from email.utils import parsedate_to_datetime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

try:
    from homeassistant.exceptions import ServiceValidationError
except ImportError:  # pragma: no cover - older HA cores
    from homeassistant.exceptions import HomeAssistantError

    class ServiceValidationError(HomeAssistantError):
        """Fallback for Home Assistant cores lacking ServiceValidationError."""

        def __init__(
            self,
            message: str | None = None,
            *,
            translation_domain: str | None = None,
            translation_key: str | None = None,
            translation_placeholders: dict[str, object] | None = None,
            **_: object,
        ) -> None:
            super().__init__(message)
            self.translation_domain = translation_domain
            self.translation_key = translation_key
            self.translation_placeholders = translation_placeholders


from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AuthTokens,
    AuthSettingsUnavailable,
    EnlightenAuthInvalidCredentials,
    EnlightenAuthMFARequired,
    EnlightenAuthUnavailable,
    EnphaseEVClient,
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
    CONF_PASSWORD,
    CONF_REMEMBER_PASSWORD,
    CONF_SCAN_INTERVAL,
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
    OPT_API_TIMEOUT,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
    OPT_SESSION_HISTORY_INTERVAL,
    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
    SAFE_LIMIT_AMPS,
)
from .energy import EnergyManager
from .session_history import (
    MIN_SESSION_HISTORY_CACHE_TTL,
    SESSION_HISTORY_CONCURRENCY,
    SESSION_HISTORY_FAILURE_BACKOFF_S,
    SessionHistoryManager,
)
from .summary import SummaryStore

_LOGGER = logging.getLogger(__name__)
GREEN_BATTERY_CACHE_TTL = 300.0
AUTH_SETTINGS_CACHE_TTL = 300.0
STORM_GUARD_CACHE_TTL = 300.0
STORM_ALERT_CACHE_TTL = 60.0

ACTIVE_CONNECTOR_STATUSES = {"CHARGING", "FINISHING", "SUSPENDED"}
ACTIVE_SUSPENDED_PREFIXES = ("SUSPENDED_EV",)
SUSPENDED_EVSE_STATUS = "SUSPENDED_EVSE"
FAST_TOGGLE_POLL_HOLD_S = 60
AMP_RESTART_DELAY_S = 30.0
STREAMING_DEFAULT_DURATION_S = 900.0


@dataclass
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


@dataclass
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
        self._refresh_lock = asyncio.Lock()
        # Nominal voltage for estimated power when API omits power; user-configurable
        self._nominal_v = 240
        if config_entry is not None:
            try:
                self._nominal_v = int(
                    config_entry.options.get(OPT_NOMINAL_VOLTAGE, 240)
                )
            except Exception:
                self._nominal_v = 240
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
        self.summary = SummaryStore(lambda: self.client, logger=_LOGGER)
        self.energy = EnergyManager(
            client_provider=lambda: self.client,
            site_id=self.site_id,
            logger=_LOGGER,
            summary_invalidator=self.summary.invalidate,
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
        self._storm_alert_active: bool | None = None
        self._storm_guard_cache_until: float | None = None
        self._storm_alert_cache_until: float | None = None
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
        self._has_successful_refresh = False
        super_kwargs = {
            "name": DOMAIN,
            "update_interval": timedelta(seconds=interval),
        }
        if config_entry is not None:
            super_kwargs["config_entry"] = config_entry
        try:
            super().__init__(
                hass,
                _LOGGER,
                **super_kwargs,
            )
        except TypeError:
            # Older HA cores (used in some test harnesses) do not accept the
            # config_entry kwarg yet. Retry without it for compatibility.
            super_kwargs.pop("config_entry", None)
            super().__init__(
                hass,
                _LOGGER,
                **super_kwargs,
            )
        # Ensure config_entry is stored after super().__init__ in case older
        # cores overwrite the attribute with None.
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
        raise AttributeError(f"{type(self).__name__} has no attribute {name!r}")

    async def _async_setup(self) -> None:
        """Prepare lightweight state before the first refresh."""
        self._phase_timings = {}

    @property
    def phase_timings(self) -> dict[str, float]:
        """Return the most recent phase timings."""
        return dict(self._phase_timings)

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
        return round(total, 3)

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
        self._session_history_cache_shim[cache_key] = (time.monotonic(), sessions)
        return sessions

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
            "backoff_ends_utc": _iso(self.backoff_ends_utc),
            "network_errors": getattr(self, "_network_errors", 0),
            "http_errors": getattr(self, "_http_errors", 0),
            "rate_limit_hits": getattr(self, "_rate_limit_hits", 0),
            "dns_errors": getattr(self, "_dns_failures", 0),
            "phase_timings": self.phase_timings,
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
        }
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
        site_energy_age = None
        site_flows = {}
        site_meta = {}
        if energy_manager is not None:
            if hasattr(energy_manager, "_site_energy_cache_age"):
                try:
                    site_energy_age = energy_manager._site_energy_cache_age()
                except Exception:
                    site_energy_age = None
            site_flows = getattr(energy_manager, "site_energy", None) or {}
            site_meta = getattr(energy_manager, "_site_energy_meta", None) or {}
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
        return metrics

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
            await self.energy._async_refresh_site_energy()
            try:
                await self._async_refresh_storm_guard_profile()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Storm Guard profile refresh failed for site %s: %s",
                    self.site_id,
                    err,
                )
            try:
                await self._async_refresh_storm_alert()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Storm Guard alert refresh failed for site %s: %s",
                    self.site_id,
                    err,
                )
            self.last_success_utc = dt_util.utcnow()
            self.latency_ms = int((time.monotonic() - t0) * 1000)
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
            if not text:
                return None

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
                self._phase_timings = dict(phase_timings)
                return fallback_data
            # Respect Retry-After and create a warning issue on repeated 429
            self._last_error = f"HTTP {err.status}"
            self._network_errors = 0
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
            raw_payload = err.message
            description = _extract_description(raw_payload)
            reason = (err.message or err.__class__.__name__).strip()
            now_utc = dt_util.utcnow()
            self.last_failure_utc = now_utc
            self.last_failure_status = err.status
            if description is None:
                try:
                    description = HTTPStatus(int(err.status)).phrase
                except Exception:
                    description = reason or "HTTP error"
            self.last_failure_description = description
            self.last_failure_response = (
                raw_payload if raw_payload is not None else (reason or None)
            )
            self.last_failure_source = "http"
            raise UpdateFailed(f"Cloud error {err.status}: {reason}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            msg = str(err).strip()
            if not msg:
                msg = err.__class__.__name__
            self._last_error = msg
            self._network_errors += 1
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

        storm_guard_start = time.monotonic()
        try:
            await self._async_refresh_storm_guard_profile()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Storm Guard profile refresh failed for site %s: %s",
                self.site_id,
                err,
            )
        phase_timings["storm_guard_s"] = round(time.monotonic() - storm_guard_start, 3)
        storm_alert_start = time.monotonic()
        try:
            await self._async_refresh_storm_alert()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Storm Guard alert refresh failed for site %s: %s",
                self.site_id,
                err,
            )
        phase_timings["storm_alert_s"] = round(time.monotonic() - storm_alert_start, 3)

        prev_data = self.data if isinstance(self.data, dict) else {}
        first_refresh = not self._has_successful_refresh
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
            not charge_mode_candidates
            and records
            and not self.scheduler_available
            and not self._scheduler_backoff_active()
        ):
            try:
                await self._get_charge_mode(records[0][0])
            except Exception:
                pass
        if charge_mode_candidates:
            unique_candidates = list(dict.fromkeys(charge_mode_candidates))
            charge_start = time.monotonic()
            charge_modes = await self._async_resolve_charge_modes(unique_candidates)
            phase_timings["charge_mode_s"] = round(time.monotonic() - charge_start, 3)

        green_settings: dict[str, tuple[bool | None, bool]] = {}
        if records:
            green_start = time.monotonic()
            green_settings = await self._async_resolve_green_battery_settings(
                [sn for sn, _obj in records]
            )
            phase_timings["green_settings_s"] = round(time.monotonic() - green_start, 3)

        auth_settings: dict[str, tuple[bool | None, bool | None, bool, bool]] = {}
        if records:
            auth_start = time.monotonic()
            auth_settings = await self._async_resolve_auth_settings(
                [sn for sn, _obj in records]
            )
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
                if normalized in ("true", "1", "yes", "y"):
                    return True
                if normalized in ("false", "0", "no", "n"):
                    return False
            return None

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

        for sn, obj in records:
            conn0 = (obj.get("connectors") or [{}])[0]
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
                        ses_kwh = round(float(ses_kwh) / 1000.0, 3)
                    else:
                        ses_kwh = round(float(ses_kwh), 3)
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
                "session_start": _sec(sess.get("start_time")),
                "session_end": session_end,
                "session_plug_in_at": sess.get("plg_in_at"),
                "session_plug_out_at": sess.get("plg_out_at"),
                "last_reported_at": last_rpt,
                "offline_since": obj.get("offlineAt"),
                "commissioned": _as_bool(commissioned_val),
                "schedule_status": sch.get("status"),
                "schedule_type": sch_info0.get("type") or sch.get("status"),
                "schedule_start": sch_info0.get("startTime"),
                "schedule_end": sch_info0.get("endTime"),
                "charge_mode": charge_mode,
                # Expose scheduler preference explicitly for entities that care
                "charge_mode_pref": charge_mode_pref,
                "charging_level": charging_level,
                "storm_guard_state": self._storm_guard_state,
                "storm_evse_enabled": self._storm_evse_enabled,
                "session_charge_level": session_charge_level,
                "session_cost": session_cost,
                "operating_v": self._operating_v.get(sn),
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
                cur["phase_mode"] = item.get("phaseMode")
                cur["status"] = item.get("status")
                supports_use_battery = _as_optional_bool(item.get("supportsUseBattery"))
                if supports_use_battery is not None:
                    cur["green_battery_supported"] = supports_use_battery
                    if not supports_use_battery:
                        cur.pop("green_battery_enabled", None)
                conn = item.get("activeConnection")
                if isinstance(conn, str):
                    conn = conn.strip()
                if conn:
                    cur["connection"] = conn
                net_cfg = item.get("networkConfig")
                ip_addr = None
                if isinstance(net_cfg, dict):
                    ip_addr = net_cfg.get("ipaddr") or net_cfg.get("ip")
                else:
                    entries: list = []
                    if isinstance(net_cfg, list):
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
                        if isinstance(entry, dict):
                            candidate = entry.get("ipaddr") or entry.get("ip")
                            if candidate:
                                ip_addr = candidate
                                if str(entry.get("connectionStatus")) in (
                                    "1",
                                    "true",
                                    "True",
                                ):
                                    break
                                continue
                        elif isinstance(entry, str):
                            parts = {}
                            for piece in entry.split(","):
                                if "=" in piece:
                                    k, v = piece.split("=", 1)
                                    parts[k.strip()] = v.strip()
                            candidate = parts.get("ipaddr") or parts.get("ip")
                            if candidate:
                                ip_addr = candidate
                                if parts.get("connectionStatus") in (
                                    "1",
                                    "true",
                                    "True",
                                ):
                                    break
                    if isinstance(ip_addr, str) and not ip_addr:
                        ip_addr = None
                if ip_addr:
                    cur["ip_address"] = str(ip_addr)
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
                # Last reported: prefer summary if present
                if item.get("lastReportedAt"):
                    cur["last_reported_at"] = item.get("lastReportedAt")
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

        site_energy_start = time.monotonic()
        await self.energy._async_refresh_site_energy()
        self._sync_site_energy_issue()
        phase_timings["site_energy_s"] = round(time.monotonic() - site_energy_start, 3)

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
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Coordinator refresh timings for site %s: %s",
                self.site_id,
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
            last_attempt = self._auto_resume_attempts.get(sn_str)
            if last_attempt is not None and (now - last_attempt) < 120:
                continue
            self._auto_resume_attempts[sn_str] = now
            _LOGGER.debug(
                "Scheduling auto-resume for charger %s after connector reported %s",
                sn_str,
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
                sn_str,
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
                sn_str,
                err,
            )
            return
        self.set_last_set_amps(sn_str, amps)
        if isinstance(result, dict) and result.get("status") == "not_ready":
            _LOGGER.debug(
                "Auto-resume start_charging for charger %s returned not_ready; will retry later",
                sn_str,
            )
            return
        if prefs.enforce_mode:
            await self._ensure_charge_mode(sn_str, prefs.enforce_mode)
        _LOGGER.info(
            "Auto-resume start_charging issued for charger %s after suspension",
            sn_str,
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
        slow_default = (
            self.update_interval.total_seconds()
            if self.update_interval
            else DEFAULT_SLOW_POLL_INTERVAL
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
                    _LOGGER.debug("Charge mode lookup failed for %s: %s", sn, response)
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
                _LOGGER.debug("Unexpected error refreshing Enlighten auth: %s", err)
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
                display,
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
                sn_str,
                err,
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
                sn_str,
                reason,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Amp restart start_charging failed for charger %s: %s",
                sn_str,
                err,
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
                _LOGGER.debug("Live stream start failed: %s", err)
                return
        else:
            start_ok = self._streaming_response_ok(response)
            if not start_ok and not was_active:
                _LOGGER.debug("Live stream start rejected: %s", response)
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
            _LOGGER.debug("Live stream stop failed: %s", err)
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
                    _LOGGER.debug("Live stream stop failed: %s", err)
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

    def _note_scheduler_unavailable(
        self,
        err: Exception | str | None = None,
        *,
        status: int | None = None,
        raw_payload: str | None = None,
    ) -> None:
        """Record scheduler outage and raise a repair issue."""
        reason = str(err).strip() if err else ""
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
        self.last_failure_response = raw_payload or reason
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

    @property
    def storm_guard_state(self) -> str | None:
        return self._storm_guard_state

    @property
    def storm_evse_enabled(self) -> bool | None:
        return self._storm_evse_enabled

    @property
    def storm_alert_active(self) -> bool | None:
        return self._storm_alert_active

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

    def _note_auth_settings_unavailable(
        self,
        err: Exception | str | None = None,
    ) -> None:
        """Record auth settings outage and raise a repair issue."""
        reason = str(err).strip() if err else ""
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
            _LOGGER.info("Discovered Enphase charger serial=%s during update", sn)
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
        active = self._coerce_optional_bool(payload.get("criticalAlertActive"))
        if active is not None:
            return active
        alerts = payload.get("stormAlerts")
        if isinstance(alerts, list):
            return bool(alerts)
        return None

    async def _async_refresh_storm_guard_profile(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._storm_guard_cache_until:
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
        state, evse = self._parse_storm_guard_profile(payload)
        if state is not None:
            self._storm_guard_state = state
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

    async def async_set_storm_guard_enabled(self, enabled: bool) -> None:
        if self._storm_evse_enabled is None:
            await self._async_refresh_storm_guard_profile(force=True)
        if self._storm_evse_enabled is None:
            raise ServiceValidationError("Storm Guard settings are unavailable.")
        await self.client.set_storm_guard(
            enabled=bool(enabled),
            evse_enabled=bool(self._storm_evse_enabled),
        )
        self._storm_guard_state = "enabled" if enabled else "disabled"
        self._storm_guard_cache_until = time.monotonic() + STORM_GUARD_CACHE_TTL

    async def async_set_storm_evse_enabled(self, enabled: bool) -> None:
        if self._storm_guard_state is None:
            await self._async_refresh_storm_guard_profile(force=True)
        if self._storm_guard_state is None:
            raise ServiceValidationError("Storm Guard settings are unavailable.")
        await self.client.set_storm_guard(
            enabled=self._storm_guard_state == "enabled",
            evse_enabled=bool(enabled),
        )
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
                        "Green battery setting lookup failed for %s: %s", sn, response
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
                        "Auth settings lookup failed for %s: %s", sn, response
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
                sn_str,
                err,
            )
            return
        self.set_charge_mode_cache(sn_str, target_mode)
