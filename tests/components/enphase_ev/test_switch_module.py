from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_ON
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.enphase_ev.api import AuthSettingsUnavailable
from custom_components.enphase_ev.coordinator import (
    FAST_TOGGLE_POLL_HOLD_S,
    EnphaseCoordinator,
    ServiceValidationError,
)
from custom_components.enphase_ev.entity import EnphaseBaseEntity
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.switch import (
    _migrated_switch_entity_id,
    AppAuthenticationSwitch,
    ChargeFromGridScheduleSwitch,
    ChargeFromGridSwitch,
    ChargingSwitch,
    GreenBatterySwitch,
    SavingsUseBatteryAfterPeakSwitch,
    ScheduleSlotSwitch,
    StormGuardEvseSwitch,
    StormGuardSwitch,
    async_setup_entry,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def test_migrated_switch_entity_id_handles_canonical_and_suffixes() -> None:
    assert (
        _migrated_switch_entity_id(
            "switch.charge_from_grid_schedule", "switch.charge_from_grid_schedule"
        )
        is None
    )
    assert (
        _migrated_switch_entity_id(
            "switch.charge_from_grid_schedule_2", "switch.charge_from_grid_schedule"
        )
        is None
    )
    assert (
        _migrated_switch_entity_id(
            "switch.custom_schedule_2", "switch.charge_from_grid_schedule"
        )
        == "switch.charge_from_grid_schedule_2"
    )


@pytest.fixture
def coordinator_factory(hass, config_entry, monkeypatch):
    """Create a configured coordinator with controllable client behavior."""

    def _create(extra: dict | None = None) -> EnphaseCoordinator:
        monkeypatch.setattr(
            "custom_components.enphase_ev.coordinator.async_get_clientsession",
            lambda *args, **kwargs: object(),
        )
        coord = EnphaseCoordinator(hass, config_entry.data, config_entry=config_entry)
        coord._set_type_device_buckets(  # noqa: SLF001
            {
                "envoy": {
                    "type_key": "envoy",
                    "type_label": "Gateway",
                    "count": 1,
                    "devices": [{"name": "IQ Gateway"}],
                },
                "encharge": {
                    "type_key": "encharge",
                    "type_label": "Battery",
                    "count": 1,
                    "devices": [{"name": "IQ Battery"}],
                },
                "iqevse": {
                    "type_key": "iqevse",
                    "type_label": "EV Chargers",
                    "count": 1,
                    "devices": [
                        {"serial_number": RANDOM_SERIAL, "name": "Garage EV"}
                    ],
                },
            },
            ["envoy", "encharge", "iqevse"],
        )
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
            set_app_authentication=AsyncMock(return_value={"status": "ok"}),
            set_storm_guard=AsyncMock(return_value={"status": "ok"}),
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
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    listener_spy = MagicMock(wraps=coord.async_add_listener)
    monkeypatch.setattr(coord, "async_add_listener", listener_spy)

    await async_setup_entry(hass, config_entry, _capture)
    charger_entities = [ent for ent in added if hasattr(ent, "_sn")]
    assert {ent._sn for ent in charger_entities} == {RANDOM_SERIAL}
    assert any(isinstance(entity, ChargingSwitch) for entity in charger_entities)
    assert any(isinstance(entity, StormGuardEvseSwitch) for entity in charger_entities)
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
    charger_entities = [ent for ent in added if hasattr(ent, "_sn")]
    assert {ent._sn for ent in charger_entities} == {RANDOM_SERIAL, new_serial}

    listener()
    charger_entities = [ent for ent in added if hasattr(ent, "_sn")]
    assert {ent._sn for ent in charger_entities} == {RANDOM_SERIAL, new_serial}
    assert config_entry._on_unload and callable(config_entry._on_unload[0])


@pytest.mark.asyncio
async def test_async_setup_entry_migrates_charge_from_grid_schedule_switch_entity_id(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_schedule"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id=unique_id,
            entity_id="switch.custom_charge_from_grid_schedule",
        )
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_called_once_with(
        "switch.custom_charge_from_grid_schedule",
        new_entity_id="switch.charge_from_grid_schedule",
    )


@pytest.mark.asyncio
async def test_async_setup_entry_migrates_charge_from_grid_schedule_switch_suffix(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_schedule"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id=unique_id,
            entity_id="switch.custom_charge_from_grid_schedule_3",
        )
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_called_once_with(
        "switch.custom_charge_from_grid_schedule_3",
        new_entity_id="switch.charge_from_grid_schedule_3",
    )


@pytest.mark.asyncio
async def test_async_setup_entry_switch_migration_skips_canonical_and_unrelated(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_schedule"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id=unique_id,
            entity_id="switch.charge_from_grid_schedule",
        ),
        SimpleNamespace(
            unique_id="enphase_ev_site_other_unrelated_switch",
            entity_id="switch.unrelated",
        ),
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_switch_migration_handles_rename_conflict(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_schedule"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock(side_effect=ValueError("duplicate"))
    entries = [
        SimpleNamespace(
            unique_id=unique_id,
            entity_id="switch.custom_charge_from_grid_schedule",
        )
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_called_once_with(
        "switch.custom_charge_from_grid_schedule",
        new_entity_id="switch.charge_from_grid_schedule",
    )


@pytest.mark.asyncio
async def test_async_setup_entry_skips_schedule_when_sync_missing(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.schedule_sync = None
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, ScheduleSlotSwitch) for entity in added)
    assert any(isinstance(entity, ChargingSwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_skips_battery_site_switches_without_battery(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, ChargingSwitch) for entity in added)
    assert not any(isinstance(entity, StormGuardSwitch) for entity in added)
    assert not any(isinstance(entity, SavingsUseBatteryAfterPeakSwitch) for entity in added)
    assert not any(isinstance(entity, ChargeFromGridSwitch) for entity in added)
    assert not any(isinstance(entity, ChargeFromGridScheduleSwitch) for entity in added)
    assert not any(isinstance(entity, StormGuardEvseSwitch) for entity in added)
    assert not any(isinstance(entity, GreenBatterySwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_green_battery_switch_when_supported(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": True}
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

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
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, GreenBatterySwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_app_auth_switch_when_supported(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        {"app_auth_supported": True, "app_auth_enabled": True}
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    listener_spy = MagicMock(wraps=coord.async_add_listener)
    monkeypatch.setattr(coord, "async_add_listener", listener_spy)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, AppAuthenticationSwitch) for entity in added)
    listener = listener_spy.call_args[0][0]
    listener()


@pytest.mark.asyncio
async def test_async_setup_entry_skips_app_auth_switch_when_unsupported(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory({"app_auth_supported": False})
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, AppAuthenticationSwitch) for entity in added)


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
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

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
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

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
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

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
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

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
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

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
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.client.start_charging = AsyncMock(return_value={"status": "not_ready"})
    coord.set_charging_expectation.reset_mock()
    coord.kick_fast.reset_mock()
    coord.async_request_refresh.reset_mock()

    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw.hass = hass

    await sw.async_turn_on()

    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 32, 1, include_level=None, strict_preference=False
    )
    coord.set_last_set_amps.assert_called_once_with(RANDOM_SERIAL, 32)
    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, False)
    coord.set_charging_expectation.assert_not_called()
    coord.kick_fast.assert_called_once_with(FAST_TOGGLE_POLL_HOLD_S)
    assert coord.async_request_refresh.called
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_async_turn_on_plugged_validation_triggers_fast_refresh(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory({"plugged": False})
    coord.kick_fast.reset_mock()
    coord.async_request_refresh.reset_mock()
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw.hass = hass
    sw.entity_id = "switch.enphase_ev_charging"
    sw.async_write_ha_state = MagicMock()

    with pytest.raises(ServiceValidationError):
        await sw.async_turn_on()

    coord.kick_fast.assert_called_once_with(FAST_TOGGLE_POLL_HOLD_S)
    assert coord.async_request_refresh.called
    sw.async_write_ha_state.assert_called_once()
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_async_turn_on_validation_without_hass_skips_refresh(
    coordinator_factory,
) -> None:
    coord = coordinator_factory({"plugged": False})
    coord.kick_fast.reset_mock()
    coord.async_request_refresh.reset_mock()
    sw = ChargingSwitch(coord, RANDOM_SERIAL)

    with pytest.raises(ServiceValidationError):
        await sw.async_turn_on()

    coord.kick_fast.assert_called_once_with(FAST_TOGGLE_POLL_HOLD_S)
    assert coord.async_request_refresh.called is False


@pytest.mark.asyncio
async def test_async_turn_off_triggers_stop(coordinator_factory) -> None:
    coord = coordinator_factory()
    sw = ChargingSwitch(coord, RANDOM_SERIAL)

    await sw.async_turn_off()

    coord.client.stop_charging.assert_awaited_once_with(RANDOM_SERIAL)


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


def test_green_battery_switch_unavailable_when_scheduler_down(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": True}
    )
    coord._scheduler_available = False  # noqa: SLF001
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


def test_app_auth_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"app_auth_supported": True, "app_auth_enabled": None}
    )
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False

    coord.data[RANDOM_SERIAL]["app_auth_enabled"] = False
    sw_updated = AppAuthenticationSwitch(coord, RANDOM_SERIAL)
    assert sw_updated.available is True
    assert sw_updated.is_on is False


def test_app_auth_switch_unavailable_without_data(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"app_auth_supported": True, "app_auth_enabled": True}
    )
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)
    sw._has_data = False
    assert sw.available is False


