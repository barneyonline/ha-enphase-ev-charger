from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
import yaml
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.enphase_ev.api import (
    OCPP_TRIGGER_MESSAGES,
    OCPP_TRIGGER_MESSAGES_REQUIRING_CONFIRMATION,
)
from custom_components.enphase_ev.const import CONF_SITE_ID, CONF_SITE_ONLY, DOMAIN
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.services import async_setup_services

SERVICES_YAML = Path(__file__).parents[3] / "custom_components/enphase_ev/services.yaml"


def _register_service_handlers(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> dict[tuple[str, str], object]:
    registered: dict[tuple[str, str], object] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = handler

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)
    return registered


def _register_service_metadata(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> dict[tuple[str, str], dict[str, object]]:
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)
    return registered


def _fake_service_coordinator(*, site_id: str, serials: set[str]):
    return SimpleNamespace(
        site_id=site_id,
        serials=serials,
        data={serial: {"sn": serial} for serial in serials},
        async_start_charging=AsyncMock(return_value={"status": "ok"}),
        async_stop_charging=AsyncMock(return_value=None),
        async_trigger_ocpp_message=AsyncMock(return_value={"status": "accepted"}),
        async_start_streaming=AsyncMock(return_value=None),
        async_stop_streaming=AsyncMock(return_value=None),
        async_request_refresh=AsyncMock(return_value=None),
        async_try_reauth_now=AsyncMock(
            return_value=SimpleNamespace(
                success=True, reason=None, retry_after_seconds=None
            )
        ),
        schedule_sync=SimpleNamespace(async_refresh=AsyncMock(return_value=None)),
        _email="user@example.com",
        _remember_password=True,
        _stored_password="secret",
    )


