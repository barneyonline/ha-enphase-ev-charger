from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .battery_schedule_editor import (
    BatteryScheduleEditorEntity,
    battery_scheduler_enabled,
)
from .const import DOMAIN, SAFE_LIMIT_AMPS
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity, evse_amp_control_applicable
from .entity_cleanup import prune_managed_entities
from .runtime_helpers import (
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
)
from .runtime_data import EnphaseConfigEntry, get_runtime_data

PARALLEL_UPDATES = 0


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    return has_encharge is not False


def _battery_write_access_confirmed(coord: EnphaseCoordinator) -> bool:
    confirmed = getattr(coord, "battery_write_access_confirmed", None)
    owner = getattr(coord, "battery_user_is_owner", None)
    installer = getattr(coord, "battery_user_is_installer", None)
    if owner is True or installer is True:
        return True
    if confirmed is not None:
        return bool(confirmed)
    return False


def _battery_schedule_editor_active(
    coord: EnphaseCoordinator, entry: EnphaseConfigEntry | None
) -> bool:
    client = getattr(coord, "client", None)
    return bool(
        battery_scheduler_enabled(entry)
        and callable(getattr(client, "battery_schedules", None))
        and callable(getattr(client, "create_battery_schedule", None))
        and callable(getattr(client, "update_battery_schedule", None))
        and callable(getattr(client, "delete_battery_schedule", None))
    )


