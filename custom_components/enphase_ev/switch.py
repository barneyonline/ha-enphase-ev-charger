from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    known_serials: set[str] = set()

    @callback
    def _async_sync_chargers() -> None:
        serials = [
            sn for sn in coord.iter_serials() if sn and sn not in known_serials
        ]
        if not serials:
            return
        entities: list[SwitchEntity] = [ChargingSwitch(coord, sn) for sn in serials]
        async_add_entities(entities, update_before_add=False)
        known_serials.update(serials)

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    _async_sync_chargers()


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
        self._coord.require_plugged(self._sn)
        amps = self._coord.pick_start_amps(self._sn)
        result = await self._coord.client.start_charging(self._sn, amps)
        self._coord.set_last_set_amps(self._sn, amps)
        if isinstance(result, dict) and result.get("status") == "not_ready":
            return
        self._coord.set_charging_expectation(self._sn, True, hold_for=90)
        self._coord.kick_fast(90)
        await self._coord.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.client.stop_charging(self._sn)
        self._coord.set_charging_expectation(self._sn, False, hold_for=90)
        self._coord.kick_fast(60)
        await self._coord.async_request_refresh()
