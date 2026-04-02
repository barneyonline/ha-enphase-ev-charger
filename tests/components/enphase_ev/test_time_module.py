from __future__ import annotations

from datetime import time as dt_time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.time import (
    _migrated_time_entity_id,
    ChargeFromGridEndTimeEntity,
    ChargeFromGridStartTimeEntity,
    DischargeToGridEndTimeEntity,
    DischargeToGridStartTimeEntity,
    RestrictBatteryDischargeEndTimeEntity,
    RestrictBatteryDischargeStartTimeEntity,
    async_setup_entry,
)


def test_time_type_available_falls_back_to_has_type() -> None:
    from custom_components.enphase_ev import time as time_mod

    coord = SimpleNamespace(has_type=lambda type_key: type_key == "encharge")
    assert time_mod._type_available(coord, "encharge") is True
    assert time_mod._type_available(coord, "envoy") is False

    coord_no_helpers = SimpleNamespace()
    assert time_mod._type_available(coord_no_helpers, "encharge") is True


def test_cfg_schedule_edit_available_handles_supported_and_existing_windows() -> None:
    from custom_components.enphase_ev import time as time_mod

    coord = SimpleNamespace(
        charge_from_grid_schedule_available=False,
        charge_from_grid_control_available=False,
        charge_from_grid_schedule_supported=False,
        _battery_cfg_schedule_id=None,
        battery_charge_from_grid_start_time=None,
        battery_charge_from_grid_end_time=None,
        _battery_charge_begin_time=None,
        _battery_charge_end_time=None,
    )

    assert time_mod._cfg_schedule_edit_available(coord) is False

    coord.charge_from_grid_control_available = True
    assert time_mod._cfg_schedule_edit_available(coord) is False

    coord.charge_from_grid_schedule_supported = True
    coord.battery_charge_from_grid_start_time = dt_time(1, 0)
    coord.battery_charge_from_grid_end_time = dt_time(2, 0)

    assert time_mod._cfg_schedule_edit_available(coord) is False

    coord._battery_cfg_schedule_id = "sched-1"
    coord.battery_charge_from_grid_start_time = None
    coord.battery_charge_from_grid_end_time = None
    coord._battery_charge_begin_time = 60
    coord._battery_charge_end_time = 120

    assert time_mod._cfg_schedule_edit_available(coord) is True


def test_dtg_and_rbd_schedule_edit_available_cover_control_window_fallbacks() -> None:
    from custom_components.enphase_ev import time as time_mod

    dtg = SimpleNamespace(
        discharge_to_grid_schedule_available=False,
        discharge_to_grid_schedule_supported=True,
        battery_discharge_to_grid_start_time=None,
        battery_discharge_to_grid_end_time=None,
        _battery_dtg_begin_time=None,
        _battery_dtg_end_time=None,
        _battery_dtg_control_begin_time=60,
        _battery_dtg_control_end_time=120,
    )
    assert time_mod._dtg_schedule_edit_available(dtg) is True
    dtg.discharge_to_grid_schedule_supported = False
    assert time_mod._dtg_schedule_edit_available(dtg) is False

    rbd = SimpleNamespace(
        restrict_battery_discharge_schedule_available=False,
        restrict_battery_discharge_schedule_supported=True,
        battery_restrict_battery_discharge_start_time=None,
        battery_restrict_battery_discharge_end_time=None,
        _battery_rbd_begin_time=None,
        _battery_rbd_end_time=None,
        _battery_rbd_control_begin_time=60,
        _battery_rbd_control_end_time=120,
    )
    assert time_mod._rbd_schedule_edit_available(rbd) is True
    rbd._battery_rbd_control_end_time = None
    assert time_mod._rbd_schedule_edit_available(rbd) is False

    rbd.restrict_battery_discharge_schedule_supported = False
    assert time_mod._rbd_schedule_edit_available(rbd) is False

    rbd.restrict_battery_discharge_schedule_supported = True
    rbd.battery_restrict_battery_discharge_start_time = dt_time(1, 0)
    rbd.battery_restrict_battery_discharge_end_time = dt_time(2, 0)
    assert time_mod._rbd_schedule_edit_available(rbd) is True


