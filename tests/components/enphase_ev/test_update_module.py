from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.components.update import UpdateEntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from unittest.mock import AsyncMock

from custom_components.enphase_ev import PLATFORMS
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.update import (
    FirmwareUpdateEntity,
    _gateway_installed_version,
    _microinverter_installed_version,
    _text,
    _type_available,
    async_setup_entry,
)


class DummyCatalogManager:
    def __init__(self, catalog):
        self.cached_catalog = catalog
        self._status = {
            "last_fetch_utc": "2026-03-01T00:00:00+00:00",
            "last_error": None,
            "catalog_generated_at": catalog.get("generated_at"),
            "catalog_source_age_seconds": 30.0,
            "using_stale": False,
        }

    async def async_get_catalog(self, *, force_refresh: bool = False):  # noqa: ARG002
        return self.cached_catalog

    def status_snapshot(self):
        return dict(self._status)


class DummyCoordinator:
    def __init__(self) -> None:
        self.site_id = "12345"
        self.last_update_success = True
        self.battery_country_code = "AU"
        self.battery_locale = "fr-fr"
        self._gateway_version = "8.2.4300"
        self._micro_version = "v04.30.31"
        self._listeners = []
        self._available_types = {"envoy", "microinverter"}

    def async_add_listener(self, callback):
        self._listeners.append(callback)

        def _remove_listener():
            self._listeners.remove(callback)

        return _remove_listener

    def has_type_for_entities(self, type_key: str) -> bool:
        return type_key in self._available_types

    def has_type(self, type_key: str) -> bool:
        return self.has_type_for_entities(type_key)

    def type_device_sw_version(self, type_key: str) -> str | None:
        if type_key == "envoy":
            return self._gateway_version
        if type_key == "microinverter":
            return self._micro_version
        return None

    def type_bucket(self, type_key: str):
        if type_key != "microinverter":
            return None
        return {
            "firmware_summary": self._micro_version,
            "count": 1,
            "devices": [{}],
        }

    def type_device_info(self, type_key: str):
        return {
            "identifiers": {("enphase_ev", f"type:{self.site_id}:{type_key}")},
            "name": f"{type_key} device",
            "manufacturer": "Enphase",
            "model": "Model",
        }


def _catalog_payload() -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {
            "envoy": {
                "latest_by_country": {
                    "AU": {
                        "version": "8.2.4401",
                        "summary": "Gateway firmware update",
                        "urls_by_locale": {
                            "en": "https://example.test/envoy/en",
                            "fr-fr": "https://example.test/envoy/fr",
                        },
                    }
                },
                "latest_global": {
                    "version": "8.2.4400",
                    "summary": "Gateway global",
                    "urls_by_locale": {"en": "https://example.test/envoy/global"},
                },
            },
            "microinverter": {
                "latest_by_country": {
                    "AU": {
                        "version": "04.30.32",
                        "summary": "Micro firmware update",
                        "urls_by_locale": {
                            "en": "https://example.test/micro/en",
                            "fr-fr": "https://example.test/micro/fr",
                        },
                    }
                },
                "latest_global": {
                    "version": "04.30.30",
                    "summary": "Micro global",
                    "urls_by_locale": {"en": "https://example.test/micro/global"},
                },
            },
        },
    }


def test_platform_registers_update() -> None:
    assert "update" in PLATFORMS


@pytest.mark.asyncio
async def test_async_setup_entry_adds_firmware_update_entities(hass, config_entry) -> None:
    coord = DummyCoordinator()
    manager = DummyCatalogManager(_catalog_payload())
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=manager,
    )

    added = []

    def _capture(entities, update_before_add=False):  # noqa: ARG001
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert len(added) == 2
    unique_ids = {entity.unique_id for entity in added}
    assert f"enphase_ev_site_{coord.site_id}_envoy_firmware" in unique_ids
    assert f"enphase_ev_site_{coord.site_id}_microinverter_firmware" in unique_ids


@pytest.mark.asyncio
async def test_async_setup_entry_skips_when_types_unavailable(hass, config_entry) -> None:
    coord = DummyCoordinator()
    coord._available_types = set()
    manager = DummyCatalogManager(_catalog_payload())
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=manager,
    )
    added = []
    await async_setup_entry(hass, config_entry, lambda entities, update_before_add=False: added.extend(entities))  # noqa: ARG005
    assert added == []


