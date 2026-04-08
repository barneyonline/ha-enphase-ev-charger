"""Tests for CurrentPowerRuntime."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_clear_resets_fields(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._current_power_consumption_w = 1.0
    coord._current_power_consumption_sample_utc = datetime.now(timezone.utc)
    coord._current_power_consumption_reported_units = "W"
    coord._current_power_consumption_reported_precision = 0
    coord._current_power_consumption_source = "x"

    coord.current_power_runtime.clear()

    assert coord._current_power_consumption_w is None
    assert coord._current_power_consumption_sample_utc is None
    assert coord._current_power_consumption_reported_units is None
    assert coord._current_power_consumption_reported_precision is None
    assert coord._current_power_consumption_source is None


@pytest.mark.asyncio
async def test_async_refresh_no_fetcher(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace()
    coord._current_power_consumption_w = 100.0

    await coord.current_power_runtime.async_refresh()

    assert coord._current_power_consumption_w is None


@pytest.mark.asyncio
async def test_async_refresh_fetcher_raises(coordinator_factory) -> None:
    coord = coordinator_factory()

    async def _boom():
        raise RuntimeError("network")

    coord.client = SimpleNamespace(latest_power=_boom)
    coord._current_power_consumption_w = 100.0

    await coord.current_power_runtime.async_refresh()

    assert coord._current_power_consumption_w is None


@pytest.mark.asyncio
async def test_async_refresh_invalid_payload_shapes(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        latest_power=AsyncMock(side_effect=[None, "x", {}, {"value": "nope"}])
    )

    for _ in range(4):
        await coord.current_power_runtime.async_refresh()
        assert coord._current_power_consumption_w is None


@pytest.mark.asyncio
async def test_async_refresh_non_finite_cleared(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        latest_power=AsyncMock(return_value={"value": float("nan")})
    )

    await coord.current_power_runtime.async_refresh()
    assert coord._current_power_consumption_w is None


@pytest.mark.asyncio
async def test_async_refresh_success_ms_timestamp_and_units(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        latest_power=AsyncMock(
            return_value={
                "value": 42.5,
                "time": 1_700_000_000_000,
                "units": "  W ",
                "precision": "0",
            }
        )
    )

    await coord.current_power_runtime.async_refresh()

    assert coord._current_power_consumption_w == 42.5
    assert coord._current_power_consumption_reported_units == "W"
    assert coord._current_power_consumption_reported_precision == 0
    assert coord._current_power_consumption_source == "app-api:get_latest_power"
    assert coord._current_power_consumption_sample_utc is not None


@pytest.mark.asyncio
async def test_async_refresh_units_str_failure(coordinator_factory) -> None:
    coord = coordinator_factory()

    class _BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    coord.client = SimpleNamespace(
        latest_power=AsyncMock(
            return_value={
                "value": 1.0,
                "units": _BadStr(),
            }
        )
    )

    await coord.current_power_runtime.async_refresh()
    assert coord._current_power_consumption_w == 1.0
    assert coord._current_power_consumption_reported_units is None


@pytest.mark.asyncio
async def test_async_refresh_precision_invalid(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        latest_power=AsyncMock(return_value={"value": 2.0, "precision": object()})
    )

    await coord.current_power_runtime.async_refresh()
    assert coord._current_power_consumption_w == 2.0
    assert coord._current_power_consumption_reported_precision is None


@pytest.mark.asyncio
async def test_async_refresh_sample_time_invalid(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        latest_power=AsyncMock(return_value={"value": 3.0, "time": "not-a-number"})
    )

    await coord.current_power_runtime.async_refresh()
    assert coord._current_power_consumption_w == 3.0
    assert coord._current_power_consumption_sample_utc is None
