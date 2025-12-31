from __future__ import annotations

import asyncio
from datetime import time, timedelta
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.schedule.const import (
    CONF_FROM,
    CONF_THURSDAY,
    CONF_MONDAY,
    CONF_TO,
    CONF_TUESDAY,
    CONF_WEDNESDAY,
)
from homeassistant.components.schedule.const import DOMAIN as SCHEDULE_DOMAIN
from homeassistant.components import websocket_api
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.enphase_ev.const import DOMAIN, OPT_SCHEDULE_SYNC_ENABLED
from custom_components.enphase_ev.schedule import slot_to_helper
from custom_components.enphase_ev import schedule_sync as schedule_sync_mod
from custom_components.enphase_ev.schedule_sync import ScheduleSync
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


class DummyCoordinator(SimpleNamespace):
    def __init__(self, hass, client, entry, data):
        super().__init__()
        self.hass = hass
        self.client = client
        self.config_entry = entry
        self.data = data
        self.serials = {RANDOM_SERIAL}
        self._listener = None

    def iter_serials(self):
        return [RANDOM_SERIAL]

    def async_add_listener(self, cb):
        self._listener = cb

        def _unsub():
            self._listener = None

        return _unsub


def _slot(slot_id: str, **overrides):
    base = {
        "id": slot_id,
        "startTime": "08:00",
        "endTime": "09:00",
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


def _reset_schedule_storage(path: Path) -> None:
    if path.exists():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


async def _setup_sync(hass, entry, payload):
    entry.add_to_hass(hass)
    options = dict(entry.options or {})
    if OPT_SCHEDULE_SYNC_ENABLED not in options:
        options[OPT_SCHEDULE_SYNC_ENABLED] = True
        hass.config_entries.async_update_entry(entry, options=options)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedules = AsyncMock()
    coord = DummyCoordinator(
        hass,
        client,
        entry,
        data={RANDOM_SERIAL: {"display_name": "Garage Charger"}},
    )
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)
    return sync, client


@pytest.fixture(autouse=True)
async def _cleanup_schedule_sync(hass):
    yield
    for sync in hass.data.pop("enphase_ev_schedule_syncs", []):
        await sync.async_stop()


@pytest.mark.asyncio
async def test_schedule_sync_async_start_listener_error_when_enabled(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedules = AsyncMock()

    class BrokenCoordinator(DummyCoordinator):
        def async_add_listener(self, _cb):
            raise RuntimeError("boom")

    coord = BrokenCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    assert sync._unsub_coordinator is None


def test_schedule_sync_notify_listeners_handles_exception(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)
    calls: list[str] = []

    def _bad() -> None:
        raise RuntimeError("boom")

    def _good() -> None:
        calls.append("ok")

    sync.async_add_listener(_bad)
    sync.async_add_listener(_good)

    sync._notify_listeners()

    assert calls == ["ok"]


@pytest.mark.asyncio
async def test_schedule_sync_disable_support_noop_when_already_done(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)
    sync._disabled_cleanup_done = True
    sync._remove_all_helpers = AsyncMock()

    await sync._disable_support()

    sync._remove_all_helpers.assert_not_awaited()

@pytest.mark.asyncio
async def test_schedule_sync_creates_helpers_and_mapping(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-1"
    off_peak_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-2"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [
            _slot(slot_id),
            _slot(
                off_peak_id,
                scheduleType="OFF_PEAK",
                startTime=None,
                endTime=None,
                enabled=False,
            ),
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_custom = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    unique_off_peak = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{off_peak_id}"
    custom_entity = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_custom
    )
    off_peak_entity = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_off_peak
    )

    assert custom_entity is not None
    assert sync._mapping[RANDOM_SERIAL][slot_id] == custom_entity
    assert off_peak_entity is None
    assert off_peak_id not in sync._mapping[RANDOM_SERIAL]
    diag = sync.diagnostics()
    assert diag["enabled"] is True


