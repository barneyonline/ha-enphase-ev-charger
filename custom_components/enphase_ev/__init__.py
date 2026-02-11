from __future__ import annotations

import logging

import voluptuous as vol

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, SupportsResponse
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers import service as ha_service
except Exception:  # pragma: no cover - allow import without HA for unit tests
    ConfigEntry = object  # type: ignore[misc,assignment]
    HomeAssistant = object  # type: ignore[misc,assignment]
    SupportsResponse = None  # type: ignore[assignment]
    dr = None  # type: ignore[assignment]
    cv = None  # type: ignore[assignment]
    ir = None  # type: ignore[assignment]
    ha_service = None  # type: ignore[assignment]

from .const import DOMAIN
from .device_types import parse_type_identifier

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [
    "sensor",
    "binary_sensor",
    "button",
    "select",
    "number",
    "switch",
    "time",
    "calendar",
]


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _sync_type_devices(entry: ConfigEntry, coord, dev_reg, site_id: object) -> dict[str, object]:
    """Create or update type devices from coordinator inventory."""
    type_devices: dict[str, object] = {}
    iter_type_keys = getattr(coord, "iter_type_keys", None)
    type_identifier_fn = getattr(coord, "type_identifier", None)
    type_label_fn = getattr(coord, "type_label", None)
    type_device_name_fn = getattr(coord, "type_device_name", None)
    type_device_model_fn = getattr(coord, "type_device_model", None)
    type_device_hw_version_fn = getattr(coord, "type_device_hw_version", None)
    type_keys = list(iter_type_keys()) if callable(iter_type_keys) else []
    for type_key in type_keys:
        ident = type_identifier_fn(type_key) if callable(type_identifier_fn) else None
        if ident is None:
            continue
        label = type_label_fn(type_key) if callable(type_label_fn) else None
        name = type_device_name_fn(type_key) if callable(type_device_name_fn) else None
        model = (
            type_device_model_fn(type_key)
            if callable(type_device_model_fn)
            else None
        ) or label
        hw_version = (
            type_device_hw_version_fn(type_key)
            if callable(type_device_hw_version_fn)
            else None
        )
        if not label or not name:
            continue
        kwargs = {
            "config_entry_id": entry.entry_id,
            "identifiers": {ident},
            "manufacturer": "Enphase",
            "name": name,
            "model": model,
        }
        if isinstance(hw_version, str) and hw_version.strip():
            kwargs["hw_version"] = hw_version.strip()
        existing = dev_reg.async_get_device(identifiers={ident})
        changes: list[str] = []
        if existing is None:
            changes.append("new_device")
        else:
            if existing.name != name:
                changes.append("name")
            if existing.manufacturer != "Enphase":
                changes.append("manufacturer")
            if existing.model != model:
                changes.append("model")
            if kwargs.get("hw_version") and existing.hw_version != kwargs["hw_version"]:
                changes.append("hw_version")
        if changes:
            _LOGGER.debug(
                (
                    "Device registry update (%s) for type device %s (site=%s): "
                    "name=%s model=%s hw=%s"
                ),
                ",".join(changes),
                type_key,
                site_id,
                name,
                model,
                kwargs.get("hw_version"),
            )
        created = dev_reg.async_get_or_create(**kwargs)
        type_devices[type_key] = created
    return type_devices


