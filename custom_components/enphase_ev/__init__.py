
from __future__ import annotations

import logging

import voluptuous as vol

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import config_validation as cv
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import issue_registry as ir
except Exception:  # pragma: no cover - allow import without HA for unit tests
    ConfigEntry = object  # type: ignore[misc,assignment]
    HomeAssistant = object  # type: ignore[misc,assignment]
    dr = None  # type: ignore[assignment]
    cv = None  # type: ignore[assignment]
    ir = None  # type: ignore[assignment]

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "binary_sensor", "button", "select", "number", "switch"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    entry_data = data.setdefault(entry.entry_id, {})

    # Create and prime the coordinator once, used by all platforms
    from .coordinator import EnphaseCoordinator  # local import to avoid heavy deps during non-HA imports
    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    entry_data["coordinator"] = coord
    await coord.async_config_entry_first_refresh()

    # Register a parent site device to link chargers via via_device
    site_id = entry.data.get("site_id")
    dev_reg = dr.async_get(hass)
    if site_id:
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"site:{site_id}")},
            manufacturer="Enphase",
            name=f"Enphase Site {site_id}",
            model="Enlighten Cloud",
        )

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

    START_SCHEMA = vol.Schema({
        vol.Required("device_id"): cv.string,
        vol.Optional("charging_level", default=32): vol.All(int, vol.Range(min=6, max=40)),
        vol.Optional("connector_id", default=1): vol.All(int, vol.Range(min=1, max=2)),
    })

    STOP_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

    TRIGGER_SCHEMA = vol.Schema({
        vol.Required("device_id"): cv.string,
        vol.Required("requested_message"): cv.string,
    })

    async def _svc_start(call):
        sn = await _resolve_sn(call.data["device_id"])
        if not sn:
            return
        coord = await _get_coordinator_for_sn(sn)
        if not coord:
            return
        level = call.data.get("charging_level")
        if level is None:
            level = coord.last_set_amps.get(sn, 32)
        await coord.client.start_charging(sn, int(level), int(call.data.get("connector_id", 1)))
        coord.kick_fast(90)
        await coord.async_request_refresh()

    async def _svc_stop(call):
        sn = await _resolve_sn(call.data["device_id"])
        if not sn:
            return
        coord = await _get_coordinator_for_sn(sn)
        if not coord:
            return
        await coord.client.stop_charging(sn)
        coord.kick_fast(60)
        await coord.async_request_refresh()

    async def _svc_trigger(call):
        sn = await _resolve_sn(call.data["device_id"])
        if not sn:
            return
        coord = await _get_coordinator_for_sn(sn)
        if not coord:
            return
        await coord.client.trigger_message(sn, call.data["requested_message"])
        await coord.async_request_refresh()

    hass.services.async_register(DOMAIN, "start_charging", _svc_start, schema=START_SCHEMA)
    hass.services.async_register(DOMAIN, "stop_charging", _svc_stop, schema=STOP_SCHEMA)
    hass.services.async_register(DOMAIN, "trigger_message", _svc_trigger, schema=TRIGGER_SCHEMA)

    # Manual clear of reauth issue (useful if issue lingers after reauth)
    CLEAR_SCHEMA = vol.Schema({vol.Optional("site_id"): cv.string})

    async def _svc_clear_issue(call):
        # Currently we use a single issue id; clear it regardless of site
        ir.async_delete_issue(hass, DOMAIN, "reauth_required")

    hass.services.async_register(DOMAIN, "clear_reauth_issue", _svc_clear_issue, schema=CLEAR_SCHEMA)

    # Live stream control (site-wide)
    async def _svc_start_stream(call):
        # Use any coordinator; streaming is site scoped
        coord = None
        for entry_data in hass.data.get(DOMAIN, {}).values():
            if isinstance(entry_data, dict) and "coordinator" in entry_data:
                coord = entry_data["coordinator"]
                break
        if not coord:
            return
        await coord.client.start_live_stream()
        coord._streaming = True
        await coord.async_request_refresh()

    async def _svc_stop_stream(call):
        coord = None
        for entry_data in hass.data.get(DOMAIN, {}).values():
            if isinstance(entry_data, dict) and "coordinator" in entry_data:
                coord = entry_data["coordinator"]
                break
        if not coord:
            return
        await coord.client.stop_live_stream()
        coord._streaming = False
        await coord.async_request_refresh()

    hass.services.async_register(DOMAIN, "start_live_stream", _svc_start_stream)
    hass.services.async_register(DOMAIN, "stop_live_stream", _svc_stop_stream)