@pytest.mark.asyncio
async def test_schedule_sync_listener_notified_on_refresh(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-listener"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    calls: list[str] = []

    def _listener() -> None:
        calls.append("ping")

    unsub = sync.async_add_listener(_listener)
    await sync.async_refresh(reason="manual")
    assert calls
    unsub()
    count = len(calls)
    await sync.async_refresh(reason="manual")
    assert len(calls) == count


@pytest.mark.asyncio
async def test_schedule_sync_removes_helper_when_slot_removed(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-3"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    assert (
        ent_reg.async_get_entity_id(SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id)
        is not None
    )

    client.get_schedules = AsyncMock(
        return_value={
            "meta": {"serverTimeStamp": "2025-01-02T00:00:00.000+00:00"},
            "config": {},
            "slots": [],
        }
    )
    await sync.async_refresh(reason="test")
    assert (
        ent_reg.async_get_entity_id(SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id)
        is None
    )


@pytest.mark.asyncio
async def test_schedule_sync_hides_off_peak(hass) -> None:
    off_peak_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-4"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [
            _slot(
                off_peak_id,
                scheduleType="OFF_PEAK",
                startTime=None,
                endTime=None,
                enabled=False,
            )
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{off_peak_id}"
    assert (
        ent_reg.async_get_entity_id(SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id)
        is None
    )
    assert off_peak_id not in sync._mapping.get(RANDOM_SERIAL, {})


@pytest.mark.asyncio
async def test_schedule_sync_off_peak_helper_not_created(hass) -> None:
    off_peak_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-4b"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [
            _slot(
                off_peak_id,
                scheduleType="OFF_PEAK",
                startTime=None,
                endTime=None,
                enabled=True,
            )
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{off_peak_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is None
    assert off_peak_id not in sync._mapping.get(RANDOM_SERIAL, {})


@pytest.mark.asyncio
async def test_schedule_sync_helper_change_missing_cache_skips(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-4c"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    sync._slot_cache = {}
    await sync.async_handle_helper_change(entity_id)
    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_helper_change_missing_times_skips(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-4d"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, startTime=None, endTime=None)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    await sync.async_handle_helper_change(entity_id)
    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_helper_change_missing_schedule_def(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-4e"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    sync._get_schedule = AsyncMock(return_value=None)
    await sync.async_handle_helper_change(entity_id)
    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_helper_change_unknown_entity(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-4f"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    await sync.async_handle_helper_change("schedule.unknown")
    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_helper_change_unchanged_noop(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-4g"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, startTime="08:00", endTime="09:00")],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    client.patch_schedules = AsyncMock()
    await sync.async_handle_helper_change(entity_id)
    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_helper_change_auto_enables_slot(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-4h"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, startTime="08:00", endTime="09:00", enabled=False)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    if sync._unsub_state is not None:
        sync._unsub_state()
        sync._unsub_state = None

    item = sync._storage_collection.data[unique_id]
    payload_update = {"name": item["name"]}
    payload_update[CONF_MONDAY] = [{CONF_FROM: time(11, 0), CONF_TO: time(13, 0)}]
    await sync._storage_collection.async_update_item(unique_id, payload_update)
    await hass.async_block_till_done()

    client.patch_schedules = AsyncMock(return_value={"meta": {"serverTimeStamp": "ts"}})
    await sync.async_handle_helper_change(entity_id)

    slots = client.patch_schedules.await_args.kwargs["slots"]
    slot_payload = next(slot for slot in slots if slot["id"] == slot_id)
    assert slot_payload["enabled"] is True


@pytest.mark.asyncio
async def test_schedule_sync_patch_failure_reverts_helper(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-5"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, startTime="08:00", endTime="09:00")],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    if sync._unsub_state is not None:
        sync._unsub_state()
        sync._unsub_state = None

    item = sync._storage_collection.data[unique_id]
    payload_update = {"name": item["name"]}
    payload_update[CONF_MONDAY] = [{CONF_FROM: time(10, 0), CONF_TO: time(11, 0)}]
    await sync._storage_collection.async_update_item(unique_id, payload_update)
    await hass.async_block_till_done()

    client.patch_schedules = AsyncMock(side_effect=RuntimeError("fail"))
    await sync.async_handle_helper_change(entity_id)

    schedule_def = await sync._get_schedule(entity_id)
    monday = schedule_def[CONF_MONDAY][0]
    assert monday[CONF_FROM] == time(8, 0)
    assert monday[CONF_TO] == time(9, 0)


@pytest.mark.asyncio
async def test_schedule_sync_loop_guard_skips_patch(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-6"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    sync._suppress_updates.add(entity_id)
    await sync.async_handle_helper_change(entity_id)
    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_disabled_skips_refresh(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-7"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: False},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedules = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    assert (
        ent_reg.async_get_entity_id(SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id)
        is None
    )
    client.get_schedules.assert_not_awaited()
    await sync.async_handle_helper_change("schedule.fake")


@pytest.mark.asyncio
async def test_schedule_sync_disable_support_removes_entities(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-disable"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    schedule_unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    schedule_entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, schedule_unique_id
    )
    assert schedule_entity_id is not None

    switch_unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}:enabled"
    ent_reg.async_get_or_create(
        "switch",
        DOMAIN,
        switch_unique_id,
        suggested_object_id="enphase_schedule_enabled",
    )
    assert (
        ent_reg.async_get_entity_id("switch", DOMAIN, switch_unique_id) is not None
    )

    initial_calls = client.get_schedules.await_count
    hass.config_entries.async_update_entry(
        entry, options={OPT_SCHEDULE_SYNC_ENABLED: False}
    )
    await sync.async_refresh(reason="manual")

    assert (
        ent_reg.async_get_entity_id(SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, schedule_unique_id)
        is None
    )
    assert ent_reg.async_get_entity_id("switch", DOMAIN, switch_unique_id) is None
    assert sync._mapping == {}
    assert sync._storage_collection is not None
    assert not any(
        item_id.startswith(f"{DOMAIN}:")
        for item_id in sync._storage_collection.data
    )
    assert client.get_schedules.await_count == initial_calls


@pytest.mark.asyncio
async def test_schedule_sync_remove_helpers_uses_registry_fallback(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-fallback"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    ent_reg.async_get_or_create(
        SCHEDULE_DOMAIN,
        SCHEDULE_DOMAIN,
        unique_id,
        suggested_object_id="enphase_schedule_cleanup",
    )
    sync._mapping = {RANDOM_SERIAL: {slot_id: ""}}

    collection = sync._storage_collection
    collection.data[123] = {"name": "bad"}
    collection.data["other:slot"] = {"name": "bad"}

    await sync._remove_all_helpers()

    assert (
        ent_reg.async_get_entity_id(SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id) is None
    )
    assert 123 in collection.data
    assert "other:slot" in collection.data


@pytest.mark.asyncio
async def test_schedule_sync_missing_bearer_skips_refresh(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-8"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: None
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedules = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    client.get_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_missing_bearer_sets_status(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client.get_schedules = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)

    await sync.async_refresh(reason="manual")

    assert sync._last_status == "missing_bearer"
    client.get_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_bearer_exception_skips_refresh(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()

    def _raise():
        raise RuntimeError("boom")

    client._bearer = _raise
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedules = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    client.get_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_callbacks_trigger_refresh(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-9"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)
    sync.async_refresh = AsyncMock()

    sync._handle_interval()
    await hass.async_block_till_done()
    assert sync.async_refresh.await_count >= 1

    sync._last_sync = None
    await sync._refresh_if_stale()
    assert sync.async_refresh.await_count >= 2

    sync._last_sync = dt_util.utcnow() - timedelta(minutes=10)
    sync._handle_coordinator_update()
    await hass.async_block_till_done()
    assert sync.async_refresh.await_count >= 3


@pytest.mark.asyncio
async def test_schedule_sync_slot_for_entity_fallback(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-10"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    sync._mapping = {RANDOM_SERIAL: {slot_id: entity_id}}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ent_reg, "async_get", lambda _entity_id: None)
        result = await sync._slot_for_entity(entity_id)
    assert result == (RANDOM_SERIAL, slot_id)


@pytest.mark.asyncio
async def test_schedule_sync_links_entities(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-11"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL)},
        manufacturer="Enphase",
        name="Garage Charger",
    )

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None
    sync._link_entity(RANDOM_SERIAL, entity_id)
    assert ent_reg.async_get(entity_id).device_id == device.id


@pytest.mark.asyncio
async def test_schedule_sync_default_naming(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-11b"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    helper_def = slot_to_helper(_slot(slot_id), dt_util.UTC)
    name = sync._default_name(RANDOM_SERIAL, _slot(slot_id), helper_def, 1)
    assert name == "Enphase Garage Charger 08:00-09:00"


def test_schedule_sync_default_name_off_peak(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-off-peak-name"
    coord = SimpleNamespace(data={RANDOM_SERIAL: {"display_name": "Garage Charger"}})
    sync = ScheduleSync(hass, coord, None)
    slot = _slot(slot_id, scheduleType="OFF_PEAK", startTime=None, endTime=None)
    helper_def = slot_to_helper(slot, dt_util.UTC)

    name = sync._default_name(RANDOM_SERIAL, slot, helper_def, 1)

    assert name == "Enphase Garage Charger Off-Peak (read-only)"


def test_schedule_sync_parse_slot_id_invalid() -> None:
    assert ScheduleSync._parse_slot_id("bad") == (None, None)
    assert ScheduleSync._parse_slot_id(f"{DOMAIN}:serial:slot") == (None, None)


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_updates_cache(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-enable"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, enabled=True)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedules = AsyncMock(
        return_value={"meta": {"serverTimeStamp": "2025-02-02T00:00:00.000+00:00"}}
    )

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, False)

    call = client.patch_schedules.await_args
    assert call.args[0] == RANDOM_SERIAL
    assert call.kwargs["server_timestamp"] == "2025-01-01T00:00:00.000+00:00"
    slots = call.kwargs["slots"]
    assert slots[0]["enabled"] is False
    assert sync.get_slot(RANDOM_SERIAL, slot_id)["enabled"] is False
    assert sync.get_helper_entity_id(RANDOM_SERIAL, slot_id) is not None
    assert any(
        mapping[1] == slot_id for mapping in sync.iter_helper_mappings()
    )


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_disabled_noop(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-disabled"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: False},
    )
    client = SimpleNamespace()
    client.patch_schedules = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    sync._slot_cache = {RANDOM_SERIAL: {slot_id: _slot(slot_id)}}

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, False)

    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_allows_off_peak(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-off-peak"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [
            _slot(
                slot_id,
                scheduleType="OFF_PEAK",
                startTime=None,
                endTime=None,
                enabled=False,
            )
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedules = AsyncMock()

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, True)
    await sync.async_set_slot_enabled(RANDOM_SERIAL, "missing", True)

    assert client.patch_schedules.await_count == 1
    slots = client.patch_schedules.await_args.kwargs["slots"]
    assert slots[0]["enabled"] is True


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_off_peak_ineligible_skips_patch(
    hass,
) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-off-peak"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {"isOffPeakEligible": False},
        "slots": [
            _slot(
                slot_id,
                scheduleType="OFF_PEAK",
                startTime=None,
                endTime=None,
                enabled=False,
            )
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedules = AsyncMock()

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, True)

    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_off_peak_sanitizes_payload(
    hass,
) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-off-peak-sanitize"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {"isOffPeakEligible": True},
        "slots": [
            _slot(
                slot_id,
                scheduleType="OFF_PEAK",
                startTime="08:00",
                endTime="09:00",
                days=[],
                enabled=False,
            )
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedules = AsyncMock()

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, True)

    slots = client.patch_schedules.await_args.kwargs["slots"]
    slot = slots[0]
    assert slot["enabled"] is True
    assert slot["startTime"] == "08:00"
    assert slot["endTime"] == "09:00"
    assert slot["chargingLevel"] == 32
    assert slot["chargingLevelAmp"] == 32
    assert slot["chargeLevelType"] == "Weekly"
    assert slot["recurringKind"] == "Recurring"
    assert slot["days"] == [1, 2, 3, 4, 5, 6, 7]


@pytest.mark.asyncio
async def test_schedule_sync_patch_success_updates_cache(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-12"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, startTime="08:00", endTime="09:00")],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    if sync._unsub_state is not None:
        sync._unsub_state()
        sync._unsub_state = None

    item = sync._storage_collection.data[unique_id]
    payload_update = {"name": item["name"]}
    payload_update[CONF_MONDAY] = [{CONF_FROM: time(9, 0), CONF_TO: time(10, 0)}]
    await sync._storage_collection.async_update_item(unique_id, payload_update)
    await hass.async_block_till_done()

    client.patch_schedules = AsyncMock(
        return_value={"meta": {"serverTimeStamp": "2025-02-02T00:00:00.000+00:00"}}
    )
    sync._meta_cache[RANDOM_SERIAL] = None
    await sync.async_handle_helper_change(entity_id)
    assert client.patch_schedules.await_count == 1
    assert sync._meta_cache[RANDOM_SERIAL] == "2025-02-02T00:00:00.000+00:00"
    assert sync._slot_cache[RANDOM_SERIAL][slot_id]["startTime"] == "09:00"


@pytest.mark.asyncio
async def test_schedule_sync_suppress_release(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-13"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    sync._suppress_entity(entity_id)
    assert entity_id in sync._suppress_updates
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert entity_id not in sync._suppress_updates


@pytest.mark.asyncio
async def test_schedule_sync_get_schedule_missing_entity_returns_none(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)
    assert await sync._get_schedule("schedule.missing") is None


@pytest.mark.asyncio
async def test_schedule_sync_missing_storage_handler_returns_none(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    handlers = hass.data.get(websocket_api.DOMAIN, {})
    handlers.pop(f"{SCHEDULE_DOMAIN}/create", None)
    sync._storage_collection = None
    assert await sync._ensure_storage_collection() is None


def test_schedule_sync_iter_slots(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)
    sync._slot_cache = {"SN1": {"slot-1": {"id": "slot-1"}}}

    assert list(sync.iter_slots()) == [("SN1", "slot-1", {"id": "slot-1"})]


def test_schedule_sync_off_peak_eligible_defaults_true(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)
    sync._config_cache["SN1"] = "bad"

    assert sync.is_off_peak_eligible("SN1") is True


@pytest.mark.asyncio
async def test_schedule_sync_sanitizes_schedule_storage_times(hass) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    _reset_schedule_storage(storage_path)
    raw = {
        "version": 1,
        "key": "schedule",
        "data": {
            "items": [
                {
                    "id": "schedule-test",
                    CONF_THURSDAY: [
                        {
                            CONF_FROM: "18:00:00.123456",
                            CONF_TO: "23:59:59.999999",
                        }
                    ],
                }
            ]
        },
    }
    storage_path.write_text(json.dumps(raw), encoding="utf-8")

    sync = ScheduleSync(hass, SimpleNamespace(), None)
    assert await sync._sanitize_schedule_storage() is True

    updated = json.loads(storage_path.read_text(encoding="utf-8"))
    entry = updated["data"]["items"][0][CONF_THURSDAY][0]
    assert entry[CONF_FROM] == "18:00:00"
    assert entry[CONF_TO] == "23:59:59"


@pytest.mark.asyncio
async def test_schedule_sync_sanitize_storage_load_error(hass) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    _reset_schedule_storage(storage_path)
    storage_path.mkdir(parents=True, exist_ok=True)

    sync = ScheduleSync(hass, SimpleNamespace(), None)
    assert await sync._sanitize_schedule_storage() is False


@pytest.mark.asyncio
async def test_schedule_sync_sanitize_storage_data_not_dict(hass) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    _reset_schedule_storage(storage_path)
    storage_path.write_text(json.dumps({"data": "bad"}), encoding="utf-8")

    sync = ScheduleSync(hass, SimpleNamespace(), None)
    assert await sync._sanitize_schedule_storage() is False


@pytest.mark.asyncio
async def test_schedule_sync_sanitize_storage_items_not_list(hass) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    _reset_schedule_storage(storage_path)
    storage_path.write_text(json.dumps({"data": {"items": "bad"}}), encoding="utf-8")

    sync = ScheduleSync(hass, SimpleNamespace(), None)
    assert await sync._sanitize_schedule_storage() is False


@pytest.mark.asyncio
async def test_schedule_sync_sanitize_storage_skips_invalid_items(hass) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    _reset_schedule_storage(storage_path)
    raw = {
        "data": {
            "items": [
                "bad-item",
                {
                    CONF_THURSDAY: [
                        "bad-entry",
                        {CONF_FROM: "bad.123456", CONF_TO: "bad.123456"},
                    ]
                },
            ]
        }
    }
    storage_path.write_text(json.dumps(raw), encoding="utf-8")

    sync = ScheduleSync(hass, SimpleNamespace(), None)
    assert await sync._sanitize_schedule_storage() is False


@pytest.mark.asyncio
async def test_schedule_sync_sanitize_storage_save_error(hass, monkeypatch) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    _reset_schedule_storage(storage_path)
    raw = {
        "data": {
            "items": [
                {CONF_THURSDAY: [{CONF_FROM: "18:00:00.123456", CONF_TO: "19:00:00"}]}
            ]
        }
    }
    storage_path.write_text(json.dumps(raw), encoding="utf-8")

    def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(schedule_sync_mod.json, "dump", _raise)
    sync = ScheduleSync(hass, SimpleNamespace(), None)

    assert await sync._sanitize_schedule_storage() is False


@pytest.mark.asyncio
async def test_schedule_sync_async_stop_clears_handlers(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-14"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    await sync.async_stop()
    assert sync._unsub_interval is None
    assert sync._unsub_state is None
    assert sync._unsub_coordinator is None


@pytest.mark.asyncio
async def test_schedule_sync_refresh_skips_when_locked(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-15"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    async with sync._lock:
        await sync.async_refresh(reason="locked")
    assert client.get_schedules.await_count >= 1


@pytest.mark.asyncio
async def test_schedule_sync_handle_state_change_suppressed(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-16"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    sync._suppress_updates.add(entity_id)
    sync._handle_state_change(SimpleNamespace(data={"entity_id": entity_id}))
    await hass.async_block_till_done()
    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_patch_slot_missing_timestamp_warns(hass, caplog) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-17"
    payload = {
        "meta": None,
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    slot_patch = _slot(slot_id, startTime="07:00", endTime="08:00")
    with caplog.at_level("WARNING"):
        await sync._patch_slot(RANDOM_SERIAL, slot_id, slot_patch)

    client.patch_schedules.assert_not_awaited()
    assert "missing server timestamp" in caplog.text


@pytest.mark.asyncio
async def test_schedule_sync_patch_slot_missing_id_skips(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-18"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    await sync._patch_slot(RANDOM_SERIAL, "missing", _slot("missing"))
    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_handles_bad_response_and_slots(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(
        return_value={
            "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
            "config": {},
            "slots": ["bad", {"id": ""}],
        }
    )
    client.patch_schedules = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    assert sync._slot_cache.get(RANDOM_SERIAL) == {}
    client.get_schedules = AsyncMock(return_value="bad")
    await sync.async_refresh(reason="bad")
    assert sync._slot_cache.get(RANDOM_SERIAL) == {}


@pytest.mark.asyncio
async def test_schedule_sync_async_start_listener_error_sets_none(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: False},
    )
    entry.add_to_hass(hass)
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedules = AsyncMock()

    class BrokenCoordinator(DummyCoordinator):
        def async_add_listener(self, _cb):
            raise RuntimeError("boom")

    coord = BrokenCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    assert sync._unsub_coordinator is None


@pytest.mark.asyncio
async def test_schedule_sync_helper_change_empty_schedule_skips_patch(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-empty"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    sync._get_schedule = AsyncMock(return_value={CONF_MONDAY: []})
    await sync.async_handle_helper_change(entity_id)
    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_helper_change_off_peak_skips_patch(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-off-peak"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.patch_schedules = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    sync._slot_cache = {
        RANDOM_SERIAL: {
            slot_id: _slot(slot_id, scheduleType="OFF_PEAK"),
        }
    }
    sync._mapping = {RANDOM_SERIAL: {slot_id: "schedule.off_peak"}}

    await sync.async_handle_helper_change("schedule.off_peak")

    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_get_schedule_storage_missing_returns_none(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    coord = DummyCoordinator(hass, SimpleNamespace(), entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    sync._ensure_storage_collection = AsyncMock(return_value=None)

    assert await sync._get_schedule("schedule.missing") is None


@pytest.mark.asyncio
async def test_schedule_sync_get_schedule_fallback_mapping_uses_unique_id(
    hass,
) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-fallback"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ent_reg, "async_get", lambda _entity_id: None)
        schedule_def = await sync._get_schedule(entity_id)

    assert schedule_def is not None
    assert schedule_def[CONF_MONDAY]


@pytest.mark.asyncio
async def test_schedule_sync_get_schedule_item_not_dict(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-bad-item"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    sync._storage_collection.data[unique_id] = "bad"
    assert await sync._get_schedule(entity_id) is None


@pytest.mark.asyncio
async def test_schedule_sync_revert_helper_missing_slot_returns(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    coord = DummyCoordinator(hass, SimpleNamespace(), entry, data={})
    sync = ScheduleSync(hass, coord, entry)

    await sync._revert_helper(RANDOM_SERIAL, "missing")


@pytest.mark.asyncio
async def test_schedule_sync_sync_serial_exception_sets_error(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-error"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    client.get_schedules = AsyncMock(side_effect=RuntimeError("boom"))
    await sync._sync_serial(RANDOM_SERIAL)

    assert sync._last_error == "boom"


@pytest.mark.asyncio
async def test_schedule_sync_apply_remove_helper_missing_collection(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-no-collection"
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    coord = DummyCoordinator(hass, SimpleNamespace(), entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    sync._ensure_storage_collection = AsyncMock(return_value=None)

    helper_def = slot_to_helper(_slot(slot_id), dt_util.UTC)
    await sync._apply_helper(RANDOM_SERIAL, slot_id, helper_def, "name")
    await sync._remove_helper(RANDOM_SERIAL, slot_id)

    assert sync._mapping == {}


@pytest.mark.asyncio
async def test_schedule_sync_load_mapping_handles_invalid_entries(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    coord = DummyCoordinator(hass, SimpleNamespace(), entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    sync._store.async_load = AsyncMock(
        return_value={"ok": {"slot": "entity"}, "bad": "oops"}
    )

    await sync._load_mapping()

    assert sync._mapping == {"ok": {"slot": "entity"}}


def test_schedule_sync_defaults_without_config_entry(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)

    assert sync._sync_enabled() is True
    assert sync._show_off_peak() is False


def test_schedule_sync_has_scheduler_bearer_edge_cases(hass) -> None:
    sync_no_client = ScheduleSync(hass, SimpleNamespace(), None)
    assert sync_no_client._has_scheduler_bearer() is False

    control_headers = SimpleNamespace(_control_headers=lambda: {"Authorization": "ok"})
    sync_control_headers = ScheduleSync(
        hass, SimpleNamespace(client=control_headers), None
    )
    assert sync_control_headers._has_scheduler_bearer() is True

    async def _control_headers_async():
        return {"Authorization": "ok"}

    control_headers_async = SimpleNamespace(_control_headers=_control_headers_async)
    sync_control_headers_async = ScheduleSync(
        hass, SimpleNamespace(client=control_headers_async), None
    )
    assert sync_control_headers_async._has_scheduler_bearer() is False

    def _control_headers_raise():
        raise RuntimeError("boom")

    control_headers_raise = SimpleNamespace(_control_headers=_control_headers_raise)
    sync_control_headers_raise = ScheduleSync(
        hass, SimpleNamespace(client=control_headers_raise), None
    )
    assert sync_control_headers_raise._has_scheduler_bearer() is False

    bearer_attr_none = SimpleNamespace(_bearer=None)
    sync_bearer_attr_none = ScheduleSync(
        hass, SimpleNamespace(client=bearer_attr_none), None
    )
    assert sync_bearer_attr_none._has_scheduler_bearer() is False

    async def _bearer_async():
        return "token"

    bearer_async = SimpleNamespace(_bearer=_bearer_async)
    sync_bearer_async = ScheduleSync(hass, SimpleNamespace(client=bearer_async), None)
    assert sync_bearer_async._has_scheduler_bearer() is False

    bearer_none = SimpleNamespace(_bearer=lambda: None)
    sync_bearer_none = ScheduleSync(hass, SimpleNamespace(client=bearer_none), None)
    assert sync_bearer_none._has_scheduler_bearer() is False

    def _bearer_raise():
        raise RuntimeError("boom")

    bearer_raise = SimpleNamespace(_bearer=_bearer_raise)
    sync_bearer_raise = ScheduleSync(hass, SimpleNamespace(client=bearer_raise), None)
    assert sync_bearer_raise._has_scheduler_bearer() is False

    bearer_awaitable = SimpleNamespace(_bearer=lambda: asyncio.sleep(0))
    sync_bearer_awaitable = ScheduleSync(
        hass, SimpleNamespace(client=bearer_awaitable), None
    )
    assert sync_bearer_awaitable._has_scheduler_bearer() is False


def test_schedule_sync_charger_name_fallbacks(hass) -> None:
    coord = SimpleNamespace(data={RANDOM_SERIAL: {"name": "Basement Charger"}})
    sync = ScheduleSync(hass, coord, None)

    assert sync._charger_name(RANDOM_SERIAL) == "Basement Charger"
    assert sync._charger_name("missing") == "Charger missing"


def test_schedule_sync_normalize_schedule_item_handles_invalid_entries() -> None:
    item = {
        CONF_MONDAY: "bad",
        CONF_TUESDAY: ["bad"],
        CONF_WEDNESDAY: [{CONF_FROM: "bad", CONF_TO: "09:00"}],
    }
    normalized = ScheduleSync._normalize_schedule_item(item)

    assert normalized[CONF_TUESDAY] == []
    assert normalized[CONF_WEDNESDAY] == []
    assert ScheduleSync._coerce_time(123) is None
