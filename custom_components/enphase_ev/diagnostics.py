from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.helpers import device_registry as dr

from .const import CONF_EMAIL, DOMAIN

TO_REDACT = [
    "e_auth_token",
    "access_token",
    "cookie",
    "session_id",
    "enlighten_manager_token_production",
    "password",
    CONF_EMAIL,
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
            upd = (
                int(coord.update_interval.total_seconds())
                if coord.update_interval
                else None
            )
        except Exception:
            upd = None

        metrics: dict[str, Any] = {}
        if hasattr(coord, "collect_site_metrics"):
            try:
                metrics = coord.collect_site_metrics()
            except Exception:
                metrics = {}

        # Last scheduler mode(s) from cache: serial -> mode
        try:
            mode_cache = coord._charge_mode_cache  # noqa: SLF001 (diagnostics only)
            last_modes = {
                str(sn): str(val[0]) for sn, val in mode_cache.items() if val and val[0]
            }
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
            "site_id": metrics.get("site_id", coord.site_id),
            "site_metrics": metrics or None,
            "serials_count": len(getattr(coord, "serials", []) or []),
            "update_interval_seconds": upd,
            "last_scheduler_modes": last_modes,
            "network_errors": metrics.get(
                "network_errors", getattr(coord, "_network_errors", 0)
            ),
            "backoff_until_monotonic": getattr(coord, "_backoff_until", None),
            "last_error": metrics.get(
                "last_error", getattr(coord, "_last_error", None)
            ),
            "headers_info": {
                "base_header_names": base_header_names,
                "has_scheduler_bearer": has_scheduler_bearer,
            },
            "phase_timings": metrics.get("phase_timings", coord.phase_timings),
            "session_history": {
                "cache_ttl_seconds": getattr(coord, "_session_history_cache_ttl", None),
                "cache_keys": len(getattr(coord, "_session_history_cache", {})),
                "interval_minutes": getattr(
                    coord, "_session_history_interval_min", None
                ),
                "in_progress": len(
                    getattr(coord, "_session_refresh_in_progress", set()) or []
                ),
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
