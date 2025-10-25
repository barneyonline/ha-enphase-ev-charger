from __future__ import annotations

import voluptuous as vol
from homeassistant.components.device_automation.const import CONF_TYPE
from homeassistant.const import CONF_DEVICE_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

ACTION_START = "start_charging"
ACTION_STOP = "stop_charging"
## Removed set_charging_amps action since amps are read-only now


async def async_get_actions(hass: HomeAssistant, device_id: str):
    actions = []
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if not device:
        return actions
    if not any(
        domain == DOMAIN and not ident.startswith("site:")
        for domain, ident in device.identifiers
    ):
        return actions

    for typ in (ACTION_START, ACTION_STOP):
        actions.append({CONF_DEVICE_ID: device_id, CONF_TYPE: typ, "domain": DOMAIN})
    return actions


async def async_call_action_from_config(
    hass: HomeAssistant, config: ConfigType, variables, context
):
    typ = config[CONF_TYPE]
    device_id = config[CONF_DEVICE_ID]

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if not device:
        return
    # Resolve serial and coordinator
    sn = None
    for domain, ident in device.identifiers:
        if domain == DOMAIN and not ident.startswith("site:"):
            sn = ident
            break
    if not sn:
        return
    coord = None
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and "coordinator" in entry_data:
            c = entry_data["coordinator"]
            if not c.serials or sn in c.serials or sn in (c.data or {}):
                coord = c
                break
    if not coord:
        return

    if typ == ACTION_START:
        level = config.get("charging_level")
        connector_id = int(config.get("connector_id", 1))
        coord.require_plugged(sn)
        amps = coord.pick_start_amps(sn, level)
        result = await coord.client.start_charging(sn, amps, connector_id)
        coord.set_last_set_amps(sn, amps)
        if isinstance(result, dict) and result.get("status") == "not_ready":
            coord.set_desired_charging(sn, False)
            return
        coord.set_desired_charging(sn, True)
        coord.set_charging_expectation(sn, True, hold_for=90)
        coord.kick_fast(90)
        await coord.async_request_refresh()
        return

    if typ == ACTION_STOP:
        await coord.client.stop_charging(sn)
        coord.set_desired_charging(sn, False)
        coord.set_charging_expectation(sn, False, hold_for=90)
        coord.kick_fast(60)
        await coord.async_request_refresh()
        return

    # Amps are read-only; no set action


async def async_get_action_capabilities(hass: HomeAssistant, config: ConfigType):
    typ = config[CONF_TYPE]
    fields = {}
    if typ in (ACTION_START,):
        fields[vol.Optional("charging_level", default=32)] = vol.All(
            int, vol.Range(min=6, max=40)
        )
    if typ == ACTION_START:
        fields[vol.Optional("connector_id", default=1)] = vol.All(
            int, vol.Range(min=1, max=2)
        )
    return {"extra_fields": vol.Schema(fields) if fields else vol.Schema({})}
