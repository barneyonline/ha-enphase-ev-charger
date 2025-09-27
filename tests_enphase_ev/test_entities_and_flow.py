import pytest


def test_power_sensor_device_class():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor
    class Dummy:
        data = {}
    ent = EnphasePowerSensor(Dummy(), "4825")
    assert ent.device_class == "power"


@pytest.mark.asyncio
async def test_config_flow_form():
    from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow
    flow = EnphaseEVConfigFlow()
    flow.hass = object()
    res = await flow.async_step_user()
    assert res["type"].name == "FORM"
    assert res["step_id"] == "user"
@pytest.mark.asyncio
async def test_config_flow_login_happy_path(hass, monkeypatch):
    from custom_components.enphase_ev.api import AuthTokens, ChargerInfo, SiteInfo
    from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_EMAIL,
        CONF_PASSWORD,
        CONF_REMEMBER_PASSWORD,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SESSION_ID,
        CONF_SITE_ID,
        CONF_SITE_NAME,
        CONF_TOKEN_EXPIRES_AT,
    )

    async def _mock_auth(session, email, password):
        assert email == "user@example.com"
        assert password == "secret"
        tokens = AuthTokens(
            cookie="jar=1",
            session_id="sid123",
            access_token="token123",
            token_expires_at=1700000000,
        )
        sites = [SiteInfo(site_id="12345", name="Garage Site"), SiteInfo(site_id="67890", name="Other")]
        return tokens, sites

    async def _mock_fetch(session, site_id, tokens):
        assert site_id == "12345"
        assert tokens.access_token == "token123"
        return [ChargerInfo(serial="EV123", name="Driveway Charger")]

    monkeypatch.setattr("custom_components.enphase_ev.config_flow.async_authenticate", _mock_auth)
    monkeypatch.setattr("custom_components.enphase_ev.config_flow.async_fetch_chargers", _mock_fetch)
    monkeypatch.setattr("custom_components.enphase_ev.config_flow.async_get_clientsession", lambda hass: object())

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {}

    result = await flow.async_step_user()
    assert result["type"].name == "FORM"
    assert result["step_id"] == "user"

    result = await flow.async_step_user(
        {
            "email": "user@example.com",
            "password": "secret",
            "remember_password": True,
        }
    )
    assert result["type"].name == "FORM"
    assert result["step_id"] == "site"

    result = await flow.async_step_site({CONF_SITE_ID: "12345"})
    assert result["type"].name == "FORM"
    assert result["step_id"] == "devices"

    result = await flow.async_step_devices({CONF_SERIALS: ["EV123"], CONF_SCAN_INTERVAL: 20})
    assert result["type"].name == "CREATE_ENTRY"
    data = result["data"]
    assert data[CONF_EMAIL] == "user@example.com"
    assert data[CONF_REMEMBER_PASSWORD] is True
    assert data[CONF_PASSWORD] == "secret"
    assert data[CONF_SITE_ID] == "12345"
    assert data[CONF_SITE_NAME] == "Garage Site"
    assert data[CONF_SERIALS] == ["EV123"]
    assert data[CONF_SCAN_INTERVAL] == 20
    assert data[CONF_COOKIE] == "jar=1"
    assert data[CONF_EAUTH] == "token123"
    assert data[CONF_SESSION_ID] == "sid123"
    assert data[CONF_TOKEN_EXPIRES_AT] == 1700000000
