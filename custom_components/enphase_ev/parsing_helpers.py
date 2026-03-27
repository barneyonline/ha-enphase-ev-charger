from __future__ import annotations

from datetime import datetime
from datetime import timezone as _tz


def coerce_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    if isinstance(value, str):
        try:
            cleaned = value.strip().replace(",", "")
        except Exception:
            return None
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except Exception:
            return None
    return None


def coerce_optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "y", "enabled", "enable", "on"):
            return True
        if normalized in ("false", "0", "no", "n", "disabled", "disable", "off"):
            return False
    return None


def coerce_optional_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def type_member_text(member: dict[str, object] | None, *keys: str) -> str | None:
    if not isinstance(member, dict):
        return None
    for key in keys:
        value = member.get(key)
        if value is None:
            continue
        try:
            text = str(value).strip()
        except Exception:
            continue
        if text:
            return text
    return None


def heatpump_member_device_type(member: dict[str, object] | None) -> str | None:
    if not isinstance(member, dict):
        return None
    raw = (
        member.get("device_type")
        if member.get("device_type") is not None
        else member.get("device-type")
    )
    if raw is None:
        return None
    try:
        text = str(raw).strip()
    except Exception:
        return None
    return text.upper() if text else None


def heatpump_status_text(member: dict[str, object] | None) -> str | None:
    if not isinstance(member, dict):
        return None
    status_text = (
        member.get("statusText")
        if member.get("statusText") is not None
        else member.get("status_text")
    )
    text = coerce_optional_text(status_text)
    if text:
        return text
    raw = coerce_optional_text(member.get("status"))
    if not raw:
        return None
    return raw.replace("_", " ").replace("-", " ").title()


def parse_inverter_last_report(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_tz.utc)
    epoch_value: float | None = None
    if isinstance(value, (int, float)):
        try:
            epoch_value = float(value)
        except Exception:
            return None
    else:
        try:
            text = str(value).strip()
        except Exception:
            return None
        if not text:
            return None
        if text.endswith("[UTC]"):
            text = text[:-5]
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt_value = datetime.fromisoformat(text)
            return dt_value if dt_value.tzinfo else dt_value.replace(tzinfo=_tz.utc)
        except Exception:
            try:
                epoch_value = float(text)
            except Exception:
                return None
    if epoch_value is None:
        return None
    if epoch_value > 1_000_000_000_000:
        epoch_value /= 1000.0
    try:
        return datetime.fromtimestamp(epoch_value, tz=_tz.utc)
    except Exception:
        return None
