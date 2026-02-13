from __future__ import annotations

from datetime import time as dt_time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.time import (
    ChargeFromGridEndTimeEntity,
    ChargeFromGridStartTimeEntity,
    async_setup_entry,
)


def test_time_type_available_falls_back_to_has_type() -> None:
    from custom_components.enphase_ev import time as time_mod

    coord = SimpleNamespace(has_type=lambda type_key: type_key == "encharge")
    assert time_mod._type_available(coord, "encharge") is True
    assert time_mod._type_available(coord, "envoy") is False

    coord_no_helpers = SimpleNamespace()
    assert time_mod._type_available(coord_no_helpers, "encharge") is True


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


@pytest.mark.asyncio
async def test_async_setup_entry_does_not_duplicate_site_time_entities_on_listener(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    callbacks: list = []

    def _capture_listener(callback):
        callbacks.append(callback)
        return lambda: None

    coord.async_add_listener = _capture_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)
    assert len([ent for ent in added if isinstance(ent, ChargeFromGridStartTimeEntity)]) == 1
    assert len([ent for ent in added if isinstance(ent, ChargeFromGridEndTimeEntity)]) == 1
    assert callbacks

    callbacks[0]()
    assert len([ent for ent in added if isinstance(ent, ChargeFromGridStartTimeEntity)]) == 1
    assert len([ent for ent in added if isinstance(ent, ChargeFromGridEndTimeEntity)]) == 1


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
    assert start.available is False
    assert end.available is False


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
