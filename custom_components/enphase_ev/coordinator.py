
from __future__ import annotations
from dataclasses import dataclass
from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
import aiohttp
import asyncio
import time
from homeassistant.util import dt as dt_util
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    CONF_SITE_ID,
    CONF_SERIALS,
    CONF_EAUTH,
    CONF_COOKIE,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    OPT_FAST_POLL_INTERVAL,
    OPT_SLOW_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
)
from .api import EnphaseEVClient, Unauthorized

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
        self.client = EnphaseEVClient(async_get_clientsession(hass), self.site_id, config[CONF_EAUTH], config[CONF_COOKIE])
        self.config_entry = config_entry
        # Options: allow dynamic fast/slow polling
        slow = None
        if config_entry is not None:
            slow = int(config_entry.options.get(OPT_SLOW_POLL_INTERVAL, config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)))
        interval = slow or config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        self.last_set_amps: dict[str, int] = {}
        self.last_success_utc = None
        self.latency_ms: int | None = None
        self._unauth_errors = 0
        self._rate_limit_hits = 0
        self._backoff_until: float | None = None
        self._last_error: str | None = None
        self._streaming: bool = False
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
        for obj in arr:
            sn = str(obj.get("sn") or "")
            if sn and (not self.serials or sn in self.serials):
                charging_level = obj.get("chargingLevel") or obj.get("charging_level") or self.last_set_amps.get(sn)
                power_w = obj.get("powerW") or obj.get("power")
                conn0 = (obj.get("connectors") or [{}])[0]
                sch = obj.get("sch_d") or {}
                sch_info0 = (sch.get("info") or [{}])[0]
                sess = obj.get("session_d") or {}
                out[sn] = {
                    "sn": sn,
                    "name": obj.get("name"),
                    "ev_manufacturer": obj.get("evManufacturerName"),
                    "connected": bool(obj.get("connected")),
                    "plugged": bool(obj.get("pluggedIn")),
                    "charging": bool(obj.get("charging")),
                    "faulted": bool(obj.get("faulted")),
                    "connector_status": obj.get("connectorStatusType") or conn0.get("connectorStatusType"),
                    "connector_reason": conn0.get("connectorStatusReason"),
                    "dlb_active": bool(conn0.get("dlbActive")),
                    "session_kwh": sess.get("e_c"),
                    "session_miles": sess.get("miles"),
                    "session_start": sess.get("start_time"),
                    "session_plug_in_at": sess.get("plg_in_at"),
                    "session_plug_out_at": sess.get("plg_out_at"),
                    "last_reported_at": obj.get("lst_rpt_at"),
                    "commissioned": bool(obj.get("commissioned")),
                    "schedule_status": sch.get("status"),
                    "schedule_type": sch_info0.get("type"),
                    "schedule_start": sch_info0.get("startTime"),
                    "schedule_end": sch_info0.get("endTime"),
                    "charging_level": charging_level,
                    "power_w": power_w,
                }
        # Dynamic poll rate: fast while any charging, otherwise slow
        if self.config_entry is not None:
            want_fast = any(v.get("charging") for v in out.values()) if out else False
            # Prefer fast when streaming if enabled in options
            fast_stream = bool(self.config_entry.options.get(OPT_FAST_WHILE_STREAMING, True)) if self.config_entry else True
            if self._streaming and fast_stream:
                want_fast = True
            fast = int(self.config_entry.options.get(OPT_FAST_POLL_INTERVAL, 10))
            slow = int(self.config_entry.options.get(OPT_SLOW_POLL_INTERVAL, self.update_interval.total_seconds() if self.update_interval else 30))
            target = fast if want_fast else slow
            if not self.update_interval or int(self.update_interval.total_seconds()) != target:
                self.update_interval = timedelta(seconds=target)

        return out

    def set_last_set_amps(self, sn: str, amps: int) -> None:
        self.last_set_amps[str(sn)] = int(amps)
