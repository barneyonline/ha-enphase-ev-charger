from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from .const import DOMAIN
from .log_redaction import redact_identifier, redact_text

_LOGGER = logging.getLogger(__name__)


def iter_entity_registry_entries(ent_reg) -> list[object]:
    """Best-effort iteration over entity registry entries."""
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


def is_owned_entity(reg_entry: object, entry_id: str | None, domain: str) -> bool:
    """Return True when the registry entry belongs to this integration entry."""
    entry_domain = getattr(reg_entry, "domain", None)
    if entry_domain is None:
        entity_id = getattr(reg_entry, "entity_id", "")
        entry_domain = entity_id.partition(".")[0] if isinstance(entity_id, str) else ""
    if entry_domain != domain:
        return False

    entry_platform = getattr(reg_entry, "platform", None)
    if entry_platform is not None and entry_platform != DOMAIN:
        return False

    config_entry_id = getattr(reg_entry, "config_entry_id", None)
    if (
        entry_id is not None
        and config_entry_id is not None
        and config_entry_id != entry_id
    ):
        return False
    return True


def prune_managed_entities(
    ent_reg,
    entry_id: str | None,
    *,
    domain: str,
    active_unique_ids: Iterable[str],
    is_managed: Callable[[str], bool],
) -> int:
    """Remove managed owned entities that are no longer active."""
    active = set(active_unique_ids)
    removed = 0
    for reg_entry in list(iter_entity_registry_entries(ent_reg)):
        if not is_owned_entity(reg_entry, entry_id, domain):
            continue
        unique_id = getattr(reg_entry, "unique_id", None)
        if not isinstance(unique_id, str) or not is_managed(unique_id):
            continue
        if unique_id in active:
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if not entity_id:
            continue
        try:
            ent_reg.async_remove(entity_id)
            removed += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed removing stale %s entity %s: %s",
                domain,
                redact_identifier(entity_id),
                redact_text(err),
            )
    return removed
