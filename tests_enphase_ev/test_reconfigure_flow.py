import pytest


@pytest.mark.asyncio
async def test_reconfigure_shows_form(monkeypatch):
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
    # Hass object not used for the display path
    flow.hass = object()
    monkeypatch.setattr(EnphaseEVConfigFlow, "_get_reconfigure_entry", lambda self: Entry())

    res = await flow.async_step_reconfigure()
    assert res["type"].name == "FORM"
    assert res["step_id"] == "reconfigure"


@pytest.mark.asyncio
async def test_reconfigure_updates_entry_on_submit(monkeypatch):
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

    class StubClient:
        def __init__(self, *a, **k):
            pass

        async def status(self):
            return {"ok": True}

    flow = EnphaseEVConfigFlow()
    # Provide a minimal hass with config_entries manager used in fallback path
    class CEM:
        def async_update_entry(self, *a, **k):
            return None

        async def async_reload(self, *a, **k):
            return None

    class Hass:
        config_entries = CEM()

    flow.hass = Hass()

    # Monkeypatch helpers inside reconfigure
    monkeypatch.setattr(EnphaseEVConfigFlow, "_get_reconfigure_entry", lambda self: Entry())
    # Patch the client in its source module, since the flow imports it inside the function
    monkeypatch.setattr(
        "custom_components.enphase_ev.api.EnphaseEVClient", StubClient
    )
    # Provide aiohttp and session helper to avoid import/runtime dependencies
    import sys, types
    monkeypatch.setitem(sys.modules, "aiohttp", types.SimpleNamespace(ClientError=Exception))
    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.async_get_clientsession", lambda hass: object()
    )
    # Bypass unique_id guard in test (make awaitable)
    async def _noop_async(*a, **k):
        return None
    monkeypatch.setattr(EnphaseEVConfigFlow, "async_set_unique_id", _noop_async)
    monkeypatch.setattr(EnphaseEVConfigFlow, "_abort_if_unique_id_mismatch", lambda *a, **k: None)
    # Prefer the helper if present to return a result with a type name
    flow.async_update_reload_and_abort = lambda entry, data_updates=None: {
        "type": type("T", (), {"name": "ABORT"})
    }

    user_input = {
        CONF_SITE_ID: "1234567",
        CONF_SERIALS: "555555555555",
        CONF_EAUTH: "EAUTH_NEW",
        CONF_COOKIE: "COOKIE_NEW",
        CONF_SCAN_INTERVAL: 15,
    }

    res = await flow.async_step_reconfigure(user_input)
    assert res["type"].name in ("ABORT", "CREATE_ENTRY")
