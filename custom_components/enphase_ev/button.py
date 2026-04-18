from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .battery_schedule_editor import (
    BatteryScheduleEditorEntity,
    battery_scheduler_enabled,
    days_list_from_editor,
)
from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity
from .entity_cleanup import prune_managed_entities
from .runtime_helpers import (
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
)
from .runtime_data import EnphaseConfigEntry, get_runtime_data

PARALLEL_UPDATES = 0


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    has_enpower = getattr(coord, "battery_has_enpower", None)
    if has_encharge is True or has_enpower is True:
        return True
    if has_encharge is False and has_enpower is False:
        return False
    return _type_available(coord, "encharge")


def _storm_guard_visible(coord: EnphaseCoordinator) -> bool:
    show_storm_guard = getattr(coord, "battery_show_storm_guard", None)
    return show_storm_guard is not False


def _retain_cancel_pending_profile_change(coord: EnphaseCoordinator) -> bool:
    return _type_available(coord, "envoy")


def _retain_request_grid_toggle_otp(coord: EnphaseCoordinator) -> bool:
    return _site_has_battery(coord) and (
        _type_available(coord, "enpower") or _type_available(coord, "envoy")
    )


def _retain_storm_alert_opt_out(coord: EnphaseCoordinator) -> bool:
    return (
        _site_has_battery(coord)
        and _type_available(coord, "envoy")
        and _storm_guard_visible(coord)
    )


def _retain_battery_schedule_refresh_button(coord: EnphaseCoordinator) -> bool:
    client = getattr(coord, "client", None)
    return (
        _site_has_battery(coord)
        and _type_available(coord, "encharge")
        and callable(getattr(client, "battery_schedules", None))
    )


