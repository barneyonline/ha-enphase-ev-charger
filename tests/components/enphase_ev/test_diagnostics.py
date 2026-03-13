"""Tests for integration diagnostics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeassistant.helpers import device_registry as dr

from custom_components.enphase_ev import diagnostics
from custom_components.enphase_ev.energy import SiteEnergyFlow
from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


def test_gateway_diagnostics_helper_branches() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert diagnostics._text(BadStr()) is None
    assert diagnostics._optional_bool(1) is True
    assert diagnostics._optional_bool("on") is True
    assert diagnostics._optional_bool("off") is False
    assert diagnostics._optional_bool("unknown") is None
    assert diagnostics._normalize_gateway_status(None) == "unknown"
    assert diagnostics._normalize_gateway_status("critical error") == "error"
    assert diagnostics._normalize_gateway_status("other") == "unknown"

    empty = diagnostics._gateway_summary([], 0)
    assert empty["connectivity"] is None

    summary = diagnostics._gateway_summary(
        [
            {"connected": True, "status": "normal", "model": "IQ Gateway"},
            {"connected": False, "statusText": "not reporting", "sw_version": "8.2.0"},
            {"connected": "maybe", "status": "warning"},
        ],
        3,
    )
    assert summary["connectivity"] == "degraded"
    assert summary["connected_devices"] == 1
    assert summary["disconnected_devices"] == 1
    assert summary["unknown_connection_devices"] == 1
    assert summary["model_counts"]["IQ Gateway"] == 1
    assert summary["firmware_counts"]["8.2.0"] == 1

    fallback_summary = diagnostics._gateway_summary(
        [
            {"connected": None, "status": "normal"},
            {"connected": None, "statusText": "not reporting"},
        ],
        2,
    )
    assert fallback_summary["connected_devices"] == 1
    assert fallback_summary["disconnected_devices"] == 1

    unknown = diagnostics._gateway_summary([{"status": "unknown"}], 2)
    assert unknown["connectivity"] == "unknown"
    assert diagnostics._gateway_summary([{"connected": True}], 1)["connectivity"] == "online"
    assert (
        diagnostics._gateway_summary(
            [{"connected": False}, {"connected": False}],
            2,
        )["connectivity"]
        == "offline"
    )
    micro_summary = diagnostics._microinverter_summary(
        {
            "count": 3,
            "status_counts": {
                "total": 3,
                "normal": 2,
                "warning": 0,
                "error": 0,
                "not_reporting": 1,
            },
            "panel_info": {"pv_module_manufacturer": "Acme"},
            "status_summary": "Normal 2 | Warning 0 | Error 0 | Not Reporting 1",
        }
    )
    assert micro_summary["connectivity"] == "degraded"
    assert micro_summary["reporting_inverters"] == 2
    assert micro_summary["panel_info"]["pv_module_manufacturer"] == "Acme"
    assert diagnostics._microinverter_summary([]) == {}
    assert (
        diagnostics._microinverter_summary({"count": object()})["total_inverters"] == 0
    )
    assert (
        diagnostics._microinverter_summary(
            {"count": 1, "status_counts": {"total": 1, "not_reporting": 0}}
        )["connectivity"]
        == "online"
    )
    assert (
        diagnostics._microinverter_summary(
            {"count": 1, "status_counts": {"total": 1, "not_reporting": 1}}
        )["connectivity"]
        == "offline"
    )
    malformed_counts = diagnostics._microinverter_summary(
        {"count": 2, "status_counts": {"total": "x", "not_reporting": object()}}
    )
    assert malformed_counts["total_inverters"] == 2
    assert malformed_counts["connectivity"] == "unknown"
    assert malformed_counts["unknown_inverters"] == 2
    overflow_counts = diagnostics._microinverter_summary(
        {"count": 1, "status_counts": {"total": 1, "not_reporting": 1, "unknown": 2}}
    )
    assert overflow_counts["unknown_inverters"] == 0
    assert overflow_counts["connectivity"] == "offline"
    assert diagnostics._microinverter_summary({"count": 2})["connectivity"] == "unknown"
    assert diagnostics._microinverter_summary({})["connectivity"] is None


class DummyClient(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__()
        self._h = {"Authorization": "REDACTED", "X-Test": "value"}

    def base_header_names(self) -> list[str]:
        return sorted(self._h.keys())

    def scheduler_bearer(self):
        return "token"

    def has_scheduler_bearer(self) -> bool:
        return bool(self.scheduler_bearer())


class DummyCoordinator(SimpleNamespace):
    """Coordinator stub exposing diagnostics attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.client = DummyClient()
        self.update_interval = timedelta(seconds=45)
        self._charge_mode_cache = {RANDOM_SERIAL: ("FAST", 0)}
        self.site_id = RANDOM_SITE_ID
        self.serials = {RANDOM_SERIAL}
        self.data = {RANDOM_SERIAL: {"sn": RANDOM_SERIAL, "status": "idle"}}
        self._network_errors = 2
        self._http_errors = 1
        self._backoff_until = 120.0
        self._last_error = "timeout"
        self.phase_timings = {"fast": 0.6}
        self._session_history_cache_ttl = 300
        self._session_history_cache = {"key": []}
        self._session_history_interval_min = 15
        self._session_refresh_in_progress = {"key"}
        self.session_history = SimpleNamespace(
            cache_ttl=300,
            cache_key_count=1,
            in_progress=1,
        )
        self._battery_site_settings_payload = {
            "data": {"showSavingsMode": True},
            "userId": "[redacted]",
        }
        self._battery_profile_payload = {
            "data": {"profile": "cost_savings", "batteryBackupPercentage": 20},
            "token": "[redacted]",
        }
        self._battery_settings_payload = {
            "data": {"batteryGridMode": "ImportExport", "chargeFromGrid": True}
        }
        self._battery_status_payload = {
            "current_charge": "48%",
            "storages": [{"serial_number": "BT0001", "status": "normal"}],
        }
        self._grid_control_check_payload = {
            "disableGridControl": False,
            "activeDownload": False,
        }
        self._dry_contact_settings_payload = {
            "data": {
                "contacts": [
                    {"serial": "DC0001", "displayName": "Solar Diverter"},
                ]
            },
            "token": "[redacted]",
        }
        self._battery_backup_history_payload = {
            "total_records": 1,
            "histories": [{"start_time": "2025-10-17T14:38:30+11:00", "duration": 121}],
        }
        self._hems_devices_payload = {
            "data": {
                "hems-devices": {
                    "gateway": [
                        {
                            "device-uid": "5956621_IQ_ENERGY_ROUTER_1",
                            "uid": "LGX-025",
                            "hems-device-id": "router-id",
                            "hems-device-facet-id": "router-facet-id",
                            "iqer-uid": "5956621_IQ_ENERGY_ROUTER_1",
                            "ip-address": "192.0.2.99",
                            "statusText": "Normal",
                        }
                    ]
                }
            }
        }
        self._devices_inventory_payload = {"result": [{"type": "encharge"}]}
        self._system_dashboard_devices_tree_payload = {
            "devices": [
                {
                    "device_uid": "GW-1",
                    "type": "envoy",
                    "name": "Gateway",
                    "children": [{"device_uid": "BAT-1", "type": "encharge"}],
                }
            ]
        }
        self._system_dashboard_devices_details_payloads = {
            "envoy": {
                "envoy": {
                    "device_link": "https://enlighten.example/systems/3381244/envoys/200001",
                    "modem": {
                        "rssi": -72,
                        "signal_strength": "strong",
                        "plan_expiry_date": "2026-08-01",
                        "imei": "359111111111111",
                    },
                    "connection_details": {
                        "interface_ip": {
                            "ethernet": "192.0.2.10",
                        }
                    },
                    "network_configuration": [
                        {
                            "details": {
                                "mac_addr": "00:11:22:33:44:55",
                                "ip_addr": "192.0.2.10",
                                "gateway_ip_addr": "192.0.2.1",
                            }
                        }
                    ],
                    "default_route": "192.0.2.1 (Ethernet)",
                    "network": {"status": "online", "mode": "dhcp"},
                    "tunnel": {"status": "connected"},
                },
                "enpower": {"earth_type": "TN-C-S"},
                "meter": {
                    "devices": [
                        {
                            "device_uid": "MTR-1",
                            "type": "meter",
                            "name": "Consumption Meter",
                            "meter_type": "consumption",
                            "configuration": {"phase": "three_phase"},
                        }
                    ]
                },
            },
            "encharge": {
                "encharge": {
                    "connectivity": {"rssi": -61, "status": "online"},
                    "software": {"app_version": "1.2.3"},
                    "operation_mode": {"mode": "backup"},
                    "imsi": "310150123456789",
                }
            },
        }
        self._system_dashboard_hierarchy_summary = {
            "total_nodes": 2,
            "counts_by_type": {"envoy": 1, "encharge": 1},
            "relationships": [
                {
                    "device_uid": "GW-1",
                    "parent_uid": None,
                    "type_key": "envoy",
                    "name": "Gateway",
                },
                {
                    "device_uid": "BAT-1",
                    "parent_uid": "GW-1",
                    "type_key": "encharge",
                    "name": "Battery 1",
                },
            ],
        }
        self._system_dashboard_type_summaries = {
            "envoy": {
                "modem": {
                    "rssi": -72,
                    "signal": "strong",
                    "sim_plan_expiry": "2026-08-01",
                    "imei": "359111111111111",
                },
                "network": {"status": "online", "mode": "dhcp"},
                "tunnel": {"status": "connected"},
                "controller": {"earth_type": "TN-C-S"},
                "meters": [
                    {
                        "name": "Consumption Meter",
                        "meter_type": "consumption",
                        "config": {"phase": "three_phase"},
                    }
                ],
                "hierarchy": {
                    "count": 1,
                    "relationships": [{"device_uid": "GW-1", "parent_uid": None}],
                },
            },
            "encharge": {
                "connectivity": {"rssi": -61, "status": "online"},
                "software": {"app_version": "1.2.3"},
                "operation_mode": {"mode": "backup"},
                "hierarchy": {
                    "count": 1,
                    "relationships": [
                        {"device_uid": "BAT-1", "parent_uid": "GW-1"}
                    ],
                },
            },
            "microinverter": {
                "total_inverters": 16,
                "not_reporting_inverters": 1,
                "plc_comm_inverters": 5,
                "model_summary": "IQ7A Microinverters x16",
            },
        }
        self._evse_site_feature_flags = {
            "evse_charging_mode": True,
            "evse_storm_guard": False,
        }
        self._evse_feature_flags_by_serial = {
            RANDOM_SERIAL: {
                "evse_authentication": True,
                "iqevse_rfid": True,
                "max_current_config_support": True,
            }
        }
        self._evse_feature_flags_payload = {
            "meta": {"serverTimeStamp": "2026-03-08T09:40:02.917+00:00"},
            "data": {
                "evse_charging_mode": True,
                RANDOM_SERIAL: {
                    "evse_authentication": True,
                    "iqevse_rfid": True,
                    "max_current_config_support": True,
                },
            },
            "error": {},
        }
        self.include_inverters = True
        self._inverter_summary_counts = {
            "total": 2,
            "normal": 2,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
        }
        self._inverter_model_counts = {"IQ7A": 2}
        self._inverters_inventory_payload = {"total": 2}
        self._inverter_status_payload = {"key": {"serialNum": "INV-A"}}
        self._inverter_production_payload = {"production": {"key": 100}}
        self.firmware_catalog_manager = SimpleNamespace(
            status_snapshot=lambda: {
                "last_fetch_utc": "2026-03-01T00:00:00+00:00",
                "last_success_utc": "2026-03-01T00:00:00+00:00",
                "last_error": None,
                "using_stale": False,
                "catalog_generated_at": "2026-03-01T00:00:00Z",
                "catalog_source_age_seconds": 42.0,
            }
        )

    def collect_site_metrics(self):
        return {
            "site_id": self.site_id,
            "site_name": "Garage Site",
            "network_errors": self._network_errors,
            "http_errors": self._http_errors,
            "last_error": self._last_error,
            "phase_timings": self.phase_timings,
            "session_cache_ttl_s": self._session_history_cache_ttl,
            "dry_contact_settings_supported": True,
            "dry_contact_settings_contact_count": 1,
            "dry_contact_settings_unmatched_count": 0,
            "dry_contact_settings_fetch_failures": 0,
            "dry_contact_settings_data_stale": False,
        }

    def charge_mode_cache_snapshot(self):
        cache = getattr(self, "_charge_mode_cache", {}) or {}
        return {str(serial): str(value[0]) for serial, value in cache.items() if value}

    def session_history_diagnostics(self):
        return {
            "cache_ttl_seconds": self._session_history_cache_ttl,
            "cache_keys": len(self._session_history_cache),
            "interval_minutes": self._session_history_interval_min,
            "in_progress": len(self._session_refresh_in_progress),
        }

    def battery_diagnostics_payloads(self):
        return {
            "site_settings_payload": self._battery_site_settings_payload,
            "profile_payload": self._battery_profile_payload,
            "settings_payload": self._battery_settings_payload,
            "status_payload": self._battery_status_payload,
            "grid_control_check_payload": self._grid_control_check_payload,
            "dry_contacts_payload": self._dry_contact_settings_payload,
            "backup_history_payload": self._battery_backup_history_payload,
            "hems_devices_payload": self._hems_devices_payload,
            "devices_inventory_payload": self._devices_inventory_payload,
        }

    def inverter_diagnostics_payloads(self):
        return {
            "enabled": self.include_inverters,
            "summary_counts": self._inverter_summary_counts,
            "model_counts": self._inverter_model_counts,
            "inventory_payload": self._inverters_inventory_payload,
            "status_payload": self._inverter_status_payload,
            "production_payload": self._inverter_production_payload,
        }

    def evse_diagnostics_payloads(self):
        return {
            "feature_flags_meta": self._evse_feature_flags_payload["meta"],
            "feature_flags_error": self._evse_feature_flags_payload["error"],
            "site_feature_flags": self._evse_site_feature_flags,
            "charger_feature_flags": [
                {"serial": RANDOM_SERIAL, "flags": self._evse_feature_flags_by_serial[RANDOM_SERIAL]}
            ],
            "charger_support_sources": [
                {
                    "serial": RANDOM_SERIAL,
                    "sources": {"auth_feature_supported": "runtime"},
                }
            ],
        }

    def scheduler_diagnostics(self):
        backoff = self._scheduler_backoff_ends_utc
        if isinstance(backoff, datetime):
            try:
                backoff = backoff.isoformat()
            except Exception:
                backoff = None
        return {"backoff_ends_utc": backoff}

    def system_dashboard_diagnostics(self):
        return {
            "devices_tree_payload": self._system_dashboard_devices_tree_payload,
            "devices_details_payloads": self._system_dashboard_devices_details_payloads,
            "hierarchy_summary": self._system_dashboard_hierarchy_summary,
            "type_summaries": self._system_dashboard_type_summaries,
        }


