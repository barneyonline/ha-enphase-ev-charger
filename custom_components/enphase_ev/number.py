
from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    serials = list(coord.serials or coord.data.keys())
    entities = []
    for sn in serials:
        entities.append(ChargingAmpsNumber(coord, sn))
    async_add_entities(entities)

class ChargingAmpsNumber(EnphaseBaseEntity, NumberEntity):
    _attr_native_min_value = 6
    _attr_native_max_value = 40
    _attr_native_step = 1
    _attr_mode = "box"
    _attr_has_entity_name = True
    _attr_translation_key = "charging_amps"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_amps"

    async def async_set_native_value(self, value: float) -> None:
        # Use start_charging to apply amperage (cloud API)
        amps = int(value)
        await self._coord.client.start_charging(self._sn, amps)
        self._coord.set_last_set_amps(self._sn, amps)
        await self._coord.async_request_refresh()

    # available and device_info inherited from base
