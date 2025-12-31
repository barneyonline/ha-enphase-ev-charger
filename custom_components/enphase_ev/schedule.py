from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import logging
from typing import Any

from homeassistant.components.schedule.const import (
    CONF_ALL_DAYS,
    CONF_DATA,
    CONF_FROM,
    CONF_FRIDAY,
    CONF_MONDAY,
    CONF_SATURDAY,
    CONF_SUNDAY,
    CONF_THURSDAY,
    CONF_TUESDAY,
    CONF_TO,
    CONF_WEDNESDAY,
)
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

ENPHASE_DAY_TO_CONF = {
    1: CONF_MONDAY,
    2: CONF_TUESDAY,
    3: CONF_WEDNESDAY,
    4: CONF_THURSDAY,
    5: CONF_FRIDAY,
    6: CONF_SATURDAY,
    7: CONF_SUNDAY,
}

CONF_DAY_ORDER = [
    CONF_MONDAY,
    CONF_TUESDAY,
    CONF_WEDNESDAY,
    CONF_THURSDAY,
    CONF_FRIDAY,
    CONF_SATURDAY,
    CONF_SUNDAY,
]
CONF_TO_ENPHASE = {day: idx + 1 for idx, day in enumerate(CONF_DAY_ORDER)}
END_OF_DAY = time(23, 59, 59)
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


@dataclass(slots=True)
class HelperDefinition:
    schedule: dict[str, list[dict[str, Any]]]
    read_only: bool
    schedule_type: str | None
    enabled: bool


@dataclass(slots=True)
class _ScheduleBlock:
    day: int
    start: time
    end: time
    data: dict[str, Any]


def _normalize_time(value: Any) -> time | None:
    if isinstance(value, time):
        return _sanitize_time(value)
    if isinstance(value, str):
        try:
            parsed = time.fromisoformat(value)
        except ValueError:
            parts = value.split(":")
            if len(parts) < 2:
                return None
            try:
                hour = int(parts[0])
                minute = int(parts[1])
            except (TypeError, ValueError):
                return None
            return _sanitize_time(time(hour=hour, minute=minute))
        return _sanitize_time(parsed)
    return None


def _format_time(value: time) -> str:
    if _is_end_of_day(value):
        return "00:00"
    return value.strftime("%H:%M")


def _is_end_of_day(value: time) -> bool:
    return value == time.max or value == END_OF_DAY


def _sanitize_time(value: time) -> time:
    if value == time.max:
        return END_OF_DAY
    if value.microsecond:
        return value.replace(microsecond=0)
    return value


def _build_empty_schedule() -> dict[str, list[dict[str, Any]]]:
    return {day: [] for day in CONF_ALL_DAYS}


def _next_day_conf(day: str) -> str:
    try:
        idx = CONF_DAY_ORDER.index(day)
    except ValueError:
        return day
    return CONF_DAY_ORDER[(idx + 1) % 7]


def _build_block_data(
    slot_id: str, schedule_type: str | None, read_only: bool, slot: dict[str, Any]
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "enphase_slot_id": slot_id,
        "schedule_type": schedule_type,
        "read_only": read_only,
    }
    remind_flag = slot.get("remindFlag")
    remind_time = slot.get("remindTime")
    if remind_flag and remind_time is not None:
        try:
            data["reminder_minutes"] = int(remind_time)
        except (TypeError, ValueError):
            pass
    return data


def slot_to_helper(slot: dict[str, Any], tz) -> HelperDefinition:
    del tz
    slot_id = str(slot.get("id") or "")
    schedule_type = slot.get("scheduleType")
    if schedule_type is not None:
        schedule_type = str(schedule_type)
    enabled = bool(slot.get("enabled", True))
    start_time = _normalize_time(slot.get("startTime"))
    end_time = _normalize_time(slot.get("endTime"))
    read_only = bool(
        schedule_type == "OFF_PEAK" or start_time is None or end_time is None
    )

    schedule = _build_empty_schedule()
    if read_only or not slot_id or start_time is None or end_time is None:
        return HelperDefinition(
            schedule=schedule,
            read_only=read_only,
            schedule_type=schedule_type,
            enabled=enabled,
        )

    data = _build_block_data(slot_id, schedule_type, read_only, slot)
    days_raw = slot.get("days") or []
    mapped_days = [
        ENPHASE_DAY_TO_CONF.get(day) for day in days_raw if day in ENPHASE_DAY_TO_CONF
    ]

    if end_time <= start_time:
        for day in mapped_days:
            schedule[day].append(
                {CONF_FROM: start_time, CONF_TO: END_OF_DAY, CONF_DATA: dict(data)}
            )
            if end_time != time.min:
                schedule[_next_day_conf(day)].append(
                    {CONF_FROM: time.min, CONF_TO: end_time, CONF_DATA: dict(data)}
                )
    else:
        for day in mapped_days:
            schedule[day].append(
                {CONF_FROM: start_time, CONF_TO: end_time, CONF_DATA: dict(data)}
            )

    return HelperDefinition(
        schedule=schedule,
        read_only=read_only,
        schedule_type=schedule_type,
        enabled=enabled,
    )


