from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_ON
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er

from custom_components.enphase_ev import DOMAIN
from custom_components.enphase_ev.coordinator import EnphaseCoordinator
from custom_components.enphase_ev.entity import EnphaseBaseEntity
from custom_components.enphase_ev.switch import (
    ChargingSwitch,
    GreenBatterySwitch,
    ScheduleSlotSwitch,
    async_setup_entry,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


@pytest.fixture
def coordinator_factory(hass, config_entry, monkeypatch):
    """Create a configured coordinator with controllable client behavior."""

    def _create(extra: dict | None = None) -> EnphaseCoordinator:
        monkeypatch.setattr(
            "custom_components.enphase_ev.coordinator.async_get_clientsession",
            lambda *args, **kwargs: object(),
        )
        coord = EnphaseCoordinator(hass, config_entry.data, config_entry=config_entry)
        coord._schedule_refresh = MagicMock()
        base = {
            RANDOM_SERIAL: {
                "name": "Garage EV",
                "display_name": "Garage EV",
                "charging": False,
                "plugged": True,
                "min_amp": 6,
                "max_amp": 32,
            }
        }
        if extra:
            base[RANDOM_SERIAL].update(extra)
        coord.data = base
        coord.last_set_amps = {}
        coord._ensure_serial_tracked(RANDOM_SERIAL)

        original_set_desired = coord.set_desired_charging
        coord.set_desired_charging = MagicMock(wraps=original_set_desired)
        original_set_last = coord.set_last_set_amps
        coord.set_last_set_amps = MagicMock(wraps=original_set_last)
        original_require = coord.require_plugged
        coord.require_plugged = MagicMock(wraps=original_require)

        coord.client = SimpleNamespace(
            start_charging=AsyncMock(return_value={"status": "ok"}),
            stop_charging=AsyncMock(return_value=None),
            set_green_battery_setting=AsyncMock(return_value={"status": "ok"}),
            start_live_stream=AsyncMock(
                return_value={"status": "accepted", "duration_s": 900}
            ),
        )
        coord.async_request_refresh = AsyncMock()
        coord.kick_fast = MagicMock()
        coord.set_charging_expectation = MagicMock()
        coord.pick_start_amps = MagicMock(return_value=32)
        return coord

    return _create


@pytest.mark.asyncio
async def test_async_setup_entry_syncs_chargers(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    listener_spy = MagicMock(wraps=coord.async_add_listener)
    monkeypatch.setattr(coord, "async_add_listener", listener_spy)

    await async_setup_entry(hass, config_entry, _capture)
    assert [ent._sn for ent in added] == [RANDOM_SERIAL]
    listener_spy.assert_called_once()
    listener = listener_spy.call_args[0][0]

    new_serial = "EV0002"
    coord.data[new_serial] = {
        "name": "Second Charger",
        "charging": False,
        "plugged": True,
    }
    coord._ensure_serial_tracked(new_serial)

    listener()
    assert [ent._sn for ent in added] == [RANDOM_SERIAL, new_serial]

    listener()
    assert [ent._sn for ent in added] == [RANDOM_SERIAL, new_serial]
    assert config_entry._on_unload and callable(config_entry._on_unload[0])


@pytest.mark.asyncio
async def test_async_setup_entry_skips_schedule_when_sync_missing(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.schedule_sync = None
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert all(isinstance(entity, ChargingSwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_green_battery_switch_when_supported(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": True}
    )
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    listener_spy = MagicMock(wraps=coord.async_add_listener)
    monkeypatch.setattr(coord, "async_add_listener", listener_spy)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, GreenBatterySwitch) for entity in added)
    listener = listener_spy.call_args[0][0]
    listener()


@pytest.mark.asyncio
async def test_async_setup_entry_skips_green_battery_switch_when_unsupported(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory({"green_battery_supported": False})
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, GreenBatterySwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_schedule_switches(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    slot_id = f"site:{RANDOM_SERIAL}:slot-1"
    helper_entity_id = "schedule.enphase_slot_1"
    coord.schedule_sync._mapping = {RANDOM_SERIAL: {slot_id: helper_entity_id}}
    coord.schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            slot_id: {
                "id": slot_id,
                "startTime": "08:00",
                "endTime": "09:00",
                "scheduleType": "CUSTOM",
                "enabled": False,
            }
        }
    }
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, ScheduleSlotSwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_skips_duplicate_schedule_switches(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    slot_id = f"site:{RANDOM_SERIAL}:slot-1"
    coord.schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            slot_id: {
                "id": slot_id,
                "startTime": "08:00",
                "endTime": "09:00",
                "scheduleType": "CUSTOM",
                "enabled": False,
            }
        }
    }
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added: list = []
    callback_holder = {}

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    def _capture_listener(callback):
        callback_holder["callback"] = callback
        return MagicMock()

    coord.schedule_sync.async_add_listener = MagicMock(side_effect=_capture_listener)

    await async_setup_entry(hass, config_entry, _capture)
    callback_holder["callback"]()

    schedule_switches = [
        entity for entity in added if isinstance(entity, ScheduleSlotSwitch)
    ]
    assert len(schedule_switches) == 1


@pytest.mark.asyncio
async def test_async_setup_entry_skips_read_only_slots(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    missing_time_id = f"site:{RANDOM_SERIAL}:slot-missing-time"
    coord.schedule_sync._mapping = {
        RANDOM_SERIAL: {
            missing_time_id: "schedule.missing_time",
        }
    }
    coord.schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            missing_time_id: {
                "id": missing_time_id,
                "startTime": None,
                "endTime": "09:00",
                "scheduleType": "CUSTOM",
                "enabled": True,
            },
        }
    }
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, ScheduleSlotSwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_off_peak_schedule_switch(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    off_peak_id = f"site:{RANDOM_SERIAL}:slot-off-peak"
    coord.schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            off_peak_id: {
                "id": off_peak_id,
                "startTime": None,
                "endTime": None,
                "scheduleType": "OFF_PEAK",
                "enabled": False,
            }
        }
    }
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, ScheduleSlotSwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_skips_off_peak_when_ineligible(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    off_peak_id = f"site:{RANDOM_SERIAL}:slot-off-peak"
    coord.schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            off_peak_id: {
                "id": off_peak_id,
                "startTime": None,
                "endTime": None,
                "scheduleType": "OFF_PEAK",
                "enabled": False,
            }
        }
    }
    coord.schedule_sync._config_cache = {
        RANDOM_SERIAL: {"isOffPeakEligible": False}
    }
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, ScheduleSlotSwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_added_to_hass_restores_last_on_state(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw.hass = hass
    sw.entity_id = "switch.enphase_ev_charging"
    sw.async_get_last_state = AsyncMock(return_value=State(sw.entity_id, STATE_ON))
    sw.async_write_ha_state = MagicMock()

    await sw.async_added_to_hass()

    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, True)
    coord.kick_fast.assert_called_once_with(60)
    coord.async_request_refresh.assert_awaited_once()
    sw.async_write_ha_state.assert_called_once()
    assert coord.get_desired_charging(RANDOM_SERIAL) is True


