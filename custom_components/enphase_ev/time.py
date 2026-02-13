from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .runtime_data import EnphaseConfigEntry, get_runtime_data

PARALLEL_UPDATES = 0


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    return has_encharge is not False


def _type_available(coord: EnphaseCoordinator, type_key: str) -> bool:
    has_type_for_entities = getattr(coord, "has_type_for_entities", None)
    if callable(has_type_for_entities):
        return bool(has_type_for_entities(type_key))
    has_type = getattr(coord, "has_type", None)
    return bool(has_type(type_key)) if callable(has_type) else True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    site_entities_added = False

    @callback
    def _async_sync_site_entities() -> None:
        nonlocal site_entities_added
        if site_entities_added:
            return
        if not _site_has_battery(coord) or not _type_available(coord, "encharge"):
            return
        async_add_entities(
            [ChargeFromGridStartTimeEntity(coord), ChargeFromGridEndTimeEntity(coord)],
            update_before_add=False,
        )
        site_entities_added = True

    unsubscribe = coord.async_add_listener(_async_sync_site_entities)
    entry.async_on_unload(unsubscribe)
    _async_sync_site_entities()


class _BaseChargeFromGridTimeEntity(CoordinatorEntity, TimeEntity):
    _attr_has_entity_name = True

    def __init__(self, coord: EnphaseCoordinator, suffix: str) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{suffix}"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and self._coord.charge_from_grid_schedule_available
        )

    @property
    def device_info(self) -> DeviceInfo:
        type_device_info = getattr(self._coord, "type_device_info", None)
        info = type_device_info("encharge") if callable(type_device_info) else None
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class ChargeFromGridStartTimeEntity(_BaseChargeFromGridTimeEntity):
    _attr_translation_key = "charge_from_grid_start_time"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "charge_from_grid_start_time")

    @property
    def native_value(self) -> dt_time | None:
        return self._coord.battery_charge_from_grid_start_time

    async def async_set_value(self, value: dt_time) -> None:
        await self._coord.async_set_charge_from_grid_schedule_time(start=value)


class ChargeFromGridEndTimeEntity(_BaseChargeFromGridTimeEntity):
    _attr_translation_key = "charge_from_grid_end_time"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "charge_from_grid_end_time")

    @property
    def native_value(self) -> dt_time | None:
        return self._coord.battery_charge_from_grid_end_time

    async def async_set_value(self, value: dt_time) -> None:
        await self._coord.async_set_charge_from_grid_schedule_time(end=value)
