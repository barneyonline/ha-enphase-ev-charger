from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
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
    schedule_sync = getattr(coord, "schedule_sync", None)
    known_serials: set[str] = set()
    known_slots: set[tuple[str, str]] = set()
    known_green_battery: set[str] = set()

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
        serials = [sn for sn in coord.iter_serials() if sn and sn not in known_serials]
        entities: list[SwitchEntity] = []
        if serials:
            entities.extend(ChargingSwitch(coord, sn) for sn in serials)
            known_serials.update(serials)
        data_source = coord.data or {}
        if isinstance(data_source, dict):
            for sn in coord.iter_serials():
                if not sn or sn in known_green_battery:
                    continue
                data = data_source.get(sn) or {}
                if data.get("green_battery_supported") is True:
                    entities.append(GreenBatterySwitch(coord, sn))
                    known_green_battery.add(sn)
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
        return super().available and self._slot() is not None

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
