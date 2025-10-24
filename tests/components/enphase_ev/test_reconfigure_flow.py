from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow
from custom_components.enphase_ev.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REMEMBER_PASSWORD,
    CONF_SITE_ID,
    DOMAIN,
)


@pytest.mark.asyncio
async def test_reconfigure_login_entry_shows_user_form(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "123456",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "secret",
        },
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_RECONFIGURE,
        "entry_id": entry.entry_id,
    }

    result = await flow.async_step_reconfigure()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_reconfigure_manual_entry_aborts(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "123456"},
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_RECONFIGURE,
        "entry_id": entry.entry_id,
    }

    result = await flow.async_step_reconfigure()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "manual_mode_removed"


@pytest.mark.asyncio
async def test_reauth_manual_entry_aborts(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "123456"},
    )
    entry.add_to_hass(hass)

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }

    result = await flow.async_step_reauth({})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "manual_mode_removed"
