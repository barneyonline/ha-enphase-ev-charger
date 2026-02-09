from __future__ import annotations

import re
from typing import Any

from .const import DOMAIN

_TYPE_ALIAS_TOKEN_MAP: dict[str, str] = {
    "envoy": "envoy",
    "gateway": "envoy",
    "iqgateway": "envoy",
    "iqgateways": "envoy",
    "encharge": "encharge",
    "battery": "encharge",
    "batteries": "encharge",
    "enpower": "enpower",
    "systemcontroller": "enpower",
    "systemcontrollers": "enpower",
    "iqevse": "iqevse",
    "evse": "iqevse",
    "evcharger": "iqevse",
    "evchargers": "iqevse",
    "microinverter": "microinverter",
    "microinverters": "microinverter",
    "generator": "generator",
    "generators": "generator",
}

KNOWN_TYPE_LABELS: dict[str, str] = {
    "envoy": "Gateway",
    "encharge": "Battery",
    "enpower": "System Controller",
    "iqevse": "EV Chargers",
    "microinverter": "Microinverters",
    "generator": "Generator",
}

KNOWN_TYPE_ORDER: tuple[str, ...] = (
    "envoy",
    "encharge",
    "enpower",
    "iqevse",
    "microinverter",
    "generator",
)

_PREFERRED_MEMBER_KEYS: tuple[str, ...] = (
    "name",
    "serial_number",
    "sku_id",
    "channel_type",
    "model",
    "status",
    "statusText",
    "connected",
    "last_report",
    "sw_version",
    "envoy_sw_version",
    "warranty_end_date",
    "ip",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalize_type_key(raw_type: object) -> str | None:
    if raw_type is None:
        return None
    try:
        text = str(raw_type).strip().lower()
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None
    slug = _SLUG_RE.sub("_", text).strip("_")
    if not slug:
        return None
    alias_token = slug.replace("_", "")
    canonical = _TYPE_ALIAS_TOKEN_MAP.get(alias_token)
    if canonical:
        return canonical
    return slug


def type_display_label(type_key: object) -> str | None:
    normalized = normalize_type_key(type_key)
    if not normalized:
        return None
    if normalized in KNOWN_TYPE_LABELS:
        return KNOWN_TYPE_LABELS[normalized]
    words = [part for part in normalized.split("_") if part]
    if not words:
        return None
    return " ".join(word.capitalize() for word in words)


def type_identifier(site_id: object, type_key: object) -> tuple[str, str] | None:
    normalized = normalize_type_key(type_key)
    if not normalized:
        return None
    try:
        site_text = str(site_id).strip()
    except Exception:  # noqa: BLE001
        return None
    if not site_text:
        return None
    return DOMAIN, f"type:{site_text}:{normalized}"


def parse_type_identifier(identifier: object) -> tuple[str, str] | None:
    if identifier is None:
        return None
    try:
        ident_text = str(identifier).strip()
    except Exception:  # noqa: BLE001
        return None
    if not ident_text.startswith("type:"):
        return None
    parts = ident_text.split(":", 2)
    if len(parts) != 3:
        return None
    site_id = parts[1].strip()
    key = normalize_type_key(parts[2])
    if not site_id or not key:
        return None
    return site_id, key


def member_is_retired(member: object) -> bool:
    if not isinstance(member, dict):
        return False
    retired_flag = member.get("isRetired")
    if retired_flag is True:
        return True
    for key in ("status", "statusText", "status_text"):
        value = member.get(key)
        if value is None:
            continue
        try:
            normalized = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            continue
        if normalized == "retired":
            return True
    return False


def sanitize_member(member: object) -> dict[str, str | int | float | bool | None]:
    if not isinstance(member, dict):
        return {}
    out: dict[str, str | int | float | bool | None] = {}
    seen: set[str] = set()

    def _clean_scalar(value: Any) -> str | int | float | bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            return value.strip()
        return None

    for key in _PREFERRED_MEMBER_KEYS:
        if key not in member:
            continue
        value = _clean_scalar(member.get(key))
        if value is None and member.get(key) is not None:
            continue
        out[key] = value
        seen.add(key)

    extra_keys = sorted(str(key) for key in member.keys() if str(key) not in seen)
    for key in extra_keys:
        value = _clean_scalar(member.get(key))
        if value is None and member.get(key) is not None:
            continue
        out[key] = value
    return out
