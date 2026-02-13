from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import EnphaseCoordinator


@dataclass(slots=True)
class EnphaseRuntimeData:
    """Runtime objects attached to a loaded config entry."""

    coordinator: EnphaseCoordinator


def get_runtime_data(hass: HomeAssistant, entry: ConfigEntry) -> EnphaseRuntimeData:
    """Return runtime data for an entry, with compatibility fallback."""

    runtime_data = getattr(entry, "runtime_data", None)
    if isinstance(runtime_data, EnphaseRuntimeData):
        return runtime_data

    legacy_entry = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = (
        legacy_entry.get("coordinator") if isinstance(legacy_entry, dict) else None
    )
    if coordinator is None:
        raise RuntimeError(f"Missing runtime data for entry {entry.entry_id}")

    hydrated = EnphaseRuntimeData(coordinator=coordinator)
    try:
        entry.runtime_data = hydrated
    except Exception:  # pragma: no cover - legacy mocked entries
        pass
    return hydrated


def iter_coordinators(
    hass: HomeAssistant, *, site_ids: set[str] | None = None
) -> list[EnphaseCoordinator]:
    """Return coordinators from loaded config entries."""

    coordinators: list[EnphaseCoordinator] = []
    seen: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime_data = getattr(entry, "runtime_data", None)
        if not isinstance(runtime_data, EnphaseRuntimeData):
            continue
        coord = runtime_data.coordinator
        site_id = str(getattr(coord, "site_id", ""))
        if site_ids and site_id not in site_ids:
            continue
        if site_id in seen:
            continue
        seen.add(site_id)
        coordinators.append(coord)
    # Compatibility path for tests and legacy code paths.
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(entry_data, dict):
            continue
        coord = entry_data.get("coordinator")
        if coord is None:
            continue
        site_id = str(getattr(coord, "site_id", ""))
        if site_ids and site_id not in site_ids:
            continue
        if site_id in seen:
            continue
        seen.add(site_id)
        coordinators.append(coord)
    return coordinators
