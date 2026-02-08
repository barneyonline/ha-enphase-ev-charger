from __future__ import annotations

import json

import aiohttp
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import SchedulerUnavailable
from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity

PARALLEL_UPDATES = 0

LABELS = {
    "MANUAL_CHARGING": "Manual",
    "SCHEDULED_CHARGING": "Scheduled",
    "GREEN_CHARGING": "Green",
}
REV_LABELS = {v: k for k, v in LABELS.items()}


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    if has_encharge is None:
        has_encharge = getattr(coord, "_battery_has_encharge", None)
    return has_encharge is not False


def _parse_scheduler_error(message: str) -> tuple[str | None, str | None]:
    if not message:
        return None, None
    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("errorMessageCode")
    display = error.get("displayMessage") or error.get("additionalInfo")
    return (str(code) if code else None, str(display) if display else None)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    coord: EnphaseCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    known_serials: set[str] = set()

    if _site_has_battery(coord):
        site_entities: list[SelectEntity] = [SystemProfileSelect(coord)]
        async_add_entities(site_entities, update_before_add=False)

    @callback
    def _async_sync_chargers() -> None:
        serials = [sn for sn in coord.iter_serials() if sn and sn not in known_serials]
        if not serials:
            return
        entities: list[SelectEntity] = [ChargeModeSelect(coord, sn) for sn in serials]
        async_add_entities(entities, update_before_add=False)
        known_serials.update(serials)

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    _async_sync_chargers()


class SystemProfileSelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "system_profile"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_system_profile"

    @property
    def options(self) -> list[str]:
        labels = self._coord.battery_profile_option_labels
        return [labels[key] for key in self._coord.battery_profile_option_keys if key in labels]

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return self._coord.battery_controls_available and bool(self.options)

    @property
    def current_option(self) -> str | None:
        selected = self._coord.battery_selected_profile
        if not selected:
            return None
        fallback = selected.replace("_", " ").replace("-", " ").title()
        return self._coord.battery_profile_option_labels.get(selected, fallback)

    async def async_select_option(self, option: str) -> None:
        labels = self._coord.battery_profile_option_labels
        selected_key = None
        for key, label in labels.items():
            if label == option:
                selected_key = key
                break
        if selected_key is None:
            raise HomeAssistantError("Selected system profile is not available.")
        await self._coord.async_set_system_profile(selected_key)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"site:{self._coord.site_id}")},
            manufacturer="Enphase",
            model="Enlighten Cloud",
            name=f"Enphase Site {self._coord.site_id}",
            translation_key="enphase_site",
            translation_placeholders={"site_id": str(self._coord.site_id)},
        )


class ChargeModeSelect(EnphaseBaseEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charge_mode"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charge_mode_select"

    @property
    def options(self) -> list[str]:
        return list(LABELS.values())

    @property
    def available(self) -> bool:  # type: ignore[override]
        return super().available and self._coord.scheduler_available

    @property
    def current_option(self) -> str | None:
        d = self.data
        # Prefer scheduler-reported charge mode when available
        val = d.get("charge_mode_pref") or d.get("charge_mode")
        if not val:
            return None
        return LABELS.get(str(val), str(val).title())

    async def async_select_option(self, option: str) -> None:
        if not self._coord.scheduler_available:
            raise HomeAssistantError(
                "Charging mode selection is unavailable while the Enphase scheduler service is down."
            )
        mode = REV_LABELS.get(option, option.upper())
        try:
            await self._coord.client.set_charge_mode(self._sn, mode)
            self._coord._mark_scheduler_available()  # noqa: SLF001
        except SchedulerUnavailable as err:
            self._coord._note_scheduler_unavailable(err)  # noqa: SLF001
            raise HomeAssistantError(
                "Charging mode selection is unavailable while the Enphase scheduler service is down."
            ) from err
        except aiohttp.ClientResponseError as err:
            code, display = _parse_scheduler_error(err.message)
            if err.status == 400 and (
                code == "iqevc_sch_10031"
                or (display and "No Schedules enabled" in display)
            ):
                raise HomeAssistantError(
                    "Enable at least one schedule before selecting Scheduled charging."
                ) from err
            raise
        # Update cache immediately to reflect in UI, then refresh
        self._coord.set_charge_mode_cache(self._sn, mode)
        await self._coord.async_request_refresh()
