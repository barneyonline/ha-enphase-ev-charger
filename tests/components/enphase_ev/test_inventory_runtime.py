from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.enphase_ev import inventory_runtime as inventory_runtime_mod
from custom_components.enphase_ev.inventory_runtime import (
    HEMS_DEVICES_STALE_AFTER_S,
    InventoryRuntime,
)


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
    snapshot = runtime.topology_snapshot()

    assert runtime._debug_devices_inventory_summary(
        grouped, ordered
    ) == {  # noqa: SLF001
        "ordered_type_keys": ["envoy"],
        "type_count": 1,
        "types": {"envoy": {"count": 1, "field_keys": []}},
    }
    hems_summary = runtime._debug_hems_inventory_summary()  # noqa: SLF001
    assert hems_summary["site_supported"] is None
    assert hems_summary["router_count"] == 0
    assert runtime._debug_system_dashboard_summary({}, {}, {}, {}) == {  # noqa: SLF001
        "tree_keys": [],
        "hierarchy_total_nodes": 0,
        "hierarchy_counts_by_type": {},
        "types": {},
    }
    assert runtime._debug_topology_summary(snapshot) == {  # noqa: SLF001
        "inventory_ready": False,
        "charger_count": 0,
        "battery_count": 0,
        "inverter_count": 0,
        "active_type_keys": [],
        "gateway_iq_router_count": 0,
        "site_energy_channels": [],
    }
    assert runtime._build_system_dashboard_summaries(None, {}) == (  # noqa: SLF001
        {
            "envoy": {"hierarchy": {"count": 0, "relationships": []}},
            "encharge": {"hierarchy": {"count": 0, "relationships": []}},
            "microinverter": {"hierarchy": {"count": 0, "relationships": []}},
        },
        {"total_nodes": 0, "counts_by_type": {}, "relationships": []},
        {},
    )
    assert runtime._coerce_optional_bool("true") is True  # noqa: SLF001
    assert (
        runtime._normalize_inverter_status("unpaired") == "not_reporting"
    )  # noqa: SLF001
    assert runtime._normalize_inverter_status("pending") == "warning"  # noqa: SLF001

    router_records = runtime._gateway_iq_energy_router_summary_records(  # noqa: SLF001
        [{"name": "Router"}, {"name": "Router"}]
    )
    assert [record["key"] for record in router_records] == [
        "name_router",
        "name_router_2",
    ]
    assert runtime.system_dashboard_battery_detail("") is None  # noqa: SLF001

    runtime.__dict__["_inverter_data"] = {
        "INV-LOCAL": {"serial_number": "INV-LOCAL"}
    }  # noqa: SLF001
    assert runtime._coordinator_backed_attr("_inverter_data") == {  # noqa: SLF001
        "INV-LOCAL": {"serial_number": "INV-LOCAL"}
    }
    runtime.__dict__.pop("_inverter_data", None)
    coord._inverter_data = {
        "INV-FALLBACK": {"serial_number": "INV-FALLBACK"}
    }  # noqa: SLF001
    assert runtime.inverter_data("INV-FALLBACK") == {  # noqa: SLF001
        "serial_number": "INV-FALLBACK"
    }


def test_inventory_runtime_summary_and_inverter_helper_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime
    coord.inventory_view.type_bucket = lambda type_key: {  # type: ignore[assignment]
        "envoy": {
            "count": 3,
            "devices": [
                {
                    "name": "Gateway A",
                    "statusText": "online",
                    "connected": "yes",
                    "model": "IQ Gateway",
                    "envoy_sw_version": "8.2.0",
                    "last_report": "2026-02-15T10:00:00Z",
                },
                {
                    "name": "Gateway B",
                    "status": "offline",
                    "connected": "no",
                },
                {
                    "name": "Gateway C",
                    "status": "mystery",
                    "connected": "maybe",
                },
            ],
        },
        "microinverter": {
            "count": 3,
            "devices": [],
            "status_counts": {"total": 3, "unknown": 1},
        },
    }.get(type_key, {})

    gateway_snapshot = runtime._build_gateway_inventory_summary()  # noqa: SLF001
    assert gateway_snapshot["connected_devices"] == 1
    assert gateway_snapshot["disconnected_devices"] == 1
    assert gateway_snapshot["unknown_connection_devices"] == 1

    micro_snapshot = runtime._build_microinverter_inventory_summary()  # noqa: SLF001
    assert micro_snapshot["connectivity_state"] == "degraded"

    coord.energy._site_energy_meta = {}  # noqa: SLF001
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {"lifetime_query_start_date": "2022-08-10"},
        "INV-B": {"lifetime_query_start_date": "2023-01-01"},
        "INV-C": {"lifetime_query_start_date": "not-a-date"},
    }
    assert runtime._inverter_start_date() == "2022-08-10"  # noqa: SLF001

    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"sku_id": "IQ7A-SKU"}],
            "status_summary": "Normal 1 | Warning 0 | Error 0 | Not Reporting 0",
            "extra_list": ["a", "b"],
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    coord.inventory_view.type_bucket = type(coord.inventory_view).type_bucket.__get__(coord.inventory_view, type(coord.inventory_view))  # type: ignore[method-assign]
    bucket = coord.inventory_view.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["extra_list"] == ["a", "b"]

    info = coord.inventory_view.type_device_info("microinverter")
    assert info is not None
    assert info["hw_version"] == "IQ7A-SKU"
    assert info.get("model_id") is None
    assert coord.inventory_view.type_device_model_id("microinverter") is None
    assert coord.inventory_view.type_device_model(None) is None
    assert coord.inventory_view.type_device_serial_number(None) is None
    assert coord.inventory_view.type_device_model_id(None) is None
    assert coord.inventory_view.type_device_sw_version(None) is None
    assert coord.inventory_view.type_device_hw_version(None) is None
    coord._type_device_buckets = {"microinverter": "bad"}  # noqa: SLF001
    assert coord.inventory_view.type_device_hw_version("microinverter") is None

    coord.inventory_view.type_bucket = lambda _key: {"devices": "bad"}  # type: ignore[assignment]
    assert coord.inventory_view._type_bucket_members("envoy") == []  # noqa: SLF001
    coord.inventory_view.type_bucket = type(coord.inventory_view).type_bucket.__get__(coord.inventory_view, type(coord.inventory_view))  # type: ignore[method-assign]

    class BadText:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert (
        coord.inventory_view._type_member_text({"name": BadText()}, "name") is None
    )  # noqa: SLF001
    assert (
        coord.inventory_view._type_summary_from_values(
            [None, BadText(), "  ", "A", "A"]
        )  # noqa: SLF001
        == "A x2"
    )

    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"sku_id": "IQ8M"}],
            "firmware_summary": "4.0 x1",
        }
    }
    assert coord.inventory_view.type_device_sw_version("microinverter") is None
    coord._type_device_buckets = {"encharge": "bad"}  # noqa: SLF001
    assert coord.inventory_view.type_device_hw_version("encharge") is None
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "type_key": "envoy",
            "type_label": "Gateway",
            "count": 1,
            "devices": [{"serial_number": "GW-1"}],
            "status_summary": "Normal 1 | Warning 0 | Error 0 | Not Reporting 0",
        }
    }
    assert coord.inventory_view.type_device_hw_version("envoy") is None

    coord._inverter_data = None  # type: ignore[assignment]  # noqa: SLF001
    assert coord.iter_inverter_serials() == []
    assert coord.inverter_data("INV-A") is None

    class BadSerial:
        def __str__(self) -> str:
            raise ValueError("bad")

    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    assert coord.inverter_data(BadSerial()) is None
    assert coord.inverter_data("") is None


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_preserves_previous_lifetime_on_regression(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {
            "serial_number": "INV-A",
            "inverter_id": "1001",
            "device_id": 11,
            "status_code": "normal",
            "show_sig_str": False,
            "emu_version": "8.3.5232",
            "issi": {"sig_str": 1},
            "rssi": {"sig_str": 2},
            "lifetime_production_wh": 2_000_000.0,
            "lifetime_query_start_date": "2022-08-10",
            "lifetime_query_end_date": "2026-02-09",
        }
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "not_reporting": 0,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "inverters": [
                {
                    "name": "IQ7A",
                    "array_name": "North",
                    "serial_number": "INV-A",
                    "status": "normal",
                    "statusText": "Normal",
                }
            ],
        }
    )
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": 1_000_000}}
    )

    await runtime._async_refresh_inverters()  # noqa: SLF001

    payload = coord.inverter_data("INV-A")
    assert payload is not None
    assert payload["inverter_id"] == "1001"
    assert payload["device_id"] == 11
    assert payload["lifetime_production_wh"] == 2_000_000.0
    assert payload["lifetime_query_start_date"] == "2022-08-10"
    assert payload["lifetime_query_end_date"] == "2026-02-09"


