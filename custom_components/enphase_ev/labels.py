from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.translation import (
    async_get_cached_translations,
    async_get_translations,
)

from .const import DOMAIN

if TYPE_CHECKING:  # pragma: no cover
    from homeassistant.core import HomeAssistant

BATTERY_PROFILE_LABELS: dict[str, str] = {
    "self_consumption": "Self-Consumption",
    "cost_savings": "Savings",
    "ai_optimisation": "AI Optimisation",
    "backup_only": "Full Backup",
}

BATTERY_GRID_MODE_LABELS: dict[str, str] = {
    "importexport": "Import and Export",
    "importonly": "Import Only",
    "exportonly": "Export Only",
}

CHARGE_MODE_LABELS: dict[str, str] = {
    "manual_charging": "Manual",
    "scheduled_charging": "Scheduled",
    "green_charging": "Green",
    "smart_charging": "Smart",
}

STATUS_LABELS: dict[str, str] = {
    "online": "Online",
    "offline": "Offline",
    "degraded": "Degraded",
    "not_reporting": "Not Reporting",
    "inactive": "Inactive",
    "normal": "Normal",
    "warning": "Warning",
    "error": "Error",
    "unknown": "Unknown",
    "unpaired": "Unpaired",
    "pairing": "Pairing",
}

_SHARED_LABEL_KEY_PREFIX = f"component.{DOMAIN}.entity.sensor.shared_labels.state."


async def async_prime_label_translations(hass: HomeAssistant) -> None:
    """Load label translations into Home Assistant's cache."""

    language = getattr(getattr(hass, "config", None), "language", "en")
    await async_get_translations(hass, language, "entity", [DOMAIN])


def _coerce_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    return text or None


def _translation_value(hass: Any | None, key: str) -> str | None:
    if hass is None:
        return None
    language = getattr(getattr(hass, "config", None), "language", "en")
    path = f"{_SHARED_LABEL_KEY_PREFIX}{key}"
    value = async_get_cached_translations(hass, language, "entity", DOMAIN).get(path)
    if isinstance(value, str) and value.strip():
        return value
    value = async_get_cached_translations(hass, "en", "entity", DOMAIN).get(path)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _render_label(template: str, **placeholders: str) -> str:
    if not placeholders:
        return template
    try:
        return template.format(**placeholders)
    except Exception:  # noqa: BLE001
        return template


def _shared_label(
    key: str,
    fallback: str,
    *,
    hass: Any | None = None,
    **placeholders: str,
) -> str:
    value = _translation_value(hass, key)
    if value is None:
        value = fallback
    return _render_label(value, **placeholders)


def _normalize_state_key(value: object) -> str | None:
    text = _coerce_text(value)
    if text is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return normalized or None


def _normalize_compact_key(value: object) -> str | None:
    text = _coerce_text(value)
    if text is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "", text.lower())
    return normalized or None


def _display_raw_value(value: object) -> str | None:
    text = _coerce_text(value)
    if text is None:
        return None
    normalized = " ".join(text.replace("_", " ").replace("-", " ").split())
    return normalized or None


def _friendly_label_text(value: object) -> str | None:
    text = _display_raw_value(value)
    if text is None:
        return None
    if text.isupper():
        text = text.lower()
    if text.islower():
        return " ".join(word.capitalize() for word in text.split())
    return text


def battery_profile_label(profile: object, *, hass: Any | None = None) -> str | None:
    key = _normalize_state_key(profile)
    if key is None:
        return None
    if key in BATTERY_PROFILE_LABELS:
        return _shared_label(key, BATTERY_PROFILE_LABELS[key], hass=hass)
    return _friendly_label_text(profile)


def battery_grid_mode_label(mode: object, *, hass: Any | None = None) -> str | None:
    key = _normalize_compact_key(mode)
    if key is None:
        return None
    if key in BATTERY_GRID_MODE_LABELS:
        return _shared_label(key, BATTERY_GRID_MODE_LABELS[key], hass=hass)
    return _friendly_label_text(mode)


def charge_mode_label(mode: object, *, hass: Any | None = None) -> str | None:
    key = _normalize_state_key(mode)
    if key is None:
        return None
    aliases = {
        "manual": "manual_charging",
        "scheduled": "scheduled_charging",
        "green": "green_charging",
        "smart": "smart_charging",
    }
    key = aliases.get(key, key)
    if key in CHARGE_MODE_LABELS:
        return _shared_label(key, CHARGE_MODE_LABELS[key], hass=hass)
    return _friendly_label_text(mode)


def status_label(value: object, *, hass: Any | None = None) -> str | None:
    key = _normalize_state_key(value)
    if key is None:
        return None
    if key in STATUS_LABELS:
        return _shared_label(key, STATUS_LABELS[key], hass=hass)
    return None


def friendly_status_text(value: object) -> str | None:
    text = _display_raw_value(value)
    if text is None:
        return None
    if text.isupper():
        text = text.lower()
    if text.islower():
        return text.capitalize()
    return text
