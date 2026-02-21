from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AuthSettingsUnavailable
from .const import DOMAIN
from .coordinator import (
    FAST_TOGGLE_POLL_HOLD_S,
    EnphaseCoordinator,
    ServiceValidationError,
)
from .entity import EnphaseBaseEntity
from .runtime_data import EnphaseConfigEntry, get_runtime_data

PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)
_AUTO_SUFFIX_RE = re.compile(r"^\d+$")


def _switch_entity_id_migrations(coord: EnphaseCoordinator) -> dict[str, str]:
    return {
        f"{DOMAIN}_site_{coord.site_id}_charge_from_grid_schedule": (
            "switch.charge_from_grid_schedule"
        )
    }


def _migrated_switch_entity_id(
    current_entity_id: str, target_entity_id: str
) -> str | None:
    """Return canonical ID target for migration, preserving numeric suffixes."""
    if current_entity_id == target_entity_id:
        return None

    target_prefix = f"{target_entity_id}_"
    if current_entity_id.startswith(target_prefix):
        suffix = current_entity_id[len(target_prefix) :]
        if _AUTO_SUFFIX_RE.fullmatch(suffix):
            return None

    base, _, suffix = current_entity_id.rpartition("_")
    if base and _AUTO_SUFFIX_RE.fullmatch(suffix):
        return f"{target_entity_id}_{suffix}"
    return target_entity_id


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
    ent_reg = er.async_get(hass)
    rename_by_unique = _switch_entity_id_migrations(coord)
    for registry_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        target_entity_id = rename_by_unique.get(registry_entry.unique_id)
        if target_entity_id is None:
            continue
        migrated_entity_id = _migrated_switch_entity_id(
            registry_entry.entity_id, target_entity_id
        )
        if migrated_entity_id is None:
            continue
        try:
            ent_reg.async_update_entity(
                registry_entry.entity_id,
                new_entity_id=migrated_entity_id,
            )
        except ValueError:
            _LOGGER.debug(
                "Could not rename %s to %s while migrating Charge From Grid Schedule switch",
                registry_entry.entity_id,
                migrated_entity_id,
            )

    schedule_sync = getattr(coord, "schedule_sync", None)
    site_entity_keys: set[str] = set()
    known_serials: set[str] = set()
    known_slots: set[tuple[str, str]] = set()
    known_green_battery: set[str] = set()
    known_app_auth: set[str] = set()

    @callback
    def _async_sync_site_entities() -> None:
        if not _site_has_battery(coord):
            return
        site_entities: list[SwitchEntity] = []
        if "storm_guard" not in site_entity_keys and _type_available(coord, "envoy"):
            site_entities.append(StormGuardSwitch(coord))
            site_entity_keys.add("storm_guard")
        if _type_available(coord, "encharge"):
            if "savings_use_battery_after_peak" not in site_entity_keys:
                site_entities.append(SavingsUseBatteryAfterPeakSwitch(coord))
                site_entity_keys.add("savings_use_battery_after_peak")
            if "charge_from_grid" not in site_entity_keys:
                site_entities.append(ChargeFromGridSwitch(coord))
                site_entity_keys.add("charge_from_grid")
            if "charge_from_grid_schedule" not in site_entity_keys:
                site_entities.append(ChargeFromGridScheduleSwitch(coord))
                site_entity_keys.add("charge_from_grid_schedule")
        if site_entities:
            async_add_entities(site_entities, update_before_add=False)

    def _slot_is_toggleable(sn: str, slot: dict[str, Any]) -> bool:
        schedule_type = str(slot.get("scheduleType") or "")
        if schedule_type == "OFF_PEAK":
            if schedule_sync is not None and hasattr(
                schedule_sync, "is_off_peak_eligible"
            ):
                if not schedule_sync.is_off_peak_eligible(sn):
                    return False
            return True
        if slot.get("startTime") is None or slot.get("endTime") is None:
            return False
        return True

    @callback
    def _async_sync_chargers() -> None:
        _async_sync_site_entities()
        site_has_battery = _site_has_battery(coord)
        serials = [sn for sn in coord.iter_serials() if sn and sn not in known_serials]
        entities: list[SwitchEntity] = []
        if serials:
            entities.extend(ChargingSwitch(coord, sn) for sn in serials)
            if site_has_battery:
                entities.extend(StormGuardEvseSwitch(coord, sn) for sn in serials)
            known_serials.update(serials)
        data_source = coord.data or {}
        if isinstance(data_source, dict):
            if site_has_battery:
                for sn in coord.iter_serials():
                    if not sn or sn in known_green_battery:
                        continue
                    data = data_source.get(sn) or {}
                    if data.get("green_battery_supported") is True:
                        entities.append(GreenBatterySwitch(coord, sn))
                        known_green_battery.add(sn)
            for sn in coord.iter_serials():
                if not sn or sn in known_app_auth:
                    continue
                data = data_source.get(sn) or {}
                if data.get("app_auth_supported") is True:
                    entities.append(AppAuthenticationSwitch(coord, sn))
                    known_app_auth.add(sn)
        if entities:
            async_add_entities(entities, update_before_add=False)

    @callback
    def _async_sync_schedule_switches() -> None:
        if schedule_sync is None:
            return
        entities: list[SwitchEntity] = []
        for sn, slot_id, slot in schedule_sync.iter_slots():
            key = (sn, slot_id)
            if key in known_slots:
                continue
            if not _slot_is_toggleable(sn, slot):
                continue
            entities.append(ScheduleSlotSwitch(coord, schedule_sync, sn, slot_id))
            known_slots.add(key)
        if entities:
            async_add_entities(entities, update_before_add=False)

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    if schedule_sync is not None:
        entry.async_on_unload(
            schedule_sync.async_add_listener(_async_sync_schedule_switches)
        )
    _async_sync_chargers()
    _async_sync_schedule_switches()


class StormGuardSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "storm_guard"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_storm_guard"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if not _type_available(self._coord, "envoy"):
            return False
        return (
            self._coord.storm_guard_state is not None
            and self._coord.storm_evse_enabled is not None
        )

    @property
    def is_on(self) -> bool:
        return self._coord.storm_guard_state == "enabled"

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_set_storm_guard_enabled(True)
        self._coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self._coord.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_set_storm_guard_enabled(False)
        self._coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self._coord.async_request_refresh()

    @property
    def device_info(self) -> DeviceInfo:
        type_device_info = getattr(self._coord, "type_device_info", None)
        info = type_device_info("envoy") if callable(type_device_info) else None
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class SavingsUseBatteryAfterPeakSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "savings_use_battery_after_peak"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_savings_use_battery_after_peak"
        )

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and self._coord.savings_use_battery_switch_available
        )

    @property
    def is_on(self) -> bool:
        return bool(self._coord.savings_use_battery_after_peak)

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_set_savings_use_battery_after_peak(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_set_savings_use_battery_after_peak(False)

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


class ChargeFromGridSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charge_from_grid"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_charge_from_grid"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and self._coord.charge_from_grid_control_available
        )

    @property
    def is_on(self) -> bool:
        return bool(self._coord.battery_charge_from_grid_enabled)

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_set_charge_from_grid(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_set_charge_from_grid(False)

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


class ChargeFromGridScheduleSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charge_from_grid_schedule"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_charge_from_grid_schedule"
        )

    @property
    def suggested_object_id(self) -> str | None:
        return "charge_from_grid_schedule"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and self._coord.charge_from_grid_schedule_available
        )

    @property
    def is_on(self) -> bool:
        return bool(self._coord.battery_charge_from_grid_schedule_enabled)

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_set_charge_from_grid_schedule_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_set_charge_from_grid_schedule_enabled(False)

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
        try:
            result = await self._coord.async_start_charging(self._sn)
        except ServiceValidationError:
            self._schedule_failure_refresh()
            self._force_write_state()
            raise
        if isinstance(result, dict) and result.get("status") == "not_ready":
            self._schedule_failure_refresh()
            self._force_write_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_stop_charging(self._sn)

    def _schedule_failure_refresh(self) -> None:
        self._coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        if self.hass is None:
            return
        self.hass.async_create_task(self._coord.async_request_refresh())

    def _force_write_state(self) -> None:
        if self.hass is None or not self.entity_id:
            return
        prev_force = getattr(self, "_attr_force_update", False)
        self._attr_force_update = True
        try:
            self.async_write_ha_state()
        finally:
            self._attr_force_update = prev_force

    @callback
    def _handle_coordinator_update(self) -> None:
        self._restored_state = None
        super()._handle_coordinator_update()