def test_retained_site_time_unique_ids_cover_each_schedule_family() -> None:
    from custom_components.enphase_ev import time as time_mod

    coord = SimpleNamespace(
        site_id="site",
        has_type=lambda type_key: type_key == "encharge",
        charge_from_grid_schedule_available=False,
        charge_from_grid_control_available=True,
        charge_from_grid_schedule_supported=True,
        _battery_cfg_schedule_id="sched-cfg",
        battery_charge_from_grid_start_time=dt_time(1, 0),
        battery_charge_from_grid_end_time=dt_time(2, 0),
        _battery_charge_begin_time=None,
        _battery_charge_end_time=None,
        discharge_to_grid_schedule_available=False,
        discharge_to_grid_schedule_supported=True,
        battery_discharge_to_grid_start_time=dt_time(3, 0),
        battery_discharge_to_grid_end_time=dt_time(4, 0),
        _battery_dtg_begin_time=None,
        _battery_dtg_end_time=None,
        _battery_dtg_control_begin_time=None,
        _battery_dtg_control_end_time=None,
        restrict_battery_discharge_schedule_available=False,
        restrict_battery_discharge_schedule_supported=True,
        battery_restrict_battery_discharge_start_time=dt_time(5, 0),
        battery_restrict_battery_discharge_end_time=dt_time(6, 0),
        _battery_rbd_begin_time=None,
        _battery_rbd_end_time=None,
        _battery_rbd_control_begin_time=None,
        _battery_rbd_control_end_time=None,
    )

    assert time_mod._retained_site_time_unique_ids(coord) == {
        "enphase_ev_site_site_charge_from_grid_start_time",
        "enphase_ev_site_site_charge_from_grid_end_time",
        "enphase_ev_site_site_discharge_to_grid_start_time",
        "enphase_ev_site_site_discharge_to_grid_end_time",
        "enphase_ev_site_site_restrict_battery_discharge_start_time",
        "enphase_ev_site_site_restrict_battery_discharge_end_time",
    }

    coord.has_type = lambda _type_key: False
    assert time_mod._retained_site_time_unique_ids(coord) == set()


def test_migrated_time_entity_id_handles_strong_migration_and_auto_suffix() -> None:
    assert (
        _migrated_time_entity_id(
            "time.charge_from_grid_start_time",
            "time.charge_from_grid_start_time",
            "time.charge_from_grid_schedule_from_time",
        )
        == "time.charge_from_grid_schedule_from_time"
    )
    assert (
        _migrated_time_entity_id(
            "time.charge_from_grid_start_time_2",
            "time.charge_from_grid_start_time",
            "time.charge_from_grid_schedule_from_time",
        )
        == "time.charge_from_grid_schedule_from_time_2"
    )
    assert (
        _migrated_time_entity_id(
            "time.charge_from_grid_start_time_custom",
            "time.charge_from_grid_start_time",
            "time.charge_from_grid_schedule_from_time",
        )
        == "time.charge_from_grid_schedule_from_time"
    )
    assert (
        _migrated_time_entity_id(
            "time.my_custom_from",
            "time.charge_from_grid_start_time",
            "time.charge_from_grid_schedule_from_time",
        )
        == "time.charge_from_grid_schedule_from_time"
    )
    assert (
        _migrated_time_entity_id(
            "time.charge_from_grid_schedule_from_time",
            "time.charge_from_grid_start_time",
            "time.charge_from_grid_schedule_from_time",
        )
        is None
    )
    assert (
        _migrated_time_entity_id(
            "time.custom_from_4",
            "time.charge_from_grid_start_time",
            "time.charge_from_grid_schedule_from_time",
        )
        == "time.charge_from_grid_schedule_from_time_4"
    )
    assert (
        _migrated_time_entity_id(
            "time.charge_from_grid_schedule_from_time_4",
            "time.charge_from_grid_start_time",
            "time.charge_from_grid_schedule_from_time",
        )
        is None
    )


