from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity

OPTIONS = ["MANUAL_CHARGING", "SCHEDULED_CHARGING", "GREEN_CHARGING"]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[SelectEntity] = []
    serials = list(coord.serials or coord.data.keys())
    for sn in serials:
        entities.append(ChargeModeSelect(coord, sn))
    async_add_entities(entities)


class ChargeModeSelect(EnphaseBaseEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charge_mode"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charge_mode_select"

    @property
    def options(self) -> list[str]:
        return OPTIONS

    @property
    def current_option(self) -> str | None:
        d = (self._coord.data or {}).get(self._sn) or {}
        val = d.get("charge_mode")
        return str(val) if val else None

    async def async_select_option(self, option: str) -> None:
        await self._coord.client.set_charge_mode(self._sn, option)
        # Update cache immediately to reflect in UI, then refresh
        self._coord.set_charge_mode_cache(self._sn, option)
        await self._coord.async_request_refresh()
