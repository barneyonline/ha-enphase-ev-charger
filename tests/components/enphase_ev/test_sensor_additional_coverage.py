"""Additional coverage for Enphase EV sensor helpers."""

from __future__ import annotations

from datetime import timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev import sensor as sensor_mod
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

pytest.importorskip("homeassistant")


def _mk_coord(sn: str, payload: dict[str, Any]) -> Any:
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = SimpleNamespace()
    coord.hass.config = SimpleNamespace(units=SimpleNamespace(length_unit="mi"))
    coord.data = {sn: payload}
    coord.serials = {sn}
    coord.last_set_amps = {}
    coord.site_id = "123456"
    coord._serial_order = [sn]
    coord.iter_serials = lambda: list(coord.serials)

    def _default_listener(callback, context=None):
        return lambda: None

    coord.async_add_listener = _default_listener  # type: ignore[assignment]
    coord.last_success_utc = None
    coord.last_failure_utc = None
    coord.last_failure_status = None
    coord.last_failure_description = None
    coord.last_failure_source = None
    coord.last_failure_response = None
    coord.backoff_ends_utc = None
    coord.latency_ms = None
    coord.last_update_success = True
    return coord


@pytest.mark.asyncio
async def test_async_setup_entry_registers_entities(
    hass, config_entry, coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.data[RANDOM_SERIAL].update(
        {
            "connector_status": "AVAILABLE",
            "charge_mode": "IMMEDIATE",
            "plugged": True,
            "charging_level": 32,
        }
    )
    callbacks: list[Any] = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _async_add_entities(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _async_add_entities)
    assert any(ent.unique_id.endswith("_energy_today") for ent in added)
    assert any(ent.unique_id.endswith("_last_rpt") for ent in added)
    assert any(ent.unique_id.endswith("_electrical_phase") for ent in added)
    assert any(ent.unique_id.endswith("_charger_authentication") for ent in added)
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 11

    sync_chargers_cb = next(cb for cb in callbacks if cb.__name__ == "_async_sync_chargers")
    sync_chargers_cb()
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 11

    new_sn = "NEWSN123"
    coord.data[new_sn] = dict(coord.data[RANDOM_SERIAL], sn=new_sn)
    coord.serials.add(new_sn)
    sync_chargers_cb()
    assert len({ent._sn for ent in added if hasattr(ent, "_sn")}) == 2


@pytest.mark.asyncio
async def test_async_setup_entry_skips_battery_entities_without_battery(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryModeSensor,
        EnphaseStormAlertSensor,
        EnphaseStormGuardStateSensor,
        EnphaseSystemProfileStatusSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_has_encharge = False  # noqa: SLF001
    coord.data[RANDOM_SERIAL].update(
        {
            "connector_status": "AVAILABLE",
            "charge_mode": "IMMEDIATE",
            "plugged": True,
            "charging_level": 32,
        }
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _async_add_entities(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _async_add_entities)

    assert not any(isinstance(ent, EnphaseStormAlertSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseBatteryModeSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseSystemProfileStatusSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseStormGuardStateSensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_battery_storage_sensors(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryOverallChargeSensor,
        EnphaseBatteryOverallStatusSensor,
        EnphaseBatteryStorageCycleCountSensor,
        EnphaseBatteryStorageChargeSensor,
        EnphaseBatteryStorageHealthSensor,
        EnphaseBatteryStorageLastReportedSensor,
        EnphaseBatteryStorageStatusSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-1": {
            "identity": "BAT-1",
            "name": "IQ Battery 5P",
            "serial_number": "BAT-1",
            "current_charge_pct": 48.0,
            "status": "normal",
        },
        "BAT-2": {
            "identity": "BAT-2",
            "name": "IQ Battery 5P",
            "serial_number": "BAT-2",
            "current_charge_pct": 47.0,
            "status": "normal",
        },
    }
    coord._battery_storage_order = ["BAT-1", "BAT-2"]  # noqa: SLF001
    coord._battery_aggregate_charge_pct = 47.5  # noqa: SLF001
    coord._battery_aggregate_status = "normal"  # noqa: SLF001
    coord._battery_aggregate_status_details = {  # noqa: SLF001
        "included_count": 2,
        "excluded_count": 0,
        "per_battery_status": {"BAT-1": "normal", "BAT-2": "normal"},
        "battery_order": ["BAT-1", "BAT-2"],
    }
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    battery_entities = [
        ent for ent in added if isinstance(ent, EnphaseBatteryStorageChargeSensor)
    ]
    assert len(battery_entities) == 2
    assert len(
        [ent for ent in added if isinstance(ent, EnphaseBatteryStorageStatusSensor)]
    ) == 2
    assert len(
        [ent for ent in added if isinstance(ent, EnphaseBatteryStorageHealthSensor)]
    ) == 2
    assert len(
        [ent for ent in added if isinstance(ent, EnphaseBatteryStorageCycleCountSensor)]
    ) == 2
    assert len(
        [ent for ent in added if isinstance(ent, EnphaseBatteryStorageLastReportedSensor)]
    ) == 2
    assert any(
        isinstance(ent, EnphaseBatteryOverallChargeSensor) for ent in added
    )
    assert any(
        isinstance(ent, EnphaseBatteryOverallStatusSensor) for ent in added
    )


@pytest.mark.asyncio
async def test_async_setup_entry_removes_battery_entity_on_serial_drop(
    hass, config_entry, coordinator_factory
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-REMOVE": {
            "identity": "BAT-REMOVE",
            "serial_number": "BAT-REMOVE",
            "current_charge_pct": 55,
        }
    }
    coord._battery_storage_order = ["BAT-REMOVE"]  # noqa: SLF001
    callbacks: list[Any] = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)
    ent_reg = er.async_get(hass)
    unique_id_charge = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-REMOVE_charge_level"
    unique_id_status = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-REMOVE_status"
    entity_id = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id_charge,
        suggested_object_id="battery_remove_charge",
    ).entity_id
    status_entity_id = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id_status,
        suggested_object_id="battery_remove_status",
    ).entity_id
    assert ent_reg.async_get(entity_id) is not None
    assert ent_reg.async_get(status_entity_id) is not None

    coord._battery_storage_data = {}  # noqa: SLF001
    coord._battery_storage_order = []  # noqa: SLF001
    for callback in callbacks:
        callback()

    assert ent_reg.async_get(entity_id) is None
    assert ent_reg.async_get(status_entity_id) is None


