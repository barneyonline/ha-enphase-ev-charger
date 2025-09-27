import pytest


@pytest.mark.asyncio
async def test_reconfigure_login_entry_shows_user_form(hass, monkeypatch):
    from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow
    from custom_components.enphase_ev.const import (
        CONF_EMAIL,
        CONF_PASSWORD,
        CONF_REMEMBER_PASSWORD,
        CONF_SITE_ID,
    )

    class _Entry:
        def __init__(self) -> None:
            self.data = {
                CONF_SITE_ID: "123456",
                CONF_EMAIL: "user@example.com",
                CONF_REMEMBER_PASSWORD: True,
                CONF_PASSWORD: "secret",
            }

    entry = _Entry()
    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {}
    monkeypatch.setattr(EnphaseEVConfigFlow, "_get_reconfigure_entry", lambda self: entry)

    result = await flow.async_step_reconfigure()

    assert result["type"].name == "FORM"
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_reconfigure_manual_entry_aborts(hass, monkeypatch):
    from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow
    from custom_components.enphase_ev.const import CONF_SITE_ID

    class _Entry:
        def __init__(self) -> None:
            self.data = {CONF_SITE_ID: "123456"}

    entry = _Entry()
    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {}
    monkeypatch.setattr(EnphaseEVConfigFlow, "_get_reconfigure_entry", lambda self: entry)

    result = await flow.async_step_reconfigure()

    assert result["type"].name == "ABORT"
    assert result["reason"] == "manual_mode_removed"


@pytest.mark.asyncio
async def test_reauth_manual_entry_aborts(hass, monkeypatch):
    from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow
    from custom_components.enphase_ev.const import CONF_SITE_ID

    class _Entry:
        def __init__(self) -> None:
            self.data = {CONF_SITE_ID: "123456"}

    entry = _Entry()
    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {"entry_id": "entry-id"}

    class _ConfigEntries:
        @staticmethod
        def async_get_entry(entry_id):
            assert entry_id == "entry-id"
            return entry

    monkeypatch.setattr(hass, "config_entries", _ConfigEntries(), raising=False)
    result = await flow.async_step_reauth({})

    assert result["type"].name == "ABORT"
    assert result["reason"] == "manual_mode_removed"
