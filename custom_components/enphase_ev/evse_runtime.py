from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone as _tz
from typing import TYPE_CHECKING, Iterable

from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from .api import AuthSettingsUnavailable, SchedulerUnavailable
from .const import (
    AUTH_APP_SETTING,
    AUTH_RFID_SETTING,
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_SLOW_POLL_INTERVAL,
    DOMAIN,
    FAST_TOGGLE_POLL_HOLD_S,
    GREEN_BATTERY_SETTING,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_SLOW_POLL_INTERVAL,
)
from .log_redaction import redact_identifier, redact_text
from .session_history import MIN_SESSION_HISTORY_CACHE_TTL

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator


_LOGGER = logging.getLogger(__name__)

GREEN_BATTERY_CACHE_TTL = 300.0
AUTH_SETTINGS_CACHE_TTL = 300.0
CHARGE_MODE_CACHE_TTL = 300.0
CHARGER_CONFIG_CACHE_TTL = 3600.0
CHARGER_CONFIG_FAILURE_BACKOFF_S = 900.0
CHARGE_MODE_PREFERENCE_MAP: dict[str, str] = {
    "MANUAL": "MANUAL_CHARGING",
    "MANUAL_CHARGING": "MANUAL_CHARGING",
    "SCHEDULED": "SCHEDULED_CHARGING",
    "SCHEDULED_CHARGING": "SCHEDULED_CHARGING",
    "GREEN": "GREEN_CHARGING",
    "GREEN_CHARGING": "GREEN_CHARGING",
    "SMART": "SMART_CHARGING",
    "SMART_CHARGING": "SMART_CHARGING",
}
EFFECTIVE_CHARGE_MODE_VALUES: frozenset[str] = frozenset(
    {
        *CHARGE_MODE_PREFERENCE_MAP.values(),
        "IDLE",
        "IMMEDIATE",
    }
)
SUSPENDED_EVSE_STATUS = "SUSPENDED_EVSE"
AMP_RESTART_DELAY_S = 30.0
STREAMING_DEFAULT_DURATION_S = 900.0


@dataclass(slots=True)
class ChargeModeStartPreferences:
    mode: str | None = None
    include_level: bool | None = None
    strict: bool = False
    enforce_mode: str | None = None