@pytest.mark.asyncio
async def test_async_setup_entry_removes_stale_battery_entity_after_restart(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {}  # noqa: SLF001
    coord._battery_storage_order = []  # noqa: SLF001
    coord.async_add_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-OLD_status"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.bat_old_charge_level": SimpleNamespace(
                    entity_id="sensor.bat_old_status",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=unique_id,
                    config_entry_id=config_entry.entry_id,
                )
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == ["sensor.bat_old_status"]


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_current_battery_entity(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-KEEP": {
            "identity": "BAT-KEEP",
            "serial_number": "BAT-KEEP",
            "current_charge_pct": 50,
        }
    }
    coord._battery_storage_order = ["BAT-KEEP"]  # noqa: SLF001
    coord.async_add_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-KEEP_charge_level"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.bat_keep_charge_level": SimpleNamespace(
                    entity_id="sensor.bat_keep_charge_level",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=unique_id,
                    config_entry_id=config_entry.entry_id,
                )
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == []


@pytest.mark.asyncio
async def test_async_setup_entry_ignores_empty_battery_serial_unique_id(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {}  # noqa: SLF001
    coord._battery_storage_order = []  # noqa: SLF001
    coord.async_add_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    unique_id = f"{DOMAIN}_site_{coord.site_id}_battery__status"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.empty_serial_status": SimpleNamespace(
                    entity_id="sensor.empty_serial_status",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=unique_id,
                    config_entry_id=config_entry.entry_id,
                )
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == []


