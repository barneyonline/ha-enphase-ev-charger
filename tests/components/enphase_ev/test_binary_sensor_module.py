from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace
from typing import Callable
from unittest.mock import MagicMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev import DOMAIN
from custom_components.enphase_ev import binary_sensor
from custom_components.enphase_ev.binary_sensor import (
    ChargingBinarySensor,
    ConnectedBinarySensor,
    HeatPumpSgReadyActiveBinarySensor,
    PluggedInBinarySensor,
    SiteCloudReachableBinarySensor,
    async_setup_entry,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _stub_listener() -> Callable[[], None]:
    """Return a reusable unsubscribe stub for coordinator listeners."""
    return lambda: None


@pytest.mark.asyncio
async def test_async_setup_entry_syncs_binary_sensors(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    """Ensure charger binary sensors add once per serial and register unload."""
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": True,
                "charging": False,
                "faulted": False,
                "connected": True,
                "commissioned": True,
            }
        }
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    callbacks: list[Callable[[], None]] = []

    def _capture_listener(callback: Callable[[], None]) -> Callable[[], None]:
        callbacks.append(callback)
        return _stub_listener()

    monkeypatch.setattr(coord, "async_add_topology_listener", _capture_listener)

    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)

    assert (
        len([ent for ent in added if isinstance(ent, SiteCloudReachableBinarySensor)])
        == 1
    )
    per_serial = [ent for ent in added if hasattr(ent, "_sn")]
    assert len(per_serial) == 3
    assert {type(ent) for ent in per_serial} == {
        PluggedInBinarySensor,
        ChargingBinarySensor,
        ConnectedBinarySensor,
    }
    assert config_entry._on_unload and callable(config_entry._on_unload[0])

    sync_cb = next(cb for cb in callbacks if cb.__name__ == "_async_sync_chargers")

    sync_cb()
    assert len(added) == 4

    new_serial = "EV0002"
    coord.data[new_serial] = {
        "sn": new_serial,
        "name": "Second EV",
        "plugged": False,
        "charging": True,
        "faulted": False,
        "connected": True,
        "commissioned": False,
    }
    coord._ensure_serial_tracked(new_serial)

    sync_cb()
    assert len(added) == 7
    assert {ent._sn for ent in added if hasattr(ent, "_sn")} == {
        RANDOM_SERIAL,
        new_serial,
    }

    sync_cb()
    assert len(added) == 7


@pytest.mark.asyncio
async def test_async_setup_entry_falls_back_to_generic_listener_for_binary_sensors(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    callbacks: list[Callable[[], None]] = []

    monkeypatch.setattr(coord, "async_add_topology_listener", None, raising=False)
    monkeypatch.setattr(
        coord,
        "async_add_listener",
        lambda callback: callbacks.append(callback) or _stub_listener(),
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert callbacks


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_site_sensor_without_gateway_type(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": True,
                "charging": False,
                "faulted": False,
                "connected": True,
                "commissioned": True,
            }
        }
    )
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
            }
        },
        ["iqevse"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )
    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)

    assert any(isinstance(ent, SiteCloudReachableBinarySensor) for ent in added)
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 3


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_site_sensor_when_inventory_unknown(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": True,
                "charging": False,
                "faulted": False,
                "connected": True,
                "commissioned": True,
            }
        }
    )
    coord._type_device_buckets = {}  # noqa: SLF001
    coord._type_device_order = []  # noqa: SLF001
    coord._devices_inventory_ready = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )
    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)

    assert any(isinstance(ent, SiteCloudReachableBinarySensor) for ent in added)
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 3
    assert any(isinstance(ent, HeatPumpSgReadyActiveBinarySensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_does_not_duplicate_site_sensor_when_gateway_type_appears_later(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": True,
                "charging": False,
                "faulted": False,
                "connected": True,
                "commissioned": True,
            }
        }
    )
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
            }
        },
        ["iqevse"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    callbacks: list[Callable[[], None]] = []

    def _capture_listener(callback: Callable[[], None]) -> Callable[[], None]:
        callbacks.append(callback)
        return _stub_listener()

    monkeypatch.setattr(coord, "async_add_topology_listener", _capture_listener)
    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)
    assert (
        len([ent for ent in added if isinstance(ent, SiteCloudReachableBinarySensor)])
        == 1
    )

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
            },
        },
        ["envoy", "iqevse"],
    )
    callbacks[0]()

    assert (
        len([ent for ent in added if isinstance(ent, SiteCloudReachableBinarySensor)])
        == 1
    )