def _retain_battery_schedule_editor_buttons(coord: EnphaseCoordinator) -> bool:
    client = getattr(coord, "client", None)
    return (
        _retain_battery_schedule_refresh_button(coord)
        and callable(getattr(client, "create_battery_schedule", None))
        and callable(getattr(client, "update_battery_schedule", None))
        and callable(getattr(client, "delete_battery_schedule", None))
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    ent_reg = er.async_get(hass)
    known_serials: set[str] = set()
    site_entity_keys: set[str] = set()

    def _site_button_unique_id(key: str) -> str:
        return f"{DOMAIN}_site_{coord.site_id}_{key}"

    def _charger_button_unique_id(sn: str, action: str) -> str:
        return f"{DOMAIN}_{sn}_{action}"

    @callback
    def _async_sync_chargers() -> None:
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        site_entities: list[ButtonEntity] = []
        retain_site_entity_keys: set[str] = set()
        current_serials = {sn for sn in coord.iter_serials() if sn}
        if _retain_cancel_pending_profile_change(coord):
            retain_site_entity_keys.add("cancel_pending_profile_change")
        if "cancel_pending_profile_change" not in site_entity_keys and _type_available(
            coord, "envoy"
        ):
            site_entities.append(CancelPendingProfileChangeButton(coord))
            site_entity_keys.add("cancel_pending_profile_change")
        if _retain_request_grid_toggle_otp(coord):
            retain_site_entity_keys.add("request_grid_toggle_otp")
        if (
            "request_grid_toggle_otp" not in site_entity_keys
            and _site_has_battery(coord)
            and (_type_available(coord, "enpower") or _type_available(coord, "envoy"))
        ):
            site_entities.append(RequestGridToggleOtpButton(coord))
            site_entity_keys.add("request_grid_toggle_otp")
        if _retain_storm_alert_opt_out(coord):
            retain_site_entity_keys.add("storm_alert_opt_out")
        if (
            "storm_alert_opt_out" not in site_entity_keys
            and _site_has_battery(coord)
            and _type_available(coord, "envoy")
            and _storm_guard_visible(coord)
        ):
            site_entities.append(StormAlertOptOutButton(coord))
            site_entity_keys.add("storm_alert_opt_out")
        scheduler_enabled = battery_scheduler_enabled(entry)
        if scheduler_enabled and _retain_battery_schedule_refresh_button(coord):
            retain_site_entity_keys.add("battery_force_refresh")
            if "battery_force_refresh" not in site_entity_keys:
                site_entities.append(BatteryForceRefreshButton(coord, entry))
                site_entity_keys.add("battery_force_refresh")
        if scheduler_enabled and _retain_battery_schedule_editor_buttons(coord):
            for key, entity in (
                ("battery_schedule_save", BatteryScheduleSaveButton(coord, entry)),
                (
                    "battery_schedule_delete",
                    BatteryScheduleDeleteButton(coord, entry),
                ),
            ):
                retain_site_entity_keys.add(key)
                if key not in site_entity_keys:
                    site_entities.append(entity)
                    site_entity_keys.add(key)
        if site_entities:
            async_add_entities(site_entities, update_before_add=False)
        serials = [sn for sn in current_serials if sn not in known_serials]
        if not serials:
            serial_entities: list[ButtonEntity] = []
        else:
            serial_entities = []
            for sn in serials:
                serial_entities.append(StartChargeButton(coord, sn))
                serial_entities.append(StopChargeButton(coord, sn))
        if serial_entities:
            async_add_entities(serial_entities, update_before_add=False)
        known_serials.intersection_update(current_serials)
        known_serials.update(serials)
        site_entity_keys.intersection_update(retain_site_entity_keys)

        if not inventory_ready:
            return

        prune_managed_entities(
            ent_reg,
            entry.entry_id,
            domain="button",
            active_unique_ids={
                *(_site_button_unique_id(key) for key in retain_site_entity_keys),
                *(
                    unique_id
                    for sn in current_serials
                    for unique_id in (
                        _charger_button_unique_id(sn, "start_charging"),
                        _charger_button_unique_id(sn, "stop_charging"),
                    )
                ),
            },
            is_managed=lambda unique_id: (
                unique_id
                in {
                    _site_button_unique_id("cancel_pending_profile_change"),
                    _site_button_unique_id("request_grid_toggle_otp"),
                    _site_button_unique_id("storm_alert_opt_out"),
                    _site_button_unique_id("battery_force_refresh"),
                    _site_button_unique_id("battery_schedule_save"),
                    _site_button_unique_id("battery_schedule_delete"),
                    _site_button_unique_id("battery_schedule_add"),
                }
                or unique_id.endswith(("_start_charging", "_stop_charging"))
            ),
        )

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    _async_sync_chargers()


class CancelPendingProfileChangeButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "cancel_pending_profile_change"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_cancel_pending_profile_change"
        )

    @property
    def available(self) -> bool:  # type: ignore[override]
        return (
            super().available
            and _type_available(self._coord, "envoy")
            and self._coord.battery_profile_pending
        )

    async def async_press(self) -> None:
        await self._coord.async_cancel_pending_profile_change()

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "envoy")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class RequestGridToggleOtpButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "request_grid_toggle_otp"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_request_grid_toggle_otp"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if not _site_has_battery(self._coord):
            return False
        if not (
            _type_available(self._coord, "enpower")
            or _type_available(self._coord, "envoy")
        ):
            return False
        return (
            self._coord.grid_control_supported is True
            and self._coord.grid_toggle_allowed is True
        )

    async def async_press(self) -> None:
        await self._coord.async_request_grid_toggle_otp()

    @property
    def device_info(self) -> DeviceInfo:
        for type_key in ("enpower", "envoy"):
            info = _type_device_info(self._coord, type_key)
            if info is not None:
                return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class StormAlertOptOutButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "storm_alert_opt_out"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_storm_alert_opt_out"

    @property
    def available(self) -> bool:  # type: ignore[override]
        return (
            super().available
            and _site_has_battery(self._coord)
            and _type_available(self._coord, "envoy")
            and _storm_guard_visible(self._coord)
        )

    async def async_press(self) -> None:
        await self._coord.async_opt_out_all_storm_alerts()

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "envoy")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class _BatteryScheduleButton(BatteryScheduleEditorEntity, ButtonEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coord: EnphaseCoordinator,
        entry: EnphaseConfigEntry,
        *,
        unique_suffix: str,
        translation_key: str,
        icon: str,
    ) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{unique_suffix}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon

    @property
    def available(self) -> bool:  # type: ignore[override]
        return (
            super().available
            and battery_scheduler_enabled(self._entry)
            and _retain_battery_schedule_refresh_button(self._coord)
            and self._editor is not None
        )

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class BatteryForceRefreshButton(_BatteryScheduleButton):
    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(
            coord,
            entry,
            unique_suffix="battery_force_refresh",
            translation_key="battery_schedule_refresh",
            icon="mdi:refresh",
        )

    async def async_press(self) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "force_refresh",
            {"config_entry_id": self._entry.entry_id},
            blocking=True,
        )