@pytest.mark.asyncio
async def test_async_setup_entry_adds_inverter_lifetime_sensors(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.sensor import (
        EnphaseInverterLifetimeEnergySensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {
            "serial_number": "INV-A",
            "name": "IQ7A",
            "inverter_id": "1001",
            "device_id": 42,
            "lifetime_production_wh": 1_500_000,
            "lifetime_query_start_date": "2022-08-10",
            "lifetime_query_end_date": "2026-02-09",
        }
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"serial_number": "INV-A"}],
            "model_summary": "IQ7A x1",
            "status_summary": "Normal 1 | Warning 0 | Error 0 | Not Reporting 0",
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)
    inverter_entities = [
        ent for ent in added if isinstance(ent, EnphaseInverterLifetimeEnergySensor)
    ]
    assert len(inverter_entities) == 1
    entity = inverter_entities[0]
    assert entity.native_value == pytest.approx(1.5)
    attrs = entity.extra_state_attributes
    assert attrs["device_id"] == 42
    assert attrs["lifetime_production_wh"] == 1_500_000

    # Entity reports unavailable once the inverter snapshot is removed.
    coord._inverter_data = {}  # noqa: SLF001
    assert entity.available is False


@pytest.mark.asyncio
async def test_async_setup_entry_removes_deleted_inverter_entity(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {"serial_number": "INV-A", "lifetime_production_wh": 100}
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    callbacks: list[Any] = []

    def _add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_listener = _add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    unique_id = f"{DOMAIN}_inverter_INV-A_lifetime_energy"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.inv_a_lifetime_energy": SimpleNamespace(
                    entity_id="sensor.inv_a_lifetime_energy",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=unique_id,
                    config_entry_id=config_entry.entry_id,
                )
            }

        def async_get_entity_id(self, domain, platform, candidate_unique_id):
            if (
                domain == "sensor"
                and platform == DOMAIN
                and candidate_unique_id == unique_id
            ):
                return "sensor.inv_a_lifetime_energy"
            return None

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)
    sync_inverters = next(
        cb for cb in callbacks if cb.__name__ == "_async_sync_inverters"
    )

    coord._inverter_data = {}  # noqa: SLF001
    coord._inverter_order = []  # noqa: SLF001
    sync_inverters()

    assert removed_ids == ["sensor.inv_a_lifetime_energy"]


@pytest.mark.asyncio
async def test_async_setup_entry_removes_stale_inverter_entity_after_restart(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {}  # noqa: SLF001
    coord._inverter_order = []  # noqa: SLF001
    coord.async_add_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    unique_id = f"{DOMAIN}_inverter_INV-Z_lifetime_energy"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.inv_z_lifetime_energy": SimpleNamespace(
                    entity_id="sensor.inv_z_lifetime_energy",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=unique_id,
                    config_entry_id=config_entry.entry_id,
                )
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == ["sensor.inv_z_lifetime_energy"]


@pytest.mark.asyncio
async def test_async_setup_entry_registry_cleanup_filters_irrelevant_entries(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {}  # noqa: SLF001
    coord._inverter_order = []  # noqa: SLF001
    coord.async_add_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.stale_should_remove": SimpleNamespace(
                    entity_id="sensor.stale_should_remove",
                    domain=None,
                    platform=DOMAIN,
                    unique_id=f"{DOMAIN}_inverter_INV-OLD_lifetime_energy",
                    config_entry_id=config_entry.entry_id,
                ),
                "switch.ignore_domain": SimpleNamespace(
                    entity_id="switch.ignore_domain",
                    domain="switch",
                    platform=DOMAIN,
                    unique_id=f"{DOMAIN}_inverter_INV-DOMAIN_lifetime_energy",
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.ignore_platform": SimpleNamespace(
                    entity_id="sensor.ignore_platform",
                    domain="sensor",
                    platform="other_domain",
                    unique_id=f"{DOMAIN}_inverter_INV-PLATFORM_lifetime_energy",
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.ignore_config_entry": SimpleNamespace(
                    entity_id="sensor.ignore_config_entry",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=f"{DOMAIN}_inverter_INV-CONFIG_lifetime_energy",
                    config_entry_id="different-entry-id",
                ),
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == ["sensor.stale_should_remove"]


