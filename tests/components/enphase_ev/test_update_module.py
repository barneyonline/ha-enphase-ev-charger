from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.components.update import UpdateEntityDescription
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from unittest.mock import AsyncMock

from custom_components.enphase_ev import PLATFORMS
from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.update import (
    ChargerFirmwareUpdateEntity,
    FirmwareUpdateEntity,
    _async_prune_removed_charger_updates,
    _as_bool,
    _as_int,
    _charger_serials,
    _charger_installed_version,
    _charger_update_unique_id,
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


class DummyEvseFirmwareManager:
    def __init__(self, details):
        self.cached_details = details
        self._status = {
            "cache_expires_utc": "2026-03-01T01:00:00+00:00",
            "last_fetch_utc": "2026-03-01T00:00:00+00:00",
            "last_success_utc": "2026-03-01T00:00:00+00:00",
            "last_error": None,
            "using_stale": False,
        }

    async def async_get_details(self, *, force_refresh: bool = False):  # noqa: ARG002
        return self.cached_details

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
        self._iqevse_version = "25.37.1.13"
        self._listeners = []
        self._available_types = {"envoy", "microinverter", "iqevse"}
        self.data = {
            "482522020944": {
                "firmware_version": "25.37.1.13",
                "system_version": "25.37.1.13",
                "display_name": "Driveway Charger",
            }
        }

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
        if type_key == "iqevse":
            return self._iqevse_version
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

    def iter_serials(self) -> list[str]:
        return list(self.data)


def _catalog_payload() -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {
            "envoy": {
                "latest_by_locale": {
                    "fr-fr": {
                        "version": "8.2.4401",
                        "summary": "Gateway firmware update",
                        "urls_by_locale": {"fr-fr": "https://example.test/envoy/fr"},
                    }
                },
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
                "latest_by_locale": {
                    "fr-fr": {
                        "version": "04.30.32",
                        "summary": "Micro firmware update",
                        "urls_by_locale": {"fr-fr": "https://example.test/micro/fr"},
                    }
                },
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


def _evse_payload() -> dict[str, dict]:
    return {
        "482522020944": {
            "serialNumber": "482522020944",
            "siteId": 3381244,
            "upgradeStatus": 5,
            "currentFwVersion": "25.37.1.13",
            "targetFwVersion": "25.37.1.14",
            "lastSuccessfulUpgradeDate": "2025-12-08T22:41:46.568837098Z[UTC]",
            "lastUpdatedAt": "2025-12-08T15:52:59.806385175Z[UTC]",
            "statusDetail": None,
            "isAutoOta": False,
        }
    }


def test_platform_disables_update_by_default() -> None:
    assert "update" not in PLATFORMS


@pytest.mark.asyncio
async def test_async_setup_entry_adds_firmware_update_entities(hass, config_entry) -> None:
    coord = DummyCoordinator()
    catalog_manager = DummyCatalogManager(_catalog_payload())
    evse_manager = DummyEvseFirmwareManager(_evse_payload())
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=catalog_manager,
        evse_firmware_details=evse_manager,
    )

    added = []

    def _capture(entities, update_before_add=False):  # noqa: ARG001
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert len(added) == 3
    unique_ids = {entity.unique_id for entity in added}
    assert f"enphase_ev_site_{coord.site_id}_envoy_firmware" in unique_ids
    assert f"enphase_ev_site_{coord.site_id}_microinverter_firmware" in unique_ids
    assert "enphase_ev_482522020944_charger_firmware" in unique_ids


@pytest.mark.asyncio
async def test_async_setup_entry_skips_when_types_unavailable(hass, config_entry) -> None:
    coord = DummyCoordinator()
    coord._available_types = set()
    catalog_manager = DummyCatalogManager(_catalog_payload())
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=catalog_manager,
        evse_firmware_details=DummyEvseFirmwareManager(_evse_payload()),
    )
    added = []
    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),  # noqa: ARG005
    )
    assert added == []


