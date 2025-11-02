from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.enphase_ev import DOMAIN, _register_services, async_setup_entry
from custom_components.enphase_ev.const import CONF_SITE_ID
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


@pytest.mark.asyncio
async def test_async_setup_entry_updates_existing_device(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    """Ensure charger devices are refreshed when registry data drifts."""
    site_id = config_entry.data[CONF_SITE_ID]
    device_registry = dr.async_get(hass)

    site_device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="LegacyVendor",
        name="Outdated Site",
        model="Old Model",
    )

    device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL)},
        manufacturer="LegacyVendor",
        name="Legacy Charger",
        model="Legacy Model",
        hw_version="0.1",
        sw_version="0.2",
    )

    class DummyCoordinator:
        def __init__(self) -> None:
            self.serials = {RANDOM_SERIAL}
            self.data = {
                RANDOM_SERIAL: {
                    "display_name": "Garage Charger",
                    "name": "Fallback Name",
                    "model_name": "IQ EVSE",
                    "hw_version": 321,
                    "sw_version": 654,
                    "model_id": "ignored",
                }
            }
            self.site_id = site_id

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return [RANDOM_SERIAL]

    dummy_coord = DummyCoordinator()
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    assert await async_setup_entry(hass, config_entry)
    forward.assert_awaited_once()

    updated = device_registry.async_get_device(identifiers={(DOMAIN, RANDOM_SERIAL)})
    assert updated is not None
    assert updated.name == "Garage Charger"
    assert updated.manufacturer == "Enphase"
    assert updated.model == "Garage Charger (IQ EVSE)"
    assert updated.hw_version == "321"
    assert updated.sw_version == "654"
    assert updated.via_device_id == site_device.id


