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


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        out = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    return out or None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "y", "enabled", "on"):
            return True
        if normalized in ("false", "0", "no", "n", "disabled", "off"):
            return False
    return None


def _normalize_gateway_status(value: Any) -> str:
    text = _text(value)
    if not text:
        return "unknown"
    normalized = text.lower().replace("-", "_").replace(" ", "_")
    if any(token in normalized for token in ("fault", "error", "critical")):
        return "error"
    if "warn" in normalized:
        return "warning"
    if any(
        token in normalized for token in ("not_reporting", "offline", "disconnected")
    ):
        return "not_reporting"
    if any(token in normalized for token in ("normal", "online", "connected", "ok")):
        return "normal"
    return "unknown"


def _gateway_summary(devices: list[dict[str, Any]], count: int) -> dict[str, Any]:
    total = max(count, len(devices))
    connected = 0
    disconnected = 0
    status_counts: dict[str, int] = {
        "normal": 0,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
        "unknown": 0,
    }
    model_counts: dict[str, int] = {}
    firmware_counts: dict[str, int] = {}
    property_keys: set[str] = set()

    for member in devices:
        property_keys.update(str(key) for key in member.keys())
        status_raw = member.get("statusText")
        if status_raw is None:
            status_raw = member.get("status")
        status = _normalize_gateway_status(status_raw)
        status_counts[status] = status_counts.get(status, 0) + 1

        connected_raw = _optional_bool(member.get("connected"))
        if connected_raw is None:
            if status == "normal":
                connected_raw = True
            elif status == "not_reporting":
                connected_raw = False
        if connected_raw is True:
            connected += 1
        elif connected_raw is False:
            disconnected += 1

        model = _text(member.get("model")) or _text(member.get("channel_type"))
        if model:
            model_counts[model] = model_counts.get(model, 0) + 1

        firmware = _text(member.get("envoy_sw_version")) or _text(
            member.get("sw_version")
        )
        if firmware:
            firmware_counts[firmware] = firmware_counts.get(firmware, 0) + 1

    unknown_connection = max(0, total - connected - disconnected)
    if total <= 0:
        connectivity = None
    elif connected >= total:
        connectivity = "online"
    elif connected == 0 and disconnected > 0:
        connectivity = "offline"
    elif connected > 0:
        connectivity = "degraded"
    else:
        connectivity = "unknown"

    return {
        "connectivity": connectivity,
        "connected_devices": connected,
        "disconnected_devices": disconnected,
        "unknown_connection_devices": unknown_connection,
        "status_counts": status_counts,
        "model_counts": model_counts,
        "firmware_counts": firmware_counts,
        "property_keys": sorted(property_keys),
    }


def _microinverter_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    try:
        total = int(payload.get("count", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    status_counts_raw = payload.get("status_counts")
    if isinstance(status_counts_raw, dict):
        status_counts = {
            key: _safe_int(status_counts_raw.get(key, 0) or 0, 0)
            for key in (
                "total",
                "normal",
                "warning",
                "error",
                "not_reporting",
                "unknown",
            )
        }
    else:
        status_counts = {
            "total": total,
            "normal": 0,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
            "unknown": total,
        }
    total = max(total, int(status_counts.get("total", 0) or 0))
    not_reporting = max(0, int(status_counts.get("not_reporting", 0) or 0))
    unknown = max(0, int(status_counts.get("unknown", 0) or 0))
    if (
        total > 0
        and int(status_counts.get("total", 0) or 0) <= 0
        and max(
            0,
            int(status_counts.get("normal", 0) or 0)
            + int(status_counts.get("warning", 0) or 0)
            + int(status_counts.get("error", 0) or 0)
            + not_reporting
            + unknown,
        )
        == 0
    ):
        unknown = total
    if unknown + not_reporting > total:
        unknown = max(0, total - not_reporting)
    reporting = max(0, total - not_reporting - unknown)
    if total <= 0:
        connectivity = None
    elif reporting >= total:
        connectivity = "online"
    elif reporting == 0 and unknown > 0:
        connectivity = "unknown"
    elif reporting > 0:
        connectivity = "degraded"
    else:
        connectivity = "offline"
    return {
        "connectivity": connectivity,
        "total_inverters": total,
        "reporting_inverters": reporting,
        "not_reporting_inverters": not_reporting,
        "unknown_inverters": unknown,
        "status_counts": status_counts,
        "status_summary": payload.get("status_summary"),
        "model_summary": payload.get("model_summary"),
        "firmware_summary": payload.get("firmware_summary"),
        "array_summary": payload.get("array_summary"),
        "status_type_counts": payload.get("status_type_counts"),
        "panel_info": payload.get("panel_info"),
        "latest_reported_utc": payload.get("latest_reported_utc"),
        "latest_reported_device": payload.get("latest_reported_device"),
        "production_start_date": payload.get("production_start_date"),
        "production_end_date": payload.get("production_end_date"),
    }


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
        payload = {
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
        if isinstance(bucket, dict):
            for key, value in bucket.items():
                if key in payload or key == "devices":
                    continue
                if isinstance(value, dict):
                    payload[key] = dict(value)
                elif isinstance(value, list):
                    payload[key] = list(value)
                else:
                    payload[key] = value
        if type_key == "envoy":
            devices = payload.get("devices")
            if isinstance(devices, list):
                safe_devices = [item for item in devices if isinstance(item, dict)]
            else:
                safe_devices = []
            try:
                gateway_count = int(payload.get("count", 0) or 0)
            except (TypeError, ValueError):
                gateway_count = 0
            payload["gateway_summary"] = _gateway_summary(safe_devices, gateway_count)
        elif type_key == "microinverter":
            payload["microinverter_summary"] = _microinverter_summary(payload)
        return payload
    if not sn:
        return {"error": "serial_not_resolved"}
    coord = None
    try:
        coord = get_runtime_data(entry).coordinator
    except RuntimeError:
        pass
    snapshot = (coord.data or {}).get(sn) if coord else None
    return {"serial": sn, "snapshot": snapshot or {}}
