from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .firmware_catalog import (
    FirmwareCatalogManager,
    compare_versions,
    normalize_locale,
    normalize_version_token,
    resolve_country_and_locale,
    select_catalog_entry,
)
from .runtime_data import EnphaseConfigEntry, get_runtime_data

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)


def _type_available(coord: EnphaseCoordinator, type_key: str) -> bool:
    has_type_for_entities = getattr(coord, "has_type_for_entities", None)
    if callable(has_type_for_entities):
        return bool(has_type_for_entities(type_key))
    has_type = getattr(coord, "has_type", None)
    return bool(has_type(type_key)) if callable(has_type) else True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime_data = get_runtime_data(entry)
    coord = runtime_data.coordinator
    manager = runtime_data.firmware_catalog or FirmwareCatalogManager(hass)

    entities: list[FirmwareUpdateEntity] = []
    if _type_available(coord, "envoy"):
        entities.append(
            FirmwareUpdateEntity(
                coordinator=coord,
                manager=manager,
                device_type="envoy",
                translation_key="gateway_firmware",
                description=UpdateEntityDescription(
                    key="gateway_firmware",
                    entity_category=EntityCategory.DIAGNOSTIC,
                ),
                installed_version_getter=_gateway_installed_version,
            )
        )
    if _type_available(coord, "microinverter"):
        entities.append(
            FirmwareUpdateEntity(
                coordinator=coord,
                manager=manager,
                device_type="microinverter",
                translation_key="microinverter_firmware",
                description=UpdateEntityDescription(
                    key="microinverter_firmware",
                    entity_category=EntityCategory.DIAGNOSTIC,
                ),
                installed_version_getter=_microinverter_installed_version,
            )
        )

    if entities:
        async_add_entities(entities, update_before_add=False)


class FirmwareUpdateEntity(CoordinatorEntity[EnphaseCoordinator], UpdateEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        *,
        coordinator: EnphaseCoordinator,
        manager: FirmwareCatalogManager,
        device_type: str,
        translation_key: str,
        description: UpdateEntityDescription,
        installed_version_getter: Callable[[EnphaseCoordinator], str | None],
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._coord = coordinator
        self._manager = manager
        self._device_type = device_type
        self._installed_version_getter = installed_version_getter
        self._refresh_task = None

        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{DOMAIN}_site_{coordinator.site_id}_{device_type}_firmware"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

        self._country_used: str | None = None
        self._locale_used: str = "en"
        self._source_scope: str | None = None
        self._raw_installed_version: str | None = None
        self._raw_latest_version: str | None = None
        self._catalog_generated_at: str | None = None

        self._refresh_from_catalog(self._manager.cached_catalog)

    @property
    def available(self) -> bool:
        return super().available and _type_available(self._coord, self._device_type)

    @property
    def device_info(self):
        get_type_info = getattr(self._coord, "type_device_info", None)
        if callable(get_type_info):
            info = get_type_info(self._device_type)
            if info is not None:
                return info
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._manager.status_snapshot()
        return {
            "country_used": self._country_used,
            "locale_used": self._locale_used,
            "catalog_source_scope": self._source_scope,
            "catalog_generated_at": self._catalog_generated_at,
            "raw_installed_version": self._raw_installed_version,
            "raw_latest_version": self._raw_latest_version,
            "catalog_last_fetch_utc": status.get("last_fetch_utc"),
            "catalog_last_error": status.get("last_error"),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_refresh_catalog()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_from_catalog(self._manager.cached_catalog)
        self._schedule_catalog_refresh()
        super()._handle_coordinator_update()

    def _schedule_catalog_refresh(self) -> None:
        if self.hass is None:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = self.hass.async_create_task(self._async_refresh_catalog())

    async def _async_refresh_catalog(self) -> None:
        try:
            catalog = await self._manager.async_get_catalog()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Firmware catalog refresh failed for %s: %s", self._device_type, err)
            return

        self._refresh_from_catalog(catalog)
        self.async_write_ha_state()

    def _refresh_from_catalog(self, catalog: dict[str, Any] | None) -> None:
        country, locale = resolve_country_and_locale(self._coord, self.hass)
        normalized_locale = normalize_locale(locale)

        self._country_used = country
        self._locale_used = normalized_locale

        raw_installed = self._installed_version_getter(self._coord)
        normalized_installed = normalize_version_token(raw_installed)
        self._raw_installed_version = raw_installed
        self._attr_installed_version = normalized_installed

        selected = select_catalog_entry(
            catalog,
            device_type=self._device_type,
            country=country,
            locale=normalized_locale,
        )
        self._source_scope = selected.source_scope
        self._locale_used = selected.locale_used or normalized_locale

        entry = selected.entry if isinstance(selected.entry, dict) else None
        self._catalog_generated_at = (
            str(catalog.get("generated_at"))
            if isinstance(catalog, dict) and catalog.get("generated_at") is not None
            else None
        )

        if entry is None:
            self._raw_latest_version = None
            self._attr_latest_version = None
            self._attr_release_url = None
            self._attr_release_summary = None
            return

        raw_latest = _text(entry.get("version"))
        normalized_latest = normalize_version_token(raw_latest)
        comparable_update = compare_versions(normalized_latest, normalized_installed)

        self._raw_latest_version = raw_latest
        if comparable_update is None:
            # Conservative fallback: avoid false-positive update state.
            self._attr_latest_version = None
        else:
            self._attr_latest_version = normalized_latest

        urls = entry.get("urls_by_locale")
        release_url = None
        if isinstance(urls, dict):
            chosen_key = (
                self._locale_used
                if self._locale_used in urls
                else (str(next(iter(urls.keys()))) if urls else None)
            )
            if chosen_key is not None:
                release_url = _text(urls.get(chosen_key))
                self._locale_used = chosen_key

        self._attr_release_url = release_url
        self._attr_release_summary = _text(entry.get("summary"))


def _gateway_installed_version(coord: EnphaseCoordinator) -> str | None:
    getter = getattr(coord, "type_device_sw_version", None)
    if callable(getter):
        return _text(getter("envoy"))
    return None


def _microinverter_installed_version(coord: EnphaseCoordinator) -> str | None:
    getter = getattr(coord, "type_device_sw_version", None)
    if callable(getter):
        version = _text(getter("microinverter"))
        if version:
            return version

    bucket_getter = getattr(coord, "type_bucket", None)
    if callable(bucket_getter):
        bucket = bucket_getter("microinverter")
        if isinstance(bucket, dict):
            firmware_summary = _text(bucket.get("firmware_summary"))
            if firmware_summary:
                return firmware_summary
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    return text or None
