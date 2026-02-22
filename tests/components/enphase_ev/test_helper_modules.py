"""Tests for helper modules used by the Enphase Energy coordinator."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp.client_reqrep import RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from custom_components.enphase_ev import session_history as sh_mod
from custom_components.enphase_ev.api import Unauthorized
from custom_components.enphase_ev.session_history import (
    MIN_SESSION_HISTORY_CACHE_TTL,
    SessionCacheView,
    SessionHistoryManager,
)
from custom_components.enphase_ev.summary import SummaryStore, SUMMARY_ACTIVE_MIN_TTL, SUMMARY_IDLE_TTL


class _DummySummaryClient:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def summary_v2(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return [{"serialNumber": "EV-01"}]


@pytest.mark.asyncio
async def test_summary_store_caches_and_handles_errors() -> None:
    """SummaryStore should cache responses, adjust TTL, and reuse data on errors."""
    client = _DummySummaryClient()
    store = SummaryStore(lambda: client)

    # First refresh should require a fetch since cache is empty.
    assert store.prepare_refresh(want_fast=False, target_interval=None) is True
    data = await store.async_fetch()
    assert data == [{"serialNumber": "EV-01"}]
    assert client.calls == 1

    # Cached response should be reused without new client calls.
    cached = await store.async_fetch()
    assert cached == data
    assert client.calls == 1

    # Force refresh while the client raises: cache is still served.
    store.prepare_refresh(want_fast=True, target_interval=5)
    client.fail = True
    again = await store.async_fetch(force=True)
    assert again == data
    assert client.calls == 2

    # With no cache and a failing client, return an empty list gracefully.
    store.invalidate()
    empty = await store.async_fetch(force=True)
    assert empty == []

    # Missing client should behave the same.
    store_no_client = SummaryStore(lambda: None)
    assert await store_no_client.async_fetch(force=True) == []


def test_summary_store_cache_helpers() -> None:
    """Exercise helper branches in _get_cache and _as_list."""
    store = SummaryStore(lambda: _DummySummaryClient())
    assert store.ttl == SUMMARY_IDLE_TTL

    # len == 2 tuple falls back to default TTL
    store._cache = (time.monotonic(), [{"serialNumber": "A"}])
    cache = store._get_cache()
    assert cache and cache[2] == SUMMARY_IDLE_TTL

    # TTL mismatch should be rewritten when refreshing
    store._cache = (
        time.monotonic(),
        [{"serialNumber": "B"}],
        SUMMARY_ACTIVE_MIN_TTL,
    )
    store.prepare_refresh(want_fast=False, target_interval=None)
    cache = store._get_cache()
    assert cache and cache[2] == SUMMARY_IDLE_TTL

    # _as_list conversions
    assert store._as_list({"data": [1, 2]}) == [1, 2]
    assert store._as_list(({"x": 1}, {"y": 2})) == [{"x": 1}, {"y": 2}]
    assert store._as_list(None) == []
    assert store._as_list(object()) == []

    store._cache = "invalid"
    assert store._get_cache() is None


class _RaceSummaryStore(SummaryStore):
    def __init__(self, client_getter):
        super().__init__(client_getter)
        self._race_cache: tuple[float, list[dict], float] | None = None
        self._calls = 0

    def _get_cache(self):
        self._calls += 1
        if self._calls == 1:
            return None
        return self._race_cache


@pytest.mark.asyncio
async def test_summary_store_async_fetch_handles_race_condition() -> None:
    """Second cache check inside the lock should return without hitting the client."""
    client = _DummySummaryClient()
    store = _RaceSummaryStore(lambda: client)
    store._race_cache = (time.monotonic(), [{"serialNumber": "RACE"}], SUMMARY_IDLE_TTL)

    data = await store.async_fetch()
    assert data == [{"serialNumber": "RACE"}]
    assert client.calls == 0


class _DummySessionClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int, int]] = []

    async def session_history(
        self,
        sn: str,
        *,
        start_date: str,
        end_date: str,
        offset: int,
        limit: int,
        **_kwargs,
    ) -> dict:
        self.calls.append((sn, start_date, end_date, offset, limit))
        return {
            "data": {
                "result": [
                    {
                        "sessionId": 1,
                        "startTime": "2025-10-15T23:30:00Z[UTC]",
                        "endTime": "2025-10-16T01:30:00Z[UTC]",
                        "aggEnergyValue": 6.0,
                        "activeChargeTime": 7200,
                    },
                    {
                        "sessionId": 2,
                        "startTime": "2025-10-16T04:00:00Z[UTC]",
                        "endTime": "2025-10-16T05:00:00Z[UTC]",
                        "aggEnergyValue": 3.0,
                        "activeChargeTime": 3600,
                    },
                ],
                "hasMore": False,
            }
        }


@pytest.mark.asyncio
async def test_session_history_fetch_caches_and_override(hass) -> None:
    """SessionHistoryManager should cache results and allow overrides."""
    await hass.config.async_set_time_zone("UTC")
    client = _DummySessionClient()
    manager = SessionHistoryManager(
        hass,
        lambda: client,
        cache_ttl=600,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    day = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    sessions = await manager._async_fetch_sessions_today("EV-01", day_local=day)
    assert len(sessions) == 2
    assert len(client.calls) == 1

    # Cached result should be reused.
    again = await manager._async_fetch_sessions_today("EV-01", day_local=day)
    assert len(again) == 2
    assert len(client.calls) == 1

    # Override fetch logic (used by legacy tests) and ensure it is honored.
    async def _override(_sn: str, *_args, **_kwargs):
        return [{"session_id": "override", "energy_kwh": 1.0}]

    manager._cache.clear()
    manager.set_fetch_override(_override)
    override = await manager._async_fetch_sessions_today("EV-02", day_local=day)
    assert override == [{"session_id": "override", "energy_kwh": 1.0}]
    manager.set_fetch_override(None)


@pytest.mark.asyncio
async def test_session_history_fetch_calls_filter_criteria(hass) -> None:
    class _CriteriaClient:
        def __init__(self) -> None:
            self.criteria_calls = 0
            self.history_calls = 0

        async def session_history_filter_criteria(self, **_kwargs):
            self.criteria_calls += 1
            return {"data": [{"id": "EV-01"}]}

        async def session_history(self, *_args, **_kwargs) -> dict:
            self.history_calls += 1
            return {"data": {"result": [], "hasMore": False}}

    await hass.config.async_set_time_zone("UTC")
    client = _CriteriaClient()
    manager = SessionHistoryManager(
        hass,
        lambda: client,
        cache_ttl=600,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    day = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    await manager._async_fetch_sessions_today("EV-01", day_local=day)
    assert client.criteria_calls == 1
    assert client.history_calls == 1


def test_session_history_apply_updates_merges_data(hass) -> None:
    """Applying updates should merge sessions and compute totals."""
    published: list[dict] = []
    base = {"EV-01": {"sn": "EV-01"}}
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: base,
        publish_callback=lambda data: published.append(data),
    )

    manager._apply_updates({"EV-01": [{"energy_kwh": 1.5}]})
    assert published
    merged = published[-1]
    assert merged["EV-01"]["energy_today_sessions_kwh"] == 1.5


def test_session_history_cache_view_states(hass) -> None:
    """Verify cache view bookkeeping for refresh, block, and reuse."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=10,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    cache_key = ("EV-01", "2025-10-16")
    manager._cache[cache_key] = (time.monotonic(), [{"session_id": "cached"}])

    fresh = manager.get_cache_view("EV-01", "2025-10-16", now_mono=time.monotonic())
    assert isinstance(fresh, SessionCacheView)
    assert fresh.needs_refresh is False

    # Force expiration and block
    manager._cache[cache_key] = (time.monotonic() - 120, [{"session_id": "old"}])
    manager._block_until["EV-01"] = time.monotonic() + 100
    blocked = manager.get_cache_view("EV-01", "2025-10-16")
    assert blocked.needs_refresh is True
    assert blocked.blocked is True


