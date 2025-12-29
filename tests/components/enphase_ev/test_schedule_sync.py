from __future__ import annotations

import asyncio
from datetime import time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.schedule.const import (
    CONF_FROM,
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

from custom_components.enphase_ev.const import (
    DEFAULT_SCHEDULE_NAMING,
    DOMAIN,
    OPT_SCHEDULE_EXPOSE_OFF_PEAK,
    OPT_SCHEDULE_NAMING,
    OPT_SCHEDULE_SYNC_ENABLED,
    SCHEDULE_NAMING_TIME_WINDOW,
    SCHEDULE_NAMING_TYPE_TIME_WINDOW,
)
from custom_components.enphase_ev.schedule import slot_to_helper
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


async def _setup_sync(hass, entry, payload):
    entry.add_to_hass(hass)
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
    assert off_peak_entity is not None
    assert sync._mapping[RANDOM_SERIAL][slot_id] == custom_entity
    assert sync._mapping[RANDOM_SERIAL][off_peak_id] == off_peak_entity
    diag = sync.diagnostics()
    assert diag["enabled"] is True


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
async def test_schedule_sync_respects_off_peak_option(hass) -> None:
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
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_EXPOSE_OFF_PEAK: False},
    )
    sync, _client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{off_peak_id}"
    assert (
        ent_reg.async_get_entity_id(SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id)
        is None
    )


@pytest.mark.asyncio
async def test_schedule_sync_off_peak_helper_change_ignored(hass) -> None:
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
    sync, client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{off_peak_id}"
    entity_id = ent_reg.async_get_entity_id(
        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
    )
    assert entity_id is not None

    await sync.async_handle_helper_change(entity_id)
    client.patch_schedules.assert_not_awaited()


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
async def test_schedule_sync_patch_failure_reverts_helper(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-5"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, startTime="08:00", endTime="09:00")],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_NAMING: DEFAULT_SCHEDULE_NAMING},
    )
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
async def test_schedule_sync_naming_styles_and_linking(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-11"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_NAMING: SCHEDULE_NAMING_TIME_WINDOW},
    )
    sync, _client = await _setup_sync(hass, entry, payload)

    helper_def = slot_to_helper(_slot(slot_id), dt_util.UTC)
    name = sync._default_name(RANDOM_SERIAL, _slot(slot_id), helper_def, 1)
    assert "08:00-09:00" in name

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
async def test_schedule_sync_type_time_window_naming(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-11b"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_NAMING: SCHEDULE_NAMING_TYPE_TIME_WINDOW},
    )
    sync, _client = await _setup_sync(hass, entry, payload)

    helper_def = slot_to_helper(_slot(slot_id), dt_util.UTC)
    name = sync._default_name(RANDOM_SERIAL, _slot(slot_id), helper_def, 1)
    assert "Custom" in name
    assert "08:00-09:00" in name


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

    sync._meta_cache[RANDOM_SERIAL] = None
    await sync.async_handle_helper_change(entity_id)
    assert client.patch_schedules.await_count == 1
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
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
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
    assert sync._show_off_peak() is True
    assert sync._naming_style() == DEFAULT_SCHEDULE_NAMING


def test_schedule_sync_has_scheduler_bearer_edge_cases(hass) -> None:
    sync_no_client = ScheduleSync(hass, SimpleNamespace(), None)
    assert sync_no_client._has_scheduler_bearer() is False

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
