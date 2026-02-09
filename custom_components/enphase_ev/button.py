from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    known_serials: set[str] = set()

    has_type = getattr(coord, "has_type", None)
    if (bool(has_type("envoy")) if callable(has_type) else True):
        site_entities: list[ButtonEntity] = [CancelPendingProfileChangeButton(coord)]
        async_add_entities(site_entities, update_before_add=False)

    @callback
    def _async_sync_chargers() -> None:
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
        has_type = getattr(self._coord, "has_type", None)
        return (
            super().available
            and (bool(has_type("envoy")) if callable(has_type) else True)
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
