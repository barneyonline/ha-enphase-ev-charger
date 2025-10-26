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

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


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


@pytest.mark.asyncio
async def test_sensor_platform_discovers_new_serial(hass, config_entry) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    class DummyCoordinator:
        def __init__(self) -> None:
            self.data: dict[str, dict] = {}
            self.site_id = RANDOM_SITE_ID
            self.last_success_utc = None
            self.latency_ms = None
            self.last_failure_status = None
            self.last_failure_source = None
            self.last_failure_description = None
            self.last_failure_response = None
            self.last_failure_utc = None
            self.backoff_ends_utc = None
            self.last_update_success = True
            self.serials: set[str] = set()
            self._listeners: list = []

        def iter_serials(self) -> list[str]:
            ordered = list(self.serials)
            ordered.extend(sn for sn in self.data.keys() if sn not in self.serials)
            return [sn for sn in dict.fromkeys(ordered) if sn]

        def async_add_listener(self, callback):
            self._listeners.append(callback)

            def _remove():
                try:
                    self._listeners.remove(callback)
                except ValueError:
                    pass

            return _remove

        def async_set_updated_data(self, data: dict[str, dict]) -> None:
            self.data = data
            self.serials.update(str(sn) for sn in data.keys())
            for callback in list(self._listeners):
                callback()

        async def async_request_refresh(self):
            return None

    coord = DummyCoordinator()
    initial_sn = RANDOM_SERIAL
    coord.serials.add(initial_sn)
    coord.data = {
        initial_sn: {
            "sn": initial_sn,
            "name": "Garage EV",
            "display_name": "Garage EV",
            "connector_status": "AVAILABLE",
        }
    }

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][config_entry.entry_id] = {"coordinator": coord}

    captures: list[list[str]] = []

    def _capture(entities, update_before_add=False):
        captures.append([entity.unique_id for entity in entities])

    await async_setup_entry(hass, config_entry, _capture)

    assert captures  # Site-level sensors added
    # Expect one site-level batch plus one per-serial batch
    assert len(captures) == 2
    assert any(initial_sn in uid for uid in captures[1])

    new_sn = "NEW987654321"
    coord.async_set_updated_data(
        {
            initial_sn: coord.data[initial_sn],
            new_sn: {
                "sn": new_sn,
                "name": "Workshop EV",
                "display_name": "Workshop EV",
                "connector_status": "AVAILABLE",
            },
        }
    )

    # A new batch should be appended for the new charger entities
    assert len(captures) == 3
    assert all(new_sn in uid for uid in captures[-1])
