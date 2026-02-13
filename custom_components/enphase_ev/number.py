from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SAFE_LIMIT_AMPS
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity
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
):
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    known_serials: set[str] = set()
    site_entities_added = False

    @callback
    def _async_sync_chargers() -> None:
        nonlocal site_entities_added
        if (
            not site_entities_added
            and _site_has_battery(coord)
            and _type_available(coord, "encharge")
        ):
            async_add_entities(
                [BatteryReserveNumber(coord), BatteryShutdownLevelNumber(coord)],
                update_before_add=False,
            )
            site_entities_added = True
        serials = [sn for sn in coord.iter_serials() if sn and sn not in known_serials]
        if not serials:
            return
        entities: list[NumberEntity] = [ChargingAmpsNumber(coord, sn) for sn in serials]
        async_add_entities(entities, update_before_add=False)
        known_serials.update(serials)

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    _async_sync_chargers()


class BatteryReserveNumber(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "battery_reserve"
    _attr_native_min_value = 10.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_reserve"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and self._coord.battery_reserve_editable
        )

    @property
    def native_value(self) -> float | None:
        value = self._coord.battery_selected_backup_percentage
        if value is None:
            return None
        return float(value)

    @property
    def native_min_value(self) -> float:
        return float(self._coord.battery_reserve_min)

    @property
    def native_max_value(self) -> float:
        return float(self._coord.battery_reserve_max)

    async def async_set_native_value(self, value: float) -> None:
        await self._coord.async_set_battery_reserve(int(value))

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


class ChargingAmpsNumber(EnphaseBaseEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charging_amps"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_amps_number"

    @staticmethod
    def _safe_limit_active(value) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return bool(value)
        try:
            return int(str(value).strip()) != 0
        except Exception:  # noqa: BLE001
            return False

    @property
    def native_value(self) -> float | None:
        data = self.data
        if self._safe_limit_active(data.get("safe_limit_state")):
            return float(SAFE_LIMIT_AMPS)
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


class BatteryShutdownLevelNumber(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "battery_shutdown_level"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_shutdown_level"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and self._coord.battery_shutdown_level_available
        )

    @property
    def native_value(self) -> float | None:
        value = self._coord.battery_shutdown_level
        if value is None:
            return None
        return float(value)

    @property
    def native_min_value(self) -> float:
        return float(self._coord.battery_shutdown_level_min)

    @property
    def native_max_value(self) -> float:
        return float(self._coord.battery_shutdown_level_max)

    async def async_set_native_value(self, value: float) -> None:
        await self._coord.async_set_battery_shutdown_level(int(value))

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