@pytest.mark.asyncio
async def test_async_added_to_hass_swallows_refresh_failure(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.async_request_refresh = AsyncMock(side_effect=RuntimeError("boom"))
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw.hass = hass
    sw.entity_id = "switch.enphase_ev_charging"
    sw.async_get_last_state = AsyncMock(return_value=State(sw.entity_id, STATE_ON))
    sw.async_write_ha_state = MagicMock()

    await sw.async_added_to_hass()

    coord.kick_fast.assert_called_once_with(60)
    coord.async_request_refresh.assert_awaited_once()
    sw.async_write_ha_state.assert_not_called()
    assert sw._restored_state is True


@pytest.mark.asyncio
async def test_async_added_to_hass_without_restore_sets_current_state(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory({"charging": True})
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw.hass = hass
    sw.async_get_last_state = AsyncMock(return_value=None)

    await sw.async_added_to_hass()

    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, True)
    assert sw._restored_state is True


def test_is_on_prefers_restored_state_when_unavailable(coordinator_factory) -> None:
    coord = coordinator_factory({"charging": True})
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw._restored_state = False
    sw._has_data = False

    assert sw.is_on is False
    sw._restored_state = True
    assert sw.is_on is True


@pytest.mark.asyncio
async def test_async_turn_on_not_ready_clears_desired(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.start_charging = AsyncMock(return_value={"status": "not_ready"})
    coord.set_charging_expectation.reset_mock()
    coord.kick_fast.reset_mock()
    coord.async_request_refresh.reset_mock()

    sw = ChargingSwitch(coord, RANDOM_SERIAL)

    await sw.async_turn_on()

    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 32, 1, include_level=None, strict_preference=False
    )
    coord.set_last_set_amps.assert_called_once_with(RANDOM_SERIAL, 32)
    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, False)
    coord.set_charging_expectation.assert_not_called()
    coord.kick_fast.assert_not_called()
    assert coord.async_request_refresh.await_count == 0


