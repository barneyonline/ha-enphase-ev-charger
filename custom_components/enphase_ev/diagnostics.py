
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN


TO_REDACT = [
    "e_auth_token",
    "cookie",
    "enlighten_manager_token_production",
]


async def async_get_config_entry_diagnostics(hass, entry):
    data = async_redact_data(dict(entry.data), TO_REDACT)
    options = dict(getattr(entry, "options", {}) or {})

    diag: dict[str, Any] = {
        "entry_data": data,
        "entry_options": options,
    }

    # Coordinator/site diagnostics (if available)
    try:
        coord = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    except Exception:
        coord = None

    if coord is not None:
        # Update interval seconds (dynamic)
        try:
            upd = int(coord.update_interval.total_seconds()) if coord.update_interval else None
        except Exception:
            upd = None

        # Last scheduler mode(s) from cache: serial -> mode
        try:
            mode_cache = coord._charge_mode_cache  # noqa: SLF001 (diagnostics only)
            last_modes = {str(sn): str(val[0]) for sn, val in mode_cache.items() if val and val[0]}
        except Exception:
            last_modes = {}

        # Header names used by the client (values redacted). Also note if
        # scheduler bearer token is derivable from cookies.
        try:
            client = coord.client
            base_header_names = sorted(list(getattr(client, "_h", {}).keys()))
            has_scheduler_bearer = bool(client._bearer())  # noqa: SLF001
        except Exception:
            base_header_names = []
            has_scheduler_bearer = False

        diag["coordinator"] = {
            "site_id": coord.site_id,
            "serials_count": len(getattr(coord, "serials", []) or []),
            "update_interval_seconds": upd,
            "last_scheduler_modes": last_modes,
            "headers_info": {
                "base_header_names": base_header_names,
                "has_scheduler_bearer": has_scheduler_bearer,
            },
        }

    return diag


async def async_get_device_diagnostics(hass, entry, device):
    """Return diagnostics for a device."""
    dev_reg = dr.async_get(hass)
    dev = dev_reg.async_get(device.id)
    if not dev:
        return {"error": "device_not_found"}
    sn = None
    for domain, ident in dev.identifiers:
        if domain == DOMAIN and not str(ident).startswith("site:"):
            sn = str(ident)
            break
    if not sn:
        return {"error": "serial_not_resolved"}
    coord = None
    try:
        coord = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    except Exception:
        pass
    snapshot = (coord.data or {}).get(sn) if coord else None
    return {"serial": sn, "snapshot": snapshot or {}}