def test_inventory_runtime_summary_helpers_reuse_stable_cache_markers(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
            },
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 1,
                "devices": [{"serial_number": "INV-1", "name": "Inverter"}],
            },
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"serial_number": "HP-1", "name": "Heat Pump"}],
            },
        },
        ["envoy", "microinverter", "heatpump"],
    )
    coord._system_dashboard_devices_details_raw = {  # noqa: SLF001
        "envoy": {"envoy": {"status": "normal"}}
    }
    coord._hems_devices_payload = {"result": {"devices": []}}  # noqa: SLF001

    gateway_builder = MagicMock(return_value={"gateway": 1})
    micro_builder = MagicMock(return_value={"micro": 1})
    heatpump_builder = MagicMock(return_value={"heatpump": 1})
    heatpump_type_builder = MagicMock(return_value={"HEAT_PUMP": {"count": 1}})

    monkeypatch.setattr(runtime, "_build_gateway_inventory_summary", gateway_builder)
    monkeypatch.setattr(
        runtime, "_build_microinverter_inventory_summary", micro_builder
    )
    monkeypatch.setattr(runtime, "_build_heatpump_inventory_summary", heatpump_builder)
    monkeypatch.setattr(
        runtime, "_build_heatpump_type_summaries", heatpump_type_builder
    )

    assert coord.gateway_inventory_summary() == {"gateway": 1}
    assert coord.gateway_inventory_summary() == {"gateway": 1}
    assert coord.microinverter_inventory_summary() == {"micro": 1}
    assert coord.microinverter_inventory_summary() == {"micro": 1}
    assert coord.heatpump_inventory_summary() == {"heatpump": 1}
    assert coord.heatpump_inventory_summary() == {"heatpump": 1}
    assert coord.heatpump_type_summary("heat_pump") == {"count": 1}
    assert coord.heatpump_type_summary("heat_pump") == {"count": 1}

    assert gateway_builder.call_count == 1
    assert micro_builder.call_count == 1
    assert heatpump_builder.call_count == 1
    assert heatpump_type_builder.call_count == 1