def test_handle_coordinator_update_clears_restored_state(coordinator_factory) -> None:
    coord = coordinator_factory()
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw._restored_state = True

    with patch.object(
        EnphaseBaseEntity, "_handle_coordinator_update", autospec=True
    ) as mock_super:
        sw._handle_coordinator_update()

    mock_super.assert_called_once_with(sw)
    assert sw._restored_state is None


def test_green_battery_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": None}
    )
    sw = GreenBatterySwitch(coord, RANDOM_SERIAL)
    assert sw.available is False

    coord.data[RANDOM_SERIAL]["green_battery_enabled"] = False
    sw_updated = GreenBatterySwitch(coord, RANDOM_SERIAL)
    assert sw_updated.available is True
    assert sw_updated.is_on is False


def test_green_battery_switch_unavailable_without_data(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": True}
    )
    sw = GreenBatterySwitch(coord, RANDOM_SERIAL)
    sw._has_data = False
    assert sw.available is False


def test_green_battery_switch_unavailable_when_unsupported(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": False, "green_battery_enabled": True}
    )
    sw = GreenBatterySwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


@pytest.mark.asyncio
async def test_green_battery_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": False}
    )
    coord._green_battery_cache.clear()
    sw = GreenBatterySwitch(coord, RANDOM_SERIAL)

    await sw.async_turn_on()
    coord.client.set_green_battery_setting.assert_awaited_once_with(
        RANDOM_SERIAL, enabled=True
    )
    assert coord._green_battery_cache[RANDOM_SERIAL][0] is True

    await sw.async_turn_off()
    assert coord.client.set_green_battery_setting.await_count == 2
    coord.client.set_green_battery_setting.assert_awaited_with(
        RANDOM_SERIAL, enabled=False
    )
    assert coord._green_battery_cache[RANDOM_SERIAL][0] is False
    assert coord.async_request_refresh.await_count == 2


@pytest.mark.asyncio
async def test_schedule_slot_switch_name_and_toggle(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    schedule_sync = coord.schedule_sync
    slot_id = f"site:{RANDOM_SERIAL}:slot-2"
    helper_entity_id = "schedule.enphase_slot_2"
    schedule_sync._mapping = {RANDOM_SERIAL: {slot_id: helper_entity_id}}
    schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            slot_id: {
                "id": slot_id,
                "startTime": "08:00",
                "endTime": "09:00",
                "scheduleType": "CUSTOM",
                "enabled": False,
            }
        }
    }
    schedule_sync.async_set_slot_enabled = AsyncMock()

    sw = ScheduleSlotSwitch(coord, schedule_sync, RANDOM_SERIAL, slot_id)
    sw.hass = hass
    hass.states.async_set(helper_entity_id, "off", {"friendly_name": "Garage Schedule"})

    assert sw.name == "Garage Schedule"
    assert sw.is_on is False

    await sw.async_turn_on()
    schedule_sync.async_set_slot_enabled.assert_awaited_once_with(
        RANDOM_SERIAL, slot_id, True
    )
    await sw.async_turn_off()
    assert schedule_sync.async_set_slot_enabled.await_args_list[1].args == (
        RANDOM_SERIAL,
        slot_id,
        False,
    )


@pytest.mark.asyncio
async def test_schedule_slot_switch_registers_listener(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    schedule_sync = coord.schedule_sync
    slot_id = f"site:{RANDOM_SERIAL}:slot-3"
    schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            slot_id: {
                "id": slot_id,
                "startTime": "08:00",
                "endTime": "09:00",
                "scheduleType": "CUSTOM",
                "enabled": True,
            }
        }
    }
    schedule_sync.async_add_listener = MagicMock()
    unsub = MagicMock()
    schedule_sync.async_add_listener.return_value = unsub

    sw = ScheduleSlotSwitch(coord, schedule_sync, RANDOM_SERIAL, slot_id)
    sw.hass = hass
    await sw.async_added_to_hass()
    schedule_sync.async_add_listener.assert_called_once()
    sw.async_write_ha_state = MagicMock()
    sw._handle_schedule_sync_update()
    sw.async_write_ha_state.assert_called_once()
    await sw.async_will_remove_from_hass()
    unsub.assert_called_once()


