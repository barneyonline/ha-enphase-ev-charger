from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

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
    DOMAIN,
)

from tests.components.enphase_ev.random_ids import RANDOM_SITE_ID


def test_power_sensor_device_class() -> None:
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    class DummyCoordinator:
        data = {}

    entity = EnphasePowerSensor(DummyCoordinator(), "4825")
    assert entity.device_class == "power"


@pytest.mark.asyncio
async def test_config_flow_happy_path(hass: HomeAssistant) -> None:
    from custom_components.enphase_ev.api import AuthTokens, ChargerInfo, SiteInfo
    from custom_components.enphase_ev.config_flow import EnphaseEVConfigFlow

    tokens = AuthTokens(
        cookie="jar=1",
        session_id="sid123",
        access_token="token123",
        token_expires_at=1_700_000_000,
    )
    sites = [
        SiteInfo(site_id="12345", name="Garage Site"),
        SiteInfo(site_id="67890", name="Backup Site"),
    ]
    chargers = [ChargerInfo(serial="EV123", name="Driveway Charger")]

    flow = EnphaseEVConfigFlow()
    flow.hass = hass
    flow.context = {}

    with (
        patch(
            "custom_components.enphase_ev.config_flow.async_authenticate",
            AsyncMock(return_value=(tokens, sites)),
        ) as mock_auth,
        patch(
            "custom_components.enphase_ev.config_flow.async_fetch_chargers",
            AsyncMock(return_value=chargers),
        ) as mock_chargers,
        patch(
            "custom_components.enphase_ev.config_flow.async_get_clientsession",
            MagicMock(),
        ),
    ):
        result = await flow.async_step_user()
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await flow.async_step_user(
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_REMEMBER_PASSWORD: True,
            }
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "site"

        result = await flow.async_step_site({CONF_SITE_ID: "12345"})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "devices"

        result = await flow.async_step_devices(
            {CONF_SERIALS: ["EV123"], CONF_SCAN_INTERVAL: 20}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
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
    assert data[CONF_TOKEN_EXPIRES_AT] == 1_700_000_000
    mock_auth.assert_awaited_once()
    mock_chargers.assert_awaited_once()



@pytest.mark.asyncio
async def test_integration_setup_creates_entities(monkeypatch, config_entry, load_fixture) -> None:
    from custom_components.enphase_ev import async_setup_entry, PLATFORMS

    status_payload = load_fixture("status_charging.json")
    summary_payload = [
        {"siteId": RANDOM_SITE_ID, "siteName": "Garage Site", "totalPower": 7.5}
    ]

    client = AsyncMock()
    client.status.return_value = status_payload
    client.summary_v2.return_value = summary_payload
    client.session_history.return_value = {"data": {"result": []}}

    forwarded: list[list[str]] = []
    registered_devices: list[dict] = []

    class DummyDeviceRegistry:
        def async_get_or_create(self, **kwargs):
            registered_devices.append(kwargs)
            return SimpleNamespace(id="site-device")

        def async_get_device(self, identifiers=None):
            if identifiers and (DOMAIN, f"site:{RANDOM_SITE_ID}") in identifiers:
                return SimpleNamespace(id="site-device")
            return None

    device_registry = DummyDeviceRegistry()
    monkeypatch.setattr(dr, "async_get", lambda hass: device_registry)

    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.async_get_clientsession",
        lambda hass: object(),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.ir.async_create_issue",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.ir.async_delete_issue",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator.async_config_entry_first_refresh",
        AsyncMock(return_value=None),
    )

    async def forward_entry(entry, platforms):
        forwarded.append(list(platforms))

    hass = SimpleNamespace(
        data={},
        services=SimpleNamespace(async_register=lambda *args, **kwargs: None),
        bus=SimpleNamespace(async_listen_once=lambda *args, **kwargs: None),
        config_entries=SimpleNamespace(
            async_forward_entry_setups=forward_entry,
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )

    with patch(
        "custom_components.enphase_ev.coordinator.EnphaseEVClient",
        return_value=client,
    ):
        assert await async_setup_entry(hass, config_entry)

    assert forwarded == [PLATFORMS]
    assert registered_devices
    site_device = next(
        (
            dev
            for dev in registered_devices
            if dev.get("identifiers") == {(DOMAIN, f"site:{RANDOM_SITE_ID}")}
        ),
        None,
    )
    assert site_device is not None
    assert hass.data[DOMAIN][config_entry.entry_id]["coordinator"] is not None
