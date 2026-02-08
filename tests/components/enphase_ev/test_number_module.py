from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.enphase_ev import DOMAIN
from custom_components.enphase_ev.number import (
    BatteryReserveNumber,
    ChargingAmpsNumber,
    async_setup_entry,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


@pytest.mark.asyncio
async def test_async_setup_entry_syncs_new_serials(hass, config_entry) -> None:
    coord = SimpleNamespace()
    coord.site_id = "123456"
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
    assert [ent._sn for ent in added if hasattr(ent, "_sn")] == [
        RANDOM_SERIAL,
        "EV2",
        "EV2",
    ]
    assert any(isinstance(ent, BatteryReserveNumber) for ent in added)
    assert config_entry._on_unload


@pytest.mark.asyncio
async def test_async_setup_entry_handles_no_serials(hass, config_entry) -> None:
    """No new serials should short-circuit without adding entities."""
    coord = SimpleNamespace()
    coord.site_id = "123456"
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

    assert len(added) == 1
    assert isinstance(added[0], BatteryReserveNumber)


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


def test_charging_number_safe_limit_overrides(hass, config_entry) -> None:
    from custom_components.enphase_ev.const import SAFE_LIMIT_AMPS

    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "charging_level": 32,
                "safe_limit_state": True,
                "min_amp": 6,
                "max_amp": 40,
            }
        },
    )
    coord.pick_start_amps = MagicMock(return_value=30)

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == float(SAFE_LIMIT_AMPS)

    coord.data[RANDOM_SERIAL]["safe_limit_state"] = 0
    assert number.native_value == 32.0


def test_charging_number_safe_limit_invalid_value_ignored(
    hass, config_entry
) -> None:
    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 22, "safe_limit_state": BadStr()}},
    )

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == 22.0


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


def test_battery_reserve_number_dynamic_bounds(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 22}},
    )
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_backup_percentage = 24  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001

    number = BatteryReserveNumber(coord)

    assert number.available is True
    assert number.native_value == 24.0
    assert number.native_min_value == 10.0
    assert number.native_max_value == 100.0


@pytest.mark.asyncio
async def test_battery_reserve_number_sets_value(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 22}},
    )
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord.async_set_battery_reserve = AsyncMock()

    number = BatteryReserveNumber(coord)
    await number.async_set_native_value(30)

    coord.async_set_battery_reserve.assert_awaited_once_with(30)


def test_battery_reserve_number_unavailable_in_full_backup(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 22}},
    )
    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_backup_percentage = 100  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001

    number = BatteryReserveNumber(coord)
    assert number.available is False


def test_battery_reserve_number_handles_super_unavailable_and_none(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 22}},
    )
    coord.last_update_success = False
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_backup_percentage = None  # noqa: SLF001
    number = BatteryReserveNumber(coord)
    assert number.available is False
    assert number.native_value is None
