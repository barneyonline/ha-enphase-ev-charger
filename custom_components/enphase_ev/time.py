from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnphaseCoordinator

PARALLEL_UPDATES = 0


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    if has_encharge is None:
        has_encharge = getattr(coord, "_battery_has_encharge", None)
    return has_encharge is not False


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    if _site_has_battery(coord):
        async_add_entities(
            [ChargeFromGridStartTimeEntity(coord), ChargeFromGridEndTimeEntity(coord)],
            update_before_add=False,
        )


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
        return self._coord.charge_from_grid_schedule_available

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"site:{self._coord.site_id}")},
            manufacturer="Enphase",
            model="Enlighten Cloud",
            name=f"Enphase Site {self._coord.site_id}",
            translation_key="enphase_site",
            translation_placeholders={"site_id": str(self._coord.site_id)},
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
