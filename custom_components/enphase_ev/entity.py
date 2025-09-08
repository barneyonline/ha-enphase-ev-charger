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
        dev_name = d.get("name") or "Enphase EV Charger"
        return DeviceInfo(
            identifiers={(DOMAIN, self._sn)},
            manufacturer="Enphase",
            model="IQ EV Charger 2",
            name=dev_name,
            via_device=(DOMAIN, f"site:{self._coord.site_id}"),
        )
