from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.enphase_ev import (
    DOMAIN,
    _async_update_listener,
    _sync_charger_devices,
    _sync_type_devices,
    _register_services,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.enphase_ev.const import CONF_SITE_ID
from custom_components.enphase_ev.device_types import type_identifier
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
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    unload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)

    assert await async_unload_entry(hass, config_entry)
    schedule_sync.async_stop.assert_awaited_once()
    unload.assert_awaited_once()
    assert config_entry.entry_id not in hass.data[DOMAIN]


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

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].clear()
    hass.data[DOMAIN]["entry-one"] = {"coordinator": coord_primary}
    hass.data[DOMAIN]["entry-two"] = {"coordinator": coord_duplicate}
    hass.data[DOMAIN]["entry-three"] = {"coordinator": coord_other}
    hass.data[DOMAIN]["entry-bad"] = "invalid"

    _register_services(hass)

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

    hass.data[DOMAIN].clear()
    await svc_start_stream(SimpleNamespace(data={"site_id": "missing"}))
    await svc_stop_stream(SimpleNamespace(data={"site_id": "missing"}))

    supports_response = registered[(DOMAIN, "trigger_message")]["kwargs"][
        "supports_response"
    ]
    from custom_components.enphase_ev import SupportsResponse

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

    _register_services(hass)

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
