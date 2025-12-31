from __future__ import annotations

from datetime import time
import logging

from homeassistant.components.schedule.const import (
    CONF_DATA,
    CONF_FROM,
    CONF_MONDAY,
    CONF_SUNDAY,
    CONF_THURSDAY,
    CONF_TUESDAY,
    CONF_TO,
)
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev import schedule as schedule_mod
from custom_components.enphase_ev.schedule import helper_to_slot, slot_to_helper


def _make_slot(**overrides):
    base = {
        "id": "site:sn:slot-1",
        "startTime": "23:00",
        "endTime": "06:00",
        "days": [1],
        "scheduleType": "CUSTOM",
        "enabled": True,
        "remindFlag": False,
        "remindTime": None,
        "chargingLevel": 32,
        "chargingLevelAmp": 32,
        "recurringKind": "Recurring",
        "chargeLevelType": "Weekly",
        "sourceType": "SYSTEM",
    }
    base.update(overrides)
    return base


def test_slot_to_helper_overnight_split() -> None:
    slot = _make_slot(remindFlag=True, remindTime=15)
    helper_def = slot_to_helper(slot, dt_util.UTC)
    schedule = helper_def.schedule

    monday = schedule[CONF_MONDAY][0]
    tuesday = schedule[CONF_TUESDAY][0]

    assert monday[CONF_FROM] == time(23, 0)
    assert monday[CONF_TO] == schedule_mod.END_OF_DAY
    assert monday[CONF_DATA]["enphase_slot_id"] == "site:sn:slot-1"
    assert monday[CONF_DATA]["reminder_minutes"] == 15
    assert tuesday[CONF_FROM] == time.min
    assert tuesday[CONF_TO] == time(6, 0)


def test_slot_to_helper_off_peak_read_only() -> None:
    slot = _make_slot(
        scheduleType="OFF_PEAK",
        startTime=None,
        endTime=None,
        enabled=True,
    )
    helper_def = slot_to_helper(slot, dt_util.UTC)
    assert helper_def.read_only is True
    assert all(not helper_def.schedule[day] for day in helper_def.schedule)


def test_slot_to_helper_disabled_includes_schedule_blocks() -> None:
    slot = _make_slot(enabled=False)
    helper_def = slot_to_helper(slot, dt_util.UTC)
    assert helper_def.enabled is False
    assert helper_def.schedule[CONF_MONDAY]
    assert helper_def.schedule[CONF_TUESDAY]


def test_slot_to_helper_simple_range() -> None:
    slot = _make_slot(startTime="08:00", endTime="09:00", days=[4])
    helper_def = slot_to_helper(slot, dt_util.UTC)
    schedule = helper_def.schedule

    thursday = schedule[CONF_THURSDAY][0]
    assert thursday[CONF_FROM] == time(8, 0)
    assert thursday[CONF_TO] == time(9, 0)


def test_slot_to_helper_invalid_time_marks_read_only() -> None:
    slot = _make_slot(startTime="aa:bb", endTime="09:00")
    helper_def = slot_to_helper(slot, dt_util.UTC)
    assert helper_def.read_only is True


def test_slot_to_helper_invalid_time_format_marks_read_only() -> None:
    slot = _make_slot(startTime="bad", endTime="09:00")
    helper_def = slot_to_helper(slot, dt_util.UTC)
    assert helper_def.read_only is True


def test_slot_to_helper_bad_reminder_time_ignored() -> None:
    slot = _make_slot(remindFlag=True, remindTime="bad")
    helper_def = slot_to_helper(slot, dt_util.UTC)
    monday = helper_def.schedule[CONF_MONDAY][0]
    assert "reminder_minutes" not in monday[CONF_DATA]


def test_slot_to_helper_non_string_time_marks_read_only() -> None:
    slot = _make_slot(startTime=123, endTime=456)
    helper_def = slot_to_helper(slot, dt_util.UTC)
    assert helper_def.read_only is True


def test_slot_to_helper_sanitizes_microseconds() -> None:
    slot = _make_slot(startTime="08:00:00.123456", endTime="09:00", days=[4])
    helper_def = slot_to_helper(slot, dt_util.UTC)
    thursday = helper_def.schedule[CONF_THURSDAY][0]
    assert thursday[CONF_FROM] == time(8, 0)
    assert thursday[CONF_FROM].microsecond == 0


def test_slot_to_helper_sanitizes_end_of_day_microseconds() -> None:
    slot = _make_slot(startTime="23:00", endTime="23:59:59.999999", days=[4])
    helper_def = slot_to_helper(slot, dt_util.UTC)
    thursday = helper_def.schedule[CONF_THURSDAY][0]
    assert thursday[CONF_TO] == schedule_mod.END_OF_DAY
    assert thursday[CONF_TO].microsecond == 0


