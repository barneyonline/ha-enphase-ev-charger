from __future__ import annotations

import asyncio
import logging
import re

from homeassistant.config_entries import ConfigEntryState, OperationNotAllowed
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)

from .const import CONF_INCLUDE_INVERTERS, CONF_SELECTED_TYPE_KEYS, DOMAIN
from .device_info_helpers import (
    _cloud_device_info,
    _compose_charger_model_display,
    _is_redundant_model_id,
    _normalize_evse_display_name,
    _normalize_evse_model_name,
    async_prime_integration_version,
)
from .device_types import is_dry_contact_type_key, normalize_type_key, parse_type_identifier
from .runtime_data import EnphaseConfigEntry, EnphaseRuntimeData, get_runtime_data
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Keep firmware catalog/update implementation in-tree, but disable exposing
# firmware version checks in the integration for now.
_ENABLE_FIRMWARE_VERSION_CHECKS = False

PLATFORMS: list[str] = [
    "sensor",
    "binary_sensor",
    "button",
    "select",
    "number",
    "switch",
    "time",
    "calendar",
    *(["update"] if _ENABLE_FIRMWARE_VERSION_CHECKS else []),
]

_LEGACY_GATEWAY_TYPE_KEYS: tuple[str, ...] = ("meter", "enpower")
_SITE_ENERGY_ENTITY_UNIQUE_ID_SUFFIXES: tuple[str, ...] = (
    "solar_production",
    "consumption",
    "grid_import",
    "grid_export",
    "battery_charge",
    "battery_discharge",
)
_CLOUD_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "binary_sensor": ("cloud_reachable",),
    "sensor": (
        "last_update",
        "latency_ms",
        "current_power_consumption",
        "last_error_code",
        "backoff_ends",
        *_SITE_ENERGY_ENTITY_UNIQUE_ID_SUFFIXES,
    ),
}
_LEGACY_CLOUD_ENTITY_SUFFIX_ALIASES_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "sensor": (
        "cloud_last_error",
        "cloud_last_error_code",
    ),
}


def _clean_optional_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None
    return text


def _site_entry_title(site_id: str) -> str:
    return f"Site: {site_id}"


