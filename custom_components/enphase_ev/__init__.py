from __future__ import annotations

import logging

try:
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.core import HomeAssistant, SupportsResponse
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers import service as ha_service
except Exception:  # pragma: no cover - allow import without HA for unit tests
    ConfigEntry = object  # type: ignore[misc,assignment]
    ConfigEntryState = object  # type: ignore[misc,assignment]
    HomeAssistant = object  # type: ignore[misc,assignment]
    SupportsResponse = None  # type: ignore[assignment]
    dr = None  # type: ignore[assignment]
    ir = None  # type: ignore[assignment]
    ha_service = None  # type: ignore[assignment]

from .const import DOMAIN
from .runtime_data import EnphaseRuntimeData, get_runtime_data
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [
    "sensor",
    "binary_sensor",
    "button",
    "select",
    "number",
    "switch",
    "time",
    "calendar",
]


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Set up the integration domain and register services."""

    _register_services(hass)
    return True


def _sync_type_devices(
    entry: ConfigEntry, coord, dev_reg, site_id: object
) -> dict[str, object]:
    """Create or update type devices from coordinator inventory."""
    type_devices: dict[str, object] = {}
    iter_type_keys = getattr(coord, "iter_type_keys", None)
    type_identifier_fn = getattr(coord, "type_identifier", None)
    type_label_fn = getattr(coord, "type_label", None)
    type_device_name_fn = getattr(coord, "type_device_name", None)
    type_device_model_fn = getattr(coord, "type_device_model", None)
    type_device_hw_version_fn = getattr(coord, "type_device_hw_version", None)
    type_keys = list(iter_type_keys()) if callable(iter_type_keys) else []
    for type_key in type_keys:
        ident = type_identifier_fn(type_key) if callable(type_identifier_fn) else None
        if ident is None:
            continue
        label = type_label_fn(type_key) if callable(type_label_fn) else None
        name = type_device_name_fn(type_key) if callable(type_device_name_fn) else None
        model = (
            type_device_model_fn(type_key) if callable(type_device_model_fn) else None
        ) or label
        hw_version = (
            type_device_hw_version_fn(type_key)
            if callable(type_device_hw_version_fn)
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
        if isinstance(hw_version, str) and hw_version.strip():
            kwargs["hw_version"] = hw_version.strip()
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
            if kwargs.get("hw_version") and existing.hw_version != kwargs["hw_version"]:
                changes.append("hw_version")
        if changes:
            _LOGGER.debug(
                (
                    "Device registry update (%s) for type device %s (site=%s): "
                    "name=%s model=%s hw=%s"
                ),
                ",".join(changes),
                type_key,
                site_id,
                name,
                model,
                kwargs.get("hw_version"),
            )
        created = dev_reg.async_get_or_create(**kwargs)
        type_devices[type_key] = created
    return type_devices


def _sync_charger_devices(
    entry: ConfigEntry, coord, dev_reg, site_id: object, type_devices: dict[str, object]
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
        display_name_raw = d.get("display_name")
        display_name = str(display_name_raw) if display_name_raw else None
        fallback_name_raw = d.get("name")
        fallback_name = str(fallback_name_raw) if fallback_name_raw else None
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
        model_name = str(model_name_raw) if model_name_raw else None
        model_display = None
        if display_name and model_name:
            model_display = f"{display_name} ({model_name})"
        elif model_name:
            model_display = model_name
        elif display_name:
            model_display = display_name
        elif dev_name:
            model_display = dev_name
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


def _sync_registry_devices(entry: ConfigEntry, coord, dev_reg, site_id: object) -> None:
    type_devices = _sync_type_devices(entry, coord, dev_reg, site_id)
    _sync_charger_devices(entry, coord, dev_reg, site_id, type_devices)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    # Ensure services are present after config-entry reloads/transient unload states.
    _register_services(hass)

    # Create and prime the coordinator once, used by all platforms
    from .coordinator import (
        EnphaseCoordinator,
    )  # local import to avoid heavy deps during non-HA imports

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    # Compatibility storage path for existing tests and legacy callers.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coord}
    await coord.async_config_entry_first_refresh()

    site_id = entry.data.get("site_id")
    dev_reg = dr.async_get(hass)
    _sync_registry_devices(entry, coord, dev_reg, site_id)

    add_listener = getattr(coord, "async_add_listener", None)
    if callable(add_listener):

        def _sync_registry_on_update() -> None:
            try:
                _sync_registry_devices(entry, coord, dev_reg, site_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Skipping registry sync for site %s after update: %s", site_id, err
                )

        entry.async_on_unload(add_listener(_sync_registry_on_update))

    # Start schedule sync after device registry has been updated to ensure linking.
    if hasattr(coord, "schedule_sync"):
        # Run schedule sync startup in the background to avoid blocking setup
        hass.async_create_task(coord.schedule_sync.async_start())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coord = None
    try:
        coord = get_runtime_data(hass, entry).coordinator
    except RuntimeError:
        pass
    if coord is not None and hasattr(coord, "schedule_sync"):
        await coord.schedule_sync.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        try:
            entry.runtime_data = None
        except Exception:
            pass
        if DOMAIN in hass.data and isinstance(hass.data[DOMAIN], dict):
            hass.data[DOMAIN].pop(entry.entry_id, None)
        loaded_state = getattr(ConfigEntryState, "LOADED", None)
        has_loaded_entries = any(
            loaded_state is not None and config_entry.state is loaded_state
            for config_entry in hass.config_entries.async_entries(DOMAIN)
        )
        if not has_loaded_entries:
            async_unload_services(hass)
    return unload_ok


def _register_services(hass: HomeAssistant) -> None:
    """Compatibility wrapper for service setup."""

    from . import services as services_module

    services_module.ir = ir
    services_module.ha_service = ha_service
    async_setup_services(hass, supports_response=SupportsResponse)