def _sync_charger_devices(
    entry: ConfigEntry, coord, dev_reg, site_id: object, type_devices: dict[str, object]
) -> None:
    """Create or update charger devices and parent links."""
    type_identifier_fn = getattr(coord, "type_identifier", None)
    evse_parent_ident = (
        type_identifier_fn("iqevse") if callable(type_identifier_fn) else None
    )
    evse_parent_id = None
    evse_parent = type_devices.get("iqevse")
    if evse_parent is None and evse_parent_ident is not None:
        evse_parent = dev_reg.async_get_device(identifiers={evse_parent_ident})
    if evse_parent is not None:
        evse_parent_id = getattr(evse_parent, "id", None)

    iter_serials = getattr(coord, "iter_serials", None)
    serials = list(iter_serials()) if callable(iter_serials) else []
    data_source = coord.data if isinstance(getattr(coord, "data", None), dict) else {}
    for sn in serials:
        d = data_source.get(sn) or {}
        display_name_raw = d.get("display_name")
        display_name = str(display_name_raw) if display_name_raw else None
        fallback_name_raw = d.get("name")
        fallback_name = str(fallback_name_raw) if fallback_name_raw else None
        dev_name = display_name or fallback_name or f"Charger {sn}"
        kwargs = {
            "config_entry_id": entry.entry_id,
            "identifiers": {(DOMAIN, sn)},
            "manufacturer": "Enphase",
            "name": dev_name,
            "serial_number": str(sn),
        }
        if evse_parent_ident is not None:
            kwargs["via_device"] = evse_parent_ident
        model_name_raw = d.get("model_name")
        model_name = str(model_name_raw) if model_name_raw else None
        model_display = None
        if display_name and model_name:
            model_display = f"{display_name} ({model_name})"
        elif model_name:
            model_display = model_name
        elif display_name:
            model_display = display_name
        elif dev_name:
            model_display = dev_name
        if model_display:
            kwargs["model"] = model_display
        model_id = d.get("model_id")
        hw = d.get("hw_version")
        if hw:
            kwargs["hw_version"] = str(hw)
        sw = d.get("sw_version")
        if sw:
            kwargs["sw_version"] = str(sw)

        changes: list[str] = []
        existing = dev_reg.async_get_device(identifiers={(DOMAIN, sn)})
        if existing is None:
            changes.append("new_device")
        else:
            if existing.name != dev_name:
                changes.append("name")
            if existing.manufacturer != "Enphase":
                changes.append("manufacturer")
            if model_display and existing.model != model_display:
                changes.append("model")
            if hw and existing.hw_version != str(hw):
                changes.append("hw_version")
            if sw and existing.sw_version != str(sw):
                changes.append("sw_version")
            if evse_parent_id is not None and existing.via_device_id != evse_parent_id:
                changes.append("via_device")
        if changes:
            _LOGGER.debug(
                (
                    "Device registry update (%s) for charger serial=%s (site=%s): "
                    "name=%s, model=%s, model_id=%s, hw=%s, sw=%s, link_via_ev_type=%s"
                ),
                ",".join(changes),
                sn,
                site_id,
                dev_name,
                model_name,
                model_id,
                hw,
                sw,
                bool(evse_parent_ident is not None),
            )
        dev_reg.async_get_or_create(**kwargs)


def _sync_registry_devices(entry: ConfigEntry, coord, dev_reg, site_id: object) -> None:
    type_devices = _sync_type_devices(entry, coord, dev_reg, site_id)
    _sync_charger_devices(entry, coord, dev_reg, site_id, type_devices)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    entry_data = data.setdefault(entry.entry_id, {})
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Create and prime the coordinator once, used by all platforms
    from .coordinator import (
        EnphaseCoordinator,
    )  # local import to avoid heavy deps during non-HA imports

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    entry_data["coordinator"] = coord
    await coord.async_config_entry_first_refresh()

    site_id = entry.data.get("site_id")
    dev_reg = dr.async_get(hass)
    _sync_registry_devices(entry, coord, dev_reg, site_id)

    add_listener = getattr(coord, "async_add_listener", None)
    if callable(add_listener):
        def _sync_registry_on_update() -> None:
            try:
                _sync_registry_devices(entry, coord, dev_reg, site_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Skipping registry sync for site %s after update: %s", site_id, err
                )

        entry.async_on_unload(add_listener(_sync_registry_on_update))

    # Start schedule sync after device registry has been updated to ensure linking.
    if hasattr(coord, "schedule_sync"):
        # Run schedule sync startup in the background to avoid blocking setup
        hass.async_create_task(coord.schedule_sync.async_start())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services once
    if not data.get("_services_registered"):
        _register_services(hass)
        data["_services_registered"] = True
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coord = entry_data.get("coordinator") if isinstance(entry_data, dict) else None
    if coord is not None and hasattr(coord, "schedule_sync"):
        await coord.schedule_sync.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


