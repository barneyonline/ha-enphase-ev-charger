
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as _tz

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
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
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OPT_API_TIMEOUT,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

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

class EnphaseCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, config, config_entry=None):
        self.hass = hass
        self.config_entry = config_entry
        self.site_id = str(config[CONF_SITE_ID])
        raw_serials = config.get(CONF_SERIALS) or []
        if isinstance(raw_serials, (list, tuple, set)):
            self.serials = {str(sn) for sn in raw_serials}
        else:
            self.serials = {str(raw_serials)}

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
                self._nominal_v = int(config_entry.options.get(OPT_NOMINAL_VOLTAGE, 240))
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
        self._unauth_errors = 0
        self._rate_limit_hits = 0
        self._backoff_until: float | None = None
        self._last_error: str | None = None
        self._streaming: bool = False
        # Per-serial operating voltage learned from summary v2; used for power estimation
        self._operating_v: dict[str, int] = {}
        # Temporary fast polling window after user actions (start/stop/etc.)
        self._fast_until: float | None = None
        # Cache charge mode results to avoid extra API calls every poll
        self._charge_mode_cache: dict[str, tuple[str, float]] = {}
        # Track charging transitions and a fixed session end timestamp so
        # session duration does not grow after charging stops
        self._last_charging: dict[str, bool] = {}
        self._session_end_fix: dict[str, int] = {}
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

    async def _async_update_data(self) -> dict:
        t0 = time.monotonic()
        # Preload operating voltage and metadata from summary v2.
        # This is relatively heavy; refresh at startup and then at most every 10 minutes.
        pre_summary = None
        now_mono = time.monotonic()
        if not hasattr(self, "_last_summary_at") or not getattr(self, "_last_summary_at"):
            do_summary = True
        else:
            do_summary = (now_mono - getattr(self, "_last_summary_at")) > 600
        if do_summary:
            try:
                pre_summary = await self.client.summary_v2()
            except Exception:
                pre_summary = None
            else:
                self._last_summary_at = now_mono
        if pre_summary:
            for item in pre_summary:
                try:
                    sn_pre = str(item.get("serialNumber") or "")
                    if not sn_pre:
                        continue
                    ov = item.get("operatingVoltage")
                    if ov is not None:
                        self._operating_v[sn_pre] = int(str(ov))
                except Exception:
                    continue
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
            data = await self.client.status()
            self._unauth_errors = 0
            ir.async_delete_issue(self.hass, DOMAIN, "reauth_required")
        except Unauthorized as err:
            self._unauth_errors += 1
            if await self._attempt_auto_refresh():
                self._unauth_errors = 0
                ir.async_delete_issue(self.hass, DOMAIN, "reauth_required")
                try:
                    data = await self.client.status()
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
            retry_after = err.headers.get("Retry-After") if err.headers else None
            delay = 0
            if retry_after:
                try:
                    delay = int(retry_after)
                except Exception:
                    delay = 0
            # Exponential backoff base
            base = 5 if err.status == 429 else 10
            jitter = 1 + (time.monotonic() % 3)
            backoff = max(delay, base * jitter)
            self._backoff_until = time.monotonic() + backoff
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
            raise UpdateFailed(f"Cloud error: {err.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            self._last_error = str(err)
            raise UpdateFailed(f"Error communicating with API: {err}")
        finally:
            self.latency_ms = int((time.monotonic() - t0) * 1000)

        # Success path: reset counters, record last success
        if self._unauth_errors:
            # Clear any outstanding reauth issues on success
            ir.async_delete_issue(self.hass, DOMAIN, "reauth_required")
        self._unauth_errors = 0
        self._rate_limit_hits = 0
        self._backoff_until = None
        self._last_error = None
        self.last_success_utc = dt_util.utcnow()

        out = {}
        arr = data.get("evChargerData") or []
        data_ts = data.get("ts")
        def _as_bool(v):
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return v != 0
            if isinstance(v, str):
                return v.strip().lower() in ("true", "1", "yes", "y")
            return False

        for obj in arr:
            sn = str(obj.get("sn") or "")
            if sn and (not self.serials or sn in self.serials):
                charging_level = obj.get("chargingLevel") or obj.get("charging_level") or self.last_set_amps.get(sn)
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
                last_rpt = obj.get("lst_rpt_at") or obj.get("lastReportedAt") or obj.get("last_reported_at")
                if not last_rpt and data_ts is not None:
                    try:
                        # Handle ISO string, seconds, or milliseconds epoch
                        if isinstance(data_ts, str):
                            if data_ts.endswith("Z[UTC]") or data_ts.endswith("Z"):
                                # Strip [UTC] if present; HA will display local time
                                s = data_ts.replace("[UTC]", "").replace("Z", "")
                                last_rpt = datetime.fromisoformat(s).replace(tzinfo=_tz.utc).isoformat()
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
                    commissioned_val = obj.get("isCommissioned") or conn0.get("commissioned")

                # Charge mode: fetch from scheduler API (cached); fall back to derived
                charge_mode_pref = await self._get_charge_mode(sn)
                charge_mode = charge_mode_pref
                if not charge_mode:
                    charge_mode = (
                        obj.get("chargeMode")
                        or obj.get("chargingMode")
                        or (obj.get("sch_d") or {}).get("mode")
                    )
                    if not charge_mode:
                        if _as_bool(obj.get("charging")):
                            charge_mode = "IMMEDIATE"
                        elif sch_info0.get("type") or sch.get("status"):
                            charge_mode = str(sch_info0.get("type") or sch.get("status")).upper()
                        else:
                            charge_mode = "IDLE"

                # Determine a stable session end when not charging
                charging_now = _as_bool(obj.get("charging"))
                if sn in self._last_charging and self._last_charging.get(sn) and not charging_now:
                    # Transition charging -> not charging: capture a fixed end time
                    try:
                        if isinstance(data_ts, (int, float)) or (isinstance(data_ts, str) and data_ts.isdigit()):
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
                ses_kwh = sess.get("e_c")
                try:
                    if isinstance(ses_kwh, (int, float)) and ses_kwh > 200:
                        ses_kwh = round(float(ses_kwh) / 1000.0, 2)
                except Exception:
                    pass

                display_name = obj.get("displayName") or obj.get("name")
                if display_name is not None:
                    try:
                        display_name = str(display_name)
                    except Exception:
                        display_name = None
                out[sn] = {
                    "sn": sn,
                    "name": obj.get("name"),
                    "display_name": display_name,
                    "connected": _as_bool(obj.get("connected")),
                    "plugged": _as_bool(obj.get("pluggedIn")),
                    "charging": _as_bool(obj.get("charging")),
                    "faulted": _as_bool(obj.get("faulted")),
                    "connector_status": obj.get("connectorStatusType") or conn0.get("connectorStatusType"),
                    "connector_reason": conn0.get("connectorStatusReason"),
                    "session_kwh": ses_kwh,
                    "session_miles": sess.get("miles"),
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
                    "operating_v": self._operating_v.get(sn),
                }

        # Enrich with summary v2 data
        try:
            summary = await self.client.summary_v2()
        except Exception:
            summary = None
        if summary:
            for item in summary:
                sn = str(item.get("serialNumber") or "")
                if not sn or (self.serials and sn not in self.serials):
                    continue
                cur = out.setdefault(sn, {})
                # Max current capability and phase/status
                cur["max_current"] = item.get("maxCurrent")
                cld = item.get("chargeLevelDetails") or {}
                try:
                    cur["min_amp"] = int(str(cld.get("min"))) if cld.get("min") is not None else None
                except Exception:
                    cur["min_amp"] = None
                try:
                    cur["max_amp"] = int(str(cld.get("max"))) if cld.get("max") is not None else None
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
                                if line.startswith("\"") and line.endswith("\""):
                                    line = line[1:-1]
                                if line:
                                    parsed.append(line)
                        entries = parsed if isinstance(parsed, list) else []
                    for entry in entries:
                        if isinstance(entry, dict):
                            candidate = entry.get("ipaddr") or entry.get("ip")
                            if candidate:
                                ip_addr = candidate
                                if str(entry.get("connectionStatus")) in ("1", "true", "True"):
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
                                if parts.get("connectionStatus") in ("1", "true", "True"):
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
                try:
                    if ov is not None:
                        self._operating_v[sn] = int(str(ov))
                except Exception:
                    pass
                # Expose operating voltage in the mapped data when known
                if self._operating_v.get(sn) is not None:
                    cur["operating_v"] = self._operating_v.get(sn)
                # Lifetime energy for Energy Dashboard (kWh) – normalize Wh→kWh when needed
                if item.get("lifeTimeConsumption") is not None:
                    try:
                        lt = float(item.get("lifeTimeConsumption"))
                        # Heuristic: values > 200 are likely Wh; divide by 1000
                        if lt > 200:
                            lt = round(lt / 1000.0, 3)
                        cur["lifetime_kwh"] = lt
                    except Exception:
                        pass
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
        # Dynamic poll rate: fast while any charging, within a fast window, or streaming
        if self.config_entry is not None:
            want_fast = any(v.get("charging") for v in out.values()) if out else False
            now_mono = time.monotonic()
            if self._fast_until and now_mono < self._fast_until:
                want_fast = True
            # Prefer fast when streaming if enabled in options
            fast_stream = (
                bool(self.config_entry.options.get(OPT_FAST_WHILE_STREAMING, True)) if self.config_entry else True
            )
            if self._streaming and fast_stream:
                want_fast = True
            fast = int(self.config_entry.options.get(OPT_FAST_POLL_INTERVAL, 10))
            slow = int(
                self.config_entry.options.get(
                    OPT_SLOW_POLL_INTERVAL,
                    self.update_interval.total_seconds() if self.update_interval else 30,
                )
            )
            target = fast if want_fast else slow
            if not self.update_interval or int(self.update_interval.total_seconds()) != target:
                new_interval = timedelta(seconds=target)
                self.update_interval = new_interval
                # Older cores require async_set_update_interval for dynamic changes
                if hasattr(self, "async_set_update_interval"):
                    try:
                        self.async_set_update_interval(new_interval)
                    except Exception:
                        pass

        return out

    async def _attempt_auto_refresh(self) -> bool:
        """Attempt to refresh authentication using stored credentials."""
        if not self._email or not self._remember_password or not self._stored_password:
            return False

        async with self._refresh_lock:
            session = async_get_clientsession(self.hass)
            try:
                tokens, _ = await async_authenticate(session, self._email, self._stored_password)
            except EnlightenAuthInvalidCredentials:
                _LOGGER.warning("Stored Enlighten credentials were rejected; reauthenticate via the integration options")
                return False
            except EnlightenAuthMFARequired:
                _LOGGER.warning("Enphase account requires multi-factor authentication; complete MFA in the browser and reauthenticate")
                return False
            except EnlightenAuthUnavailable:
                _LOGGER.debug("Auth service unavailable while refreshing tokens; will retry later")
                return False
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Unexpected error refreshing Enlighten auth: %s", err)
                return False

            self._tokens = tokens
            self.client.update_credentials(eauth=tokens.access_token, cookie=tokens.cookie)
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

    def set_last_set_amps(self, sn: str, amps: int) -> None:
        self.last_set_amps[str(sn)] = int(amps)

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
