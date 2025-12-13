from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from voluptuous.schema_builder import Optional as VolOptional
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType, AbortFlow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev.api import (
    AuthTokens,
    ChargerInfo,
    EnlightenAuthInvalidCredentials,
    EnlightenAuthMFARequired,
    EnlightenAuthUnavailable,
    SiteInfo,
)
from custom_components.enphase_ev.config_flow import (
    EnphaseEVConfigFlow,
    OptionsFlowHandler,
)
from custom_components.enphase_ev.const import (
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REMEMBER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SESSION_ID,
    CONF_SITE_ID,
    CONF_SERIALS,
    CONF_SITE_ONLY,
    CONF_ACCESS_TOKEN,
    DOMAIN,
    OPT_API_TIMEOUT,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_NOMINAL_VOLTAGE,
    OPT_SESSION_HISTORY_INTERVAL,
    OPT_SLOW_POLL_INTERVAL,
)


TOKENS = AuthTokens(
    cookie="jar=1",
    session_id="sid-123",
    access_token="token-abc",
    token_expires_at=1_700_000_000,
)


def _make_flow(hass) -> EnphaseEVConfigFlow:
    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {}
    return flow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (EnlightenAuthInvalidCredentials(), "invalid_auth"),
        (EnlightenAuthMFARequired(), "mfa_required"),
        (EnlightenAuthUnavailable(), "service_unavailable"),
        (ValueError("boom"), "unknown"),
    ],
)
async def test_user_step_handles_auth_errors(hass, exc, expected) -> None:
    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            side_effect=exc,
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: " user@example.com ",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: True,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.asyncio
async def test_user_step_single_site_shortcuts_to_devices(hass) -> None:
    site = SiteInfo(site_id="site-123", name="Garage Site")
    chargers = [ChargerInfo(serial="EV123", name="Driveway")]

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(TOKENS, [site])),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: False,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._selected_site_id == "site-123"
    assert flow._chargers_loaded is True
    assert flow._chargers == [("EV123", "Driveway")]
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.asyncio
async def test_site_step_requires_selection(hass) -> None:
    flow = _make_flow(hass)
    flow._sites = {"site-1": "Existing"}
    result = await flow.async_step_site({})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "site_required"}


@pytest.mark.asyncio
async def test_site_step_handles_unknown_site_id(hass) -> None:
    flow = _make_flow(hass)
    flow._sites = {"site-1": "Existing"}
    flow._selected_site_id = None
    with patch.object(
        flow,
        "async_step_devices",
        AsyncMock(return_value={"type": FlowResultType.FORM, "step_id": "devices"}),
    ) as mock_devices:
        result = await flow.async_step_site({CONF_SITE_ID: "new-site"})

    assert result["type"] is FlowResultType.FORM
    mock_devices.assert_awaited_once()
    assert "new-site" in flow._sites


@pytest.mark.asyncio
async def test_site_step_without_options_uses_text_schema(hass) -> None:
    flow = _make_flow(hass)
    flow._sites = {}
    result = await flow.async_step_site()
    assert result["type"] is FlowResultType.FORM
    # No options provided, schema should still exist
    assert result["step_id"] == "site"


@pytest.mark.asyncio
async def test_devices_step_requires_serial_selection(hass) -> None:
    flow = _make_flow(hass)
    flow._chargers_loaded = True
    flow._chargers = [("EV1", "Garage")]
    result = await flow.async_step_devices({})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "serials_required"}


@pytest.mark.asyncio
async def test_devices_step_requires_site_only_opt_in(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "site-123"
    flow._sites = {"site-123": "Garage"}
    with patch(
        "custom_components.enphase_ev.config_flow.async_fetch_chargers",
        AsyncMock(return_value=[]),
    ):
        result = await flow.async_step_devices({})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "serials_or_site_only_required"}