def _register_services(hass: HomeAssistant) -> None:
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

    async def _get_coordinator_for_sn(sn: str):
        # Find the coordinator that has this serial
        for entry_data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(entry_data, dict) or "coordinator" not in entry_data:
                continue
            coord = entry_data["coordinator"]
            # Coordinator may not have data yet; still return the first one
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

    def _extract_device_ids(call) -> list[str]:
        device_ids: set[str] = set()
        if ha_service is not None:
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

    async def _resolve_site_ids_from_call(call) -> set[str]:
        site_ids: set[str] = set()
        for device_id in _extract_device_ids(call):
            site_id = await _resolve_site_id(device_id)
            if site_id:
                site_ids.add(site_id)
        explicit = call.data.get("site_id")
        if explicit:
            site_ids.add(str(explicit))
        return site_ids

    def _iter_coordinators(site_ids: set[str] | None = None):
        seen: set[str] = set()
        for entry_data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(entry_data, dict) or "coordinator" not in entry_data:
                continue
            coord = entry_data["coordinator"]
            if site_ids and str(coord.site_id) not in site_ids:
                continue
            if str(coord.site_id) in seen:
                continue
            seen.add(str(coord.site_id))
            yield coord

    async def _svc_start(call):
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

    async def _svc_stop(call):
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

    async def _svc_trigger(call):
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

    hass.services.async_register(
        DOMAIN, "start_charging", _svc_start, schema=START_SCHEMA
    )
    hass.services.async_register(DOMAIN, "stop_charging", _svc_stop, schema=STOP_SCHEMA)
    trigger_register_kwargs: dict[str, object] = {"schema": TRIGGER_SCHEMA}
    if SupportsResponse is not None:
        try:
            trigger_register_kwargs["supports_response"] = SupportsResponse.OPTIONAL
        except AttributeError:
            trigger_register_kwargs["supports_response"] = SupportsResponse
    hass.services.async_register(
        DOMAIN, "trigger_message", _svc_trigger, **trigger_register_kwargs
    )

    # Manual clear of reauth issue (useful if issue lingers after reauth)
    CLEAR_SCHEMA = vol.Schema(
        {
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
        }
    )

    async def _svc_clear_issue(call):
        site_ids: set[str] = set()
        for device_id in call.data.get("device_id", []) or []:
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

    hass.services.async_register(
        DOMAIN, "clear_reauth_issue", _svc_clear_issue, schema=CLEAR_SCHEMA
    )

    # Live stream control (site-wide)
    async def _svc_start_stream(call):
        site_ids = await _resolve_site_ids_from_call(call)
        coords = list(_iter_coordinators(site_ids or None))
        if not coords:
            return
        if not site_ids:
            coords = coords[:1]
        for coord in coords:
            await coord.async_start_streaming(manual=True)
            await coord.async_request_refresh()

    async def _svc_stop_stream(call):
        site_ids = await _resolve_site_ids_from_call(call)
        coords = list(_iter_coordinators(site_ids or None))
        if not coords:
            return
        if not site_ids:
            coords = coords[:1]
        for coord in coords:
            await coord.async_stop_streaming(manual=True)
            await coord.async_request_refresh()

    hass.services.async_register(DOMAIN, "start_live_stream", _svc_start_stream)
    hass.services.async_register(DOMAIN, "stop_live_stream", _svc_stop_stream)

    SYNC_SCHEMA = vol.Schema({vol.Optional("device_id"): DEVICE_ID_LIST})

    async def _svc_sync_schedules(call):
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
        for coord in _iter_coordinators():
            if hasattr(coord, "schedule_sync"):
                await coord.schedule_sync.async_refresh(reason="service")

    hass.services.async_register(
        DOMAIN, "sync_schedules", _svc_sync_schedules, schema=SYNC_SCHEMA
    )