def _collect_blocks(schedule_def: dict[str, Any]) -> list[_ScheduleBlock]:
    blocks: list[_ScheduleBlock] = []
    for conf_day, enphase_day in CONF_TO_ENPHASE.items():
        entries = schedule_def.get(conf_day) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            start = _normalize_time(entry.get(CONF_FROM))
            end = _normalize_time(entry.get(CONF_TO))
            if start is None or end is None:
                continue
            data = entry.get(CONF_DATA) or {}
            if not isinstance(data, dict):
                data = {}
            blocks.append(
                _ScheduleBlock(
                    day=enphase_day,
                    start=start,
                    end=end,
                    data=data,
                )
            )
    return blocks


def _extract_reminder_minutes(blocks: list[_ScheduleBlock]) -> int | None:
    for block in blocks:
        minutes = block.data.get("reminder_minutes")
        if minutes is None:
            continue
        try:
            minutes_int = int(minutes)
        except (TypeError, ValueError):
            continue
        if minutes_int > 0:
            return minutes_int
    return None


def _detect_overnight_pair(
    blocks: list[_ScheduleBlock],
) -> tuple[time, time, list[int]] | None:
    late_blocks = [block for block in blocks if _is_end_of_day(block.end)]
    early_blocks = [block for block in blocks if block.start == time.min]
    if not late_blocks or not early_blocks:
        return None
    if len(late_blocks) + len(early_blocks) != len(blocks):
        return None
    start_time = late_blocks[0].start
    if any(block.start != start_time for block in late_blocks):
        return None
    end_time = early_blocks[0].end
    if any(block.end != end_time for block in early_blocks):
        return None
    early_days = {block.day for block in early_blocks}
    late_days = sorted({block.day for block in late_blocks})
    for day in late_days:
        next_day = 1 if day == 7 else day + 1
        if next_day not in early_days:
            return None
    return start_time, end_time, late_days


def _compute_reminder_utc(start_time: time, minutes: int, tz) -> str:
    base_time = time.min if _is_end_of_day(start_time) else start_time
    local_dt = datetime.combine(dt_util.now(tz).date(), base_time, tzinfo=tz)
    reminder_dt = local_dt - timedelta(minutes=minutes)
    reminder_utc = reminder_dt.astimezone(dt_util.UTC)
    return reminder_utc.strftime("%H:%M")


def helper_to_slot(
    schedule_def: dict[str, Any], slot_cache: dict[str, Any], tz
) -> dict[str, Any] | None:
    blocks = _collect_blocks(schedule_def)
    if not blocks:
        return None
    blocks_sorted = sorted(blocks, key=lambda block: (block.day, block.start))
    reminder_minutes = _extract_reminder_minutes(blocks_sorted)
    overnight = _detect_overnight_pair(blocks_sorted)

    if overnight:
        start_time, end_time, days = overnight
    else:
        primary = blocks_sorted[0]
        start_time = primary.start
        end_time = primary.end
        days = sorted(
            {
                block.day
                for block in blocks_sorted
                if block.start == start_time and block.end == end_time
            }
        )
        if len(blocks_sorted) != len(days):
            _LOGGER.warning(
                "Schedule helper contains multiple time blocks; using first block only"
            )

    if not days:
        return None

    slot = dict(slot_cache or {})
    slot["startTime"] = _format_time(start_time)
    slot["endTime"] = _format_time(end_time)
    slot["days"] = days
    slot["enabled"] = bool(slot.get("enabled", True))

    slot.setdefault("scheduleType", "CUSTOM")
    if reminder_minutes is not None:
        slot["remindFlag"] = True
        slot["remindTime"] = reminder_minutes
        slot["reminderTimeUtc"] = _compute_reminder_utc(
            start_time, reminder_minutes, tz
        )
    else:
        existing_flag = slot.get("remindFlag")
        existing_time = slot.get("remindTime")
        reminder_minutes = None
        if existing_flag and existing_time is not None:
            try:
                reminder_minutes = int(existing_time)
            except (TypeError, ValueError):
                reminder_minutes = None
        if reminder_minutes and reminder_minutes > 0:
            slot["remindFlag"] = True
            slot["remindTime"] = reminder_minutes
            slot["reminderTimeUtc"] = _compute_reminder_utc(
                start_time, reminder_minutes, tz
            )
        else:
            slot.setdefault("remindFlag", False)
            slot.setdefault("remindTime", None)
            slot.setdefault("reminderTimeUtc", None)

    if not slot.get("chargeLevelType"):
        slot["chargeLevelType"] = "Weekly"
    if not slot.get("recurringKind"):
        slot["recurringKind"] = "Recurring"
    if not slot.get("sourceType"):
        slot["sourceType"] = "SYSTEM"

    return slot


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