def test_inverter_lifetime_sensor_clamps_regressions(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {"serial_number": "INV-A", "lifetime_production_wh": 2_000_000}
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")

    assert entity.native_value == pytest.approx(2.0)
    coord._inverter_data["INV-A"]["lifetime_production_wh"] = 1_500_000  # noqa: SLF001
    assert entity.native_value == pytest.approx(2.0)
    coord._inverter_data["INV-A"]["lifetime_production_wh"] = None  # noqa: SLF001
    assert entity.native_value == pytest.approx(2.0)
    coord._inverter_data = {}  # noqa: SLF001
    assert entity.native_value == pytest.approx(2.0)


def test_inverter_lifetime_sensor_handles_non_numeric_payload(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {"serial_number": "INV-A", "lifetime_production_wh": 2_000_000}
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    assert entity.native_value == pytest.approx(2.0)

    coord._inverter_data["INV-A"]["lifetime_production_wh"] = "bad"  # noqa: SLF001
    assert entity.native_value == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_inverter_lifetime_sensor_restores_last_value(monkeypatch, coordinator_factory):
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(native_value="3.25")
    )
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())

    await entity.async_added_to_hass()
    assert entity.native_value == pytest.approx(3.25)


@pytest.mark.asyncio
async def test_inverter_lifetime_sensor_restore_handles_empty_and_invalid_data(
    monkeypatch, coordinator_factory
) -> None:
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())

    entity_none = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_none.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    await entity_none.async_added_to_hass()
    assert entity_none.native_value is None

    entity_bad = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_bad.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(native_value=object())
    )
    await entity_bad.async_added_to_hass()
    assert entity_bad.native_value is None


def test_inverter_lifetime_sensor_device_info_fallback(coordinator_factory) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.site_id = "123456"
    coord.type_device_info = lambda _key: None  # type: ignore[assignment]
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")

    info = entity.device_info
    assert info is not None
    assert (DOMAIN, "type:123456:microinverter") in info["identifiers"]


def test_inverter_lifetime_sensor_device_info_prefers_coordinator_info(
    coordinator_factory,
) -> None:
    from homeassistant.helpers.entity import DeviceInfo

    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    expected = DeviceInfo(
        identifiers={(DOMAIN, f"type:{coord.site_id}:microinverter")},
        manufacturer="Enphase",
        name="Microinverters",
    )
    coord.type_device_info = lambda _key: expected  # type: ignore[assignment]
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")

    assert entity.device_info is expected


