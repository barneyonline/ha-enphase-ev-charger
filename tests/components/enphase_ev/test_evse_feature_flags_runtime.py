"""Tests for EvseFeatureFlagsRuntime."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.const import EVSE_FEATURE_FLAGS_CACHE_TTL
from custom_components.enphase_ev.evse_feature_flags_runtime import (
    EvseFeatureFlagsRuntime,
    EvseFeatureFlagsSnapshot,
    evse_feature_flag_debug_summary,
)


def test_evse_feature_flags_cache_ttl_in_const() -> None:
    assert EVSE_FEATURE_FLAGS_CACHE_TTL == 1800.0


def test_evse_feature_flag_debug_summary_from_snapshot() -> None:
    snap = EvseFeatureFlagsSnapshot(
        payload={"meta": {"x": 1}, "error": None},
        site_feature_flags={"a": True},
        charger_feature_flags_by_serial={"S1": {"f": 1}, "S2": "bad"},
        charger_serial_count=2,
    )
    out = evse_feature_flag_debug_summary(snap)
    assert out["charger_count"] == 2
    assert "a" in out["site_flag_keys"]


def test_evse_feature_flags_snapshot_from_coordinator(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._evse_feature_flags_payload = {"meta": {}}  # noqa: SLF001
    coord._evse_site_feature_flags = {"s": 1}  # noqa: SLF001
    coord._evse_feature_flags_by_serial = {"C1": {"f": 2}}  # noqa: SLF001
    snap = EvseFeatureFlagsSnapshot.from_coordinator(coord)
    assert snap.site_feature_flags == {"s": 1}
    assert snap.charger_feature_flags_by_serial["C1"]["f"] == 2
    assert snap.charger_serial_count == 1


def test_debug_feature_flag_summary_skips_non_dict_flags(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._evse_feature_flags_by_serial = {"SN1": "not-a-dict"}  # noqa: SLF001
    coord._evse_feature_flags_payload = {
        "meta": {"a": 1},
        "error": None,
    }  # noqa: SLF001
    coord._evse_site_feature_flags = {"k": True}  # noqa: SLF001

    summary = coord.evse_feature_flags_runtime.debug_feature_flag_summary()
    assert summary["charger_count"] == 1
    assert "k" in summary["site_flag_keys"]


def test_coerce_evse_feature_flags_map_edge_cases() -> None:
    assert EvseFeatureFlagsRuntime.coerce_evse_feature_flags_map([]) == {}

    class _BadKey:
        def __str__(self) -> str:
            raise RuntimeError("bad")

    assert EvseFeatureFlagsRuntime.coerce_evse_feature_flags_map({_BadKey(): 1}) == {}
    assert EvseFeatureFlagsRuntime.coerce_evse_feature_flags_map(
        {"  x  ": 1, "": 2, 3: "y"}
    ) == {"x": 1, "3": "y"}


def test_parse_payload_early_exits(coordinator_factory) -> None:
    coord = coordinator_factory()
    r = coord.evse_feature_flags_runtime
    r.parse_payload([])
    assert coord._evse_site_feature_flags == {}  # noqa: SLF001
    r.parse_payload({"data": []})
    assert coord._evse_site_feature_flags == {}  # noqa: SLF001


def test_parse_payload_skips_bad_keys_and_empty_charger_flags(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class _BadDataKey:
        def __str__(self) -> str:
            raise RuntimeError("bad")

    payload = {
        "data": {
            _BadDataKey(): True,
            "": 1,
            "only_site": True,
            "empty_charger": {},
            "charger1": {"a": 1},
        }
    }
    coord.evse_feature_flags_runtime.parse_payload(payload)
    assert coord._evse_site_feature_flags == {"only_site": True}  # noqa: SLF001
    assert "charger1" in coord._evse_feature_flags_by_serial  # noqa: SLF001
    assert "empty_charger" not in coord._evse_feature_flags_by_serial  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_refresh_respects_cache_ttl(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._evse_feature_flags_cache_until = 1e30  # noqa: SLF001
    calls: list[object] = []

    async def _fetch(**_kwargs):
        calls.append(True)
        return {"data": {}}

    coord.client = SimpleNamespace(evse_feature_flags=_fetch)

    await coord.evse_feature_flags_runtime.async_refresh(force=False)
    assert calls == []


@pytest.mark.asyncio
async def test_async_refresh_no_client_method(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace()
    await coord.evse_feature_flags_runtime.async_refresh(force=True)
    assert coord._evse_feature_flags_payload is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_refresh_fetch_error_sets_backoff(coordinator_factory) -> None:
    coord = coordinator_factory()

    async def _boom(**_kwargs):
        raise RuntimeError("x")

    coord.client = SimpleNamespace(evse_feature_flags=_boom)
    coord._evse_feature_flags_cache_until = None  # noqa: SLF001

    await coord.evse_feature_flags_runtime.async_refresh(force=True)
    assert coord._evse_feature_flags_cache_until is not None  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_refresh_invalid_dict_payload(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        evse_feature_flags=AsyncMock(return_value=["not", "dict"])
    )
    monkeypatch.setattr(coord, "_debug_log_summary_if_changed", lambda *a, **k: None)

    await coord.evse_feature_flags_runtime.async_refresh(force=True)
    assert coord._evse_feature_flags_payload is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_refresh_success(coordinator_factory, monkeypatch) -> None:
    coord = coordinator_factory()
    payload = {
        "data": {
            "site_flag": True,
            "EV1": {"f1": 1},
        }
    }
    coord.client = SimpleNamespace(evse_feature_flags=AsyncMock(return_value=payload))
    monkeypatch.setattr(coord, "_debug_log_summary_if_changed", lambda *a, **k: None)

    await coord.evse_feature_flags_runtime.async_refresh(force=True)

    assert coord._evse_feature_flags_payload is not None  # noqa: SLF001
    assert coord.evse_feature_flags_runtime.feature_flag("f1", "EV1") == 1
    assert coord.evse_feature_flags_runtime.feature_flag("site_flag") is True


def test_feature_flag_empty_key(coordinator_factory) -> None:
    coord = coordinator_factory()
    assert coord.evse_feature_flags_runtime.feature_flag("  ") is None
    assert coord.evse_feature_flags_runtime.feature_flag_enabled("x") is None
