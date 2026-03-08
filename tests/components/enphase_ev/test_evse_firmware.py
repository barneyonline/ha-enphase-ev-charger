from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.evse_firmware import (
    EvseFirmwareDetailsManager,
    _iso_or_none,
    _mono_to_utc_iso,
    _normalize_details,
    _text,
)


class DummyClient:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0

    async def evse_fw_details(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


class BlockingClient(DummyClient):
    def __init__(self, payload=None) -> None:
        super().__init__(payload=payload)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def evse_fw_details(self):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return self.payload


@pytest.mark.asyncio
async def test_manager_caches_payload() -> None:
    client = DummyClient(
        payload=[
            {
                "serialNumber": "499900000001",
                "currentFwVersion": "25.37.1.13",
                "targetFwVersion": "25.37.1.14",
            }
        ]
    )
    manager = EvseFirmwareDetailsManager(lambda: client, ttl_seconds=600)

    details = await manager.async_get_details()
    assert details == {
        "499900000001": {
            "serialNumber": "499900000001",
            "currentFwVersion": "25.37.1.13",
            "targetFwVersion": "25.37.1.14",
        }
    }
    assert manager.cached_details == details
    assert client.calls == 1

    cached = await manager.async_get_details()
    assert cached == details
    assert client.calls == 1
    status = manager.status_snapshot()
    assert status["last_error"] is None
    assert status["using_stale"] is False
    assert status["cache_expires_utc"] is not None


@pytest.mark.asyncio
async def test_manager_uses_stale_cache_on_error() -> None:
    client = DummyClient(payload=[{"serialNumber": "499900000001"}])
    manager = EvseFirmwareDetailsManager(lambda: client, ttl_seconds=300)
    await manager.async_get_details()
    manager._expires_mono = 0
    client.error = RuntimeError("boom")

    details = await manager.async_get_details()
    assert details == {"499900000001": {"serialNumber": "499900000001"}}
    status = manager.status_snapshot()
    assert status["last_error"] == "boom"
    assert status["using_stale"] is True


@pytest.mark.asyncio
async def test_manager_handles_unavailable_endpoint_without_cache() -> None:
    client = DummyClient(payload=None)
    manager = EvseFirmwareDetailsManager(lambda: client)

    assert await manager.async_get_details() is None
    assert manager.status_snapshot()["last_error"] == "fwDetails endpoint unavailable"


@pytest.mark.asyncio
async def test_manager_handles_missing_client() -> None:
    manager = EvseFirmwareDetailsManager(lambda: None)

    assert await manager.async_get_details() is None
    assert manager.status_snapshot()["last_error"] == "client unavailable"


@pytest.mark.asyncio
async def test_manager_returns_cached_details_after_waiting_for_lock() -> None:
    client = BlockingClient(payload=[{"serialNumber": "499900000001"}])
    manager = EvseFirmwareDetailsManager(lambda: client, ttl_seconds=300)

    first = asyncio.create_task(manager.async_get_details())
    await client.started.wait()
    second = asyncio.create_task(manager.async_get_details())
    client.release.set()

    assert await first == {"499900000001": {"serialNumber": "499900000001"}}
    assert await second == {"499900000001": {"serialNumber": "499900000001"}}
    assert client.calls == 1


def test_normalize_details_and_helpers_cover_edge_cases() -> None:
    payload = [
        {"serialNumber": "499900000001", "targetFwVersion": "1.0"},
        {"serialNumber": ""},
        "bad",
    ]
    assert _normalize_details(payload) == {
        "499900000001": {
            "serialNumber": "499900000001",
            "targetFwVersion": "1.0",
        }
    }

    try:
        _normalize_details({"serialNumber": "bad"})  # type: ignore[arg-type]
    except ValueError as err:
        assert str(err) == "fwDetails payload must be a list"
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected ValueError")

    assert _iso_or_none(datetime(2026, 3, 1, tzinfo=dt_util.UTC)) == (
        "2026-03-01T00:00:00+00:00"
    )
    assert _iso_or_none(None) is None
    assert _mono_to_utc_iso(0) is None
    assert _mono_to_utc_iso(-1) is None
    assert _mono_to_utc_iso(SimpleNamespace()) is None  # type: ignore[arg-type]
    assert _text(" value ") == "value"
    assert _text(None) is None

    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert _text(_BadStr()) is None

    class _BadDatetime:
        def isoformat(self) -> str:
            raise ValueError("boom")

    assert _iso_or_none(_BadDatetime()) is None  # type: ignore[arg-type]
