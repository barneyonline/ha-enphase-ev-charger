from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.enphase_ev import (
    DOMAIN,
    _async_update_listener,
    _entries_for_device,
    _is_owned_entity,
    _iter_entity_registry_entries,
    _migrate_legacy_gateway_type_devices,
    _remove_legacy_inventory_entities,
    _sync_charger_devices,
    _sync_type_devices,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.enphase_ev.const import CONF_SITE_ID
from custom_components.enphase_ev.device_types import type_identifier
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.services import async_setup_services
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
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return [RANDOM_SERIAL]

        def iter_type_keys(self) -> list[str]:
            return ["envoy", "iqevse"]

        def type_identifier(self, type_key: str):
            return type_identifier(self.site_id, type_key)

        def type_label(self, type_key: str) -> str:
            if type_key == "envoy":
                return "Gateway"
            return "EV Chargers"

        def type_device_name(self, type_key: str) -> str:
            if type_key == "envoy":
                return "Gateway (1)"
            return "EV Chargers (1)"

    dummy_coord = DummyCoordinator()
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    assert await async_setup_entry(hass, config_entry)
    dummy_coord.schedule_sync.async_start.assert_awaited_once()
    forward.assert_awaited_once()

    updated = device_registry.async_get_device(identifiers={(DOMAIN, RANDOM_SERIAL)})
    assert updated is not None
    assert updated.name == "Garage Charger"
    assert updated.manufacturer == "Enphase"
    assert updated.model == "Garage Charger (IQ EVSE)"
    assert updated.hw_version == "321"
    assert updated.sw_version == "654"
    ev_type_device = device_registry.async_get_device(
        identifiers={(DOMAIN, f"type:{site_id}:iqevse")}
    )
    assert ev_type_device is not None
    assert updated.via_device_id == ev_type_device.id
    assert updated.via_device_id != site_device.id


@pytest.mark.asyncio
async def test_async_setup_entry_model_display_variants(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    """Ensure model metadata covers display-only and model-only chargers."""
    device_registry = dr.async_get(hass)
    device_registry.async_clear_config_entry(config_entry.entry_id)
    hass.data.pop(DOMAIN, None)

    site_id = config_entry.data[CONF_SITE_ID]

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.serials = {"MODEL_ONLY", "DISPLAY_ONLY"}
            self.data = {
                "MODEL_ONLY": {
                    "model_name": "IQ EVSE",
                },
                "DISPLAY_ONLY": {
                    "display_name": "Workshop Charger",
                },
            }

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return ["MODEL_ONLY", "DISPLAY_ONLY"]

        def iter_type_keys(self) -> list[str]:
            return ["iqevse"]

        def type_identifier(self, type_key: str):
            return type_identifier(self.site_id, type_key)

        def type_label(self, _type_key: str) -> str:
            return "EV Chargers"

        def type_device_name(self, _type_key: str) -> str:
            return "EV Chargers (2)"

    dummy_coord = DummyCoordinator()
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    assert await async_setup_entry(hass, config_entry)

    model_device = device_registry.async_get_device(identifiers={(DOMAIN, "MODEL_ONLY")})
    display_device = device_registry.async_get_device(
        identifiers={(DOMAIN, "DISPLAY_ONLY")}
    )

    assert model_device is not None
    assert model_device.model == "IQ EVSE"
    assert display_device is not None
    assert display_device.model == "Workshop Charger"


@pytest.mark.asyncio
async def test_async_setup_entry_uses_fallback_name_for_model(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    device_registry = dr.async_get(hass)
    device_registry.async_clear_config_entry(config_entry.entry_id)
    hass.data.pop(DOMAIN, None)

    site_id = config_entry.data[CONF_SITE_ID]

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.serials = {"FALLBACK_ONLY"}
            self.data = {
                "FALLBACK_ONLY": {
                    "name": "Fallback Charger",
                },
            }

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return ["FALLBACK_ONLY"]

        def iter_type_keys(self) -> list[str]:
            return ["iqevse"]

        def type_identifier(self, type_key: str):
            return type_identifier(self.site_id, type_key)

        def type_label(self, _type_key: str) -> str:
            return "EV Chargers"

        def type_device_name(self, _type_key: str) -> str:
            return "EV Chargers (1)"

    dummy_coord = DummyCoordinator()
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    assert await async_setup_entry(hass, config_entry)

    device = device_registry.async_get_device(identifiers={(DOMAIN, "FALLBACK_ONLY")})
    assert device is not None
    assert device.model == "Fallback Charger"


@pytest.mark.asyncio
async def test_async_setup_entry_registry_sync_listener_handles_exceptions(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    listeners: list = []

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.serials = {RANDOM_SERIAL}
            self.data = {RANDOM_SERIAL: {"name": "Fallback Charger"}}
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return [RANDOM_SERIAL]

        def iter_type_keys(self) -> list[str]:
            return ["iqevse"]

        def type_identifier(self, type_key: str):
            return type_identifier(self.site_id, type_key)

        def type_label(self, _type_key: str) -> str:
            return "EV Chargers"

        def type_device_name(self, _type_key: str) -> str:
            return "EV Chargers (1)"

        def async_add_listener(self, callback):
            listeners.append(callback)
            return lambda: None

    dummy_coord = DummyCoordinator()
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    assert await async_setup_entry(hass, config_entry)
    assert listeners, "expected setup to register a coordinator listener"

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("custom_components.enphase_ev._sync_registry_devices", _boom)
    listeners[0]()  # should swallow and log internal sync exceptions


@pytest.mark.asyncio
async def test_async_unload_entry_stops_schedule_sync(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    schedule_sync = SimpleNamespace(async_stop=AsyncMock())
    coord = SimpleNamespace(schedule_sync=schedule_sync)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    unload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)

    assert await async_unload_entry(hass, config_entry)
    schedule_sync.async_stop.assert_awaited_once()
    unload.assert_awaited_once()
    assert config_entry.runtime_data is None


@pytest.mark.asyncio
async def test_async_unload_entry_handles_missing_runtime_data(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    unload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)

    assert await async_unload_entry(hass, config_entry)
    unload.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_listener_reloads_entry(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    reload = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)

    await _async_update_listener(hass, config_entry)

    reload.assert_awaited_once_with(config_entry.entry_id)


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
        "custom_components.enphase_ev.services.ir",
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
        "custom_components.enphase_ev.services.ha_service", fake_service_helper
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
    lonely_device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "EV4040")},
        manufacturer="Enphase",
        name="Lonely Charger",
    )
    other_site_device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "site:other-site")},
        manufacturer="Enphase",
        name="Other Site",
    )

    class FakeCoordinator:
        def __init__(self, site, serials, data, start_results):
            self.site_id = site
            self.serials = set(serials)
            self.data = data
            self._start_results = start_results
            self._streaming = False
            self.schedule_sync = SimpleNamespace(async_refresh=AsyncMock())

            async def _start(sn, **_kwargs):
                return self._start_results[sn]

            self.async_start_charging = AsyncMock(side_effect=_start)
            self.async_stop_charging = AsyncMock(return_value=None)
            self.async_trigger_ocpp_message = AsyncMock(
                side_effect=lambda sn, message: {"sent": message, "sn": sn}
                )
            async def _start_streaming(*_args, **_kwargs):
                self._streaming = True
                return None

            async def _stop_streaming(*_args, **_kwargs):
                self._streaming = False
                return None

            self.async_start_streaming = AsyncMock(side_effect=_start_streaming)
            self.async_stop_streaming = AsyncMock(side_effect=_stop_streaming)
            self.async_request_grid_toggle_otp = AsyncMock(return_value=None)
            self.async_set_grid_mode = AsyncMock(return_value=None)

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
        serials={"unused"},
        data={},
        start_results={},
    )
    coord_other = FakeCoordinator(
        "other-site",
        serials={"EV9999"},
        data={"EV9999": {}},
        start_results={"EV9999": {"status": "ok"}},
    )

    entry_one = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: site_id},
        title="Primary Site",
        unique_id="entry-one",
    )
    entry_one.add_to_hass(hass)
    entry_one.runtime_data = EnphaseRuntimeData(coordinator=coord_primary)

    entry_two = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: site_id},
        title="Duplicate Site",
        unique_id="entry-two",
    )
    entry_two.add_to_hass(hass)
    entry_two.runtime_data = EnphaseRuntimeData(coordinator=coord_duplicate)

    entry_three = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "other-site"},
        title="Other Site",
        unique_id="entry-three",
    )
    entry_three.add_to_hass(hass)
    entry_three.runtime_data = EnphaseRuntimeData(coordinator=coord_other)

    async_setup_services(hass)

    svc_start = registered[(DOMAIN, "start_charging")]["handler"]
    svc_stop = registered[(DOMAIN, "stop_charging")]["handler"]
    svc_trigger = registered[(DOMAIN, "trigger_message")]["handler"]
    svc_clear = registered[(DOMAIN, "clear_reauth_issue")]["handler"]
    svc_start_stream = registered[(DOMAIN, "start_live_stream")]["handler"]
    svc_stop_stream = registered[(DOMAIN, "stop_live_stream")]["handler"]
    svc_sync = registered[(DOMAIN, "sync_schedules")]["handler"]
    svc_request_grid_otp = registered[(DOMAIN, "request_grid_toggle_otp")]["handler"]
    svc_set_grid_mode = registered[(DOMAIN, "set_grid_mode")]["handler"]

    await svc_start(SimpleNamespace(data={}))
    await svc_stop(SimpleNamespace(data={}))


    fake_service_helper.calls = 0
    assert await svc_trigger(SimpleNamespace(data={})) == {}

    await svc_start(SimpleNamespace(data={"device_id": [lonely_device.id]}))
    await svc_stop(SimpleNamespace(data={"device_id": lonely_device.id}))
    empty_trigger = await svc_trigger(
        SimpleNamespace(
            data={"device_id": lonely_device.id, "requested_message": "status"}
        )
    )
    assert empty_trigger == {"results": []}

    await svc_sync(
        SimpleNamespace(
            data={"device_id": [charger_two.id, site_device.id, lonely_device.id]}
        )
    )
    assert call(reason="service", serials=[second_serial]) in (
        coord_primary.schedule_sync.async_refresh.await_args_list
    )
    await svc_request_grid_otp(SimpleNamespace(data={"site_id": site_id}))
    coord_primary.async_request_grid_toggle_otp.assert_awaited_once()
    coord_primary.async_request_refresh.assert_awaited()

    await svc_set_grid_mode(
        SimpleNamespace(data={"site_id": site_id, "mode": "off_grid", "otp": "1234"})
    )
    coord_primary.async_set_grid_mode.assert_awaited_once_with("off_grid", "1234")

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
    await svc_start_stream(SimpleNamespace(data={"device_id": [charger_one.id]}))
    await svc_start_stream(SimpleNamespace(data={}))
    coord_primary.async_start_streaming.assert_awaited()
    assert coord_other.async_start_streaming.await_count == 0
    assert coord_primary._streaming is True

    await svc_stop_stream(SimpleNamespace(data={"site_id": site_id}))
    await svc_stop_stream(SimpleNamespace(data={"device_id": [charger_one.id]}))
    await svc_stop_stream(SimpleNamespace(data={}))
    coord_primary.async_stop_streaming.assert_awaited()
    assert coord_other.async_stop_streaming.await_count == 0
    assert coord_primary._streaming is False

    fake_service_helper.calls = 0
    await svc_sync(SimpleNamespace(data={}))
    assert coord_primary.schedule_sync.async_refresh.await_count >= 2

    entry_one.runtime_data = None
    entry_two.runtime_data = None
    entry_three.runtime_data = None
    await svc_start_stream(SimpleNamespace(data={"site_id": "missing"}))
    await svc_stop_stream(SimpleNamespace(data={"site_id": "missing"}))

    supports_response = registered[(DOMAIN, "trigger_message")]["kwargs"][
        "supports_response"
    ]
    from custom_components.enphase_ev.services import SupportsResponse

    assert supports_response is SupportsResponse.OPTIONAL
    assert fake_service_helper.calls >= 3

    from custom_components.enphase_ev.coordinator import ServiceValidationError

    with pytest.raises(ServiceValidationError):
        await svc_request_grid_otp(SimpleNamespace(data={}))
    with pytest.raises(ServiceValidationError):
        await svc_set_grid_mode(
            SimpleNamespace(
                data={
                    "device_id": [charger_one.id, other_site_device.id],
                    "mode": "on_grid",
                    "otp": "1234",
                }
            )
        )
    with pytest.raises(ServiceValidationError):
        await svc_request_grid_otp(SimpleNamespace(data={"site_id": "missing-site"}))


