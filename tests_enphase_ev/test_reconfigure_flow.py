import pytest


@pytest.mark.asyncio
async def test_reconfigure_shows_form(hass, monkeypatch):
    from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )

    class Entry:
        def __init__(self):
            self.data = {
                CONF_SITE_ID: "1234567",
                CONF_SERIALS: ["555555555555"],
                CONF_SCAN_INTERVAL: 30,
                CONF_EAUTH: "EAUTH",
                CONF_COOKIE: "COOKIE",
            }

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {}
    entry = Entry()
    monkeypatch.setattr(EnphaseEVConfigFlow, "_get_reconfigure_entry", lambda self: entry)

    res = await flow.async_step_reconfigure()
    assert res["type"].name == "FORM"
    assert res["step_id"] == "manual"


@pytest.mark.asyncio
async def test_reconfigure_updates_entry_on_submit(hass, monkeypatch):
    from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )

    class Entry:
        def __init__(self):
            self.data = {
                CONF_SITE_ID: "1234567",
                CONF_SERIALS: ["555555555555"],
                CONF_SCAN_INTERVAL: 30,
                CONF_EAUTH: "EAUTH_OLD",
                CONF_COOKIE: "COOKIE_OLD",
            }
            self.entry_id = "entry-id"
            self.unique_id = "1234567"

    class StubClient:
        def __init__(self, *a, **k):
            pass

        async def status(self):
            return {"ok": True}

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {}
    entry = Entry()
    # Monkeypatch helpers inside reconfigure
    monkeypatch.setattr(EnphaseEVConfigFlow, "_get_reconfigure_entry", lambda self: entry)
    # Patch the client in its source module, since the flow imports it inside the function
    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.EnphaseEVClient", StubClient
    )
    # Provide aiohttp and session helper to avoid import/runtime dependencies
    import sys
    import types
    from tests_enphase_ev.conftest import _DummySession

    monkeypatch.setitem(sys.modules, "aiohttp", types.SimpleNamespace(ClientError=Exception))
    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.async_get_clientsession",
        lambda hass: _DummySession(),
    )
    # Prefer the helper if present to return a result with a type name (awaitable)
    async def _helper(entry, data_updates=None):
        return {"type": type("T", (), {"name": "ABORT"})}
    flow.async_update_reload_and_abort = _helper

    user_input = {
        CONF_SITE_ID: "1234567",
        CONF_SERIALS: "555555555555",
        CONF_EAUTH: "EAUTH_NEW",
        CONF_COOKIE: "COOKIE_NEW",
        CONF_SCAN_INTERVAL: 15,
    }

    await flow.async_step_reconfigure()
    res = await flow.async_step_manual(user_input)
    assert res["type"].name in ("ABORT", "CREATE_ENTRY")


@pytest.mark.asyncio
async def test_reconfigure_wrong_account_aborts(hass, monkeypatch):
    from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )

    class Entry:
        def __init__(self):
            self.data = {
                CONF_SITE_ID: "1234567",
                CONF_SERIALS: ["555555555555"],
                CONF_SCAN_INTERVAL: 30,
                CONF_EAUTH: "EAUTH_OLD",
                CONF_COOKIE: "COOKIE_OLD",
            }
            self.entry_id = "entry-id"
            self.unique_id = "1234567"

    class StubClient:
        def __init__(self, *a, **k):
            pass

        async def status(self):
            return {"ok": True}

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {}
    entry = Entry()

    # Patch environment and helpers
    import sys
    import types
    from tests_enphase_ev.conftest import _DummySession

    monkeypatch.setitem(sys.modules, "aiohttp", types.SimpleNamespace(ClientError=Exception))
    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.async_get_clientsession",
        lambda hass: _DummySession(),
    )
    monkeypatch.setattr(EnphaseEVConfigFlow, "_get_reconfigure_entry", lambda self: entry)
    monkeypatch.setattr("custom_components.enphase_ev.config_flow.EnphaseEVClient", StubClient)

    user_input = {
        CONF_SITE_ID: "7654321",  # different from entry (1234567)
        CONF_SERIALS: "555555555555",
        CONF_EAUTH: "EAUTH_NEW",
        CONF_COOKIE: "COOKIE_NEW",
        CONF_SCAN_INTERVAL: 15,
    }

    await flow.async_step_reconfigure()
    from homeassistant.data_entry_flow import AbortFlow

    with pytest.raises(AbortFlow):
        await flow.async_step_manual(user_input)


@pytest.mark.asyncio
async def test_reconfigure_curl_autofill(hass, monkeypatch):
    from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )

    class Entry:
        def __init__(self):
            self.data = {
                CONF_SITE_ID: "1234567",
                CONF_SERIALS: ["555555555555"],
                CONF_SCAN_INTERVAL: 30,
                CONF_EAUTH: "EAUTH_OLD",
                CONF_COOKIE: "COOKIE_OLD",
            }
            self.entry_id = "entry-id"
            self.unique_id = "1234567"

    class StubClient:
        def __init__(self, *a, **k):
            pass

        async def status(self):
            return {"ok": True}

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {}
    entry = Entry()

    import sys
    import types
    from tests_enphase_ev.conftest import _DummySession

    monkeypatch.setitem(sys.modules, "aiohttp", types.SimpleNamespace(ClientError=Exception))
    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.async_get_clientsession",
        lambda hass: _DummySession(),
    )
    monkeypatch.setattr(EnphaseEVConfigFlow, "_get_reconfigure_entry", lambda self: entry)
    monkeypatch.setattr("custom_components.enphase_ev.config_flow.EnphaseEVClient", StubClient)

    monkeypatch.setattr(EnphaseEVConfigFlow, "_abort_if_unique_id_mismatch", lambda *a, **k: None)

    # Provide helper to return a recognizable result (awaitable)
    async def _helper(entry, data_updates=None):
        return {"type": type("T", (), {"name": "ABORT"})}
    flow.async_update_reload_and_abort = _helper

    # cURL that includes the same site id as entry to avoid mismatch
    curl = (
        "curl 'https://enlighten.enphaseenergy.com/service/evse_controller/1234567/ev_chargers/status' "
        "-H 'e-auth-token: TOKEN123' -H 'Cookie: COOKIE123'"
    )

    user_input = {
        CONF_SITE_ID: "1234567",  # will be set from curl anyway
        CONF_SERIALS: "555555555555",
        CONF_EAUTH: "",
        CONF_COOKIE: "",
        CONF_SCAN_INTERVAL: 20,
        "curl": curl,
    }

    await flow.async_step_reconfigure()
    res = await flow.async_step_manual(user_input)
    assert res["type"].name in ("ABORT", "CREATE_ENTRY")