def _make_request_info() -> RequestInfo:
    """Return a minimal request info for client errors."""
    return RequestInfo(
        URL("http://example.com"),
        "GET",
        CIMultiDictProxy(CIMultiDict()),
        None,
    )


@pytest.mark.asyncio
async def test_session_history_async_fetch_handles_errors(hass) -> None:
    """Cover unauthorized, server, client-less, and generic error paths."""
    await hass.config.async_set_time_zone("UTC")
    day = datetime(2025, 10, 16, tzinfo=timezone.utc)

    async def _invoke(exc):
        manager = SessionHistoryManager(
            hass,
            lambda: type("C", (), {"session_history": AsyncMock(side_effect=exc)})(),
            cache_ttl=60,
            data_supplier=lambda: {},
            publish_callback=lambda _: None,
        )
        return await manager._async_fetch_sessions_today("EV-ERR", day_local=day)

    assert await _invoke(Unauthorized("bad")) == []

    response_error = aiohttp.ClientResponseError(
        _make_request_info(),
        (),
        status=503,
        message="unavailable",
    )
    assert await _invoke(response_error) == []

    assert (
        await SessionHistoryManager(
            hass,
            lambda: None,
            cache_ttl=60,
            data_supplier=lambda: {},
            publish_callback=lambda _: None,
        )._async_fetch_sessions_today("EV-NULL", day_local=day)
    ) == []

    assert await _invoke(RuntimeError("boom")) == []