def _normalize_selected_type_keys(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        iterable = value
    elif isinstance(value, str):
        iterable = re.split(r"[,\n]+", value)
    else:
        iterable = []
    selected: list[str] = []
    for item in iterable:
        key = normalize_type_key(item)
        if key and key not in selected:
            selected.append(key)
    return selected


def _migrate_selected_type_keys(entry: EnphaseConfigEntry) -> dict[str, object] | None:
    if CONF_SELECTED_TYPE_KEYS not in entry.data:
        return None
    raw_selected = entry.data.get(CONF_SELECTED_TYPE_KEYS, [])
    normalized_selected = _normalize_selected_type_keys(raw_selected)
    include_inverters = bool(entry.data.get(CONF_INCLUDE_INVERTERS, True))
    if include_inverters and "microinverter" not in normalized_selected:
        normalized_selected.append("microinverter")
    if not include_inverters:
        normalized_selected = [
            key for key in normalized_selected if key != "microinverter"
        ]
    if raw_selected == normalized_selected:
        return None
    updated = dict(entry.data)
    updated[CONF_SELECTED_TYPE_KEYS] = normalized_selected
    return updated


def _is_disabled_by_integration(disabled_by: object) -> bool:
    if disabled_by is None:
        return False
    value = getattr(disabled_by, "value", disabled_by)
    try:
        text = str(value).strip().lower()
    except Exception:  # noqa: BLE001
        return False
    return text == "integration"


async def _async_update_listener(
    hass: HomeAssistant, entry: EnphaseConfigEntry
) -> None:
    if getattr(entry, "disabled_by", None) is not None:
        return
    loaded_state = getattr(ConfigEntryState, "LOADED", None)
    if loaded_state is not None and entry.state is not loaded_state:
        return
    try:
        await hass.config_entries.async_reload(entry.entry_id)
    except OperationNotAllowed as err:
        _LOGGER.debug(
            "Skipping reload for entry %s while state is changing: %s",
            entry.entry_id,
            err,
        )


async def _async_unload_platforms_safe(
    hass: HomeAssistant, entry: EnphaseConfigEntry
) -> bool:
    """Unload forwarded platforms, tolerating components that never loaded the entry."""

    async def _unload_platform(platform: str) -> bool:
        try:
            return await hass.config_entries.async_forward_entry_unload(entry, platform)
        except ValueError as err:
            if str(err) != "Config entry was never loaded!":
                raise
            _LOGGER.debug(
                "Skipping unload for platform %s on entry %s because it never loaded",
                platform,
                entry.entry_id,
            )
            return True

    return all(await asyncio.gather(*(_unload_platform(platform) for platform in PLATFORMS)))


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Set up the integration domain and register services."""

    async_setup_services(hass, supports_response=SupportsResponse)
    return True


def _sync_type_devices(
    entry: EnphaseConfigEntry, coord, dev_reg, site_id: object
) -> dict[str, object]:
    """Create or update type devices from coordinator inventory."""
    type_devices: dict[str, object] = {}
    type_devices_by_identifier: dict[tuple[str, str], object] = {}
    iter_type_keys = getattr(coord, "iter_type_keys", None)
    type_identifier_fn = getattr(coord, "type_identifier", None)
    type_label_fn = getattr(coord, "type_label", None)
    type_device_name_fn = getattr(coord, "type_device_name", None)
    type_device_model_fn = getattr(coord, "type_device_model", None)
    type_device_hw_version_fn = getattr(coord, "type_device_hw_version", None)
    type_device_serial_number_fn = getattr(coord, "type_device_serial_number", None)
    type_device_model_id_fn = getattr(coord, "type_device_model_id", None)
    type_device_sw_version_fn = getattr(coord, "type_device_sw_version", None)
    has_hw_version = callable(type_device_hw_version_fn)
    has_serial_number = callable(type_device_serial_number_fn)
    has_model_id = callable(type_device_model_id_fn)
    has_sw_version = callable(type_device_sw_version_fn)
    type_keys = list(iter_type_keys()) if callable(iter_type_keys) else []
    for type_key in type_keys:
        if is_dry_contact_type_key(type_key):
            continue
        ident = type_identifier_fn(type_key) if callable(type_identifier_fn) else None
        if ident is None:
            continue
        if (
            isinstance(ident, tuple)
            and len(ident) == 2
            and ident in type_devices_by_identifier
        ):
            type_devices[type_key] = type_devices_by_identifier[ident]
            continue
        label = type_label_fn(type_key) if callable(type_label_fn) else None
        name = type_device_name_fn(type_key) if callable(type_device_name_fn) else None
        model = (
            type_device_model_fn(type_key) if callable(type_device_model_fn) else None
        ) or label
        hw_version = (
            _clean_optional_text(type_device_hw_version_fn(type_key))
            if has_hw_version
            else None
        )
        serial_number = (
            _clean_optional_text(type_device_serial_number_fn(type_key))
            if has_serial_number
            else None
        )
        model_id = (
            _clean_optional_text(type_device_model_id_fn(type_key))
            if has_model_id
            else None
        )
        if has_model_id and _is_redundant_model_id(model, model_id):
            model_id = None
        sw_version = (
            _clean_optional_text(type_device_sw_version_fn(type_key))
            if has_sw_version
            else None
        )
        if not label or not name:
            continue
        kwargs = {
            "config_entry_id": entry.entry_id,
            "identifiers": {ident},
            "manufacturer": "Enphase",
            "name": name,
            "model": model,
        }
        # Keep registry fields aligned with current coordinator data: clear stale
        # values by passing explicit None when helper methods return no value.
        if has_hw_version:
            kwargs["hw_version"] = hw_version
        if has_serial_number:
            kwargs["serial_number"] = serial_number
        if has_model_id:
            kwargs["model_id"] = model_id
        if has_sw_version:
            kwargs["sw_version"] = sw_version
        existing = dev_reg.async_get_device(identifiers={ident})
        changes: list[str] = []
        if existing is None:
            changes.append("new_device")
        else:
            if existing.name != name:
                changes.append("name")
            if existing.manufacturer != "Enphase":
                changes.append("manufacturer")
            if existing.model != model:
                changes.append("model")
            if has_hw_version and existing.hw_version != kwargs.get("hw_version"):
                changes.append("hw_version")
            if has_serial_number and (
                getattr(existing, "serial_number", None)
                != kwargs.get("serial_number")
            ):
                changes.append("serial_number")
            if has_model_id and (
                getattr(existing, "model_id", None) != kwargs.get("model_id")
            ):
                changes.append("model_id")
            if has_sw_version and (
                getattr(existing, "sw_version", None) != kwargs.get("sw_version")
            ):
                changes.append("sw_version")
        if changes:
            _LOGGER.debug(
                (
                    "Device registry update (%s) for type device %s (site=%s): "
                    "name=%s model=%s model_id=%s serial=%s sw=%s hw=%s"
                ),
                ",".join(changes),
                type_key,
                site_id,
                name,
                model,
                kwargs.get("model_id"),
                kwargs.get("serial_number"),
                kwargs.get("sw_version"),
                kwargs.get("hw_version"),
            )
        created = dev_reg.async_get_or_create(**kwargs)
        type_devices[type_key] = created
        if isinstance(ident, tuple) and len(ident) == 2:
            type_devices_by_identifier[ident] = created
    return type_devices


def _sync_charger_devices(
    entry: EnphaseConfigEntry,
    coord,
    dev_reg,
    site_id: object,
    type_devices: dict[str, object],
) -> None:
    """Create or update charger devices and parent links."""
    type_identifier_fn = getattr(coord, "type_identifier", None)
    evse_parent_ident = (
        type_identifier_fn("iqevse") if callable(type_identifier_fn) else None
    )
    evse_parent_id = None
    evse_parent = type_devices.get("iqevse")
    if evse_parent is None and evse_parent_ident is not None:
        evse_parent = dev_reg.async_get_device(identifiers={evse_parent_ident})
    if evse_parent is not None:
        evse_parent_id = getattr(evse_parent, "id", None)

    iter_serials = getattr(coord, "iter_serials", None)
    serials = list(iter_serials()) if callable(iter_serials) else []
    data_source = coord.data if isinstance(getattr(coord, "data", None), dict) else {}
    for sn in serials:
        d = data_source.get(sn) or {}
        display_name = _normalize_evse_display_name(d.get("display_name"))
        fallback_name = _normalize_evse_display_name(d.get("name"))
        dev_name = display_name or fallback_name or f"Charger {sn}"
        kwargs = {
            "config_entry_id": entry.entry_id,
            "identifiers": {(DOMAIN, sn)},
            "manufacturer": "Enphase",
            "name": dev_name,
            "serial_number": str(sn),
        }
        if evse_parent_ident is not None:
            kwargs["via_device"] = evse_parent_ident
        model_name_raw = d.get("model_name")
        model_name = _normalize_evse_model_name(model_name_raw)
        model_display = _compose_charger_model_display(
            display_name,
            model_name_raw,
            dev_name,
        )
        if model_display:
            kwargs["model"] = model_display
        model_id = d.get("model_id")
        hw = d.get("hw_version")
        if hw:
            kwargs["hw_version"] = str(hw)
        sw = d.get("sw_version")
        if sw:
            kwargs["sw_version"] = str(sw)

        changes: list[str] = []
        existing = dev_reg.async_get_device(identifiers={(DOMAIN, sn)})
        if existing is None:
            changes.append("new_device")
        else:
            if existing.name != dev_name:
                changes.append("name")
            if existing.manufacturer != "Enphase":
                changes.append("manufacturer")
            if model_display and existing.model != model_display:
                changes.append("model")
            if hw and existing.hw_version != str(hw):
                changes.append("hw_version")
            if sw and existing.sw_version != str(sw):
                changes.append("sw_version")
            if evse_parent_id is not None and existing.via_device_id != evse_parent_id:
                changes.append("via_device")
        if changes:
            _LOGGER.debug(
                (
                    "Device registry update (%s) for charger serial=%s (site=%s): "
                    "name=%s, model=%s, model_id=%s, hw=%s, sw=%s, link_via_ev_type=%s"
                ),
                ",".join(changes),
                sn,
                site_id,
                dev_name,
                model_name,
                model_id,
                hw,
                sw,
                bool(evse_parent_ident is not None),
            )
        dev_reg.async_get_or_create(**kwargs)


def _sync_registry_devices(
    entry: EnphaseConfigEntry, coord, dev_reg, site_id: object
) -> None:
    type_devices = _sync_type_devices(entry, coord, dev_reg, site_id)
    _sync_charger_devices(entry, coord, dev_reg, site_id, type_devices)


def _iter_entity_registry_entries(ent_reg) -> list[object]:
    entities = getattr(ent_reg, "entities", None)
    if entities is None:
        return []
    values = getattr(entities, "values", None)
    if callable(values):
        try:
            return list(values())
        except Exception:  # noqa: BLE001
            return []
    if isinstance(entities, dict):
        return list(dict.values(entities))
    return []


def _iter_device_registry_entries(dev_reg) -> list[object]:
    devices = getattr(dev_reg, "devices", None)
    if devices is None:
        return []
    values = getattr(devices, "values", None)
    if callable(values):
        try:
            return list(values())
        except Exception:  # noqa: BLE001
            return []
    if isinstance(devices, dict):
        return list(dict.values(devices))
    return []


def _entries_for_device(ent_reg, device_id: str) -> list[object]:
    entries_for_device = getattr(er, "async_entries_for_device", None)
    if callable(entries_for_device):
        try:
            return list(entries_for_device(ent_reg, device_id))
        except Exception:  # noqa: BLE001
            pass
    return [
        entry
        for entry in _iter_entity_registry_entries(ent_reg)
        if getattr(entry, "device_id", None) == device_id
    ]


def _is_owned_entity(reg_entry: object, entry_id: str | None) -> bool:
    platform = getattr(reg_entry, "platform", None)
    if platform is not None and platform != DOMAIN:
        return False
    config_entry_id = getattr(reg_entry, "config_entry_id", None)
    if (
        entry_id is not None
        and config_entry_id is not None
        and config_entry_id != entry_id
    ):
        return False
    return True


def _remove_legacy_inventory_entities(
    ent_reg, site_id: str, *, entry_id: str | None
) -> int:
    unique_ids = {
        f"{DOMAIN}_site_{site_id}_type_meter_inventory",
        f"{DOMAIN}_site_{site_id}_type_envoy_inventory",
        f"{DOMAIN}_site_{site_id}_type_microinverter_inventory",
    }
    removed = 0
    for entry in _iter_entity_registry_entries(ent_reg):
        if not _is_owned_entity(entry, entry_id):
            continue
        if getattr(entry, "unique_id", None) not in unique_ids:
            continue
        entity_id = getattr(entry, "entity_id", None)
        if not entity_id:
            continue
        try:
            ent_reg.async_remove(entity_id)
            removed += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed removing legacy inventory entity %s: %s", entity_id, err
            )
    return removed


def _find_entity_id_by_unique_id(
    ent_reg, domain: str, unique_id: str, *, entry_id: str | None
) -> str | None:
    get_entity_id = getattr(ent_reg, "async_get_entity_id", None)
    get_entry = getattr(ent_reg, "async_get", None)
    if callable(get_entity_id):
        try:
            entity_id = get_entity_id(domain, DOMAIN, unique_id)
        except Exception:  # noqa: BLE001
            entity_id = None
        if entity_id:
            if callable(get_entry):
                reg_entry = get_entry(entity_id)
                if reg_entry is not None and not _is_owned_entity(reg_entry, entry_id):
                    return None
            return entity_id

    for reg_entry in _iter_entity_registry_entries(ent_reg):
        if getattr(reg_entry, "unique_id", None) != unique_id:
            continue
        entry_domain = getattr(reg_entry, "domain", None)
        if entry_domain is None:
            entity_id = getattr(reg_entry, "entity_id", "")
            entry_domain = entity_id.partition(".")[0] if isinstance(entity_id, str) else None
        if entry_domain != domain:
            continue
        if not _is_owned_entity(reg_entry, entry_id):
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if entity_id:
            return entity_id
    return None


def _migrate_cloud_entities_to_cloud_device(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    coord,
    dev_reg,
    site_id: object,
) -> None:
    if er is None:
        return
    site_id_raw = site_id
    if site_id_raw is None:
        site_id_raw = getattr(coord, "site_id", None)
    try:
        site_id_text = str(site_id_raw).strip()
    except Exception:  # noqa: BLE001
        site_id_text = ""
    if not site_id_text:
        return

    try:
        ent_reg = er.async_get(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Skipping cloud-device migration for site %s: %s", site_id_text, err
        )
        return

    create_device = getattr(dev_reg, "async_get_or_create", None)
    if not callable(create_device):
        return
    cloud_info = _cloud_device_info(site_id_text)
    cloud_model = cloud_info.get("model")
    if not isinstance(cloud_model, str) or not cloud_model.strip():
        cloud_model = "Cloud Service"
    cloud_sw_version = cloud_info.get("sw_version")
    if not isinstance(cloud_sw_version, str) or not cloud_sw_version.strip():
        cloud_sw_version = None
    cloud_device = create_device(
        config_entry_id=getattr(entry, "entry_id", None),
        identifiers={(DOMAIN, f"type:{site_id_text}:cloud")},
        manufacturer="Enphase",
        name="Enphase Cloud",
        model=cloud_model,
        sw_version=cloud_sw_version,
        entry_type=getattr(getattr(dr, "DeviceEntryType", None), "SERVICE", None),
    )
    cloud_device_id = getattr(cloud_device, "id", None)
    if cloud_device_id is None:
        return

    entry_id = getattr(entry, "entry_id", None)
    moved = 0
    enabled = 0
    processed_entity_ids: set[str] = set()

    def _match_cloud_suffix(unique_id: str, candidates: tuple[str, ...]) -> str | None:
        for suffix in candidates:
            if unique_id.endswith(f"_{suffix}"):
                return suffix
        return None

    def _move_entity_to_cloud_device(entity_id: str, *, should_enable: bool) -> None:
        nonlocal moved, enabled
        if not entity_id or entity_id in processed_entity_ids:
            return
        processed_entity_ids.add(entity_id)
        get_entry = getattr(ent_reg, "async_get", None)
        reg_entry = get_entry(entity_id) if callable(get_entry) else None
        update_kwargs: dict[str, object] = {}
        if reg_entry is None or getattr(reg_entry, "device_id", None) != cloud_device_id:
            update_kwargs["device_id"] = cloud_device_id
        if should_enable and _is_disabled_by_integration(
            getattr(reg_entry, "disabled_by", None)
        ):
            update_kwargs["disabled_by"] = None
        if not update_kwargs:
            return
        try:
            ent_reg.async_update_entity(entity_id, **update_kwargs)
            if "device_id" in update_kwargs:
                moved += 1
            if "disabled_by" in update_kwargs:
                enabled += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed updating cloud entity %s for site %s: %s",
                entity_id,
                site_id_text,
                err,
            )

    all_cloud_suffixes_by_domain: dict[str, tuple[str, ...]] = {}
    for domain, suffixes in _CLOUD_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN.items():
        aliases = _LEGACY_CLOUD_ENTITY_SUFFIX_ALIASES_BY_DOMAIN.get(domain, ())
        combined = tuple(dict.fromkeys((*suffixes, *aliases)))
        all_cloud_suffixes_by_domain[domain] = combined

    for domain, unique_suffixes in _CLOUD_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN.items():
        for suffix in unique_suffixes:
            unique_id = f"{DOMAIN}_site_{site_id_text}_{suffix}"
            entity_id = _find_entity_id_by_unique_id(
                ent_reg, domain, unique_id, entry_id=entry_id
            )
            if not entity_id:
                continue
            should_enable = bool(
                suffix in _SITE_ENERGY_ENTITY_UNIQUE_ID_SUFFIXES
            )
            _move_entity_to_cloud_device(entity_id, should_enable=should_enable)

    # Older releases used different unique_id prefixes for some cloud diagnostics.
    # Sweep owned entities and match by known cloud suffixes to catch those variants.
    site_marker = f"_site_{site_id_text}_"
    for reg_entry in _iter_entity_registry_entries(ent_reg):
        if not _is_owned_entity(reg_entry, entry_id):
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if not entity_id:
            continue
        domain = getattr(reg_entry, "domain", None)
        if domain is None and isinstance(entity_id, str):
            domain = entity_id.partition(".")[0]
        if domain not in all_cloud_suffixes_by_domain:
            continue
        unique_id = getattr(reg_entry, "unique_id", None)
        if not isinstance(unique_id, str) or not unique_id:
            continue
        if "_site_" in unique_id and site_marker not in unique_id:
            continue
        suffix = _match_cloud_suffix(unique_id, all_cloud_suffixes_by_domain[domain])
        if suffix is None:
            continue
        should_enable = suffix in _SITE_ENERGY_ENTITY_UNIQUE_ID_SUFFIXES
        _move_entity_to_cloud_device(entity_id, should_enable=should_enable)

    if moved:
        _LOGGER.debug(
            "Migrated %s cloud entities to cloud device for site %s",
            moved,
            site_id_text,
        )
    if enabled:
        _LOGGER.debug(
            "Enabled %s site energy entities by default for site %s",
            enabled,
            site_id_text,
        )


def _migrate_legacy_gateway_type_devices(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    coord,
    dev_reg,
    site_id: object,
) -> None:
    if er is None:
        return
    site_id_raw = site_id
    if site_id_raw is None:
        site_id_raw = getattr(coord, "site_id", None)
    try:
        site_id_text = str(site_id_raw).strip()
    except Exception:  # noqa: BLE001
        site_id_text = ""
    if not site_id_text:
        return

    type_identifier_fn = getattr(coord, "type_identifier", None)
    gateway_ident = (
        type_identifier_fn("envoy") if callable(type_identifier_fn) else None
    ) or (DOMAIN, f"type:{site_id_text}:envoy")
    gateway_device = dev_reg.async_get_device(identifiers={gateway_ident})
    if gateway_device is None:
        return
    gateway_device_id = getattr(gateway_device, "id", None)
    if gateway_device_id is None:
        return

    try:
        ent_reg = er.async_get(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Skipping legacy type-device migration for site %s: %s", site_id_text, err
        )
        return

    entry_id = getattr(entry, "entry_id", None)
    removed_inventory = _remove_legacy_inventory_entities(
        ent_reg, site_id_text, entry_id=entry_id
    )
    if removed_inventory:
        _LOGGER.debug(
            "Removed %s legacy inventory entities for site %s",
            removed_inventory,
            site_id_text,
        )

    remove_device = getattr(dev_reg, "async_remove_device", None)

    def _move_device_to_gateway(legacy_device: object, type_key: str) -> None:
        legacy_device_id = getattr(legacy_device, "id", None)
        if legacy_device_id is None or legacy_device_id == gateway_device_id:
            return

        moved = 0
        for reg_entry in _entries_for_device(ent_reg, legacy_device_id):
            if not _is_owned_entity(reg_entry, entry_id):
                continue
            entity_id = getattr(reg_entry, "entity_id", None)
            if not entity_id:
                continue
            try:
                ent_reg.async_update_entity(entity_id, device_id=gateway_device_id)
                moved += 1
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed moving entity %s from legacy %s device to gateway for site %s: %s",
                    entity_id,
                    type_key,
                    site_id_text,
                    err,
                )

        remaining = _entries_for_device(ent_reg, legacy_device_id)
        if remaining:
            _LOGGER.debug(
                "Keeping legacy %s type device for site %s; %s entities remain",
                type_key,
                site_id_text,
                len(remaining),
            )
            return

        if callable(remove_device):
            try:
                remove_device(legacy_device_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed removing legacy %s type device for site %s: %s",
                    type_key,
                    site_id_text,
                    err,
                )
        if moved:
            _LOGGER.debug(
                "Migrated %s entities from legacy %s type device to gateway for site %s",
                moved,
                type_key,
                site_id_text,
            )

    for type_key in _LEGACY_GATEWAY_TYPE_KEYS:
        legacy_ident = (DOMAIN, f"type:{site_id_text}:{type_key}")
        legacy_device = dev_reg.async_get_device(identifiers={legacy_ident})
        if legacy_device is None:
            continue
        _move_device_to_gateway(legacy_device, type_key)

    for legacy_device in _iter_device_registry_entries(dev_reg):
        config_entries = getattr(legacy_device, "config_entries", None)
        if config_entries is not None and entry_id not in config_entries:
            continue
        identifiers = getattr(legacy_device, "identifiers", None)
        if not identifiers:
            continue
        matched_type_key: str | None = None
        for ident_domain, ident_value in identifiers:
            if ident_domain != DOMAIN:
                continue
            parsed = parse_type_identifier(ident_value)
            if parsed is None:
                continue
            ident_site_id, type_key = parsed
            if ident_site_id != site_id_text or not is_dry_contact_type_key(type_key):
                continue
            matched_type_key = type_key
            break
        if matched_type_key is None:
            continue
        _move_device_to_gateway(legacy_device, matched_type_key)

    legacy_site_ident = (DOMAIN, f"site:{site_id_text}")
    legacy_site_device = dev_reg.async_get_device(identifiers={legacy_site_ident})
    if legacy_site_device is None:
        return
    legacy_site_device_id = getattr(legacy_site_device, "id", None)
    if legacy_site_device_id is None or legacy_site_device_id == gateway_device_id:
        return

    moved_site_entities = 0
    for reg_entry in _entries_for_device(ent_reg, legacy_site_device_id):
        if not _is_owned_entity(reg_entry, entry_id):
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if not entity_id:
            continue
        try:
            ent_reg.async_update_entity(entity_id, device_id=gateway_device_id)
            moved_site_entities += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed moving entity %s from legacy site device to gateway for site %s: %s",
                entity_id,
                site_id_text,
                err,
            )

    remaining_site_entries = _entries_for_device(ent_reg, legacy_site_device_id)
    if remaining_site_entries:
        _LOGGER.debug(
            "Keeping legacy site device for site %s; %s entities remain",
            site_id_text,
            len(remaining_site_entries),
        )
        return

    if callable(remove_device):
        try:
            remove_device(legacy_site_device_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed removing legacy site device for site %s: %s",
                site_id_text,
                err,
            )
    if moved_site_entities:
        _LOGGER.debug(
            "Migrated %s entities from legacy site device to gateway for site %s",
            moved_site_entities,
            site_id_text,
        )


async def async_setup_entry(hass: HomeAssistant, entry: EnphaseConfigEntry) -> bool:
    migrated_data = _migrate_selected_type_keys(entry)
    if migrated_data is not None:
        hass.config_entries.async_update_entry(entry, data=migrated_data)

    site_id_text = str(entry.data.get("site_id", "")).strip()
    if site_id_text:
        desired_title = _site_entry_title(site_id_text)
        if entry.title != desired_title:
            hass.config_entries.async_update_entry(entry, title=desired_title)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    # Ensure services are present after config-entry reloads/transient unload states.
    async_setup_services(hass, supports_response=SupportsResponse)

    # Create and prime the coordinator once, used by all platforms
    from .coordinator import (
        EnphaseCoordinator,
    )  # local import to avoid heavy deps during non-HA imports
    from .evse_firmware import EvseFirmwareDetailsManager
    from .firmware_catalog import FirmwareCatalogManager

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    firmware_catalog = FirmwareCatalogManager(hass)
    evse_firmware_details = EvseFirmwareDetailsManager(lambda: coord.client)
    setattr(coord, "firmware_catalog_manager", firmware_catalog)
    setattr(coord, "evse_firmware_details_manager", evse_firmware_details)
    entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=firmware_catalog,
        evse_firmware_details=evse_firmware_details,
    )
    await coord.async_config_entry_first_refresh()
    await async_prime_integration_version(hass)

    site_id = entry.data.get("site_id")
    dev_reg = dr.async_get(hass)
    _sync_registry_devices(entry, coord, dev_reg, site_id)
    _migrate_legacy_gateway_type_devices(hass, entry, coord, dev_reg, site_id)
    _migrate_cloud_entities_to_cloud_device(hass, entry, coord, dev_reg, site_id)

    add_listener = getattr(coord, "async_add_listener", None)
    if callable(add_listener):

        def _sync_registry_on_update() -> None:
            try:
                _sync_registry_devices(entry, coord, dev_reg, site_id)
                _migrate_legacy_gateway_type_devices(
                    hass, entry, coord, dev_reg, site_id
                )
                _migrate_cloud_entities_to_cloud_device(
                    hass, entry, coord, dev_reg, site_id
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Skipping registry sync for site %s after update: %s", site_id, err
                )

        entry.async_on_unload(add_listener(_sync_registry_on_update))

    # Start schedule sync after device registry has been updated to ensure linking.
    # Use background-task APIs so bootstrap does not wait on schedule helper startup.
    schedule_sync = getattr(coord, "schedule_sync", None)
    if schedule_sync is not None and hasattr(schedule_sync, "async_start"):
        start_name = f"{DOMAIN}_schedule_sync_start"
        entry_create_background = getattr(entry, "async_create_background_task", None)
        hass_create_background = getattr(hass, "async_create_background_task", None)
        if callable(entry_create_background):
            entry_create_background(
                hass,
                schedule_sync.async_start(),
                start_name,
            )
        elif callable(hass_create_background):
            hass_create_background(
                schedule_sync.async_start(),
                start_name,
            )
        else:
            hass.async_create_task(schedule_sync.async_start())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnphaseConfigEntry) -> bool:
    coord = None
    try:
        coord = get_runtime_data(entry).coordinator
    except RuntimeError:
        pass
    unload_ok = await _async_unload_platforms_safe(hass, entry)
    if unload_ok:
        if coord is not None and hasattr(coord, "schedule_sync"):
            await coord.schedule_sync.async_stop()
        if coord is not None and hasattr(coord, "cleanup_runtime_state"):
            coord.cleanup_runtime_state()
        entry.runtime_data = None
        loaded_state = getattr(ConfigEntryState, "LOADED", None)
        has_loaded_entries = any(
            loaded_state is not None and config_entry.state is loaded_state
            for config_entry in hass.config_entries.async_entries(DOMAIN)
        )
        if not has_loaded_entries:
            async_unload_services(hass)
    return unload_ok
