from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

from .const import DOMAIN

if TYPE_CHECKING:  # pragma: no cover
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import EnphaseCoordinator


@dataclass(slots=True)
class EnphaseRuntimeData:
    """Runtime objects attached to a loaded config entry."""

    coordinator: EnphaseCoordinator


if TYPE_CHECKING:  # pragma: no cover
    EnphaseConfigEntry: TypeAlias = ConfigEntry[EnphaseRuntimeData]
else:
    EnphaseConfigEntry = Any


def get_runtime_data(entry: EnphaseConfigEntry) -> EnphaseRuntimeData:
    """Return runtime data for a loaded config entry."""

    runtime_data = getattr(entry, "runtime_data", None)
    if isinstance(runtime_data, EnphaseRuntimeData):
        return runtime_data

    raise RuntimeError(f"Missing runtime data for entry {entry.entry_id}")


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
    return coordinators