def test_type_inventory_sensor_summary_attributes(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import EnphaseTypeInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 2,
            "devices": [{"serial_number": "INV-A"}, {"serial_number": "INV-B"}],
            "status_counts": {"normal": 2, "warning": 0, "error": 0, "not_reporting": 0},
            "status_summary": "Normal 2 | Warning 0 | Error 0 | Not Reporting 0",
            "model_counts": {"IQ7A": 2},
            "model_summary": "IQ7A x2",
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    entity = EnphaseTypeInventorySensor(coord, "microinverter")

    attrs = entity.extra_state_attributes
    assert attrs["status_counts"]["normal"] == 2
    assert attrs["status_summary"].startswith("Normal 2")
    assert attrs["model_counts"]["IQ7A"] == 2
    assert attrs["model_summary"] == "IQ7A x2"


def test_inverter_lifetime_sensor_snapshot_handles_non_callable_getter(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inverter_data = None  # type: ignore[assignment]
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    assert entity.native_value is None


@pytest.mark.asyncio
async def test_async_setup_entry_adds_site_energy_entities(
    hass, config_entry, coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.energy.site_energy = {
        "grid_import": SimpleNamespace(
            value_kwh=1.0,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=None,
            update_pending=False,
            source_unit="Wh",
        )
    }

    callbacks: list = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _async_add_entities(entities, update_before_add=False):
        added.extend(entities)

    created: list = []

    class StubSiteEnergy(sensor_mod.EnphaseSiteEnergySensor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(sensor_mod, "EnphaseSiteEnergySensor", StubSiteEnergy)

    await async_setup_entry(hass, config_entry, _async_add_entities)
    for cb in callbacks:
        cb()
    assert created, "Expected site energy sensor to be created"
    assert any(ent._flow_key == "consumption" for ent in created)
    assert any(ent.translation_key == "site_consumption" for ent in created)


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_gateway_site_entities_when_inventory_unknown(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseCloudLatencySensor,
        EnphaseSiteLastUpdateSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._type_device_buckets = {}  # noqa: SLF001
    coord._type_device_order = []  # noqa: SLF001
    coord._devices_inventory_ready = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(ent, EnphaseSiteLastUpdateSensor) for ent in added)
    assert any(isinstance(ent, EnphaseCloudLatencySensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_type_inventory_sensors(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.sensor import (
        EnphaseTypeInventorySensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "wind_turbine": {
                "type_key": "wind_turbine",
                "type_label": "Wind Turbine",
                "count": 2,
                "devices": [{"name": "Wind 1"}, {"name": "Wind 2"}],
            },
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"name": "Battery 1"}],
            },
        },
        ["wind_turbine", "encharge"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    type_entities = [ent for ent in added if isinstance(ent, EnphaseTypeInventorySensor)]
    assert len(type_entities) == 2
    wind = next(ent for ent in type_entities if ent._type_key == "wind_turbine")  # noqa: SLF001
    assert wind.native_value == 2
    assert wind.extra_state_attributes["type_label"] == "Wind Turbine"
    assert wind.device_info["name"] == "Wind Turbine"


def test_session_metadata_attributes_handle_blanks():
    attrs = sensor_mod.EnphaseEnergyTodaySensor._session_metadata_attributes(
        {},
        hass=None,
        context={},
        energy_kwh=None,
        energy_wh=None,
        duration_min=None,
        session_key=None,
    )
    assert attrs["energy_consumed_kwh"] is None
    assert attrs["energy_consumed_wh"] is None
    assert attrs["range_added"] is None
    assert attrs["session_cost"] is None
    assert attrs["session_duration_min"] is None
    assert attrs["session_id"] is None
    assert attrs["session_started_at"] is None
    assert attrs["session_ended_at"] is None
    assert attrs["active_charge_time_s"] is None
    assert attrs["avg_cost_per_kwh"] is None
    assert attrs["cost_calculated"] is None
    assert attrs["session_cost_state"] is None
    assert attrs["manual_override"] is None
    assert attrs["charge_profile_stack_level"] is None


def test_session_metadata_attributes_formats_fields(monkeypatch):
    from homeassistant.const import UnitOfLength
    from homeassistant.util import dt as dt_util

    monkeypatch.setattr(
        dt_util, "as_local", lambda dt: dt.replace(tzinfo=timezone.utc)  # type: ignore[override]
    )
    payload = {
        "session_plug_in_at": 0,
        "session_plug_out_at": "2025-11-01T05:00:00Z",
        "session_miles": 10,
        "session_cost": "2.50",
        "session_charge_level": "24",
    }
    attrs = sensor_mod.EnphaseEnergyTodaySensor._session_metadata_attributes(
        payload,
        hass=SimpleNamespace(config=SimpleNamespace(units=SimpleNamespace(length_unit=UnitOfLength.KILOMETERS))),
        context={"energy_kwh": 1.5, "energy_wh": 1500.0, "session_charge_level": "20"},
        energy_kwh=1.5,
        energy_wh=1500.0,
        duration_min=90,
        session_key="abc",
    )
    assert attrs["plugged_in_at"].startswith("1970-01-01T00:00:00")
    assert attrs["plugged_out_at"] == "2025-11-01T05:00:00+00:00"
    assert attrs["energy_consumed_kwh"] == 1.5
    assert attrs["energy_consumed_wh"] == 1500.0
    assert attrs["session_cost"] == pytest.approx(2.5)
    assert attrs["session_charge_level"] == 20
    assert attrs["session_duration_min"] == 90