@pytest.mark.asyncio
async def test_devices_step_site_only_schema_allows_empty_serials(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "site-123"
    flow._sites = {"site-123": "Garage"}
    with patch(
        "custom_components.enphase_ev.config_flow.async_fetch_chargers",
        AsyncMock(return_value=[]),
    ):
        result = await flow.async_step_devices()

    assert result["type"] is FlowResultType.FORM
    schema_keys = list(result["data_schema"].schema.keys())
    assert any(
        isinstance(key, VolOptional) and key.schema == CONF_SERIALS
        for key in schema_keys
    )


@pytest.mark.asyncio
async def test_devices_step_allows_site_only_entry(hass) -> None:
    site = SiteInfo(site_id="site-123", name="Garage Site")

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(TOKENS, [site])),
        ),
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=[]),
        ),
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        devices = await hass.config_entries.flow.async_configure(
            init["flow_id"],
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: False,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            devices["flow_id"],
            {CONF_SITE_ONLY: True, CONF_SCAN_INTERVAL: 55},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIALS] == []
    assert result["data"][CONF_SITE_ONLY] is True
    assert result["data"][CONF_SCAN_INTERVAL] == 55


@pytest.mark.asyncio
async def test_finalize_login_entry_without_state_aborts(hass) -> None:
    flow = _make_flow(hass)
    result = await flow._finalize_login_entry(["EV123"], 60)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"


@pytest.mark.asyncio
async def test_finalize_login_entry_reconfigure_awaits_helper(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "site-123",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: False,
        },
    )
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    flow._auth_tokens = TOKENS
    flow._sites = {"site-123": "Garage"}
    flow._selected_site_id = "site-123"
    flow._remember_password = False
    flow._email = "user@example.com"
    flow.async_update_reload_and_abort = AsyncMock(
        return_value={"type": FlowResultType.ABORT, "reason": "handled"}
    )

    result = await flow._finalize_login_entry(["EV123"], 45)

    assert result == {"type": FlowResultType.ABORT, "reason": "handled"}
    flow.async_update_reload_and_abort.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalize_login_entry_reconfigure_updates_entry(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="site-123",
        data={
            CONF_SITE_ID: "site-123",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "old-secret",
        },
    )
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    flow._auth_tokens = TOKENS
    flow._sites = {"site-123": "Garage"}
    flow._selected_site_id = "site-123"
    flow._remember_password = True
    flow._password = "new-secret"
    flow._email = "user@example.com"

    with patch.object(
        hass.config_entries, "async_reload", AsyncMock()
    ) as mock_reload:
        result = await flow._finalize_login_entry(["EV123"], 30)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"
    mock_reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_finalize_login_entry_sync_update_removes_none(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="site-123",
        data={
            CONF_SITE_ID: "site-123",
            CONF_EMAIL: "user@example.com",
            CONF_REMEMBER_PASSWORD: True,
            CONF_PASSWORD: "legacy",
            CONF_SESSION_ID: "old-session",
        },
    )
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    flow._auth_tokens = AuthTokens(
        cookie=None,
        session_id=None,
        access_token=None,
        token_expires_at=None,
    )
    flow._sites = {"site-123": "Garage"}
    flow._selected_site_id = "site-123"
    flow._remember_password = False
    flow._password = None
    flow._email = "user@example.com"

    captured: dict[str, dict] = {}

    def _sync_update(entry_obj, *, data_updates):
        captured["entry"] = entry_obj
        captured["data"] = dict(data_updates)
        return {"type": FlowResultType.ABORT, "reason": "sync"}

    flow.async_update_reload_and_abort = _sync_update  # type: ignore[assignment]

    result = await flow._finalize_login_entry(["EV1"], 30)

    assert result == {"type": FlowResultType.ABORT, "reason": "sync"}
    assert captured["entry"] is entry
    assert CONF_PASSWORD not in captured["data"]
    assert CONF_SESSION_ID not in captured["data"]
    assert CONF_COOKIE not in captured["data"]
    assert CONF_EAUTH not in captured["data"]
    assert CONF_ACCESS_TOKEN not in captured["data"]