@pytest.mark.asyncio
async def test_async_setup_entry_skips_charger_entities_when_no_serials(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord._available_types = {"iqevse"}
    coord.data = {}
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=DummyCatalogManager(_catalog_payload()),
        evse_firmware_details=DummyEvseFirmwareManager(_evse_payload()),
    )

    added = []
    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),  # noqa: ARG005
    )
    assert added == []


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_removed_charger_entities(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=DummyCatalogManager(_catalog_payload()),
        evse_firmware_details=DummyEvseFirmwareManager(_evse_payload()),
    )
    ent_reg = er.async_get(hass)
    removed_unique_id = _charger_update_unique_id("REMOVED123")
    ent_reg.async_get_or_create(
        "update",
        "enphase_ev",
        removed_unique_id,
        config_entry=config_entry,
    )

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: None,  # noqa: ARG005
    )

    assert ent_reg.async_get_entity_id("update", "enphase_ev", removed_unique_id) is None


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_charger_entities_when_type_unavailable(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord._available_types = set()
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=DummyCatalogManager(_catalog_payload()),
        evse_firmware_details=DummyEvseFirmwareManager(_evse_payload()),
    )
    ent_reg = er.async_get(hass)
    removed_unique_id = _charger_update_unique_id("482522020944")
    ent_reg.async_get_or_create(
        "update",
        "enphase_ev",
        removed_unique_id,
        config_entry=config_entry,
    )

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: None,  # noqa: ARG005
    )

    assert ent_reg.async_get_entity_id("update", "enphase_ev", removed_unique_id) is None


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
    assert attrs["catalog_source_scope"] == "locale"
    assert attrs["catalog_generated_at"] == "2026-03-01T00:00:00Z"
    assert attrs["raw_installed_version"] == "v04.30.31"


@pytest.mark.asyncio
async def test_charger_update_entity_uses_fw_details_payload(hass) -> None:
    coord = DummyCoordinator()
    manager = DummyEvseFirmwareManager(_evse_payload())
    entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        serial="482522020944",
        description=UpdateEntityDescription(key="charger_firmware"),
    )
    entity.hass = hass

    entity._refresh_from_details(manager.cached_details)
    assert entity.installed_version == "25.37.1.13"
    assert entity.latest_version == "25.37.1.14"
    assert entity.state == "on"
    assert entity.available is True
    assert entity.device_info["identifiers"] == {("enphase_ev", "482522020944")}

    attrs = entity.extra_state_attributes
    assert attrs["upgrade_status"] == 5
    assert attrs["last_successful_upgrade_date"] == (
        "2025-12-08T22:41:46.568837098Z[UTC]"
    )
    assert attrs["last_updated_at"] == "2025-12-08T15:52:59.806385175Z[UTC]"
    assert attrs["is_auto_ota"] is False
    assert attrs["details_last_error"] is None


@pytest.mark.asyncio
async def test_charger_update_entity_falls_back_to_summary_versions(hass) -> None:
    coord = DummyCoordinator()
    manager = DummyEvseFirmwareManager(
        {
            "482522020944": {
                "serialNumber": "482522020944",
                "targetFwVersion": "25.37.1.14",
            }
        }
    )
    entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        serial="482522020944",
        description=UpdateEntityDescription(key="charger_firmware"),
    )
    entity.hass = hass

    entity._refresh_from_details(manager.cached_details)
    assert entity.installed_version == "25.37.1.13"
    assert entity.latest_version == "25.37.1.14"
    assert entity.state == "on"

    manager.cached_details["482522020944"]["targetFwVersion"] = "firmware pending"
    entity._refresh_from_details(manager.cached_details)
    assert entity.latest_version is None
    assert entity.state is None


