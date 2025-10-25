from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
        entities = []
        for sn in serials:
            entities.append(StartChargeButton(coord, sn))
            entities.append(StopChargeButton(coord, sn))
        async_add_entities(entities, update_before_add=False)
        known_serials.update(serials)

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    _async_sync_chargers()


class _BaseButton(EnphaseBaseEntity, ButtonEntity):
    def __init__(self, coord: EnphaseCoordinator, sn: str, name_suffix: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_{name_suffix.replace(' ', '_').lower()}"


class StartChargeButton(_BaseButton):
    def __init__(self, coord, sn):
        super().__init__(coord, sn, "Start Charging")
        self._attr_translation_key = "start_charging"

    async def async_press(self) -> None:
        # Coordinator picks a safe amp value within device limits
        self._coord.require_plugged(self._sn)
        amps = self._coord.pick_start_amps(self._sn)
        result = await self._coord.client.start_charging(self._sn, amps)
        self._coord.set_last_set_amps(self._sn, amps)
        if isinstance(result, dict) and result.get("status") == "not_ready":
            self._coord.set_desired_charging(self._sn, False)
            return
        self._coord.set_desired_charging(self._sn, True)
        self._coord.set_charging_expectation(self._sn, True, hold_for=90)
        # Poll quickly for a short window to reflect new state
        self._coord.kick_fast(90)
        await self._coord.async_request_refresh()


class StopChargeButton(_BaseButton):
    def __init__(self, coord, sn):
        super().__init__(coord, sn, "Stop Charging")
        self._attr_translation_key = "stop_charging"

    async def async_press(self) -> None:
        await self._coord.client.stop_charging(self._sn)
        self._coord.set_desired_charging(self._sn, False)
        self._coord.set_charging_expectation(self._sn, False, hold_for=90)
        # Poll quickly after stop to clear state faster
        self._coord.kick_fast(60)
        await self._coord.async_request_refresh()
