from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.enphase_ev.inventory_runtime import HEMS_DEVICES_STALE_AFTER_S


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


def test_inventory_runtime_summary_and_inverter_helper_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime
    coord.type_bucket = lambda type_key: {  # type: ignore[assignment]
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
    coord.type_bucket = type(coord).type_bucket.__get__(coord, type(coord))  # type: ignore[method-assign]
    bucket = coord.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["extra_list"] == ["a", "b"]

    info = coord.type_device_info("microinverter")
    assert info is not None
    assert info["hw_version"] == "IQ7A-SKU"
    assert info.get("model_id") is None
    assert coord.type_device_model_id("microinverter") is None
    assert coord.type_device_model(None) is None
    assert coord.type_device_serial_number(None) is None
    assert coord.type_device_model_id(None) is None
    assert coord.type_device_sw_version(None) is None
    assert coord.type_device_hw_version(None) is None
    coord._type_device_buckets = {"microinverter": "bad"}  # noqa: SLF001
    assert coord.type_device_hw_version("microinverter") is None

    coord.type_bucket = lambda _key: {"devices": "bad"}  # type: ignore[assignment]
    assert coord._type_bucket_members("envoy") == []  # noqa: SLF001
    coord.type_bucket = type(coord).type_bucket.__get__(coord, type(coord))  # type: ignore[method-assign]

    class BadText:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert coord._type_member_text({"name": BadText()}, "name") is None  # noqa: SLF001
    assert (
        coord._type_summary_from_values(
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
    assert coord.type_device_sw_version("microinverter") is None
    coord._type_device_buckets = {"encharge": "bad"}  # noqa: SLF001
    assert coord.type_device_hw_version("encharge") is None
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "type_key": "envoy",
            "type_label": "Gateway",
            "count": 1,
            "devices": [{"serial_number": "GW-1"}],
            "status_summary": "Normal 1 | Warning 0 | Error 0 | Not Reporting 0",
        }
    }
    assert coord.type_device_hw_version("envoy") is None

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
    coord._set_type_device_buckets(  # noqa: SLF001
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

    assert coord.iter_type_keys() == ["wind_turbine", "encharge", "microinverter"]
    assert coord.type_device_name("wind-turbine") == "Wind Turbine"
    assert coord.type_bucket("encharge")["count"] == 3
    assert coord.type_bucket("encharge")["model_summary"] == "IQ Battery 5P x2"
    assert coord.type_bucket("microinverter")["count"] == 3
    assert coord.has_type("generator") is False

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
    assert coord.type_bucket("meter") == coord.type_bucket("envoy")
    assert coord.type_bucket("enpower") == coord.type_bucket("envoy")

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

    monkeypatch.setattr(coord, "_redact_battery_payload", lambda payload: "raw")
    coord.client.devices_inventory = AsyncMock(
        return_value={
            "result": [{"type": "envoy", "devices": [{"name": "IQ Gateway"}]}]
        }
    )
    await runtime._async_refresh_devices_inventory(force=True)
    assert runtime._devices_inventory_payload == {"value": "raw"}  # noqa: SLF001

    monkeypatch.setattr(coord, "_redact_battery_payload", lambda payload: payload)
    await runtime._async_refresh_devices_inventory(force=True)
    assert coord.has_type("envoy") is True

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

    monkeypatch.setattr(coord, "_redact_battery_payload", lambda payload: payload)
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
    bucket = coord.type_bucket("microinverter")
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
    assert coord.type_bucket("microinverter") is None


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