@pytest.mark.asyncio
async def test_entity_refresh_and_scheduler_branches(hass, monkeypatch) -> None:
    coord = DummyCoordinator()
    catalog_manager = DummyCatalogManager(_catalog_payload())
    entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=catalog_manager,
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
async def test_charger_entity_refresh_branches(hass, monkeypatch) -> None:
    coord = DummyCoordinator()
    manager = DummyEvseFirmwareManager(_evse_payload())
    entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        serial="482522020944",
        description=UpdateEntityDescription(key="charger_firmware"),
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
    entity._schedule_details_refresh()
    assert entity._refresh_task is running
    running.cancel()
    try:
        await running
    except asyncio.CancelledError:
        pass

    entity.hass = None
    entity._schedule_details_refresh()
    entity.hass = hass
    entity._handle_coordinator_update()

    class _FailingManager(DummyEvseFirmwareManager):
        async def async_get_details(self, *, force_refresh: bool = False):  # noqa: ARG002
            raise RuntimeError("boom")

    failing_entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=_FailingManager(_evse_payload()),
        serial="482522020944",
        description=UpdateEntityDescription(key="charger_firmware"),
    )
    failing_entity.hass = hass
    await failing_entity._async_refresh_details()


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
        data={"SN1": {"firmware_version": "1.2.3"}},
        type_device_sw_version=lambda _type: None,
        type_bucket=lambda _type: {"firmware_summary": "v1.2.3"},
    )
    assert _microinverter_installed_version(coord) == "v1.2.3"
    assert _microinverter_installed_version(SimpleNamespace()) is None
    assert _charger_serials(SimpleNamespace()) == []
    assert _charger_installed_version(coord, "SN1") == "1.2.3"
    fallback_coord = SimpleNamespace(type_device_sw_version=lambda _type: "2.0")
    assert _charger_installed_version(fallback_coord, "SN2") == "2.0"
    assert _charger_installed_version(SimpleNamespace(), "SN3") is None
    assert _as_bool(True) is True
    assert _as_bool(1) is True
    assert _as_bool("0") is False
    assert _as_bool("yes") is True
    assert _as_bool("unknown") is None
    assert _as_int("5") == 5
    assert _as_int("bad") is None
    assert _text(None) is None

    class _BadStr:
        def __str__(self):
            raise ValueError("boom")

    assert _text(_BadStr()) is None
    assert _as_bool(_BadStr()) is None


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


def test_prune_removed_charger_updates_covers_registry_filters() -> None:
    removed: list[str] = []
    ent_reg = SimpleNamespace(
        entities={
            "no_domain": SimpleNamespace(
                entity_id="update.no_domain",
                domain=None,
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id=_charger_update_unique_id("REMOVE_ME"),
            ),
            "wrong_domain": SimpleNamespace(
                entity_id="sensor.wrong_domain",
                domain="sensor",
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id=_charger_update_unique_id("SENSOR_SKIP"),
            ),
            "wrong_platform": SimpleNamespace(
                entity_id="update.wrong_platform",
                domain="update",
                platform="other_platform",
                config_entry_id="entry-1",
                unique_id=_charger_update_unique_id("PLATFORM_SKIP"),
            ),
            "wrong_entry": SimpleNamespace(
                entity_id="update.wrong_entry",
                domain="update",
                platform=DOMAIN,
                config_entry_id="entry-2",
                unique_id=_charger_update_unique_id("ENTRY_SKIP"),
            ),
            "bad_unique": SimpleNamespace(
                entity_id="update.bad_unique",
                domain="update",
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id="not_a_charger_update",
            ),
            "current_serial": SimpleNamespace(
                entity_id="update.current_serial",
                domain="update",
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id=_charger_update_unique_id("KEEP_ME"),
            ),
        },
        async_remove=lambda entity_id: removed.append(entity_id),
    )
    known_serials = {"REMOVE_ME", "KEEP_ME"}

    _async_prune_removed_charger_updates(
        entry=SimpleNamespace(entry_id="entry-1"),
        ent_reg=ent_reg,
        current_serials={"KEEP_ME"},
        known_serials=known_serials,
    )

    assert removed == ["update.no_domain"]
    assert known_serials == {"KEEP_ME"}
