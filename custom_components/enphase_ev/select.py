from __future__ import annotations

import json

import aiohttp
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import SchedulerUnavailable
from .const import DOMAIN
from .coordinator import EnphaseCoordinator, ServiceValidationError
from .entity import EnphaseBaseEntity
from .runtime_data import EnphaseConfigEntry, get_runtime_data

PARALLEL_UPDATES = 0

LABELS = {
    "MANUAL_CHARGING": "Manual",
    "SCHEDULED_CHARGING": "Scheduled",
    "GREEN_CHARGING": "Green",
}
REV_LABELS = {v: k for k, v in LABELS.items()}


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    return has_encharge is not False


def _type_available(coord: EnphaseCoordinator, type_key: str) -> bool:
    has_type_for_entities = getattr(coord, "has_type_for_entities", None)
    if callable(has_type_for_entities):
        return bool(has_type_for_entities(type_key))
    has_type = getattr(coord, "has_type", None)
    return bool(has_type(type_key)) if callable(has_type) else True


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
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    known_serials: set[str] = set()
    site_entity_added = False

    @callback
    def _async_sync_chargers() -> None:
        nonlocal site_entity_added
        if (
            not site_entity_added
            and _site_has_battery(coord)
            and _type_available(coord, "envoy")
        ):
            async_add_entities([SystemProfileSelect(coord)], update_before_add=False)
            site_entity_added = True
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
        return [
            labels[key]
            for key in self._coord.battery_profile_option_keys
            if key in labels
        ]

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return (
            _type_available(self._coord, "envoy")
            and self._coord.battery_controls_available
            and bool(self.options)
        )

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
        try:
            await self._coord.async_set_system_profile(selected_key)
        except ServiceValidationError as err:
            message = str(err).strip() or "System profile update failed."
            raise HomeAssistantError(message) from err
        except aiohttp.ClientResponseError as err:
            if err.status == 403:
                raise HomeAssistantError(
                    "System profile update was rejected by Enphase (HTTP 403 Forbidden)."
                ) from err
            if err.status == 401:
                raise HomeAssistantError(
                    "System profile update could not be authenticated. Reauthenticate and try again."
                ) from err
            raise HomeAssistantError("System profile update failed.") from err
        except aiohttp.ClientError as err:
            raise HomeAssistantError(
                "System profile update failed due to a network error. Try again."
            ) from err

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
            self._coord.mark_scheduler_available()
        except SchedulerUnavailable as err:
            self._coord.note_scheduler_unavailable(err)
            raise HomeAssistantError(
                "Charging mode selection is unavailable while the Enphase scheduler service is down."
            ) from err
        except aiohttp.ClientResponseError as err:
            code, display = _parse_scheduler_error(err.message)
            if err.status == 400 and (
                code == "iqevc_sch_10031"
                or (display and "No Schedules enabled" in display)
            ):
                raise ServiceValidationError(
                    "Enable at least one schedule before selecting Scheduled charging.",
                    translation_domain=DOMAIN,
                    translation_key="exceptions.schedule_required",
                ) from err
            raise
        # Update cache immediately to reflect in UI, then refresh
        self._coord.set_charge_mode_cache(self._sn, mode)
        await self._coord.async_request_refresh()