class EvseRuntime:
    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator

    def _instance_override(self, name: str) -> object | None:
        return self.coordinator.__dict__.get(name)

    def schedule_session_enrichment(
        self,
        serials: Iterable[str],
        day_local: datetime,
    ) -> None:
        manager = getattr(self.coordinator, "session_history", None)
        if manager is not None:
            manager.schedule_enrichment(serials, day_local)

    async def async_enrich_sessions(
        self,
        serials: Iterable[str],
        day_local: datetime,
        *,
        in_background: bool,
    ) -> dict[str, list[dict]]:
        manager = getattr(self.coordinator, "session_history", None)
        if manager is not None:
            return await manager.async_enrich(
                serials, day_local, in_background=in_background
            )
        return {}

    def sum_session_energy(self, sessions: list[dict]) -> float:
        manager = getattr(self.coordinator, "session_history", None)
        if manager is not None:
            return manager.sum_energy(sessions)
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
    def session_history_day(
        payload: dict,
        day_local_default: datetime,
    ) -> datetime:
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

    async def async_fetch_sessions_today(
        self,
        sn: str,
        *,
        day_local: datetime | None = None,
    ) -> list[dict]:
        if not sn:
            return []
        day_ref = day_local or dt_util.now()
        try:
            local_dt = dt_util.as_local(day_ref)
        except Exception:
            if day_ref.tzinfo is None:
                day_ref = day_ref.replace(tzinfo=_tz.utc)
            local_dt = dt_util.as_local(day_ref)
        day_key = local_dt.strftime("%Y-%m-%d")
        cache_key = (str(sn), day_key)
        tracked_serials = set(self.coordinator.iter_serials())
        tracked_serials.add(str(sn))
        self.prune_session_history_cache_shim(
            active_serials=tracked_serials,
            keep_day_keys={day_key},
        )
        cached = self.coordinator._session_history_cache_shim.get(cache_key)
        ttl = (
            self.coordinator._session_history_cache_ttl or MIN_SESSION_HISTORY_CACHE_TTL
        )
        if cached:
            cached_ts, cached_sessions = cached
            if time.monotonic() - cached_ts < ttl:
                return cached_sessions
        manager = getattr(self.coordinator, "session_history", None)
        if manager is not None:
            sessions = await manager._async_fetch_sessions_today(sn, day_local=local_dt)
        else:
            sessions = []
        self.set_session_history_cache_shim_entry(str(sn), day_key, sessions)
        return sessions

    @staticmethod
    def normalize_serials(serials: Iterable[str] | None) -> set[str]:
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

    def retained_session_history_days(
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
        day_retention = max(
            1, int(getattr(self.coordinator, "_session_history_day_retention", 1))
        )
        for day_offset in range(day_retention):
            retained.add((now_local - timedelta(days=day_offset)).strftime("%Y-%m-%d"))
        return retained

    def prune_session_history_cache_shim(
        self,
        *,
        active_serials: Iterable[str] | None,
        keep_day_keys: Iterable[str] | None = None,
    ) -> None:
        coord = self.coordinator
        if not isinstance(getattr(coord, "_session_history_cache_shim", None), dict):
            coord._session_history_cache_shim = {}
            return
        active_set = (
            None if active_serials is None else self.normalize_serials(active_serials)
        )
        retained_days = self.retained_session_history_days(keep_day_keys)
        coord._session_history_cache_shim = {
            (sn, day_key): entry
            for (sn, day_key), entry in coord._session_history_cache_shim.items()
            if day_key in retained_days and (active_set is None or sn in active_set)
        }

    def set_session_history_cache_shim_entry(
        self,
        serial: str,
        day_key: str,
        sessions: list[dict],
    ) -> None:
        self.coordinator._session_history_cache_shim[(serial, day_key)] = (
            time.monotonic(),
            sessions,
        )
        keep_serials = self.normalize_serials(self.coordinator.iter_serials())
        keep_serials.add(serial)
        self.prune_session_history_cache_shim(
            active_serials=keep_serials,
            keep_day_keys={day_key},
        )

    def prune_serial_runtime_state(self, active_serials: Iterable[str]) -> set[str]:
        coord = self.coordinator
        keep_serials = self.normalize_serials(active_serials)
        keep_serials.update(
            self.normalize_serials(getattr(coord, "_configured_serials", ()))
        )
        if isinstance(getattr(coord, "serials", None), set):
            coord.serials.intersection_update(keep_serials)
        else:
            coord.serials = set(keep_serials)
        serial_order = getattr(coord, "_serial_order", None)
        if isinstance(serial_order, list):
            coord._serial_order = [sn for sn in serial_order if sn in keep_serials]
        else:
            coord._serial_order = [sn for sn in keep_serials]
        for attr_name in (
            "last_set_amps",
            "_operating_v",
            "_charge_mode_cache",
            "_green_battery_cache",
            "_charger_config_cache",
            "_charger_config_backoff_until",
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
            cache = getattr(coord, attr_name, None)
            if not isinstance(cache, dict):
                continue
            for key in list(cache):
                key_sn = str(key).strip()
                if key_sn not in keep_serials:
                    cache.pop(key, None)
        return keep_serials

    def prune_runtime_caches(
        self,
        *,
        active_serials: Iterable[str],
        keep_day_keys: Iterable[str] | None = None,
    ) -> None:
        keep_serials = self.prune_serial_runtime_state(active_serials)
        self.prune_session_history_cache_shim(
            active_serials=keep_serials,
            keep_day_keys=keep_day_keys,
        )
        manager = getattr(self.coordinator, "session_history", None)
        if manager is not None and hasattr(manager, "prune"):
            manager.prune(active_serials=keep_serials, keep_day_keys=keep_day_keys)

    def sync_desired_charging(self, data: dict[str, dict]) -> None:
        if not data:
            return
        coord = self.coordinator
        now = time.monotonic()
        for sn, info in data.items():
            sn_str = str(sn)
            charging = bool(info.get("charging"))
            desired = coord._desired_charging.get(sn_str)
            if desired is None:
                coord._desired_charging[sn_str] = charging
                desired = charging
            if charging:
                coord._auto_resume_attempts.pop(sn_str, None)
                continue
            if not desired or not info.get("plugged"):
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
            if mode in {"GREEN_CHARGING", "SMART_CHARGING"}:
                _LOGGER.debug(
                    "Skipping auto-resume for charger %s because mode is %s",
                    redact_identifier(sn_str),
                    mode,
                )
                continue
            last_attempt = coord._auto_resume_attempts.get(sn_str)
            if last_attempt is not None and (now - last_attempt) < 120:
                continue
            coord._auto_resume_attempts[sn_str] = now
            _LOGGER.debug(
                "Scheduling auto-resume for charger %s after connector reported %s",
                redact_identifier(sn_str),
                status_norm or "unknown",
            )
            snapshot = dict(info)
            task_name = f"enphase_ev_auto_resume_{sn_str}"
            try:
                coord.hass.async_create_task(
                    self.async_auto_resume(sn_str, snapshot), name=task_name
                )
            except TypeError:
                coord.hass.async_create_task(self.async_auto_resume(sn_str, snapshot))

    async def async_auto_resume(self, sn: str, snapshot: dict | None = None) -> None:
        coord = self.coordinator
        sn_str = str(sn)
        try:
            current = (coord.data or {}).get(sn_str, {})
        except Exception:  # noqa: BLE001
            current = {}
        plugged_snapshot = (
            snapshot.get("plugged") if isinstance(snapshot, dict) else None
        )
        plugged = (
            plugged_snapshot if plugged_snapshot is not None else current.get("plugged")
        )
        if not plugged:
            _LOGGER.debug(
                "Auto-resume aborted for charger %s because it is not plugged in",
                redact_identifier(sn_str),
            )
            return
        amps = coord.pick_start_amps(sn_str)
        prefs = coord._charge_mode_start_preferences(sn_str)
        try:
            result = await coord.client.start_charging(
                sn_str,
                amps,
                include_level=prefs.include_level,
                strict_preference=prefs.strict,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Auto-resume start_charging failed for charger %s: %s",
                redact_identifier(sn_str),
                redact_text(err, site_ids=(coord.site_id,), identifiers=(sn_str,)),
            )
            return
        coord.set_last_set_amps(sn_str, amps)
        if isinstance(result, dict) and result.get("status") == "not_ready":
            _LOGGER.debug(
                "Auto-resume start_charging for charger %s returned not_ready; will retry later",
                redact_identifier(sn_str),
            )
            return
        if prefs.enforce_mode:
            await coord._ensure_charge_mode(sn_str, prefs.enforce_mode)
        _LOGGER.info(
            "Auto-resume start_charging issued for charger %s after suspension",
            redact_identifier(sn_str),
        )
        coord.set_charging_expectation(sn_str, True, hold_for=120)
        coord.kick_fast(120)
        await coord.async_request_refresh()

    def determine_polling_state(self, data: dict[str, dict]) -> dict[str, object]:
        coord = self.coordinator
        charging_now = any(v.get("charging") for v in data.values()) if data else False
        want_fast = charging_now
        now_mono = time.monotonic()
        if coord._fast_until and now_mono < coord._fast_until:
            want_fast = True
        fast_stream_enabled = True
        if coord.config_entry is not None:
            try:
                fast_stream_enabled = bool(
                    coord.config_entry.options.get(OPT_FAST_WHILE_STREAMING, True)
                )
            except Exception:
                fast_stream_enabled = True
        if self.streaming_active() and fast_stream_enabled:
            want_fast = True
        fast_opt = None
        if coord.config_entry is not None:
            fast_opt = coord.config_entry.options.get(OPT_FAST_POLL_INTERVAL)
        fast_configured = fast_opt is not None
        try:
            fast = int(fast_opt) if fast_opt is not None else DEFAULT_FAST_POLL_INTERVAL
        except Exception:
            fast = DEFAULT_FAST_POLL_INTERVAL
            fast_configured = False
        fast = max(1, fast)
        slow_default = getattr(
            coord,
            "_configured_slow_poll_interval",
            DEFAULT_SLOW_POLL_INTERVAL,
        )
        slow_opt = None
        if coord.config_entry is not None:
            slow_opt = coord.config_entry.options.get(OPT_SLOW_POLL_INTERVAL)
        try:
            slow = int(slow_opt) if slow_opt is not None else int(slow_default)
        except Exception:
            slow = int(slow_default)
        slow = max(1, slow)
        target = fast if want_fast else slow
        return {
            "charging_now": charging_now,
            "want_fast": want_fast,
            "fast": fast,
            "slow": slow,
            "target": target,
            "fast_configured": fast_configured,
        }

    async def async_resolve_charge_modes(
        self, serials: Iterable[str]
    ) -> dict[str, str | None]:
        coord = self.coordinator
        results: dict[str, str | None] = {}
        pending: dict[str, asyncio.Task[str | None]] = {}
        if coord._scheduler_backoff_active():
            for sn in dict.fromkeys(serials):
                if not sn:
                    continue
                cached = self.cached_charge_mode_preference(sn)
                if cached is not None:
                    results[sn] = cached
            return results
        for sn in dict.fromkeys(serials):
            if not sn:
                continue
            cached = self.cached_charge_mode_preference(sn)
            if cached is not None:
                results[sn] = cached
                continue
            pending[sn] = asyncio.create_task(self.async_get_charge_mode(sn))
        if pending:
            responses = await asyncio.gather(*pending.values(), return_exceptions=True)
            for sn, response in zip(pending.keys(), responses, strict=False):
                if isinstance(response, Exception):
                    _LOGGER.debug(
                        "Charge mode lookup failed for %s: %s",
                        redact_identifier(sn),
                        redact_text(
                            response,
                            site_ids=(coord.site_id,),
                            identifiers=(sn,),
                        ),
                    )
                    continue
                if response:
                    results[sn] = response
        return results

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
        coord = self.coordinator
        sn_str = str(sn)
        if not allow_unplugged:
            coord.require_plugged(sn_str)
        try:
            data = (coord.data or {}).get(sn_str, {})
        except Exception:
            data = {}
        if data.get("auth_required") is True:
            display = data.get("display_name") or data.get("name") or sn_str
            _LOGGER.warning(
                "Start charging requested for %s but session authentication is required; charging will begin after app/RFID auth completes.",
                redact_identifier(display),
            )
        fallback = fallback_amps if fallback_amps is not None else 32
        amps = coord.pick_start_amps(sn_str, requested_amps, fallback=fallback)
        connector = connector_id if connector_id is not None else 1
        prefs = coord._charge_mode_start_preferences(sn_str)
        result = await coord.client.start_charging(
            sn_str,
            amps,
            connector,
            include_level=prefs.include_level,
            strict_preference=prefs.strict,
        )
        coord.set_last_set_amps(sn_str, amps)
        if isinstance(result, dict) and result.get("status") == "not_ready":
            coord.set_desired_charging(sn_str, False)
            return result
        await coord.async_start_streaming(
            manual=False, serial=sn_str, expected_state=True
        )
        coord.set_desired_charging(sn_str, True)
        coord.set_charging_expectation(sn_str, True, hold_for=hold_seconds)
        coord.kick_fast(int(hold_seconds))
        if prefs.enforce_mode:
            await coord._ensure_charge_mode(sn_str, prefs.enforce_mode)
        await coord.async_request_refresh()
        return result

    async def async_stop_charging(
        self,
        sn: str,
        *,
        hold_seconds: float = 90.0,
        fast_seconds: int = 60,
        allow_unplugged: bool = True,
    ) -> object:
        coord = self.coordinator
        sn_str = str(sn)
        prefs = coord._charge_mode_start_preferences(sn_str)
        if not allow_unplugged:
            coord.require_plugged(sn_str)
        result = await coord.client.stop_charging(sn_str)
        await coord.async_start_streaming(
            manual=False, serial=sn_str, expected_state=False
        )
        coord.set_desired_charging(sn_str, False)
        coord.set_charging_expectation(sn_str, False, hold_for=hold_seconds)
        coord.kick_fast(fast_seconds)
        if prefs.enforce_mode == "SCHEDULED_CHARGING":
            await coord._ensure_charge_mode(sn_str, prefs.enforce_mode)
        await coord.async_request_refresh()
        return result

    def schedule_amp_restart(self, sn: str, delay: float = AMP_RESTART_DELAY_S) -> None:
        coord = self.coordinator
        sn_str = str(sn)
        existing = coord._amp_restart_tasks.pop(sn_str, None)
        if existing and not existing.done():
            existing.cancel()
        restart = self._instance_override("_async_restart_after_amp_change")
        if not callable(restart):
            restart = self.async_restart_after_amp_change
        try:
            task = coord.hass.async_create_task(
                restart(sn_str, delay),
                name=f"enphase_ev_amp_restart_{sn_str}",
            )
        except TypeError:
            task = coord.hass.async_create_task(restart(sn_str, delay))
        coord._amp_restart_tasks[sn_str] = task

        def _cleanup(_: object) -> None:
            stored = coord._amp_restart_tasks.get(sn_str)
            if stored is task:
                coord._amp_restart_tasks.pop(sn_str, None)

        task.add_done_callback(_cleanup)

    async def async_restart_after_amp_change(self, sn: str, delay: float) -> None:
        coord = self.coordinator
        sn_str = str(sn)
        try:
            delay_s = max(0.0, float(delay))
        except Exception:  # noqa: BLE001
            delay_s = AMP_RESTART_DELAY_S
        fast_seconds = max(60, int(delay_s) if delay_s else 60)
        stop_hold = max(90.0, delay_s)
        try:
            await coord.async_stop_charging(
                sn_str,
                hold_seconds=stop_hold,
                fast_seconds=fast_seconds,
                allow_unplugged=True,
            )
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Amp restart stop failed for charger %s: %s",
                redact_identifier(sn_str),
                redact_text(err, site_ids=(coord.site_id,), identifiers=(sn_str,)),
            )
            return
        if delay_s:
            try:
                await asyncio.sleep(delay_s)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:  # noqa: BLE001
                return
        try:
            await coord.async_start_charging(sn_str)
        except asyncio.CancelledError:  # pragma: no cover
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
                redact_text(err, site_ids=(coord.site_id,), identifiers=(sn_str,)),
            )

    def kick_fast(self, seconds: int = 60) -> None:
        try:
            sec = int(seconds)
        except Exception:
            sec = 60
        self.coordinator._fast_until = time.monotonic() + max(1, sec)

    def streaming_active(self) -> bool:
        coord = self.coordinator
        if not coord._streaming:
            return False
        if coord._streaming_until is None:
            return True
        now = time.monotonic()
        if now >= coord._streaming_until:
            self.clear_streaming_state()
            return False
        return True

    def clear_streaming_state(self) -> None:
        coord = self.coordinator
        coord._streaming = False
        coord._streaming_until = None
        coord._streaming_manual = False
        coord._streaming_targets.clear()

    @staticmethod
    def streaming_response_ok(response: object) -> bool:
        if not isinstance(response, dict):
            return True
        status = response.get("status")
        if status is None:
            return True
        status_norm = str(status).strip().lower()
        return status_norm in ("accepted", "ok", "success")

    @staticmethod
    def streaming_duration_s(response: object) -> float:
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
        coord = self.coordinator
        was_active = self.streaming_active()
        if not manual and coord._streaming_manual:
            return
        response = None
        start_ok = False
        try:
            response = await coord.client.start_live_stream()
        except Exception as err:  # noqa: BLE001
            if not was_active:
                _LOGGER.debug("Live stream start failed: %s", redact_text(err))
                return
        else:
            start_ok = self.streaming_response_ok(response)
            if not start_ok and not was_active:
                _LOGGER.debug(
                    "Live stream start rejected: %s",
                    redact_text(response, site_ids=(coord.site_id,)),
                )
                return
        if start_ok:
            duration = self.streaming_duration_s(response)
            coord._streaming = True
            coord._streaming_until = time.monotonic() + duration
        if manual:
            coord._streaming_manual = True
            coord._streaming_targets.clear()
        elif (self.streaming_active() or was_active) and serial is not None:
            if expected_state is not None:
                coord._streaming_targets[str(serial)] = bool(expected_state)

    async def async_stop_streaming(self, *, manual: bool = False) -> None:
        coord = self.coordinator
        active = self.streaming_active()
        if not manual and coord._streaming_manual:
            return
        if not manual and not active:
            return
        try:
            await coord.client.stop_live_stream()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Live stream stop failed: %s", redact_text(err))
        self.clear_streaming_state()

    def schedule_stream_stop(self, *, force: bool = False) -> None:
        coord = self.coordinator
        existing = coord._streaming_stop_task
        if existing and not existing.done():
            return

        async def _runner() -> None:
            if force:
                try:
                    await coord.client.stop_live_stream()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Live stream stop failed: %s", redact_text(err))
                self.clear_streaming_state()
            else:
                await coord.async_stop_streaming()

        try:
            task = coord.hass.async_create_task(
                _runner(), name="enphase_ev_stop_stream"
            )
        except TypeError:
            task = coord.hass.async_create_task(_runner())
        coord._streaming_stop_task = task

        def _cleanup(task: asyncio.Task) -> None:
            if coord._streaming_stop_task is task:
                coord._streaming_stop_task = None

        task.add_done_callback(_cleanup)

    def record_actual_charging(self, sn: str, charging: bool | None) -> None:
        coord = self.coordinator
        sn_str = str(sn)
        if charging is None:
            coord._last_actual_charging.pop(sn_str, None)
            return
        previous = coord._last_actual_charging.get(sn_str)
        if previous is not None and previous != charging:
            coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        coord._last_actual_charging[sn_str] = charging
        if not coord._streaming_manual and self.streaming_active():
            expected = coord._streaming_targets.get(sn_str)
            if expected is not None and charging == expected:
                coord._streaming_targets.pop(sn_str, None)
                if not coord._streaming_targets:
                    coord._streaming = False
                    coord._streaming_until = None
                    coord._schedule_stream_stop(force=True)

    def set_charging_expectation(
        self,
        sn: str,
        should_charge: bool,
        hold_for: float = 90.0,
    ) -> None:
        sn_str = str(sn)
        try:
            hold = float(hold_for)
        except Exception:
            hold = 90.0
        if hold <= 0:
            self.coordinator._pending_charging.pop(sn_str, None)
            return
        expires = time.monotonic() + hold
        self.coordinator._pending_charging[sn_str] = (bool(should_charge), expires)

    def slow_interval_floor(self) -> int:
        coord = self.coordinator
        slow_floor = DEFAULT_SLOW_POLL_INTERVAL
        if coord.config_entry is not None:
            try:
                slow_opt = coord.config_entry.options.get(
                    OPT_SLOW_POLL_INTERVAL,
                    DEFAULT_SLOW_POLL_INTERVAL,
                )
                slow_floor = max(slow_floor, int(slow_opt))
            except Exception:
                slow_floor = max(slow_floor, DEFAULT_SLOW_POLL_INTERVAL)
        if coord.update_interval:
            try:
                slow_floor = max(slow_floor, int(coord.update_interval.total_seconds()))
            except Exception:
                pass
        return max(1, slow_floor)

    def set_last_set_amps(self, sn: str, amps: int) -> None:
        safe = self.apply_amp_limits(str(sn), amps)
        self.coordinator.last_set_amps[str(sn)] = safe

    def require_plugged(self, sn: str) -> None:
        coord = self.coordinator
        try:
            data = (coord.data or {}).get(str(sn), {})
        except Exception:
            data = {}
        if data.get("plugged") is True:
            return
        display = data.get("display_name") or data.get("name") or sn
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="exceptions.charger_not_plugged",
            translation_placeholders={"name": str(display)},
        )

    def ensure_serial_tracked(self, serial: str) -> bool:
        coord = self.coordinator
        if not hasattr(coord, "serials") or coord.serials is None:
            coord.serials = set()
        if not hasattr(coord, "_serial_order") or coord._serial_order is None:
            coord._serial_order = []
        if serial is None:
            return False
        try:
            sn = str(serial).strip()
        except Exception:
            return False
        if not sn:
            return False
        if sn not in coord.serials:
            coord.serials.add(sn)
            if sn not in coord._serial_order:
                coord._serial_order.append(sn)
            _LOGGER.info(
                "Discovered Enphase charger serial=%s during update",
                redact_identifier(sn),
            )
            return True
        if sn not in coord._serial_order:
            coord._serial_order.append(sn)
        return False

    def get_desired_charging(self, sn: str) -> bool | None:
        return self.coordinator._desired_charging.get(str(sn))

    def set_desired_charging(self, sn: str, desired: bool | None) -> None:
        sn_str = str(sn)
        if desired is None:
            self.coordinator._desired_charging.pop(sn_str, None)
            return
        self.coordinator._desired_charging[sn_str] = bool(desired)

    @staticmethod
    def coerce_amp(value: object) -> int | None:
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

    def amp_limits(self, sn: str) -> tuple[int | None, int | None]:
        try:
            data = (self.coordinator.data or {}).get(str(sn))
        except Exception:
            data = None
        data = data or {}
        min_amp = self.coerce_amp(data.get("min_amp"))
        max_amp = self.coerce_amp(data.get("max_amp"))
        if min_amp is not None and max_amp is not None and max_amp < min_amp:
            max_amp = min_amp
        return min_amp, max_amp

    def apply_amp_limits(self, sn: str, amps: int | float | str | None) -> int:
        value = self.coerce_amp(amps)
        if value is None:
            value = 32
        min_amp, max_amp = self.amp_limits(sn)
        if max_amp is not None and value > max_amp:
            value = max_amp
        if min_amp is not None and value < min_amp:
            value = min_amp
        return value

    def pick_start_amps(
        self,
        sn: str,
        requested: int | float | str | None = None,
        fallback: int = 32,
    ) -> int:
        sn_str = str(sn)
        candidates: list[int | float | str | None] = []
        if requested is not None:
            candidates.append(requested)
        candidates.append(self.coordinator.last_set_amps.get(sn_str))
        try:
            data = (self.coordinator.data or {}).get(sn_str)
        except Exception:
            data = None
        data = data or {}
        for key in ("charging_level", "session_charge_level"):
            if key in data:
                candidates.append(data.get(key))
        candidates.append(fallback)
        for candidate in candidates:
            coerced = self.coerce_amp(candidate)
            if coerced is not None:
                return self.apply_amp_limits(sn_str, coerced)
        return self.apply_amp_limits(sn_str, fallback)

    async def async_get_charge_mode(self, sn: str) -> str | None:
        cached = self.cached_charge_mode_preference(sn)
        if cached is not None:
            return cached
        now = time.monotonic()
        try:
            mode = self.normalize_charge_mode_preference(
                await self.coordinator.client.charge_mode(sn)
            )
        except SchedulerUnavailable as err:
            self.coordinator.note_scheduler_unavailable(err)
            return None
        except Exception:
            mode = None
        if mode:
            self.coordinator.mark_scheduler_available()
            self.coordinator._charge_mode_cache[sn] = (mode, now)
        return mode

    async def async_get_green_battery_setting(
        self, sn: str
    ) -> tuple[bool | None, bool] | None:
        now = time.monotonic()
        cached = self.coordinator._green_battery_cache.get(sn)
        if cached and (now - cached[2] < GREEN_BATTERY_CACHE_TTL):
            return cached[0], cached[1]
        try:
            settings = await self.coordinator.client.green_charging_settings(sn)
        except SchedulerUnavailable as err:
            self.coordinator.note_scheduler_unavailable(err)
            return None
        except Exception:
            return None
        self.coordinator.mark_scheduler_available()
        enabled: bool | None = None
        supported = False

        def _as_bool(value: object) -> bool | None:
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
        self.coordinator._green_battery_cache[sn] = (enabled, supported, now)
        return enabled, supported

    async def async_get_auth_settings(
        self, sn: str
    ) -> tuple[bool | None, bool | None, bool, bool] | None:
        coord = self.coordinator
        now = time.monotonic()
        cached = coord._auth_settings_cache.get(sn)
        if cached and (now - cached[4] < AUTH_SETTINGS_CACHE_TTL):
            return cached[0], cached[1], cached[2], cached[3]
        if coord._auth_settings_backoff_active():
            if cached:
                return cached[0], cached[1], cached[2], cached[3]
            return None
        try:
            settings = await coord.client.charger_auth_settings(sn)
        except AuthSettingsUnavailable as err:
            coord.note_auth_settings_unavailable(err)
            return None
        except Exception:
            return None
        app_enabled: bool | None = None
        rfid_enabled: bool | None = None
        app_supported = False
        rfid_supported = False

        def _coerce(value: object) -> bool | None:
            if value is None:
                return None
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
        coord.mark_auth_settings_available()
        coord._auth_settings_cache[sn] = (
            app_enabled,
            rfid_enabled,
            app_supported,
            rfid_supported,
            now,
        )
        return app_enabled, rfid_enabled, app_supported, rfid_supported

    async def async_get_charger_config(
        self,
        sn: str,
        *,
        keys: Iterable[str],
    ) -> dict[str, object] | None:
        coord = self.coordinator
        now = time.monotonic()
        requested: list[str] = []
        seen: set[str] = set()
        for key in keys:
            try:
                key_text = str(key).strip()
            except Exception:
                continue
            if not key_text or key_text in seen:
                continue
            seen.add(key_text)
            requested.append(key_text)
        if not requested:
            return {}

        cached = coord._charger_config_cache.get(sn)
        cached_values: dict[str, object] = {}
        cache_fresh = False
        if cached and (now - cached[1] < CHARGER_CONFIG_CACHE_TTL):
            cache_fresh = True
            cached_values = dict(cached[0])
            if all(key in cached_values for key in requested):
                return {key: cached_values[key] for key in requested}
        elif cached:
            cached_values = dict(cached[0])

        backoff_until = coord._charger_config_backoff_until.get(sn)
        if backoff_until is not None and backoff_until > now:
            if cache_fresh:
                return {
                    key: cached_values[key] for key in requested if key in cached_values
                }
            return None

        try:
            settings = await coord.client.charger_config(sn, requested)
        except Exception:
            coord._charger_config_backoff_until[sn] = (
                time.monotonic() + CHARGER_CONFIG_FAILURE_BACKOFF_S
            )
            if cache_fresh:
                return {
                    key: cached_values[key] for key in requested if key in cached_values
                }
            return None

        merged = dict(cached_values)
        if isinstance(settings, list):
            for item in settings:
                if not isinstance(item, dict):
                    continue
                key = item.get("key")
                try:
                    key_text = str(key).strip()
                except Exception:
                    continue
                if key_text not in seen:
                    continue
                if "value" in item:
                    merged[key_text] = item.get("value")
                elif "reqValue" in item:
                    merged[key_text] = item.get("reqValue")

        coord._charger_config_cache[sn] = (merged, now)
        coord._charger_config_backoff_until.pop(sn, None)
        return {key: merged[key] for key in requested if key in merged}

    def set_charge_mode_cache(self, sn: str, mode: str) -> None:
        normalized = self.normalize_charge_mode_preference(mode)
        if normalized is None:
            return
        self.coordinator._charge_mode_cache[str(sn)] = (normalized, time.monotonic())

    def set_green_battery_cache(
        self, sn: str, enabled: bool, supported: bool = True
    ) -> None:
        self.coordinator._green_battery_cache[str(sn)] = (
            bool(enabled),
            bool(supported),
            time.monotonic(),
        )

    def set_app_auth_cache(self, sn: str, enabled: bool) -> None:
        sn_str = str(sn)
        now = time.monotonic()
        cached = self.coordinator._auth_settings_cache.get(sn_str)
        if cached:
            _, rfid_enabled, _app_supported, rfid_supported, _ts = cached
            self.coordinator._auth_settings_cache[sn_str] = (
                bool(enabled),
                rfid_enabled,
                True,
                rfid_supported,
                now,
            )
            return
        self.coordinator._auth_settings_cache[sn_str] = (
            bool(enabled),
            None,
            True,
            False,
            now,
        )

    async def async_resolve_green_battery_settings(
        self, serials: Iterable[str]
    ) -> dict[str, tuple[bool | None, bool]]:
        coord = self.coordinator
        results: dict[str, tuple[bool | None, bool]] = {}
        pending: dict[str, asyncio.Task[tuple[bool | None, bool] | None]] = {}
        now = time.monotonic()
        if coord._scheduler_backoff_active():
            for sn in dict.fromkeys(serials):
                if not sn:
                    continue
                cached = coord._green_battery_cache.get(sn)
                if cached and (now - cached[2] < GREEN_BATTERY_CACHE_TTL):
                    results[sn] = (cached[0], cached[1])
            return results
        for sn in dict.fromkeys(serials):
            if not sn:
                continue
            cached = coord._green_battery_cache.get(sn)
            if cached and (now - cached[2] < GREEN_BATTERY_CACHE_TTL):
                results[sn] = (cached[0], cached[1])
                continue
            pending[sn] = asyncio.create_task(self.async_get_green_battery_setting(sn))
        if pending:
            responses = await asyncio.gather(*pending.values(), return_exceptions=True)
            for sn, response in zip(pending.keys(), responses, strict=False):
                if isinstance(response, Exception):
                    _LOGGER.debug(
                        "Green battery setting lookup failed for %s: %s",
                        redact_identifier(sn),
                        redact_text(
                            response,
                            site_ids=(coord.site_id,),
                            identifiers=(sn,),
                        ),
                    )
                    cached = coord._green_battery_cache.get(sn)
                    if cached:
                        results[sn] = (cached[0], cached[1])
                    continue
                if response is None:
                    cached = coord._green_battery_cache.get(sn)
                    if cached:
                        results[sn] = (cached[0], cached[1])
                    continue
                results[sn] = response
        return results

    async def async_resolve_auth_settings(
        self, serials: Iterable[str]
    ) -> dict[str, tuple[bool | None, bool | None, bool, bool]]:
        coord = self.coordinator
        results: dict[str, tuple[bool | None, bool | None, bool, bool]] = {}
        pending: dict[
            str,
            asyncio.Task[tuple[bool | None, bool | None, bool, bool] | None],
        ] = {}
        now = time.monotonic()
        if coord._auth_settings_backoff_active():
            for sn in dict.fromkeys(serials):
                if not sn:
                    continue
                cached = coord._auth_settings_cache.get(sn)
                if cached and (now - cached[4] < AUTH_SETTINGS_CACHE_TTL):
                    results[sn] = cached[0], cached[1], cached[2], cached[3]
            return results
        for sn in dict.fromkeys(serials):
            if not sn:
                continue
            cached = coord._auth_settings_cache.get(sn)
            if cached and (now - cached[4] < AUTH_SETTINGS_CACHE_TTL):
                results[sn] = cached[0], cached[1], cached[2], cached[3]
                continue
            pending[sn] = asyncio.create_task(self.async_get_auth_settings(sn))
        if pending:
            responses = await asyncio.gather(*pending.values(), return_exceptions=True)
            for sn, response in zip(pending.keys(), responses, strict=False):
                if isinstance(response, Exception):
                    _LOGGER.debug(
                        "Auth settings lookup failed for %s: %s",
                        redact_identifier(sn),
                        redact_text(
                            response,
                            site_ids=(coord.site_id,),
                            identifiers=(sn,),
                        ),
                    )
                    cached = coord._auth_settings_cache.get(sn)
                    if cached:
                        results[sn] = cached[0], cached[1], cached[2], cached[3]
                    continue
                if response is None:
                    cached = coord._auth_settings_cache.get(sn)
                    if cached:
                        results[sn] = cached[0], cached[1], cached[2], cached[3]
                    continue
                results[sn] = response
        return results

    async def async_resolve_charger_config(
        self,
        serials: Iterable[str],
        *,
        keys: Iterable[str],
    ) -> dict[str, dict[str, object]]:
        coord = self.coordinator
        requested: list[str] = []
        seen: set[str] = set()
        for key in keys:
            try:
                key_text = str(key).strip()
            except Exception:
                continue
            if not key_text or key_text in seen:
                continue
            seen.add(key_text)
            requested.append(key_text)
        if not requested:
            return {}

        results: dict[str, dict[str, object]] = {}
        pending: dict[str, asyncio.Task[dict[str, object] | None]] = {}
        now = time.monotonic()
        for sn in dict.fromkeys(serials):
            if not sn:
                continue
            cached = coord._charger_config_cache.get(sn)
            if cached and (now - cached[1] < CHARGER_CONFIG_CACHE_TTL):
                cached_values = dict(cached[0])
                if all(key in cached_values for key in requested):
                    results[sn] = {
                        key: cached_values[key]
                        for key in requested
                        if key in cached_values
                    }
                    continue
            pending[sn] = asyncio.create_task(
                self.async_get_charger_config(sn, keys=requested)
            )
        if pending:
            responses = await asyncio.gather(*pending.values(), return_exceptions=True)
            for sn, response in zip(pending.keys(), responses, strict=False):
                if isinstance(response, Exception):
                    _LOGGER.debug(
                        "Charger config lookup failed for %s: %s",
                        redact_identifier(sn),
                        redact_text(
                            response,
                            site_ids=(coord.site_id,),
                            identifiers=(sn,),
                        ),
                    )
                    cached = coord._charger_config_cache.get(sn)
                    if cached:
                        cached_values = dict(cached[0])
                        filtered = {
                            key: cached_values[key]
                            for key in requested
                            if key in cached_values
                        }
                        if filtered:
                            results[sn] = filtered
                    continue
                if response:
                    results[sn] = response
        return results

    def resolve_charge_mode_pref(self, sn: str) -> str | None:
        sn_str = str(sn)
        try:
            data = (self.coordinator.data or {}).get(sn_str)
        except Exception:
            data = None
        data = data or {}
        candidates: list[str | None] = [
            data.get("charge_mode_pref"),
            data.get("charge_mode"),
            self.schedule_type_charge_mode_preference(data.get("schedule_type")),
        ]
        cached = self.cached_charge_mode_preference(sn_str)
        if cached is not None:
            candidates.append(cached)
        battery_profile = self.battery_profile_charge_mode_preference(sn_str)
        if battery_profile is not None:
            candidates.append(battery_profile)
        for raw in candidates:
            value = self.normalize_charge_mode_preference(raw)
            if value is not None:
                return value
        return None

    def battery_profile_charge_mode_preference(self, sn: str) -> str | None:
        coord = self.coordinator
        sn_str = str(sn)
        configured_serials = self.normalize_serials(
            getattr(coord, "_configured_serials", ())
        )
        if configured_serials:
            if len(configured_serials) != 1 or sn_str not in configured_serials:
                return None
        else:
            serials = self.normalize_serials(getattr(coord, "serials", ()))
            if len(serials) != 1 or sn_str not in serials:
                return None
        cache_until = getattr(coord, "_storm_guard_cache_until", None)
        if cache_until is None:
            return None
        try:
            if time.monotonic() >= float(cache_until):
                return None
        except Exception:
            return None
        try:
            devices = getattr(coord, "_battery_profile_devices", None)
        except Exception:
            return None
        if not isinstance(devices, list) or len(devices) != 1:
            return None
        device = devices[0]
        if not isinstance(device, dict):
            return None
        return self.normalize_charge_mode_preference(device.get("chargeMode"))

    def cached_charge_mode_preference(
        self,
        sn: str,
        *,
        now: float | None = None,
    ) -> str | None:
        cache_entry = self.coordinator._charge_mode_cache.get(str(sn))
        if not cache_entry:
            return None
        if now is None:
            now = time.monotonic()
        if now - cache_entry[1] >= CHARGE_MODE_CACHE_TTL:
            return None
        return self.normalize_charge_mode_preference(cache_entry[0])

    @staticmethod
    def schedule_type_charge_mode_preference(schedule_type: object) -> str | None:
        if schedule_type is None:
            return None
        try:
            normalized = str(schedule_type).strip().upper()
        except Exception:
            return None
        if not normalized:
            return None
        compact = normalized.replace("_", "").replace("-", "").replace(" ", "")
        if compact == "GREENCHARGING":
            return "GREEN_CHARGING"
        return None

    @staticmethod
    def normalize_charge_mode_preference(value: object) -> str | None:
        if value is None:
            return None
        try:
            normalized = str(value).strip().upper()
        except Exception:
            return None
        if not normalized:
            return None
        return CHARGE_MODE_PREFERENCE_MAP.get(normalized)

    def normalize_effective_charge_mode(self, value: object) -> str | None:
        preferred = self.normalize_charge_mode_preference(value)
        if preferred is not None:
            return preferred
        if value is None:
            return None
        try:
            normalized = str(value).strip().upper()
        except Exception:
            return None
        if not normalized:
            return None
        if normalized in EFFECTIVE_CHARGE_MODE_VALUES:
            return normalized
        return None

    def charge_mode_start_preferences(self, sn: str) -> ChargeModeStartPreferences:
        mode = self.resolve_charge_mode_pref(sn)
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
        elif mode in {"GREEN_CHARGING", "SMART_CHARGING"}:
            include_level = False
            strict = True
        return ChargeModeStartPreferences(
            mode=mode,
            include_level=include_level,
            strict=strict,
            enforce_mode=enforce_mode,
        )

    async def async_ensure_charge_mode(self, sn: str, target_mode: str) -> None:
        sn_str = str(sn)
        try:
            await self.coordinator.client.set_charge_mode(sn_str, target_mode)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to enforce %s charge mode for charger %s: %s",
                target_mode,
                redact_identifier(sn_str),
                redact_text(
                    err,
                    site_ids=(self.coordinator.site_id,),
                    identifiers=(sn_str,),
                ),
            )
            return
        self.set_charge_mode_cache(sn_str, target_mode)
