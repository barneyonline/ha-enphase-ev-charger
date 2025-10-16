
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    serials = list(coord.serials or coord.data.keys())
    entities = []
    for sn in serials:
        entities.append(StartChargeButton(coord, sn))
        entities.append(StopChargeButton(coord, sn))
    async_add_entities(entities)

class _BaseButton(EnphaseBaseEntity, ButtonEntity):
    def __init__(self, coord: EnphaseCoordinator, sn: str, name_suffix: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_{name_suffix.replace(' ', '_').lower()}"

class StartChargeButton(_BaseButton):
    def __init__(self, coord, sn):
        super().__init__(coord, sn, "Start Charging")
        self._attr_translation_key = "start_charging"
    async def async_press(self) -> None:
        # Use last requested amps or default to 32A
        amps = int(self._coord.last_set_amps.get(self._sn) or 32)
        await self._coord.client.start_charging(self._sn, amps)
        self._coord.set_last_set_amps(self._sn, amps)
        # Poll quickly for a short window to reflect new state
        self._coord.kick_fast(90)
        await self._coord.async_request_refresh()

class StopChargeButton(_BaseButton):
    def __init__(self, coord, sn):
        super().__init__(coord, sn, "Stop Charging")
        self._attr_translation_key = "stop_charging"
    async def async_press(self) -> None:
        await self._coord.client.stop_charging(self._sn)
        # Poll quickly after stop to clear state faster
        self._coord.kick_fast(60)
        await self._coord.async_request_refresh()
