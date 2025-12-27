from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_ON
from homeassistant.core import State

from custom_components.enphase_ev import DOMAIN
from custom_components.enphase_ev.coordinator import EnphaseCoordinator
from custom_components.enphase_ev.entity import EnphaseBaseEntity
from custom_components.enphase_ev.switch import ChargingSwitch, async_setup_entry
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
