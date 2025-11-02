from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

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


class ChargingSwitch(EnphaseBaseEntity, RestoreEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charging"
    # Main feature of the device; let entity name equal device name
    _attr_name = None

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charging_switch"
        self._restored_state: bool | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            desired = last_state.state == STATE_ON
            self._restored_state = desired
            self._coord.set_desired_charging(self._sn, desired)
            if desired and not self.is_on:
                self._coord.kick_fast(60)
                try:
                    await self._coord.async_request_refresh()
                except Exception:  # noqa: BLE001
                    return
            self.async_write_ha_state()
        else:
            if self.available:
                self._coord.set_desired_charging(self._sn, self.is_on)
                self._restored_state = self.is_on

    @property
    def is_on(self) -> bool:
        if not self.available and self._restored_state is not None:
            return self._restored_state
        return bool(self.data.get("charging"))

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_start_charging(self._sn)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_stop_charging(self._sn)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._restored_state = None
        super()._handle_coordinator_update()
