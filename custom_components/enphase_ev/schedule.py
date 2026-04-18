from __future__ import annotations

from datetime import time
from typing import Any

SLOT_PATCH_FIELDS = (
    "id",
    "startTime",
    "endTime",
    "chargingLevel",
    "chargingLevelAmp",
    "scheduleType",
    "days",
    "remindTime",
    "remindFlag",
    "enabled",
    "recurringKind",
    "chargeLevelType",
    "sourceType",
    "reminderTimeUtc",
    "serializedDays",
)


def _coerce_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return value


def _coerce_int(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    return value


def normalize_slot_payload(slot: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {
        key: slot.get(key) for key in SLOT_PATCH_FIELDS if key in slot
    }
    slot_id = sanitized.get("id")
    if slot_id is not None:
        sanitized["id"] = str(slot_id)

    schedule_type = sanitized.get("scheduleType")
    if schedule_type is None:
        schedule_type = "CUSTOM"
    sanitized["scheduleType"] = str(schedule_type)

    if "enabled" in sanitized:
        sanitized["enabled"] = _coerce_bool(sanitized["enabled"])
    if "remindFlag" in sanitized:
        sanitized["remindFlag"] = _coerce_bool(sanitized["remindFlag"])

    days = sanitized.get("days")
    if isinstance(days, list):
        normalized_days = []
        for day in days:
            try:
                day_int = int(day)
            except (TypeError, ValueError):
                continue
            if 1 <= day_int <= 7 and day_int not in normalized_days:
                normalized_days.append(day_int)
        sanitized["days"] = normalized_days
    elif schedule_type == "OFF_PEAK":
        sanitized["days"] = list(range(1, 8))

    for key in ("startTime", "endTime", "reminderTimeUtc"):
        value = sanitized.get(key)
        if isinstance(value, time):
            sanitized[key] = value.strftime("%H:%M")

    if "remindTime" in sanitized:
        sanitized["remindTime"] = _coerce_int(sanitized["remindTime"])
    if "chargingLevel" in sanitized:
        sanitized["chargingLevel"] = _coerce_int(sanitized["chargingLevel"])
    if "chargingLevelAmp" in sanitized:
        sanitized["chargingLevelAmp"] = _coerce_int(sanitized["chargingLevelAmp"])

    return sanitized
