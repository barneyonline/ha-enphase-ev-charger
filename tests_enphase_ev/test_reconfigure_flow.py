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


@pytest.mark.asyncio
async def test_reconfigure_wrong_account_aborts(monkeypatch):
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
    class Hass:
        class CEM:
            def async_update_entry(self, *a, **k):
                return None
            async def async_reload(self, *a, **k):
                return None
        config_entries = CEM()
    flow.hass = Hass()

    # Patch environment and helpers
    import sys, types
    monkeypatch.setitem(sys.modules, "aiohttp", types.SimpleNamespace(ClientError=Exception))
    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.async_get_clientsession", lambda hass: object()
    )
    monkeypatch.setattr(EnphaseEVConfigFlow, "_get_reconfigure_entry", lambda self: Entry())
    monkeypatch.setattr("custom_components.enphase_ev.api.EnphaseEVClient", StubClient)

    # Make async_set_unique_id awaitable
    async def _noop_async(*a, **k):
        return None
    monkeypatch.setattr(EnphaseEVConfigFlow, "async_set_unique_id", _noop_async)

    # Force abort when site ID mismatches by raising from mismatch guard
    def _raise_abort(*a, **k):  # called with reason="wrong_account"
        raise RuntimeError("wrong_account")
    monkeypatch.setattr(EnphaseEVConfigFlow, "_abort_if_unique_id_mismatch", _raise_abort)

    user_input = {
        CONF_SITE_ID: "7654321",  # different from entry (1234567)
        CONF_SERIALS: "555555555555",
        CONF_EAUTH: "EAUTH_NEW",
        CONF_COOKIE: "COOKIE_NEW",
        CONF_SCAN_INTERVAL: 15,
    }

    with pytest.raises(RuntimeError):
        await flow.async_step_reconfigure(user_input)


@pytest.mark.asyncio
async def test_reconfigure_curl_autofill(monkeypatch):
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
    class Hass:
        class CEM:
            def async_update_entry(self, *a, **k):
                return None
            async def async_reload(self, *a, **k):
                return None
        config_entries = CEM()
    flow.hass = Hass()

    import sys, types
    monkeypatch.setitem(sys.modules, "aiohttp", types.SimpleNamespace(ClientError=Exception))
    monkeypatch.setattr(
        "custom_components.enphase_ev.config_flow.async_get_clientsession", lambda hass: object()
    )
    monkeypatch.setattr(EnphaseEVConfigFlow, "_get_reconfigure_entry", lambda self: Entry())
    monkeypatch.setattr("custom_components.enphase_ev.api.EnphaseEVClient", StubClient)

    async def _noop_async(*a, **k):
        return None
    monkeypatch.setattr(EnphaseEVConfigFlow, "async_set_unique_id", _noop_async)
    monkeypatch.setattr(EnphaseEVConfigFlow, "_abort_if_unique_id_mismatch", lambda *a, **k: None)

    # Provide helper to return a recognizable result
    flow.async_update_reload_and_abort = lambda entry, data_updates=None: {
        "type": type("T", (), {"name": "ABORT"})
    }

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

    res = await flow.async_step_reconfigure(user_input)
    assert res["type"].name in ("ABORT", "CREATE_ENTRY")