@pytest.mark.asyncio
async def test_session_history_schedule_enrichment_runs(hass, monkeypatch) -> None:
    """Background enrichment should call through to the manager."""
    await hass.config.async_set_time_zone("UTC")
    published: list[dict] = []
    manager = SessionHistoryManager(
        hass,
        lambda: _DummySessionClient(),
        cache_ttl=60,
        data_supplier=lambda: {"EV-01": {"sn": "EV-01"}},
        publish_callback=lambda data: published.append(data),
    )
    updates = {"EV-01": [{"energy_kwh": 2.0}]}
    monkeypatch.setattr(
        manager,
        "_async_enrich_sessions",
        AsyncMock(return_value=updates),
    )

    manager.schedule_enrichment(
        ["EV-01", "EV-01", "EV-02"],
        datetime.now(tz=timezone.utc),
    )
    await hass.async_block_till_done()
    assert published and published[-1]["EV-01"]["energy_today_sessions_kwh"] == 2.0

    manager.schedule_enrichment([], datetime.now(tz=timezone.utc))

    # Trigger the TypeError fallback path
    class _FlakyTask:
        def __init__(self, hass_obj):
            self.hass = hass_obj
            self.calls = 0

        def __call__(self, coro, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                coro.close()
                raise TypeError("legacy hass")
            return self.hass.loop.create_task(coro)

    monkeypatch.setattr(hass, "async_create_task", _FlakyTask(hass))
    manager.schedule_enrichment(["EV-03"], datetime.now(tz=timezone.utc))
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_session_history_async_enrich_handles_failures(hass, monkeypatch) -> None:
    """Ensure async_enrich collects updates even when some refreshes fail."""
    await hass.config.async_set_time_zone("UTC")
    manager = SessionHistoryManager(
        hass,
        lambda: _DummySessionClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    async def _fake_fetch(sn: str, *, day_local=None):
        if sn == "bad":
            raise RuntimeError("boom")
        return [{"session_id": sn, "energy_kwh": 1.0}]

    monkeypatch.setattr(manager, "_async_fetch_sessions_today", _fake_fetch)

    updates = await manager.async_enrich(
        ["good", "bad", "good"], datetime.now(tz=timezone.utc), in_background=False
    )
    assert updates == {"good": [{"session_id": "good", "energy_kwh": 1.0}]}


@pytest.mark.asyncio
async def test_session_history_async_enrich_handles_special_exceptions(hass, monkeypatch) -> None:
    """Unauthorized fetches and unexpected task failures should be ignored safely."""
    await hass.config.async_set_time_zone("UTC")
    manager = SessionHistoryManager(
        hass,
        lambda: _DummySessionClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    async def _fake_fetch(sn: str, *, day_local=None):
        if sn == "unauth":
            raise Unauthorized("denied")
        if sn == "cancel":
            raise asyncio.CancelledError()
        return [{"session_id": sn, "energy_kwh": 0.25}]

    monkeypatch.setattr(
        manager,
        "_async_fetch_sessions_today",
        AsyncMock(side_effect=_fake_fetch),
    )

    day = datetime(2025, 10, 16, tzinfo=timezone.utc)
    updates = await manager._async_enrich_sessions(
        ["ok", "unauth", "cancel"], day_local=day
    )
    assert updates == {"ok": [{"session_id": "ok", "energy_kwh": 0.25}]}


def test_session_history_apply_updates_noop_without_inputs(hass) -> None:
    """_apply_updates should return immediately when required hooks are missing."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=None,
        publish_callback=None,
    )
    manager._apply_updates(None)
    manager._apply_updates({})


@pytest.mark.asyncio
async def test_session_history_async_enrich_no_valid_serials(hass) -> None:
    """Empty serial list should short-circuit."""
    manager = SessionHistoryManager(
        hass,
        lambda: _DummySessionClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    result = await manager._async_enrich_sessions(["", None], day_local=datetime.now(tz=timezone.utc))
    assert result == {}


@pytest.mark.asyncio
async def test_session_history_async_enrich_in_background(hass, monkeypatch) -> None:
    """in_background=True should still apply updates."""
    await hass.config.async_set_time_zone("UTC")
    published: list[dict] = []
    manager = SessionHistoryManager(
        hass,
        lambda: _DummySessionClient(),
        cache_ttl=60,
        data_supplier=lambda: {"EV-01": {"sn": "EV-01"}},
        publish_callback=lambda data: published.append(data),
    )

    async def _fake_fetch(sn: str, *, day_local=None):
        return [{"session_id": sn, "energy_kwh": 0.5}]

    monkeypatch.setattr(manager, "_async_fetch_sessions_today", _fake_fetch)
    updates = await manager.async_enrich(
        ["EV-01"],
        datetime.now(tz=timezone.utc),
        in_background=True,
    )
    assert updates == {"EV-01": [{"session_id": "EV-01", "energy_kwh": 0.5}]}
    assert published


def test_session_history_cache_ttl_accessor(hass) -> None:
    """cache_ttl setter and getters should enforce sane bounds."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=120,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    assert manager.cache_ttl == max(MIN_SESSION_HISTORY_CACHE_TTL, 120)
    manager.cache_ttl = None
    assert manager.cache_ttl == MIN_SESSION_HISTORY_CACHE_TTL
    manager.cache_ttl = "5"
    assert manager.cache_ttl == MIN_SESSION_HISTORY_CACHE_TTL
    manager.cache_ttl = "bad"
    assert manager.cache_ttl == MIN_SESSION_HISTORY_CACHE_TTL
    assert manager.cache_key_count == 0
    assert manager.in_progress == 0


@pytest.mark.asyncio
async def test_session_history_async_fetch_handles_invalid_payload(hass) -> None:
    """Non-list payload should be ignored safely."""
    await hass.config.async_set_time_zone("UTC")

    class BadClient:
        async def session_history(self, *_args, **_kwargs):
            return {"data": {"result": "not-a-list", "hasMore": False}}

    manager = SessionHistoryManager(
        hass,
        lambda: BadClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    sessions = await manager._async_fetch_sessions_today(
        "EV-BAD", day_local=datetime(2025, 10, 16, tzinfo=timezone.utc)
    )
    assert sessions == []


@pytest.mark.asyncio
async def test_session_history_fetch_handles_empty_serial_and_block(hass, monkeypatch) -> None:
    """Empty serials and active block entries should short-circuit fetches."""
    await hass.config.async_set_time_zone("UTC")

    class DummyClient:
        async def session_history(self, *_args, **_kwargs):
            return {"data": {"result": [], "hasMore": False}}

    manager = SessionHistoryManager(
        hass,
        lambda: DummyClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    day = datetime(2025, 10, 16, tzinfo=timezone.utc)
    assert await manager._async_fetch_sessions_today("", day_local=day) == []

    naive_now = datetime(2025, 10, 17, 8, 0, 0)
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: naive_now)
    orig_as_local = sh_mod.dt_util.as_local
    calls = {"count": 0}

    def _fake_as_local(value):
        if calls["count"] == 0:
            calls["count"] += 1
            raise ValueError("tz boom")
        return orig_as_local(value)

    monkeypatch.setattr(sh_mod.dt_util, "as_local", _fake_as_local)

    assert await manager._async_fetch_sessions_today("EV-ALPHA", day_local=None) == []

    day_local = orig_as_local(naive_now.replace(tzinfo=timezone.utc))
    day_key = day_local.strftime("%Y-%m-%d")
    cache_key = ("EV-ALPHA", day_key)
    manager._cache[cache_key] = (time.monotonic(), ["cached"])
    manager._block_until["EV-ALPHA"] = time.monotonic() + 30
    cached = await manager._async_fetch_sessions_today("EV-ALPHA", day_local=day_local)
    assert cached == ["cached"]


@pytest.mark.asyncio
async def test_session_history_fetch_handles_page_unauthorized(hass) -> None:
    """Unauthorized responses during pagination should be cached as empty."""

    class FailingClient:
        async def session_history(self, *_args, **_kwargs):
            raise Unauthorized("blocked")

    await hass.config.async_set_time_zone("UTC")
    manager = SessionHistoryManager(
        hass,
        lambda: FailingClient(),
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    day = datetime(2025, 10, 16, tzinfo=timezone.utc)
    sessions = await manager._async_fetch_sessions_today("EV-DENY", day_local=day)
    assert sessions == []
    day_key = day.strftime("%Y-%m-%d")
    assert manager._cache[("EV-DENY", day_key)][1] == []


def test_session_history_normalise_handles_parse_failures(hass, monkeypatch) -> None:
    """Normalisation should survive malformed timestamps and metrics."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    local_dt = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    orig_as_local = sh_mod.dt_util.as_local
    flags = {"outer": True, "inner": True}

    def _fake_as_local(value):
        if value is local_dt and flags["outer"]:
            flags["outer"] = False
            raise ValueError("outer")
        if (
            isinstance(value, datetime)
            and value.year == 2025
            and value.month == 10
            and value.day == 16
            and value.hour == 2
            and flags["inner"]
        ):
            flags["inner"] = False
            raise ValueError("inner")
        return orig_as_local(value)

    monkeypatch.setattr(sh_mod.dt_util, "as_local", _fake_as_local)

    orig_datetime = sh_mod.datetime

    class _FakeDatetime:
        @staticmethod
        def fromtimestamp(val, tz=None):
            if int(val) == 24601:
                raise OverflowError
            return orig_datetime.fromtimestamp(val, tz=tz)

        @staticmethod
        def fromisoformat(value):
            return orig_datetime.fromisoformat(value)

    monkeypatch.setattr(sh_mod, "datetime", _FakeDatetime)

    entries = [
        None,
        {
            "sessionId": "bad-numeric",
            "startTime": 24601,
            "endTime": 24700,
            "aggEnergyValue": "bad",
            "activeChargeTime": "bad",
            "costCalculated": "yes",
        },
        {
            "sessionId": "bad-iso",
            "startTime": "not-a-date",
            "endTime": "2025-10-16T00:30:00Z",
            "aggEnergyValue": 0.5,
            "manualOverridden": 1,
        },
        {
            "sessionId": "naive",
            "startTime": "2025-10-16T02:00:00",
            "endTime": "2025-10-16T02:10:00",
            "aggEnergyValue": 1.5,
            "activeChargeTime": True,
        },
        {
            "sessionId": "window",
            "startTime": "2025-10-16T03:00:00Z",
            "endTime": "2025-10-16T02:30:00Z",
            "aggEnergyValue": 2.0,
            "activeChargeTime": 60,
        },
        {
            "sessionId": "outside",
            "startTime": "2025-10-15T00:00:00Z",
            "endTime": "2025-10-15T00:10:00Z",
            "aggEnergyValue": 1.0,
            "activeChargeTime": 600,
        },
        {
            "sessionId": "good",
            "startTime": "2025-10-16T05:00:00Z",
            "endTime": None,
            "aggEnergyValue": 1.0,
            "activeChargeTime": 1200,
            "costCalculated": True,
            "manualOverridden": False,
            "milesAdded": "12.5",
            "sessionCost": "3.5",
            "avgCostPerUnitEnergy": "0.2",
            "sessionCostState": "estimated",
            "chargeProfileStackLevel": "2",
        },
    ]

    sessions = manager._normalise_sessions_for_day(local_dt=local_dt, results=entries)
    assert any(entry["session_id"] == "good" for entry in sessions)


def test_session_history_normalise_adjusts_windows(hass) -> None:
    """Ensure window adjustments handle inverted timestamps and range clipping."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    local_dt = datetime(2025, 10, 16, 0, 0, 0, tzinfo=timezone.utc)
    entries = [
        {
            "sessionId": "inverted",
            "startTime": "2025-10-16T05:00:00Z",
            "endTime": "2025-10-16T04:00:00Z",
            "aggEnergyValue": 1.0,
            "activeChargeTime": 0,
        },
        {
            "sessionId": "overlap",
            "startTime": "2025-10-15T23:30:00Z",
            "endTime": "2025-10-16T01:00:00Z",
            "aggEnergyValue": 2.0,
            "activeChargeTime": 1800,
        },
    ]
    sessions = manager._normalise_sessions_for_day(local_dt=local_dt, results=entries)
    assert len(sessions) == 2


def test_session_history_sum_energy_handles_invalid_entries(hass) -> None:
    """sum_energy should ignore entries that cannot be coerced to floats."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )

    class BadNumber:
        def __float__(self):
            raise ValueError("boom")

    total = manager._sum_session_energy(
        [{"energy_kwh": 1.5}, {"energy_kwh": BadNumber()}]
    )
    assert total == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_session_history_normalise_various_paths(hass) -> None:
    """Exercise the different normalization branches."""
    await hass.config.async_set_time_zone("UTC")
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    local_dt = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    entries = [
        {
            "sessionId": "cross-midnight",
            "startTime": "2025-10-15T23:30:00Z[UTC]",
            "endTime": "2025-10-16T01:30:00Z[UTC]",
            "aggEnergyValue": 6.0,
            "activeChargeTime": 7200,
        },
        {
            "sessionId": "timestamp",
            "startTime": None,
            "endTime": (datetime(2025, 10, 16, 4, 0, 0, tzinfo=timezone.utc)).timestamp(),
            "aggEnergyValue": 3.0,
            "activeChargeTime": 0,
        },
        {
            "sessionId": "skip",
            "startTime": None,
            "endTime": None,
        },
    ]
    sessions = manager._normalise_sessions_for_day(local_dt=local_dt, results=entries)
    assert len(sessions) == 2
    assert sessions[0]["session_id"] == "cross-midnight"
    assert sessions[1]["session_id"] == "timestamp"


def test_session_history_sum_energy_helper(hass) -> None:
    """sum_energy wrapper should aggregate numeric values."""
    manager = SessionHistoryManager(
        hass,
        lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
        publish_callback=lambda _: None,
    )
    total = manager.sum_energy(
        [{"energy_kwh": 1.5}, {"energy_kwh": "2.5"}, {"energy_kwh": None}]
    )
    assert total == pytest.approx(1.5, abs=1e-6)