class GreenBatterySwitch(EnphaseBaseEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "green_battery"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_green_battery"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if not self._coord.scheduler_available:
            return False
        if self.data.get("green_battery_supported") is not True:
            return False
        return self.data.get("green_battery_enabled") is not None

    @property
    def is_on(self) -> bool:
        return bool(self.data.get("green_battery_enabled"))

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.client.set_green_battery_setting(self._sn, enabled=True)
        self._coord.set_green_battery_cache(self._sn, True)
        await self._coord.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.client.set_green_battery_setting(self._sn, enabled=False)
        self._coord.set_green_battery_cache(self._sn, False)
        await self._coord.async_request_refresh()


class AppAuthenticationSwitch(EnphaseBaseEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "app_authentication"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_app_authentication"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if not self._coord.auth_settings_available:
            return False
        if self.data.get("app_auth_supported") is not True:
            return False
        return self.data.get("app_auth_enabled") is not None

    @property
    def is_on(self) -> bool:
        return bool(self.data.get("app_auth_enabled"))

    async def async_turn_on(self, **kwargs) -> None:
        try:
            await self._coord.client.set_app_authentication(self._sn, enabled=True)
            self._coord.mark_auth_settings_available()
        except AuthSettingsUnavailable as err:
            self._coord.note_auth_settings_unavailable(err)
            raise HomeAssistantError(
                "Authentication settings are unavailable while the Enphase service is down."
            ) from err
        self._coord.set_app_auth_cache(self._sn, True)
        await self._coord.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        try:
            await self._coord.client.set_app_authentication(self._sn, enabled=False)
            self._coord.mark_auth_settings_available()
        except AuthSettingsUnavailable as err:
            self._coord.note_auth_settings_unavailable(err)
            raise HomeAssistantError(
                "Authentication settings are unavailable while the Enphase service is down."
            ) from err
        self._coord.set_app_auth_cache(self._sn, False)
        await self._coord.async_request_refresh()


class StormGuardEvseSwitch(EnphaseBaseEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "storm_guard_evse_charge"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_storm_guard_evse_charge"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if self.data.get("storm_guard_state") is None:
            return False
        return self.data.get("storm_evse_enabled") is not None

    @property
    def is_on(self) -> bool:
        return bool(self.data.get("storm_evse_enabled"))

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_set_storm_evse_enabled(True)
        self._coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self._coord.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_set_storm_evse_enabled(False)
        self._coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self._coord.async_request_refresh()


class ScheduleSlotSwitch(EnphaseBaseEntity, SwitchEntity):
    _attr_has_entity_name = False

    def __init__(self, coord: EnphaseCoordinator, schedule_sync, sn: str, slot_id: str):
        super().__init__(coord, sn)
        self._schedule_sync = schedule_sync
        self._slot_id = slot_id
        self._attr_unique_id = f"{DOMAIN}:{sn}:schedule:{slot_id}:enabled"
        self._unsub_schedule = None

    @property
    def name(self) -> str | None:  # type: ignore[override]
        if self._is_off_peak():
            return "Off Peak Schedule"
        helper_name = self._helper_name()
        if helper_name:
            return helper_name
        return f"Schedule {self._slot_id}"

    @property
    def available(self) -> bool:  # type: ignore[override]
        return (
            super().available
            and self._slot() is not None
            and self._coord.scheduler_available
        )

    @property
    def is_on(self) -> bool:
        slot = self._slot()
        if not slot:
            return False
        return bool(slot.get("enabled", True))

    async def async_turn_on(self, **kwargs) -> None:
        await self._schedule_sync.async_set_slot_enabled(self._sn, self._slot_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._schedule_sync.async_set_slot_enabled(self._sn, self._slot_id, False)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if hasattr(self._schedule_sync, "async_add_listener"):
            self._unsub_schedule = self._schedule_sync.async_add_listener(
                self._handle_schedule_sync_update
            )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_schedule is not None:
            self._unsub_schedule()
            self._unsub_schedule = None
        await super().async_will_remove_from_hass()

    def _slot(self) -> dict[str, Any] | None:
        return self._schedule_sync.get_slot(self._sn, self._slot_id)

    def _is_off_peak(self) -> bool:
        slot = self._slot()
        schedule_type = str(slot.get("scheduleType") or "") if slot else ""
        return schedule_type == "OFF_PEAK"

    def _helper_name(self) -> str | None:
        if self.hass is None:
            return None
        helper_entity_id = self._schedule_sync.get_helper_entity_id(
            self._sn, self._slot_id
        )
        if not helper_entity_id:
            return None
        state = self.hass.states.get(helper_entity_id)
        if state:
            friendly = state.attributes.get("friendly_name")
            if friendly:
                return str(friendly)
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(helper_entity_id)
        if entry:
            return entry.name or entry.original_name
        return None

    @callback
    def _handle_schedule_sync_update(self) -> None:
        self.async_write_ha_state()
