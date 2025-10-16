from __future__ import annotations

from typing import Any

from homeassistant.core import callback
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
        self._data: dict[str, Any] = {}
        self._has_data = False
        source = coordinator.data or {}
        if isinstance(source, dict):
            self._has_data = serial in source
            if self._has_data:
                self._data = source.get(serial) or {}

    @property
    def available(self) -> bool:  # type: ignore[override]
        return super().available and self._has_data

    @property
    def data(self) -> dict[str, Any]:
        if hasattr(self, "_data"):
            return self._data
        source = getattr(self, "_coord", None)
        if source is not None:
            try:
                coord_data = source.data or {}
            except AttributeError:
                coord_data = {}
            if isinstance(coord_data, dict):
                return coord_data.get(getattr(self, "_sn", ""), {}) or {}
        return {}

    @callback
    def _handle_coordinator_update(self) -> None:
        source = self._coord.data or {}
        self._has_data = self._sn in source
        self._data = source.get(self._sn) or {}
        super()._handle_coordinator_update()

    @property
    def device_info(self) -> DeviceInfo:
        d = self.data
        display_name_raw = d.get("display_name") or d.get("name")
        display_name = str(display_name_raw) if display_name_raw else None
        model_name_raw = d.get("model_name")
        model_name = str(model_name_raw) if model_name_raw else None

        if display_name:
            dev_name = display_name
        elif model_name:
            dev_name = model_name
        else:
            dev_name = "Enphase EV Charger"

        model_display: str | None = None
        if display_name and model_name:
            model_display = f"{display_name} ({model_name})"
        elif model_name:
            model_display = model_name
        elif display_name:
            model_display = display_name
        # Build DeviceInfo using keyword arguments as per HA dev docs
        info_kwargs: dict[str, object] = {
            "identifiers": {(DOMAIN, self._sn)},
            "manufacturer": "Enphase",
            "name": dev_name,
            "serial_number": str(self._sn),
            "via_device": (DOMAIN, f"site:{self._coord.site_id}"),
        }
        # Optional enrichment when available
        if model_display:
            info_kwargs["model"] = model_display
        if d.get("hw_version"):
            info_kwargs["hw_version"] = str(d.get("hw_version"))
        if d.get("sw_version"):
            info_kwargs["sw_version"] = str(d.get("sw_version"))
        return DeviceInfo(**info_kwargs)