def test_inventory_runtime_debug_cache_and_gateway_fallback_edges(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime

    class BadText:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert runtime._debug_sorted_keys({BadText(): 1, " ok ": 2}) == [
        "ok"
    ]  # noqa: SLF001
    assert runtime._debug_field_keys([{"a": 1}, "bad"]) == ["a"]  # noqa: SLF001
    rendered = runtime._debug_render_summary({"bad": object()})  # noqa: SLF001
    assert isinstance(rendered, str)
    assert "'bad':" in rendered

    monkeypatch.setattr(
        runtime,
        "_hems_grouped_devices",
        lambda: ["bad", {BadText(): []}, {"router": []}],
    )
    monkeypatch.setattr(runtime, "_hems_group_members", lambda *_args: [])
    hems_summary = runtime._debug_hems_inventory_summary()  # noqa: SLF001
    assert hems_summary["router_count"] == 0

    coord.energy = None
    assert coord.discovery_snapshot.live_site_energy_channels() == set()
    coord.energy = SimpleNamespace(
        site_energy={BadText(): 1, " grid_import ": 1},
        site_energy_meta={
            "bucket_lengths": {
                BadText(): 2,
                "": 1,
                "heatpump": "2",
                "evse": 0,
                "ignored": [],
                "battery_discharge": "bad",
                "consumption": False,
            }
        },
    )
    assert coord.discovery_snapshot.live_site_energy_channels() == {
        "battery_discharge",
        "grid_import",
        "heat_pump",
    }

    monkeypatch.setattr(runtime, "_gateway_inventory_summary_marker", lambda: "gw")
    monkeypatch.setattr(
        runtime, "_microinverter_inventory_summary_marker", lambda: "mi"
    )
    monkeypatch.setattr(runtime, "_heatpump_inventory_summary_marker", lambda: "hp")
    monkeypatch.setattr(
        runtime, "_gateway_iq_energy_router_records_marker", lambda: "router"
    )
    gateway_builder = MagicMock(return_value={"gateway": 1})
    micro_builder = MagicMock(return_value={"micro": 1})
    heatpump_builder = MagicMock(return_value={"heatpump": 1})
    heatpump_type_builder = MagicMock(return_value={"HEAT_PUMP": {"count": 1}})
    router_builder = MagicMock(return_value=[{"key": "router-1", "name": "Router"}])
    monkeypatch.setattr(runtime, "_build_gateway_inventory_summary", gateway_builder)
    monkeypatch.setattr(
        runtime, "_build_microinverter_inventory_summary", micro_builder
    )
    monkeypatch.setattr(runtime, "_build_heatpump_inventory_summary", heatpump_builder)
    monkeypatch.setattr(
        runtime, "_build_heatpump_type_summaries", heatpump_type_builder
    )
    monkeypatch.setattr(
        runtime, "_gateway_iq_energy_router_summary_records", router_builder
    )
    monkeypatch.setattr(
        runtime,
        "gateway_iq_energy_router_records",
        lambda: [{"serial_number": "router-1"}],
    )

    assert runtime.gateway_inventory_summary() == {"gateway": 1}
    assert runtime.gateway_inventory_summary() == {"gateway": 1}
    assert runtime.microinverter_inventory_summary() == {"micro": 1}
    assert runtime.microinverter_inventory_summary() == {"micro": 1}
    assert runtime.heatpump_inventory_summary() == {"heatpump": 1}
    assert runtime.heatpump_inventory_summary() == {"heatpump": 1}
    assert runtime.heatpump_type_summary(BadText()) == {}
    assert runtime.heatpump_type_summary("heat_pump") == {"count": 1}
    assert runtime.gateway_iq_energy_router_summary_records() == [
        {"key": "router-1", "name": "Router"}
    ]
    assert runtime.gateway_iq_energy_router_summary_records() == [
        {"key": "router-1", "name": "Router"}
    ]
    assert runtime.gateway_iq_energy_router_record(" router-1 ") == {
        "key": "router-1",
        "name": "Router",
    }
    assert runtime.gateway_iq_energy_router_record(BadText()) is None
    assert runtime.gateway_iq_energy_router_record("   ") is None

    assert gateway_builder.call_count == 1
    assert micro_builder.call_count == 1
    assert heatpump_builder.call_count == 1
    assert heatpump_type_builder.call_count == 1
    assert router_builder.call_count >= 1

    monkeypatch.setattr(
        runtime,
        "_build_gateway_inventory_summary",
        type(runtime)._build_gateway_inventory_summary.__get__(runtime, type(runtime)),
    )

    monkeypatch.setattr(
        runtime,
        "type_bucket",
        lambda _key: {"type_key": "envoy", "count": "bad", "devices": []},
    )
    monkeypatch.setattr(
        runtime,
        "system_dashboard_envoy_detail",
        lambda: {
            "name": "Gateway",
            "serial_number": "GW-1",
            "status": "normal",
            "firmware_version": "8.2.0",
            "last_interval_end_date": "2026-02-15T10:00:00Z",
        },
    )
    gateway_summary = runtime._build_gateway_inventory_summary()  # noqa: SLF001
    assert gateway_summary["firmware_summary"] == "8.2.0 x1"
    assert gateway_summary["latest_reported_device"] == {
        "name": "Gateway",
        "serial_number": "GW-1",
        "status": "normal",
    }

    monkeypatch.setattr(
        runtime,
        "type_bucket",
        lambda _key: {
            "type_key": "envoy",
            "count": 1,
            "devices": [{"name": "Online Gateway", "status": "normal"}],
        },
    )
    monkeypatch.setattr(runtime, "system_dashboard_envoy_detail", lambda: None)
    online_summary = runtime._build_gateway_inventory_summary()  # noqa: SLF001
    assert online_summary["connected_devices"] == 1

    monkeypatch.setattr(
        runtime,
        "type_bucket",
        lambda _key: {
            "type_key": "envoy",
            "count": 1,
            "devices": [{"name": "Offline Gateway", "status": "not_reporting"}],
        },
    )
    monkeypatch.setattr(runtime, "system_dashboard_envoy_detail", lambda: None)
    offline_summary = runtime._build_gateway_inventory_summary()  # noqa: SLF001
    assert offline_summary["disconnected_devices"] == 1


def test_inventory_runtime_debug_summary_helpers_cover_optional_counts(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    device_summary = runtime._debug_devices_inventory_summary(  # noqa: SLF001
        {
            "envoy": {
                "devices": [{"serial": "1"}],
                "count": 0,
                "status_counts": {"online": "2"},
                "device_type_counts": {"gateway": "1"},
            },
            "skip": "bad",
        },
        ["envoy", "skip"],
    )
    assert device_summary["types"]["envoy"]["status_counts"] == {"online": 2}
    assert device_summary["types"]["envoy"]["device_type_counts"] == {"gateway": 1}

    monkeypatch.setattr(
        runtime,
        "_hems_grouped_devices",
        lambda: ["skip", {"router": []}],
    )
    monkeypatch.setattr(runtime, "_hems_group_members", lambda *_args: [])
    monkeypatch.setattr(
        runtime,
        "_build_heatpump_inventory_summary",
        lambda: {"total_devices": 0},
    )
    monkeypatch.setattr(
        runtime,
        "gateway_iq_energy_router_summary_records",
        lambda: [],
    )
    hems_summary = runtime._debug_hems_inventory_summary()  # noqa: SLF001
    assert hems_summary["group_keys"] == ["router"]
    assert hems_summary["router_count"] == 0
    assert runtime._heatpump_member_device_type({"device_type": "heat_pump"}) == (
        "HEAT_PUMP"
    )  # noqa: SLF001
    assert runtime._heatpump_worst_status_text({"not_reporting": 1}) == (
        "Not Reporting"
    )  # noqa: SLF001
    assert (
        runtime._debug_topology_summary(runtime.topology_snapshot())["charger_count"]
        == 0
    )  # noqa: SLF001
    dashboard_summary = runtime._debug_system_dashboard_summary(  # noqa: SLF001
        {},
        {"envoy": {"envoys": {}}},
        {
            "envoy": {
                "hierarchy": {"count": "2"},
                "counts_by_type": {"gateway": "1"},
                "status_counts": {"normal": "3"},
            }
        },
        {"total_nodes": 1, "counts_by_type": {"envoy": 1}},
    )
    assert dashboard_summary["types"]["envoy"]["counts_by_type"] == {"gateway": 1}
    assert dashboard_summary["types"]["envoy"]["status_counts"] == {"normal": 3}


def test_devices_inventory_runtime_parser_shapes_and_buckets(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import inventory_runtime as inv_mod

    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    payload = {
        "result": [
            {
                "type": "wind-turbine",
                "devices": [
                    {"name": "Wind 1", "status": "normal"},
                    {"name": "Retired Wind", "statusText": "Retired"},
                ],
            },
            {
                "type": "encharge",
                "devices": [
                    {"serial_number": "BAT-1", "name": "IQ Battery 5P"},
                    {"serial_number": "BAT-2", "name": "IQ Battery 5P"},
                    {"serial_number": "BAT-3", "name": "   "},
                ],
            },
            {
                "deviceType": "inverters",
                "members": [
                    {"serial_number": "INV-1", "name": "Micro 1"},
                    {"serial_number": "INV-2", "name": "Micro 2"},
                ],
            },
            {
                "device_type": "microinverter",
                "items": [{"serial_number": "INV-3", "name": "Micro 3"}],
            },
            {
                "type": "generator",
                "devices": [{"name": "Generator 1", "status": "RETIRED"}],
            },
        ]
    }

    valid, grouped, ordered = runtime._parse_devices_inventory_payload(
        payload
    )  # noqa: SLF001

    assert valid is True
    assert ordered == ["wind_turbine", "encharge", "microinverter", "generator"]
    runtime._set_type_device_buckets(grouped, ordered)  # noqa: SLF001

    assert coord.inventory_view.iter_type_keys() == [
        "wind_turbine",
        "encharge",
        "microinverter",
    ]
    assert coord.inventory_view.type_device_name("wind-turbine") == "Wind Turbine"
    assert coord.inventory_view.type_bucket("encharge")["count"] == 3
    assert (
        coord.inventory_view.type_bucket("encharge")["model_summary"]
        == "IQ Battery 5P x2"
    )
    assert coord.inventory_view.type_bucket("microinverter")["count"] == 3
    assert coord.inventory_view.has_type("generator") is False

    valid, grouped, ordered = runtime._parse_devices_inventory_payload(
        {
            "result": [
                {"type": "envoy", "devices": [{"serial_number": "GW-1"}]},
                {"type": "meter", "devices": [{"serial_number": "MTR-1"}]},
                {"type": "enpower", "devices": [{"serial_number": "SC-1"}]},
            ]
        }
    )
    assert valid is True
    assert ordered == ["envoy"]
    runtime._set_type_device_buckets(grouped, ordered)  # noqa: SLF001
    assert coord.inventory_view.type_bucket(
        "meter"
    ) == coord.inventory_view.type_bucket("envoy")
    assert coord.inventory_view.type_bucket(
        "enpower"
    ) == coord.inventory_view.type_bucket("envoy")

    class _BadName:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    class _WeirdSanitized:
        def get(self, key, default=None):
            if key == "name":
                return "Weird Battery"
            return default

    def _fake_sanitize(member):
        marker = member.get("name")
        if marker == "WEIRD_NON_DICT":
            return _WeirdSanitized()
        if marker == "WEIRD_BAD_STR":
            return {"name": _BadName()}
        return {"name": "IQ Battery 5P"}

    monkeypatch.setattr(inv_mod, "sanitize_member", _fake_sanitize)
    valid, grouped, _ordered = runtime._parse_devices_inventory_payload(
        {
            "result": [
                {
                    "type": "encharge",
                    "devices": [
                        {"name": "WEIRD_NON_DICT"},
                        {"name": "WEIRD_BAD_STR"},
                        {"name": "IQ Battery 5P"},
                    ],
                }
            ]
        }
    )
    assert valid is True
    assert grouped["encharge"]["model_summary"] == "IQ Battery 5P x1"


def test_devices_inventory_runtime_dry_contact_dedupe_and_helpers(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import inventory_runtime as inv_mod

    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    valid, grouped, ordered = runtime._parse_devices_inventory_payload(
        {
            "result": [
                {
                    "type": "drycontactloads",
                    "devices": [
                        {"serial_number": "DRY-1", "name": "Inventory"},
                        {"serial_number": "DRY-1", "name": "Inventory"},
                        {"channel_type": "NC1", "meta": {"ignored": True}},
                        {"channel_type": "NC1", "meta": {"ignored": True}},
                        {"id": "2"},
                    ],
                }
            ]
        }
    )

    assert valid is True
    assert ordered == ["dry_contact"]
    assert grouped["dry_contact"]["devices"] == [
        {"name": "Inventory", "serial_number": "DRY-1"},
        {"channel_type": "NC1"},
        {"id": "2"},
    ]

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    values = iter(
        [
            {"name": "Inventory"},
            {"name": "Inventory"},
            {"nested": {"ignored": True}},
        ]
    )
    monkeypatch.setattr(inv_mod, "normalize_type_key", lambda _raw: "dry_contact")
    monkeypatch.setattr(inv_mod, "type_display_label", lambda _raw: "Dry Contacts")
    monkeypatch.setattr(inv_mod, "sanitize_member", lambda _member: next(values))

    valid, grouped, ordered = runtime._parse_devices_inventory_payload(
        {"result": [{"type": BadStr(), "devices": [{}, {}, {}]}]}
    )
    assert valid is True
    assert ordered == ["dry_contact"]
    assert grouped["dry_contact"]["devices"] == [
        {"name": "Inventory"},
        {"name": "Inventory"},
        {"nested": {"ignored": True}},
    ]

    assert runtime._parse_devices_inventory_payload("bad") == (
        False,
        {},
        [],
    )  # noqa: SLF001
    assert runtime._parse_devices_inventory_payload({}) == (
        False,
        {},
        [],
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_devices_and_hems_refresh_cache_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    runtime._devices_inventory_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.devices_inventory = AsyncMock(side_effect=AssertionError("no fetch"))
    await runtime._async_refresh_devices_inventory()

    runtime._devices_inventory_cache_until = None  # noqa: SLF001
    coord.client.devices_inventory = AsyncMock(return_value={})
    await runtime._async_refresh_devices_inventory()

    monkeypatch.setattr(
        inventory_runtime_mod, "redact_battery_payload", lambda payload: "raw"
    )
    coord.client.devices_inventory = AsyncMock(
        return_value={
            "result": [{"type": "envoy", "devices": [{"name": "IQ Gateway"}]}]
        }
    )
    await runtime._async_refresh_devices_inventory(force=True)
    assert runtime._devices_inventory_payload == {"value": "raw"}  # noqa: SLF001

    monkeypatch.setattr(
        inventory_runtime_mod, "redact_battery_payload", lambda payload: payload
    )
    await runtime._async_refresh_devices_inventory(force=True)
    assert coord.inventory_view.has_type("envoy") is True

    runtime._devices_inventory_cache_until = None  # noqa: SLF001
    coord.client.devices_inventory = AsyncMock(
        return_value={"result": [{"type": "envoy"}]}
    )
    monkeypatch.setattr(
        runtime,
        "_parse_devices_inventory_payload",
        lambda payload: (
            True,
            {"envoy": {"type_key": "envoy", "count": object(), "devices": [{}]}},
            ["envoy"],
        ),
    )
    await runtime._async_refresh_devices_inventory(force=True)
    assert runtime._devices_inventory_cache_until is not None  # noqa: SLF001

    runtime._hems_devices_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    await runtime._async_refresh_hems_devices()

    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = None
    await runtime._async_refresh_hems_devices()

    coord.client._hems_site_supported = False  # noqa: SLF001
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    await runtime._async_refresh_hems_devices()
    coord.client.hems_devices.assert_not_awaited()
    assert runtime._hems_devices_payload is None  # noqa: SLF001

    coord.client._hems_site_supported = None  # noqa: SLF001
    runtime._hems_support_preflight_cache_until = None  # noqa: SLF001
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value={"is_hems": False})
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    await runtime._async_refresh_hems_devices()
    assert coord.client.hems_site_supported is False

    coord.client._hems_site_supported = None  # noqa: SLF001
    runtime._hems_support_preflight_cache_until = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value=None)
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await runtime._async_refresh_hems_devices()
    assert runtime._hems_devices_payload is None  # noqa: SLF001

    monkeypatch.setattr(
        inventory_runtime_mod, "redact_battery_payload", lambda payload: payload
    )
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(
        return_value={"data": {"hems-devices": {"heat-pump": []}}}
    )
    await runtime._async_refresh_hems_devices(force=True)
    assert runtime._hems_devices_payload == {
        "data": {"hems-devices": {"heat-pump": []}}
    }  # noqa: SLF001

    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client._hems_site_supported = True  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await runtime._async_refresh_hems_devices()
    assert runtime._hems_devices_using_stale is True  # noqa: SLF001

    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=RuntimeError("boom"))
    await runtime._async_refresh_hems_devices(force=True)
    assert runtime._hems_devices_using_stale is True  # noqa: SLF001

    runtime._hems_devices_cache_until = None  # noqa: SLF001
    runtime._hems_devices_last_success_mono = (
        time.monotonic() - HEMS_DEVICES_STALE_AFTER_S - 1
    )  # noqa: SLF001
    coord.client._hems_site_supported = True  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await runtime._async_refresh_hems_devices()
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_devices_using_stale is False  # noqa: SLF001


def test_inventory_runtime_inverter_helper_paths(coordinator_factory) -> None:
    runtime = coordinator_factory().inventory_runtime

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert (
        runtime._format_inverter_model_summary({"": 1, "IQ7A": "x", "IQ8": 0}) is None
    )  # noqa: SLF001
    assert runtime._normalize_inverter_status("normal") == "normal"  # noqa: SLF001
    assert runtime._normalize_inverter_status("recommended") == "normal"  # noqa: SLF001
    assert runtime._normalize_inverter_status("warning") == "warning"  # noqa: SLF001
    assert (
        runtime._normalize_inverter_status("critical error") == "error"
    )  # noqa: SLF001
    assert (
        runtime._normalize_inverter_status("not reporting") == "not_reporting"
    )  # noqa: SLF001
    assert runtime._normalize_inverter_status("mystery") == "unknown"  # noqa: SLF001
    assert runtime._normalize_inverter_status(BadStr()) == "unknown"  # noqa: SLF001
    assert (
        runtime._inverter_connectivity_state({"total": 2, "not_reporting": 0})
        == "online"
    )  # noqa: SLF001
    assert (
        runtime._inverter_connectivity_state({"total": 2, "not_reporting": 1})
        == "degraded"
    )  # noqa: SLF001
    assert (
        runtime._inverter_connectivity_state({"total": 2, "not_reporting": 2})
        == "offline"
    )  # noqa: SLF001
    assert runtime._inverter_connectivity_state({"total": 0}) is None  # noqa: SLF001
    assert runtime._parse_inverter_last_report(None) is None  # noqa: SLF001
    assert runtime._parse_inverter_last_report("   ") is None  # noqa: SLF001
    assert (
        runtime._parse_inverter_last_report("2026-02-09T00:00:00Z") is not None
    )  # noqa: SLF001
    assert (
        runtime._parse_inverter_last_report("2026-02-09T00:00:00Z[UTC]") is not None
    )  # noqa: SLF001
    assert (
        runtime._parse_inverter_last_report(1_780_000_000_000) is not None
    )  # noqa: SLF001
    assert (
        runtime._parse_inverter_last_report(datetime(2026, 2, 9, 0, 0, 0)).tzinfo
        == timezone.utc
    )  # noqa: SLF001
    assert runtime._parse_inverter_last_report(float("inf")) is None  # noqa: SLF001
    assert runtime._parse_inverter_last_report("bad") is None  # noqa: SLF001
    assert runtime._parse_inverter_last_report(BadStr()) is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_paths(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 2,
            "not_reporting": 0,
            "normal_count": 2,
            "warning_count": 0,
            "error_count": 0,
            "inverters": [
                {
                    "name": "IQ7A",
                    "array_name": "North",
                    "serial_number": "INV-A",
                    "status": "normal",
                    "statusText": "Normal",
                    "last_report": 1_780_000_000,
                    "fw1": "520-00082-r01-v04.30.32",
                },
                {
                    "name": "IQ7A",
                    "array_name": "West",
                    "serial_number": "INV-B",
                    "status": "normal",
                    "statusText": "Normal",
                    "last_report": 1_770_000_000,
                    "fw1": "520-00082-r01-v04.30.32",
                },
            ],
            "panel_info": {
                "pv_module_manufacturer": "Acme",
                "model_name": "PV-123",
                "stc_rating": 420,
            },
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": {
                "serialNum": "INV-A",
                "deviceId": 11,
                "statusCode": "normal",
                "type": "IQ7A",
            },
            "1002": {
                "serialNum": "INV-B",
                "deviceId": 12,
                "statusCode": "normal",
                "type": "IQ7A",
            },
        }
    )
    coord.client.inverter_production = AsyncMock(
        return_value={
            "production": {"1001": 1_000_000, "1002": "2_000_000"},
            "start_date": "2022-08-10",
            "end_date": "2026-02-09",
        }
    )

    await runtime._async_refresh_inverters()  # noqa: SLF001

    assert coord.iter_inverter_serials() == ["INV-A", "INV-B"]
    assert coord.inverter_data("INV-A")["inverter_id"] == "1001"
    assert coord.inverter_data("INV-A")["device_id"] == 11
    assert coord.inverter_data("INV-A")["lifetime_production_wh"] == 1_000_000.0
    bucket = coord.inventory_view.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["count"] == 2
    assert bucket["status_counts"]["normal"] == 2
    assert bucket["connectivity_state"] == "online"

    coord._inverter_data = []  # type: ignore[assignment]  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(return_value={"inverters": {"bad": 1}})
    coord.client.inverter_status = AsyncMock(side_effect=RuntimeError("boom"))
    coord.client.inverter_production = AsyncMock(side_effect=RuntimeError("boom"))
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == []

    coord.energy._site_energy_meta = {}  # noqa: SLF001
    coord._inverter_data = {}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 0,
            "inverters": [{"serial_number": "INV-A", "name": "IQ7A"}],
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={"1001": {"serialNum": "INV-A", "deviceId": 11}}
    )
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": 1}}
    )
    await runtime._async_refresh_inverters()  # noqa: SLF001
    coord.client.inverter_production.assert_not_awaited()

    coord.include_inverters = False
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    coord._type_device_buckets = {
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"serial_number": "INV-A"}],
        }
    }  # noqa: SLF001
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == []
    assert coord.inventory_view.type_bucket("microinverter") is None


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
async def test_inventory_runtime_refresh_system_dashboard_populates_summaries_and_accessors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.client.devices_tree = AsyncMock(
        return_value={
            "devices": [
                {
                    "device_uid": "GW-1",
                    "type": "envoy",
                    "name": "Gateway",
                    "children": [
                        {
                            "device_uid": "BAT-1",
                            "type": "encharge",
                            "name": "Battery 1",
                        },
                        {"device_uid": "MTR-1", "type": "meter", "name": "Meter 1"},
                    ],
                }
            ]
        }
    )
    coord.client.devices_details = AsyncMock(
        side_effect=lambda type_key: {
            "envoys": {
                "envoys": [
                    {
                        "device_uid": "GW-1",
                        "type": "envoy",
                        "status": "normal",
                        "last_report": "2026-03-09T05:45:00+00:00",
                        "network": {"mode": "dhcp"},
                    }
                ]
            },
            "meters": {
                "meters": [
                    {
                        "device_uid": "MTR-1",
                        "type": "meter",
                        "name": "Consumption Meter",
                        "meter_type": "consumption",
                        "meter_state": "Enabled",
                    }
                ]
            },
            "encharges": {
                "encharges": [
                    {
                        "device_uid": "BAT-1",
                        "type": "encharge",
                        "serial_number": "BAT-1",
                        "rssi_dbm": -61,
                    }
                ]
            },
            "inverters": {
                "inverters": {
                    "total": 16,
                    "not_reporting": 1,
                    "plc_comm": 5,
                    "items": [{"name": "IQ7A Microinverters", "count": 16}],
                }
            },
        }.get(type_key)
    )

    await runtime._async_refresh_system_dashboard(force=True)  # noqa: SLF001

    diagnostics = runtime.system_dashboard_diagnostics()
    assert diagnostics["hierarchy_summary"]["counts_by_type"]["envoy"] == 2
    assert diagnostics["type_summaries"]["microinverter"]["model_summary"] == (
        "IQ7A Microinverters x16"
    )
    assert runtime.system_dashboard_envoy_detail()["status"] == "normal"
    assert runtime.system_dashboard_meter_detail("consumption")["meter_state"] == (
        "Enabled"
    )
    assert runtime.system_dashboard_battery_detail("BAT-1")["rssi_dbm"] == -61

    coord.client.devices_tree.reset_mock()
    coord.client.devices_details.reset_mock()
    await runtime._async_refresh_system_dashboard()
    coord.client.devices_tree.assert_not_called()
    coord.client.devices_details.assert_not_called()


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_system_dashboard_handles_missing_fetchers_and_invalid_payloads(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.client.devices_tree = None
    coord.client.devices_details = None

    await runtime._async_refresh_system_dashboard(force=True)  # noqa: SLF001

    assert runtime.system_dashboard_diagnostics()["devices_tree_payload"] is None

    monkeypatch.setattr(
        inventory_runtime_mod,
        "SYSTEM_DASHBOARD_DIAGNOSTIC_TYPES",
        ("", "envoy", "envoys"),
    )
    coord.client.devices_tree = AsyncMock(return_value=["bad"])
    coord.client.devices_details = AsyncMock(side_effect=[{}, ["bad"], {}])

    await runtime._async_refresh_system_dashboard(force=True)  # noqa: SLF001

    diagnostics = runtime.system_dashboard_diagnostics()
    assert diagnostics["devices_tree_payload"] is None
    assert diagnostics["devices_details_payloads"]["envoy"] == {"envoys": {}}


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_system_dashboard_handles_fetch_exceptions(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord._system_dashboard_devices_tree_raw = {
        "devices": [{"device_uid": "cached"}]
    }  # noqa: SLF001
    coord._system_dashboard_devices_details_raw = {  # noqa: SLF001
        "envoy": {"envoys": {"envoys": [{"device_uid": "GW-1"}]}}
    }
    monkeypatch.setattr(
        inventory_runtime_mod,
        "SYSTEM_DASHBOARD_DIAGNOSTIC_TYPES",
        ("envoys", "meters"),
    )
    coord.client.devices_tree = AsyncMock(side_effect=RuntimeError("tree"))

    async def _details(source_type: str):
        if source_type == "envoys":
            raise RuntimeError("detail")
        return {"meters": [{"name": "Meter"}]}

    coord.client.devices_details = AsyncMock(side_effect=_details)

    await runtime._async_refresh_system_dashboard(force=True)  # noqa: SLF001

    diagnostics = runtime.system_dashboard_diagnostics()
    assert diagnostics["devices_tree_payload"] == {
        "devices": [{"device_uid": "cached"}]
    }
    assert diagnostics["devices_details_payloads"]["envoy"]["envoys"] == {
        "envoys": [{"device_uid": "GW-1"}]
    }
    assert diagnostics["devices_details_payloads"]["envoy"]["meters"] == {
        "meters": [{"name": "Meter"}]
    }


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
async def test_inventory_runtime_refresh_hems_devices_uses_heatpump_runtime_preflight(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    async def _mark_unsupported(*, force: bool = False) -> None:
        assert force is True
        coord.client._hems_site_supported = False  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=_mark_unsupported
    )
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    runtime._merge_heatpump_type_bucket = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    runtime._debug_log_summary_if_changed = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001

    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight.assert_awaited_once_with(  # noqa: SLF001
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

    assert (
        InventoryRuntime._router_record_key({"key": "router"}) == "router"
    )  # noqa: SLF001
    assert coord._current_topology_snapshot() is snapshot  # noqa: SLF001
    assert InventoryRuntime._legacy_hems_devices_groups(  # noqa: SLF001
        {"result": [{"type": "hemsDevices", "devices": [{"gateway": [{}]}]}]}
    ) == [{"gateway": [{}]}]
    assert InventoryRuntime._normalize_hems_member(
        {"device-uid": "abc", "serial": "123"}
    ) == {  # noqa: SLF001
        "device-uid": "abc",
        "serial": "123",
        "device_uid": "abc",
        "serial_number": "123",
        "uid": "abc",
    }
    assert coord.inventory_runtime._extract_hems_group_members(
        [], {"gateway"}
    ) == (  # noqa: SLF001
        True,
        [{"device_uid": "x"}],
    )

    coord.inventory_runtime._rebuild_inventory_summary_caches()  # noqa: SLF001
    await coord.inventory_runtime._async_refresh_devices_inventory(
        force=True
    )  # noqa: SLF001

    runtime._current_topology_snapshot.assert_called_once_with()
    runtime._extract_hems_group_members.assert_called_once_with([], {"gateway"})
    runtime._rebuild_inventory_summary_caches.assert_called_once_with()
    runtime._async_refresh_devices_inventory.assert_awaited_once_with(force=True)


def test_inventory_runtime_rebuild_summary_caches_updates_coordinator_caches(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime

    coord._gateway_inventory_summary_cache = {"stale": True}  # noqa: SLF001
    coord._microinverter_inventory_summary_cache = {"stale": True}  # noqa: SLF001
    coord._heatpump_inventory_summary_cache = {"stale": True}  # noqa: SLF001
    coord._heatpump_type_summaries_cache = {
        "HEAT_PUMP": {"stale": True}
    }  # noqa: SLF001
    coord._gateway_iq_energy_router_records_cache = [{"key": "stale"}]  # noqa: SLF001

    monkeypatch.setattr(runtime, "_gateway_inventory_summary_marker", lambda: "gw")
    monkeypatch.setattr(
        runtime, "_microinverter_inventory_summary_marker", lambda: "micro"
    )
    monkeypatch.setattr(runtime, "_heatpump_inventory_summary_marker", lambda: "heat")
    monkeypatch.setattr(
        runtime, "_gateway_iq_energy_router_records_marker", lambda: "router"
    )
    monkeypatch.setattr(
        runtime, "_build_gateway_inventory_summary", lambda: {"gateway": 1}
    )
    monkeypatch.setattr(
        runtime, "_build_microinverter_inventory_summary", lambda: {"micro": 2}
    )
    monkeypatch.setattr(
        runtime, "_build_heatpump_inventory_summary", lambda: {"heatpump": 3}
    )
    monkeypatch.setattr(
        runtime,
        "_build_heatpump_type_summaries",
        lambda: {"HEAT_PUMP": {"count": 4}},
    )
    monkeypatch.setattr(
        runtime,
        "_gateway_iq_energy_router_summary_records",
        lambda _records: [{"key": "router-1", "name": "Router"}],
    )
    monkeypatch.setattr(runtime, "gateway_iq_energy_router_records", lambda: [])

    coord.inventory_runtime._rebuild_inventory_summary_caches()  # noqa: SLF001

    assert coord.gateway_inventory_summary() == {"gateway": 1}
    assert coord.microinverter_inventory_summary() == {"micro": 2}
    assert coord.heatpump_inventory_summary() == {"heatpump": 3}
    assert coord.heatpump_type_summary("heat_pump") == {"count": 4}
    assert coord.inventory_view.gateway_iq_energy_router_summary_records() == [
        {"key": "router-1", "name": "Router"}
    ]


def test_inventory_runtime_parser_and_hems_edge_cases(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.inventory_runtime import InventoryRuntime
    from custom_components.enphase_ev import inventory_runtime as inv_mod

    runtime = coordinator_factory().inventory_runtime

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    class EmptyNormalized(dict):
        def __bool__(self) -> bool:
            return False

    buckets = runtime._devices_inventory_buckets(  # noqa: SLF001
        {"value": {"result": [{"type": "envoy"}, "bad"]}}
    )
    assert buckets == [{"type": "envoy"}]

    assert runtime._hems_devices_groups(
        {"result": {"devices": [{"a": 1}, "bad"]}}
    ) == [  # noqa: SLF001
        {"a": 1}
    ]
    assert runtime._hems_devices_groups(
        {"result": {"devices": {"gateway": []}}}
    ) == [  # noqa: SLF001
        {"gateway": []}
    ]
    assert runtime._hems_devices_groups({"data": []}) == []  # noqa: SLF001

    assert (
        runtime._legacy_hems_devices_groups(  # noqa: SLF001
            {"result": [{"type": "hemsDevices", "devices": {"bad": True}}]}
        )
        == []
    )
    assert runtime._normalize_heatpump_member({"deviceUid": "abc"}) == {  # noqa: SLF001
        "deviceUid": "abc",
        "device_uid": "abc",
        "uid": "abc",
    }

    groups = [
        {
            "gateway": [
                "bad",
                {"name": "skip-retired"},
                {"device-uid": "UID-1", "name": "first"},
                {"device-uid": "UID-1", "name": "duplicate"},
                {"name": "empty-normalized"},
                {"serial": "SER-2", "name": "second"},
            ]
        }
    ]

    monkeypatch.setattr(
        runtime,
        "member_is_retired",
        lambda member: isinstance(member, dict)
        and member.get("name") == "skip-retired",
    )
    monkeypatch.setattr(
        runtime,
        "_normalize_hems_member",
        lambda member: (
            EmptyNormalized()
            if member.get("name") == "empty-normalized"
            else InventoryRuntime._normalize_hems_member(member)
        ),
    )

    found, members = runtime._extract_hems_group_members(
        groups, {"gateway"}
    )  # noqa: SLF001
    assert found is True
    assert members == [
        {"device-uid": "UID-1", "name": "first", "device_uid": "UID-1", "uid": "UID-1"},
        {"serial": "SER-2", "name": "second", "serial_number": "SER-2"},
    ]

    assert runtime._hems_bucket_type("") is None  # noqa: SLF001
    assert runtime._hems_bucket_type("heat-pump") == "heatpump"  # noqa: SLF001
    assert runtime._hems_bucket_type(BadStr()) is None  # noqa: SLF001
    original_hems_normalize_type_key = inv_mod.normalize_type_key
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.normalize_type_key",
        lambda raw: (
            None if raw == "raw hems type" else original_hems_normalize_type_key(raw)
        ),
    )
    assert runtime._hems_bucket_type("raw hems type") == "rawhemstype"  # noqa: SLF001
    assert (
        runtime._heatpump_worst_status_text({"warning": 1}) == "Warning"
    )  # noqa: SLF001
    assert (
        runtime._heatpump_worst_status_text({"not_reporting": 1}) == "Not Reporting"
    )  # noqa: SLF001
    assert runtime._heatpump_worst_status_text({"error": 1}) == "Error"  # noqa: SLF001

    values = iter(
        [
            {"name": "Dry Contact 1", "rating": 10},
            {"name": "Dry Contact 2", "enabled": True},
            {"meta": {"ignored": True}},
        ]
    )
    original_normalize_type_key = inv_mod.normalize_type_key
    original_sanitize_member = inv_mod.sanitize_member
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.normalize_type_key",
        lambda raw: (
            "dry_contact"
            if isinstance(raw, BadStr)
            else original_normalize_type_key(raw)
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.type_display_label",
        lambda _raw: "Dry Contact",
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.sanitize_member",
        lambda _member: next(values, {"name": None}),
    )

    valid, grouped, ordered = runtime._parse_devices_inventory_payload(  # noqa: SLF001
        [
            "bad",
            {"type": BadStr(), "devices": [{}, {}, {}]},
            {"type": "envoy", "devices": "bad"},
            {"type": "envoy", "devices": [None, {}, {"name": None}]},
        ]
    )
    assert valid is True
    assert ordered == ["dry_contact", "envoy"]
    assert grouped["dry_contact"]["devices"] == [
        {"name": "Dry Contact 1", "rating": 10},
        {"name": "Dry Contact 2", "enabled": True},
        {"meta": {"ignored": True}},
    ]
    assert grouped["envoy"]["devices"] == [{"name": None}, {"name": None}]

    with monkeypatch.context() as nested:
        nested.setattr(
            "custom_components.enphase_ev.inventory_runtime.normalize_type_key",
            original_hems_normalize_type_key,
        )
        nested.setattr(
            "custom_components.enphase_ev.inventory_runtime.sanitize_member",
            original_sanitize_member,
        )
        assert (
            runtime._parse_devices_inventory_payload(  # noqa: SLF001
                {
                    "result": [
                        {
                            "type": "drycontactloads",
                            "devices": [{"name": "x"}, {"name": "y"}],
                        },
                        {
                            "type": "envoy",
                            "devices": [{"name": "ignored"}],
                        },
                        {
                            "type": "encharge",
                            "devices": [
                                {"serial_number": "BAT-1"},
                                {"serial_number": "BAT-2", "name": "IQ Battery 5P"},
                            ],
                        },
                    ]
                }
            )[1]["encharge"]["model_summary"]
            == "IQ Battery 5P x1"
        )

    with monkeypatch.context() as nested:
        dry_values = iter(
            [
                {"rating": 10},
                {"meta": {"ignored": True}},
                {},
                {"serial_number": "ENV-1"},
            ]
        )
        nested.setattr(
            "custom_components.enphase_ev.inventory_runtime.sanitize_member",
            lambda _member: next(dry_values),
        )
        valid, grouped, _ordered = (
            runtime._parse_devices_inventory_payload(  # noqa: SLF001
                {
                    "result": [
                        {
                            "type": "drycontactloads",
                            "devices": [{}, {}, {}],
                        },
                        {"type": "envoy", "devices": [{}]},
                    ]
                }
            )
        )
        assert valid is True
        assert grouped["dry_contact"]["devices"] == [
            {"rating": 10},
            {"meta": {"ignored": True}},
        ]
        assert grouped["envoy"]["devices"] == [{"serial_number": "ENV-1"}]


def test_inventory_runtime_merge_heatpump_bucket_uses_worst_status_fallback(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord._type_device_buckets = {  # noqa: SLF001
        "iqevse": {
            "type_key": "iqevse",
            "type_label": "EV Chargers",
            "count": 1,
            "devices": [{"serial_number": "EV-1"}],
        }
    }
    coord._type_device_order = ["iqevse"]  # noqa: SLF001
    runtime._devices_inventory_ready = False  # noqa: SLF001
    runtime._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "heat-pump": [
                    {
                        "deviceType": "furnace",
                        "device-uid": "HP-1",
                        "name": "Aux Heat",
                        "statusText": "warning",
                        "firmware-version": "1.2.3",
                        "part-number": "PN-1",
                        "lastReportedAt": "2026-02-09T00:00:00Z",
                    }
                ]
            }
        }
    }
    refresh_calls: list[str] = []
    runtime._refresh_cached_topology = lambda: refresh_calls.append("refresh") or True  # type: ignore[method-assign]  # noqa: SLF001

    runtime._merge_heatpump_type_bucket()  # noqa: SLF001

    bucket = coord.inventory_view.type_bucket("heatpump")
    assert bucket is not None
    assert coord.inventory_view.iter_type_keys() == ["iqevse", "heatpump"]
    assert bucket["overall_status_text"] == "Warning"
    assert bucket["latest_reported_device"] == {
        "device_type": "FURNACE",
        "device_uid": "HP-1",
        "name": "Aux Heat",
        "status": "warning",
    }
    assert bucket["model_summary"] == "PN-1 x1"
    assert bucket["firmware_summary"] == "1.2.3 x1"


def test_inventory_runtime_system_dashboard_and_microinverter_edge_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime

    class BadText:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    coord._inverter_data = {  # noqa: SLF001
        "INV-BAD": "bad-payload",
        "INV-OK": {
            "name": "Inverter",
            "serial_number": "INV-OK",
            "status": "normal",
            "last_report": "2026-02-15T10:00:00Z",
            "array_name": BadText(),
            "fw1": BadText(),
        },
    }
    coord._inverter_order = ["INV-BAD", "INV-OK"]  # noqa: SLF001
    runtime._inverter_summary_counts = {  # noqa: SLF001
        "total": 1,
        "normal": 1,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
        "unknown": 0,
    }
    runtime._inverter_model_counts = {"IQ8": 1}  # noqa: SLF001

    runtime._merge_microinverter_type_bucket()  # noqa: SLF001

    bucket = coord.inventory_view.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["count"] == 1
    assert bucket["array_summary"] is None
    assert bucket["firmware_summary"] is None

    runtime._system_dashboard_devices_details_raw = []  # type: ignore[assignment]  # noqa: SLF001
    assert runtime._system_dashboard_raw_payloads("envoy") == {}  # noqa: SLF001

    runtime._system_dashboard_devices_details_raw = {  # noqa: SLF001
        "encharge": {
            "encharges": {
                "encharges": [
                    {"serial_number": "OTHER", "id": "OTHER", "rssi_dbm": -70},
                    {"serial_number": "BAT-1", "id": "BAT-1", "rssi_dbm": -61},
                ]
            }
        }
    }
    runtime._battery_storage_data = {  # noqa: SLF001
        "BAT-1": {"serial_number": "BAT-1", "identity": "BAT-1"}
    }
    assert runtime.system_dashboard_battery_detail("BAT-1") == {"rssi_dbm": -61}


def test_merge_heatpump_type_bucket_preserves_fault_over_lifecycle_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "heat-pump": [
                    {
                        "device-type": "HEAT_PUMP",
                        "device-uid": "HP-1",
                        "name": "Primary Heat Pump",
                        "statusText": "Fault",
                        "pairing-status": "UNPAIRED",
                        "device-state": "INACTIVE",
                    }
                ]
            }
        }
    }

    runtime._merge_heatpump_type_bucket()  # noqa: SLF001

    bucket = coord.inventory_view.type_bucket("heatpump")
    assert bucket is not None
    assert bucket["overall_status_text"] == "Fault"
    assert bucket["status_counts"] == {
        "total": 1,
        "normal": 0,
        "warning": 0,
        "error": 1,
        "not_reporting": 0,
        "unknown": 0,
    }
    assert bucket["latest_reported_device"] is None


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_devices_inventory_logs_empty_grouped_summary(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.client.devices_inventory = AsyncMock(return_value={"result": []})
    runtime._debug_devices_inventory_summary = MagicMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value={"devices": 0}
    )
    runtime._debug_log_summary_if_changed = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001

    await runtime._async_refresh_devices_inventory(force=True)  # noqa: SLF001

    runtime._debug_log_summary_if_changed.assert_called_once_with(  # noqa: SLF001
        "devices_inventory",
        "Device inventory discovery summary",
        {"devices": 0},
    )


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_hems_devices_unsupported_and_redaction_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._merge_heatpump_type_bucket = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    runtime._debug_log_summary_if_changed = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    runtime._debug_hems_inventory_summary = MagicMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value={"hems": 1}
    )

    coord.client._hems_site_supported = True  # noqa: SLF001
    runtime._hems_devices_payload = {"existing": True}  # noqa: SLF001
    runtime._hems_devices_last_success_mono = time.monotonic()  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=RuntimeError("boom"))
    original_preflight = coord.heatpump_runtime.async_refresh_hems_support_preflight

    async def unsupported_on_error(*, force: bool = False) -> None:
        await original_preflight(force=force)
        coord.client._hems_site_supported = True  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=unsupported_on_error
    )
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload == {"existing": True}  # noqa: SLF001
    assert runtime._hems_devices_using_stale is True  # noqa: SLF001

    async def unsupported_preflight(*, force: bool = False) -> None:
        coord.client._hems_site_supported = False  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=unsupported_preflight
    )
    coord.client.hems_devices = AsyncMock(side_effect=RuntimeError("unused"))
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_inventory_ready is True  # noqa: SLF001

    async def supported_preflight(*, force: bool = False) -> None:
        coord.client._hems_site_supported = True  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=supported_preflight
    )
    runtime._hems_devices_payload = {"keep": True}  # noqa: SLF001
    runtime._hems_devices_last_success_mono = (
        time.monotonic() - HEMS_DEVICES_STALE_AFTER_S - 1
    )  # noqa: SLF001
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value="bad")
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_inventory_ready is False  # noqa: SLF001

    async def unsupported_invalid_payload(*, force: bool = False) -> None:
        coord.client._hems_site_supported = False  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=unsupported_invalid_payload
    )
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value="still bad")
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_inventory_ready is True  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=supported_preflight
    )
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    monkeypatch.setattr(
        inventory_runtime_mod,
        "redact_battery_payload",
        lambda payload: "wrapped",
    )
    coord.client.hems_devices = AsyncMock(return_value={"result": {"devices": []}})
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload == {"value": "wrapped"}  # noqa: SLF001

    async def unsupported_during_fetch(*, force: bool = False) -> None:
        coord.client._hems_site_supported = True  # noqa: SLF001

    async def fetch_marks_unsupported(*, refresh_data: bool = False):
        coord.client._hems_site_supported = False  # noqa: SLF001
        raise RuntimeError("boom")

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=unsupported_during_fetch
    )
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=fetch_marks_unsupported)
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_inventory_ready is True  # noqa: SLF001

    async def fetch_invalid_marks_unsupported(*, refresh_data: bool = False):
        coord.client._hems_site_supported = False  # noqa: SLF001
        return "bad"

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=unsupported_during_fetch
    )
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=fetch_invalid_marks_unsupported)
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_inventory_ready is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_pagination_and_error_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    runtime._merge_microinverter_type_bucket = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    runtime._merge_heatpump_type_bucket = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001

    class BadStatusType:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    pages: list[tuple[int, int]] = []

    async def inventory_fetcher(*args, **kwargs):
        if kwargs:
            offset = kwargs["offset"]
            pages.append((kwargs["limit"], offset))
            if offset == 0:
                return {
                    "total": 3,
                    "normal_count": 5,
                    "warning_count": 5,
                    "error_count": 5,
                    "not_reporting": 5,
                    "inverters": [
                        {"serial_number": "INV-A", "name": "IQ8", "status": "normal"},
                        {"serial_number": "INV-B", "name": "IQ8", "status": "offline"},
                    ],
                    "panel_info": {
                        "manufacturer": "  ",
                        "model": None,
                        "rating": [],
                    },
                }
            if offset == 2:
                return {
                    "total": 3,
                    "inverters": [
                        {"serial_number": "INV-C", "name": "IQ7", "status": "warning"},
                        {"serial_number": "   ", "name": "blank"},
                        {"serial_number": "INV-RET", "name": "retired"},
                    ],
                }
            raise AssertionError(offset)
        return {"total": 1, "inverters": [{"serial_number": "INV-FB", "name": "IQ7"}]}

    coord.client.inverters_inventory = AsyncMock(side_effect=inventory_fetcher)
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": {
                "serialNum": "INV-A",
                "deviceId": 1,
                "statusCode": "normal",
                "type": BadStatusType(),
            },
            "1002": "bad",
            "1003": {"serialNum": "", "deviceId": 3},
        }
    )
    coord.client.inverter_production = AsyncMock(return_value={"production": []})
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {
            "serial_number": "INV-A",
            "lifetime_production_wh": "bad",
            "lifetime_query_start_date": "2022-08-01",
            "lifetime_query_end_date": "2026-02-01",
        },
        "INV-B": {
            "serial_number": "INV-B",
            "inverter_id": "1002",
            "device_id": 2,
            "status_code": "warning",
            "lifetime_production_wh": "still-bad",
        },
        "INV-C": {
            "serial_number": "INV-C",
            "lifetime_production_wh": 20.0,
            "lifetime_query_start_date": "2022-08-05",
            "lifetime_query_end_date": "2026-02-05",
        },
    }

    monkeypatch.setattr(
        runtime,
        "member_is_retired",
        lambda item: item.get("serial_number") == "INV-RET",
    )

    await runtime._async_refresh_inverters()  # noqa: SLF001

    assert pages == [(1000, 0), (1000, 2)]
    assert coord.iter_inverter_serials() == ["INV-A", "INV-B", "INV-C"]
    assert coord.inverter_data("INV-A")["lifetime_production_wh"] is None
    assert coord.inverter_data("INV-A")["lifetime_query_start_date"] == "2022-08-01"
    assert coord.inverter_data("INV-B")["lifetime_query_start_date"] == "2022-08-10"
    assert (
        coord.inverter_data("INV-B")["lifetime_query_end_date"]
        == coord._site_local_current_date()
    )  # noqa: SLF001
    assert coord.inverter_data("INV-B")["lifetime_production_wh"] is None
    assert coord.inverter_data("INV-B")["inverter_type"] is None
    assert coord.inverter_data("INV-C")["lifetime_production_wh"] == 20.0
    assert runtime._inverter_summary_counts == {  # noqa: SLF001
        "total": 3,
        "normal": 1,
        "warning": 1,
        "error": 1,
        "not_reporting": 0,
        "unknown": 0,
    }
    assert runtime._inverter_panel_info is None  # noqa: SLF001

    coord.client.inverters_inventory = AsyncMock(return_value="bad")
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == ["INV-A", "INV-B", "INV-C"]

    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 2,
            "normal_count": 5,
            "warning_count": 5,
            "error_count": 5,
            "not_reporting": 5,
            "inverters": [
                {"serial_number": "INV-X", "name": "IQX", "status": "mystery"},
                {"serial_number": "INV-Y", "name": "IQY", "status": "mystery"},
            ],
        }
    )
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(return_value={})
    coord._inverter_data = {}  # noqa: SLF001
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert runtime._inverter_summary_counts == {  # noqa: SLF001
        "total": 2,
        "normal": 2,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
        "unknown": 0,
    }

    async def legacy_inventory_fetcher(*args, **kwargs):
        if kwargs:
            raise TypeError("legacy")
        return {"total": 1, "inverters": [{"serial_number": "INV-FB", "name": "IQ7"}]}

    coord.client.inverters_inventory = AsyncMock(side_effect=legacy_inventory_fetcher)
    coord.client.inverter_status = AsyncMock(return_value=[])
    coord.client.inverter_production = AsyncMock(return_value="bad")
    await runtime._async_refresh_inverters()  # noqa: SLF001
    coord.client.inverter_production.assert_awaited_once()

    async def pagination_typeerror_fetcher(*args, **kwargs):
        if kwargs.get("offset") == 0:
            return {
                "total": 2,
                "inverters": [{"serial_number": "INV-1", "name": "IQ7"}],
            }
        raise TypeError("legacy page")

    coord.client.inverters_inventory = AsyncMock(
        side_effect=pagination_typeerror_fetcher
    )
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": "bad"}}
    )
    coord._inverter_data = {
        "INV-1": {"serial_number": "INV-1", "inverter_id": "1001"}
    }  # noqa: SLF001
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == ["INV-1"]
    assert coord.inverter_data("INV-1")["lifetime_production_wh"] is None

    async def pagination_non_list_fetcher(*args, **kwargs):
        if kwargs.get("offset") == 0:
            return {
                "total": 2,
                "inverters": [{"serial_number": "INV-2", "name": "IQ7"}],
            }
        return {"total": 2, "inverters": "bad"}

    coord.client.inverters_inventory = AsyncMock(
        side_effect=pagination_non_list_fetcher
    )
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == ["INV-2"]

    async def pagination_empty_page_fetcher(*args, **kwargs):
        if kwargs.get("offset") == 0:
            return {
                "total": 2,
                "inverters": [{"serial_number": "INV-3", "name": "IQ7"}],
            }
        return {"total": 2, "inverters": []}

    coord.client.inverters_inventory = AsyncMock(
        side_effect=pagination_empty_page_fetcher
    )
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == ["INV-3"]

    async def pagination_total_growth_fetcher(*args, **kwargs):
        if kwargs.get("offset") == 0:
            return {
                "total": 2,
                "inverters": [{"serial_number": "INV-4", "name": "IQ7"}],
            }
        return {
            "total": 3,
            "inverters": [{"serial_number": "INV-5", "name": "IQ8"}],
        }

    coord.client.inverters_inventory = AsyncMock(
        side_effect=pagination_total_growth_fetcher
    )
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == ["INV-4", "INV-5"]


def test_inventory_runtime_misc_helper_edges(coordinator_factory) -> None:
    runtime = coordinator_factory().inventory_runtime

    assert runtime._normalize_inverter_status("") == "unknown"  # noqa: SLF001
    assert (
        runtime._inverter_connectivity_state({"total": 2, "unknown": 2}) == "unknown"
    )  # noqa: SLF001


def test_inventory_runtime_refresh_cached_topology_handles_snapshot_errors(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory(serials=[]).inventory_runtime
    runtime._rebuild_inventory_summary_caches = lambda: None  # type: ignore[method-assign]  # noqa: SLF001

    def _boom():
        raise RuntimeError("snapshot failed")

    runtime._current_topology_snapshot = _boom  # type: ignore[method-assign]  # noqa: SLF001

    assert runtime._refresh_cached_topology() is False  # noqa: SLF001
