"""Helpers for working with Enphase Energy summary responses."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

SUMMARY_IDLE_TTL = 600.0
SUMMARY_ACTIVE_MIN_TTL = 5.0


class SummaryStore:
    """Cache and manage summary_v2 responses."""

    def __init__(
        self,
        client_getter: Callable[[], Any],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client_getter = client_getter
        self._logger = logger or _LOGGER
        self._cache: tuple[float, list[dict], float] | None = None
        self._ttl: float = SUMMARY_IDLE_TTL
        self._lock = asyncio.Lock()

    @property
    def ttl(self) -> float:
        """Return the current cache TTL in seconds."""
        return self._ttl

    def invalidate(self) -> None:
        """Drop the cached payload."""
        self._cache = None

    def _get_cache(
        self,
    ) -> tuple[float, list[dict], float] | None:
        cache = self._cache
        if not cache:
            return None
        if isinstance(cache, tuple) and len(cache) == 3:
            ts, data, ttl = cache
            return ts, data, float(ttl)
        if isinstance(cache, tuple) and len(cache) == 2:
            ts, data = cache
            return ts, data, SUMMARY_IDLE_TTL
        return None

    def prepare_refresh(
        self, *, want_fast: bool, target_interval: float | None
    ) -> bool:
        """Update the cache TTL target and return if a refresh is required."""
        summary_ttl = SUMMARY_IDLE_TTL
        if want_fast and target_interval:
            summary_ttl = max(
                SUMMARY_ACTIVE_MIN_TTL,
                min(target_interval, SUMMARY_IDLE_TTL),
            )
        cache_info = self._get_cache()
        force = False
        if cache_info is None:
            force = True
        else:
            cache_ts, cache_data, cache_ttl = cache_info
            age = time.monotonic() - cache_ts
            if cache_ttl > summary_ttl or age >= summary_ttl:
                force = True
            elif cache_ttl != summary_ttl:
                self._cache = (cache_ts, cache_data, summary_ttl)
        self._ttl = summary_ttl
        return force

    async def async_fetch(self, *, force: bool = False) -> list[dict]:
        """Return the cached summary, optionally forcing a refresh."""
        cache = self._get_cache()
        if not force and cache:
            cache_ts, cache_data, cache_ttl = cache
            if time.monotonic() - cache_ts < cache_ttl:
                return cache_data

        async with self._lock:
            cached = self._get_cache()
            if not force and cached:
                cache_ts, cache_data, cache_ttl = cached
                if time.monotonic() - cache_ts < cache_ttl:
                    return cache_data
            client = self._client_getter()
            if client is None:
                self._logger.debug("Summary v2 fetch skipped; client unavailable")
                return cached[1] if cached else []
            try:
                summary = await client.summary_v2()
            except Exception as err:  # noqa: BLE001
                if cached:
                    self._logger.debug(
                        "Summary v2 fetch failed; reusing cache: %s",
                        err,
                    )
                    return cached[1]
                self._logger.debug("Summary v2 fetch failed: %s", err)
                return []

            summary_list = self._as_list(summary)
            self._cache = (time.monotonic(), summary_list, self._ttl)
            return summary_list

    def _as_list(self, summary: Any) -> list[dict]:
        """Normalize the raw summary payload into a list."""
        if not summary:
            return []
        if isinstance(summary, list):
            return summary
        if isinstance(summary, dict):
            interim = summary.get("data")
            return interim if isinstance(interim, list) else []
        if isinstance(summary, (tuple, set)):
            return list(summary)
        return []
