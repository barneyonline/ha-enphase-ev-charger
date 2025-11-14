"""Additional coverage for SessionHistoryManager helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev import session_history as sh_mod

pytest.importorskip("homeassistant")


def _close_task(coro, name=None):
    try:
        coro.close()
    except AttributeError:
        pass
    return None


@pytest.mark.asyncio
async def test_async_enrich_sessions_handles_task_errors(monkeypatch):
    hass = SimpleNamespace(async_create_task=_close_task)
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
    )

    async def fake_gather(*tasks, **kwargs):
        return [RuntimeError("boom")]

    def fake_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(sh_mod.asyncio, "gather", fake_gather)
    monkeypatch.setattr(sh_mod.asyncio, "create_task", fake_task)

    updates = await manager._async_enrich_sessions(["SN"], day_local=datetime.now(timezone.utc))
    assert updates == {}


@pytest.mark.asyncio
async def test_fetch_sessions_respects_block_until(monkeypatch):
    hass = SimpleNamespace(async_create_task=_close_task)
    client = SimpleNamespace(session_history=AsyncMock())
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: client,
        cache_ttl=60,
    )

    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: now)
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    cache_key = ("SN", now.strftime("%Y-%m-%d"))
    current = time.monotonic()
    manager._cache[cache_key] = (current, ["cached"])
    manager._block_until["SN"] = current + 10

    result = await manager._async_fetch_sessions_today("SN", day_local=now)
    assert result == ["cached"]


@pytest.mark.asyncio
async def test_fetch_sessions_handles_paging(monkeypatch):
    hass = SimpleNamespace(async_create_task=_close_task)
    calls = {"count": 0}

    async def fake_history(sn, start_date, end_date, offset, limit):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "data": {
                    "result": [
                        {
                            "startTime": "2025-01-01T00:00:00Z",
                            "endTime": "2025-01-01T01:00:00Z",
                            "aggEnergyValue": 1.0,
                        }
                    ]
                    * limit,
                    "hasMore": True,
                }
            }
        return {
            "data": {
                "result": [],
                "hasMore": False,
            }
        }

    client = SimpleNamespace(session_history=AsyncMock(side_effect=fake_history))
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: client,
        cache_ttl=60,
    )
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: datetime(2025, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    sessions = await manager._async_fetch_sessions_today("SN", day_local=datetime(2025, 1, 1, tzinfo=timezone.utc))
    assert sessions


def test_normalise_sessions_handles_invalid_values(monkeypatch):
    hass = SimpleNamespace(async_create_task=_close_task)
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
    )
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)
    local_dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
    sessions = manager._normalise_sessions_for_day(
        local_dt=local_dt,
        results=[
            {
                "startTime": ["bad"],
                "endTime": "2025-01-01T01:00:00Z",
                "aggEnergyValue": "bad",
                "activeChargeTime": "bad",
                "costCalculated": "Yes",
                "manualOverridden": "no",
            },
            {
                "startTime": "2025-01-01T02:00:00Z",
                "endTime": "2025-01-01T03:00:00Z",
                "aggEnergyValue": 1.0,
                "activeChargeTime": 3600,
                "costCalculated": "True",
            },
        ],
    )
    assert isinstance(sessions, list)


def test_sum_session_energy_skips_invalid_entries():
    hass = SimpleNamespace(async_create_task=_close_task)
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
    )

    class BadFloat(float):
        def __new__(cls, value):
            return super().__new__(cls, value)

        def __float__(self):
            raise ValueError("boom")

    total = manager._sum_session_energy(
        [
            {"energy_kwh": 1.0},
            {"energy_kwh": BadFloat(2.0)},
        ]
    )
    assert total == 1.0
