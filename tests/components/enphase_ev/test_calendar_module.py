from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.enphase_ev import DOMAIN
from custom_components.enphase_ev.calendar import (
    BackupHistoryCalendarEntity,
    async_setup_entry,
)


def test_site_has_battery_helper_defaults_and_strict() -> None:
    from custom_components.enphase_ev import calendar as calendar_mod

    coord = SimpleNamespace()
    assert calendar_mod._site_has_battery(coord) is True
    assert calendar_mod._site_has_battery(coord, strict=True) is False

    coord._battery_has_encharge = False
    assert calendar_mod._site_has_battery(coord) is False
    assert calendar_mod._site_has_battery(coord, strict=True) is False

    coord._battery_has_encharge = True
    assert calendar_mod._site_has_battery(coord) is True
    assert calendar_mod._site_has_battery(coord, strict=True) is True


def test_calendar_type_available_falls_back_to_has_type() -> None:
    from custom_components.enphase_ev import calendar as calendar_mod

    coord = SimpleNamespace(has_type=lambda type_key: type_key == "encharge")
    assert calendar_mod._type_available(coord, "encharge") is True
    assert calendar_mod._type_available(coord, "envoy") is False

    coord_no_helpers = SimpleNamespace()
    assert calendar_mod._type_available(coord_no_helpers, "encharge") is True


