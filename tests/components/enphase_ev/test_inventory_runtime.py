from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def test_inventory_runtime_helper_paths(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert runtime._router_record_key("bad") is None  # noqa: SLF001
    assert runtime._router_record_key({"key": None}) is None  # noqa: SLF001
    assert runtime._router_record_key({"key": BadStr()}) is None  # noqa: SLF001
    assert runtime._router_record_key({"key": " router "}) == "router"  # noqa: SLF001

    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {"type_key": "envoy", "count": 1}
    }
    assert runtime._summary_type_bucket_source("envoy") == {  # noqa: SLF001
        "type_key": "envoy",
        "count": 1,
    }

    grouped = {"envoy": {"count": 1}}
    ordered = ["envoy"]
    snapshot = object()
    coord._debug_devices_inventory_summary = MagicMock(return_value={"devices": 1})  # type: ignore[method-assign]  # noqa: SLF001
    coord._debug_hems_inventory_summary = MagicMock(return_value={"hems": 1})  # type: ignore[method-assign]  # noqa: SLF001
    coord._debug_system_dashboard_summary = MagicMock(return_value={"dashboard": 1})  # type: ignore[method-assign]  # noqa: SLF001
    coord._debug_topology_summary = MagicMock(return_value={"topology": 1})  # type: ignore[method-assign]  # noqa: SLF001
    coord._build_system_dashboard_summaries = MagicMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value=({"envoy": {}}, {"tree": 1}, {"index": {}})
    )

    assert runtime._debug_devices_inventory_summary(grouped, ordered) == {
        "devices": 1
    }  # noqa: SLF001
    assert runtime._debug_hems_inventory_summary() == {"hems": 1}  # noqa: SLF001
    assert runtime._debug_system_dashboard_summary({}, {}, {}, {}) == {
        "dashboard": 1
    }  # noqa: SLF001
    assert runtime._debug_topology_summary(snapshot) == {"topology": 1}  # noqa: SLF001
    assert runtime._build_system_dashboard_summaries(None, {}) == (  # noqa: SLF001
        {"envoy": {}},
        {"tree": 1},
        {"index": {}},
    )
    assert runtime._coerce_optional_bool("true") is True  # noqa: SLF001

    router_records = runtime._gateway_iq_energy_router_summary_records(  # noqa: SLF001
        [{"name": "Router"}, {"name": "Router"}]
    )
    assert [record["key"] for record in router_records] == [
        "name_router",
        "name_router_2",
    ]
    assert runtime.system_dashboard_battery_detail("") is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_ensure_dashboard_refreshes_when_empty(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._system_dashboard_type_summaries = {}  # noqa: SLF001
    runtime._system_dashboard_hierarchy_summary = {}  # noqa: SLF001
    refresh = AsyncMock()
    object.__setattr__(runtime, "_async_refresh_system_dashboard", refresh)

    await runtime.async_ensure_system_dashboard_diagnostics()

    refresh.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_devices_inventory_without_refresh_kw(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    calls: list[str] = []

    async def devices_inventory():
        calls.append("devices_inventory")
        return {"ok": True}

    coord.client.devices_inventory = devices_inventory  # type: ignore[method-assign]
    coord._parse_devices_inventory_payload = MagicMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value=(True, {"envoy": {"count": 1}}, ["envoy"])
    )
    runtime._set_type_device_buckets = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    runtime._merge_heatpump_type_bucket = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001

    await runtime._async_refresh_devices_inventory(force=True)  # noqa: SLF001

    assert calls == ["devices_inventory"]
    runtime._set_type_device_buckets.assert_called_once_with(  # noqa: SLF001
        {"envoy": {"count": 1}},
        ["envoy"],
    )
    assert coord._devices_inventory_payload == {"ok": True}  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_hems_devices_uses_coordinator_preflight(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    async def _mark_unsupported(*, force: bool = False) -> None:
        assert force is True
        coord.client._hems_site_supported = False  # noqa: SLF001

    coord._async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=_mark_unsupported
    )
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    runtime._merge_heatpump_type_bucket = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    runtime._debug_log_summary_if_changed = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001

    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001

    coord._async_refresh_hems_support_preflight.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    coord.client.hems_devices.assert_not_awaited()
    runtime._merge_heatpump_type_bucket.assert_called_once_with()  # noqa: SLF001
    assert runtime._hems_inventory_ready is True  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_refreshable_fetcher_falls_back_when_uninspectable(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().inventory_runtime

    class BadSignatureFetcher:
        @property
        def __signature__(self):
            raise ValueError("boom")

        async def __call__(self):
            return {"ok": True}

    assert await runtime._async_call_refreshable_fetcher(  # noqa: SLF001
        BadSignatureFetcher(),
        force=True,
    ) == {"ok": True}


@pytest.mark.asyncio
async def test_coordinator_inventory_runtime_wrapper_delegation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = MagicMock()
    snapshot = object()
    runtime._current_topology_snapshot.return_value = snapshot
    runtime._extract_hems_group_members.return_value = (True, [{"device_uid": "x"}])
    runtime._async_refresh_devices_inventory = AsyncMock()
    runtime._rebuild_inventory_summary_caches = MagicMock()
    coord.inventory_runtime = runtime

    assert coord._router_record_key({"key": "router"}) == "router"  # noqa: SLF001
    assert coord._current_topology_snapshot() is snapshot  # noqa: SLF001
    assert coord._legacy_hems_devices_groups(  # noqa: SLF001
        {"result": [{"type": "hemsDevices", "devices": [{"gateway": [{}]}]}]}
    ) == [{"gateway": [{}]}]
    assert coord._normalize_hems_member(
        {"device-uid": "abc", "serial": "123"}
    ) == {  # noqa: SLF001
        "device-uid": "abc",
        "serial": "123",
        "device_uid": "abc",
        "serial_number": "123",
        "uid": "abc",
    }
    assert coord._extract_hems_group_members([], {"gateway"}) == (  # noqa: SLF001
        True,
        [{"device_uid": "x"}],
    )

    coord._rebuild_inventory_summary_caches()  # noqa: SLF001
    await coord._async_refresh_devices_inventory(force=True)  # noqa: SLF001

    runtime._current_topology_snapshot.assert_called_once_with()
    runtime._extract_hems_group_members.assert_called_once_with([], {"gateway"})
    runtime._rebuild_inventory_summary_caches.assert_called_once_with()
    runtime._async_refresh_devices_inventory.assert_awaited_once_with(force=True)