def _retained_site_number_unique_ids(
    coord: EnphaseCoordinator, entry: EnphaseConfigEntry | None = None
) -> set[str]:
    unique_ids: set[str] = set()
    if not _type_available(coord, "encharge"):
        return unique_ids
    if not battery_scheduler_enabled(entry):
        if _battery_write_access_confirmed(coord):
            if getattr(coord, "battery_reserve_editable", False):
                unique_ids.add(f"{DOMAIN}_site_{coord.site_id}_battery_reserve")
            unique_ids.add(f"{DOMAIN}_site_{coord.site_id}_battery_shutdown_level")
        return unique_ids
    editor_active = _battery_schedule_editor_active(coord, entry)
    if _battery_write_access_confirmed(coord):
        if getattr(coord, "battery_reserve_editable", False):
            unique_ids.add(f"{DOMAIN}_site_{coord.site_id}_battery_reserve")
        unique_ids.add(f"{DOMAIN}_site_{coord.site_id}_battery_shutdown_level")
    if editor_active:
        unique_ids.add(f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_limit")
    return unique_ids


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    ent_reg = er.async_get(hass)
    known_serials: set[str] = set()
    added_site_number_unique_ids: set[str] = set()

    def _managed_site_number_unique_ids() -> set[str]:
        return {
            f"{DOMAIN}_site_{coord.site_id}_battery_reserve",
            f"{DOMAIN}_site_{coord.site_id}_battery_shutdown_level",
            f"{DOMAIN}_site_{coord.site_id}_battery_cfg_schedule_limit",
            f"{DOMAIN}_site_{coord.site_id}_battery_dtg_schedule_limit",
            f"{DOMAIN}_site_{coord.site_id}_battery_rbd_schedule_limit",
            f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_limit",
            f"{DOMAIN}_site_{coord.site_id}_battery_new_schedule_limit",
        }

    def _core_site_number_unique_ids() -> set[str]:
        return {
            f"{DOMAIN}_site_{coord.site_id}_battery_reserve",
            f"{DOMAIN}_site_{coord.site_id}_battery_shutdown_level",
        }

    def _charger_number_unique_id(sn: str) -> str:
        return f"{DOMAIN}_{sn}_amps_number"

    def _site_number_entities_by_unique_id(
        retained_site_number_unique_ids: set[str],
    ) -> dict[str, NumberEntity]:
        site_entities: dict[str, NumberEntity] = {}
        write_access_confirmed = _battery_write_access_confirmed(coord)

        entity_factories: dict[str, Callable[[], NumberEntity]] = {
            f"{DOMAIN}_site_{coord.site_id}_battery_reserve": lambda: BatteryReserveNumber(
                coord
            ),
            f"{DOMAIN}_site_{coord.site_id}_battery_shutdown_level": lambda: BatteryShutdownLevelNumber(
                coord
            ),
        }

        if battery_scheduler_enabled(entry):
            entity_factories[
                f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_limit"
            ] = lambda: BatteryScheduleEditLimitNumber(coord, entry)

        active_site_number_unique_ids: set[str] = set()
        if write_access_confirmed:
            active_site_number_unique_ids |= _core_site_number_unique_ids()
        if battery_scheduler_enabled(entry):
            active_site_number_unique_ids |= retained_site_number_unique_ids & {
                f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_limit"
            }

        for unique_id, factory in entity_factories.items():
            if unique_id in active_site_number_unique_ids:
                site_entities[unique_id] = factory()

        return site_entities

    @callback
    def _async_sync_chargers() -> None:
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        current_serials = {sn for sn in coord.iter_serials() if sn}
        retained_site_number_unique_ids = _retained_site_number_unique_ids(coord, entry)
        active_site_number_unique_ids: set[str] = set()
        site_entities: list[NumberEntity] = []
        if _site_has_battery(coord) and _type_available(coord, "encharge"):
            if _battery_write_access_confirmed(coord):
                active_site_number_unique_ids = _core_site_number_unique_ids()
            if battery_scheduler_enabled(entry):
                active_site_number_unique_ids |= retained_site_number_unique_ids & {
                    f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_limit"
                }
            current_site_entities = _site_number_entities_by_unique_id(
                retained_site_number_unique_ids
            )
            site_entities = [
                entity
                for unique_id, entity in current_site_entities.items()
                if unique_id not in added_site_number_unique_ids
            ]
            if site_entities:
                async_add_entities(site_entities, update_before_add=False)
                added_site_number_unique_ids.update(
                    entity.unique_id
                    for entity in site_entities
                    if isinstance(entity.unique_id, str)
                )
        serials = [sn for sn in current_serials if sn not in known_serials]
        if not serials:
            entities: list[NumberEntity] = []
        else:
            entities = []
            for sn in serials:
                entities.append(ChargingAmpsNumber(coord, sn))
        if entities:
            async_add_entities(entities, update_before_add=False)
        known_serials.intersection_update(current_serials)
        known_serials.update(serials)
        added_site_number_unique_ids.intersection_update(active_site_number_unique_ids)

        if not inventory_ready:
            return

        active_charger_unique_ids = {
            _charger_number_unique_id(sn) for sn in current_serials
        }
        prune_managed_entities(
            ent_reg,
            entry.entry_id,
            domain="number",
            active_unique_ids={
                *active_site_number_unique_ids,
                *active_charger_unique_ids,
            },
            is_managed=lambda unique_id: (
                unique_id in _managed_site_number_unique_ids()
                or unique_id.endswith(("_amps_number", "_schedule_edit_limit"))
            ),
        )

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    _async_sync_chargers()


class BatteryReserveNumber(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "battery_reserve"
    _attr_native_min_value = 5.0
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
            and _battery_write_access_confirmed(self._coord)
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
        info = _type_device_info(self._coord, "encharge")
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

    @staticmethod
    def _charging_active(value) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1", "yes", "y", "on"):
                return True
            if normalized in ("false", "0", "no", "n", "off"):
                return False
            return False
        return False

    @property
    def native_value(self) -> float | None:
        data = self.data
        if not evse_amp_control_applicable(self._coord, self._sn):
            return float(self._coord.pick_start_amps(self._sn))
        if self._safe_limit_active(
            data.get("safe_limit_state")
        ) and self._charging_active(data.get("charging")):
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
        if bool(self.data.get("charging")) and evse_amp_control_applicable(
            self._coord, self._sn
        ):
            # Restart the active session so the updated amps take effect
            self._coord.schedule_amp_restart(self._sn)


class BatteryShutdownLevelNumber(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "battery_shutdown_level"
    _attr_native_min_value = 5.0
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
            and _battery_write_access_confirmed(self._coord)
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
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class _BatteryScheduleEditorLimitNumber(BatteryScheduleEditorEntity, NumberEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 5.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coord: EnphaseCoordinator,
        entry: EnphaseConfigEntry,
        *,
        unique_suffix: str,
        translation_key: str,
    ) -> None:
        super().__init__(coord, entry)
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{unique_suffix}"

    @property
    def available(self) -> bool:  # type: ignore[override]
        client = getattr(self._coord, "client", None)
        return (
            super().available
            and battery_scheduler_enabled(self._entry)
            and _type_available(self._coord, "encharge")
            and _battery_write_access_confirmed(self._coord)
            and callable(getattr(client, "battery_schedules", None))
            and callable(getattr(client, "create_battery_schedule", None))
            and callable(getattr(client, "update_battery_schedule", None))
            and callable(getattr(client, "delete_battery_schedule", None))
            and self._editor is not None
        )

    @property
    def native_value(self) -> float | None:
        if self._editor is None:
            return None
        return float(self._editor.edit.limit)

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class BatteryScheduleEditLimitNumber(_BatteryScheduleEditorLimitNumber):
    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(
            coord,
            entry,
            unique_suffix="battery_schedule_edit_limit",
            translation_key="battery_schedule_edit_limit",
        )

    async def async_set_native_value(self, value: float) -> None:
        if self._editor is not None:
            self._editor.set_edit_limit(int(value))