@pytest.mark.asyncio
async def test_async_setup_entry_adds_backup_history_calendar(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert len([ent for ent in added if isinstance(ent, BackupHistoryCalendarEntity)]) == 1


@pytest.mark.asyncio
async def test_async_setup_entry_does_not_duplicate_backup_history_calendar(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    callbacks: list = []

    def _capture_listener(callback):
        callbacks.append(callback)
        return lambda: None

    coord.async_add_listener = _capture_listener  # type: ignore[assignment]
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)
    assert len([ent for ent in added if isinstance(ent, BackupHistoryCalendarEntity)]) == 1
    assert callbacks

    callbacks[0]()
    assert len([ent for ent in added if isinstance(ent, BackupHistoryCalendarEntity)]) == 1


@pytest.mark.asyncio
async def test_async_setup_entry_waits_for_explicit_battery_detection(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = None  # noqa: SLF001
    callbacks: list = []

    def _capture_listener(callback):
        callbacks.append(callback)
        return lambda: None

    coord.async_add_listener = _capture_listener  # type: ignore[assignment]
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)
    assert not any(isinstance(ent, BackupHistoryCalendarEntity) for ent in added)
    assert callbacks

    coord._battery_has_encharge = True  # noqa: SLF001
    callbacks[0]()
    assert len([ent for ent in added if isinstance(ent, BackupHistoryCalendarEntity)]) == 1


@pytest.mark.asyncio
async def test_async_setup_entry_skips_backup_history_calendar_without_battery(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = False  # noqa: SLF001
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(ent, BackupHistoryCalendarEntity) for ent in added)


def test_backup_history_calendar_available_gating(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.last_update_success = True
    coord._battery_has_encharge = True  # noqa: SLF001
    entity = BackupHistoryCalendarEntity(coord)

    assert entity.available is True

    coord.last_update_success = False
    assert entity.available is False

    coord.last_update_success = True
    coord.has_type_for_entities = lambda _type_key: False
    assert entity.available is False

    coord.has_type_for_entities = lambda _type_key: True
    coord._battery_has_encharge = False  # noqa: SLF001
    assert entity.available is False


def test_backup_history_calendar_device_info_uses_encharge(coordinator_factory) -> None:
    coord = coordinator_factory()
    entity = BackupHistoryCalendarEntity(coord)

    info = entity.device_info
    assert (DOMAIN, f"type:{coord.site_id}:encharge") in info["identifiers"]


def test_backup_history_calendar_device_info_fallback(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.type_device_info = lambda _type_key: None
    entity = BackupHistoryCalendarEntity(coord)

    info = entity.device_info
    assert info["manufacturer"] == "Enphase"
    assert (DOMAIN, f"type:{coord.site_id}:encharge") in info["identifiers"]


def test_backup_history_calendar_iter_history_events_filters_invalid_rows(
    coordinator_factory,
    monkeypatch,
) -> None:
    coord = coordinator_factory()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        type(coord),
        "battery_backup_history_events",
        property(
            lambda self: [  # noqa: ARG005
                "bad-row",
                {"start": "bad", "end": now + timedelta(minutes=1), "duration_seconds": 60},
                {"start": datetime(2026, 2, 1, 10, 0), "end": now, "duration_seconds": 60},
                {
                    "start": now + timedelta(minutes=5),
                    "end": now + timedelta(minutes=4),
                    "duration_seconds": 60,
                },
                {
                    "start": now + timedelta(minutes=1),
                    "end": now + timedelta(minutes=2),
                    "duration_seconds": 60,
                },
            ]
        ),
    )
    entity = BackupHistoryCalendarEntity(coord)

    events = entity._iter_history_events()  # noqa: SLF001
    assert len(events) == 1
    assert events[0][0] == now + timedelta(minutes=1)
    assert events[0][1] == now + timedelta(minutes=2)


def test_backup_history_calendar_to_calendar_event_summary_prefers_name(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    entity = BackupHistoryCalendarEntity(coord)
    monkeypatch.setattr(
        BackupHistoryCalendarEntity,
        "name",
        property(lambda self: " Backup History "),
    )

    event = entity._to_calendar_event(  # noqa: SLF001
        datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 2, 1, 8, 1, tzinfo=timezone.utc),
    )
    assert event.summary == "Backup History"


def test_backup_history_calendar_to_calendar_event_summary_uses_entity_id(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    entity = BackupHistoryCalendarEntity(coord)
    monkeypatch.setattr(
        BackupHistoryCalendarEntity,
        "name",
        property(lambda self: "   "),
    )
    monkeypatch.setattr(
        BackupHistoryCalendarEntity,
        "entity_id",
        property(lambda self: " calendar.backup_history "),
        raising=False,
    )

    event = entity._to_calendar_event(  # noqa: SLF001
        datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 2, 1, 8, 1, tzinfo=timezone.utc),
    )
    assert event.summary == "calendar.backup_history"


def test_backup_history_calendar_event_current_next_none(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.last_update_success = True
    now = datetime.now(timezone.utc)
    coord._battery_backup_history_events = [  # noqa: SLF001
        {
            "start": now - timedelta(minutes=3),
            "end": now - timedelta(minutes=1),
            "duration_seconds": 120,
        },
        {
            "start": now - timedelta(minutes=1),
            "end": now + timedelta(minutes=1),
            "duration_seconds": 120,
        },
        {
            "start": now + timedelta(minutes=3),
            "end": now + timedelta(minutes=4),
            "duration_seconds": 60,
        },
    ]
    entity = BackupHistoryCalendarEntity(coord)

    current = entity.event
    assert current is not None
    assert current.start <= now <= current.end

    coord._battery_backup_history_events = [  # noqa: SLF001
        {
            "start": now + timedelta(minutes=2),
            "end": now + timedelta(minutes=4),
            "duration_seconds": 120,
        }
    ]
    upcoming = entity.event
    assert upcoming is not None
    assert upcoming.start > now

    coord._battery_backup_history_events = []  # noqa: SLF001
    assert entity.event is None


@pytest.mark.asyncio
async def test_backup_history_calendar_get_events_range_filter(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.last_update_success = True
    start = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    coord._battery_backup_history_events = [  # noqa: SLF001
        {
            "start": datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 2, 1, 8, 2, tzinfo=timezone.utc),
            "duration_seconds": 120,
        },
        {
            "start": datetime(2026, 2, 2, 8, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 2, 2, 8, 1, tzinfo=timezone.utc),
            "duration_seconds": 60,
        },
        {
            "start": datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 2, 10, 8, 1, tzinfo=timezone.utc),
            "duration_seconds": 60,
        },
    ]
    entity = BackupHistoryCalendarEntity(coord)

    events = await entity.async_get_events(
        None, start, datetime(2026, 2, 3, 0, 0, tzinfo=timezone.utc)
    )
    assert len(events) == 2
    assert all(event.start < datetime(2026, 2, 3, 0, 0, tzinfo=timezone.utc) for event in events)


@pytest.mark.asyncio
async def test_backup_history_calendar_get_events_accepts_naive_range(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.last_update_success = True
    coord._battery_backup_history_events = [  # noqa: SLF001
        {
            "start": datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 2, 1, 8, 2, tzinfo=timezone.utc),
            "duration_seconds": 120,
        }
    ]
    entity = BackupHistoryCalendarEntity(coord)

    events = await entity.async_get_events(
        None,
        datetime(2026, 2, 1, 0, 0),
        datetime(2026, 2, 2, 0, 0),
    )
    assert len(events) == 1
