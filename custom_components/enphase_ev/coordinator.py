
from __future__ import annotations

import asyncio
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

from .api import EnphaseEVClient, Unauthorized
from .const import (
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_SCAN_INTERVAL,
    CONF_SERIALS,
    CONF_SITE_ID,
    DEFAULT_API_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OPT_API_TIMEOUT,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
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
        self.site_id = config[CONF_SITE_ID]
        self.serials = set(config[CONF_SERIALS])
        timeout = (
            int(config_entry.options.get(OPT_API_TIMEOUT, DEFAULT_API_TIMEOUT))
            if config_entry
            else DEFAULT_API_TIMEOUT
        )
        self.client = EnphaseEVClient(
            async_get_clientsession(hass),
            self.site_id,
            config[CONF_EAUTH],
            config[CONF_COOKIE],
            timeout=timeout,
        )
        self.config_entry = config_entry
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
        # Cache charge mode results to avoid extra API calls every poll
        self._charge_mode_cache: dict[str, tuple[str, float]] = {}
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
            config_entry=config_entry,
        )

    async def _async_update_data(self) -> dict:
        t0 = time.monotonic()
        # Handle backoff window
        if self._backoff_until and time.monotonic() < self._backoff_until:
            raise UpdateFailed("In backoff due to rate limiting or server errors")

        try:
            data = await self.client.status()
        except Unauthorized as err:
            self._unauth_errors += 1
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
        for obj in arr:
            sn = str(obj.get("sn") or "")
            if sn and (not self.serials or sn in self.serials):
                charging_level = obj.get("chargingLevel") or obj.get("charging_level") or self.last_set_amps.get(sn)
                power_w = obj.get("powerW") or obj.get("power")
                conn0 = (obj.get("connectors") or [{}])[0]
                sch = obj.get("sch_d") or {}
                sch_info0 = (sch.get("info") or [{}])[0]
                sess = obj.get("session_d") or {}
                # Robust bool parsing for commissioned
                def _as_bool(v):
                    if isinstance(v, bool):
                        return v
                    if isinstance(v, (int, float)):
                        return v != 0
                    if isinstance(v, str):
                        return v.strip().lower() in ("true", "1", "yes", "y")
                    return False
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

                out[sn] = {
                    "sn": sn,
                    "name": obj.get("name"),
                    "connected": _as_bool(obj.get("connected")),
                    "plugged": _as_bool(obj.get("pluggedIn")),
                    "charging": _as_bool(obj.get("charging")),
                    "faulted": _as_bool(obj.get("faulted")),
                    "connector_status": obj.get("connectorStatusType") or conn0.get("connectorStatusType"),
                    "connector_reason": conn0.get("connectorStatusReason"),
                    "dlb_active": _as_bool(conn0.get("dlbActive")),
                    "session_kwh": sess.get("e_c"),
                    "session_miles": sess.get("miles"),
                    "session_start": sess.get("start_time"),
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
                    "power_w": power_w,
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
                # Commissioning: prefer explicit commissioningStatus from summary
                if item.get("commissioningStatus") is not None:
                    cur["commissioned"] = bool(item.get("commissioningStatus"))
                # Last reported: prefer summary if present
                if item.get("lastReportedAt"):
                    cur["last_reported_at"] = item.get("lastReportedAt")
                # Lifetime energy for Energy Dashboard (kWh)
                if item.get("lifeTimeConsumption") is not None:
                    try:
                        cur["lifetime_kwh"] = float(item.get("lifeTimeConsumption"))
                    except Exception:
                        pass
        # Dynamic poll rate: fast while any charging, otherwise slow
        if self.config_entry is not None:
            want_fast = any(v.get("charging") for v in out.values()) if out else False
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
                self.update_interval = timedelta(seconds=target)

        return out

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
