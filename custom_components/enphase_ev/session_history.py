"""Session history helpers for the Enphase EV coordinator."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as _tz
from typing import Any, Awaitable, Callable, Iterable

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import Unauthorized

_LOGGER = logging.getLogger(__name__)

MIN_SESSION_HISTORY_CACHE_TTL = 60  # seconds
SESSION_HISTORY_FAILURE_BACKOFF_S = 15 * 60
SESSION_HISTORY_CONCURRENCY = 3


@dataclass(slots=True)
class SessionCacheView:
    """Represents the current cache state for a serial."""

    sessions: list[dict]
    cache_age: float | None
    needs_refresh: bool
    blocked: bool


class SessionHistoryManager:
    """Encapsulate session history caching, fetching, and enrichment."""

    def __init__(
        self,
        hass: HomeAssistant,
        client_getter: Callable[[], Any],
        *,
        cache_ttl: float,
        failure_backoff: float = SESSION_HISTORY_FAILURE_BACKOFF_S,
        concurrency: int = SESSION_HISTORY_CONCURRENCY,
        data_supplier: Callable[[], dict[str, dict] | None] | None = None,
        publish_callback: Callable[[dict[str, dict]], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._hass = hass
        self._client_getter = client_getter
        self._cache_ttl = max(MIN_SESSION_HISTORY_CACHE_TTL, float(cache_ttl or 0))
        self._failure_backoff = max(MIN_SESSION_HISTORY_CACHE_TTL, float(failure_backoff))
        self._concurrency = max(1, int(concurrency))
        self._cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
        self._block_until: dict[str, float] = {}
        self._refresh_in_progress: set[str] = set()
        self._data_supplier = data_supplier
        self._publish_callback = publish_callback
        self._logger = logger or _LOGGER
        self._fetch_override: Callable[
            [str, datetime | None], Awaitable[list[dict]]
        ] | None = None

    @property
    def cache_ttl(self) -> float:
        """Return the cache TTL in seconds."""
        return self._cache_ttl

    @cache_ttl.setter
    def cache_ttl(self, value: float | None) -> None:
        """Update the cache TTL, enforcing sane bounds."""
        if value is None:
            self._cache_ttl = MIN_SESSION_HISTORY_CACHE_TTL
            return
        try:
            ttl = float(value)
        except (TypeError, ValueError):
            ttl = MIN_SESSION_HISTORY_CACHE_TTL
        self._cache_ttl = max(MIN_SESSION_HISTORY_CACHE_TTL, ttl)

    @property
    def cache_key_count(self) -> int:
        """Return the number of cached serial/day entries."""
        return len(self._cache)

    @property
    def in_progress(self) -> int:
        """Return the number of serials going through enrichment."""
        return len(self._refresh_in_progress)

    def get_cache_view(
        self,
        serial: str,
        day_key: str,
        now_mono: float | None = None,
    ) -> SessionCacheView:
        """Return the cache state for a serial/day pair."""
        now = now_mono or time.monotonic()
        cache_key = (serial, day_key)
        cached = self._cache.get(cache_key)
        sessions: list[dict] = []
        cache_age: float | None = None
        if cached:
            cached_ts, cached_sessions = cached
            cache_age = now - cached_ts
            sessions = cached_sessions
        needs_refresh = cached is None or (
            cache_age is not None and cache_age >= self._cache_ttl
        )
        block_until = self._block_until.get(serial)
        blocked = bool(block_until and block_until > now)
        return SessionCacheView(
            sessions=sessions or [],
            cache_age=cache_age,
            needs_refresh=needs_refresh,
            blocked=blocked,
        )

    def schedule_enrichment(self, serials: Iterable[str], day_local: datetime) -> None:
        """Launch a background enrichment task for the provided serials."""
        candidates = [sn for sn in dict.fromkeys(serials) if sn]
        pending = [
            sn for sn in candidates if sn not in self._refresh_in_progress
        ]
        if not pending:
            return
        self._refresh_in_progress.update(pending)

        async def _run() -> None:
            try:
                updates = await self._async_enrich_sessions(
                    pending, day_local=day_local
                )
                if updates:
                    self._apply_updates(updates)
            finally:
                for sn in pending:
                    self._refresh_in_progress.discard(sn)

        try:
            self._hass.async_create_task(
                _run(),
                name="enphase_ev_session_enrichment",
            )
        except TypeError:
            self._hass.async_create_task(_run())

    async def async_enrich(
        self,
        serials: Iterable[str],
        day_local: datetime,
        *,
        in_background: bool,
    ) -> dict[str, list[dict]]:
        """Fetch session history for the provided serials."""
        updates = await self._async_enrich_sessions(serials, day_local=day_local)
        if in_background and updates:
            self._apply_updates(updates)
        return updates

    async def _async_enrich_sessions(
        self,
        serials: Iterable[str],
        *,
        day_local: datetime,
    ) -> dict[str, list[dict]]:
        serial_list = [sn for sn in dict.fromkeys(serials) if sn]
        if not serial_list:
            return {}
        semaphore = asyncio.Semaphore(self._concurrency)

        async def _refresh(sn: str) -> tuple[str, list[dict] | None]:
            async with semaphore:
                try:
                    sessions = await self._async_fetch_sessions_today(
                        sn, day_local=day_local
                    )
                except asyncio.CancelledError as err:
                    self._logger.debug(
                        "Session history enrichment cancelled for %s: %s", sn, err
                    )
                    return sn, None
                except Unauthorized as err:
                    self._logger.debug(
                        "Session history unauthorized for %s during enrichment: %s",
                        sn,
                        err,
                    )
                    return sn, None
                except Exception as err:  # noqa: BLE001
                    self._logger.debug(
                        "Session history enrichment failed for %s: %s", sn, err
                    )
                    return sn, None
                return sn, sessions

        tasks = [asyncio.create_task(_refresh(sn)) for sn in serial_list]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        updates: dict[str, list[dict]] = {}
        for response in responses:
            if isinstance(response, Exception):
                self._logger.debug("Session history enrichment task error: %s", response)
                continue
            sn, sessions = response
            if sessions is None:
                continue
            updates[sn] = sessions
        return updates

    def _apply_updates(self, updates: dict[str, list[dict]]) -> None:
        """Merge enrichment results into the published coordinator data."""
        if not updates or not self._publish_callback or not self._data_supplier:
            return
        current = self._data_supplier()
        if not isinstance(current, dict):
            return
        merged: dict[str, dict] = {}
        for sn, payload in current.items():
            merged[sn] = dict(payload)
        for sn, sessions in updates.items():
            entry = merged.setdefault(sn, {})
            entry["energy_today_sessions"] = sessions
            entry["energy_today_sessions_kwh"] = self._sum_session_energy(sessions)
        self._publish_callback(merged)

    async def _async_fetch_sessions_today(
        self,
        sn: str,
        *,
        day_local: datetime | None = None,
    ) -> list[dict]:
        """Return session history for the provided day, caching results."""
        if self._fetch_override is not None:
            return await self._fetch_override(sn, day_local=day_local)
        if not sn:
            return []
        if day_local is None:
            day_local = dt_util.now()
        try:
            local_dt = dt_util.as_local(day_local)
        except Exception:
            if day_local.tzinfo is None:
                day_local = day_local.replace(tzinfo=dt_util.UTC)
            local_dt = dt_util.as_local(day_local)

        day_key = local_dt.strftime("%Y-%m-%d")
        cache_key = (sn, day_key)
        now_mono = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and (now_mono - cached[0] < self._cache_ttl):
            return cached[1]
        block_until = self._block_until.get(sn)
        if block_until and now_mono < block_until:
            return cached[1] if cached else []

        api_day = local_dt.strftime("%d-%m-%Y")
        client = self._client_getter()
        if client is None:
            self._logger.debug("Session history fetch skipped; client unavailable")
            return []

        async def _fetch_page(offset: int, limit: int) -> tuple[list[dict], bool]:
            payload = await client.session_history(
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
            self._logger.debug(
                "Session history unauthorized for %s on %s: %s",
                sn,
                api_day,
                err,
            )
            self._cache[cache_key] = (now_mono, [])
            return []
        except aiohttp.ClientResponseError as err:
            self._logger.debug(
                "Session history server error for %s on %s: %s (%s)",
                sn,
                api_day,
                err.status,
                err.message,
            )
            if err.status in (500, 502, 503, 504, 550):
                self._block_until[sn] = now_mono + self._failure_backoff
            self._cache[cache_key] = (now_mono, [])
            return []
        except Exception as err:  # noqa: BLE001
            self._logger.debug(
                "Session history fetch failed for %s on %s: %s", sn, api_day, err
            )
            self._cache[cache_key] = (now_mono, [])
            return []

        sessions = self._normalise_sessions_for_day(local_dt=local_dt, results=results)
        self._block_until.pop(sn, None)
        self._cache[cache_key] = (now_mono, sessions)
        return sessions

    def set_fetch_override(
        self,
        callback: Callable[[str, datetime | None], Awaitable[list[dict]]] | None,
    ) -> None:
        """Allow callers to override the fetch implementation (legacy hook)."""
        self._fetch_override = callback

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
                    dt_val = datetime.fromisoformat(cleaned)
                except ValueError:
                    return None
                if dt_val.tzinfo is None:
                    dt_val = dt_val.replace(tzinfo=_tz.utc)
                try:
                    return dt_util.as_local(dt_val)
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

            window_start = start_dt or day_start
            window_end = end_dt or now_local

            if window_end < window_start:
                window_end = window_start

            if not (window_start < day_end and window_end >= day_start):
                continue

            energy_total_kwh = _as_float(item.get("aggEnergyValue"), precision=3)
            if energy_total_kwh is None:
                energy_total_kwh = _as_float(item.get("aggEnergyValue"))

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

    def sum_energy(self, sessions: list[dict]) -> float:
        """Public wrapper for summing session energy."""
        return self._sum_session_energy(sessions)

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