class BatteryScheduleSaveButton(_BatteryScheduleButton):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(
            coord,
            entry,
            unique_suffix="battery_schedule_save",
            translation_key="battery_schedule_save",
            icon="mdi:content-save",
        )

    @property
    def available(self) -> bool:  # type: ignore[override]
        return (
            super().available
            and self._editor is not None
            and _retain_battery_schedule_editor_buttons(self._coord)
            and bool(getattr(self._coord, "battery_write_access_confirmed", False))
            and (
                self._editor.is_creating
                or self._editor.edit.selected_schedule_id is not None
            )
        )

    async def async_press(self) -> None:
        if self._editor is None:
            return
        service = "add_schedule" if self._editor.is_creating else "update_schedule"
        service_data: dict[str, object] = {
            "config_entry_id": self._entry.entry_id,
            "schedule_type": self._editor.edit.schedule_type,
            "start_time": self._editor.edit.start_time,
            "end_time": self._editor.edit.end_time,
            "limit": int(self._editor.edit.limit),
            "days": days_list_from_editor(self._editor.edit.days),
        }
        if self._editor.is_creating:
            await self.hass.services.async_call(
                DOMAIN, service, service_data, blocking=True
            )
            return
        if self._editor.edit.selected_schedule_id is None:
            return
        service_data["schedule_id"] = self._editor.edit.selected_schedule_id
        service_data["confirm"] = True
        await self.hass.services.async_call(
            DOMAIN, service, service_data, blocking=True
        )


class BatteryScheduleDeleteButton(_BatteryScheduleButton):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(
            coord,
            entry,
            unique_suffix="battery_schedule_delete",
            translation_key="battery_schedule_delete",
            icon="mdi:calendar-remove",
        )

    @property
    def available(self) -> bool:  # type: ignore[override]
        return super().available and bool(
            self._editor
            and _retain_battery_schedule_editor_buttons(self._coord)
            and bool(getattr(self._coord, "battery_write_access_confirmed", False))
            and not self._editor.is_creating
            and self._editor.edit.selected_schedule_id
        )

    async def async_press(self) -> None:
        if self._editor is None or self._editor.edit.selected_schedule_id is None:
            return
        await self.hass.services.async_call(
            DOMAIN,
            "delete_schedule",
            {
                "config_entry_id": self._entry.entry_id,
                "schedule_id": self._editor.edit.selected_schedule_id,
                "confirm": True,
            },
            blocking=True,
        )


class _BaseButton(EnphaseBaseEntity, ButtonEntity):
    def __init__(self, coord: EnphaseCoordinator, sn: str, name_suffix: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_{name_suffix.replace(' ', '_').lower()}"


class StartChargeButton(_BaseButton):
    def __init__(self, coord, sn):
        super().__init__(coord, sn, "Start Charging")
        self._attr_translation_key = "start_charging"

    async def async_press(self) -> None:
        await self._coord.async_start_charging(self._sn)


class StopChargeButton(_BaseButton):
    def __init__(self, coord, sn):
        super().__init__(coord, sn, "Stop Charging")
        self._attr_translation_key = "stop_charging"

    async def async_press(self) -> None:
        await self._coord.async_stop_charging(self._sn)
