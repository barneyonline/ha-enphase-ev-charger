from __future__ import annotations

import asyncio
from collections.abc import Iterable
import copy
import inspect
import logging
from datetime import datetime, time as dt_time, timedelta
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.components.schedule.const import (
    CONF_ALL_DAYS,
    CONF_FROM,
    CONF_TO,
    DOMAIN as SCHEDULE_DOMAIN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.collection import CHANGE_ADDED, CollectionChange
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_SCHEDULE_NAMING,
    DOMAIN,
    OPT_SCHEDULE_EXPOSE_OFF_PEAK,
    OPT_SCHEDULE_NAMING,
    OPT_SCHEDULE_SYNC_ENABLED,
    SCHEDULE_NAMING_TIME_WINDOW,
    SCHEDULE_NAMING_TYPE_TIME_WINDOW,
)
from .schedule import HelperDefinition, helper_to_slot, slot_to_helper

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
SYNC_INTERVAL = timedelta(minutes=5)
SUPPRESS_SECONDS = 2.0


class ScheduleSync:
    def __init__(self, hass: HomeAssistant, coordinator, config_entry=None) -> None:
        self.hass = hass
        self._coordinator = coordinator
        self._config_entry = config_entry
        entry_id = getattr(config_entry, "entry_id", "default")
        self._store = Store(hass, STORE_VERSION, f"{DOMAIN}.schedule_map.{entry_id}")
        self._mapping: dict[str, dict[str, str]] = {}
        self._slot_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._meta_cache: dict[str, str | None] = {}
        self._lock = asyncio.Lock()
        self._storage_collection = None
        self._unsub_interval = None
        self._unsub_state = None
        self._unsub_coordinator = None
        self._suppress_updates: set[str] = set()
        self._last_sync: datetime | None = None
        self._last_error: str | None = None
        self._last_status: str | None = None

    async def async_start(self) -> None:
        await self._load_mapping()
        await self._ensure_storage_collection()
        self._update_state_listener()
        self._unsub_interval = async_track_time_interval(
            self.hass, self._handle_interval, SYNC_INTERVAL
        )
        try:
            self._unsub_coordinator = self._coordinator.async_add_listener(
                self._handle_coordinator_update
            )
        except Exception:
            self._unsub_coordinator = None
        await self.async_refresh(reason="startup")

    async def async_stop(self) -> None:
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_coordinator is not None:
            self._unsub_coordinator()
            self._unsub_coordinator = None

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": self._sync_enabled(),
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
            "last_status": self._last_status,
            "last_error": self._last_error,
            "cached_serials": sorted(self._slot_cache.keys()),
            "mapping_counts": {
                serial: len(slots) for serial, slots in self._mapping.items()
            },
        }

    @callback
    def _handle_interval(self, *_args) -> None:
        self.hass.async_create_task(self.async_refresh(reason="interval"))

    @callback
    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._refresh_if_stale())

    async def _refresh_if_stale(self) -> None:
        if not self._last_sync:
            await self.async_refresh(reason="coordinator")
            return
        age = dt_util.utcnow() - self._last_sync
        if age >= SYNC_INTERVAL:
            await self.async_refresh(reason="coordinator")

    async def async_refresh(
        self, *, reason: str = "manual", serials: Iterable[str] | None = None
    ) -> None:
        if not self._sync_enabled():
            self._last_status = "disabled"
            return
        if not self._has_scheduler_bearer():
            self._last_status = "missing_bearer"
            return
        if self._lock.locked():
            return
        async with self._lock:
            await self._ensure_storage_collection()
            serial_list = (
                list(serials)
                if serials is not None
                else self._coordinator.iter_serials()
            )
            for sn in serial_list:
                await self._sync_serial(sn)
            self._last_sync = dt_util.utcnow()
            self._last_status = f"ok:{reason}"

    @callback
    def _handle_state_change(self, event) -> None:
        entity_id = event.data.get("entity_id")
        if not entity_id or entity_id in self._suppress_updates:
            return
        self.hass.async_create_task(self.async_handle_helper_change(entity_id))

    async def async_handle_helper_change(self, entity_id: str) -> None:
        if not self._sync_enabled():
            return
        if entity_id in self._suppress_updates:
            return
        slot_info = await self._slot_for_entity(entity_id)
        if not slot_info:
            return
        sn, slot_id = slot_info
        slot_cache = self._slot_cache.get(sn, {}).get(slot_id)
        if not slot_cache:
            return
        schedule_type = slot_cache.get("scheduleType")
        if schedule_type == "OFF_PEAK":
            return
        if slot_cache.get("startTime") is None or slot_cache.get("endTime") is None:
            return
        schedule_def = await self._get_schedule(entity_id)
        if schedule_def is None:
            return
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        slot_patch = helper_to_slot(schedule_def, slot_cache, tz)
        if slot_patch is None:
            return
        await self._patch_slot(sn, slot_id, slot_patch)

    async def _get_schedule(self, entity_id: str) -> dict[str, Any] | None:
        collection = await self._ensure_storage_collection()
        if collection is None:
            return None
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(entity_id)
        unique_id = str(entry.unique_id) if entry and entry.unique_id else None
        if not unique_id:
            slot_info = await self._slot_for_entity(entity_id)
            if slot_info:
                unique_id = self._unique_id(*slot_info)
        if not unique_id:
            return None
        item = collection.data.get(unique_id)
        if not isinstance(item, dict):
            return None
        return self._normalize_schedule_item(item)

    async def _patch_slot(
        self, sn: str, slot_id: str, slot_patch: dict[str, Any]
    ) -> None:
        server_timestamp = self._meta_cache.get(sn)
        if not server_timestamp:
            await self._sync_serial(sn)
            server_timestamp = self._meta_cache.get(sn)
        if not server_timestamp:
            _LOGGER.warning(
                "Skipping schedule PATCH for %s: missing server timestamp", sn
            )
            return
        slots = [copy.deepcopy(slot) for slot in self._slot_cache.get(sn, {}).values()]
        for idx, slot in enumerate(slots):
            if str(slot.get("id")) == slot_id:
                slots[idx] = slot_patch
                break
        else:
            return
        try:
            response = await self._coordinator.client.patch_schedules(
                sn, server_timestamp=server_timestamp, slots=slots
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Schedule PATCH failed for %s: %s", sn, err)
            await self._revert_helper(sn, slot_id)
            return
        new_timestamp = None
        if isinstance(response, dict):
            meta = response.get("meta")
            if isinstance(meta, dict):
                new_timestamp = meta.get("serverTimeStamp")
        if new_timestamp:
            self._meta_cache[sn] = new_timestamp
        else:
            # Force a fresh timestamp before the next PATCH if none was returned.
            self._meta_cache[sn] = None
        self._slot_cache.setdefault(sn, {})[slot_id] = slot_patch

    async def _revert_helper(self, sn: str, slot_id: str) -> None:
        slot = self._slot_cache.get(sn, {}).get(slot_id)
        if not slot:
            return
        helper_def = slot_to_helper(
            slot, dt_util.get_time_zone(self.hass.config.time_zone)
        )
        name = self._default_name(sn, slot, helper_def)
        await self._apply_helper(sn, slot_id, helper_def, name)

    async def _sync_serial(self, sn: str) -> None:
        try:
            response = await self._coordinator.client.get_schedules(sn)
        except Exception as err:  # noqa: BLE001
            self._last_error = str(err)
            _LOGGER.warning("Failed to fetch schedules for %s: %s", sn, err)
            return
        self._last_error = None

        meta = response.get("meta") if isinstance(response, dict) else None
        if isinstance(meta, dict):
            self._meta_cache[sn] = meta.get("serverTimeStamp")
        slots = response.get("slots") if isinstance(response, dict) else None
        if not isinstance(slots, list):
            slots = []
        slot_map: dict[str, dict[str, Any]] = {}
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            slot_id = str(slot.get("id") or "")
            if not slot_id:
                continue
            slot_map[slot_id] = slot
        self._slot_cache[sn] = slot_map

        existing = dict(self._mapping.get(sn, {}))
        custom_index = 0
        for slot_id, slot in slot_map.items():
            helper_def = slot_to_helper(
                slot, dt_util.get_time_zone(self.hass.config.time_zone)
            )
            if helper_def.schedule_type == "OFF_PEAK" and not self._show_off_peak():
                await self._remove_helper(sn, slot_id)
                continue
            if helper_def.schedule_type != "OFF_PEAK":
                custom_index += 1
            name = self._default_name(sn, slot, helper_def, custom_index)
            await self._apply_helper(sn, slot_id, helper_def, name)

        for slot_id in set(existing) - set(slot_map):
            await self._remove_helper(sn, slot_id)

        if self._mapping.get(sn) != existing:
            await self._save_mapping()
        self._update_state_listener()

    async def _apply_helper(
        self, sn: str, slot_id: str, helper_def: HelperDefinition, name: str
    ) -> None:
        collection = await self._ensure_storage_collection()
        if collection is None:
            return
        item_id = self._unique_id(sn, slot_id)
        entity_id = self._resolve_entity_id(item_id)
        if entity_id:
            self._suppress_entity(entity_id)
        existing = collection.data.get(item_id)
        payload = dict(helper_def.schedule)
        if existing and isinstance(existing, dict) and existing.get("name"):
            payload["name"] = existing.get("name")
        else:
            payload["name"] = name
        if existing:
            await collection.async_update_item(item_id, payload)
        else:
            await self._create_item_with_id(collection, item_id, payload)
        await self.hass.async_block_till_done()
        entity_id = self._resolve_entity_id(item_id)
        if entity_id:
            self._link_entity(sn, entity_id)
            self._mapping.setdefault(sn, {})[slot_id] = entity_id

    async def _remove_helper(self, sn: str, slot_id: str) -> None:
        collection = await self._ensure_storage_collection()
        if collection is None:
            return
        item_id = self._unique_id(sn, slot_id)
        if item_id in collection.data:
            entity_id = self._resolve_entity_id(item_id)
            if entity_id:
                self._suppress_entity(entity_id)
            await collection.async_delete_item(item_id)
            await self.hass.async_block_till_done()
        self._mapping.get(sn, {}).pop(slot_id, None)

    async def _create_item_with_id(
        self, collection, item_id: str, payload: dict[str, Any]
    ) -> None:
        validated = await collection._process_create_data(payload)
        item = collection._create_item(item_id, validated)
        collection.data[item_id] = item
        collection._async_schedule_save()
        await collection.notify_changes(
            [
                CollectionChange(
                    CHANGE_ADDED,
                    item_id,
                    item,
                    collection._hash_item(collection._serialize_item(item_id, item)),
                )
            ]
        )

    async def _ensure_storage_collection(self):
        if self._storage_collection is not None:
            return self._storage_collection
        await async_setup_component(self.hass, SCHEDULE_DOMAIN, {})
        handlers = self.hass.data.get(websocket_api.DOMAIN) or {}
        handler_entry = handlers.get(f"{SCHEDULE_DOMAIN}/create")
        if not handler_entry:
            return None
        handler = handler_entry[0]
        target = inspect.unwrap(handler)
        self._storage_collection = getattr(
            getattr(target, "__self__", None), "storage_collection", None
        )
        return self._storage_collection

    async def _load_mapping(self) -> None:
        stored = await self._store.async_load() or {}
        mapping: dict[str, dict[str, str]] = {}
        if isinstance(stored, dict):
            for serial, slots in stored.items():
                if not isinstance(slots, dict):
                    continue
                mapping[str(serial)] = {str(k): str(v) for k, v in slots.items()}
        self._mapping = mapping

    async def _save_mapping(self) -> None:
        await self._store.async_save(self._mapping)

    def _sync_enabled(self) -> bool:
        if not self._config_entry:
            return True
        return bool(self._config_entry.options.get(OPT_SCHEDULE_SYNC_ENABLED, True))

    def _show_off_peak(self) -> bool:
        if not self._config_entry:
            return True
        return bool(self._config_entry.options.get(OPT_SCHEDULE_EXPOSE_OFF_PEAK, True))

    def _naming_style(self) -> str:
        if not self._config_entry:
            return DEFAULT_SCHEDULE_NAMING
        return str(
            self._config_entry.options.get(OPT_SCHEDULE_NAMING, DEFAULT_SCHEDULE_NAMING)
        )

    def _has_scheduler_bearer(self) -> bool:
        client = getattr(self._coordinator, "client", None)
        if not client:
            return False
        bearer = getattr(client, "_bearer", None)  # noqa: SLF001
        if bearer is None:
            return False
        if inspect.iscoroutinefunction(bearer):
            return False
        try:
            token = bearer()
        except Exception:
            return False
        if inspect.isawaitable(token):
            if hasattr(token, "close"):
                token.close()
            return False
        return bool(token)

    def _charger_name(self, sn: str) -> str:
        data = (getattr(self._coordinator, "data", {}) or {}).get(sn) or {}
        display_name = data.get("display_name")
        if display_name:
            return str(display_name)
        fallback_name = data.get("name")
        if fallback_name:
            return str(fallback_name)
        return f"Charger {sn}"

    def _default_name(
        self,
        sn: str,
        slot: dict[str, Any],
        helper_def: HelperDefinition,
        index: int | None = None,
    ) -> str:
        charger_name = self._charger_name(sn)
        schedule_type = helper_def.schedule_type or "CUSTOM"
        style = self._naming_style()
        start = slot.get("startTime")
        end = slot.get("endTime")
        time_window = None
        if start and end:
            time_window = f"{start}-{end}"

        if schedule_type == "OFF_PEAK":
            return f"Enphase {charger_name} Off-Peak (read-only)"

        if style == SCHEDULE_NAMING_TIME_WINDOW and time_window:
            return f"Enphase {charger_name} {time_window}"
        if style == SCHEDULE_NAMING_TYPE_TIME_WINDOW and time_window:
            return f"Enphase {charger_name} {schedule_type.title()} {time_window}"

        fallback_index = index if index is not None else 1
        return f"Enphase {charger_name} Schedule {fallback_index}"

    def _unique_id(self, sn: str, slot_id: str) -> str:
        return f"{DOMAIN}:{sn}:schedule:{slot_id}"

    def _resolve_entity_id(self, unique_id: str) -> str | None:
        ent_reg = er.async_get(self.hass)
        return ent_reg.async_get_entity_id(SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id)

    async def _slot_for_entity(self, entity_id: str) -> tuple[str, str] | None:
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(entity_id)
        if entry and entry.unique_id:
            unique_id = str(entry.unique_id)
            prefix = f"{DOMAIN}:"
            if unique_id.startswith(prefix):
                rest = unique_id[len(prefix) :]
                serial, sep, slot_id = rest.partition(":schedule:")
                if sep and serial and slot_id:
                    return serial, slot_id
        for serial, slots in self._mapping.items():
            for slot_id, mapped_entity in slots.items():
                if mapped_entity == entity_id:
                    return serial, slot_id
        return None

    def _link_entity(self, sn: str, entity_id: str) -> None:
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get_device(identifiers={(DOMAIN, sn)})
        if not device:
            return
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(entity_id)
        if entry and entry.device_id != device.id:
            ent_reg.async_update_entity(entity_id, device_id=device.id)

    @callback
    def _suppress_entity(self, entity_id: str) -> None:
        self._suppress_updates.add(entity_id)

        @callback
        def _release(_now) -> None:
            self._suppress_updates.discard(entity_id)

        async_call_later(self.hass, SUPPRESS_SECONDS, _release)

    def _update_state_listener(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        entity_ids = [
            entity_id
            for slots in self._mapping.values()
            for entity_id in slots.values()
        ]
        if entity_ids:
            self._unsub_state = async_track_state_change_event(
                self.hass, entity_ids, self._handle_state_change
            )

    @staticmethod
    def _coerce_time(value: Any) -> dt_time | None:
        if isinstance(value, dt_time):
            return value
        if isinstance(value, str):
            try:
                return dt_time.fromisoformat(value)
            except ValueError:
                return None
        return None

    @classmethod
    def _normalize_schedule_item(cls, item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        for day in CONF_ALL_DAYS:
            entries = item.get(day) or []
            if not isinstance(entries, list):
                continue
            normalized_entries = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                start = cls._coerce_time(entry.get(CONF_FROM))
                end = cls._coerce_time(entry.get(CONF_TO))
                if start is None or end is None:
                    continue
                normalized_entry = dict(entry)
                normalized_entry[CONF_FROM] = start
                normalized_entry[CONF_TO] = end
                normalized_entries.append(normalized_entry)
            normalized[day] = normalized_entries
        return normalized
