"""Additional coverage for Enphase EV sensor helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import UnitOfEnergy

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
async def test_async_setup_entry_battery_registry_ignores_unknown_unique_suffix(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {}  # noqa: SLF001
    coord._battery_storage_order = []  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    fake_registry = SimpleNamespace(
        entities={
            "sensor.battery_unknown": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.battery_unknown",
                platform="enphase_ev",
                config_entry_id=config_entry.entry_id,
                unique_id=(
                    f"enphase_ev_site_{coord.site_id}_battery_BAT-123_unknown_suffix"
                ),
            )
        },
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_removed_gateway_connected_devices_entity(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    unique_ids = {
        f"enphase_ev_site_{coord.site_id}_gateway_connected_devices": "sensor.gateway_connected_devices",
        f"enphase_ev_site_{coord.site_id}_type_microinverter_inventory": "sensor.microinverter_inventory",
    }
    fake_registry = SimpleNamespace(
        entities={},
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(
            side_effect=lambda domain, platform, candidate_unique_id: (
                unique_ids.get(candidate_unique_id)
                if domain == "sensor" and platform == "enphase_ev"
                else None
            ),
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert fake_registry.async_remove.call_count == 2
    fake_registry.async_remove.assert_any_call("sensor.gateway_connected_devices")
    fake_registry.async_remove.assert_any_call("sensor.microinverter_inventory")


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
        EnphaseBatteryLastReportedSensor,
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
    ) == 0
    assert any(
        isinstance(ent, EnphaseBatteryOverallChargeSensor) for ent in added
    )
    assert any(
        isinstance(ent, EnphaseBatteryOverallStatusSensor) for ent in added
    )
    assert any(
        isinstance(ent, EnphaseBatteryLastReportedSensor) for ent in added
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
async def test_async_setup_entry_keeps_battery_site_summary_entities_on_registry_prune(
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
    overall_unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_overall_status"
    battery_last_reported_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_battery_last_reported"
    )

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.battery_overall_status": SimpleNamespace(
                    entity_id="sensor.battery_overall_status",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=overall_unique_id,
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.battery_last_reported": SimpleNamespace(
                    entity_id="sensor.battery_last_reported",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=battery_last_reported_unique_id,
                    config_entry_id=config_entry.entry_id,
                ),
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == []


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
async def test_async_setup_entry_prunes_legacy_battery_last_reported_entities_for_active_serial(
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
    legacy_unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-KEEP_last_reported"
    legacy_at_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_battery_BAT-KEEP_last_reported_at"
    )
    current_unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-KEEP_status"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.bat_keep_last_reported": SimpleNamespace(
                    entity_id="sensor.bat_keep_last_reported",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=legacy_unique_id,
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.bat_keep_last_reported_at": SimpleNamespace(
                    entity_id="sensor.bat_keep_last_reported_at",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=legacy_at_unique_id,
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.bat_keep_status": SimpleNamespace(
                    entity_id="sensor.bat_keep_status",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=current_unique_id,
                    config_entry_id=config_entry.entry_id,
                ),
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert sorted(removed_ids) == [
        "sensor.bat_keep_last_reported",
        "sensor.bat_keep_last_reported_at",
    ]


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
    assert entity.native_value == pytest.approx(1500.0)
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

    assert entity.native_value == pytest.approx(2000.0)
    coord._inverter_data["INV-A"]["lifetime_production_wh"] = 1_500_000  # noqa: SLF001
    assert entity.native_value == pytest.approx(2000.0)
    coord._inverter_data["INV-A"]["lifetime_production_wh"] = None  # noqa: SLF001
    assert entity.native_value == pytest.approx(2000.0)
    coord._inverter_data = {}  # noqa: SLF001
    assert entity.native_value == pytest.approx(2000.0)


def test_inverter_lifetime_sensor_handles_non_numeric_payload(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {"serial_number": "INV-A", "lifetime_production_wh": 2_000_000}
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    assert entity.native_value == pytest.approx(2000.0)

    coord._inverter_data["INV-A"]["lifetime_production_wh"] = "bad"  # noqa: SLF001
    assert entity.native_value == pytest.approx(2000.0)


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


@pytest.mark.asyncio
async def test_inverter_lifetime_sensor_restore_migrates_legacy_units(
    monkeypatch, coordinator_factory
) -> None:
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    class BadUnit:
        def __str__(self) -> str:
            raise ValueError("bad")

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())

    entity_mwh = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_mwh.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            native_value="1.5",
            native_unit_of_measurement="MWh",
        )
    )
    await entity_mwh.async_added_to_hass()
    assert entity_mwh.native_value == pytest.approx(1500.0)

    entity_wh = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_wh.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            native_value="1500",
            native_unit_of_measurement="Wh",
        )
    )
    await entity_wh.async_added_to_hass()
    assert entity_wh.native_value == pytest.approx(1.5)

    entity_bad_unit = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_bad_unit.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            native_value="2.0",
            native_unit_of_measurement=BadUnit(),
        )
    )
    await entity_bad_unit.async_added_to_hass()
    assert entity_bad_unit.native_value == pytest.approx(2.0)

    entity_inf = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_inf.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            native_value=float("inf"),
            native_unit_of_measurement="kWh",
        )
    )
    await entity_inf.async_added_to_hass()
    assert entity_inf.native_value is None


@pytest.mark.asyncio
async def test_inverter_lifetime_sensor_restore_forces_kwh_unit(
    monkeypatch, coordinator_factory
) -> None:
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())

    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity._attr_native_unit_of_measurement = UnitOfEnergy.MEGA_WATT_HOUR
    entity.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            native_value="1.5",
            native_unit_of_measurement="MWh",
        )
    )

    await entity.async_added_to_hass()

    assert entity.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert entity.native_value == pytest.approx(1500.0)


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
            "status_counts": {
                "total": 2,
                "normal": 2,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
            },
            "status_summary": "Normal 2 | Warning 0 | Error 0 | Not Reporting 0",
            "model_counts": {"IQ7A": 2},
            "model_summary": "IQ7A x2",
            "firmware_counts": {"520-00082-r01-v04.30.32": 2},
            "firmware_summary": "520-00082-r01-v04.30.32 x2",
            "array_counts": {"North": 1, "West": 1},
            "array_summary": "North x1, West x1",
            "panel_info": {"pv_module_manufacturer": "Acme"},
            "status_type_counts": {"IQ7A": 2},
            "connectivity_state": "online",
            "reporting_count": 2,
            "latest_reported_utc": "2026-02-15T08:00:00+00:00",
            "latest_reported_device": {"serial_number": "INV-B"},
            "production_start_date": "2022-08-10",
            "production_end_date": "2026-02-15",
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    entity = EnphaseTypeInventorySensor(coord, "microinverter")

    attrs = entity.extra_state_attributes
    assert attrs["status_counts"]["normal"] == 2
    assert attrs["status_summary"].startswith("Normal 2")
    assert attrs["model_counts"]["IQ7A"] == 2
    assert attrs["model_summary"] == "IQ7A x2"
    assert attrs["firmware_summary"] == "520-00082-r01-v04.30.32 x2"
    assert attrs["array_summary"] == "North x1, West x1"
    assert attrs["panel_info"]["pv_module_manufacturer"] == "Acme"
    assert attrs["status_type_counts"]["IQ7A"] == 2
    assert attrs["connectivity_state"] == "online"
    assert attrs["reporting_count"] == 2
    assert attrs["latest_reported_device"]["serial_number"] == "INV-B"
    assert attrs["production_start_date"] == "2022-08-10"
    assert attrs["production_end_date"] == "2026-02-15"


def test_gateway_diagnostic_sensors_expose_inventory_summary(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConnectivityStatusSensor,
        EnphaseGatewayLastReportedSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 3,
                "devices": [
                    {
                        "name": "IQ Gateway",
                        "serial_number": "GW-1",
                        "connected": True,
                        "status": "normal",
                        "model": "IQ Gateway",
                        "envoy_sw_version": "8.2.0",
                        "last_report": "2026-02-15T18:00:00Z",
                    },
                    {
                        "name": "System Controller",
                        "serial_number": "GW-2",
                        "connected": False,
                        "statusText": "Not Reporting",
                        "channel_type": "enpower",
                        "sw_version": "8.2.0",
                    },
                    {
                        "name": "Meter",
                        "serial_number": "GW-3",
                        "status": "warning",
                        "last_report": 1_708_016_100_000,
                    },
                ],
            }
        },
        ["envoy"],
    )

    status_sensor = EnphaseGatewayConnectivityStatusSensor(coord)
    assert status_sensor.native_value == "Degraded"
    status_attrs = status_sensor.extra_state_attributes
    assert status_attrs["total_devices"] == 3
    assert status_attrs["connected_devices"] == 1
    assert status_attrs["disconnected_devices"] == 1
    assert status_attrs["unknown_connection_devices"] == 1
    assert status_attrs["status_counts"]["warning"] == 1

    last_reported_sensor = EnphaseGatewayLastReportedSensor(coord)
    assert last_reported_sensor.entity_registry_enabled_default is True
    assert last_reported_sensor.native_value is not None
    report_attrs = last_reported_sensor.extra_state_attributes
    assert report_attrs["latest_reported_device"]["serial_number"] == "GW-1"
    assert report_attrs["without_last_report_count"] == 1


def test_gateway_diagnostic_sensors_handle_missing_inventory(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConnectivityStatusSensor,
        EnphaseGatewayLastReportedSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._type_device_buckets = {"envoy": {"count": 0, "devices": []}}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001

    assert EnphaseGatewayConnectivityStatusSensor(coord).native_value is None
    assert EnphaseGatewayLastReportedSensor(coord).native_value is None


def test_microinverter_diagnostic_sensors_expose_inventory_summary(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseMicroinverterConnectivityStatusSensor,
        EnphaseMicroinverterLastReportedSensor,
        EnphaseMicroinverterReportingCountSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 3,
                "devices": [
                    {
                        "name": "IQ7A",
                        "serial_number": "INV-A",
                        "statusText": "Normal",
                        "last_report": "2026-02-15T18:00:00Z",
                    },
                    {
                        "name": "IQ7A",
                        "serial_number": "INV-B",
                        "status": "NOT_REPORTING",
                        "last_report": 1_780_000_000_000,
                    },
                    {
                        "name": "IQ7A",
                        "serial_number": "INV-C",
                        "status": "normal",
                    },
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
                "firmware_summary": "520-00082-r01-v04.30.32 x3",
                "array_summary": "North x2, West x1",
                "panel_info": {"pv_module_manufacturer": "Acme"},
                "status_type_counts": {"IQ7A": 3},
                "production_start_date": "2022-08-10",
                "production_end_date": "2026-02-15",
            }
        },
        ["microinverter"],
    )

    status_sensor = EnphaseMicroinverterConnectivityStatusSensor(coord)
    assert status_sensor.native_value == "Degraded"
    status_attrs = status_sensor.extra_state_attributes
    assert status_attrs["total_inverters"] == 3
    assert status_attrs["reporting_inverters"] == 2
    assert status_attrs["not_reporting_inverters"] == 1
    assert status_attrs["unknown_inverters"] == 0
    assert set(status_attrs) == {
        "total_inverters",
        "reporting_inverters",
        "not_reporting_inverters",
        "unknown_inverters",
        "status_counts",
        "status_summary",
    }

    reporting_sensor = EnphaseMicroinverterReportingCountSensor(coord)
    assert reporting_sensor.native_value == 2
    reporting_attrs = reporting_sensor.extra_state_attributes
    assert reporting_attrs["type_key"] == "microinverter"
    assert reporting_attrs["type_label"] == "Microinverters"
    assert reporting_attrs["device_count"] == 3
    assert len(reporting_attrs["devices"]) == 3
    assert reporting_attrs["model_summary"] == "IQ7A x3"
    assert reporting_attrs["firmware_summary"] == "520-00082-r01-v04.30.32 x3"
    assert reporting_attrs["array_summary"] == "North x2, West x1"
    assert reporting_attrs["panel_info"]["pv_module_manufacturer"] == "Acme"
    assert reporting_attrs["status_type_counts"]["IQ7A"] == 3
    assert reporting_attrs["production_start_date"] == "2022-08-10"
    assert reporting_attrs["production_end_date"] == "2026-02-15"
    assert "connectivity_state" not in reporting_attrs
    assert "status_summary" not in reporting_attrs

    last_reported_sensor = EnphaseMicroinverterLastReportedSensor(coord)
    assert last_reported_sensor.entity_registry_enabled_default is True
    assert last_reported_sensor.available is True
    assert last_reported_sensor.native_value is not None
    report_attrs = last_reported_sensor.extra_state_attributes
    assert set(report_attrs) == {"latest_reported_device"}
    assert report_attrs["latest_reported_device"]["serial_number"] == "INV-B"


def test_microinverter_diagnostic_sensors_handle_disabled_or_empty_inventory(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseMicroinverterConnectivityStatusSensor,
        EnphaseMicroinverterLastReportedSensor,
        EnphaseMicroinverterReportingCountSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._type_device_buckets = {"microinverter": {"count": 0, "devices": []}}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001

    assert EnphaseMicroinverterConnectivityStatusSensor(coord).native_value is None
    assert EnphaseMicroinverterReportingCountSensor(coord).native_value is None
    assert EnphaseMicroinverterLastReportedSensor(coord).native_value is None

    coord.include_inverters = False
    assert EnphaseMicroinverterConnectivityStatusSensor(coord).available is False
    assert EnphaseMicroinverterReportingCountSensor(coord).available is False
    assert EnphaseMicroinverterLastReportedSensor(coord).available is False


def test_microinverter_snapshot_helper_handles_invalid_shapes(
    coordinator_factory,
) -> None:
    class BadInt:
        def __int__(self) -> int:
            raise ValueError("bad")

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.type_bucket = lambda _type_key: {  # type: ignore[assignment]
        "count": BadInt(),
        "devices": [{"serial_number": "INV-A", "last_report": "2026-02-15T10:00:00Z"}],
        "status_counts": {"total": BadInt(), "not_reporting": BadInt()},
    }

    snapshot = sensor_mod._microinverter_inventory_snapshot(coord)
    assert snapshot["total_inverters"] == 1
    assert snapshot["not_reporting_inverters"] == 0
    assert snapshot["unknown_inverters"] == 1
    assert snapshot["connectivity_state"] == "unknown"

    assert (
        sensor_mod._microinverter_connectivity_state(
            {"total_inverters": 3, "reporting_inverters": 3}
        )
        == "online"
    )
    assert (
        sensor_mod._microinverter_connectivity_state(
            {"total_inverters": 3, "reporting_inverters": 0, "not_reporting_inverters": 3}
        )
        == "offline"
    )
    assert (
        sensor_mod._microinverter_connectivity_state(
            {"total_inverters": 3, "reporting_inverters": 0, "unknown_inverters": 3}
        )
        == "unknown"
    )
    assert (
        sensor_mod._microinverter_connectivity_state(
            {"total_inverters": 3, "reporting_inverters": 0, "unknown_inverters": 1}
        )
        == "degraded"
    )


def test_microinverter_snapshot_defaults_unknown_when_status_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.type_bucket = lambda _type_key: {  # type: ignore[assignment]
        "count": 2,
        "devices": [{"serial_number": "INV-A"}, {"serial_number": "INV-B"}],
    }

    snapshot = sensor_mod._microinverter_inventory_snapshot(coord)
    assert snapshot["total_inverters"] == 2
    assert snapshot["reporting_inverters"] == 0
    assert snapshot["not_reporting_inverters"] == 0
    assert snapshot["unknown_inverters"] == 2
    assert snapshot["connectivity_state"] == "unknown"


def test_microinverter_snapshot_clamps_unknown_overflow(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.type_bucket = lambda _type_key: {  # type: ignore[assignment]
        "count": 1,
        "devices": [{"serial_number": "INV-A"}],
        "status_counts": {"total": 1, "not_reporting": 1, "unknown": 2},
    }

    snapshot = sensor_mod._microinverter_inventory_snapshot(coord)
    assert snapshot["total_inverters"] == 1
    assert snapshot["not_reporting_inverters"] == 1
    assert snapshot["unknown_inverters"] == 0
    assert snapshot["connectivity_state"] == "offline"


def test_microinverter_reporting_count_attributes_fallbacks(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseMicroinverterReportingCountSensor

    class BadInt:
        def __int__(self) -> int:
            raise ValueError("bad")

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.type_bucket = lambda _type_key: {  # type: ignore[assignment]
        "count": BadInt(),
        "type_label": " ",
        "devices": [{"serial_number": "INV-A"}],
    }
    coord.type_label = lambda _type_key: "Microinverters Fallback"  # type: ignore[assignment]

    sensor = EnphaseMicroinverterReportingCountSensor(coord)
    attrs = sensor.extra_state_attributes
    assert attrs["device_count"] == 1
    assert attrs["type_label"] == "Microinverters Fallback"
    assert "devices" in sensor._unrecorded_attributes
    assert "panel_info" in sensor._unrecorded_attributes

    coord.type_label = lambda _type_key: " "  # type: ignore[assignment]
    attrs = EnphaseMicroinverterReportingCountSensor(coord).extra_state_attributes
    assert attrs["type_label"] == "Microinverters"


def test_microinverter_sensor_available_branches(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseMicroinverterConnectivityStatusSensor,
        EnphaseMicroinverterLastReportedSensor,
        EnphaseMicroinverterReportingCountSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "microinverter": {
                "type_key": "microinverter",
                "count": 1,
                "devices": [{"serial_number": "INV-A"}],
                "status_counts": {"total": 1, "not_reporting": 0},
            }
        },
        ["microinverter"],
    )
    coord.last_update_success = False
    coord.last_success_utc = None

    assert EnphaseMicroinverterConnectivityStatusSensor(coord).available is False
    assert EnphaseMicroinverterReportingCountSensor(coord).available is False
    assert EnphaseMicroinverterLastReportedSensor(coord).available is False

    coord.last_update_success = True
    assert EnphaseMicroinverterConnectivityStatusSensor(coord).available is True
    assert EnphaseMicroinverterReportingCountSensor(coord).available is True
    assert EnphaseMicroinverterLastReportedSensor(coord).available is False


def test_gateway_meter_sensors_expose_status_and_meter_attributes(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConsumptionMeterSensor,
        EnphaseGatewayProductionMeterSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 3,
                "devices": [
                    {
                        "name": "Production Meter",
                        "serial_number": "MTR-P",
                        "channel_type": "production_meter",
                        "statusText": "Normal",
                        "connected": "true",
                        "ip": "192.0.2.10",
                        "last_report": "2026-02-15T10:00:00Z",
                        "envoy_sw_version": "8.3.1",
                    },
                    {
                        "name": "Consumption Meter",
                        "serial_number": "MTR-C",
                        "channel_type": "consumption_meter",
                        "status": "NOT_REPORTING",
                        "connected": 0,
                        "ip": "192.0.2.11",
                    },
                    {"name": "System Controller", "channel_type": "enpower"},
                ],
            }
        },
        ["envoy"],
    )

    production = EnphaseGatewayProductionMeterSensor(coord)
    assert production.native_value == "Normal"
    assert production.entity_registry_enabled_default is True
    p_attrs = production.extra_state_attributes
    assert p_attrs["meter_name"] == "Production Meter"
    assert p_attrs["meter_type"] == "production"
    assert p_attrs["channel_type"] == "production_meter"
    assert p_attrs["connected"] is True
    assert p_attrs["envoy_sw_version"] == "8.3.1"
    assert p_attrs["meter_attributes"]["serial_number"] == "MTR-P"
    assert p_attrs["last_reported_utc"] is not None

    consumption = EnphaseGatewayConsumptionMeterSensor(coord)
    assert consumption.native_value == "Not Reporting"
    assert consumption.entity_registry_enabled_default is True
    c_attrs = consumption.extra_state_attributes
    assert c_attrs["meter_name"] == "Consumption Meter"
    assert c_attrs["meter_type"] == "consumption"
    assert c_attrs["channel_type"] == "consumption_meter"
    assert c_attrs["connected"] is False
    assert c_attrs["meter_attributes"]["serial_number"] == "MTR-C"
    assert "meter_attributes" in consumption._unrecorded_attributes  # noqa: SLF001
    assert "last_reported_utc" in consumption._unrecorded_attributes  # noqa: SLF001


def test_gateway_meter_sensor_name_fallback_and_missing_member(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConsumptionMeterSensor,
        EnphaseGatewayProductionMeterSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [
                    {
                        "name": "Production Meter",
                        "serial_number": "MTR-P",
                        "status": "NORMAL",
                    }
                ],
            }
        },
        ["envoy"],
    )

    production = EnphaseGatewayProductionMeterSensor(coord)
    assert production.available is True
    assert production.native_value == "Normal"

    consumption = EnphaseGatewayConsumptionMeterSensor(coord)
    assert consumption.available is False
    assert consumption.native_value is None
    assert consumption.extra_state_attributes == {}

    coord.last_update_success = False
    assert production.available is False


def test_system_controller_inventory_sensor_state_and_attributes(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSystemControllerInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 2,
                "devices": [
                    {
                        "name": "System Controller",
                        "serial_number": "SC-1",
                        "channel_type": "enpower",
                        "statusText": "Normal",
                        "connected": True,
                        "last_report": "2026-02-15T10:00:00Z",
                        "lastReportedAt": "2026-02-15T10:05:00Z",
                        "envoy_sw_version": "8.3.1",
                    },
                    {
                        "name": "Production Meter",
                        "channel_type": "production_meter",
                        "statusText": "Normal",
                    },
                ],
            }
        },
        ["envoy"],
    )

    sensor = EnphaseSystemControllerInventorySensor(coord)
    assert sensor.available is True
    assert sensor.native_value == "Normal"
    assert sensor.entity_registry_enabled_default is True
    attrs = sensor.extra_state_attributes
    assert attrs["name"] == "System Controller"
    assert attrs["channel_type"] == "enpower"
    assert attrs["status_text"] == "Normal"
    assert attrs["serial_number"] == "SC-1"
    assert attrs["connected"] is True
    assert attrs["envoy_sw_version"] == "8.3.1"
    assert "last_reported_utc" in attrs
    assert "last_reported_at" not in attrs
    assert "last_reported_utc" in sensor._unrecorded_attributes  # noqa: SLF001


def test_system_controller_inventory_sensor_missing_member_unavailable(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSystemControllerInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "Production Meter", "channel_type": "production_meter"}],
            }
        },
        ["envoy"],
    )
    sensor = EnphaseSystemControllerInventorySensor(coord)
    assert sensor.available is False
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            }
        },
        ["envoy"],
    )
    coord.last_update_success = False
    assert sensor.available is False


def test_gateway_helpers_cover_edge_paths(coordinator_factory) -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    class BadFloat(float):
        def __float__(self) -> float:
            raise ValueError("boom")

    class BadInt:
        def __int__(self) -> int:
            raise ValueError("boom")

    assert sensor_mod._gateway_clean_text(BadStr()) is None
    assert sensor_mod._gateway_optional_bool(True) is True
    assert sensor_mod._gateway_optional_bool(0) is False
    assert sensor_mod._gateway_optional_bool("enable") is True
    assert sensor_mod._gateway_optional_bool("disable") is False
    assert sensor_mod._gateway_optional_bool("maybe") is None
    assert sensor_mod._gateway_channel_type_kind("production_meter") == "production"
    assert sensor_mod._gateway_channel_type_kind("consumption meter") == "consumption"
    assert sensor_mod._gateway_channel_type_kind("unknown") is None
    assert sensor_mod._gateway_channel_type_kind(BadStr()) is None
    assert sensor_mod._gateway_attr_key("statusText") == "status_text"
    assert sensor_mod._gateway_attr_key("Last-Report") == "last_report"
    assert sensor_mod._gateway_attr_key(BadStr()) is None
    assert sensor_mod._gateway_flat_member_attributes(
        {
            "statusText": "Normal",
            "empty": " ",
            "count": 1,
            "nested": {"bad": True},
            "nullable": None,
        },
        skip_keys={"count"},
    ) == {"status_text": "Normal"}
    assert sensor_mod._gateway_normalize_status(None) == "unknown"
    assert sensor_mod._gateway_normalize_status("critical fault") == "error"
    assert sensor_mod._gateway_normalize_status("mystery") == "unknown"

    aware = datetime(2026, 2, 15, 10, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 2, 15, 10, 0)
    assert sensor_mod._gateway_parse_timestamp(aware) == aware
    assert sensor_mod._gateway_parse_timestamp(naive) == aware
    assert sensor_mod._gateway_parse_timestamp(BadFloat(1.0)) is None
    assert sensor_mod._gateway_parse_timestamp(" ") is None
    assert sensor_mod._gateway_parse_timestamp("not-a-timestamp") is None
    parsed_naive = sensor_mod._gateway_parse_timestamp("2026-02-15T10:00:00")
    assert parsed_naive is not None
    assert parsed_naive.tzinfo is not None
    assert sensor_mod._gateway_parse_timestamp({}) is None
    assert sensor_mod._gateway_parse_timestamp(float("inf")) is None
    assert sensor_mod._battery_parse_timestamp(0) is None
    assert sensor_mod._battery_parse_timestamp("1e20") is None
    parsed_epoch = sensor_mod._battery_parse_timestamp(1_771_144_293_000)
    assert parsed_epoch == datetime(2026, 2, 15, 8, 31, 33, tzinfo=timezone.utc)
    parsed_battery_naive = sensor_mod._battery_parse_timestamp(
        datetime(2026, 2, 15, 8, 31, 33)
    )
    assert parsed_battery_naive == datetime(
        2026, 2, 15, 8, 31, 33, tzinfo=timezone.utc
    )
    assert sensor_mod._battery_optional_bool(1) is True
    assert sensor_mod._battery_optional_bool("disabled") is False
    assert sensor_mod._battery_optional_bool("maybe") is None
    assert sensor_mod._EnphaseBatteryStorageBaseSensor._parse_timestamp(
        "2026-02-15T08:31:33Z"
    ) == datetime(2026, 2, 15, 8, 31, 33, tzinfo=timezone.utc)
    members = sensor_mod._battery_last_reported_members(
        SimpleNamespace(
            battery_status_payload={
                "storages": [
                    "bad",
                    {"serial_number": "BAT-X", "excluded": "true"},
                    {"serial_number": "BAT-1", "last_report": 1_771_144_293},
                ]
            }
        )
    )
    assert members == [{"serial_number": "BAT-1", "last_report": 1_771_144_293}]

    assert sensor_mod._gateway_format_counts({"": 2, "A": BadInt(), "B": 0}) is None
    assert sensor_mod._gateway_format_counts({"B": 2, "A": 2}) == "A x2, B x2"

    assert sensor_mod._gateway_meter_status_text(None) is None
    assert (
        sensor_mod._gateway_meter_status_text({"statusText": "Normal"})
        == "Normal"
    )
    assert (
        sensor_mod._gateway_meter_status_text({"status": "NOT_REPORTING"})
        == "Not Reporting"
    )
    assert sensor_mod._gateway_meter_status_text({"status": " "}) is None
    assert sensor_mod._gateway_meter_last_reported(None) is None
    assert (
        sensor_mod._gateway_meter_last_reported({"last_report": "invalid"})
        is None
    )
    parsed_report = sensor_mod._gateway_meter_last_reported(
        {"last_reported": "2026-02-15T10:00:00Z"}
    )
    assert parsed_report is not None
    assert sensor_mod._title_case_status("not_reporting") == "Not Reporting"
    assert sensor_mod._title_case_status("_") is None

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.type_bucket = lambda _key: {  # type: ignore[assignment]
        "count": BadInt(),
        "devices": [
            {"status": "normal", "connected": None, "model": "IQ Gateway"},
            {"statusText": "not reporting", "connected": None, "sw_version": "8.2.0"},
            {"status": "mystery", "connected": "maybe"},
        ],
    }
    snapshot = sensor_mod._gateway_inventory_snapshot(coord)
    assert snapshot["total_devices"] == 3
    assert snapshot["connected_devices"] == 1
    assert snapshot["disconnected_devices"] == 1
    assert snapshot["unknown_connection_devices"] == 1
    assert snapshot["model_summary"] == "IQ Gateway x1"
    assert snapshot["firmware_summary"] == "8.2.0 x1"
    meter_member = sensor_mod._gateway_meter_member(coord, "production")
    assert meter_member is None

    coord.type_bucket = lambda _key: {  # type: ignore[assignment]
        "count": 2,
        "devices": [
            {"channel_type": "production_meter", "name": "Production Meter"},
            {"name": "Consumption Meter"},
        ],
    }
    assert sensor_mod._gateway_meter_member(coord, "production") is not None
    assert sensor_mod._gateway_meter_member(coord, "consumption") is not None
    coord.type_bucket = lambda _key: {"devices": "bad"}  # type: ignore[assignment]
    assert sensor_mod._gateway_meter_member(coord, "production") is None
    coord.type_bucket = lambda _key: {"devices": ["bad"]}  # type: ignore[assignment]
    assert sensor_mod._gateway_meter_member(coord, "production") is None
    coord.type_bucket = lambda _key: {"devices": "bad"}  # type: ignore[assignment]
    assert sensor_mod._gateway_system_controller_member(coord) is None
    coord.type_bucket = lambda _key: {"devices": ["bad"]}  # type: ignore[assignment]
    assert sensor_mod._gateway_system_controller_member(coord) is None
    coord.type_bucket = lambda _key: {  # type: ignore[assignment]
        "devices": [{"name": "System Controller (Main)"}]
    }
    assert sensor_mod._gateway_system_controller_member(coord) is not None

    assert (
        sensor_mod._gateway_connectivity_state(
            {
                "total_devices": 2,
                "connected_devices": 2,
                "disconnected_devices": 0,
                "unknown_connection_devices": 0,
            }
        )
        == "online"
    )
    assert (
        sensor_mod._gateway_connectivity_state(
            {
                "total_devices": 2,
                "connected_devices": 0,
                "disconnected_devices": 1,
                "unknown_connection_devices": 1,
            }
        )
        == "offline"
    )
    assert (
        sensor_mod._gateway_connectivity_state(
            {
                "total_devices": 2,
                "connected_devices": 0,
                "disconnected_devices": 0,
                "unknown_connection_devices": 2,
            }
        )
        == "unknown"
    )
    assert (
        sensor_mod._gateway_connectivity_state(
            {
                "total_devices": 2,
                "connected_devices": 0,
                "disconnected_devices": 0,
                "unknown_connection_devices": 1,
            }
        )
        == "degraded"
    )


def test_gateway_diagnostic_sensor_availability_paths(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConnectivityStatusSensor,
        EnphaseGatewayLastReportedSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {"envoy": {"count": 1, "devices": [{"name": "GW"}]}}  # noqa: SLF001
    coord.last_update_success = False
    coord.last_success_utc = None

    assert EnphaseGatewayConnectivityStatusSensor(coord).available is False
    assert EnphaseGatewayLastReportedSensor(coord).available is False

    coord.last_update_success = True
    coord._devices_inventory_ready = False  # noqa: SLF001
    coord._type_device_buckets = {"envoy": {"count": 0, "devices": []}}  # noqa: SLF001
    assert EnphaseGatewayConnectivityStatusSensor(coord).available is True

    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {"envoy": {"count": 1, "devices": [{"name": "GW"}]}}  # noqa: SLF001
    assert EnphaseGatewayLastReportedSensor(coord).available is False

    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"name": "GW", "status": "normal", "last_report": "2026-02-15T10:00:00Z"}],
        }
    }
    assert EnphaseGatewayConnectivityStatusSensor(coord).available is True
    assert EnphaseGatewayLastReportedSensor(coord).available is True


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
        EnphaseGatewayConsumptionMeterSensor,
        EnphaseMicroinverterConnectivityStatusSensor,
        EnphaseMicroinverterLastReportedSensor,
        EnphaseMicroinverterReportingCountSensor,
        EnphaseGatewayProductionMeterSensor,
        EnphaseSystemControllerInventorySensor,
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
    assert any(isinstance(ent, EnphaseSystemControllerInventorySensor) for ent in added)
    assert any(isinstance(ent, EnphaseGatewayProductionMeterSensor) for ent in added)
    assert any(isinstance(ent, EnphaseGatewayConsumptionMeterSensor) for ent in added)
    assert any(
        isinstance(ent, EnphaseMicroinverterConnectivityStatusSensor) for ent in added
    )
    assert any(isinstance(ent, EnphaseMicroinverterReportingCountSensor) for ent in added)
    assert any(isinstance(ent, EnphaseMicroinverterLastReportedSensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_microinverter_site_entities_without_gateway_type(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConsumptionMeterSensor,
        EnphaseGatewayProductionMeterSensor,
        EnphaseMicroinverterConnectivityStatusSensor,
        EnphaseMicroinverterLastReportedSensor,
        EnphaseMicroinverterReportingCountSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 1,
                "devices": [{"serial_number": "INV-A"}],
                "status_counts": {"total": 1, "unknown": 1},
            }
        },
        ["microinverter"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(
        isinstance(ent, EnphaseMicroinverterConnectivityStatusSensor) for ent in added
    )
    assert any(isinstance(ent, EnphaseMicroinverterReportingCountSensor) for ent in added)
    assert any(isinstance(ent, EnphaseMicroinverterLastReportedSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseGatewayProductionMeterSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseGatewayConsumptionMeterSensor) for ent in added)


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
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 1,
                "devices": [{"name": "Inverter 1"}],
            },
        },
        ["wind_turbine", "encharge", "microinverter"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    type_entities = [ent for ent in added if isinstance(ent, EnphaseTypeInventorySensor)]
    assert len(type_entities) == 2
    assert not any(ent._type_key == "microinverter" for ent in type_entities)  # noqa: SLF001
    wind = next(ent for ent in type_entities if ent._type_key == "wind_turbine")  # noqa: SLF001
    assert wind.native_value == 2
    assert wind.extra_state_attributes["type_label"] == "Wind Turbine"
    assert wind.device_info["name"] == "Wind Turbine"


@pytest.mark.asyncio
async def test_async_setup_entry_skips_gateway_inventory_sensor(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseTypeInventorySensor, async_setup_entry

    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"name": "Battery 1"}],
            },
        },
        ["envoy", "encharge"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    type_entities = [ent for ent in added if isinstance(ent, EnphaseTypeInventorySensor)]
    assert len(type_entities) == 1
    assert type_entities[0]._type_key == "encharge"  # noqa: SLF001


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