@pytest.mark.asyncio
async def test_ensure_chargers_handles_missing_state(hass) -> None:
    flow = _make_flow(hass)
    await flow._ensure_chargers()
    assert flow._chargers_loaded is True
    assert flow._chargers == []


@pytest.mark.asyncio
async def test_ensure_chargers_fetches_from_api(hass) -> None:
    flow = _make_flow(hass)
    flow._auth_tokens = TOKENS
    flow._selected_site_id = "site-123"
    chargers = [ChargerInfo(serial="EV1", name=None)]

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ),
    ):
        await flow._ensure_chargers()

    assert flow._chargers_loaded is True
    assert flow._chargers == [("EV1", None)]


@pytest.mark.asyncio
async def test_ensure_chargers_skips_when_already_loaded(hass) -> None:
    flow = _make_flow(hass)
    flow._chargers_loaded = True

    with patch(
        "custom_components.enphase_ev.config_flow.async_fetch_chargers",
        AsyncMock(side_effect=AssertionError("should not call")),
    ):
        await flow._ensure_chargers()


def test_normalize_serials_variants(hass) -> None:
    flow = _make_flow(hass)
    assert flow._normalize_serials(["A", "A", " "]) == ["A"]
    assert flow._normalize_serials("A, B\nC") == ["A", "B", "C"]
    assert flow._normalize_serials(123) == []


def test_default_scan_interval_uses_reconfigure_value(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SCAN_INTERVAL: 15},
    )
    flow = _make_flow(hass)
    flow._reconfigure_entry = entry
    assert flow._default_scan_interval() == 15


def test_get_reconfigure_entry_falls_back_to_context(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    flow = _make_flow(hass)
    flow.context = {"entry_id": entry.entry_id}

    with patch.object(
        config_entries.ConfigFlow, "_get_reconfigure_entry", side_effect=Exception
    ):
        assert flow._get_reconfigure_entry() == entry


@pytest.mark.asyncio
async def test_abort_if_unique_id_mismatch_fallback(hass) -> None:
    flow = _make_flow(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_SITE_ID: "site-1"}, unique_id="site-1"
    )
    flow._reconfigure_entry = entry
    flow._get_reconfigure_entry = MagicMock(return_value=entry)
    await flow.async_set_unique_id("site-2")

    with patch(
        "homeassistant.config_entries.ConfigFlow._abort_if_unique_id_mismatch",
        side_effect=AttributeError,
    ):
        with pytest.raises(AbortFlow):
            flow._abort_if_unique_id_mismatch(reason="wrong_account")


def test_abort_if_unique_id_mismatch_no_entry(hass) -> None:
    flow = _make_flow(hass)
    flow._get_reconfigure_entry = MagicMock(return_value=None)
    with patch(
        "homeassistant.config_entries.ConfigFlow._abort_if_unique_id_mismatch",
        side_effect=AttributeError,
    ):
        # Should not raise when there is no entry
        flow._abort_if_unique_id_mismatch(reason="wrong_account")


@pytest.mark.asyncio
async def test_abort_if_unique_id_mismatch_propagates_abort(hass, monkeypatch) -> None:
    flow = _make_flow(hass)
    flow._get_reconfigure_entry = MagicMock(return_value=None)

    def raise_abort(self, *, reason):
        raise AbortFlow(reason)

    monkeypatch.setattr(
        config_entries.ConfigFlow,
        "_abort_if_unique_id_mismatch",
        raise_abort,
        raising=False,
    )

    with pytest.raises(AbortFlow):
        flow._abort_if_unique_id_mismatch(reason="wrong_account")


def test_abort_if_unique_id_mismatch_handles_generic_exception(hass) -> None:
    flow = _make_flow(hass)
    flow._get_reconfigure_entry = MagicMock(return_value=None)

    with patch(
        "homeassistant.config_entries.ConfigFlow._abort_if_unique_id_mismatch",
        side_effect=RuntimeError("boom"),
    ):
        flow._abort_if_unique_id_mismatch(reason="wrong_account")


