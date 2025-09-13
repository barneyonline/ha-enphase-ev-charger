from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnphaseCoordinator


class EnphaseBaseEntity(CoordinatorEntity[EnphaseCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coordinator, context=serial)
        self._coord = coordinator
        self._sn = serial

    @property
    def available(self) -> bool:  # type: ignore[override]
        return super().available and self._sn in (self._coord.data or {})

    @property
    def device_info(self) -> DeviceInfo:
        d = (self._coord.data or {}).get(self._sn) or {}
        dev_name = d.get("display_name") or d.get("name") or "Enphase EV Charger"
        # Build DeviceInfo using keyword arguments as per HA dev docs
        info_kwargs: dict[str, object] = {
            "identifiers": {(DOMAIN, self._sn)},
            "manufacturer": "Enphase",
            "name": dev_name,
            "serial_number": str(self._sn),
            "via_device": (DOMAIN, f"site:{self._coord.site_id}"),
        }
        # Optional enrichment when available
        if d.get("model_name"):
            info_kwargs["model"] = str(d.get("model_name"))
        if d.get("model_id"):
            info_kwargs["model_id"] = str(d.get("model_id"))
        if d.get("hw_version"):
            info_kwargs["hw_version"] = str(d.get("hw_version"))
        if d.get("sw_version"):
            info_kwargs["sw_version"] = str(d.get("sw_version"))
        return DeviceInfo(**info_kwargs)
