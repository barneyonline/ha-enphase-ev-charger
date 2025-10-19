from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[SwitchEntity] = []
    serials = list(coord.serials or coord.data.keys())
    for sn in serials:
        entities.append(ChargingSwitch(coord, sn))
    async_add_entities(entities)


class ChargingSwitch(EnphaseBaseEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charging"
    # Main feature of the device; let entity name equal device name
    _attr_name = None

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charging_switch"

    @property
    def is_on(self) -> bool:
        return bool(self.data.get("charging"))

    async def async_turn_on(self, **kwargs) -> None:
        # Delegate amp selection to coordinator to honor charger limits
        amps = self._coord.pick_start_amps(self._sn)
        await self._coord.client.start_charging(self._sn, amps)
        self._coord.set_last_set_amps(self._sn, amps)
        self._coord.set_charging_expectation(self._sn, True, hold_for=90)
        self._coord.kick_fast(90)
        await self._coord.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.client.stop_charging(self._sn)
        self._coord.set_charging_expectation(self._sn, False, hold_for=90)
        self._coord.kick_fast(60)
        await self._coord.async_request_refresh()