def test_trigger_message_schema_restricts_requested_message(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trigger message service accepts only known OCPP message names."""

    registered = _register_service_metadata(hass, monkeypatch)
    schema = registered[(DOMAIN, "trigger_message")]["schema"]

    assert schema({"requested_message": "MeterValues"}) == {
        "requested_message": "MeterValues",
        "confirm_advanced": False,
    }
    assert schema(
        {"requested_message": "BootNotification", "confirm_advanced": True}
    ) == {
        "requested_message": "BootNotification",
        "confirm_advanced": True,
    }

    for requested_message in (
        "status",
        "Status",
        "MeterValues ",
        "DataTransfer",
        "MeterValues;rm",
        "M" * 65,
    ):
        with pytest.raises(vol.Invalid):
            schema({"requested_message": requested_message})


def test_trigger_message_service_options_match_allowlist() -> None:
    """Service selector options must stay aligned with backend validation."""

    services = yaml.safe_load(SERVICES_YAML.read_text())
    options = services["trigger_message"]["fields"]["requested_message"]["selector"][
        "select"
    ]["options"]

    assert set(options) == OCPP_TRIGGER_MESSAGES
    assert OCPP_TRIGGER_MESSAGES_REQUIRING_CONFIRMATION < OCPP_TRIGGER_MESSAGES


@pytest.mark.asyncio
async def test_trigger_message_handler_restricts_requested_message_without_schema(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct handler calls still reject unsupported OCPP message names."""

    registered = _register_service_handlers(hass, monkeypatch)

    with pytest.raises(ServiceValidationError):
        await registered[(DOMAIN, "trigger_message")](
            SimpleNamespace(data={"requested_message": "DataTransfer"})
        )


@pytest.mark.asyncio
async def test_trigger_message_handler_requires_confirmation_for_advanced_messages(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct handler calls require confirmation for advanced OCPP triggers."""

    registered = _register_service_handlers(hass, monkeypatch)

    with pytest.raises(ServiceValidationError) as err:
        await registered[(DOMAIN, "trigger_message")](
            SimpleNamespace(data={"requested_message": "BootNotification"})
        )

    assert (
        err.value.translation_key == "exceptions.trigger_message_confirmation_required"
    )

    with pytest.raises(ServiceValidationError) as err:
        await registered[(DOMAIN, "trigger_message")](
            SimpleNamespace(
                data={
                    "requested_message": "BootNotification",
                    "confirm_advanced": "true",
                }
            )
        )

    assert (
        err.value.translation_key == "exceptions.trigger_message_confirmation_required"
    )


@pytest.mark.asyncio
async def test_services_route_evse_targets_to_owning_entry_with_site_only_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Service calls must not route EVSE work to a site-only config entry."""

    handlers = _register_service_handlers(hass, monkeypatch)

    site_only_coord = _fake_service_coordinator(site_id="site-only", serials=set())
    evse_coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})

    site_only_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "site-only", CONF_SITE_ONLY: True},
        title="Site Only",
        unique_id="site-only",
    )
    site_only_entry.add_to_hass(hass)
    site_only_entry.runtime_data = EnphaseRuntimeData(coordinator=site_only_coord)

    evse_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    evse_entry.add_to_hass(hass)
    evse_entry.runtime_data = EnphaseRuntimeData(coordinator=evse_coord)

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=site_only_entry.entry_id,
        identifiers={(DOMAIN, "site:site-only")},
        manufacturer="Enphase",
        name="Site Only Device",
    )
    device_registry.async_get_or_create(
        config_entry_id=evse_entry.entry_id,
        identifiers={(DOMAIN, "site:evse-site")},
        manufacturer="Enphase",
        name="EVSE Site Device",
    )
    charger = device_registry.async_get_or_create(
        config_entry_id=evse_entry.entry_id,
        identifiers={(DOMAIN, "EVSE123")},
        manufacturer="Enphase",
        name="Garage Charger",
        via_device=(DOMAIN, "site:evse-site"),
    )

    await handlers[(DOMAIN, "start_charging")](
        SimpleNamespace(
            data={
                "device_id": [charger.id],
                "charging_level": 24,
                "connector_id": 2,
            }
        )
    )
    evse_coord.async_start_charging.assert_awaited_once_with(
        "EVSE123", requested_amps=24, connector_id=2
    )
    site_only_coord.async_start_charging.assert_not_awaited()

    await handlers[(DOMAIN, "stop_charging")](
        SimpleNamespace(data={"device_id": [charger.id]})
    )
    evse_coord.async_stop_charging.assert_awaited_once_with("EVSE123")
    site_only_coord.async_stop_charging.assert_not_awaited()

    trigger_result = await handlers[(DOMAIN, "trigger_message")](
        SimpleNamespace(
            data={"device_id": [charger.id], "requested_message": "MeterValues"}
        )
    )
    assert trigger_result == {
        "results": [
            {
                "device_id": charger.id,
                "serial": "EVSE123",
                "site_id": "evse-site",
                "response": {"status": "accepted"},
            }
        ]
    }
    evse_coord.async_trigger_ocpp_message.assert_awaited_once_with(
        "EVSE123", "MeterValues"
    )
    site_only_coord.async_trigger_ocpp_message.assert_not_awaited()

    evse_coord.async_trigger_ocpp_message.reset_mock()
    trigger_result = await handlers[(DOMAIN, "trigger_message")](
        SimpleNamespace(
            data={
                "device_id": [charger.id],
                "requested_message": "BootNotification",
                "confirm_advanced": True,
            }
        )
    )
    assert trigger_result["results"][0]["response"] == {"status": "accepted"}
    evse_coord.async_trigger_ocpp_message.assert_awaited_once_with(
        "EVSE123", "BootNotification"
    )

    await handlers[(DOMAIN, "start_live_stream")](
        SimpleNamespace(data={"device_id": [charger.id]})
    )
    evse_coord.async_start_streaming.assert_awaited_once_with(manual=True)
    site_only_coord.async_start_streaming.assert_not_awaited()

    await handlers[(DOMAIN, "stop_live_stream")](
        SimpleNamespace(data={"device_id": [charger.id]})
    )
    evse_coord.async_stop_streaming.assert_awaited_once_with(manual=True)
    site_only_coord.async_stop_streaming.assert_not_awaited()

    await handlers[(DOMAIN, "sync_schedules")](
        SimpleNamespace(data={"device_id": [charger.id]})
    )
    evse_coord.schedule_sync.async_refresh.assert_awaited_once_with(
        reason="service", serials=["EVSE123"]
    )
    site_only_coord.schedule_sync.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_targeted_services_raise_without_target_or_owner(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Targeted services should fail instead of silently doing nothing."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    device_registry = dr.async_get(hass)
    site_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "site:evse-site")},
        manufacturer="Enphase",
        name="EVSE Site Device",
    )
    orphan_charger = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "ORPHAN123")},
        manufacturer="Enphase",
        name="Orphan Charger",
    )

    no_target_calls = (
        ("start_charging", {}),
        ("stop_charging", {}),
        ("trigger_message", {"requested_message": "MeterValues"}),
        ("start_live_stream", {}),
        ("stop_live_stream", {}),
        ("sync_schedules", {}),
    )
    for service, data in no_target_calls:
        with pytest.raises(ServiceValidationError):
            await handlers[(DOMAIN, service)](SimpleNamespace(data=data))

    charger_target_calls = (
        ("start_charging", {"device_id": [site_device.id]}),
        ("stop_charging", {"device_id": [site_device.id]}),
        (
            "trigger_message",
            {"device_id": [site_device.id], "requested_message": "MeterValues"},
        ),
        ("sync_schedules", {"device_id": [site_device.id]}),
    )
    for service, data in charger_target_calls:
        with pytest.raises(ServiceValidationError):
            await handlers[(DOMAIN, service)](SimpleNamespace(data=data))

    owner_required_calls = (
        ("start_charging", {"device_id": [orphan_charger.id]}),
        ("stop_charging", {"device_id": [orphan_charger.id]}),
        (
            "trigger_message",
            {"device_id": [orphan_charger.id], "requested_message": "MeterValues"},
        ),
        ("sync_schedules", {"device_id": [orphan_charger.id]}),
    )
    for service, data in owner_required_calls:
        with pytest.raises(ServiceValidationError):
            await handlers[(DOMAIN, service)](SimpleNamespace(data=data))


