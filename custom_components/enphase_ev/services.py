from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import aiohttp
import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import service as ha_service

from .battery_schedule_editor import (
    battery_schedule_inventory,
    battery_schedule_overlap_message,
    battery_schedule_overlap_placeholders,
    battery_schedule_overlap_record,
)
from .const import DOMAIN, ISSUE_AUTH_BLOCKED, ISSUE_REAUTH_REQUIRED
from .device_types import parse_type_identifier
from .parsing_helpers import coerce_optional_bool
from .runtime_data import EnphaseRuntimeData, iter_coordinators
from .service_validation import raise_translated_service_validation

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

REGISTERED_SERVICES = (
    "force_refresh",
    "start_charging",
    "stop_charging",
    "trigger_message",
    "request_grid_toggle_otp",
    "set_grid_mode",
    "clear_reauth_issue",
    "start_live_stream",
    "stop_live_stream",
    "sync_schedules",
    "add_schedule",
    "update_schedule",
    "delete_schedule",
    "validate_schedule",
    "update_cfg_schedule",
)

_LOGGER = logging.getLogger(__name__)


def async_setup_services(
    hass: HomeAssistant, *, supports_response: object = SupportsResponse
) -> None:
    """Register integration services once."""

    if hass.services.has_service(DOMAIN, "start_charging"):
        return

    from homeassistant.exceptions import ServiceValidationError

    SCHEDULE_TYPE_SCHEMA = vol.In(("cfg", "dtg", "rbd"))
    DAYS_SCHEMA = vol.All(
        cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=1, max=7))]
    )
    SCHEDULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

    def _raise_service_validation(
        key: str,
        *,
        placeholders: dict[str, object] | None = None,
        message: str | None = None,
    ) -> None:
        raise_translated_service_validation(
            translation_domain=DOMAIN,
            translation_key=f"exceptions.{key}",
            translation_placeholders=placeholders,
            message=message,
        )

    async def _resolve_sn(device_id: str) -> str | None:
        dev_reg = dr.async_get(hass)
        dev = dev_reg.async_get(device_id)
        if not dev:
            return None
        for domain, sn in dev.identifiers:
            if domain == DOMAIN:
                if sn.startswith("site:"):
                    continue
                if sn.startswith("type:"):
                    continue
                return sn
        return None

    async def _resolve_site_id(device_id: str) -> str | None:
        dev_reg = dr.async_get(hass)
        dev = dev_reg.async_get(device_id)
        if not dev:
            return None
        for domain, identifier in dev.identifiers:
            if domain == DOMAIN and identifier.startswith("site:"):
                return identifier.partition(":")[2]
            if domain == DOMAIN and identifier.startswith("type:"):
                parsed = parse_type_identifier(identifier)
                if parsed:
                    return parsed[0]
        via = dev.via_device_id
        if via:
            parent = dev_reg.async_get(via)
            if parent:
                for domain, identifier in parent.identifiers:
                    if domain == DOMAIN and identifier.startswith("site:"):
                        return identifier.partition(":")[2]
                    if domain == DOMAIN and identifier.startswith("type:"):
                        parsed = parse_type_identifier(identifier)
                        if parsed:
                            return parsed[0]
        return None

    async def _get_coordinator_for_sn(sn: str) -> EnphaseCoordinator | None:
        for coord in iter_coordinators(hass):
            if not coord.serials or sn in coord.serials or sn in (coord.data or {}):
                return coord
        return None

    def _get_coordinator_for_entry_id(entry_id: str) -> EnphaseCoordinator | None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id != entry_id:
                continue
            runtime_data = getattr(entry, "runtime_data", None)
            if isinstance(runtime_data, EnphaseRuntimeData):
                return runtime_data.coordinator
        return None

    DEVICE_ID_LIST = vol.All(cv.ensure_list, [cv.string])
    ENTRY_SCHEMA = {vol.Optional("config_entry_id"): cv.string}

    START_SCHEMA = vol.Schema(
        {
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("charging_level", default=32): vol.All(
                int, vol.Range(min=6, max=40)
            ),
            vol.Optional("connector_id", default=1): vol.All(
                int, vol.Range(min=1, max=2)
            ),
        }
    )
    STOP_SCHEMA = vol.Schema({vol.Optional("device_id"): DEVICE_ID_LIST})
    TRIGGER_SCHEMA = vol.Schema(
        {
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Required("requested_message"): cv.string,
        }
    )
    REQUEST_GRID_OTP_SCHEMA = vol.Schema(
        {vol.Optional("device_id"): DEVICE_ID_LIST, vol.Optional("site_id"): cv.string}
    )
    SET_GRID_MODE_SCHEMA = vol.Schema(
        {
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Required("mode"): vol.In(("on_grid", "off_grid")),
            vol.Required("otp"): cv.string,
        }
    )
    CLEAR_SCHEMA = vol.Schema(
        {vol.Optional("device_id"): DEVICE_ID_LIST, vol.Optional("site_id"): cv.string}
    )
    SYNC_SCHEMA = vol.Schema({vol.Optional("device_id"): DEVICE_ID_LIST})
    FORCE_REFRESH_SCHEMA = vol.Schema(
        {
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Optional("config_entry_id"): cv.string,
        }
    )
    ADD_SCHEDULE_SCHEMA = vol.Schema(
        {
            **ENTRY_SCHEMA,
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Required("schedule_type"): SCHEDULE_TYPE_SCHEMA,
            vol.Required("start_time"): cv.time,
            vol.Required("end_time"): cv.time,
            vol.Required("limit"): vol.All(vol.Coerce(int), vol.Range(min=5, max=100)),
            vol.Required("days"): DAYS_SCHEMA,
        }
    )
    UPDATE_SCHEDULE_SCHEMA = vol.Schema(
        {
            **ENTRY_SCHEMA,
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Required("schedule_id"): cv.string,
            vol.Required("schedule_type"): SCHEDULE_TYPE_SCHEMA,
            vol.Required("start_time"): cv.time,
            vol.Required("end_time"): cv.time,
            vol.Required("limit"): vol.All(vol.Coerce(int), vol.Range(min=5, max=100)),
            vol.Required("days"): DAYS_SCHEMA,
            vol.Required("confirm"): cv.boolean,
        }
    )
    DELETE_SCHEDULE_SCHEMA = vol.Schema(
        {
            **ENTRY_SCHEMA,
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Optional("schedule_id"): cv.string,
            vol.Optional("schedule_ids"): cv.ensure_list,
            vol.Optional("schedule_type"): SCHEDULE_TYPE_SCHEMA,
            vol.Required("confirm"): cv.boolean,
        }
    )
    VALIDATE_SCHEDULE_SCHEMA = vol.Schema(
        {
            **ENTRY_SCHEMA,
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Required("schedule_type"): SCHEDULE_TYPE_SCHEMA,
        }
    )

    def _normalize_schedule_ids(raw: object) -> list[str]:
        schedule_ids: list[str] = []
        if raw is None:
            return schedule_ids
        if isinstance(raw, (list, tuple, set)):
            candidates = [str(value) for value in raw]
        else:
            candidates = re.split(r"[,\s]+", str(raw))
        for candidate in candidates:
            if candidate:
                schedule_ids.append(candidate)
        return [
            schedule_id.strip().strip("'\"")
            for schedule_id in schedule_ids
            if schedule_id.strip().strip("'\"")
        ]

    def _validate_schedule_fields(
        *,
        schedule_type: str,
        start_time,
        end_time,
        days: list[int],
        limit: int,
    ) -> tuple[str, str]:
        if not days:
            _raise_service_validation(
                "battery_schedule_day_required",
                message="Select at least one day for the schedule.",
            )
        start_str = start_time.strftime("%H:%M")
        end_str = end_time.strftime("%H:%M")
        if start_str == end_str:
            _raise_service_validation(
                "battery_schedule_times_different",
                message="Schedule start and end times must be different.",
            )
        if not 5 <= int(limit) <= 100:
            schedule_type_label = str(schedule_type).upper()
            _raise_service_validation(
                "battery_schedule_limit_range",
                placeholders={
                    "schedule_type": schedule_type_label,
                    "minimum": "5",
                    "maximum": "100",
                },
                message=(
                    f"{schedule_type_label} schedule limit must be between 5 and 100."
                ),
            )
        return start_str, end_str

    async def _validate_schedule_with_api(
        coord: EnphaseCoordinator, schedule_type: str
    ) -> dict[str, object]:
        validator = getattr(coord.client, "validate_battery_schedule", None)
        if not callable(validator):
            return {}
        try:
            result = await validator(schedule_type)
        except aiohttp.ClientResponseError as err:
            if err.status in {403, 404}:
                _LOGGER.debug(
                    "Ignoring battery schedule preflight failure for site %s (%s %s)",
                    getattr(coord, "site_id", "unknown"),
                    err.status,
                    str(schedule_type).upper(),
                )
                return {}
            raise
        if not isinstance(result, dict):
            return {}
        valid = coerce_optional_bool(result.get("valid"))
        if valid is None and "isValid" in result:
            valid = coerce_optional_bool(result.get("isValid"))
        if valid is False:
            raw_message = result.get("message")
            if isinstance(raw_message, str) and raw_message.strip():
                detail = raw_message.strip()
                _raise_service_validation(
                    "battery_schedule_validation_rejected_detail",
                    placeholders={"message": detail},
                    message=(
                        "Schedule rejected by the Enphase validation endpoint: "
                        f"{detail}"
                    ),
                )
            _raise_service_validation(
                "battery_schedule_validation_rejected",
                message="Schedule rejected by the Enphase validation endpoint.",
            )
        if valid is not None and "valid" not in result:
            return {**result, "valid": bool(valid)}
        return result

    def _known_schedule_ids(coord: EnphaseCoordinator) -> set[str]:
        return {schedule.schedule_id for schedule in battery_schedule_inventory(coord)}

    def _schedule_inventory_by_id(coord: EnphaseCoordinator) -> dict[str, object]:
        return {
            schedule.schedule_id: schedule
            for schedule in battery_schedule_inventory(coord)
        }

    def _remaining_schedule_for_delete_family(
        coord: EnphaseCoordinator,
        schedule_type: str,
        deleted_schedule_ids: set[str],
    ) -> object | None:
        normalized_schedule_type = str(schedule_type).lower()
        selected_schedule_id = getattr(
            coord, f"_battery_{normalized_schedule_type}_schedule_id", None
        )
        remaining = [
            schedule
            for schedule in battery_schedule_inventory(coord)
            if schedule.schedule_type == normalized_schedule_type
            and schedule.schedule_id not in deleted_schedule_ids
        ]
        if not remaining:
            return None
        if selected_schedule_id is not None:
            for schedule in remaining:
                if schedule.schedule_id == selected_schedule_id:
                    return schedule
        for schedule in remaining:
            if schedule.enabled is True:
                return schedule
        return remaining[0]

    def _apply_schedule_for_update(
        coord: EnphaseCoordinator,
        *,
        schedule_inventory: dict[str, object],
        schedule_id: str,
        schedule_type: str,
        start_time: str,
        end_time: str,
        enabled: bool | None,
    ) -> tuple[str, str, bool | None]:
        normalized_schedule_type = str(schedule_type).lower()
        selected_schedule_id = getattr(
            coord, f"_battery_{normalized_schedule_type}_schedule_id", None
        )
        if selected_schedule_id is None or selected_schedule_id == schedule_id:
            return start_time, end_time, enabled
        selected_schedule = schedule_inventory.get(str(selected_schedule_id))
        if (
            selected_schedule is not None
            and selected_schedule.schedule_type == normalized_schedule_type
        ):
            return (
                selected_schedule.start_time,
                selected_schedule.end_time,
                selected_schedule.enabled,
            )
        return start_time, end_time, enabled

    def _validate_schedule_overlap(
        coord: EnphaseCoordinator,
        *,
        start_time: str,
        end_time: str,
        days: list[int],
        exclude_schedule_id: str | None = None,
    ) -> None:
        overlapping = battery_schedule_overlap_record(
            coord,
            start_time=start_time,
            end_time=end_time,
            days=days,
            exclude_schedule_id=exclude_schedule_id,
        )
        if overlapping is not None:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="exceptions.battery_schedule_overlap",
                translation_placeholders=battery_schedule_overlap_placeholders(
                    overlapping, hass=hass
                ),
                message=battery_schedule_overlap_message(overlapping, hass=hass),
            )

    def _validate_cfg_schedule(data: dict) -> dict:
        if not any(k in data for k in ("start_time", "end_time", "limit")):
            raise vol.Invalid(
                "At least one of start_time, end_time, or limit must be provided"
            )
        return data

    UPDATE_CFG_SCHEMA = vol.All(
        vol.Schema(
            {
                vol.Optional("device_id"): DEVICE_ID_LIST,
                vol.Optional("site_id"): cv.string,
                vol.Optional("start_time"): cv.time,
                vol.Optional("end_time"): cv.time,
                vol.Optional("limit"): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=100)
                ),
            }
        ),
        _validate_cfg_schedule,
    )

    def _extract_device_ids(call: ServiceCall) -> list[str]:
        device_ids: set[str] = set()
        try:
            device_ids |= set(
                ha_service.async_extract_referenced_device_ids(hass, call)
            )
        except Exception:
            pass
        data_ids = call.data.get("device_id")
        if data_ids:
            if isinstance(data_ids, str):
                device_ids.add(data_ids)
            else:
                device_ids |= {str(v) for v in data_ids}
        return list(device_ids)

    async def _resolve_site_ids_from_call(call: ServiceCall) -> set[str]:
        site_ids: set[str] = set()
        config_entry_id = call.data.get("config_entry_id")
        if config_entry_id:
            coord = _get_coordinator_for_entry_id(str(config_entry_id))
            if coord is not None:
                site_ids.add(str(coord.site_id))
        for device_id in _extract_device_ids(call):
            site_id = await _resolve_site_id(device_id)
            if site_id:
                site_ids.add(site_id)
        explicit = call.data.get("site_id")
        if explicit:
            site_ids.add(str(explicit))
        return site_ids

    async def _resolve_single_site_coordinator(
        call: ServiceCall,
    ) -> EnphaseCoordinator:
        config_entry_id = call.data.get("config_entry_id")
        if config_entry_id:
            coord = _get_coordinator_for_entry_id(str(config_entry_id))
            if coord is not None:
                return coord
        site_ids = await _resolve_site_ids_from_call(call)
        if not site_ids:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="exceptions.grid_site_required",
            )
        if len(site_ids) > 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="exceptions.grid_site_ambiguous",
                translation_placeholders={"count": str(len(site_ids))},
            )
        target = next(iter(site_ids))
        coordinators = iter_coordinators(hass, site_ids={target})
        if coordinators:
            return coordinators[0]
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="exceptions.grid_site_required",
        )

    async def _svc_force_refresh(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        await coord.async_request_refresh()

    async def _svc_start(call: ServiceCall) -> None:
        device_ids = _extract_device_ids(call)
        if not device_ids:
            return
        connector_id = int(call.data.get("connector_id", 1))
        for device_id in device_ids:
            sn = await _resolve_sn(device_id)
            if not sn:
                continue
            coord = await _get_coordinator_for_sn(sn)
            if not coord:
                continue
            level = call.data.get("charging_level")
            await coord.async_start_charging(
                sn, requested_amps=level, connector_id=connector_id
            )

    async def _svc_stop(call: ServiceCall) -> None:
        device_ids = _extract_device_ids(call)
        if not device_ids:
            return
        for device_id in device_ids:
            sn = await _resolve_sn(device_id)
            if not sn:
                continue
            coord = await _get_coordinator_for_sn(sn)
            if not coord:
                continue
            await coord.async_stop_charging(sn)

    async def _svc_trigger(call: ServiceCall) -> dict[str, object]:
        device_ids = _extract_device_ids(call)
        if not device_ids:
            return {}
        message = call.data["requested_message"]
        results: list[dict[str, object]] = []
        for device_id in device_ids:
            sn = await _resolve_sn(device_id)
            if not sn:
                continue
            coord = await _get_coordinator_for_sn(sn)
            if not coord:
                continue
            reply = await coord.async_trigger_ocpp_message(sn, message)
            results.append(
                {
                    "device_id": device_id,
                    "serial": sn,
                    "site_id": coord.site_id,
                    "response": reply,
                }
            )
        return {"results": results}

    async def _svc_request_grid_otp(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        await coord.async_request_grid_toggle_otp()
        await coord.async_request_refresh()

    async def _svc_set_grid_mode(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        await coord.async_set_grid_mode(call.data["mode"], call.data["otp"])

    async def _svc_clear_issue(call: ServiceCall) -> None:
        site_ids: set[str] = set()
        for device_id in _extract_device_ids(call):
            site_id = await _resolve_site_id(device_id)
            if site_id:
                site_ids.add(site_id)
        explicit = call.data.get("site_id")
        if explicit:
            site_ids.add(str(explicit))

        issue_ids = {ISSUE_REAUTH_REQUIRED, ISSUE_AUTH_BLOCKED}
        for site_id in site_ids:
            issue_ids.add(f"{ISSUE_REAUTH_REQUIRED}_{site_id}")
            issue_ids.add(f"{ISSUE_AUTH_BLOCKED}_{site_id}")
        for issue_id in issue_ids:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    async def _svc_start_stream(call: ServiceCall) -> None:
        site_ids = await _resolve_site_ids_from_call(call)
        coords = iter_coordinators(hass, site_ids=site_ids or None)
        if not coords:
            return
        if not site_ids:
            coords = coords[:1]
        for coord in coords:
            await coord.async_start_streaming(manual=True)
            await coord.async_request_refresh()

    async def _svc_stop_stream(call: ServiceCall) -> None:
        site_ids = await _resolve_site_ids_from_call(call)
        coords = iter_coordinators(hass, site_ids=site_ids or None)
        if not coords:
            return
        if not site_ids:
            coords = coords[:1]
        for coord in coords:
            await coord.async_stop_streaming(manual=True)
            await coord.async_request_refresh()

    async def _svc_update_cfg_schedule(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        await coord.async_update_cfg_schedule(
            start=call.data.get("start_time"),
            end=call.data.get("end_time"),
            limit=call.data.get("limit"),
        )

    async def _svc_add_schedule(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        if not coord.battery_write_access_confirmed:
            _raise_service_validation(
                "battery_schedule_editing_unavailable",
                message="Battery schedule editing is unavailable.",
            )
        schedule_type = str(call.data["schedule_type"]).lower()
        days = sorted({int(day) for day in call.data["days"]})
        limit = int(call.data["limit"])
        start_str, end_str = _validate_schedule_fields(
            schedule_type=schedule_type,
            start_time=call.data["start_time"],
            end_time=call.data["end_time"],
            days=days,
            limit=limit,
        )
        _validate_schedule_overlap(
            coord,
            start_time=start_str,
            end_time=end_str,
            days=days,
        )
        await _validate_schedule_with_api(coord, schedule_type)
        creator = getattr(coord.client, "create_battery_schedule", None)
        if not callable(creator):
            _raise_service_validation(
                "battery_schedule_api_unavailable",
                message="Battery schedule API is unavailable.",
            )
        try:
            await coord.battery_runtime.async_create_battery_schedule(
                schedule_type=str(schedule_type).upper(),
                start_time=start_str,
                end_time=end_str,
                limit=limit,
                days=days,
                timezone=str(
                    getattr(coord, "battery_timezone", None)
                    or hass.config.time_zone
                    or "UTC"
                ),
                is_enabled=True,
            )
        except aiohttp.ClientResponseError as err:
            coord.battery_runtime.raise_schedule_update_validation_error(err)
            raise
        await coord.async_request_refresh()

    async def _svc_update_schedule(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        if not coord.battery_write_access_confirmed:
            _raise_service_validation(
                "battery_schedule_editing_unavailable",
                message="Battery schedule editing is unavailable.",
            )
        if not call.data.get("confirm"):
            _raise_service_validation(
                "battery_schedule_update_confirm_required",
                message="Confirmation required to update a schedule.",
            )
        schedule_id = str(call.data["schedule_id"]).strip()
        if not SCHEDULE_ID_PATTERN.match(schedule_id):
            _raise_service_validation(
                "battery_schedule_id_invalid",
                placeholders={"schedule_id": schedule_id},
                message=f"Invalid schedule ID: {schedule_id}",
            )
        schedule_inventory = _schedule_inventory_by_id(coord)
        known_ids = set(schedule_inventory)
        if known_ids and schedule_id not in known_ids:
            _raise_service_validation(
                "battery_schedule_id_not_found",
                placeholders={"schedule_id": schedule_id},
                message=f"Schedule ID not found in current data: {schedule_id}",
            )
        existing_schedule = schedule_inventory.get(schedule_id)
        schedule_type = (
            existing_schedule.schedule_type
            if existing_schedule is not None
            else str(call.data["schedule_type"]).lower()
        )
        days = sorted({int(day) for day in call.data["days"]})
        limit = int(call.data["limit"])
        start_str, end_str = _validate_schedule_fields(
            schedule_type=schedule_type,
            start_time=call.data["start_time"],
            end_time=call.data["end_time"],
            days=days,
            limit=limit,
        )
        _validate_schedule_overlap(
            coord,
            start_time=start_str,
            end_time=end_str,
            days=days,
            exclude_schedule_id=schedule_id,
        )
        await _validate_schedule_with_api(coord, schedule_type)
        updater = getattr(coord.client, "update_battery_schedule", None)
        if not callable(updater):
            _raise_service_validation(
                "battery_schedule_api_unavailable",
                message="Battery schedule API is unavailable.",
            )
        apply_start_str, apply_end_str, apply_enabled = _apply_schedule_for_update(
            coord,
            schedule_inventory=schedule_inventory,
            schedule_id=schedule_id,
            schedule_type=schedule_type,
            start_time=start_str,
            end_time=end_str,
            enabled=(
                existing_schedule.enabled if existing_schedule is not None else None
            ),
        )
        try:
            await coord.battery_runtime.async_update_battery_schedule(
                schedule_id,
                schedule_type=str(schedule_type).upper(),
                start_time=start_str,
                end_time=end_str,
                limit=limit,
                days=days,
                timezone=str(
                    (
                        existing_schedule.timezone
                        if existing_schedule is not None
                        else getattr(coord, "battery_timezone", None)
                    )
                    or hass.config.time_zone
                    or "UTC"
                ),
                apply_settings=False,
            )
            if schedule_type == "cfg":
                await coord.battery_runtime.async_commit_cfg_schedule_write(
                    schedule_enabled=apply_enabled
                )
            else:
                await coord.battery_runtime.async_apply_schedule_family_settings(
                    schedule_type,
                    start_time=apply_start_str,
                    end_time=apply_end_str,
                    enabled=apply_enabled,
                )
        except aiohttp.ClientResponseError as err:
            coord.battery_runtime.raise_schedule_update_validation_error(err)
            raise
        await coord.async_request_refresh()

    async def _svc_delete_schedule(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        if not coord.battery_write_access_confirmed:
            _raise_service_validation(
                "battery_schedule_editing_unavailable",
                message="Battery schedule editing is unavailable.",
            )
        if not call.data.get("confirm"):
            _raise_service_validation(
                "battery_schedule_delete_confirm_required",
                message="Confirmation required to delete a schedule.",
            )
        raw_schedule_ids = call.data.get("schedule_ids")
        if raw_schedule_ids:
            schedule_ids = _normalize_schedule_ids(raw_schedule_ids)
        else:
            schedule_ids = _normalize_schedule_ids(call.data.get("schedule_id"))
        if not schedule_ids:
            _raise_service_validation(
                "battery_schedule_ids_required",
                message="Provide at least one schedule ID to delete.",
            )
        invalid_ids = [
            schedule_id
            for schedule_id in schedule_ids
            if not SCHEDULE_ID_PATTERN.match(schedule_id)
        ]
        if invalid_ids:
            ids = ", ".join(invalid_ids)
            _raise_service_validation(
                "battery_schedule_ids_invalid",
                placeholders={"schedule_ids": ids},
                message=f"Invalid schedule ID(s): {ids}",
            )
        known_ids = _known_schedule_ids(coord)
        if known_ids:
            missing = [
                schedule_id
                for schedule_id in schedule_ids
                if schedule_id not in known_ids
            ]
            if missing:
                ids = ", ".join(missing)
                _raise_service_validation(
                    "battery_schedule_ids_not_found",
                    placeholders={"schedule_ids": ids},
                    message=f"Schedule ID(s) not found in current data: {ids}",
                )
        deleter = getattr(coord.client, "delete_battery_schedule", None)
        if not callable(deleter):
            _raise_service_validation(
                "battery_schedule_api_unavailable",
                message="Battery schedule API is unavailable.",
            )
        schedule_inventory = _schedule_inventory_by_id(coord)
        requested_schedule_type = call.data.get("schedule_type")
        deleted_schedule_ids_by_family: dict[str, set[str]] = {}
        for schedule_id in schedule_ids:
            schedule = schedule_inventory.get(schedule_id)
            schedule_type = (
                schedule.schedule_type
                if schedule is not None
                else (
                    str(requested_schedule_type).lower()
                    if requested_schedule_type is not None
                    else "cfg"
                )
            )
            try:
                await deleter(schedule_id, schedule_type=schedule_type)
            except aiohttp.ClientResponseError as err:
                coord.battery_runtime.raise_schedule_update_validation_error(err)
                raise
            deleted_schedule_ids_by_family.setdefault(
                str(schedule_type).lower(), set()
            ).add(schedule_id)
        for schedule_type, deleted_ids in deleted_schedule_ids_by_family.items():
            remaining_schedule = _remaining_schedule_for_delete_family(
                coord, schedule_type, deleted_ids
            )
            await coord.battery_runtime.async_apply_schedule_family_settings(
                schedule_type,
                start_time=(
                    remaining_schedule.start_time
                    if remaining_schedule is not None
                    else None
                ),
                end_time=(
                    remaining_schedule.end_time
                    if remaining_schedule is not None
                    else None
                ),
                enabled=(
                    remaining_schedule.enabled
                    if remaining_schedule is not None
                    else False
                ),
            )
        await coord.async_request_refresh()

    async def _svc_validate_schedule(call: ServiceCall) -> dict[str, object]:
        coord = await _resolve_single_site_coordinator(call)
        if not coord.battery_write_access_confirmed:
            _raise_service_validation(
                "battery_schedule_editing_unavailable",
                message="Battery schedule editing is unavailable.",
            )
        result = await _validate_schedule_with_api(
            coord, str(call.data["schedule_type"]).lower()
        )
        return result

    async def _svc_sync_schedules(call: ServiceCall) -> None:
        device_ids = _extract_device_ids(call)
        if device_ids:
            for device_id in device_ids:
                sn = await _resolve_sn(device_id)
                if not sn:
                    continue
                coord = await _get_coordinator_for_sn(sn)
                if not coord or not hasattr(coord, "schedule_sync"):
                    continue
                await coord.schedule_sync.async_refresh(reason="service", serials=[sn])
            return

        for coord in iter_coordinators(hass):
            if hasattr(coord, "schedule_sync"):
                await coord.schedule_sync.async_refresh(reason="service")

    hass.services.async_register(
        DOMAIN, "force_refresh", _svc_force_refresh, schema=FORCE_REFRESH_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "start_charging", _svc_start, schema=START_SCHEMA
    )
    hass.services.async_register(DOMAIN, "stop_charging", _svc_stop, schema=STOP_SCHEMA)

    trigger_register_kwargs: dict[str, object] = {"schema": TRIGGER_SCHEMA}
    try:
        trigger_register_kwargs["supports_response"] = supports_response.OPTIONAL
    except AttributeError:
        trigger_register_kwargs["supports_response"] = supports_response
    hass.services.async_register(
        DOMAIN, "trigger_message", _svc_trigger, **trigger_register_kwargs
    )

    hass.services.async_register(
        DOMAIN,
        "request_grid_toggle_otp",
        _svc_request_grid_otp,
        schema=REQUEST_GRID_OTP_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "set_grid_mode", _svc_set_grid_mode, schema=SET_GRID_MODE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "clear_reauth_issue", _svc_clear_issue, schema=CLEAR_SCHEMA
    )
    hass.services.async_register(DOMAIN, "start_live_stream", _svc_start_stream)
    hass.services.async_register(DOMAIN, "stop_live_stream", _svc_stop_stream)
    hass.services.async_register(
        DOMAIN, "sync_schedules", _svc_sync_schedules, schema=SYNC_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "add_schedule", _svc_add_schedule, schema=ADD_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "update_schedule", _svc_update_schedule, schema=UPDATE_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "delete_schedule", _svc_delete_schedule, schema=DELETE_SCHEDULE_SCHEMA
    )
    validate_register_kwargs: dict[str, object] = {"schema": VALIDATE_SCHEDULE_SCHEMA}
    try:
        validate_register_kwargs["supports_response"] = supports_response.OPTIONAL
    except AttributeError:
        validate_register_kwargs["supports_response"] = supports_response
    hass.services.async_register(
        DOMAIN, "validate_schedule", _svc_validate_schedule, **validate_register_kwargs
    )
    hass.services.async_register(
        DOMAIN,
        "update_cfg_schedule",
        _svc_update_cfg_schedule,
        schema=UPDATE_CFG_SCHEMA,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove registered integration services."""

    for service in REGISTERED_SERVICES:
        hass.services.async_remove(DOMAIN, service)
