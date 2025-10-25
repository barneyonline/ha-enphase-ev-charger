from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as _tz
from typing import Callable, Iterable

import aiohttp
from email.utils import parsedate_to_datetime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
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

_LOGGER = logging.getLogger(__name__)

MIN_SESSION_HISTORY_CACHE_TTL = 60  # seconds
SESSION_HISTORY_FAILURE_BACKOFF_S = 15 * 60
LIFETIME_DROP_JITTER_KWH = 0.02
LIFETIME_RESET_DROP_THRESHOLD_KWH = 0.5
LIFETIME_RESET_FLOOR_KWH = 5.0
LIFETIME_RESET_RATIO = 0.5
LIFETIME_CONFIRM_TOLERANCE_KWH = 0.05
LIFETIME_CONFIRM_COUNT = 2
LIFETIME_CONFIRM_WINDOW_S = 180.0
SUMMARY_IDLE_TTL = 600.0
SUMMARY_ACTIVE_MIN_TTL = 5.0
ACTIVE_CONNECTOR_STATUSES = {"CHARGING", "FINISHING", "SUSPENDED"}
ACTIVE_SUSPENDED_PREFIXES = ("SUSPENDED_EV",)
SUSPENDED_EVSE_STATUS = "SUSPENDED_EVSE"
SESSION_HISTORY_CONCURRENCY = 3


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
        self.last_success_utc = None
        self.latency_ms: int | None = None
        self.last_failure_utc = None
        self.last_failure_status: int | None = None
        self.last_failure_reason: str | None = None
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
        self._summary_cache: tuple[float, list[dict], float] | None = None
        self._summary_ttl: float = SUMMARY_IDLE_TTL
        self._summary_lock = asyncio.Lock()
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
        self._session_history_cache_ttl = max(
            MIN_SESSION_HISTORY_CACHE_TTL, self._session_history_interval_min * 60
        )
        self._session_history_failure_backoff = SESSION_HISTORY_FAILURE_BACKOFF_S
        self._session_history_block_until: dict[str, float] = {}
        self._session_history_cache: dict[tuple[str, str], tuple[float, list[dict]]] = (
            {}
        )
        self._session_history_concurrency = SESSION_HISTORY_CONCURRENCY
        self._session_refresh_in_progress: set[str] = set()
        # Per-serial operating voltage learned from summary v2; used for power estimation
        self._operating_v: dict[str, int] = {}
        # Temporary fast polling window after user actions (start/stop/etc.)
        self._fast_until: float | None = None
        # Cache charge mode results to avoid extra API calls every poll
        self._charge_mode_cache: dict[str, tuple[str, float]] = {}
        # Track charging transitions and a fixed session end timestamp so
        # session duration does not grow after charging stops
        self._last_charging: dict[str, bool] = {}
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

    async def _async_setup(self) -> None:
        """Prepare lightweight state before the first refresh."""
        self._phase_timings = {}

    @property
    def phase_timings(self) -> dict[str, float]:
        """Return the most recent phase timings."""
        return dict(self._phase_timings)

    async def _async_update_data(self) -> dict:
        t0 = time.monotonic()
        phase_timings: dict[str, float] = {}

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

        # Handle backoff window
        if self._backoff_until and time.monotonic() < self._backoff_until:
            raise UpdateFailed("In backoff due to rate limiting or server errors")

        try:
            status_start = time.monotonic()
            data = await self.client.status()
            phase_timings["status_s"] = round(time.monotonic() - status_start, 3)
            self._unauth_errors = 0
            ir.async_delete_issue(self.hass, DOMAIN, "reauth_required")
        except Unauthorized as err:
            self._unauth_errors += 1
            if await self._attempt_auto_refresh():
                self._unauth_errors = 0
                ir.async_delete_issue(self.hass, DOMAIN, "reauth_required")
                try:
                    status_start = time.monotonic()
                    data = await self.client.status()
                    phase_timings["status_s"] = round(
                        time.monotonic() - status_start, 3
                    )
                except Unauthorized as err_refresh:
                    raise ConfigEntryAuthFailed from err_refresh
            else:
                if self._unauth_errors >= 2:
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        "reauth_required",
                        is_fixable=False,
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="reauth_required",
                        translation_placeholders={"site_id": str(self.site_id)},
                    )
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
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        "rate_limited",
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="rate_limited",
                        translation_placeholders={"site_id": str(self.site_id)},
                    )
            else:
                is_server_error = 500 <= err.status < 600
                if is_server_error:
                    if self._http_errors >= 3 and not self._cloud_issue_reported:
                        ir.async_create_issue(
                            self.hass,
                            DOMAIN,
                            ISSUE_CLOUD_ERRORS,
                            is_fixable=False,
                            severity=ir.IssueSeverity.WARNING,
                            translation_key=ISSUE_CLOUD_ERRORS,
                            translation_placeholders={"site_id": str(self.site_id)},
                        )
                        self._cloud_issue_reported = True
                elif self._cloud_issue_reported:
                    ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CLOUD_ERRORS)
                    self._cloud_issue_reported = False
            reason = (err.message or err.__class__.__name__).strip()
            now_utc = dt_util.utcnow()
            self.last_failure_utc = now_utc
            self.last_failure_status = err.status
            self.last_failure_reason = reason or "HTTP error"
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
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    ISSUE_NETWORK_UNREACHABLE,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_NETWORK_UNREACHABLE,
                    translation_placeholders={"site_id": str(self.site_id)},
                )
                self._network_issue_reported = True
            if dns_failure and self._dns_failures >= 2 and not self._dns_issue_reported:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    ISSUE_DNS_RESOLUTION,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_DNS_RESOLUTION,
                    translation_placeholders={"site_id": str(self.site_id)},
                )
                self._dns_issue_reported = True
            now_utc = dt_util.utcnow()
            self.last_failure_utc = now_utc
            self.last_failure_status = None
            self.last_failure_reason = msg
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
        summary_ttl = SUMMARY_IDLE_TTL
        if polling_state["want_fast"]:
            target_interval = float(polling_state["target"])
            summary_ttl = max(
                SUMMARY_ACTIVE_MIN_TTL,
                min(target_interval, SUMMARY_IDLE_TTL),
            )
        cache_info = self._get_summary_cache()
        summary_force = False
        if cache_info is None:
            summary_force = True
        else:
            cache_ts, cache_data, cache_ttl = cache_info
            age = time.monotonic() - cache_ts
            if cache_ttl > summary_ttl or age >= summary_ttl:
                summary_force = True
            elif cache_ttl != summary_ttl:
                self._summary_cache = (cache_ts, cache_data, summary_ttl)
        self._summary_ttl = summary_ttl

        # Enrich with summary v2 data
        summary_start = time.monotonic()
        summary = await self._async_fetch_summary(force=summary_force)
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
            cache_key = (sn, day_key)
            cached = self._session_history_cache.get(cache_key)
            sessions_cached: list[dict] = []
            cache_age: float | None = None
            if cached:
                cached_ts, cached_sessions = cached
                cache_age = now_mono - cached_ts
                sessions_cached = cached_sessions
            if sessions_cached:
                cur["energy_today_sessions"] = sessions_cached
                cur["energy_today_sessions_kwh"] = self._sum_session_energy(
                    sessions_cached
                )
            else:
                cur["energy_today_sessions"] = []
                cur["energy_today_sessions_kwh"] = 0.0
            needs_refresh = False
            if cached is None:
                needs_refresh = True
            elif cache_age is not None and cache_age >= self._session_history_cache_ttl:
                needs_refresh = True
            if not needs_refresh:
                continue
            block_until = self._session_history_block_until.get(sn)
            if block_until and block_until > now_mono:
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
        try:
            result = await self.client.start_charging(sn_str, amps)
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
                self._summary_cache = None
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

    def _get_summary_cache(
        self,
    ) -> tuple[float, list[dict], float] | None:
        cache = self._summary_cache
        if not cache:
            return None
        if isinstance(cache, tuple) and len(cache) == 3:
            ts, data, ttl = cache
            return ts, data, float(ttl)
        if isinstance(cache, tuple) and len(cache) == 2:
            ts, data = cache
            return ts, data, SUMMARY_IDLE_TTL
        return None

    async def _async_fetch_summary(self, *, force: bool = False) -> list[dict]:
        """Return summary data, refreshing at most every 10 minutes."""
        cache_info = self._get_summary_cache()
        if not force and cache_info:
            cache_ts, cache_data, cache_ttl = cache_info
            age = time.monotonic() - cache_ts
            if age < cache_ttl:
                return cache_data

        async with self._summary_lock:
            cached = self._get_summary_cache()
            if not force and cached:
                cache_ts, cache_data, cache_ttl = cached
                age = time.monotonic() - cache_ts
                if age < cache_ttl:
                    return cache_data
            try:
                summary = await self.client.summary_v2()
            except Exception as err:  # noqa: BLE001
                if cached:
                    _LOGGER.debug("Summary v2 fetch failed; reusing cache: %s", err)
                    return cached[1]
                _LOGGER.debug("Summary v2 fetch failed: %s", err)
                return []

            if not summary:
                summary_list: list[dict] = []
            elif isinstance(summary, list):
                summary_list = summary
            elif isinstance(summary, dict):
                interim = summary.get("data")
                summary_list = interim if isinstance(interim, list) else []
            else:
                summary_list = (
                    list(summary) if isinstance(summary, (tuple, set)) else []
                )

            self._summary_cache = (time.monotonic(), summary_list, self._summary_ttl)
            return summary_list

    async def _async_fetch_sessions_today(
        self,
        sn: str,
        *,
        day_local: datetime | None = None,
    ) -> list[dict]:
        """Return session history for the current day, cached for a short window."""
        if not sn:
            return []
        if day_local is None:
            day_local = dt_util.now()
        try:
            local_dt = dt_util.as_local(day_local)
        except Exception:
            if day_local.tzinfo is None:
                day_local = day_local.replace(tzinfo=_tz.utc)
            local_dt = dt_util.as_local(day_local)

        day_key = local_dt.strftime("%Y-%m-%d")
        cache_key = (sn, day_key)
        now_mono = time.monotonic()
        cached = self._session_history_cache.get(cache_key)
        cache_ttl = self._session_history_cache_ttl
        if cached and (now_mono - cached[0] < cache_ttl):
            return cached[1]

        block_until = self._session_history_block_until.get(sn)
        if block_until and now_mono < block_until:
            if cached:
                return cached[1]
            return []

        api_day = local_dt.strftime("%d-%m-%Y")

        async def _fetch_page(offset: int, limit: int) -> tuple[list[dict], bool]:
            payload = await self.client.session_history(
                sn,
                start_date=api_day,
                end_date=api_day,
                offset=offset,
                limit=limit,
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            items = data.get("result") if isinstance(data, dict) else None
            has_more = bool(data.get("hasMore")) if isinstance(data, dict) else False
            if not isinstance(items, list):
                return [], False
            return items, has_more

        results: list[dict] = []
        offset = 0
        limit = 50
        try:
            for _ in range(5):
                page, has_more = await _fetch_page(offset, limit)
                if page:
                    results.extend(page)
                if not has_more or len(page) < limit:
                    break
                offset += limit
        except Unauthorized as err:
            _LOGGER.debug(
                "Session history unauthorized for %s on %s: %s",
                sn,
                api_day,
                err,
            )
            self._session_history_cache[cache_key] = (now_mono, [])
            return []
        except aiohttp.ClientResponseError as err:
            _LOGGER.debug(
                "Session history server error for %s on %s: %s (%s)",
                sn,
                api_day,
                err.status,
                err.message,
            )
            if err.status in (500, 502, 503, 504, 550):
                self._session_history_block_until[sn] = (
                    now_mono + self._session_history_failure_backoff
                )
            self._session_history_cache[cache_key] = (now_mono, [])
            return []
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Session history fetch failed for %s on %s: %s", sn, api_day, err
            )
            self._session_history_cache[cache_key] = (now_mono, [])
            return []

        sessions = self._normalise_sessions_for_day(
            local_dt=local_dt,
            results=results,
        )
        self._session_history_block_until.pop(sn, None)
        self._session_history_cache[cache_key] = (now_mono, sessions)
        return sessions

    def _normalise_sessions_for_day(
        self,
        *,
        local_dt: datetime,
        results: list[dict],
    ) -> list[dict]:
        """Trim and normalise raw session history entries for a given local day."""

        try:
            now_local = dt_util.as_local(local_dt)
        except Exception:  # noqa: BLE001
            now_local = local_dt

        day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        def _parse_ts(value) -> datetime | None:
            if value is None:
                return None
            if isinstance(value, (int, float)):
                try:
                    return dt_util.as_local(
                        datetime.fromtimestamp(float(value), tz=_tz.utc)
                    )
                except Exception:  # noqa: BLE001
                    return None
            if isinstance(value, str):
                cleaned = value.strip().replace("[UTC]", "")
                if cleaned.endswith("Z"):
                    cleaned = cleaned[:-1] + "+00:00"
                try:
                    dt = datetime.fromisoformat(cleaned)
                except ValueError:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                try:
                    return dt_util.as_local(dt)
                except Exception:  # noqa: BLE001
                    return None
            return None

        def _as_float(val, *, precision: int | None = None) -> float | None:
            if val is None:
                return None
            try:
                out = float(val)
                if precision is not None:
                    return round(out, precision)
                return out
            except Exception:  # noqa: BLE001
                return None

        def _as_int(val) -> int | None:
            if val is None:
                return None
            if isinstance(val, bool):
                return int(val)
            try:
                return int(float(val))
            except Exception:  # noqa: BLE001
                return None

        def _as_bool(val) -> bool:
            if isinstance(val, bool):
                return val
            if isinstance(val, (int, float)):
                return val != 0
            if isinstance(val, str):
                return val.strip().lower() in ("true", "1", "yes", "y")
            return False

        sessions: list[dict] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            start_dt = _parse_ts(item.get("startTime"))
            end_dt = _parse_ts(item.get("endTime"))

            if start_dt is None and end_dt is None:
                continue

            if start_dt is None:
                start_dt = end_dt
            if end_dt is None:
                end_dt = now_local

            window_start = start_dt
            window_end = end_dt

            if window_start is None:
                window_start = day_start
            if window_end is None:
                window_end = now_local

            if window_end < window_start:
                window_end = window_start

            if not (window_start < day_end and window_end >= day_start):
                continue

            energy_total_kwh = _as_float(item.get("aggEnergyValue"), precision=3)

            overlap_start = window_start if window_start > day_start else day_start
            overlap_end = window_end if window_end < day_end else day_end
            overlap_seconds = max((overlap_end - overlap_start).total_seconds(), 0.0)

            active_charge_seconds_raw = _as_int(item.get("activeChargeTime"))
            active_charge_seconds = active_charge_seconds_raw
            if (
                (active_charge_seconds is None or active_charge_seconds <= 0)
                and start_dt
                and end_dt
            ):
                active_charge_seconds = max(
                    int((end_dt - start_dt).total_seconds()),
                    0,
                )

            energy_window_kwh = energy_total_kwh
            if (
                energy_total_kwh is not None
                and active_charge_seconds
                and active_charge_seconds > 0
                and overlap_seconds
            ):
                fraction = min(max(overlap_seconds / active_charge_seconds, 0.0), 1.0)
                energy_window_kwh = round(energy_total_kwh * fraction, 3)
            elif energy_total_kwh is not None and overlap_seconds == 0:
                energy_window_kwh = 0.0

            overlap_active_seconds = (
                int(overlap_seconds) if overlap_seconds and overlap_seconds > 0 else 0
            )

            sessions.append(
                {
                    "session_id": str(item.get("sessionId") or item.get("id") or ""),
                    "start": start_dt.isoformat() if start_dt else None,
                    "end": end_dt.isoformat() if end_dt else None,
                    "auth_type": item.get("authType"),
                    "auth_identifier": item.get("authIdentifier"),
                    "auth_token": item.get("authToken"),
                    "active_charge_time_s": active_charge_seconds_raw,
                    "active_charge_time_overlap_s": overlap_active_seconds,
                    "energy_kwh_total": energy_total_kwh,
                    "energy_kwh": energy_window_kwh,
                    "miles_added": _as_float(item.get("milesAdded"), precision=3),
                    "session_cost": _as_float(item.get("sessionCost"), precision=3),
                    "avg_cost_per_kwh": _as_float(
                        item.get("avgCostPerUnitEnergy"), precision=3
                    ),
                    "cost_calculated": _as_bool(item.get("costCalculated")),
                    "manual_override": _as_bool(item.get("manualOverridden")),
                    "session_cost_state": item.get("sessionCostState"),
                    "charge_profile_stack_level": _as_int(
                        item.get("chargeProfileStackLevel")
                    ),
                }
            )

        sessions.sort(
            key=lambda entry: (
                entry.get("start") or "",
                entry.get("session_id") or "",
            )
        )
        return sessions

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

    def _sum_session_energy(self, sessions: list[dict]) -> float:
        """Compute total energy from session entries."""
        total = 0.0
        for entry in sessions or []:
            val = entry.get("energy_kwh")
            if isinstance(val, (int, float)):
                try:
                    total += float(val)
                except Exception:  # noqa: BLE001
                    continue
        return round(total, 3)

    def _schedule_session_enrichment(
        self,
        serials: Iterable[str],
        day_local: datetime,
    ) -> None:
        """Launch background enrichment for the provided serials."""
        candidates = [sn for sn in dict.fromkeys(serials) if sn]
        if not candidates:
            return
        pending = [
            sn for sn in candidates if sn not in self._session_refresh_in_progress
        ]
        if not pending:
            return
        self._session_refresh_in_progress.update(pending)

        async def _run() -> None:
            try:
                await self._async_enrich_sessions(
                    pending, day_local, in_background=True
                )
            finally:
                for sn in pending:
                    self._session_refresh_in_progress.discard(sn)

        self.hass.async_create_task(_run())

    async def _async_enrich_sessions(
        self,
        serials: Iterable[str],
        day_local: datetime,
        *,
        in_background: bool,
    ) -> dict[str, list[dict]]:
        """Fetch session history concurrently for the provided serials."""
        serial_list = [sn for sn in dict.fromkeys(serials) if sn]
        if not serial_list:
            return {}
        semaphore = asyncio.Semaphore(self._session_history_concurrency)

        async def _refresh(sn: str) -> tuple[str, list[dict] | None]:
            async with semaphore:
                try:
                    sessions = await self._async_fetch_sessions_today(
                        sn, day_local=day_local
                    )
                except Unauthorized as err:
                    _LOGGER.debug(
                        "Session history unauthorized for %s during enrichment: %s",
                        sn,
                        err,
                    )
                    return sn, None
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Session history enrichment failed for %s: %s", sn, err
                    )
                    return sn, None
                return sn, sessions

        tasks = [asyncio.create_task(_refresh(sn)) for sn in serial_list]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        updates: dict[str, list[dict]] = {}
        for response in responses:
            if isinstance(response, Exception):
                _LOGGER.debug("Session history enrichment task error: %s", response)
                continue
            sn, sessions = response
            if sessions is None:
                continue
            updates[sn] = sessions

        if in_background and updates:
            self._apply_session_enrichment(updates)
            return updates

        if in_background:
            return {}
        return updates

    def _apply_session_enrichment(self, updates: dict[str, list[dict]]) -> None:
        """Merge session enrichment results into coordinator data."""
        if not updates:
            return
        if not isinstance(self.data, dict):
            return
        # Clone existing dataset to avoid mutating listeners mid-iteration
        merged: dict[str, dict] = {}
        for sn, payload in self.data.items():
            merged[sn] = dict(payload)
        for sn, sessions in updates.items():
            entry = merged.setdefault(sn, {})
            entry["energy_today_sessions"] = sessions
            entry["energy_today_sessions_kwh"] = self._sum_session_energy(sessions)
        self.async_set_updated_data(merged)

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
        candidates = []
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
