from __future__ import annotations

from datetime import datetime
from datetime import timezone as _tz
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util


def coerce_int(value: object, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def coerce_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except Exception:  # noqa: BLE001
        return None


def copy_diagnostics_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: copy_diagnostics_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_diagnostics_value(item) for item in value]
    return value


def normalize_iso_date(value: object) -> str | None:
    if value is None:
        return None
    try:
        cleaned = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    if not cleaned:
        return None
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").date().isoformat()
    except Exception:  # noqa: BLE001
        return None


def resolve_inverter_start_date(
    site_energy_meta: object,
    inverter_data: object,
) -> str | None:
    start_date: str | None = None
    if isinstance(site_energy_meta, dict):
        start_date = normalize_iso_date(site_energy_meta.get("start_date"))
    if start_date:
        return start_date

    existing_starts: list[str] = []
    if isinstance(inverter_data, dict):
        for payload in inverter_data.values():
            if not isinstance(payload, dict):
                continue
            normalized = normalize_iso_date(payload.get("lifetime_query_start_date"))
            if normalized:
                existing_starts.append(normalized)
    if existing_starts:
        return min(existing_starts)
    return None


def resolve_site_timezone_name(battery_timezone: object) -> str:
    tz_name = battery_timezone
    if isinstance(tz_name, str) and tz_name.strip():
        try:
            ZoneInfo(tz_name.strip())
        except Exception:  # noqa: BLE001
            pass
        else:
            return tz_name.strip()
    return "UTC"


def resolve_site_local_current_date(
    devices_inventory_payload: object,
    battery_timezone: object,
) -> str:
    inventory_payload = (
        devices_inventory_payload
        if isinstance(devices_inventory_payload, dict)
        else None
    )
    if inventory_payload is not None:
        direct = normalize_iso_date(inventory_payload.get("curr_date_site"))
        if direct:
            return direct
        result = inventory_payload.get("result")
        if isinstance(result, list):
            for item in result:
                if not isinstance(item, dict):
                    continue
                candidate = normalize_iso_date(item.get("curr_date_site"))
                if candidate:
                    return candidate

    tz_name = battery_timezone
    if isinstance(tz_name, str) and tz_name.strip():
        try:
            return datetime.now(ZoneInfo(tz_name.strip())).date().isoformat()
        except Exception:  # noqa: BLE001
            pass

    try:
        return dt_util.now().date().isoformat()
    except Exception:  # noqa: BLE001
        return datetime.now(tz=_tz.utc).date().isoformat()


def redact_battery_payload(value: object) -> object:
    sensitive = {
        "email",
        "authorization",
        "cookie",
        "token",
        "access_token",
        "refresh_token",
        "xsrf_token",
        "x_xsrf_token",
        "session_id",
        "userid",
        "user_id",
        "username",
        "device_link",
        "device_url",
        "href",
        "url",
        "location",
        "redirect_target",
        "interface_ip",
        "ip_addr",
        "gateway_ip_addr",
        "default_route",
        "mac_addr",
        "site",
        "site_id",
        "battery_id",
        "battery_ids",
        "serial",
        "serials",
        "serial_number",
        "data_site_id",
        "data_battery_id",
    }
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.strip().lower().replace("-", "_") in sensitive:
                out[key_text] = "[redacted]"
            else:
                out[key_text] = redact_battery_payload(item)
        return out
    if isinstance(value, list):
        return [redact_battery_payload(item) for item in value]
    return value