def test_helper_to_slot_reminder_utc_cross_midnight() -> None:
    schedule_def = {
        CONF_MONDAY: [
            {
                CONF_FROM: time(0, 5),
                CONF_TO: time(1, 0),
                CONF_DATA: {"reminder_minutes": 10},
            }
        ],
        CONF_TUESDAY: [],
        CONF_THURSDAY: [],
        CONF_SUNDAY: [],
    }
    slot_cache = _make_slot(startTime="00:05", endTime="01:00")
    slot_patch = helper_to_slot(schedule_def, slot_cache, dt_util.UTC)

    assert slot_patch is not None
    assert slot_patch["startTime"] == "00:05"
    assert slot_patch["endTime"] == "01:00"
    assert slot_patch["remindFlag"] is True
    assert slot_patch["remindTime"] == 10
    assert slot_patch["reminderTimeUtc"] == "23:55"


def test_helper_to_slot_invalid_existing_reminder_preserved() -> None:
    schedule_def = {
        CONF_MONDAY: [
            {
                CONF_FROM: time(8, 0),
                CONF_TO: time(9, 0),
                CONF_DATA: {"reminder_minutes": "bad"},
            }
        ],
    }
    slot_cache = _make_slot(remindFlag=True, remindTime="bad")
    slot_patch = helper_to_slot(schedule_def, slot_cache, dt_util.UTC)

    assert slot_patch is not None
    assert slot_patch["remindFlag"] is True
    assert slot_patch["remindTime"] == "bad"
    assert slot_patch["reminderTimeUtc"] is None


def test_helper_to_slot_end_time_max_maps_to_midnight() -> None:
    schedule_def = {
        CONF_MONDAY: [{CONF_FROM: time(20, 0), CONF_TO: schedule_mod.END_OF_DAY}],
    }
    slot_cache = _make_slot(startTime="20:00", endTime="00:00")
    slot_patch = helper_to_slot(schedule_def, slot_cache, dt_util.UTC)

    assert slot_patch is not None
    assert slot_patch["endTime"] == "00:00"


def test_helper_to_slot_missing_reminder_preserves_existing() -> None:
    schedule_def = {
        CONF_MONDAY: [
            {
                CONF_FROM: time(8, 0),
                CONF_TO: time(9, 0),
                CONF_DATA: {"reminder_minutes": "bad"},
            }
        ],
    }
    slot_cache = _make_slot(remindFlag=True, remindTime=5)
    slot_patch = helper_to_slot(schedule_def, slot_cache, dt_util.UTC)

    assert slot_patch is not None
    assert slot_patch["remindFlag"] is True
    assert slot_patch["remindTime"] == 5
    assert slot_patch["reminderTimeUtc"] == "07:55"


def test_helper_to_slot_sets_defaults_when_missing_fields() -> None:
    schedule_def = {
        CONF_MONDAY: [{CONF_FROM: time(8, 0), CONF_TO: time(9, 0)}],
    }
    slot_patch = helper_to_slot(schedule_def, {"id": "slot-2"}, dt_util.UTC)

    assert slot_patch is not None
    assert slot_patch["enabled"] is True
    assert slot_patch["chargeLevelType"] == "Weekly"
    assert slot_patch["recurringKind"] == "Recurring"
    assert slot_patch["sourceType"] == "SYSTEM"


def test_helper_to_slot_preserves_disabled_slot() -> None:
    schedule_def = {
        CONF_MONDAY: [{CONF_FROM: time(8, 0), CONF_TO: time(9, 0)}],
    }
    slot_cache = _make_slot(enabled=False)
    slot_patch = helper_to_slot(schedule_def, slot_cache, dt_util.UTC)

    assert slot_patch is not None
    assert slot_patch["enabled"] is False


def test_helper_to_slot_multi_block_warns_and_uses_first(caplog) -> None:
    schedule_def = {
        CONF_MONDAY: [
            {CONF_FROM: time(8, 0), CONF_TO: time(9, 0)},
            {CONF_FROM: time(10, 0), CONF_TO: time(11, 0)},
        ],
        CONF_TUESDAY: [],
    }
    slot_cache = _make_slot(startTime="08:00", endTime="09:00")
    with caplog.at_level(logging.WARNING):
        slot_patch = helper_to_slot(schedule_def, slot_cache, dt_util.UTC)

    assert slot_patch is not None
    assert slot_patch["startTime"] == "08:00"
    assert slot_patch["endTime"] == "09:00"
    assert slot_patch["days"] == [1]
    assert "multiple time blocks" in caplog.text


def test_helper_to_slot_overnight_pair() -> None:
    schedule_def = {
        CONF_MONDAY: [{CONF_FROM: time(23, 0), CONF_TO: schedule_mod.END_OF_DAY}],
        CONF_TUESDAY: [{CONF_FROM: time.min, CONF_TO: time(6, 0)}],
    }
    slot_cache = _make_slot(startTime="23:00", endTime="06:00")
    slot_patch = helper_to_slot(schedule_def, slot_cache, dt_util.UTC)

    assert slot_patch is not None
    assert slot_patch["startTime"] == "23:00"
    assert slot_patch["endTime"] == "06:00"
    assert slot_patch["days"] == [1]