def test_app_auth_switch_unavailable_when_unsupported(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"app_auth_supported": False, "app_auth_enabled": True}
    )
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


def test_app_auth_switch_unavailable_when_service_down(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"app_auth_supported": True, "app_auth_enabled": True}
    )
    coord._auth_settings_available = False  # noqa: SLF001
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


@pytest.mark.asyncio
async def test_app_auth_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"app_auth_supported": True, "app_auth_enabled": False}
    )
    coord._auth_settings_cache.clear()
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)

    await sw.async_turn_on()
    coord.client.set_app_authentication.assert_awaited_once_with(
        RANDOM_SERIAL, enabled=True
    )
    assert coord._auth_settings_cache[RANDOM_SERIAL][0] is True

    await sw.async_turn_off()
    assert coord.client.set_app_authentication.await_count == 2
    coord.client.set_app_authentication.assert_awaited_with(
        RANDOM_SERIAL, enabled=False
    )
    assert coord._auth_settings_cache[RANDOM_SERIAL][0] is False
    assert coord.async_request_refresh.await_count == 2


@pytest.mark.asyncio
async def test_app_auth_switch_handles_auth_settings_unavailable(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory(
        {"app_auth_supported": True, "app_auth_enabled": False}
    )
    coord.client.set_app_authentication = AsyncMock(
        side_effect=AuthSettingsUnavailable("down")
    )
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)

    with pytest.raises(
        HomeAssistantError, match="Authentication settings are unavailable"
    ):
        await sw.async_turn_on()

    assert any(
        issue[1] == "auth_settings_unavailable"
        for issue in mock_issue_registry.created
    )