def test_schedule_slot_switch_name_uses_registry(hass, coordinator_factory) -> None:
    coord = coordinator_factory()
    schedule_sync = coord.schedule_sync
    slot_id = f"site:{RANDOM_SERIAL}:slot-4"
    helper_entity_id = "schedule.enphase_slot_4"
    schedule_sync._mapping = {RANDOM_SERIAL: {slot_id: helper_entity_id}}
    schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            slot_id: {
                "id": slot_id,
                "startTime": "08:00",
                "endTime": "09:00",
                "scheduleType": "CUSTOM",
                "enabled": True,
            }
        }
    }
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "schedule",
        "schedule",
        "helper-4",
        suggested_object_id="enphase_slot_4",
        original_name="Registry Schedule",
    )

    sw = ScheduleSlotSwitch(coord, schedule_sync, RANDOM_SERIAL, slot_id)
    sw.hass = hass
    assert sw.name == "Registry Schedule"


def test_schedule_slot_switch_is_on_without_slot(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    schedule_sync = SimpleNamespace(get_slot=lambda *_args: None)
    slot_id = f"site:{RANDOM_SERIAL}:slot-missing"
    sw = ScheduleSlotSwitch(coord, schedule_sync, RANDOM_SERIAL, slot_id)

    assert sw.is_on is False


def test_schedule_slot_switch_name_without_helper_entity(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    slot_id = f"site:{RANDOM_SERIAL}:slot-5"
    schedule_sync = SimpleNamespace(
        get_slot=lambda *_args: {
            "id": slot_id,
            "startTime": "08:00",
            "endTime": "09:00",
            "scheduleType": "CUSTOM",
            "enabled": True,
        },
        get_helper_entity_id=lambda *_args: None,
    )
    sw = ScheduleSlotSwitch(coord, schedule_sync, RANDOM_SERIAL, slot_id)
    sw.hass = hass

    assert sw.name == f"Schedule {slot_id}"


def test_schedule_slot_switch_unavailable_without_slot(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    schedule_sync = coord.schedule_sync
    slot_id = f"site:{RANDOM_SERIAL}:slot-missing"
    schedule_sync._mapping = {RANDOM_SERIAL: {slot_id: "schedule.missing"}}
    sw = ScheduleSlotSwitch(coord, schedule_sync, RANDOM_SERIAL, slot_id)
    sw.hass = hass
    assert sw.available is False


def test_schedule_slot_switch_name_fallback(hass, coordinator_factory) -> None:
    coord = coordinator_factory()
    schedule_sync = coord.schedule_sync
    slot_id = f"site:{RANDOM_SERIAL}:slot-5"
    schedule_sync._mapping = {RANDOM_SERIAL: {slot_id: "schedule.missing"}}
    schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            slot_id: {
                "id": slot_id,
                "startTime": "08:00",
                "endTime": "09:00",
                "scheduleType": "CUSTOM",
                "enabled": True,
            }
        }
    }
    sw = ScheduleSlotSwitch(coord, schedule_sync, RANDOM_SERIAL, slot_id)
    sw.hass = hass
    assert sw.name == f"Schedule {slot_id}"


def test_schedule_slot_switch_name_off_peak(hass, coordinator_factory) -> None:
    coord = coordinator_factory()
    schedule_sync = coord.schedule_sync
    slot_id = f"site:{RANDOM_SERIAL}:slot-off-peak-name"
    schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            slot_id: {
                "id": slot_id,
                "startTime": None,
                "endTime": None,
                "scheduleType": "OFF_PEAK",
                "enabled": False,
            }
        }
    }
    sw = ScheduleSlotSwitch(coord, schedule_sync, RANDOM_SERIAL, slot_id)
    sw.hass = hass
    assert sw.name == "Off Peak Schedule"
    assert sw.is_on is False


def test_schedule_slot_switch_name_without_hass(coordinator_factory) -> None:
    coord = coordinator_factory()
    schedule_sync = coord.schedule_sync
    slot_id = f"site:{RANDOM_SERIAL}:slot-6"
    schedule_sync._mapping = {RANDOM_SERIAL: {slot_id: "schedule.missing"}}
    schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            slot_id: {
                "id": slot_id,
                "startTime": "08:00",
                "endTime": "09:00",
                "scheduleType": "CUSTOM",
                "enabled": True,
            }
        }
    }
    sw = ScheduleSlotSwitch(coord, schedule_sync, RANDOM_SERIAL, slot_id)
    assert sw.name == f"Schedule {slot_id}"