def test_helper_to_slot_empty_schedule_returns_none() -> None:
    slot_cache = _make_slot()
    assert helper_to_slot({}, slot_cache, dt_util.UTC) is None


def test_helper_to_slot_non_list_entries_return_none() -> None:
    slot_cache = _make_slot()
    schedule_def = {CONF_MONDAY: "invalid"}
    assert helper_to_slot(schedule_def, slot_cache, dt_util.UTC) is None


def test_helper_to_slot_skips_non_dict_entries() -> None:
    slot_cache = _make_slot()
    schedule_def = {CONF_MONDAY: ["bad"]}
    assert helper_to_slot(schedule_def, slot_cache, dt_util.UTC) is None


def test_helper_to_slot_skips_invalid_time_entries() -> None:
    slot_cache = _make_slot()
    schedule_def = {CONF_MONDAY: [{CONF_FROM: "bad", CONF_TO: time(9, 0)}]}
    assert helper_to_slot(schedule_def, slot_cache, dt_util.UTC) is None


def test_helper_to_slot_handles_non_dict_data() -> None:
    slot_cache = _make_slot()
    schedule_def = {
        CONF_MONDAY: [
            {
                CONF_FROM: time(8, 0),
                CONF_TO: time(9, 0),
                CONF_DATA: "bad",
            }
        ],
    }
    slot_patch = helper_to_slot(schedule_def, slot_cache, dt_util.UTC)
    assert slot_patch is not None
    assert slot_patch["startTime"] == "08:00"


def test_detect_overnight_pair_rejects_mismatched_block_count() -> None:
    blocks = [
        schedule_mod._ScheduleBlock(
            day=1, start=time(22, 0), end=schedule_mod.END_OF_DAY, data={}
        ),
        schedule_mod._ScheduleBlock(day=2, start=time.min, end=time(6, 0), data={}),
        schedule_mod._ScheduleBlock(day=3, start=time(9, 0), end=time(10, 0), data={}),
    ]
    assert schedule_mod._detect_overnight_pair(blocks) is None


def test_detect_overnight_pair_rejects_mismatched_late_starts() -> None:
    blocks = [
        schedule_mod._ScheduleBlock(
            day=1, start=time(22, 0), end=schedule_mod.END_OF_DAY, data={}
        ),
        schedule_mod._ScheduleBlock(
            day=2, start=time(23, 0), end=schedule_mod.END_OF_DAY, data={}
        ),
        schedule_mod._ScheduleBlock(day=2, start=time.min, end=time(6, 0), data={}),
        schedule_mod._ScheduleBlock(day=3, start=time.min, end=time(6, 0), data={}),
    ]
    assert schedule_mod._detect_overnight_pair(blocks) is None


def test_detect_overnight_pair_rejects_mismatched_early_ends() -> None:
    blocks = [
        schedule_mod._ScheduleBlock(
            day=1, start=time(22, 0), end=schedule_mod.END_OF_DAY, data={}
        ),
        schedule_mod._ScheduleBlock(day=2, start=time.min, end=time(6, 0), data={}),
        schedule_mod._ScheduleBlock(
            day=2, start=time(22, 0), end=schedule_mod.END_OF_DAY, data={}
        ),
        schedule_mod._ScheduleBlock(day=3, start=time.min, end=time(7, 0), data={}),
    ]
    assert schedule_mod._detect_overnight_pair(blocks) is None


def test_detect_overnight_pair_rejects_missing_next_day() -> None:
    blocks = [
        schedule_mod._ScheduleBlock(
            day=1, start=time(22, 0), end=schedule_mod.END_OF_DAY, data={}
        ),
        schedule_mod._ScheduleBlock(day=3, start=time.min, end=time(6, 0), data={}),
    ]
    assert schedule_mod._detect_overnight_pair(blocks) is None


def test_helper_to_slot_empty_days_returns_none(monkeypatch) -> None:
    schedule_def = {CONF_MONDAY: [{CONF_FROM: time(8, 0), CONF_TO: time(9, 0)}]}
    slot_cache = _make_slot(startTime="08:00", endTime="09:00")

    monkeypatch.setattr(
        schedule_mod,
        "_detect_overnight_pair",
        lambda _blocks: (time(8, 0), time(9, 0), []),
    )

    assert helper_to_slot(schedule_def, slot_cache, dt_util.UTC) is None


def test_schedule_helpers_internal_helpers() -> None:
    assert schedule_mod._next_day_conf("invalid") == "invalid"
    assert (
        schedule_mod._compute_reminder_utc(schedule_mod.END_OF_DAY, 5, dt_util.UTC)
        == "23:55"
    )


def test_normalize_time_fallback_parses_non_iso() -> None:
    assert schedule_mod._normalize_time("8:0") == time(8, 0)
