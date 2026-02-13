from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import service as ha_service

from .const import DOMAIN
from .device_types import parse_type_identifier
from .runtime_data import iter_coordinators

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

REGISTERED_SERVICES = (
    "start_charging",
    "stop_charging",
    "trigger_message",
    "request_grid_toggle_otp",
    "set_grid_mode",
    "clear_reauth_issue",
    "start_live_stream",
    "stop_live_stream",
    "sync_schedules",
)


def async_setup_services(
    hass: HomeAssistant, *, supports_response: object = SupportsResponse
) -> None:
    """Register integration services once."""

    if hass.services.has_service(DOMAIN, "start_charging"):
        return

    from .coordinator import ServiceValidationError

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

    DEVICE_ID_LIST = vol.All(cv.ensure_list, [cv.string])

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

        issue_ids = {"reauth_required"}
        for site_id in site_ids:
            issue_ids.add(f"reauth_required_{site_id}")
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


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove registered integration services."""

    for service in REGISTERED_SERVICES:
        hass.services.async_remove(DOMAIN, service)