@pytest.mark.asyncio
async def test_async_setup_entry_adds_heatpump_sg_ready_binary_sensor_when_type_appears(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": True,
                "charging": False,
                "faulted": False,
                "connected": True,
                "commissioned": True,
            }
        }
    )
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
            },
        },
        ["envoy", "iqevse"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    callbacks: list[Callable[[], None]] = []

    def _capture_listener(callback: Callable[[], None]) -> Callable[[], None]:
        callbacks.append(callback)
        return _stub_listener()

    monkeypatch.setattr(coord, "async_add_topology_listener", _capture_listener)
    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)
    assert not any(isinstance(ent, HeatPumpSgReadyActiveBinarySensor) for ent in added)

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
            },
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "name": "SG Ready Gateway",
                        "statusText": "Recommended",
                    }
                ],
            },
        },
        ["envoy", "iqevse", "heatpump"],
    )
    callbacks[0]()

    assert (
        len(
            [ent for ent in added if isinstance(ent, HeatPumpSgReadyActiveBinarySensor)]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_heatpump_sg_ready_binary_sensor_when_type_removed(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "name": "SG Ready Gateway",
                        "statusText": "Recommended",
                    }
                ],
            },
        },
        ["envoy", "heatpump"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    callbacks: list[Callable[[], None]] = []

    def _capture_listener(callback: Callable[[], None]) -> Callable[[], None]:
        callbacks.append(callback)
        return _stub_listener()

    fake_registry = SimpleNamespace(
        async_get_entity_id=MagicMock(
            return_value="binary_sensor.heat_pump_sg_ready_active"
        ),
        async_remove=MagicMock(),
    )
    monkeypatch.setattr(coord, "async_add_topology_listener", _capture_listener)
    monkeypatch.setattr(
        "custom_components.enphase_ev.binary_sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)
    assert any(isinstance(ent, HeatPumpSgReadyActiveBinarySensor) for ent in added)

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            }
        },
        ["envoy"],
    )
    callbacks[0]()

    fake_registry.async_get_entity_id.assert_any_call(
        "binary_sensor",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_heat_pump_sg_ready_active",
    )
    fake_registry.async_remove.assert_called_once_with(
        "binary_sensor.heat_pump_sg_ready_active"
    )


def test_ev_bool_sensors_reflect_coordinator_state(
    coordinator_factory, monkeypatch
) -> None:
    """Validate EV charger binary sensor helpers read from coordinator data."""
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": 0,
                "charging": 1,
                "faulted": True,
                "connected": False,
                "commissioned": True,
                "connection": " wifi ",
                "ip_address": " 192.0.2.10 ",
            }
        }
    )
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    plugged = PluggedInBinarySensor(coord, RANDOM_SERIAL)
    assert plugged.is_on is False

    charging = ChargingBinarySensor(coord, RANDOM_SERIAL)
    assert charging.icon == "mdi:flash"

    coord.data[RANDOM_SERIAL]["charging"] = 0
    assert charging.icon == "mdi:flash-off"

    connected = ConnectedBinarySensor(coord, RANDOM_SERIAL)
    assert connected.device_class == BinarySensorDeviceClass.CONNECTIVITY
    assert connected.entity_category == EntityCategory.DIAGNOSTIC
    attrs = connected.extra_state_attributes
    assert attrs["connection"] == "wifi"
    assert attrs["ip_address"] == "192.0.2.10"