@pytest.mark.asyncio
async def test_async_step_reconfigure_missing_entry_aborts(hass) -> None:
    flow = _make_flow(hass)
    flow._get_reconfigure_entry = MagicMock(return_value=None)
    result = await flow.async_step_reconfigure()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"


@pytest.mark.asyncio
async def test_async_step_reauth_missing_entry_aborts(hass) -> None:
    flow = _make_flow(hass)
    flow.context = {"entry_id": "missing"}
    with patch.object(
        hass.config_entries, "async_get_entry", return_value=None
    ):
        result = await flow.async_step_reauth({})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"


def test_async_get_options_flow_returns_handler(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    handler = EnphaseEVConfigFlow.async_get_options_flow(entry)
    assert isinstance(handler, OptionsFlowHandler)


def test_options_flow_init_fallback(monkeypatch, hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})

    original_init = config_entries.OptionsFlow.__init__

    def maybe_raise(self, *args, **kwargs):
        if args or kwargs:
            raise TypeError
        return original_init(self)

    monkeypatch.setattr(
        config_entries.OptionsFlow, "__init__", maybe_raise
    )

    handler = OptionsFlowHandler(entry)
    assert handler._entry is entry


@pytest.mark.asyncio
async def test_options_flow_forget_password(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: "site-123",
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_REMEMBER_PASSWORD: True,
        },
    )
    entry.add_to_hass(hass)

    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init({"forget_password": True})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_REMEMBER_PASSWORD] is False
    assert CONF_PASSWORD not in entry.data


@pytest.mark.asyncio
async def test_options_flow_reauth_invokes_callback(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.async_start_reauth = AsyncMock()

    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init({"reauth": True})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry.async_start_reauth.assert_awaited_once_with(hass)


@pytest.mark.asyncio
async def test_options_flow_show_form_with_defaults(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SCAN_INTERVAL: 33},
        options={},
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    with patch.object(
        handler, "add_suggested_values_to_schema", wraps=handler.add_suggested_values_to_schema
    ) as mock_add:
        result = await handler.async_step_init()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    mock_add.assert_called_once()


@pytest.mark.asyncio
async def test_options_flow_show_form_uses_existing_options(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SCAN_INTERVAL: 40},
        options={
            OPT_FAST_POLL_INTERVAL: 5,
            OPT_SLOW_POLL_INTERVAL: 120,
            OPT_FAST_WHILE_STREAMING: False,
            OPT_API_TIMEOUT: 25,
            OPT_NOMINAL_VOLTAGE: 230,
            OPT_SESSION_HISTORY_INTERVAL: 30,
            CONF_SITE_ONLY: True,
        },
    )
    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    result = await handler.async_step_init()

    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"]
    validated = schema({})
    assert validated[OPT_FAST_POLL_INTERVAL] == 5
    assert validated[OPT_SLOW_POLL_INTERVAL] == 120
    assert validated[OPT_FAST_WHILE_STREAMING] is False
    assert validated[OPT_API_TIMEOUT] == 25
    assert validated[OPT_NOMINAL_VOLTAGE] == 230
    assert validated[OPT_SESSION_HISTORY_INTERVAL] == 30
    assert validated[CONF_SITE_ONLY] is True


@pytest.mark.asyncio
async def test_options_flow_updates_site_only_in_data(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "site-123", CONF_SITE_ONLY: False},
        options={},
    )
    entry.add_to_hass(hass)

    handler = OptionsFlowHandler(entry)
    handler.hass = hass

    form = await handler.async_step_init()
    user_input = form["data_schema"]({CONF_SITE_ONLY: True})
    result = await handler.async_step_init(user_input)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_SITE_ONLY] is True
    assert result["data"][CONF_SITE_ONLY] is True
