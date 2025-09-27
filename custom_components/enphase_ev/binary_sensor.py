
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = []
    # Site-level cloud reachability
    entities.append(SiteCloudReachableBinarySensor(coord))
    serials = list(coord.serials or coord.data.keys())
    for sn in serials:
        entities.append(PluggedInBinarySensor(coord, sn))
        entities.append(ChargingBinarySensor(coord, sn))
        entities.append(FaultedBinarySensor(coord, sn))
        entities.append(ConnectedBinarySensor(coord, sn))
        entities.append(CommissionedBinarySensor(coord, sn))
    async_add_entities(entities)

class _EVBoolSensor(EnphaseBaseEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _translation_key: str | None = None

    def __init__(self, coord: EnphaseCoordinator, sn: str, key: str, tkey: str):
        super().__init__(coord, sn)
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_{sn}_{key}"
        self._attr_translation_key = tkey

    @property
    def is_on(self) -> bool:
        d = (self._coord.data or {}).get(self._sn) or {}
        v = d.get(self._key)
        return bool(v)

    # available and device_info inherited from base


class PluggedInBinarySensor(_EVBoolSensor):
    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "plugged", "plugged_in")


class ChargingBinarySensor(_EVBoolSensor):
    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "charging", "charging")
    @property
    def icon(self) -> str | None:
        # Lightning bolt when charging, dimmed/off otherwise
        return "mdi:flash" if self.is_on else "mdi:flash-off"


class FaultedBinarySensor(_EVBoolSensor):
    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "faulted", "faulted")
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        from homeassistant.helpers.entity import EntityCategory
        self._attr_entity_category = EntityCategory.DIAGNOSTIC


class ConnectedBinarySensor(_EVBoolSensor):
    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "connected", "connected")
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_entity_category = EntityCategory.DIAGNOSTIC


class CommissionedBinarySensor(_EVBoolSensor):
    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "commissioned", "commissioned")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC


class SiteCloudReachableBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "cloud_reachable"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_cloud_reachable"

    @property
    def name(self):
        return "Cloud Reachable"

    @property
    def is_on(self) -> bool:
        last = self._coord.last_success_utc
        if not last:
            return False
        now = dt_util.utcnow()
        interval = self._coord.update_interval.total_seconds() if self._coord.update_interval else 30
        threshold = interval * 2
        return (now - last).total_seconds() <= threshold

    @property
    def device_info(self):
        from homeassistant.helpers.entity import DeviceInfo
        return DeviceInfo(
            identifiers={(DOMAIN, f"site:{self._coord.site_id}")},
            manufacturer="Enphase",
            model="Enlighten Cloud",
            name=f"Enphase Site {self._coord.site_id}",
        )
