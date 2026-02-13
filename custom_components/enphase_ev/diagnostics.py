from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.helpers import device_registry as dr

from .const import CONF_EMAIL, DOMAIN
from .device_types import parse_type_identifier
from .runtime_data import get_runtime_data

DIAGNOSTIC_CAPTURE_ERRORS = (RuntimeError, TypeError, ValueError, AttributeError)

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

    try:
        coord = get_runtime_data(entry).coordinator
    except RuntimeError:
        return diag

    try:
        upd = (
            int(coord.update_interval.total_seconds())
            if coord.update_interval
            else None
        )
    except DIAGNOSTIC_CAPTURE_ERRORS:
        upd = None

    try:
        metrics: dict[str, Any] = coord.collect_site_metrics()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        metrics = {}

    try:
        last_modes = coord.charge_mode_cache_snapshot()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        last_modes = {}

    client = getattr(coord, "client", None)
    base_header_names: list[str] = []
    if client is not None:
        header_names = getattr(client, "base_header_names", None)
        if callable(header_names):
            try:
                base_header_names = header_names()
            except DIAGNOSTIC_CAPTURE_ERRORS:
                base_header_names = []
    has_scheduler_bearer = False
    if client is not None and hasattr(client, "has_scheduler_bearer"):
        try:
            has_scheduler_bearer = bool(client.has_scheduler_bearer())
        except DIAGNOSTIC_CAPTURE_ERRORS:
            has_scheduler_bearer = False

    try:
        session_history = coord.session_history_diagnostics()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        session_history = {}

    try:
        battery_config = coord.battery_diagnostics_payloads()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        battery_config = {}

    try:
        inverters = coord.inverter_diagnostics_payloads()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        inverters = {}

    try:
        scheduler = coord.scheduler_diagnostics()
    except DIAGNOSTIC_CAPTURE_ERRORS:
        scheduler = {}

    diag["coordinator"] = {
        "site_id": metrics.get("site_id", coord.site_id),
        "site_metrics": metrics or None,
        "serials_count": len(getattr(coord, "serials", []) or []),
        "update_interval_seconds": upd,
        "last_scheduler_modes": last_modes,
        "network_errors": metrics.get("network_errors", 0),
        "backoff_until_monotonic": metrics.get("backoff_until_monotonic"),
        "last_error": metrics.get("last_error"),
        "headers_info": {
            "base_header_names": base_header_names,
            "has_scheduler_bearer": has_scheduler_bearer,
        },
        "phase_timings": metrics.get("phase_timings", coord.phase_timings),
        "session_history": session_history,
        "battery_config": battery_config,
        "inverters": inverters,
        "scheduler": scheduler,
    }

    schedule_sync = getattr(coord, "schedule_sync", None)
    if schedule_sync is not None and hasattr(schedule_sync, "diagnostics"):
        try:
            diag["coordinator"]["schedule_sync"] = schedule_sync.diagnostics()
        except DIAGNOSTIC_CAPTURE_ERRORS:
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
            entry_data = dict(raw)
            lr = entry_data.get("last_report_date")
            if isinstance(lr, datetime):
                entry_data["last_report_date"] = lr.isoformat()
            site_energy[str(key)] = entry_data
    except DIAGNOSTIC_CAPTURE_ERRORS:
        site_energy = {}

    cache_age = getattr(energy, "site_energy_cache_age", None) if energy else None
    meta = getattr(energy, "site_energy_meta", None) if energy else None
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
            coord = get_runtime_data(entry).coordinator
        except RuntimeError:
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
        coord = get_runtime_data(entry).coordinator
    except RuntimeError:
        pass
    snapshot = (coord.data or {}).get(sn) if coord else None
    return {"serial": sn, "snapshot": snapshot or {}}
