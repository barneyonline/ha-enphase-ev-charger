from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.enphase_ev import DOMAIN
from custom_components.enphase_ev.number import ChargingAmpsNumber, async_setup_entry
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


@pytest.mark.asyncio
async def test_async_setup_entry_syncs_new_serials(hass, config_entry) -> None:
    coord = SimpleNamespace()
    coord.serials = {RANDOM_SERIAL}
    coord._serial_order = [RANDOM_SERIAL]
    coord.data = {RANDOM_SERIAL: {"name": "Garage EV"}}

    def iter_serials():
        yield from [RANDOM_SERIAL, "EV2", "", None, "EV2"]

    coord.iter_serials = iter_serials
    added = []

    def capture(entities, update_before_add=False):
        added.extend(entities)

    coord.async_add_listener = MagicMock(return_value=lambda: None)

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    await async_setup_entry(hass, config_entry, capture)

    assert coord.async_add_listener.called
    assert [ent._sn for ent in added] == [RANDOM_SERIAL, "EV2", "EV2"]
    assert config_entry._on_unload


@pytest.mark.asyncio
async def test_async_setup_entry_handles_no_serials(hass, config_entry) -> None:
    """No new serials should short-circuit without adding entities."""
    coord = SimpleNamespace()
    coord.serials = set()
    coord._serial_order = []
    coord.data = {}
    coord.iter_serials = lambda: []
    coord.async_add_listener = MagicMock(return_value=lambda: None)

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert added == []


def _make_coordinator(hass, config_entry, data):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    with patch(
        "custom_components.enphase_ev.coordinator.async_get_clientsession",
        return_value=None,
    ):
        coord = EnphaseCoordinator(hass, config_entry.data, config_entry=config_entry)
    coord.data = data
    coord.last_set_amps = {}
    coord.async_request_refresh = AsyncMock()
    coord.set_last_set_amps = MagicMock(wraps=coord.set_last_set_amps)
    coord.client = SimpleNamespace()
    return coord


def test_charging_number_converts_values(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "charging_level": "36",
                "min_amp": "6",
                "max_amp": "48",
            }
        },
    )
    coord.pick_start_amps = MagicMock(return_value=30)

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == 36.0
    assert number.native_min_value == 6.0
    assert number.native_max_value == 48.0
    assert number.native_step == 1.0


def test_charging_number_fallbacks_to_pick_start(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "charging_level": None,
                "min_amp": "bad",
                "max_amp": "bad",
            }
        },
    )

    coord.pick_start_amps = MagicMock(return_value=28)

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == 28.0
    assert number.native_min_value == 6.0
    assert number.native_max_value == 40.0


def test_charging_number_invalid_level_uses_pick_start(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": "invalid", "min_amp": 6, "max_amp": 40}},
    )
    coord.pick_start_amps = MagicMock(return_value=26)

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == 26.0


@pytest.mark.asyncio
async def test_charging_number_set_value_records_and_refreshes(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 32, "min_amp": 6, "max_amp": 40}},
    )

    coord.schedule_amp_restart = MagicMock()
    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    await number.async_set_native_value(24)

    coord.set_last_set_amps.assert_called_once_with(RANDOM_SERIAL, 24)
    coord.async_request_refresh.assert_awaited_once()
    coord.schedule_amp_restart.assert_not_called()


@pytest.mark.asyncio
async def test_charging_number_set_value_restarts_when_active(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "charging_level": 20,
                "min_amp": 6,
                "max_amp": 40,
                "charging": True,
            }
        },
    )

    coord.schedule_amp_restart = MagicMock()
    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    await number.async_set_native_value(26)

    coord.schedule_amp_restart.assert_called_once_with(RANDOM_SERIAL)