def test_site_cloud_reachable_binary_sensor_metadata(
    coordinator_factory, monkeypatch
) -> None:
    """Exercise availability, attributes, and device info for the site sensor."""
    coord = coordinator_factory(serials=[], data={})
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    sensor = SiteCloudReachableBinarySensor(coord)
    assert sensor.translation_key == "cloud_reachable"

    coord.last_success_utc = None
    coord.last_update_success = False
    assert sensor.available is False

    now = datetime.now(timezone.utc)
    coord.last_update_success = True
    coord.last_success_utc = now - timedelta(seconds=45)
    coord.update_interval = None
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    assert sensor.available is True
    assert sensor.is_on is True

    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(seconds=61))
    assert sensor.is_on is False

    failure_time = now - timedelta(seconds=15)
    coord.last_success_utc = now
    coord.last_failure_utc = failure_time
    coord.last_failure_status = 503
    coord.last_failure_description = "Gateway error"
    coord.last_failure_response = {"retry": True}
    coord.last_failure_source = "http"
    coord.backoff_ends_utc = now + timedelta(seconds=90)

    attrs = sensor.extra_state_attributes
    assert "last_success_utc" not in attrs
    assert attrs["last_failure_utc"] == failure_time.isoformat()
    assert attrs["last_failure_status"] == 503
    assert attrs["code_description"] == "Gateway error"
    assert attrs["last_failure_response"] == {"retry": True}
    assert attrs["last_failure_source"] == "http"
    assert attrs["backoff_ends_utc"] == coord.backoff_ends_utc.isoformat()

    info = sensor.device_info
    assert info["identifiers"] == {(DOMAIN, f"type:{coord.site_id}:cloud")}
    assert info["manufacturer"] == "Enphase"
    assert info["model"] == "Cloud Service"
    assert info["name"] == "Enphase Cloud"


def test_binary_sensor_helper_type_available_falls_back_to_has_type() -> None:
    coord = SimpleNamespace(
        has_type=lambda type_key: type_key == "heatpump",
    )

    assert binary_sensor._type_available(coord, "heatpump") is True
    assert binary_sensor._type_available(coord, "envoy") is False


def test_heatpump_sg_ready_active_binary_sensor_metadata(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-1",
                        "name": "SG Ready Gateway",
                        "statusText": "Recommended",
                        "last_report": "2026-03-03T07:30:00Z",
                        "model": "Europa Mini WP",
                        "serial_number": "HP-1",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )
    coord._hems_devices_last_success_utc = datetime(
        2026, 3, 3, 7, 31, tzinfo=timezone.utc
    )  # noqa: SLF001
    coord._hems_devices_last_success_mono = time.monotonic() - 30  # noqa: SLF001
    coord._hems_devices_using_stale = True  # noqa: SLF001

    sensor = HeatPumpSgReadyActiveBinarySensor(coord)
    assert sensor.translation_key == "heat_pump_sg_ready_active"
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor.available is True
    assert sensor.is_on is True

    attrs = sensor.extra_state_attributes
    assert attrs["status_text"] == "Recommended"
    assert attrs["active_member_count"] == 1
    assert attrs["sg_ready_mode"] == 3
    assert attrs["sg_ready_contact_state"] == "closed"
    assert attrs["status_explanation"] == (
        "Recommended means the SG Ready contact is closed."
    )
    assert attrs["latest_reported_utc"] == "2026-03-03T07:30:00+00:00"
    assert attrs["hems_data_stale"] is True
    assert attrs["hems_last_success_utc"] == "2026-03-03T07:31:00+00:00"
    assert attrs["hems_last_success_age_s"] is not None

    info = sensor.device_info
    assert info["name"] == "Heat Pump"
    assert info["model"] == "Europa Mini WP"
    assert info["serial_number"] == "HP-1"

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "name": "SG Ready Gateway",
                        "statusText": "Normal",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    assert sensor.is_on is False

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "name": "Heat Pump"}],
            }
        },
        ["heatpump"],
    )
    assert sensor.available is False