def test_register_services_supports_response_fallback(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Service setup should honor an explicit supports_response fallback."""
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)

    fallback = SimpleNamespace()
    async_setup_services(hass, supports_response=fallback)

    assert registered[(DOMAIN, "trigger_message")]["kwargs"]["supports_response"] is fallback


def test_init_module_importable() -> None:
    import importlib

    module = importlib.import_module("custom_components.enphase_ev.__init__")
    assert module.DOMAIN == DOMAIN


@pytest.mark.asyncio
async def test_service_helper_resolve_functions_cover_none_branches(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    """Ensure resolve helpers handle missing identifiers gracefully."""
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)

    async_setup_services(hass)

    svc_start = registered[(DOMAIN, "start_charging")]["handler"]
    svc_stop = registered[(DOMAIN, "stop_charging")]["handler"]
    svc_clear = registered[(DOMAIN, "clear_reauth_issue")]["handler"]

    def _extract_helper(func, target):
        for cell in func.__closure__ or ():
            value = cell.cell_contents
            if callable(value) and getattr(value, "__name__", "") == target:
                return value
        raise AssertionError(f"helper {target} not found")

    resolve_sn = _extract_helper(svc_start, "_resolve_sn")
    resolve_site = _extract_helper(svc_clear, "_resolve_site_id")

    dev_reg = dr.async_get(hass)
    missing_sn = await resolve_sn("does-not-exist")
    assert missing_sn is None

    site_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "site:ABC123")},
        manufacturer="Enphase",
        name="Site Device",
    )
    assert await resolve_sn(site_device.id) is None
    assert await resolve_site(site_device.id) == "ABC123"

    child_no_parent = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("other", "value")},
        manufacturer="Vendor",
        name="Third Party Device",
    )
    assert await resolve_sn(child_no_parent.id) is None
    assert await resolve_site(child_no_parent.id) is None

    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "site:PARENT")},
        manufacturer="Enphase",
        name="Parent Site",
    )
    child_with_via = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "EVCHILD")},
        manufacturer="Enphase",
        name="Child Device",
        via_device=(DOMAIN, "site:PARENT"),
    )
    assert await resolve_site(child_with_via.id) == "PARENT"

    type_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "type:TYPED:envoy")},
        manufacturer="Enphase",
        name="Gateway (1)",
    )
    assert await resolve_sn(type_device.id) is None
    assert await resolve_site(type_device.id) == "TYPED"

    child_with_type_parent = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "EVTYPED")},
        manufacturer="Enphase",
        name="Typed Child",
        via_device=(DOMAIN, "type:TYPED:envoy"),
    )
    assert await resolve_site(child_with_type_parent.id) == "TYPED"

    await svc_stop(SimpleNamespace(data={}))


def test_init_module_reload_executes_module_code() -> None:
    module = importlib.import_module("custom_components.enphase_ev")
    assert importlib.reload(module).DOMAIN == DOMAIN


class _FakeDevice(SimpleNamespace):
    pass


class _FakeDeviceRegistry:
    def __init__(self) -> None:
        self._devices: dict[tuple[str, str], _FakeDevice] = {}
        self._next_id = 1

    def async_get_device(self, *, identifiers):
        ident = next(iter(identifiers))
        return self._devices.get(ident)

    def async_get_or_create(self, **kwargs):
        ident = next(iter(kwargs["identifiers"]))
        existing = self._devices.get(ident)
        via_device_id = None
        via = kwargs.get("via_device")
        if via is not None:
            parent = self._devices.get(via)
            via_device_id = parent.id if parent else None
        if existing is None:
            existing = _FakeDevice(
                id=f"dev-{self._next_id}",
                identifiers={ident},
            )
            self._next_id += 1
            self._devices[ident] = existing
        existing.name = kwargs.get("name")
        existing.manufacturer = kwargs.get("manufacturer")
        existing.model = kwargs.get("model")
        existing.hw_version = kwargs.get("hw_version")
        existing.sw_version = kwargs.get("sw_version")
        existing.via_device_id = via_device_id
        return existing


def test_sync_type_devices_skips_invalid_and_updates_existing(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    existing = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="LegacyVendor",
        name="Old Gateway",
        model="Old",
    )

    coord = SimpleNamespace(
        iter_type_keys=lambda: ["invalid", "empty", "envoy"],
        type_identifier=lambda key: (
            None
            if key == "invalid"
            else (DOMAIN, f"type:{site_id}:{key}")
        ),
        type_label=lambda key: "" if key == "empty" else "Gateway",
        type_device_name=lambda key: "" if key == "empty" else "Gateway (1)",
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)

    assert "envoy" in type_devices
    updated = type_devices["envoy"]
    assert updated.id == existing.id
    assert updated.manufacturer == "Enphase"
    assert updated.name == "Gateway (1)"
    assert updated.model == "Gateway"


def test_sync_type_devices_deduplicates_merged_identifiers(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    coord = SimpleNamespace(
        iter_type_keys=lambda: ["envoy", "meter", "enpower"],
        type_identifier=lambda _key: (DOMAIN, f"type:{site_id}:envoy"),
        type_label=lambda _key: "Gateway",
        type_device_name=lambda _key: "Gateway (1)",
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)

    assert set(type_devices) == {"envoy", "meter", "enpower"}
    assert len({type_devices[key].id for key in type_devices}) == 1
    assert len(dev_reg._devices) == 1


def test_sync_type_devices_uses_model_and_hw_summary(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()

    coord = SimpleNamespace(
        iter_type_keys=lambda: ["microinverter"],
        type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
        type_label=lambda key: "Microinverters",
        type_device_name=lambda key: "Microinverters (16)",
        type_device_model=lambda key: "IQ7A x16",
        type_device_hw_version=lambda key: "Normal 16 | Warning 0 | Error 0 | Not Reporting 0",
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)
    device = type_devices["microinverter"]
    assert device.model == "IQ7A x16"
    assert device.hw_version.startswith("Normal 16")


def test_sync_type_devices_updates_existing_hw_summary(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:microinverter")},
        manufacturer="Enphase",
        name="Microinverters (16)",
        model="IQ7A x16",
        hw_version="Normal 15 | Warning 1 | Error 0 | Not Reporting 0",
    )

    coord = SimpleNamespace(
        iter_type_keys=lambda: ["microinverter"],
        type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
        type_label=lambda key: "Microinverters",
        type_device_name=lambda key: "Microinverters (16)",
        type_device_model=lambda key: "IQ7A x16",
        type_device_hw_version=lambda key: "Normal 16 | Warning 0 | Error 0 | Not Reporting 0",
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)
    device = type_devices["microinverter"]
    assert device.hw_version.startswith("Normal 16")


def test_sync_charger_devices_resolves_parent_from_registry_when_missing(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    parent = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:iqevse")},
        manufacturer="Enphase",
        name="EV Chargers (1)",
        model="EV Chargers",
    )

    coord = SimpleNamespace(
        type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
        iter_serials=lambda: [RANDOM_SERIAL],
        data={
            RANDOM_SERIAL: {
                "display_name": "Garage Charger",
                "model_name": "IQ EVSE",
                "hw_version": "1.0",
                "sw_version": "2.0",
            }
        },
    )

    _sync_charger_devices(config_entry, coord, dev_reg, site_id, type_devices={})
    charger = dev_reg.async_get_device(identifiers={(DOMAIN, RANDOM_SERIAL)})
    assert charger is not None
    assert charger.via_device_id == parent.id


def test_iter_entity_registry_entries_handles_edge_shapes() -> None:
    assert _iter_entity_registry_entries(SimpleNamespace()) == []

    class _ValuesRaises:
        def values(self):
            raise RuntimeError("boom")

    class _DictNoCallableValues(dict):
        values = []  # type: ignore[assignment]

    assert _iter_entity_registry_entries(SimpleNamespace(entities=_ValuesRaises())) == []
    assert _iter_entity_registry_entries(SimpleNamespace(entities={"x": 1})) == [1]
    assert _iter_entity_registry_entries(SimpleNamespace(entities=_DictNoCallableValues(x=1))) == [1]
    assert _iter_entity_registry_entries(SimpleNamespace(entities=["bad"])) == []


def test_entries_for_device_falls_back_when_helper_errors(monkeypatch) -> None:
    reg_entries = {
        "sensor.a": SimpleNamespace(device_id="dev-1"),
        "sensor.b": SimpleNamespace(device_id="dev-2"),
    }
    ent_reg = SimpleNamespace(entities=reg_entries)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("custom_components.enphase_ev.er.async_entries_for_device", _boom)
    entries = _entries_for_device(ent_reg, "dev-1")
    assert len(entries) == 1
    assert entries[0].device_id == "dev-1"


def test_is_owned_entity_checks_platform_and_config_entry() -> None:
    assert _is_owned_entity(SimpleNamespace(platform=DOMAIN, config_entry_id="a"), "a")
    assert not _is_owned_entity(SimpleNamespace(platform="other", config_entry_id="a"), "a")
    assert not _is_owned_entity(SimpleNamespace(platform=DOMAIN, config_entry_id="b"), "a")


def test_remove_legacy_inventory_entities_handles_missing_entity_and_remove_errors() -> None:
    site_id = "SITE-123"
    ent_reg = SimpleNamespace(
        entities={
            "sensor.missing_id": SimpleNamespace(
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id=f"{DOMAIN}_site_{site_id}_type_meter_inventory",
                entity_id=None,
            ),
            "sensor.remove_error": SimpleNamespace(
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id=f"{DOMAIN}_site_{site_id}_type_envoy_inventory",
                entity_id="sensor.remove_error",
            ),
        },
        async_remove=lambda _entity_id: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    removed = _remove_legacy_inventory_entities(ent_reg, site_id, entry_id="entry-1")
    assert removed == 0


@pytest.mark.asyncio
async def test_migrate_legacy_gateway_type_devices_rehomes_entities_and_prunes(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    gateway = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway (3)",
    )
    meter = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:meter")},
        manufacturer="Enphase",
        name="Meter (1)",
    )
    enpower = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:enpower")},
        manufacturer="Enphase",
        name="System Controller (1)",
    )
    site_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name=f"Enphase Site {site_id}",
    )

    meter_inventory = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_type_meter_inventory",
        device_id=meter.id,
        config_entry=config_entry,
    )
    gateway_inventory = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_type_envoy_inventory",
        device_id=gateway.id,
        config_entry=config_entry,
    )
    enpower_inventory = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_type_enpower_inventory",
        device_id=enpower.id,
        config_entry=config_entry,
    )
    legacy_metric = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_legacy_metric",
        device_id=enpower.id,
        config_entry=config_entry,
    )
    site_metric = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_legacy_site_metric",
        device_id=site_device.id,
        config_entry=config_entry,
    )

    coord = SimpleNamespace(
        type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
    )

    _migrate_legacy_gateway_type_devices(hass, config_entry, coord, dev_reg, site_id)

    assert ent_reg.async_get(meter_inventory.entity_id) is None
    assert ent_reg.async_get(gateway_inventory.entity_id) is None
    moved_enpower = ent_reg.async_get(enpower_inventory.entity_id)
    assert moved_enpower is not None
    assert moved_enpower.device_id == gateway.id
    moved_entry = ent_reg.async_get(legacy_metric.entity_id)
    assert moved_entry is not None
    assert moved_entry.device_id == gateway.id
    moved_site_entry = ent_reg.async_get(site_metric.entity_id)
    assert moved_site_entry is not None
    assert moved_site_entry.device_id == gateway.id

    remove_device = getattr(dev_reg, "async_remove_device", None)
    if callable(remove_device):
        assert dev_reg.async_get(meter.id) is None
        assert dev_reg.async_get(enpower.id) is None
        assert dev_reg.async_get(site_device.id) is None


def test_migrate_legacy_gateway_type_devices_handles_internal_edge_paths(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    module = importlib.import_module("custom_components.enphase_ev")

    # Cover guard path when entity registry helper is unavailable.
    original_er = module.er
    monkeypatch.setattr(module, "er", None)
    _migrate_legacy_gateway_type_devices(
        hass,
        config_entry,
        SimpleNamespace(type_identifier=lambda _key: (DOMAIN, "type:x:envoy")),
        SimpleNamespace(async_get_device=lambda **_kwargs: None),
        "x",
    )
    monkeypatch.setattr(module, "er", original_er)

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    # Cover str(site_id) failure and blank-site early return.
    _migrate_legacy_gateway_type_devices(
        hass,
        config_entry,
        SimpleNamespace(type_identifier=lambda _key: (DOMAIN, "type:x:envoy")),
        SimpleNamespace(async_get_device=lambda **_kwargs: None),
        BadStr(),
    )
    _migrate_legacy_gateway_type_devices(
        hass,
        config_entry,
        SimpleNamespace(type_identifier=lambda _key: (DOMAIN, "type:x:envoy")),
        SimpleNamespace(async_get_device=lambda **_kwargs: None),
        "   ",
    )

    # Cover gateway-without-id early return.
    _migrate_legacy_gateway_type_devices(
        hass,
        config_entry,
        SimpleNamespace(type_identifier=lambda _key: (DOMAIN, "type:x:envoy")),
        SimpleNamespace(async_get_device=lambda **_kwargs: SimpleNamespace(id=None)),
        "x",
    )

    # Cover entity registry acquisition failure.
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _migrate_legacy_gateway_type_devices(
        hass,
        config_entry,
        SimpleNamespace(type_identifier=lambda _key: (DOMAIN, "type:x:envoy")),
        SimpleNamespace(
            async_get_device=lambda **kwargs: (
                SimpleNamespace(id="gw")
                if next(iter(kwargs["identifiers"])) == (DOMAIN, "type:x:envoy")
                else None
            )
        ),
        "x",
    )

    # Cover site_id fallback, legacy device without id, missing entity_id branch,
    # and update-entity failure branch.
    entries = [
        SimpleNamespace(platform=DOMAIN, config_entry_id=config_entry.entry_id, entity_id=None),
        SimpleNamespace(
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
            entity_id="sensor.fail_move",
        ),
    ]
    ent_reg = SimpleNamespace(
        entities={f"e{idx}": entry for idx, entry in enumerate(entries)},
        async_remove=lambda _entity_id: None,
        async_update_entity=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("move failed")
        ),
    )
    monkeypatch.setattr("custom_components.enphase_ev.er.async_get", lambda _hass: ent_reg)
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_entries_for_device",
        lambda _reg, _device_id: entries,
    )
    dev_reg = SimpleNamespace(
        async_get_device=lambda **kwargs: {
            (DOMAIN, "type:site-fallback:envoy"): SimpleNamespace(id="gw"),
            (DOMAIN, "type:site-fallback:meter"): SimpleNamespace(id="legacy-meter"),
            (DOMAIN, "type:site-fallback:enpower"): SimpleNamespace(id=None),
            (DOMAIN, "site:site-fallback"): SimpleNamespace(id=None),
        }.get(next(iter(kwargs["identifiers"]))),
        async_remove_device=lambda _device_id: None,
    )
    coord = SimpleNamespace(
        site_id="site-fallback",
        type_identifier=lambda key: (DOMAIN, f"type:site-fallback:{key}"),
    )

    _migrate_legacy_gateway_type_devices(hass, config_entry, coord, dev_reg, None)

    dev_reg_site_update = SimpleNamespace(
        async_get_device=lambda **kwargs: {
            (DOMAIN, "type:site-fallback:envoy"): SimpleNamespace(id="gw"),
            (DOMAIN, "site:site-fallback"): SimpleNamespace(id="legacy-site"),
        }.get(next(iter(kwargs["identifiers"]))),
        async_remove_device=lambda _device_id: None,
    )
    _migrate_legacy_gateway_type_devices(
        hass, config_entry, coord, dev_reg_site_update, None
    )


@pytest.mark.asyncio
async def test_migrate_legacy_gateway_type_devices_skips_without_gateway(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    meter = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:meter")},
        manufacturer="Enphase",
        name="Meter (1)",
    )
    legacy = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_legacy_metric_no_gateway",
        device_id=meter.id,
        config_entry=config_entry,
    )
    coord = SimpleNamespace(
        type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
    )

    _migrate_legacy_gateway_type_devices(hass, config_entry, coord, dev_reg, site_id)

    assert ent_reg.async_get(legacy.entity_id) is not None
    assert ent_reg.async_get(legacy.entity_id).device_id == meter.id


@pytest.mark.asyncio
async def test_migrate_legacy_gateway_type_devices_keeps_unowned_entities(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    gateway = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway (1)",
    )
    meter = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:meter")},
        manufacturer="Enphase",
        name="Meter (1)",
    )
    site_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name=f"Enphase Site {site_id}",
    )

    owned = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_owned_metric",
        device_id=meter.id,
        config_entry=config_entry,
    )
    other_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "other-site"},
        title="Other",
        unique_id="other-entry",
    )
    other_entry.add_to_hass(hass)
    foreign = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_foreign_metric",
        device_id=meter.id,
        config_entry=other_entry,
    )
    foreign_inventory = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_type_meter_inventory",
        device_id=meter.id,
        config_entry=other_entry,
    )
    foreign_site_entity = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_foreign_site_metric",
        device_id=site_device.id,
        config_entry=other_entry,
    )

    coord = SimpleNamespace(
        type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
    )
    _migrate_legacy_gateway_type_devices(hass, config_entry, coord, dev_reg, site_id)

    owned_entry = ent_reg.async_get(owned.entity_id)
    assert owned_entry is not None
    assert owned_entry.device_id == gateway.id
    foreign_entry = ent_reg.async_get(foreign.entity_id)
    assert foreign_entry is not None
    assert foreign_entry.device_id == meter.id
    foreign_inventory_entry = ent_reg.async_get(foreign_inventory.entity_id)
    assert foreign_inventory_entry is not None
    assert foreign_inventory_entry.device_id == meter.id
    foreign_site_entry = ent_reg.async_get(foreign_site_entity.entity_id)
    assert foreign_site_entry is not None
    assert foreign_site_entry.device_id == site_device.id
    assert dev_reg.async_get(meter.id) is not None
    assert dev_reg.async_get(site_device.id) is not None


@pytest.mark.asyncio
async def test_migrate_legacy_gateway_type_devices_handles_remove_failure(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    gateway = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway (1)",
    )
    enpower = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:enpower")},
        manufacturer="Enphase",
        name="System Controller (1)",
    )
    site_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name=f"Enphase Site {site_id}",
    )
    moved = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_remove_failure_metric",
        device_id=enpower.id,
        config_entry=config_entry,
    )
    moved_site = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_remove_failure_site_metric",
        device_id=site_device.id,
        config_entry=config_entry,
    )

    def _boom(_device_id: str) -> None:
        raise RuntimeError("cannot remove")

    monkeypatch.setattr(dev_reg, "async_remove_device", _boom)
    coord = SimpleNamespace(
        type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
    )

    _migrate_legacy_gateway_type_devices(hass, config_entry, coord, dev_reg, site_id)

    moved_entry = ent_reg.async_get(moved.entity_id)
    assert moved_entry is not None
    assert moved_entry.device_id == gateway.id
    moved_site_entry = ent_reg.async_get(moved_site.entity_id)
    assert moved_site_entry is not None
    assert moved_site_entry.device_id == gateway.id


@pytest.mark.asyncio
async def test_async_setup_entry_registry_sync_listener_runs_migration_on_update(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    listeners: list = []

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.serials = {RANDOM_SERIAL}
            self.data = {RANDOM_SERIAL: {"name": "Fallback Charger"}}
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return [RANDOM_SERIAL]

        def iter_type_keys(self) -> list[str]:
            return ["iqevse"]

        def type_identifier(self, type_key: str):
            return type_identifier(self.site_id, type_key)

        def type_label(self, _type_key: str) -> str:
            return "EV Chargers"

        def type_device_name(self, _type_key: str) -> str:
            return "EV Chargers (1)"

        def async_add_listener(self, callback):
            listeners.append(callback)
            return lambda: None

    dummy_coord = DummyCoordinator()
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    migrate = Mock()
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_legacy_gateway_type_devices", migrate
    )

    assert await async_setup_entry(hass, config_entry)
    assert listeners, "expected setup to register a coordinator listener"

    listeners[0]()

    assert migrate.call_count >= 2