@pytest.mark.asyncio
async def test_gateway_update_entity_states_and_release_url_selection(hass) -> None:
    coord = DummyCoordinator()
    manager = DummyCatalogManager(_catalog_payload())

    entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    entity.hass = hass

    entity._refresh_from_catalog(manager.cached_catalog)
    assert entity.installed_version == "8.2.4300"
    assert entity.latest_version == "8.2.4401"
    assert entity.state == "on"
    assert entity.release_url == "https://example.test/envoy/fr"
    assert entity.device_info["name"] == "envoy device"

    coord._gateway_version = "8.2.4401"
    entity._refresh_from_catalog(manager.cached_catalog)
    assert entity.state == "off"

    coord._gateway_version = "firmware build unknown"
    entity._refresh_from_catalog(manager.cached_catalog)
    assert entity.latest_version is None
    assert entity.state is None
    coord._available_types = set()
    assert entity.available is False


@pytest.mark.asyncio
async def test_microinverter_update_entity_uses_normalized_versions(hass) -> None:
    coord = DummyCoordinator()
    manager = DummyCatalogManager(_catalog_payload())

    entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        device_type="microinverter",
        translation_key="microinverter_firmware",
        description=UpdateEntityDescription(key="microinverter_firmware"),
        installed_version_getter=_microinverter_installed_version,
    )
    entity.hass = hass

    entity._refresh_from_catalog(manager.cached_catalog)
    assert entity.installed_version == "04.30.31"
    assert entity.latest_version == "04.30.32"
    assert entity.state == "on"
    assert entity.release_url == "https://example.test/micro/fr"

    attrs = entity.extra_state_attributes
    assert attrs["country_used"] == "AU"
    assert attrs["locale_used"] == "fr-fr"
    assert attrs["catalog_generated_at"] == "2026-03-01T00:00:00Z"
    assert attrs["raw_installed_version"] == "v04.30.31"


@pytest.mark.asyncio
async def test_entity_refresh_and_scheduler_branches(hass, monkeypatch) -> None:
    coord = DummyCoordinator()
    manager = DummyCatalogManager(_catalog_payload())
    entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    entity.hass = hass

    monkeypatch.setattr(
        CoordinatorEntity,
        "async_added_to_hass",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(CoordinatorEntity, "_handle_coordinator_update", lambda self: None)
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)

    await entity.async_added_to_hass()

    running = hass.async_create_task(asyncio.sleep(0.2))
    entity._refresh_task = running
    entity._schedule_catalog_refresh()
    assert entity._refresh_task is running
    running.cancel()
    try:
        await running
    except asyncio.CancelledError:
        pass

    entity.hass = None
    entity._schedule_catalog_refresh()
    entity.hass = hass

    entity._handle_coordinator_update()

    class _FailingManager(DummyCatalogManager):
        async def async_get_catalog(self, *, force_refresh: bool = False):  # noqa: ARG002
            raise RuntimeError("boom")

    failing_entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=_FailingManager(_catalog_payload()),
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    failing_entity.hass = hass
    await failing_entity._async_refresh_catalog()


@pytest.mark.asyncio
async def test_refresh_from_catalog_none_and_locale_fallback_paths(hass) -> None:
    coord = DummyCoordinator()
    coord.battery_locale = "es-es"
    payload = _catalog_payload()
    payload["devices"]["envoy"]["latest_by_country"] = {}
    payload["devices"]["envoy"]["latest_global"] = {
        "version": "8.2.4401",
        "summary": "Global only",
        "urls_by_locale": {
            "de-de": "https://example.test/envoy/de",
            "fr-fr": "https://example.test/envoy/fr",
        },
    }
    manager = DummyCatalogManager(payload)
    entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    entity.hass = hass

    entity._refresh_from_catalog(None)
    assert entity.latest_version is None
    assert entity.release_url is None
    assert entity.release_summary is None

    entity._refresh_from_catalog(payload)
    assert entity.release_url == "https://example.test/envoy/de"
    assert entity.extra_state_attributes["locale_used"] == "de-de"


def test_helper_functions_cover_edge_paths() -> None:
    assert _type_available(object(), "envoy") is True
    assert _type_available(SimpleNamespace(has_type=lambda key: key == "envoy"), "envoy")
    assert not _type_available(SimpleNamespace(has_type=lambda key: False), "envoy")

    assert _gateway_installed_version(object()) is None

    coord = SimpleNamespace(
        type_device_sw_version=lambda _type: None,
        type_bucket=lambda _type: {"firmware_summary": "v1.2.3"},
    )
    assert _microinverter_installed_version(coord) == "v1.2.3"
    assert _microinverter_installed_version(SimpleNamespace()) is None
    assert _text(None) is None

    class _BadStr:
        def __str__(self):
            raise ValueError("boom")

    assert _text(_BadStr()) is None


def test_device_info_none_when_coordinator_does_not_supply_info() -> None:
    coord = DummyCoordinator()
    coord.type_device_info = lambda _type: None
    entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=DummyCatalogManager(_catalog_payload()),
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    assert entity.device_info is None
