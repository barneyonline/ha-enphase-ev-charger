from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    site_entity = SiteCloudReachableBinarySensor(coord)
    async_add_entities([site_entity], update_before_add=False)

    known_serials: set[str] = set()

    @callback
    def _async_sync_chargers() -> None:
        serials = [
            sn for sn in coord.iter_serials() if sn and sn not in known_serials
        ]
        if not serials:
            return
        entities = []
        for sn in serials:
            entities.append(PluggedInBinarySensor(coord, sn))
            entities.append(ChargingBinarySensor(coord, sn))
            entities.append(ConnectedBinarySensor(coord, sn))
        if entities:
            async_add_entities(entities, update_before_add=False)
            known_serials.update(serials)

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    _async_sync_chargers()


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
        v = self.data.get(self._key)
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


class ConnectedBinarySensor(_EVBoolSensor):
    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "connected", "connected")
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _friendly_phase_mode(self) -> tuple[str | None, str | None]:
        raw = self.data.get("phase_mode")
        if raw is None:
            return None, None
        try:
            normalized = str(raw).strip()
        except Exception:  # noqa: BLE001
            return None, raw
        if not normalized:
            return None, raw
        friendly: str | None = None
        try:
            n = int(normalized)
        except Exception:  # noqa: BLE001
            n = None
        if n == 1:
            friendly = "Single Phase"
        elif n == 3:
            friendly = "Three Phase"
        if friendly is None:
            friendly = normalized
        return friendly, normalized

    @property
    def extra_state_attributes(self):
        friendly_phase, phase_raw = self._friendly_phase_mode()
        connection = self.data.get("connection")
        if isinstance(connection, str):
            connection = connection.strip() or None
        ip_attr = self.data.get("ip_address")
        if isinstance(ip_attr, str):
            ip_attr = ip_attr.strip() or None
        dlb_raw = self.data.get("dlb_enabled")
        dlb_bool = None
        try:
            if dlb_raw is not None:
                dlb_bool = bool(dlb_raw)
        except Exception:  # noqa: BLE001
            dlb_bool = None
        return {
            "connection": connection,
            "ip_address": ip_attr,
            "phase_mode": friendly_phase,
            "phase_mode_raw": phase_raw,
            "dlb_enabled": dlb_bool,
            "dlb_status": "enabled"
            if dlb_bool
            else "disabled"
            if dlb_bool is False
            else None,
        }


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
    def available(self) -> bool:
        if self._coord.last_success_utc is not None:
            return True
        return super().available

    @property
    def is_on(self) -> bool:
        last = self._coord.last_success_utc
        if not last:
            return False
        now = dt_util.utcnow()
        interval = (
            self._coord.update_interval.total_seconds()
            if self._coord.update_interval
            else 30
        )
        threshold = interval * 2
        return (now - last).total_seconds() <= threshold

    @property
    def extra_state_attributes(self):
        attrs: dict[str, object] = {}
        if self._coord.last_success_utc:
            attrs["last_success_utc"] = self._coord.last_success_utc.isoformat()
        if self._coord.last_failure_utc:
            attrs["last_failure_utc"] = self._coord.last_failure_utc.isoformat()
        if self._coord.last_failure_status is not None:
            attrs["last_failure_status"] = self._coord.last_failure_status
        if self._coord.last_failure_description:
            attrs["code_description"] = self._coord.last_failure_description
        if self._coord.last_failure_response:
            attrs["last_failure_response"] = self._coord.last_failure_response
        if self._coord.last_failure_source:
            attrs["last_failure_source"] = self._coord.last_failure_source
        if self._coord.backoff_ends_utc:
            attrs["backoff_ends_utc"] = self._coord.backoff_ends_utc.isoformat()
        return attrs

    @property
    def device_info(self):
        from homeassistant.helpers.entity import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, f"site:{self._coord.site_id}")},
            manufacturer="Enphase",
            model="Enlighten Cloud",
            name=f"Enphase Site {self._coord.site_id}",
        )