@pytest.mark.asyncio
async def test_targeted_services_reject_mixed_valid_and_unknown_devices(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Targeted services should fail when any requested device cannot be resolved."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "site:evse-site")},
        manufacturer="Enphase",
        name="EVSE Site Device",
    )
    charger = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "EVSE123")},
        manufacturer="Enphase",
        name="Garage Charger",
        via_device=(DOMAIN, "site:evse-site"),
    )
    unknown_device_id = "missing-device-id"

    for service, data in (
        (
            "start_charging",
            {"device_id": [charger.id, unknown_device_id], "charging_level": 24},
        ),
        ("stop_charging", {"device_id": [charger.id, unknown_device_id]}),
        (
            "trigger_message",
            {
                "device_id": [charger.id, unknown_device_id],
                "requested_message": "MeterValues",
            },
        ),
        ("sync_schedules", {"device_id": [charger.id, unknown_device_id]}),
    ):
        with pytest.raises(ServiceValidationError):
            await handlers[(DOMAIN, service)](SimpleNamespace(data=data))

    coord.async_start_charging.assert_not_awaited()
    coord.async_stop_charging.assert_not_awaited()
    coord.async_trigger_ocpp_message.assert_not_awaited()
    coord.schedule_sync.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_try_reauth_now_uses_stored_credentials_for_selected_site(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual reauth should run once for the targeted stored-credential site."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    result = await handlers[(DOMAIN, "try_reauth_now")](
        SimpleNamespace(data={"site_id": "evse-site"})
    )

    assert result == {"site_id": "evse-site", "success": True, "reason": None}
    coord.async_try_reauth_now.assert_awaited_once_with()
    coord.async_request_refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_try_reauth_now_reports_missing_stored_credentials(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual reauth should not prompt or retry when no stored password exists."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    coord._stored_password = None
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    result = await handlers[(DOMAIN, "try_reauth_now")](
        SimpleNamespace(data={"site_id": "evse-site"})
    )

    assert result == {
        "site_id": "evse-site",
        "success": False,
        "reason": "stored_credentials_unavailable",
    }
    coord.async_try_reauth_now.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_try_reauth_now_reports_manual_retry_cooldown(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual reauth should report when the retry cooldown prevents a new login."""

    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="evse-site", serials={"EVSE123"})
    coord.async_try_reauth_now.return_value = SimpleNamespace(
        success=False,
        reason="manual_retry_cooldown_active",
        retry_after_seconds=42,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "evse-site", CONF_SITE_ONLY: False},
        title="EVSE Site",
        unique_id="evse-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    result = await handlers[(DOMAIN, "try_reauth_now")](
        SimpleNamespace(data={"site_id": "evse-site"})
    )

    assert result == {
        "site_id": "evse-site",
        "success": False,
        "reason": "manual_retry_cooldown_active",
        "retry_after_seconds": 42,
    }
    coord.async_try_reauth_now.assert_awaited_once_with()
    coord.async_request_refresh.assert_not_awaited()

    coord.async_start_charging.assert_not_awaited()
    coord.async_stop_charging.assert_not_awaited()
    coord.async_trigger_ocpp_message.assert_not_awaited()
    coord.async_start_streaming.assert_not_awaited()
    coord.async_stop_streaming.assert_not_awaited()
    coord.schedule_sync.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_tariff_rate_targets_tariff_entity(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_set_tariff_rate=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    locator = {
        "branch": "purchase",
        "kind": "period",
        "season_index": 1,
        "day_index": 1,
        "period_index": 1,
    }
    reg_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_tariff_import_rate_default_week_peak",
        config_entry=entry,
    )
    hass.states.async_set(reg_entry.entity_id, 0.18, {"tariff_locator": locator})

    await handlers[(DOMAIN, "set_tariff_rate")](
        SimpleNamespace(data={"entity_id": [reg_entry.entity_id], "rate": 0.25})
    )

    coord.tariff_runtime.async_set_tariff_rate.assert_awaited_once_with(locator, 0.25)


@pytest.mark.asyncio
async def test_set_tariff_rate_targets_current_tariff_sensor(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_set_tariff_rate=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    locator = {
        "branch": "purchase",
        "kind": "period",
        "season_index": 1,
        "day_index": 1,
        "period_index": 1,
    }
    reg_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_tariff_current_import_rate",
        config_entry=entry,
    )
    hass.states.async_set(reg_entry.entity_id, 0.18, {"tariff_locator": locator})

    await handlers[(DOMAIN, "set_tariff_rate")](
        SimpleNamespace(data={"entity_id": [reg_entry.entity_id], "rate": 0.25})
    )

    coord.tariff_runtime.async_set_tariff_rate.assert_awaited_once_with(locator, 0.25)


@pytest.mark.asyncio
async def test_set_tariff_rate_rejects_invalid_targets(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_set_tariff_rate=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)

    with pytest.raises(ServiceValidationError):
        await handlers[(DOMAIN, "set_tariff_rate")](
            SimpleNamespace(data={"rate": 0.25})
        )

    with pytest.raises(ServiceValidationError):
        await handlers[(DOMAIN, "set_tariff_rate")](
            SimpleNamespace(
                data={"entity_id": ["sensor.one", "sensor.two"], "rate": 0.25}
            )
        )

    hass.states.async_set("sensor.not_tariff", 1)
    with pytest.raises(ServiceValidationError):
        await handlers[(DOMAIN, "set_tariff_rate")](
            SimpleNamespace(data={"entity_id": ["sensor.not_tariff"], "rate": 0.25})
        )

    other_entry = ent_reg.async_get_or_create(
        "sensor",
        "other_platform",
        "external_tariff_import_rate",
        config_entry=entry,
    )
    with pytest.raises(ServiceValidationError):
        await handlers[(DOMAIN, "set_tariff_rate")](
            SimpleNamespace(data={"entity_id": [other_entry.entity_id], "rate": 0.25})
        )

    non_tariff_entry = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_grid_import",
        config_entry=entry,
    )
    with pytest.raises(ServiceValidationError):
        await handlers[(DOMAIN, "set_tariff_rate")](
            SimpleNamespace(
                data={"entity_id": [non_tariff_entry.entity_id], "rate": 0.25}
            )
        )

    missing_locator = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_tariff_import_rate_default_week_peak",
        config_entry=entry,
    )
    hass.states.async_set(missing_locator.entity_id, 0.18)
    with pytest.raises(ServiceValidationError):
        await handlers[(DOMAIN, "set_tariff_rate")](
            SimpleNamespace(
                data={"entity_id": [missing_locator.entity_id], "rate": 0.25}
            )
        )

    wrong_site = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_other-site_tariff_import_rate_default_week_peak",
    )
    hass.states.async_set(wrong_site.entity_id, 0.18, {"tariff_locator": {}})
    with pytest.raises(ServiceValidationError):
        await handlers[(DOMAIN, "set_tariff_rate")](
            SimpleNamespace(data={"entity_id": [wrong_site.entity_id], "rate": 0.25})
        )


@pytest.mark.asyncio
async def test_set_tariff_rate_falls_back_to_tariff_entity_unique_id(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = _register_service_handlers(hass, monkeypatch)
    coord = _fake_service_coordinator(site_id="tariff-site", serials=set())
    coord.tariff_runtime = SimpleNamespace(async_set_tariff_rate=AsyncMock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "tariff-site", CONF_SITE_ONLY: True},
        title="Tariff Site",
        unique_id="tariff-site",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    locator = {
        "branch": "purchase",
        "kind": "period",
        "season_index": 1,
        "day_index": 1,
        "period_index": 1,
    }
    reg_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_tariff-site_tariff_import_rate_default_week_peak",
    )
    hass.states.async_set(reg_entry.entity_id, 0.18, {"tariff_locator": locator})

    await handlers[(DOMAIN, "set_tariff_rate")](
        SimpleNamespace(data={"entity_id": reg_entry.entity_id, "rate": 0.25})
    )

    coord.tariff_runtime.async_set_tariff_rate.assert_awaited_once_with(locator, 0.25)


@pytest.mark.asyncio
async def test_set_tariff_rate_uses_legacy_entity_target_extractor(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.enphase_ev import services as services_mod

    monkeypatch.setattr(
        services_mod.ha_target,
        "async_extract_referenced_entity_ids",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        services_mod.ha_service,
        "async_extract_referenced_entity_ids",
        lambda _hass, _call: {"sensor.missing"},
    )
    handlers = _register_service_handlers(hass, monkeypatch)

    with pytest.raises(ServiceValidationError):
        await handlers[(DOMAIN, "set_tariff_rate")](SimpleNamespace(data={"rate": 1}))

    def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        services_mod.ha_service,
        "async_extract_referenced_entity_ids",
        _raise,
    )
    handlers = _register_service_handlers(hass, monkeypatch)

    with pytest.raises(ServiceValidationError):
        await handlers[(DOMAIN, "set_tariff_rate")](SimpleNamespace(data={"rate": 1}))
