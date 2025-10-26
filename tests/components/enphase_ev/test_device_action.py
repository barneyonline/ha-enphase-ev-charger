"""Tests for the device action helpers."""

from __future__ import annotations

import sys
from datetime import timedelta
from importlib import import_module
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.const import CONF_DEVICE_ID
from homeassistant.helpers import device_registry as dr

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID

# Provide a lightweight shim for the device automation constants to avoid
# importing the full Home Assistant stack during unit tests.
shim = ModuleType("homeassistant.components.device_automation.const")
shim.CONF_TYPE = "type"
sys.modules["homeassistant.components.device_automation.const"] = shim

device_action = import_module("custom_components.enphase_ev.device_action")
const_module = import_module("custom_components.enphase_ev.const")
DOMAIN = const_module.DOMAIN

CONF_TYPE = "type"


@pytest.fixture
def device_id(hass, config_entry) -> str:
    """Create a charger device linked to the config entry."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL), (DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Garage Charger",
    )
    return device.id


def _make_coordinator() -> SimpleNamespace:
    """Return a coordinator stub with the attributes device actions rely on."""

    class DummyCoord(SimpleNamespace):
        pass

    coord = DummyCoord()
    coord.serials = {RANDOM_SERIAL}
    coord.data = {RANDOM_SERIAL: {}}
    coord.site_id = RANDOM_SITE_ID
    coord.update_interval = timedelta(seconds=30)
    coord.phase_timings = {}

    coord.require_plugged = MagicMock()
    coord.pick_start_amps = MagicMock(return_value=32)
    coord.set_last_set_amps = MagicMock()
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.async_request_refresh = AsyncMock()

    client = SimpleNamespace()
    client.start_charging = AsyncMock(return_value={"status": "ok"})
    client.stop_charging = AsyncMock(return_value=None)
    coord.client = client

    return coord


@pytest.mark.asyncio
async def test_async_get_actions_returns_start_stop(hass, config_entry, device_id) -> None:
    """Device actions should expose start/stop toggles for charger devices."""
    actions = await device_action.async_get_actions(hass, device_id)
    assert len(actions) == 2
    assert {
        (action[CONF_DEVICE_ID], action[CONF_TYPE])
        for action in actions
    } == {(device_id, device_action.ACTION_START), (device_id, device_action.ACTION_STOP)}


@pytest.mark.asyncio
async def test_async_call_action_start_success(
    hass, config_entry, device_id, monkeypatch
) -> None:
    """Starting a charge session should invoke the coordinator helpers."""
    coord = _make_coordinator()
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    config = {
        CONF_DEVICE_ID: device_id,
        CONF_TYPE: device_action.ACTION_START,
        "charging_level": 30,
        "connector_id": 2,
    }

    await device_action.async_call_action_from_config(hass, config, {}, None)

    coord.require_plugged.assert_called_once_with(RANDOM_SERIAL)
    coord.pick_start_amps.assert_called_once_with(RANDOM_SERIAL, 30)
    coord.client.start_charging.assert_awaited_once_with(RANDOM_SERIAL, 32, 2)
    coord.set_last_set_amps.assert_called_once_with(RANDOM_SERIAL, 32)
    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, True)
    coord.set_charging_expectation.assert_called_with(RANDOM_SERIAL, True, hold_for=90)
    coord.kick_fast.assert_called_with(90)
    coord.async_request_refresh.assert_awaited()


@pytest.mark.asyncio
async def test_async_call_action_start_handles_not_ready(
    hass, config_entry, device_id
) -> None:
    """A not_ready response should flip desired charging back to False."""
    coord = _make_coordinator()
    coord.client.start_charging.return_value = {"status": "not_ready"}
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    await device_action.async_call_action_from_config(
        hass,
        {
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: device_action.ACTION_START,
        },
        {},
        None,
    )

    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, False)


@pytest.mark.asyncio
async def test_async_call_action_stop(hass, config_entry, device_id) -> None:
    """Stopping a charge session should invoke the coordinator client."""
    coord = _make_coordinator()
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    await device_action.async_call_action_from_config(
        hass,
        {
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: device_action.ACTION_STOP,
        },
        {},
        None,
    )

    coord.client.stop_charging.assert_awaited_once_with(RANDOM_SERIAL)
    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, False)
    coord.set_charging_expectation.assert_called_with(RANDOM_SERIAL, False, hold_for=90)
    coord.kick_fast.assert_called_with(60)


@pytest.mark.asyncio
async def test_async_get_action_capabilities() -> None:
    """Capabilities schema should expose optional fields for start action."""
    result = await device_action.async_get_action_capabilities(
        None,
        {
            CONF_TYPE: device_action.ACTION_START,
        },
    )
    schema = result["extra_fields"]
    validated = schema(
        {
            "charging_level": 24,
            "connector_id": 1,
        }
    )
    assert validated["charging_level"] == 24
    assert validated["connector_id"] == 1
