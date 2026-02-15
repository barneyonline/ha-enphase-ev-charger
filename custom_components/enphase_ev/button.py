from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity
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
    site_entity_keys: set[str] = set()

    @callback
    def _async_sync_chargers() -> None:
        site_entities: list[ButtonEntity] = []
        if (
            "cancel_pending_profile_change" not in site_entity_keys
            and _type_available(coord, "envoy")
        ):
            site_entities.append(CancelPendingProfileChangeButton(coord))
            site_entity_keys.add("cancel_pending_profile_change")
        if (
            "request_grid_toggle_otp" not in site_entity_keys
            and _site_has_battery(coord)
            and (_type_available(coord, "enpower") or _type_available(coord, "envoy"))
        ):
            site_entities.append(RequestGridToggleOtpButton(coord))
            site_entity_keys.add("request_grid_toggle_otp")
        if site_entities:
            async_add_entities(site_entities, update_before_add=False)
        serials = [sn for sn in coord.iter_serials() if sn and sn not in known_serials]
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
        type_device_info = getattr(self._coord, "type_device_info", None)
        info = type_device_info("envoy") if callable(type_device_info) else None
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
        type_device_info = getattr(self._coord, "type_device_info", None)
        if callable(type_device_info):
            for type_key in ("enpower", "envoy"):
                info = type_device_info(type_key)
                if info is not None:
                    return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
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