@pytest.mark.asyncio
async def test_registered_services_cover_branches(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    """Exercise service handlers to cover edge cases in helpers."""
    site_id = config_entry.data[CONF_SITE_ID]
    device_registry = dr.async_get(hass)
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)

    fake_ir_deletes: list[str] = []
    monkeypatch.setattr(
        "custom_components.enphase_ev.ir",
        SimpleNamespace(
            async_delete_issue=lambda hass_, domain, issue_id: fake_ir_deletes.append(
                issue_id
            )
        ),
    )

    class FakeHAService:
        def __init__(self) -> None:
            self.calls = 0

        def async_extract_referenced_device_ids(self, hass_, call):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return ["ref-device"]

    fake_service_helper = FakeHAService()
    monkeypatch.setattr(
        "custom_components.enphase_ev.ha_service", fake_service_helper
    )

    site_device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name="Garage Site",
    )
    first_serial = RANDOM_SERIAL
    second_serial = "EV0002"

    charger_one = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, first_serial)},
        manufacturer="Enphase",
        name="Driveway Charger",
        via_device=(DOMAIN, f"site:{site_id}"),
    )
    charger_two = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={
            (DOMAIN, second_serial),
            (DOMAIN, f"site:{site_id}"),
        },
        manufacturer="Enphase",
        name="Garage Charger B",
    )

    class FakeCoordinator:
        def __init__(self, site, serials, data, start_results):
            self.site_id = site
            self.serials = set(serials)
            self.data = data
            self._start_results = start_results
            self._streaming = False

            async def _start(sn, **_kwargs):
                return self._start_results[sn]

            self.async_start_charging = AsyncMock(side_effect=_start)
            self.async_stop_charging = AsyncMock(return_value=None)
            self.async_trigger_ocpp_message = AsyncMock(
                side_effect=lambda sn, message: {"sent": message, "sn": sn}
                )

            self.client = SimpleNamespace(
                start_live_stream=AsyncMock(return_value=None),
                stop_live_stream=AsyncMock(return_value=None),
            )
            self.async_request_refresh = AsyncMock()

    coord_primary = FakeCoordinator(
        site_id,
        serials={second_serial},
        data={first_serial: {}, second_serial: {}},
        start_results={
            first_serial: {"status": "not_ready"},
            second_serial: {"status": "ok"},
        },
    )
    coord_duplicate = FakeCoordinator(
        site_id,
        serials=set(),
        data={},
        start_results={},
    )
    coord_other = FakeCoordinator(
        "other-site",
        serials={"EV9999"},
        data={"EV9999": {}},
        start_results={"EV9999": {"status": "ok"}},
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].clear()
    hass.data[DOMAIN]["entry-one"] = {"coordinator": coord_primary}
    hass.data[DOMAIN]["entry-two"] = {"coordinator": coord_duplicate}
    hass.data[DOMAIN]["entry-three"] = {"coordinator": coord_other}

    _register_services(hass)

    svc_start = registered[(DOMAIN, "start_charging")]["handler"]
    svc_stop = registered[(DOMAIN, "stop_charging")]["handler"]
    svc_trigger = registered[(DOMAIN, "trigger_message")]["handler"]
    svc_clear = registered[(DOMAIN, "clear_reauth_issue")]["handler"]
    svc_start_stream = registered[(DOMAIN, "start_live_stream")]["handler"]
    svc_stop_stream = registered[(DOMAIN, "stop_live_stream")]["handler"]

    start_call = SimpleNamespace(
        data={
            "device_id": [charger_one.id, site_device.id, charger_two.id],
            "charging_level": 30,
            "connector_id": 2,
        }
    )
    await svc_start(start_call)

    await_args = coord_primary.async_start_charging.await_args_list
    assert call(first_serial, requested_amps=30, connector_id=2) in await_args
    assert call(second_serial, requested_amps=30, connector_id=2) in await_args
    assert coord_primary.async_start_charging.await_count == 2

    stop_call = SimpleNamespace(data={"device_id": charger_one.id})
    await svc_stop(stop_call)
    coord_primary.async_stop_charging.assert_awaited_once_with(first_serial)

    trigger_call = SimpleNamespace(
        data={"device_id": charger_two.id, "requested_message": "status"}
    )
    trigger_result = await svc_trigger(trigger_call)
    assert trigger_result["results"] == [
        {
            "device_id": charger_two.id,
            "serial": second_serial,
            "site_id": site_id,
            "response": {"sent": "status", "sn": second_serial},
        }
    ]
    coord_primary.async_trigger_ocpp_message.assert_awaited_once_with(
        second_serial, "status"
    )

    clear_call = SimpleNamespace(
        data={"device_id": [charger_one.id], "site_id": "explicit-site"}
    )
    await svc_clear(clear_call)
    assert set(fake_ir_deletes) == {
        "reauth_required",
        f"reauth_required_{site_id}",
        "reauth_required_explicit-site",
    }

    await svc_start_stream(SimpleNamespace(data={"site_id": site_id}))
    await svc_start_stream(SimpleNamespace(data={}))
    coord_primary.client.start_live_stream.assert_awaited()
    assert coord_other.client.start_live_stream.await_count == 0
    assert coord_primary._streaming is True

    await svc_stop_stream(SimpleNamespace(data={"site_id": site_id}))
    await svc_stop_stream(SimpleNamespace(data={}))
    coord_primary.client.stop_live_stream.assert_awaited()
    assert coord_other.client.stop_live_stream.await_count == 0
    assert coord_primary._streaming is False

    supports_response = registered[(DOMAIN, "trigger_message")]["kwargs"][
        "supports_response"
    ]
    from custom_components.enphase_ev import SupportsResponse

    assert supports_response is SupportsResponse.OPTIONAL
    assert fake_service_helper.calls >= 3


def test_register_services_supports_response_fallback(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Fallback to SupportsResponse when OPTIONAL is missing."""
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)

    fallback = SimpleNamespace()
    monkeypatch.setattr("custom_components.enphase_ev.SupportsResponse", fallback)

    _register_services(hass)

    assert registered[(DOMAIN, "trigger_message")]["kwargs"]["supports_response"] is fallback