@pytest.mark.asyncio
async def test_async_setup_entry_adds_site_time_entities(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(ent, ChargeFromGridStartTimeEntity) for ent in added)
    assert any(isinstance(ent, ChargeFromGridEndTimeEntity) for ent in added)
    assert any(isinstance(ent, DischargeToGridStartTimeEntity) for ent in added)
    assert any(isinstance(ent, DischargeToGridEndTimeEntity) for ent in added)
    assert any(
        isinstance(ent, RestrictBatteryDischargeStartTimeEntity) for ent in added
    )
    assert any(isinstance(ent, RestrictBatteryDischargeEndTimeEntity) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_falls_back_to_generic_listener_for_time_entities(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    callbacks: list = []

    monkeypatch.setattr(coord, "async_add_topology_listener", None, raising=False)
    monkeypatch.setattr(
        coord,
        "async_add_listener",
        lambda callback: callbacks.append(callback) or (lambda: None),
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert callbacks


@pytest.mark.asyncio
async def test_async_setup_entry_migrates_charge_from_grid_time_entity_ids(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    start_unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_start_time"
    end_unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_end_time"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id=start_unique_id,
            entity_id="time.charge_from_grid_start_time",
        ),
        SimpleNamespace(
            unique_id=end_unique_id,
            entity_id="time.charge_from_grid_end_time",
        ),
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_any_call(
        "time.charge_from_grid_start_time",
        new_entity_id="time.charge_from_grid_schedule_from_time",
    )
    fake_registry.async_update_entity.assert_any_call(
        "time.charge_from_grid_end_time",
        new_entity_id="time.charge_from_grid_schedule_to_time",
    )


@pytest.mark.asyncio
async def test_async_setup_entry_migration_handles_rename_conflict(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    start_unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_start_time"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock(side_effect=ValueError("duplicate"))
    entries = [
        SimpleNamespace(
            unique_id=start_unique_id,
            entity_id="time.charge_from_grid_start_time",
        )
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_called_once_with(
        "time.charge_from_grid_start_time",
        new_entity_id="time.charge_from_grid_schedule_from_time",
    )


@pytest.mark.asyncio
async def test_async_setup_entry_migration_ignores_unrelated_unique_ids(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id="enphase_ev_site_other_unrelated",
            entity_id="time.custom_entity",
        )
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_migration_renames_custom_entity_ids(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    start_unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_start_time"
    end_unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_end_time"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id=start_unique_id,
            entity_id="time.charge_from_grid_start_time_custom",
        ),
        SimpleNamespace(
            unique_id=end_unique_id,
            entity_id="time.charge_from_grid_end_time_custom",
        ),
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_any_call(
        "time.charge_from_grid_start_time_custom",
        new_entity_id="time.charge_from_grid_schedule_from_time",
    )
    fake_registry.async_update_entity.assert_any_call(
        "time.charge_from_grid_end_time_custom",
        new_entity_id="time.charge_from_grid_schedule_to_time",
    )


@pytest.mark.asyncio
async def test_async_setup_entry_time_cleanup_waits_for_inventory_ready(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    coord._devices_inventory_ready = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "time",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_charge_from_grid_start_time",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    remove_spy.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_migrates_auto_suffixed_legacy_entity_ids(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    start_unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_start_time"
    end_unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_end_time"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id=start_unique_id,
            entity_id="time.charge_from_grid_start_time_2",
        ),
        SimpleNamespace(
            unique_id=end_unique_id,
            entity_id="time.charge_from_grid_end_time_3",
        ),
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_any_call(
        "time.charge_from_grid_start_time_2",
        new_entity_id="time.charge_from_grid_schedule_from_time_2",
    )
    fake_registry.async_update_entity.assert_any_call(
        "time.charge_from_grid_end_time_3",
        new_entity_id="time.charge_from_grid_schedule_to_time_3",
    )


@pytest.mark.asyncio
async def test_async_setup_entry_migration_skips_already_migrated_entity_ids(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    start_unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_start_time"
    end_unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_end_time"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id=start_unique_id,
            entity_id="time.charge_from_grid_schedule_from_time",
        ),
        SimpleNamespace(
            unique_id=end_unique_id,
            entity_id="time.charge_from_grid_schedule_to_time",
        ),
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.time.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_does_not_duplicate_site_time_entities_on_listener(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    callbacks: list = []

    def _capture_listener(callback):
        callbacks.append(callback)
        return lambda: None

    coord.async_add_topology_listener = _capture_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)
    assert (
        len([ent for ent in added if isinstance(ent, ChargeFromGridStartTimeEntity)])
        == 1
    )
    assert (
        len([ent for ent in added if isinstance(ent, ChargeFromGridEndTimeEntity)]) == 1
    )
    assert callbacks

    callbacks[0]()
    assert (
        len([ent for ent in added if isinstance(ent, ChargeFromGridStartTimeEntity)])
        == 1
    )
    assert (
        len([ent for ent in added if isinstance(ent, ChargeFromGridEndTimeEntity)]) == 1
    )


@pytest.mark.asyncio
async def test_async_setup_entry_skips_site_time_entities_without_battery(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(ent, ChargeFromGridStartTimeEntity) for ent in added)
    assert not any(isinstance(ent, ChargeFromGridEndTimeEntity) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_stale_time_entities_when_inventory_ready(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    coord._devices_inventory_ready = True  # noqa: SLF001
    callbacks: list = []

    def _capture_listener(callback):
        callbacks.append(callback)
        return lambda: None

    coord.async_add_topology_listener = _capture_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "time",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_charge_from_grid_start_time",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)
    coord._battery_has_encharge = False  # noqa: SLF001
    callbacks[0]()

    assert remove_spy.call_count == 1
    removed_entity_id = remove_spy.call_args.args[0]
    assert removed_entity_id in {
        stale.entity_id,
        "time.charge_from_grid_schedule_from_time",
    }


@pytest.mark.asyncio
async def test_charge_from_grid_time_entity_availability_and_values(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001

    start = ChargeFromGridStartTimeEntity(coord)
    end = ChargeFromGridEndTimeEntity(coord)

    assert start.available is True
    assert end.available is True
    assert start.native_value == dt_time(2, 0)
    assert end.native_value == dt_time(5, 0)

    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_cfg_schedule_id = "sched-1"  # noqa: SLF001
    assert start.available is True
    assert end.available is True


@pytest.mark.asyncio
async def test_charge_from_grid_time_entity_available_from_capability_without_times(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = None  # noqa: SLF001
    coord._battery_charge_end_time = None  # noqa: SLF001
    coord._battery_cfg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "forceScheduleSupported": False,
            }
        )
    )

    start = ChargeFromGridStartTimeEntity(coord)
    end = ChargeFromGridEndTimeEntity(coord)

    assert start.available is True
    assert end.available is True
    assert start.native_value is None
    assert end.native_value is None


def test_charge_from_grid_time_entity_unavailable_when_coordinator_down(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.last_update_success = False
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001

    start = ChargeFromGridStartTimeEntity(coord)
    assert start.available is False


def test_charge_from_grid_time_entity_suggested_object_ids(coordinator_factory) -> None:
    coord = coordinator_factory()
    start = ChargeFromGridStartTimeEntity(coord)
    end = ChargeFromGridEndTimeEntity(coord)

    assert start.suggested_object_id == "charge_from_grid_schedule_from_time"
    assert end.suggested_object_id == "charge_from_grid_schedule_to_time"


@pytest.mark.asyncio
async def test_base_named_battery_schedule_time_entity_fallbacks(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev import time as time_mod

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord.type_device_info = None
    coord.async_custom_schedule_time = AsyncMock()
    coord.custom_schedule_time = dt_time(3, 15)

    entity = time_mod._BaseNamedBatteryScheduleTimeEntity(
        coord,
        suffix="custom_schedule_time",
        availability_check=lambda _: True,
        value_attr="custom_schedule_time",
        setter_name="async_custom_schedule_time",
        suggested_object_id="custom_schedule_time",
    )

    assert entity.available is True
    assert entity.native_value == dt_time(3, 15)
    assert entity._schedule_endpoint_key() == "start"  # noqa: SLF001
    await entity.async_set_value(dt_time(4, 30))
    coord.async_custom_schedule_time.assert_awaited_once_with(start=dt_time(4, 30))
    assert entity.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }

    expected = {"identifiers": {("enphase_ev", "provided")}}
    coord.type_device_info = MagicMock(return_value=expected)
    assert entity.device_info is expected

    coord.last_update_success = False
    assert entity.available is False


def test_charge_from_grid_time_entity_device_info_fallback(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.type_device_info = None

    entity = ChargeFromGridStartTimeEntity(coord)

    assert entity.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }


def test_charge_from_grid_time_entity_uses_type_device_info(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    expected = {"identifiers": {("enphase_ev", "provided")}}
    coord.type_device_info = MagicMock(return_value=expected)

    assert ChargeFromGridStartTimeEntity(coord).device_info is expected


@pytest.mark.asyncio
async def test_charge_from_grid_time_entity_sets_value(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    coord.async_set_charge_from_grid_schedule_time = AsyncMock()

    start = ChargeFromGridStartTimeEntity(coord)
    end = ChargeFromGridEndTimeEntity(coord)

    await start.async_set_value(dt_time(1, 30))
    coord.async_set_charge_from_grid_schedule_time.assert_awaited_with(
        start=dt_time(1, 30)
    )

    await end.async_set_value(dt_time(4, 45))
    coord.async_set_charge_from_grid_schedule_time.assert_awaited_with(
        end=dt_time(4, 45)
    )


@pytest.mark.asyncio
async def test_charge_from_grid_time_entity_updates_existing_schedule_when_cfg_disabled(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_begin_time = 180  # noqa: SLF001
    coord._battery_charge_end_time = 960  # noqa: SLF001
    coord._battery_cfg_schedule_id = "sched-1"  # noqa: SLF001
    coord._battery_cfg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "forceScheduleSupported": False,
            }
        )
    )
    coord.async_update_cfg_schedule = AsyncMock()

    start = ChargeFromGridStartTimeEntity(coord)
    end = ChargeFromGridEndTimeEntity(coord)

    assert start.available is True
    assert end.available is True

    await start.async_set_value(dt_time(1, 30))
    coord.async_update_cfg_schedule.assert_awaited_with(start=dt_time(1, 30))

    await end.async_set_value(dt_time(4, 45))
    coord.async_update_cfg_schedule.assert_awaited_with(end=dt_time(4, 45))


@pytest.mark.asyncio
async def test_discharge_to_grid_time_entities_availability_and_setters(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_dtg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_dtg_schedule_id = "sched-dtg"  # noqa: SLF001
    coord._battery_dtg_begin_time = 1080  # noqa: SLF001
    coord._battery_dtg_end_time = 1380  # noqa: SLF001
    coord.async_set_discharge_to_grid_schedule_time = AsyncMock()

    start = DischargeToGridStartTimeEntity(coord)
    end = DischargeToGridEndTimeEntity(coord)

    assert start.available is True
    assert end.available is True
    assert start.native_value == dt_time(18, 0)
    assert end.native_value == dt_time(23, 0)
    assert start.suggested_object_id == "discharge_to_grid_schedule_from_time"
    assert end.suggested_object_id == "discharge_to_grid_schedule_to_time"

    await start.async_set_value(dt_time(17, 30))
    coord.async_set_discharge_to_grid_schedule_time.assert_awaited_with(
        start=dt_time(17, 30)
    )

    await end.async_set_value(dt_time(22, 45))
    coord.async_set_discharge_to_grid_schedule_time.assert_awaited_with(
        end=dt_time(22, 45)
    )


def test_discharge_to_grid_time_entities_available_from_control_window(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord.battery_runtime.parse_battery_settings_payload(
        {
            "data": {
                "batteryGridMode": "ImportExport",
                "dtgControl": {
                    "show": True,
                    "showDaySchedule": True,
                    "scheduleSupported": True,
                    "enabled": False,
                    "locked": False,
                    "startTime": 1140,
                    "endTime": 1320,
                },
            }
        }
    )

    start = DischargeToGridStartTimeEntity(coord)
    end = DischargeToGridEndTimeEntity(coord)

    assert start.available is True
    assert end.available is True
    assert start.native_value == dt_time(19, 0)
    assert end.native_value == dt_time(22, 0)


@pytest.mark.asyncio
async def test_restrict_battery_discharge_time_entities_availability_and_setters(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_rbd_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_rbd_schedule_id = "sched-rbd"  # noqa: SLF001
    coord._battery_rbd_begin_time = 60  # noqa: SLF001
    coord._battery_rbd_end_time = 960  # noqa: SLF001
    coord.async_set_restrict_battery_discharge_schedule_time = AsyncMock()

    start = RestrictBatteryDischargeStartTimeEntity(coord)
    end = RestrictBatteryDischargeEndTimeEntity(coord)

    assert start.available is True
    assert end.available is True
    assert start.native_value == dt_time(1, 0)
    assert end.native_value == dt_time(16, 0)
    assert start.suggested_object_id == "restrict_battery_discharge_schedule_from_time"
    assert end.suggested_object_id == "restrict_battery_discharge_schedule_to_time"

    await start.async_set_value(dt_time(2, 0))
    coord.async_set_restrict_battery_discharge_schedule_time.assert_awaited_with(
        start=dt_time(2, 0)
    )

    await end.async_set_value(dt_time(15, 30))
    coord.async_set_restrict_battery_discharge_schedule_time.assert_awaited_with(
        end=dt_time(15, 30)
    )
