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
        return value
    if isinstance(value, str):
        parts = value.split(":")
        if len(parts) < 2:
            return None
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            return time(hour=hour, minute=minute)
        except (TypeError, ValueError):
            return None
    return None


def _format_time(value: time) -> str:
    if value == time.max:
        return "00:00"
    return value.strftime("%H:%M")


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
    if (
        read_only
        or not enabled
        or not slot_id
        or start_time is None
        or end_time is None
    ):
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
                {CONF_FROM: start_time, CONF_TO: time.max, CONF_DATA: dict(data)}
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
    late_blocks = [block for block in blocks if block.end == time.max]
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
    base_time = start_time if start_time != time.max else time.min
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
    slot["enabled"] = True

    slot.setdefault("scheduleType", "CUSTOM")
    if reminder_minutes:
        slot["remindFlag"] = True
        slot["remindTime"] = reminder_minutes
        slot["reminderTimeUtc"] = _compute_reminder_utc(
            start_time, reminder_minutes, tz
        )
    else:
        slot["remindFlag"] = False
        slot["remindTime"] = None
        slot["reminderTimeUtc"] = None

    if not slot.get("chargeLevelType"):
        slot["chargeLevelType"] = "Weekly"
    if not slot.get("recurringKind"):
        slot["recurringKind"] = "Recurring"
    if not slot.get("sourceType"):
        slot["sourceType"] = "SYSTEM"

    return slot
