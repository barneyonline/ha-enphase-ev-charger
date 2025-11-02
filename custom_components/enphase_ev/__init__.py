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

from .const import CONF_SITE_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [
    "sensor",
    "binary_sensor",
    "button",
    "select",
    "number",
    "switch",
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    entry_data = data.setdefault(entry.entry_id, {})

    # Create and prime the coordinator once, used by all platforms
    from .coordinator import (
        EnphaseCoordinator,
    )  # local import to avoid heavy deps during non-HA imports

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    entry_data["coordinator"] = coord
    await coord.async_config_entry_first_refresh()

    # Register a parent site device to link chargers via via_device
    site_id = entry.data.get("site_id")
    site_label = entry.data.get(CONF_SITE_NAME) or (
        f"Enphase Site {site_id}" if site_id else "Enphase Site"
    )
    dev_reg = dr.async_get(hass)
    site_dev = None
    if site_id:
        # Ensure the parent site device exists; keep the entry for via_device_id linking
        site_dev = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"site:{site_id}")},
            manufacturer="Enphase",
            name=site_label,
            model="Enlighten Cloud",
        )

    # One-time backfill/update of charger Device registry info for existing installs
    # This harmonizes name/model/version and links chargers to the site via via_device
    serials: list[str] = coord.iter_serials()
    for sn in serials:
        d = (coord.data or {}).get(sn) or {}
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
        if site_dev is not None:
            # Link the charger device via the parent site using identifiers
            kwargs["via_device"] = (DOMAIN, f"site:{site_id}")
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
        # Device registry does not support a separate model_id field; ignore it
        hw = d.get("hw_version")
        if hw:
            kwargs["hw_version"] = str(hw)
        sw = d.get("sw_version")
        if sw:
            kwargs["sw_version"] = str(sw)
        # Compare with existing device and only log if a change is needed
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
            if site_dev is not None and existing.via_device_id != site_dev.id:
                changes.append("via_device")
        if changes:
            _LOGGER.debug(
                (
                    "Device registry update (%s) for charger serial=%s (site=%s): "
                    "name=%s, model=%s, model_id=%s, hw=%s, sw=%s, link_via_site=%s"
                ),
                ",".join(changes),
                sn,
                site_id,
                dev_name,
                model_name,
                model_id,
                hw,
                sw,
                bool(site_dev is not None),
            )
        # Idempotent: updates existing device or creates if missing
        dev_reg.async_get_or_create(**kwargs)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services once
    if not data.get("_services_registered"):
        _register_services(hass)
        data["_services_registered"] = True
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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
        via = dev.via_device_id
        if via:
            parent = dev_reg.async_get(via)
            if parent:
                for domain, identifier in parent.identifiers:
                    if domain == DOMAIN and identifier.startswith("site:"):
                        return identifier.partition(":")[2]
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
            await coord.client.start_live_stream()
            coord._streaming = (
                True  # noqa: SLF001 - internal flag for coordinator behaviour
            )
            await coord.async_request_refresh()

    async def _svc_stop_stream(call):
        site_ids = await _resolve_site_ids_from_call(call)
        coords = list(_iter_coordinators(site_ids or None))
        if not coords:
            return
        if not site_ids:
            coords = coords[:1]
        for coord in coords:
            await coord.client.stop_live_stream()
            coord._streaming = (
                False  # noqa: SLF001 - internal flag for coordinator behaviour
            )
            await coord.async_request_refresh()

    hass.services.async_register(DOMAIN, "start_live_stream", _svc_start_stream)
    hass.services.async_register(DOMAIN, "stop_live_stream", _svc_stop_stream)
