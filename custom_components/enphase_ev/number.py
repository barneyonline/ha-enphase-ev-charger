from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
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
        serials = [sn for sn in coord.iter_serials() if sn and sn not in known_serials]
        if not serials:
            return
        entities: list[NumberEntity] = [ChargingAmpsNumber(coord, sn) for sn in serials]
        async_add_entities(entities, update_before_add=False)
        known_serials.update(serials)

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    _async_sync_chargers()


class ChargingAmpsNumber(EnphaseBaseEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charging_amps"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_amps_number"

    @property
    def native_value(self) -> float | None:
        data = self.data
        lvl = data.get("charging_level")
        if lvl is None:
            # Let coordinator choose a safe default within charger limits
            return float(self._coord.pick_start_amps(self._sn))
        try:
            return float(int(lvl))
        except Exception:
            return float(self._coord.pick_start_amps(self._sn))

    @property
    def native_min_value(self) -> float:
        v = self.data.get("min_amp")
        try:
            return float(int(v)) if v is not None else 6.0
        except Exception:
            return 6.0

    @property
    def native_max_value(self) -> float:
        v = self.data.get("max_amp")
        try:
            return float(int(v)) if v is not None else 40.0
        except Exception:
            return 40.0

    @property
    def native_step(self) -> float:
        return 1.0

    async def async_set_native_value(self, value: float) -> None:
        amps = int(value)
        # Store desired setpoint locally; do not start charging here.
        # Start actions (switch/button/service) will use this setpoint.
        self._coord.set_last_set_amps(self._sn, amps)
        await self._coord.async_request_refresh()
        if bool(self.data.get("charging")):
            # Restart the active session so the updated amps take effect
            self._coord.schedule_amp_restart(self._sn)
