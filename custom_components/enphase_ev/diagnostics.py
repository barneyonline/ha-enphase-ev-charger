
from __future__ import annotations

from typing import Any

from .const import DOMAIN


def _redact_entry_data(d: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    for k in ("e_auth_token", "cookie", "enlighten_manager_token_production"):
        if k in out:
            out[k] = "***redacted***"
    return out


async def async_get_config_entry_diagnostics(hass, entry):
    data = _redact_entry_data(dict(entry.data))
    options = dict(entry.options or {})

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
            mode_cache = coord._charge_mode_cache  # noqa: SLF001 (introspecting for diagnostics only)
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
