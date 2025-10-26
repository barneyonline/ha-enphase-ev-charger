"""Tests for device triggers."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.helpers import device_registry as dr, entity_registry as er

triggers_pkg = ModuleType("homeassistant.components.automation.triggers")
state_mod = ModuleType("homeassistant.components.automation.triggers.state")


async def _placeholder_async_attach_trigger(*args, **kwargs):
    raise NotImplementedError


state_mod.async_attach_trigger = _placeholder_async_attach_trigger
triggers_pkg.state = state_mod
sys.modules.setdefault("homeassistant.components.automation", ModuleType("homeassistant.components.automation"))
sys.modules["homeassistant.components.automation.triggers"] = triggers_pkg
sys.modules["homeassistant.components.automation.triggers.state"] = state_mod
sys.modules["homeassistant.components.automation"].triggers = triggers_pkg

from custom_components.enphase_ev import device_trigger
from custom_components.enphase_ev.const import DOMAIN
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


@pytest.fixture
def device_entry(hass, config_entry):
    """Create a device and matching binary sensors for trigger discovery."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL), (DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Garage Charger",
    )

    ent_reg = er.async_get(hass)
    entity_ids: list[str] = []
    for tkey, unique in (
        ("charging", "charging-bin"),
        ("plugged_in", "plug-bin"),
        ("faulted", "fault-bin"),
    ):
        entry = ent_reg.async_get_or_create(
            domain="binary_sensor",
            platform="enphase_ev",
            unique_id=unique,
            device_id=device.id,
            config_entry=config_entry,
            translation_key=tkey,
        )
        entity_ids.append(entry.entity_id)
    return device, entity_ids


@pytest.mark.asyncio
async def test_async_get_triggers_exposes_device_triggers(hass, device_entry):
    """All known triggers for the device should be discovered."""
    device, _ = device_entry
    triggers = await device_trigger.async_get_triggers(hass, device.id)
    assert {trigger["type"] for trigger in triggers} == {
        "charging_started",
        "charging_stopped",
        "plugged_in",
        "unplugged",
        "faulted",
    }
    assert all(trigger["entity_id"].startswith("binary_sensor.") for trigger in triggers)


@pytest.mark.asyncio
async def test_async_attach_trigger_wraps_state_trigger(
    hass, device_entry, monkeypatch
):
    """Attaching a trigger should delegate to the state trigger helper."""
    async_mock = AsyncMock(return_value="unsubscribe")
    monkeypatch.setattr(
        device_trigger.state_trigger, "async_attach_trigger", async_mock
    )

    action = MagicMock()
    automation_info = {"name": "automation"}

    device, _ = device_entry
    unsubscribe = await device_trigger.async_attach_trigger(
        hass,
        {"device_id": device.id, "type": "charging_started"},
        action,
        automation_info,
    )

    async_mock.assert_awaited_once()
    state_cfg = async_mock.await_args.args[1]
    assert state_cfg["entity_id"].startswith("binary_sensor.")
    assert state_cfg["to"] == "on"
    assert state_cfg["from"] == "off"
    assert unsubscribe == "unsubscribe"


@pytest.mark.asyncio
async def test_async_attach_trigger_handles_missing_entity(hass, device_entry):
    """If the entity is missing, the helper should return a no-op."""
    device, entity_ids = device_entry
    ent_reg = er.async_get(hass)
    for entity_id in entity_ids:
        ent_reg.async_remove(entity_id)

    detach = await device_trigger.async_attach_trigger(
        hass, {"device_id": device.id, "type": "charging_started"}, None, {}
    )
    assert callable(detach)
    assert detach() is None
