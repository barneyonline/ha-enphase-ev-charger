from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.number import (
    BatteryReserveNumber,
    async_setup_entry as async_setup_number_entry,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.sensor import EnphaseBatteryModeSensor
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


def _inventory_without_battery(serial: str) -> dict[str, dict[str, object]]:
    return {
        "envoy": {
            "type_key": "envoy",
            "type_label": "Gateway",
            "count": 1,
            "devices": [
                {"serial_number": f"GW-{RANDOM_SITE_ID}", "name": "IQ Gateway"}
            ],
        },
        "iqevse": {
            "type_key": "iqevse",
            "type_label": "EV Chargers",
            "count": 1,
            "devices": [{"serial_number": serial, "name": "Garage EV"}],
        },
    }


def test_has_type_for_entities_uses_battery_site_settings_fallback(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord._set_type_device_buckets(  # noqa: SLF001
        _inventory_without_battery(RANDOM_SERIAL),
        ["envoy", "iqevse"],
    )
    coord._battery_has_encharge = True  # noqa: SLF001

    assert coord.has_type("encharge") is False
    assert coord.has_type_for_entities("encharge") is True


def test_battery_mode_sensor_available_when_inventory_omits_encharge(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord._set_type_device_buckets(  # noqa: SLF001
        _inventory_without_battery(RANDOM_SERIAL),
        ["envoy", "iqevse"],
    )
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_grid_mode = "ImportExport"  # noqa: SLF001
    coord.last_success_utc = dt_util.utcnow()

    sensor = EnphaseBatteryModeSensor(coord)

    assert sensor.available is True
    assert sensor.native_value == "Import and Export"


@pytest.mark.asyncio
async def test_number_setup_adds_battery_entities_when_site_settings_confirm_battery(
    hass,
    config_entry,
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._set_type_device_buckets(  # noqa: SLF001
        _inventory_without_battery(RANDOM_SERIAL),
        ["envoy", "iqevse"],
    )
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.async_add_listener = MagicMock(return_value=lambda: None)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_number_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, BatteryReserveNumber) for entity in added)
