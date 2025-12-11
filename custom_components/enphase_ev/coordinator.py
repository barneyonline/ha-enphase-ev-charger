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
    EnlightenAuthInvalidCredentials,
    EnlightenAuthMFARequired,
    EnlightenAuthUnavailable,
    EnphaseEVClient,
    Unauthorized,
    async_authenticate,
)
from .const import (
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
    ISSUE_NETWORK_UNREACHABLE,
    ISSUE_DNS_RESOLUTION,
    ISSUE_CLOUD_ERRORS,
    OPT_API_TIMEOUT,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
    OPT_SESSION_HISTORY_INTERVAL,
    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
)
from .session_history import (
    MIN_SESSION_HISTORY_CACHE_TTL,
    SESSION_HISTORY_CONCURRENCY,
    SESSION_HISTORY_FAILURE_BACKOFF_S,
    SessionHistoryManager,
)
from .summary import SummaryStore

_LOGGER = logging.getLogger(__name__)

LIFETIME_DROP_JITTER_KWH = 0.02
LIFETIME_RESET_DROP_THRESHOLD_KWH = 0.5
LIFETIME_RESET_FLOOR_KWH = 5.0
LIFETIME_RESET_RATIO = 0.5
LIFETIME_CONFIRM_TOLERANCE_KWH = 0.05
LIFETIME_CONFIRM_COUNT = 2
LIFETIME_CONFIRM_WINDOW_S = 180.0
SITE_ENERGY_CACHE_TTL = 900.0
ACTIVE_CONNECTOR_STATUSES = {"CHARGING", "FINISHING", "SUSPENDED"}
ACTIVE_SUSPENDED_PREFIXES = ("SUSPENDED_EV",)
SUSPENDED_EVSE_STATUS = "SUSPENDED_EVSE"
FAST_TOGGLE_POLL_HOLD_S = 60
AMP_RESTART_DELAY_S = 30.0


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
class LifetimeGuardState:
    last: float | None = None
    pending_value: float | None = None
    pending_ts: float | None = None
    pending_count: int = 0


@dataclass
class ChargeModeStartPreferences:
    mode: str | None = None
    include_level: bool | None = None
    strict: bool = False
    enforce_mode: str | None = None