def test_heatpump_sg_ready_active_binary_sensor_uses_dedicated_hems_inventory(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "heat-pump": [
                    {
                        "device-type": "SG_READY_GATEWAY",
                        "device-uid": "HP-SG-1",
                        "name": "SG Ready Gateway",
                        "statusText": "Recommended",
                        "last-report": "2026-03-03T07:30:00Z",
                        "model": "Europa Mini WP",
                        "serial": "HP-1",
                    },
                    {
                        "device-type": "HEAT_PUMP",
                        "device-uid": "HP-CTRL-1",
                        "name": "Heat Pump",
                        "statusText": "Normal",
                    },
                ]
            }
        }
    }
    coord._merge_heatpump_type_bucket()  # noqa: SLF001
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    sensor = HeatPumpSgReadyActiveBinarySensor(coord)
    assert sensor.available is True
    assert sensor.is_on is True
    assert (
        sensor.unique_id == f"{DOMAIN}_site_{coord.site_id}_heat_pump_sg_ready_active"
    )
    assert (
        sensor.extra_state_attributes["latest_reported_utc"]
        == "2026-03-03T07:30:00+00:00"
    )


def test_heatpump_sg_ready_active_binary_sensor_stays_on_for_mixed_member_statuses(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-1",
                        "name": "SG Ready Gateway 1",
                        "statusText": "Recommended",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-2",
                        "name": "SG Ready Gateway 2",
                        "statusText": "Normal",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    sensor = HeatPumpSgReadyActiveBinarySensor(coord)
    assert sensor.available is True
    assert sensor.is_on is True
    attrs = sensor.extra_state_attributes
    assert attrs["status_text"] == "Normal"
    assert attrs["active_member_count"] == 1


def test_heatpump_sg_ready_active_binary_sensor_helper_edge_cases(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )
    sensor = HeatPumpSgReadyActiveBinarySensor(coord)

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "name": "SG Ready Gateway",
                        "status_text": "Recommended",
                    },
                    "bad-member",
                ],
            }
        },
        ["heatpump"],
    )
    assert sensor._status_text() == "Recommended"  # noqa: SLF001
    assert sensor._active_member_count() == 1  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "name": "SG Ready Gateway",
                        "status": "Normal",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    assert sensor._status_text() == "Normal"  # noqa: SLF001
    assert sensor._active_member_count() == 0  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [],
            }
        },
        ["heatpump"],
    )
    assert sensor._status_text() is None  # noqa: SLF001
    monkeypatch.setattr(sensor, "_snapshot", lambda: {"members": None})
    assert sensor._active_member_count() == 0  # noqa: SLF001
    monkeypatch.setattr(sensor, "_snapshot", lambda: {"members": ["bad-member"]})
    assert sensor._active_member_count() == 0  # noqa: SLF001


def test_heatpump_sg_ready_active_binary_sensor_unavailable_without_type(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord.has_type_for_entities = lambda _type_key: False  # type: ignore[assignment]
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    sensor = HeatPumpSgReadyActiveBinarySensor(coord)
    assert sensor.available is False


def test_site_cloud_reachable_binary_sensor_fallback_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )
    coord.has_type_for_entities = lambda _type_key: False  # type: ignore[assignment]
    coord.last_update_success = False

    sensor = SiteCloudReachableBinarySensor(coord)
    assert sensor.available is False

    coord.last_update_success = True
    coord.last_success_utc = datetime.now(timezone.utc)
    assert sensor.available is True

    coord.last_success_utc = None
    assert sensor.is_on is False

    info = sensor.device_info
    assert info["identifiers"] == {(DOMAIN, f"type:{coord.site_id}:cloud")}

    custom_info = {"name": "Custom Cloud"}
    coord.type_device_info = lambda _type_key: custom_info  # type: ignore[assignment]
    assert sensor.device_info is custom_info