@pytest.mark.asyncio
async def test_app_auth_switch_turn_off_handles_auth_settings_unavailable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(
        {"app_auth_supported": True, "app_auth_enabled": True}
    )
    coord.client.set_app_authentication = AsyncMock(
        side_effect=AuthSettingsUnavailable("down")
    )
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)

    with pytest.raises(
        HomeAssistantError, match="Authentication settings are unavailable"
    ):
        await sw.async_turn_off()


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


def test_storm_guard_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._storm_guard_state = None  # noqa: SLF001
    coord._storm_evse_enabled = None  # noqa: SLF001
    sw = StormGuardSwitch(coord)
    assert sw.available is False

    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._storm_evse_enabled = True  # noqa: SLF001
    assert sw.available is True
    assert sw.is_on is True


def test_storm_guard_switch_hidden_by_site_settings(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_show_storm_guard = False  # noqa: SLF001
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._storm_evse_enabled = True  # noqa: SLF001
    sw = StormGuardSwitch(coord)
    assert sw.available is False


def test_storm_guard_switch_unavailable_without_coordinator(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.last_update_success = False
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._storm_evse_enabled = True  # noqa: SLF001
    sw = StormGuardSwitch(coord)
    assert sw.available is False


def test_savings_use_battery_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    sw = SavingsUseBatteryAfterPeakSwitch(coord)

    assert sw.available is True
    assert sw.is_on is False

    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    assert sw.is_on is True

    coord._battery_profile = "self-consumption"  # noqa: SLF001
    assert sw.available is False


def test_savings_use_battery_switch_unavailable_without_coordinator(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.last_update_success = False
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    sw = SavingsUseBatteryAfterPeakSwitch(coord)
    assert sw.available is False


def test_charge_from_grid_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    sw = ChargeFromGridSwitch(coord)
    assert sw.available is True
    assert sw.is_on is False

    coord._battery_hide_charge_from_grid = True  # noqa: SLF001
    assert sw.available is False


@pytest.mark.asyncio
async def test_charge_from_grid_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord.async_set_charge_from_grid = AsyncMock()
    sw = ChargeFromGridSwitch(coord)

    await sw.async_turn_on()
    coord.async_set_charge_from_grid.assert_awaited_with(True)

    await sw.async_turn_off()
    coord.async_set_charge_from_grid.assert_awaited_with(False)


def test_charge_from_grid_schedule_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    sw = ChargeFromGridScheduleSwitch(coord)

    assert sw.available is False
    coord._battery_charge_from_grid = True  # noqa: SLF001
    assert sw.available is True
    assert sw.is_on is True


def test_charge_from_grid_schedule_switch_suggested_object_id(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sw = ChargeFromGridScheduleSwitch(coord)
    assert sw.suggested_object_id == "charge_from_grid_schedule"


@pytest.mark.asyncio
async def test_charge_from_grid_schedule_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = False  # noqa: SLF001
    coord.async_set_charge_from_grid_schedule_enabled = AsyncMock()
    sw = ChargeFromGridScheduleSwitch(coord)

    await sw.async_turn_on()
    coord.async_set_charge_from_grid_schedule_enabled.assert_awaited_with(True)

    await sw.async_turn_off()
    coord.async_set_charge_from_grid_schedule_enabled.assert_awaited_with(False)


def test_charge_from_grid_switches_unavailable_when_coordinator_unavailable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.last_update_success = False
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001

    assert ChargeFromGridSwitch(coord).available is False
    assert ChargeFromGridScheduleSwitch(coord).available is False


@pytest.mark.asyncio
async def test_savings_use_battery_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord.async_set_savings_use_battery_after_peak = AsyncMock()
    sw = SavingsUseBatteryAfterPeakSwitch(coord)

    await sw.async_turn_on()
    coord.async_set_savings_use_battery_after_peak.assert_awaited_with(True)

    await sw.async_turn_off()
    coord.async_set_savings_use_battery_after_peak.assert_awaited_with(False)


@pytest.mark.asyncio
async def test_storm_guard_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._storm_guard_state = "disabled"  # noqa: SLF001
    coord._storm_evse_enabled = False  # noqa: SLF001
    coord.async_set_storm_guard_enabled = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    sw = StormGuardSwitch(coord)

    await sw.async_turn_on()
    coord.async_set_storm_guard_enabled.assert_awaited_with(True)
    coord.async_request_refresh.assert_awaited_once()

    coord.async_request_refresh.reset_mock()
    await sw.async_turn_off()
    coord.async_set_storm_guard_enabled.assert_awaited_with(False)
    coord.async_request_refresh.assert_awaited_once()


def test_storm_guard_evse_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory({"storm_guard_state": None, "storm_evse_enabled": None})
    sw = StormGuardEvseSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False

    coord.data[RANDOM_SERIAL]["storm_guard_state"] = "enabled"
    coord.data[RANDOM_SERIAL]["storm_evse_enabled"] = True
    assert sw.available is True
    assert sw.is_on is True


def test_storm_guard_evse_switch_hidden_by_site_settings(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"storm_guard_state": "enabled", "storm_evse_enabled": True}
    )
    coord._battery_show_storm_guard = False  # noqa: SLF001
    sw = StormGuardEvseSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


def test_storm_guard_evse_switch_unavailable_without_data(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.data = {}
    sw = StormGuardEvseSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


@pytest.mark.asyncio
async def test_storm_guard_evse_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"storm_guard_state": "enabled", "storm_evse_enabled": False}
    )
    coord.async_set_storm_evse_enabled = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    sw = StormGuardEvseSwitch(coord, RANDOM_SERIAL)

    await sw.async_turn_on()
    coord.async_set_storm_evse_enabled.assert_awaited_with(True)
    coord.async_request_refresh.assert_awaited_once()

    coord.async_request_refresh.reset_mock()
    await sw.async_turn_off()
    coord.async_set_storm_evse_enabled.assert_awaited_with(False)
    coord.async_request_refresh.assert_awaited_once()
