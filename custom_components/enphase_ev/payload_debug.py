"""Shared debug helpers for payload shape logging (coordinator, inventory, EVSE)."""

from __future__ import annotations

import json
from typing import Any


def debug_sorted_keys(value: object) -> list[str]:
    """Return sorted string keys from a mapping."""

    if not isinstance(value, dict):
        return []
    keys: set[str] = set()
    for key in value:
        try:
            key_text = str(key).strip()
        except Exception:  # noqa: BLE001
            continue
        if key_text:
            keys.add(key_text)
    return sorted(keys)


def debug_field_keys(members: object) -> list[str]:
    """Return sorted field keys present across a list of mappings."""

    if not isinstance(members, list):
        return []
    keys: set[str] = set()
    for member in members:
        if not isinstance(member, dict):
            continue
        keys.update(debug_sorted_keys(member))
    return sorted(keys)


def debug_payload_shape(payload: object) -> dict[str, Any]:
    """Return a payload-shape summary suitable for debug logging."""

    if isinstance(payload, dict):
        shape: dict[str, Any] = {
            "kind": "dict",
            "keys": debug_sorted_keys(payload),
        }
        for key in ("result", "data", "devices", "items", "members"):
            nested = payload.get(key)
            if isinstance(nested, list):
                shape[f"{key}_length"] = len(nested)
                field_keys = debug_field_keys(nested)
                if field_keys:
                    shape[f"{key}_field_keys"] = field_keys
            elif isinstance(nested, dict):
                shape[f"{key}_keys"] = debug_sorted_keys(nested)
        return shape
    if isinstance(payload, list):
        return {
            "kind": "list",
            "length": len(payload),
            "field_keys": debug_field_keys(payload),
        }
    if payload is None:
        return {"kind": "none"}
    return {"kind": type(payload).__name__}


def debug_render_summary(summary: object) -> str:
    """Serialize a debug summary into stable compact JSON."""

    try:
        return json.dumps(summary, sort_keys=True, ensure_ascii=True)
    except Exception:  # noqa: BLE001
        return str(summary)
