from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.helpers import device_registry as dr

from .const import CONF_EMAIL, DOMAIN
from .device_types import parse_type_identifier

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

        session_manager = getattr(coord, "session_history", None)
        cache_ttl = getattr(coord, "_session_history_cache_ttl", None)
        cache_keys = len(getattr(coord, "_session_history_cache", {}))
        in_progress = len(getattr(coord, "_session_refresh_in_progress", set()) or [])
        if session_manager is not None:
            cache_ttl = session_manager.cache_ttl
            cache_keys = session_manager.cache_key_count
            in_progress = session_manager.in_progress

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
                "cache_ttl_seconds": cache_ttl,
                "cache_keys": cache_keys,
                "interval_minutes": getattr(
                    coord, "_session_history_interval_min", None
                ),
                "in_progress": in_progress,
            },
            "battery_config": {
                "site_settings_payload": getattr(
                    coord, "_battery_site_settings_payload", None
                ),
                "profile_payload": getattr(coord, "_battery_profile_payload", None),
                "settings_payload": getattr(coord, "_battery_settings_payload", None),
                "devices_inventory_payload": getattr(
                    coord, "_devices_inventory_payload", None
                ),
            },
            "inverters": {
                "enabled": bool(getattr(coord, "include_inverters", True)),
                "summary_counts": getattr(coord, "_inverter_summary_counts", None),
                "model_counts": getattr(coord, "_inverter_model_counts", None),
                "inventory_payload": getattr(
                    coord, "_inverters_inventory_payload", None
                ),
                "status_payload": getattr(coord, "_inverter_status_payload", None),
                "production_payload": getattr(
                    coord, "_inverter_production_payload", None
                ),
            },
            "scheduler": {
                "available": getattr(coord, "scheduler_available", None),
                "last_error": getattr(coord, "scheduler_last_error", None),
                "failures": getattr(coord, "_scheduler_failures", None),
                "backoff_until_monotonic": getattr(
                    coord, "_scheduler_backoff_until", None
                ),
                "backoff_ends_utc": None,
            },
        }
        try:
            sched_end = getattr(coord, "_scheduler_backoff_ends_utc", None)
            if isinstance(sched_end, datetime):
                diag["coordinator"]["scheduler"][
                    "backoff_ends_utc"
                ] = sched_end.isoformat()
        except Exception:
            pass
        schedule_sync = getattr(coord, "schedule_sync", None)
        if schedule_sync is not None and hasattr(schedule_sync, "diagnostics"):
            try:
                diag["coordinator"]["schedule_sync"] = schedule_sync.diagnostics()
            except Exception:
                diag["coordinator"]["schedule_sync"] = None

        site_energy: dict[str, Any] = {}
        energy = getattr(coord, "energy", None)
        try:
            flows = (
                getattr(energy, "site_energy", None)
                if energy is not None
                else getattr(coord, "site_energy", None)
            ) or {}
            for key, flow in flows.items():
                if flow is None:
                    continue
                raw = flow
                if hasattr(flow, "__dict__"):
                    raw = flow.__dict__
                if not isinstance(raw, dict):
                    continue
                entry = dict(raw)
                lr = entry.get("last_report_date")
                if isinstance(lr, datetime):
                    entry["last_report_date"] = lr.isoformat()
                site_energy[str(key)] = entry
        except Exception:
            site_energy = {}
        try:
            if energy is not None and hasattr(energy, "_site_energy_cache_age"):
                cache_age = energy._site_energy_cache_age()  # noqa: SLF001
            elif hasattr(coord, "_site_energy_cache_age"):
                cache_age = coord._site_energy_cache_age()  # noqa: SLF001
            else:
                cache_age = None
        except Exception:
            cache_age = None
        meta = (
            getattr(energy, "_site_energy_meta", None)
            if energy is not None
            else getattr(coord, "_site_energy_meta", None)
        )
        if isinstance(meta, dict):
            meta_copy = dict(meta)
            lr_meta = meta_copy.get("last_report_date")
            if isinstance(lr_meta, datetime):
                meta_copy["last_report_date"] = lr_meta.isoformat()
            meta = meta_copy
        if site_energy or meta:
            diag["site_energy"] = {
                "flows": site_energy or None,
                "meta": meta,
                "cache_age_s": cache_age,
            }

    return diag


async def async_get_device_diagnostics(hass, entry, device):
    """Return diagnostics for a device."""
    dev_reg = dr.async_get(hass)
    dev = dev_reg.async_get(device.id)
    if not dev:
        return {"error": "device_not_found"}
    sn = None
    type_key = None
    type_site_id = None
    for domain, ident in dev.identifiers:
        if domain != DOMAIN:
            continue
        ident_text = str(ident)
        if ident_text.startswith("site:"):
            continue
        if ident_text.startswith("type:"):
            parsed = parse_type_identifier(ident_text)
            if parsed:
                type_site_id, type_key = parsed
            continue
        sn = ident_text
        break
    if type_key:
        coord = None
        try:
            coord = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        except Exception:
            pass
        bucket = (
            coord.type_bucket(type_key)
            if coord and hasattr(coord, "type_bucket")
            else None
        )
        return {
            "site_id": type_site_id,
            "type_key": type_key,
            "type_label": (
                bucket.get("type_label")
                if isinstance(bucket, dict)
                else (
                    coord.type_label(type_key)
                    if coord and hasattr(coord, "type_label")
                    else None
                )
            ),
            "count": (bucket.get("count", 0) if isinstance(bucket, dict) else 0),
            "devices": (bucket.get("devices", []) if isinstance(bucket, dict) else []),
        }
    if not sn:
        return {"error": "serial_not_resolved"}
    coord = None
    try:
        coord = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    except Exception:
        pass
    snapshot = (coord.data or {}).get(sn) if coord else None
    return {"serial": sn, "snapshot": snapshot or {}}