@pytest.mark.asyncio
async def test_config_entry_diagnostics_includes_coordinator(hass, config_entry) -> None:
    """Validate coordinator diagnostics payload and redaction logic."""
    coord = DummyCoordinator()
    coord.schedule_sync = SimpleNamespace(diagnostics=lambda: {"enabled": True})
    coord._scheduler_backoff_ends_utc = datetime(2025, 1, 1, tzinfo=timezone.utc)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["entry_data"]["cookie"] == "**REDACTED**"
    assert diag["entry_data"]["email"] == "**REDACTED**"
    assert diag["coordinator"]["site_id"] == "**REDACTED**"
    assert diag["coordinator"]["site_metrics"]["site_name"] == "**REDACTED**"
    assert diag["coordinator"]["headers_info"]["base_header_names"] == [
        "Authorization",
        "X-Test",
    ]
    assert diag["coordinator"]["headers_info"]["has_scheduler_bearer"] is True
    assert diag["coordinator"]["last_scheduler_modes"] == {RANDOM_SERIAL: "FAST"}
    assert diag["coordinator"]["session_history"]["cache_keys"] == 1
    assert diag["coordinator"]["battery_config"]["site_settings_payload"]["userId"] == "[redacted]"
    assert diag["coordinator"]["battery_config"]["profile_payload"]["token"] == "[redacted]"
    assert (
        diag["coordinator"]["battery_config"]["settings_payload"]["data"]["batteryGridMode"]
        == "ImportExport"
    )
    assert diag["coordinator"]["battery_config"]["status_payload"]["current_charge"] == "48%"
    assert (
        diag["coordinator"]["battery_config"]["grid_control_check_payload"][
            "disableGridControl"
        ]
        is False
    )
    assert (
        diag["coordinator"]["battery_config"]["dry_contacts_payload"]["token"]
        == "[redacted]"
    )
    assert (
        diag["coordinator"]["battery_config"]["backup_history_payload"]["total_records"]
        == 1
    )
    assert diag["coordinator"]["battery_config"]["hems_devices_payload"]["data"][
        "hems-devices"
    ]["gateway"][0]["device-uid"] == "**REDACTED**"
    assert diag["coordinator"]["battery_config"]["hems_devices_payload"]["data"][
        "hems-devices"
    ]["gateway"][0]["uid"] == "**REDACTED**"
    assert diag["coordinator"]["battery_config"]["hems_devices_payload"]["data"][
        "hems-devices"
    ]["gateway"][0]["hems-device-id"] == "**REDACTED**"
    assert diag["coordinator"]["battery_config"]["hems_devices_payload"]["data"][
        "hems-devices"
    ]["gateway"][0]["ip-address"] == "**REDACTED**"
    assert diag["coordinator"]["battery_config"]["devices_inventory_payload"] == {
        "result": [{"type": "encharge"}]
    }
    assert (
        diag["coordinator"]["site_metrics"]["dry_contact_settings_supported"] is True
    )
    assert diag["coordinator"]["site_metrics"]["dry_contact_settings_contact_count"] == 1
    assert diag["coordinator"]["evse"]["site_feature_flags"]["evse_charging_mode"] is True
    assert (
        diag["coordinator"]["evse"]["charger_feature_flags"][0]["serial"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["evse"]["charger_feature_flags"][0]["flags"][
            "max_current_config_support"
        ]
        is True
    )
    assert (
        diag["coordinator"]["evse"]["charger_support_sources"][0]["serial"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["evse"]["charger_support_sources"][0]["sources"][
            "auth_feature_supported"
        ]
        == "runtime"
    )
    assert diag["coordinator"]["inverters"]["enabled"] is True
    assert diag["coordinator"]["inverters"]["summary_counts"]["total"] == 2
    assert diag["coordinator"]["inverters"]["model_counts"]["IQ7A"] == 2
    assert diag["coordinator"]["firmware_catalog"]["catalog_generated_at"] == (
        "2026-03-01T00:00:00Z"
    )
    assert diag["coordinator"]["system_dashboard"]["devices_tree_payload"]["devices"][0][
        "device_uid"
    ] == "**REDACTED**"
    assert diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
        "envoy"
    ]["modem"]["imei"] == "**REDACTED**"
    assert diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
        "envoy"
    ]["device_link"] == "**REDACTED**"
    assert diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
        "envoy"
    ]["connection_details"]["interface_ip"] == "**REDACTED**"
    assert diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
        "envoy"
    ]["network_configuration"][0]["details"]["mac_addr"] == "**REDACTED**"
    assert diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
        "envoy"
    ]["network_configuration"][0]["details"]["ip_addr"] == "**REDACTED**"
    assert diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
        "envoy"
    ]["network_configuration"][0]["details"]["gateway_ip_addr"] == "**REDACTED**"
    assert diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
        "envoy"
    ]["default_route"] == "**REDACTED**"
    assert diag["coordinator"]["system_dashboard"]["hierarchy_summary"]["relationships"][
        1
    ]["parent_uid"] == "**REDACTED**"
    assert diag["coordinator"]["system_dashboard"]["type_summaries"]["envoy"]["modem"][
        "sim_plan_expiry"
    ] == "2026-08-01"
    assert (
        diag["coordinator"]["system_dashboard"]["type_summaries"]["microinverter"][
            "plc_comm_inverters"
        ]
        == 5
    )
    assert diag["coordinator"]["schedule_sync"] == {"enabled": True}
    assert diag["coordinator"]["scheduler"]["backoff_ends_utc"] == "2025-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_schedule_sync_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()

    class BadScheduleSync:
        def diagnostics(self):
            raise RuntimeError("boom")

    coord.schedule_sync = BadScheduleSync()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["schedule_sync"] is None


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_evse_capture_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.evse_diagnostics_payloads = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["evse"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_firmware_catalog_snapshot_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.firmware_catalog_manager = SimpleNamespace(
        status_snapshot=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["firmware_catalog"] is None


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_scheduler_backoff_format_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()

    class BadDatetime(datetime):
        def isoformat(self):  # type: ignore[override]
            raise ValueError("boom")

    coord._scheduler_backoff_ends_utc = BadDatetime(2025, 1, 1, tzinfo=timezone.utc)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["scheduler"]["backoff_ends_utc"] is None


@pytest.mark.asyncio
async def test_config_entry_diagnostics_without_coordinator(hass, config_entry) -> None:
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert "coordinator" not in diag


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_faulty_coordinator(
    hass, config_entry
) -> None:
    class FaultyClient:
        def base_header_names(self):
            raise RuntimeError("no headers")

        def has_scheduler_bearer(self):
            raise RuntimeError("no bearer")

    class FaultyCoordinator(DummyCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.update_interval = object()
            self._charge_mode_cache = None
            self.client = FaultyClient()

        def collect_site_metrics(self):
            raise RuntimeError("boom")

    coord = FaultyCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    coordinator = diag["coordinator"]
    assert coordinator["site_metrics"] is None
    assert coordinator["headers_info"]["base_header_names"] == []
    assert coordinator["headers_info"]["has_scheduler_bearer"] is False
    assert coordinator["last_scheduler_modes"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_snapshot_helper_errors(
    hass, config_entry
) -> None:
    class PartialFailureCoordinator(DummyCoordinator):
        def charge_mode_cache_snapshot(self):
            raise RuntimeError("modes")

        def session_history_diagnostics(self):
            raise RuntimeError("session")

        def battery_diagnostics_payloads(self):
            raise RuntimeError("battery")

        def inverter_diagnostics_payloads(self):
            raise RuntimeError("inverters")

    coord = PartialFailureCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    coordinator = diag["coordinator"]
    assert coordinator["last_scheduler_modes"] == {}
    assert coordinator["session_history"] == {}
    assert coordinator["battery_config"] == {}
    assert coordinator["inverters"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_system_dashboard_capture_error(
    hass, config_entry
) -> None:
    class PartialFailureCoordinator(DummyCoordinator):
        def system_dashboard_diagnostics(self):
            raise RuntimeError("dashboard")

    coord = PartialFailureCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["coordinator"]["system_dashboard"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_fetches_system_dashboard_lazily(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.async_ensure_system_dashboard_diagnostics = AsyncMock()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    coord.async_ensure_system_dashboard_diagnostics.assert_awaited_once()
    assert "system_dashboard" in diag["coordinator"]


@pytest.mark.asyncio
async def test_config_entry_diagnostics_ignores_dashboard_prefetch_failure(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.async_ensure_system_dashboard_diagnostics = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    coord.async_ensure_system_dashboard_diagnostics.assert_awaited_once()
    assert "system_dashboard" in diag["coordinator"]


@pytest.mark.asyncio
async def test_config_entry_diagnostics_includes_site_energy(hass, config_entry) -> None:
    coord = DummyCoordinator()
    coord.energy = SimpleNamespace(
        site_energy={
            "grid_import": SiteEnergyFlow(
                value_kwh=1.0,
                bucket_count=2,
                fields_used=["import"],
                start_date="2024-01-01",
                last_report_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
                update_pending=True,
                source_unit="Wh",
                last_reset_at=None,
                interval_minutes=60,
            ),
            "legacy_flow": SimpleNamespace(
                value_kwh=2.0,
                bucket_count=1,
                fields_used=["legacy"],
                start_date="2024-01-02",
                last_report_date=datetime(2024, 1, 4, tzinfo=timezone.utc),
                update_pending=False,
                source_unit="Wh",
                last_reset_at="2024-01-05T00:00:00+00:00",
                interval_minutes=30,
            ),
        },
        site_energy_meta={
            "start_date": "2024-01-01",
            "last_report_date": datetime(2024, 1, 3, tzinfo=timezone.utc),
        },
        site_energy_cache_age=1.23,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    site_energy = diag["site_energy"]
    assert "grid_import" in site_energy["flows"]
    assert site_energy["flows"]["grid_import"]["interval_minutes"] == 60
    assert site_energy["flows"]["legacy_flow"]["interval_minutes"] == 30
    assert site_energy["meta"]["last_report_date"].startswith("2024-01-03")


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_unexpected_site_energy(hass, config_entry) -> None:
    coord = DummyCoordinator()
    coord.energy = SimpleNamespace(
        site_energy={"bad": None, "other": "string"},
        site_energy_meta={"last_report_date": datetime(2024, 1, 1, tzinfo=timezone.utc)},
        site_energy_cache_age=1.23,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["site_energy"]["flows"] in (None, {})

    class BoomSiteEnergy:
        def items(self):
            raise RuntimeError("boom")

    coord.energy.site_energy = BoomSiteEnergy()
    coord.energy.site_energy_meta = {
        "last_report_date": datetime(2024, 1, 2, tzinfo=timezone.utc)
    }
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["site_energy"]["flows"] is None


@pytest.mark.asyncio
async def test_config_entry_diagnostics_cache_age_failure(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.energy = SimpleNamespace(
        site_energy={"grid_import": {"value_kwh": 1.0}},
        site_energy_meta={
            "last_report_date": datetime(2024, 1, 2, tzinfo=timezone.utc)
        },
        site_energy_cache_age=None,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["site_energy"]["cache_age_s"] is None


@pytest.mark.asyncio
async def test_device_diagnostics_returns_snapshot(
    hass, config_entry
) -> None:
    """Device diagnostics should resolve a serial and return cached data."""
    coord = DummyCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL), (DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Garage Charger",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    assert result["serial"] == "**REDACTED**"
    assert result["snapshot"] == {"sn": "**REDACTED**", "status": "idle"}


@pytest.mark.asyncio
async def test_device_diagnostics_handles_missing_serial(
    hass, config_entry
) -> None:
    """If a device has no serial identifier, report the error."""
    coord = DummyCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    assert result == {"error": "serial_not_resolved"}


@pytest.mark.asyncio
async def test_device_diagnostics_device_not_found(hass, config_entry) -> None:
    device = SimpleNamespace(id="missing-device")
    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    assert result == {"error": "device_not_found"}


@pytest.mark.asyncio
async def test_device_diagnostics_missing_coordinator(hass, config_entry) -> None:
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL), (DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Garage Charger",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    assert result == {"serial": "**REDACTED**", "snapshot": {}}


@pytest.mark.asyncio
async def test_device_diagnostics_type_device_payload(hass, config_entry) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: {  # type: ignore[attr-defined]
        "type_label": "Battery",
        "count": 2,
        "devices": [{"serial_number": "BAT-1"}, {"serial_number": "BAT-2"}],
        "status_counts": {"normal": 2},
        "property_keys": ["name", "serial_number"],
        "summary_label": "ok",
    } if type_key == "encharge" else None
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:encharge")},
        manufacturer="Enphase",
        name="Battery (2)",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    assert result["site_id"] == "**REDACTED**"
    assert result["type_key"] == "encharge"
    assert result["type_label"] == "Battery"
    assert result["count"] == 2
    assert len(result["devices"]) == 2
    assert result["status_counts"]["normal"] == 2
    assert result["property_keys"] == ["name", "serial_number"]
    assert result["summary_label"] == "ok"


@pytest.mark.asyncio
async def test_device_diagnostics_envoy_includes_gateway_summary(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: {  # type: ignore[attr-defined]
        "type_label": "Gateway",
        "count": 3,
        "devices": [
            {
                "serial_number": "GW-1",
                "name": "IQ Gateway",
                "connected": True,
                "status": "normal",
                "model": "IQ Gateway",
                "envoy_sw_version": "8.2.0",
            },
            {
                "serial_number": "GW-2",
                "name": "System Controller",
                "connected": False,
                "statusText": "Not Reporting",
                "channel_type": "enpower",
                "sw_version": "8.2.0",
            },
            {
                "serial_number": "GW-3",
                "name": "Meter",
                "connected": None,
                "status": "warning",
            },
        ],
    } if type_key == "envoy" else None
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    summary = result["gateway_summary"]
    assert result["type_key"] == "envoy"
    assert summary["connectivity"] == "degraded"
    assert summary["connected_devices"] == 1
    assert summary["disconnected_devices"] == 1
    assert summary["unknown_connection_devices"] == 1
    assert summary["status_counts"]["warning"] == 1
    assert summary["model_counts"]["IQ Gateway"] == 1
    assert summary["firmware_counts"]["8.2.0"] == 2
    assert "serial_number" in summary["property_keys"]
    assert result["system_dashboard_details"]["modem"]["rssi"] == -72
    assert result["system_dashboard_details"]["controller"]["earth_type"] == "TN-C-S"
    assert result["system_dashboard_details"]["hierarchy"]["relationships"][0][
        "device_uid"
    ] == "**REDACTED**"


@pytest.mark.asyncio
async def test_device_diagnostics_envoy_gateway_summary_handles_bad_bucket_shapes(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: {  # type: ignore[attr-defined]
        "type_label": "Gateway",
        "count": "bad",
        "devices": "bad",
    } if type_key == "envoy" else None
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    summary = result["gateway_summary"]
    assert summary["connectivity"] is None
    assert summary["connected_devices"] == 0


@pytest.mark.asyncio
async def test_device_diagnostics_microinverter_includes_summary(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: {  # type: ignore[attr-defined]
        "type_label": "Microinverters",
        "count": 3,
        "devices": [
            {"serial_number": "INV-A"},
            {"serial_number": "INV-B"},
            {"serial_number": "INV-C"},
        ],
        "status_counts": {
            "total": 3,
            "normal": 2,
            "warning": 0,
            "error": 0,
            "not_reporting": 1,
        },
        "status_summary": "Normal 2 | Warning 0 | Error 0 | Not Reporting 1",
        "model_summary": "IQ7A x3",
        "panel_info": {"pv_module_manufacturer": "Acme"},
        "latest_reported_utc": "2026-02-15T18:00:00+00:00",
    } if type_key == "microinverter" else None
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:microinverter")},
        manufacturer="Enphase",
        name="Microinverters",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    summary = result["microinverter_summary"]
    assert result["type_key"] == "microinverter"
    assert summary["connectivity"] == "degraded"
    assert summary["total_inverters"] == 3
    assert summary["reporting_inverters"] == 2
    assert summary["not_reporting_inverters"] == 1
    assert summary["panel_info"]["pv_module_manufacturer"] == "Acme"


@pytest.mark.asyncio
async def test_device_diagnostics_type_device_without_coordinator_payload(
    hass, config_entry
) -> None:
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={
            ("other", "value"),
            (DOMAIN, f"type:{RANDOM_SITE_ID}:encharge"),
        },
        manufacturer="Enphase",
        name="Battery (2)",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    assert result["site_id"] == "**REDACTED**"
    assert result["type_key"] == "encharge"
    assert result["count"] == 0
    assert result["devices"] == []


@pytest.mark.asyncio
async def test_device_diagnostics_encharge_includes_system_dashboard_details(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.async_ensure_system_dashboard_diagnostics = AsyncMock()
    coord.type_bucket = lambda type_key: {  # type: ignore[attr-defined]
        "type_label": "Battery",
        "count": 1,
        "devices": [{"serial_number": "BAT-1", "name": "Battery 1"}],
    } if type_key == "encharge" else None
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:encharge")},
        manufacturer="Enphase",
        name="Battery",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    coord.async_ensure_system_dashboard_diagnostics.assert_awaited_once()
    assert result["system_dashboard_details"]["connectivity"]["rssi"] == -61
    assert result["system_dashboard_details"]["software"]["app_version"] == "1.2.3"
    assert result["system_dashboard_details"]["operation_mode"]["mode"] == "backup"
    assert result["system_dashboard_details"]["hierarchy"]["relationships"][0][
        "parent_uid"
    ] == "**REDACTED**"


@pytest.mark.asyncio
async def test_device_diagnostics_ignores_dashboard_prefetch_failure(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.async_ensure_system_dashboard_diagnostics = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    coord.type_bucket = lambda type_key: {  # type: ignore[attr-defined]
        "type_label": "Gateway",
        "count": 1,
        "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
    } if type_key == "envoy" else None
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )

    coord.async_ensure_system_dashboard_diagnostics.assert_awaited_once()
    assert result["type_key"] == "envoy"


@pytest.mark.asyncio
async def test_device_diagnostics_handles_system_dashboard_capture_error(
    hass, config_entry
) -> None:
    class BrokenDashboardCoordinator(DummyCoordinator):
        def system_dashboard_diagnostics(self):
            raise RuntimeError("boom")

    coord = BrokenDashboardCoordinator()
    coord.type_bucket = lambda type_key: {  # type: ignore[attr-defined]
        "type_label": "Gateway",
        "count": 1,
        "devices": [{"serial_number": "GW-1"}],
    } if type_key == "envoy" else None
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    assert "system_dashboard_details" not in result