@dataclass
class SiteEnergyFlow:
    """Aggregated site-level energy flow."""

    value_kwh: float | None
    bucket_count: int
    fields_used: list[str]
    start_date: str | None
    last_report_date: datetime | None
    update_pending: bool | None
    source_unit: str = "Wh"
    last_reset_at: str | None = None


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
        self._network_issue_reported = False
        self._dns_failures = 0
        self._dns_issue_reported = False
        self.summary = SummaryStore(lambda: self.client, logger=_LOGGER)
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
        self._lifetime_guard: dict[str, LifetimeGuardState] = {}
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
        # Site-level lifetime energy cache
        self.site_energy: dict[str, SiteEnergyFlow] = {}
        self._site_energy_meta: dict[str, object] = {}
        self._site_energy_cache_ts: float | None = None
        self._site_energy_cache_ttl: float = SITE_ENERGY_CACHE_TTL
        self._site_energy_guard: dict[str, LifetimeGuardState] = {}
        self._site_energy_last_reset: dict[str, str | None] = {}
        self._site_energy_force_refresh = False

    def __setattr__(self, name, value):
        if name == "_async_fetch_sessions_today" and hasattr(self, "session_history"):
            object.__setattr__(self, name, value)
            self.session_history.set_fetch_override(value)
            return
        super().__setattr__(name, value)

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

    def _site_energy_cache_age(self) -> float | None:
        """Return the age of the cached site energy payload."""
        cache_ts = getattr(self, "_site_energy_cache_ts", None)
        if cache_ts is None:
            return None
        try:
            return time.monotonic() - cache_ts
        except Exception:
            return None

    def _invalidate_site_energy_cache(self) -> None:
        """Drop the cached site energy payload."""
        self._site_energy_cache_ts = None

    def _parse_site_energy_timestamp(self, value) -> datetime | None:
        """Best-effort parsing for last_report_date fields."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                iv = int(value)
                if iv > 10**12:
                    iv = iv // 1000
                return datetime.fromtimestamp(iv, tz=_tz.utc)
            except Exception:  # noqa: BLE001
                return None
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return None
            if cleaned.isdigit():
                return self._parse_site_energy_timestamp(int(cleaned))
            parsed_dt = None
            try:
                parsed_dt = dt_util.parse_datetime(cleaned)
            except Exception:  # noqa: BLE001
                parsed_dt = None
            if parsed_dt is None:
                try:
                    parsed_date = dt_util.parse_date(cleaned)
                except Exception:  # noqa: BLE001
                    parsed_date = None
                if parsed_date is not None:
                    parsed_dt = datetime.combine(
                        parsed_date, datetime.min.time(), tzinfo=_tz.utc
                    )
            if parsed_dt is None:
                return None
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=_tz.utc)
            return parsed_dt.astimezone(_tz.utc)
        return None

    @staticmethod
    def _coerce_energy_value(value) -> float | None:
        """Normalize numeric bucket values into floats."""
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except Exception:  # noqa: BLE001
                return None
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            try:
                return float(s)
            except Exception:  # noqa: BLE001
                return None
        return None

    def _sum_energy_buckets(self, values) -> tuple[float, int]:
        """Return total Wh and positive bucket count for a field."""
        total = 0.0
        count = 0
        if not isinstance(values, list):
            return total, count
        for val in values:
            num = self._coerce_energy_value(val)
            if num is None or num <= 0:
                continue
            total += num
            count += 1
        return total, count

    def _sum_energy_fields(
        self, payload: dict, fields: Iterable[str]
    ) -> tuple[float, int, list[str]]:
        """Aggregate Wh totals across multiple fields."""
        total = 0.0
        max_count = 0
        used: list[str] = []
        for field in fields:
            field_total, field_count = self._sum_energy_buckets(payload.get(field))
            if field_count <= 0 or field_total <= 0:
                continue
            total += field_total
            max_count = max(max_count, field_count)
            used.append(field)
        return total, max_count, used

    def _diff_energy_fields(
        self, payload: dict, minuend: str, subtrahend: str
    ) -> tuple[float, int, list[str]]:
        """Derive a flow by subtracting one field from another."""
        pos_total, pos_count = self._sum_energy_buckets(payload.get(minuend))
        neg_total, neg_count = self._sum_energy_buckets(payload.get(subtrahend))
        if pos_total <= 0 or neg_total <= 0:
            return 0.0, 0, []
        if pos_total <= neg_total:
            return 0.0, 0, []
        bucket_count = max(min(pos_count, neg_count), 1)
        return pos_total - neg_total, bucket_count, [minuend, subtrahend]

    def _apply_site_energy_guard(
        self, flow: str, sample: float | None, prev: float | None
    ) -> tuple[float | None, str | None]:
        """Apply monotonic guard to a site energy flow."""
        state = self._site_energy_guard.setdefault(flow, LifetimeGuardState())
        if state.last is None and prev is not None:
            state.last = prev

        try:
            value = float(sample) if sample is not None else None
        except Exception:  # noqa: BLE001
            value = None

        if value is None or value < 0:
            return (state.last if state.last is not None else prev), None

        value = round(value, 3)
        last = state.last
        if last is None:
            state.last = value
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return value, None

        drop = last - value
        if drop < 0:
            state.last = value
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return value, None

        if drop <= LIFETIME_DROP_JITTER_KWH:
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return last, None

        is_reset_candidate = drop >= LIFETIME_RESET_DROP_THRESHOLD_KWH and (
            value <= LIFETIME_RESET_FLOOR_KWH or value <= (last * LIFETIME_RESET_RATIO)
        )
        if is_reset_candidate:
            now = time.monotonic()
            if (
                state.pending_value is not None
                and abs(value - state.pending_value) <= LIFETIME_CONFIRM_TOLERANCE_KWH
            ):
                state.pending_count += 1
            else:
                state.pending_value = value
                state.pending_ts = now
                state.pending_count = 1
                self._site_energy_force_refresh = True
                _LOGGER.debug(
                    "Ignoring suspected site energy reset for %s: %.3f -> %.3f",
                    flow,
                    last,
                    value,
                )
            if state.pending_count >= LIFETIME_CONFIRM_COUNT or (
                state.pending_ts is not None
                and (now - state.pending_ts) >= LIFETIME_CONFIRM_WINDOW_S
            ):
                confirm_count = state.pending_count
                state.last = value
                state.pending_value = None
                state.pending_ts = None
                state.pending_count = 0
                reset_at = dt_util.utcnow().isoformat()
                _LOGGER.debug(
                    "Accepting site energy reset for %s after %d samples: %.3f -> %.3f",
                    flow,
                    confirm_count,
                    last,
                    value,
                )
                return value, reset_at
            return last, None

        state.pending_value = None
        state.pending_ts = None
        state.pending_count = 0
        return last, None

    def _aggregate_site_energy(
        self, payload: dict | None
    ) -> tuple[dict[str, SiteEnergyFlow], dict[str, object]] | None:
        """Aggregate lifetime energy payload into kWh totals."""
        if not isinstance(payload, dict):
            return None

        start_date_raw = payload.get("start_date")
        start_date = (
            str(start_date_raw) if start_date_raw is not None else None
        )
        last_report_date = self._parse_site_energy_timestamp(
            payload.get("last_report_date")
        )
        update_pending = payload.get("update_pending")
        prev = self.site_energy or {}
        flows: dict[str, SiteEnergyFlow] = {}

        def _store(flow: str, total_wh: float, fields: list[str], bucket_count: int):
            if bucket_count <= 0 or total_wh <= 0:
                return
            try:
                total_kwh = round(total_wh / 1000.0, 3)
            except Exception:  # noqa: BLE001
                return
            prev_entry = prev.get(flow)
            prev_value = None
            prev_reset_at = None
            if isinstance(prev_entry, SiteEnergyFlow):
                prev_value = prev_entry.value_kwh
                prev_reset_at = prev_entry.last_reset_at
            filtered, reset_at = self._apply_site_energy_guard(
                flow, total_kwh, prev_value
            )
            if filtered is None:
                return
            last_reset = reset_at or prev_reset_at or self._site_energy_last_reset.get(
                flow
            )
            flows[flow] = SiteEnergyFlow(
                value_kwh=filtered,
                bucket_count=bucket_count,
                fields_used=fields,
                start_date=start_date,
                last_report_date=last_report_date,
                update_pending=bool(update_pending)
                if update_pending is not None
                else None,
                source_unit="Wh",
                last_reset_at=last_reset,
            )
            if last_reset:
                self._site_energy_last_reset[flow] = last_reset

        # Solar production
        prod_total, prod_count = self._sum_energy_buckets(payload.get("production"))
        _store("solar_production", prod_total, ["production"], prod_count)

        # Grid import (consumption from grid)
        imp_total, imp_count = self._sum_energy_buckets(payload.get("import"))
        if imp_total > 0 and imp_count > 0:
            _store("grid_import", imp_total, ["import"], imp_count)
        else:
            grid_total, grid_count, grid_fields = self._sum_energy_fields(
                payload, ("grid_home", "grid_battery")
            )
            if grid_total > 0 and grid_fields:
                _store("grid_import", grid_total, grid_fields, grid_count)
            else:
                derived_total, derived_count, derived_fields = self._diff_energy_fields(
                    payload, "consumption", "solar_home"
                )
                if derived_total > 0 and derived_fields:
                    _store(
                        "grid_import",
                        derived_total,
                        derived_fields,
                        derived_count,
                    )

        # Grid export
        exp_total, exp_count = self._sum_energy_buckets(payload.get("export"))
        if exp_total > 0 and exp_count > 0:
            _store("grid_export", exp_total, ["export"], exp_count)
        else:
            export_total, export_count, export_fields = self._sum_energy_fields(
                payload, ("solar_grid", "battery_grid")
            )
            if export_total > 0 and export_fields:
                _store("grid_export", export_total, export_fields, export_count)

        # Battery charge (into battery)
        charge_total, charge_count = self._sum_energy_buckets(payload.get("charge"))
        if charge_total > 0 and charge_count > 0:
            _store("battery_charge", charge_total, ["charge"], charge_count)
        else:
            charge_total, charge_count, charge_fields = self._sum_energy_fields(
                payload, ("solar_battery", "grid_battery")
            )
            if charge_total > 0 and charge_fields:
                _store("battery_charge", charge_total, charge_fields, charge_count)

        # Battery discharge (out of battery)
        discharge_total, discharge_count = self._sum_energy_buckets(
            payload.get("discharge")
        )
        if discharge_total > 0 and discharge_count > 0:
            _store(
                "battery_discharge",
                discharge_total,
                ["discharge"],
                discharge_count,
            )
        else:
            discharge_total, discharge_count, discharge_fields = (
                self._sum_energy_fields(payload, ("battery_home", "battery_grid"))
            )
            if discharge_total > 0 and discharge_fields:
                _store(
                    "battery_discharge",
                    discharge_total,
                    discharge_fields,
                    discharge_count,
                )

        meta = {
            "start_date": start_date,
            "last_report_date": last_report_date,
            "update_pending": bool(update_pending)
            if update_pending is not None
            else None,
            "bucket_lengths": {
                key: len(value) for key, value in payload.items() if isinstance(value, list)
            },
        }
        return flows, meta

    async def _async_refresh_site_energy(self, *, force: bool = False) -> None:
        """Refresh lifetime energy cache with TTL enforcement."""
        if not hasattr(self, "_site_energy_cache_ts"):
            self._site_energy_cache_ts = None
        if not hasattr(self, "_site_energy_cache_ttl"):
            self._site_energy_cache_ttl = SITE_ENERGY_CACHE_TTL
        force_refresh = force or self._site_energy_force_refresh
        self._site_energy_force_refresh = False
        now_mono = time.monotonic()
        if (
            not force_refresh
            and self._site_energy_cache_ts is not None
            and (now_mono - self._site_energy_cache_ts) < self._site_energy_cache_ttl
        ):
            return
        try:
            payload = await self.client.lifetime_energy()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to fetch lifetime energy for site %s: %s", self.site_id, err
            )
            return
        parsed = self._aggregate_site_energy(payload)
        if parsed is None:
            return
        flows, meta = parsed
        self.site_energy = flows
        self._site_energy_meta = meta
        self._site_energy_cache_ts = time.monotonic()

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
        metrics: dict[str, object] = {
            "site_id": self.site_id,
            "site_name": self.site_name,
            "last_success": _iso(self.last_success_utc),
            "last_error": getattr(self, "_last_error", None),
            "last_failure": _iso(self.last_failure_utc),
            "last_failure_status": self.last_failure_status,
            "last_failure_description": self.last_failure_description,
            "last_failure_source": self.last_failure_source,
            "last_failure_response": self.last_failure_response,
            "latency_ms": self.latency_ms,
            "backoff_active": backoff_active,
            "backoff_ends_utc": _iso(self.backoff_ends_utc),
            "network_errors": self._network_errors,
            "http_errors": self._http_errors,
            "rate_limit_hits": self._rate_limit_hits,
            "dns_errors": self._dns_failures,
            "phase_timings": self.phase_timings,
            "session_cache_ttl_s": getattr(self, "_session_history_cache_ttl", None),
        }
        site_energy_age = self._site_energy_cache_age()
        site_flows = getattr(self, "site_energy", None) or {}
        site_meta = getattr(self, "_site_energy_meta", None) or {}
        if site_flows or site_energy_age is not None or site_meta:
            metrics["site_energy"] = {
                "flows": sorted(list(site_flows.keys())),
                "cache_age_s": round(site_energy_age, 3)
                if site_energy_age is not None
                else None,
                "start_date": site_meta.get("start_date"),
                "last_report_date": _iso(site_meta.get("last_report_date")),
                "update_pending": site_meta.get("update_pending"),
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
            await self._async_refresh_site_energy()
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
        if charge_mode_candidates:
            unique_candidates = list(dict.fromkeys(charge_mode_candidates))
            charge_start = time.monotonic()
            charge_modes = await self._async_resolve_charge_modes(unique_candidates)
            phase_timings["charge_mode_s"] = round(time.monotonic() - charge_start, 3)

        def _as_bool(v):
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return v != 0
            if isinstance(v, str):
                return v.strip().lower() in ("true", "1", "yes", "y")
            return False

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
            if sn not in self.last_set_amps and charging_level is not None:
                try:
                    self.set_last_set_amps(sn, int(charging_level))
                except Exception:
                    pass
            conn0 = (obj.get("connectors") or [{}])[0]
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

            out[sn] = {
                "sn": sn,
                "name": obj.get("name"),
                "display_name": display_name,
                "connected": _as_bool(obj.get("connected")),
                "plugged": _as_bool(obj.get("pluggedIn")),
                "charging": charging_now_flag,
                "faulted": _as_bool(obj.get("faulted")),
                "connector_status": connector_status,
                "connector_reason": conn0.get("connectorStatusReason"),
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
                "commissioned": _as_bool(commissioned_val),
                "schedule_status": sch.get("status"),
                "schedule_type": sch_info0.get("type") or sch.get("status"),
                "schedule_start": sch_info0.get("startTime"),
                "schedule_end": sch_info0.get("endTime"),
                "charge_mode": charge_mode,
                # Expose scheduler preference explicitly for entities that care
                "charge_mode_pref": charge_mode_pref,
                "charging_level": charging_level,
                "session_charge_level": session_charge_level,
                "session_cost": session_cost,
                "operating_v": self._operating_v.get(sn),
            }

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
                cur["phase_mode"] = item.get("phaseMode")
                cur["status"] = item.get("status")
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
                    filtered = self._apply_lifetime_guard(
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
            day_local = dt_util.as_local(day_ref)
        except Exception:
            if day_ref.tzinfo is None:
                day_ref = day_ref.replace(tzinfo=_tz.utc)
            day_local = dt_util.as_local(day_ref)
        day_key = day_local.strftime("%Y-%m-%d")
        now_mono = time.monotonic()
        immediate_serials: list[str] = []
        background_serials: list[str] = []
        for sn, cur in out.items():
            view = self.session_history.get_cache_view(sn, day_key, now_mono)
            sessions_cached = view.sessions or []
            cur["energy_today_sessions"] = sessions_cached
            cur["energy_today_sessions_kwh"] = self._sum_session_energy(sessions_cached)
            if not view.needs_refresh or view.blocked:
                continue
            if first_refresh:
                background_serials.append(sn)
            else:
                immediate_serials.append(sn)

        if immediate_serials:
            updates = await self._async_enrich_sessions(
                immediate_serials, day_local, in_background=False
            )
            for sn, sessions in updates.items():
                cur = out.get(sn)
                if cur is None:
                    continue
                cur["energy_today_sessions"] = sessions
                cur["energy_today_sessions_kwh"] = self._sum_session_energy(sessions)
        if background_serials:
            self._schedule_session_enrichment(background_serials, day_local)
        phase_timings["sessions_s"] = round(time.monotonic() - sessions_start, 3)

        site_energy_start = time.monotonic()
        await self._async_refresh_site_energy()
        phase_timings["site_energy_s"] = round(
            time.monotonic() - site_energy_start, 3
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

    def _apply_lifetime_guard(
        self,
        sn: str,
        raw_value,
        prev: dict | None,
    ) -> float | None:
        state = self._lifetime_guard.setdefault(sn, LifetimeGuardState())
        prev_val: float | None = None
        if isinstance(prev, dict):
            raw_prev = prev.get("lifetime_kwh")
            if isinstance(raw_prev, (int, float)):
                try:
                    prev_val = round(float(raw_prev), 3)
                except Exception:
                    prev_val = None
        if state.last is None and prev_val is not None:
            state.last = prev_val

        try:
            sample = float(raw_value)
        except (TypeError, ValueError):
            sample = None

        if sample is not None:
            if sample > 200:
                sample = sample / 1000.0
            sample = round(sample, 3)
            if sample < 0:
                sample = 0.0

        if sample is None:
            return state.last if state.last is not None else prev_val

        last = state.last
        if last is None:
            state.last = sample
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return sample

        drop = last - sample
        if drop < 0:
            state.last = sample
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return sample

        if drop <= LIFETIME_DROP_JITTER_KWH:
            state.pending_value = None
            state.pending_ts = None
            state.pending_count = 0
            return last

        is_reset_candidate = drop >= LIFETIME_RESET_DROP_THRESHOLD_KWH and (
            sample <= LIFETIME_RESET_FLOOR_KWH
            or sample <= (last * LIFETIME_RESET_RATIO)
        )

        if is_reset_candidate:
            now = time.monotonic()
            if (
                state.pending_value is not None
                and abs(sample - state.pending_value) <= LIFETIME_CONFIRM_TOLERANCE_KWH
            ):
                state.pending_count += 1
            else:
                state.pending_value = sample
                state.pending_ts = now
                state.pending_count = 1
                # Force next poll to refresh summary to validate reset
                self.summary.invalidate()
                _LOGGER.debug(
                    "Ignoring suspected lifetime reset for %s: %.3f -> %.3f",
                    sn,
                    last,
                    sample,
                )
            if state.pending_count >= LIFETIME_CONFIRM_COUNT or (
                state.pending_ts is not None
                and (now - state.pending_ts) >= LIFETIME_CONFIRM_WINDOW_S
            ):
                confirm_count = state.pending_count
                state.last = sample
                state.pending_value = None
                state.pending_ts = None
                state.pending_count = 0
                _LOGGER.debug(
                    "Accepting lifetime reset for %s after %d samples: %.3f -> %.3f",
                    sn,
                    confirm_count,
                    last,
                    sample,
                )
                return sample
            return last

        # Generic backward jitter – hold previous reading
        state.pending_value = None
        state.pending_ts = None
        state.pending_count = 0
        return last

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
        if self._streaming and fast_stream_enabled:
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
                eauth=tokens.access_token, cookie=tokens.cookie
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
        except ServiceValidationError:
            _LOGGER.debug(
                "Amp restart aborted for charger %s because it is not plugged in",
                sn_str,
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
        except Exception:
            mode = None
        if mode:
            self._charge_mode_cache[sn] = (mode, now)
        return mode

    def set_charge_mode_cache(self, sn: str, mode: str) -> None:
        """Update cache when user changes mode via select."""
        self._charge_mode_cache[str(sn)] = (str(mode), time.monotonic())

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
