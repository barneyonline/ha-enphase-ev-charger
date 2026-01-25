from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, time as dt_time, timedelta
import inspect
import json
import logging
from typing import Any, Callable

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

from .const import DOMAIN, OPT_SCHEDULE_SYNC_ENABLED
from .schedule import (
    HelperDefinition,
    helper_to_slot,
    normalize_slot_payload,
    slot_to_helper,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
SYNC_INTERVAL = timedelta(minutes=5)
SUPPRESS_SECONDS = 2.0
PATCH_REFRESH_DELAY_S = 1.0


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
        self._config_cache: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._storage_collection = None
        self._unsub_interval = None
        self._unsub_state = None
        self._unsub_coordinator = None
        self._suppress_updates: set[str] = set()
        self._listeners: list[Callable[[], None]] = []
        self._disabled_cleanup_done = False
        self._storage_sanitize_done = False
        self._last_sync: datetime | None = None
        self._last_error: str | None = None
        self._last_status: str | None = None
        self._pending_patch_refresh: set[str] = set()

    async def async_start(self) -> None:
        await self._load_mapping()
        await self._ensure_storage_collection()
        self._disabled_cleanup_done = False
        if not self._sync_enabled():
            await self._disable_support()
            return
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
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def _unsub() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsub

    @callback
    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - keep other listeners alive
                _LOGGER.exception("Schedule sync listener error")

    def get_slot(self, sn: str, slot_id: str) -> dict[str, Any] | None:
        return self._slot_cache.get(sn, {}).get(slot_id)

    def get_helper_entity_id(self, sn: str, slot_id: str) -> str | None:
        return self._mapping.get(sn, {}).get(slot_id)

    def iter_slots(self) -> Iterable[tuple[str, str, dict[str, Any]]]:
        for serial, slots in self._slot_cache.items():
            for slot_id, slot in slots.items():
                yield serial, slot_id, slot

    def iter_helper_mappings(self) -> Iterable[tuple[str, str, str]]:
        for serial, slots in self._mapping.items():
            for slot_id, entity_id in slots.items():
                yield serial, slot_id, entity_id

    def is_off_peak_eligible(self, sn: str) -> bool:
        config = self._config_cache.get(sn)
        if not isinstance(config, dict):
            return True
        eligible = config.get("isOffPeakEligible")
        if eligible is None:
            return True
        return bool(eligible)

    @callback
    def _schedule_post_patch_refresh(self, sn: str) -> None:
        if sn in self._pending_patch_refresh:
            return
        self._pending_patch_refresh.add(sn)

        @callback
        def _run(_now) -> None:
            self._pending_patch_refresh.discard(sn)
            self.hass.async_create_task(
                self.async_refresh(reason="patch", serials=[sn])
            )

        async_call_later(self.hass, PATCH_REFRESH_DELAY_S, _run)

    async def _disable_support(self) -> None:
        if self._disabled_cleanup_done:
            return
        self._disabled_cleanup_done = True
        await self.async_stop()
        await self._remove_all_helpers()
        self._slot_cache.clear()
        self._meta_cache.clear()
        self._config_cache.clear()
        self._last_status = "disabled"
        self._notify_listeners()

    async def _remove_all_helpers(self) -> None:
        collection = await self._ensure_storage_collection()
        ent_reg = er.async_get(self.hass)
        slot_keys: set[tuple[str, str]] = set()

        for serial, slots in self._slot_cache.items():
            for slot_id in slots:
                if serial and slot_id:
                    slot_keys.add((serial, slot_id))

        for serial, slots in list(self._mapping.items()):
            for slot_id, entity_id in list(slots.items()):
                if serial and slot_id:
                    slot_keys.add((serial, slot_id))
                schedule_entity_id = entity_id
                if not schedule_entity_id:
                    unique_id = self._unique_id(serial, slot_id)
                    schedule_entity_id = ent_reg.async_get_entity_id(
                        SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id
                    )
                if schedule_entity_id:
                    self._suppress_entity(schedule_entity_id)
                    ent_reg.async_remove(schedule_entity_id)

        if collection is not None:
            for item_id in list(collection.data):
                if not isinstance(item_id, str):
                    continue
                if not item_id.startswith(f"{DOMAIN}:"):
                    continue
                serial, slot_id = self._parse_slot_id(item_id)
                if serial and slot_id:
                    slot_keys.add((serial, slot_id))
                await collection.async_delete_item(item_id)
            await self.hass.async_block_till_done()

        for serial, slot_id in slot_keys:
            switch_unique_id = f"{DOMAIN}:{serial}:schedule:{slot_id}:enabled"
            switch_entity_id = ent_reg.async_get_entity_id(
                "switch", DOMAIN, switch_unique_id
            )
            if switch_entity_id:
                ent_reg.async_remove(switch_entity_id)

        known_serials: set[str] = set()
        serial_provider = getattr(self._coordinator, "iter_serials", None)
        if callable(serial_provider):
            try:
                known_serials = {str(sn) for sn in serial_provider() if sn}
            except Exception:
                known_serials = set()
        if not known_serials:
            serials = getattr(self._coordinator, "serials", None)
            if isinstance(serials, (list, set, tuple)):
                known_serials = {str(sn) for sn in serials if sn}

        for entry in list(ent_reg.entities.values()):
            entry_domain = getattr(entry, "domain", None)
            if entry_domain is None:
                entry_domain = entry.entity_id.partition(".")[0]
            if entry_domain != "switch":
                continue
            entry_platform = getattr(entry, "platform", None)
            if entry_platform is not None and entry_platform != DOMAIN:
                continue
            unique_id = entry.unique_id or ""
            if (
                not unique_id.startswith(f"{DOMAIN}:")
                or ":schedule:" not in unique_id
                or not unique_id.endswith(":enabled")
            ):
                continue
            entry_config_id = getattr(entry, "config_entry_id", None)
            if (
                self._config_entry is not None
                and entry_config_id is not None
                and entry_config_id != self._config_entry.entry_id
            ):
                continue
            base_unique_id = unique_id[: -len(":enabled")]
            serial, slot_id = self._parse_slot_id(base_unique_id)
            if serial is None or slot_id is None:
                continue
            if known_serials and serial not in known_serials:
                continue
            ent_reg.async_remove(entry.entity_id)

        self._mapping = {}
        await self._save_mapping()

    @staticmethod
    def _parse_slot_id(unique_id: str) -> tuple[str | None, str | None]:
        prefix = f"{DOMAIN}:"
        if not unique_id.startswith(prefix):
            return None, None
        rest = unique_id[len(prefix) :]
        serial, sep, slot_id = rest.partition(":schedule:")
        if not sep or not serial or not slot_id:
            return None, None
        return serial, slot_id

    async def async_set_slot_enabled(
        self, sn: str, slot_id: str, enabled: bool
    ) -> None:
        if not self._sync_enabled():
            return
        slot = self._slot_cache.get(sn, {}).get(slot_id)
        if not slot:
            return
        schedule_type = str(slot.get("scheduleType") or "")
        if schedule_type == "OFF_PEAK" and not self.is_off_peak_eligible(sn):
            _LOGGER.debug(
                "Skipping OFF_PEAK toggle for %s: not eligible for off-peak schedules",
                sn,
            )
            return
        slot_states: dict[str, bool] = {}
        for cached_id, cached_slot in self._slot_cache.get(sn, {}).items():
            desired = bool(cached_slot.get("enabled", True))
            if cached_id == slot_id:
                desired = bool(enabled)
            slot_states[str(cached_id)] = desired
        if not slot_states:
            return
        try:
            response = await self._coordinator.client.patch_schedule_states(
                sn, slot_states=slot_states
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Schedule state PATCH failed for %s: %s", sn, err)
            return
        for cached_id, desired in slot_states.items():
            cached_slot = self._slot_cache.get(sn, {}).get(cached_id)
            if cached_slot is not None:
                cached_slot["enabled"] = bool(desired)
        needs_refresh = True
        if isinstance(response, dict):
            meta = response.get("meta")
            if isinstance(meta, dict) and meta.get("serverTimeStamp"):
                self._meta_cache[sn] = meta.get("serverTimeStamp")
            data = response.get("data")
            if isinstance(data, dict):
                config = data.get("config")
                if isinstance(config, dict):
                    self._config_cache[sn] = config
                slots = data.get("slots")
                if isinstance(slots, list):
                    slot_map: dict[str, dict[str, Any]] = {}
                    for slot_item in slots:
                        if not isinstance(slot_item, dict):
                            continue
                        cached_slot_id = str(slot_item.get("id") or "")
                        if not cached_slot_id:
                            continue
                        slot_map[cached_slot_id] = slot_item
                    if slot_map:
                        self._slot_cache[sn] = slot_map
                        needs_refresh = False
            elif isinstance(data, list):
                slot_map = {}
                for slot_item in data:
                    if not isinstance(slot_item, dict):
                        continue
                    cached_slot_id = str(slot_item.get("id") or "")
                    if not cached_slot_id:
                        continue
                    slot_map[cached_slot_id] = slot_item
                if slot_map:
                    self._slot_cache[sn] = slot_map
                    needs_refresh = False
        if needs_refresh:
            self._schedule_post_patch_refresh(sn)
        self._notify_listeners()

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
            await self._disable_support()
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
        if not self._slot_payload_changed(slot_cache, slot_patch):
            return
        if not slot_cache.get("enabled", True):
            slot_patch["enabled"] = True
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
        if slot_id not in self._slot_cache.get(sn, {}):
            return
        try:
            slot_patch = normalize_slot_payload(slot_patch)
            slot_patch["id"] = str(slot_id)
            response = await self._coordinator.client.patch_schedule(
                sn, slot_id, slot_patch
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
            data = response.get("data")
            if isinstance(data, dict):
                inner_meta = data.get("meta")
                if isinstance(inner_meta, dict) and not new_timestamp:
                    new_timestamp = inner_meta.get("serverTimeStamp")
        if new_timestamp:
            self._meta_cache[sn] = new_timestamp
        self._slot_cache.setdefault(sn, {})[slot_id] = slot_patch
        self._schedule_post_patch_refresh(sn)
        self._notify_listeners()

    async def _revert_helper(self, sn: str, slot_id: str) -> None:
        slot = self._slot_cache.get(sn, {}).get(slot_id)
        if not slot:
            return
        helper_def = slot_to_helper(
            slot, dt_util.get_time_zone(self.hass.config.time_zone)
        )
        name = self._default_name(sn, slot, helper_def)
        await self._apply_helper(
            sn,
            slot_id,
            helper_def,
            name,
            previous_default_name=name,
        )

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
        config = response.get("config") if isinstance(response, dict) else None
        if isinstance(config, dict):
            self._config_cache[sn] = config
        else:
            self._config_cache.pop(sn, None)
        prev_slots = self._slot_cache.get(sn)
        if not isinstance(prev_slots, dict):
            prev_slots = {}
        else:
            prev_slots = dict(prev_slots)
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        prev_indexes = self._custom_slot_indexes(prev_slots, tz)

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
            helper_def = slot_to_helper(slot, tz)
            if helper_def.schedule_type == "OFF_PEAK" and not self._show_off_peak():
                await self._remove_helper(sn, slot_id)
                continue
            if helper_def.schedule_type != "OFF_PEAK":
                custom_index += 1
            name = self._default_name(sn, slot, helper_def, custom_index)
            previous_default_name = None
            prev_slot = prev_slots.get(slot_id)
            if prev_slot:
                prev_helper_def = slot_to_helper(prev_slot, tz)
                prev_index = prev_indexes.get(slot_id)
                previous_default_name = self._default_name(
                    sn, prev_slot, prev_helper_def, prev_index
                )
            await self._apply_helper(
                sn,
                slot_id,
                helper_def,
                name,
                previous_default_name=previous_default_name,
            )

        for slot_id in set(existing) - set(slot_map):
            await self._remove_helper(sn, slot_id)

        if self._mapping.get(sn) != existing:
            await self._save_mapping()
        self._update_state_listener()
        self._notify_listeners()

    async def _apply_helper(
        self,
        sn: str,
        slot_id: str,
        helper_def: HelperDefinition,
        name: str,
        previous_default_name: str | None = None,
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
        existing_name = None
        if existing and isinstance(existing, dict):
            existing_name = existing.get("name")
        if existing_name:
            if previous_default_name and existing_name == previous_default_name:
                payload["name"] = name
            else:
                payload["name"] = existing_name
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
        if not self._storage_sanitize_done:
            self._storage_sanitize_done = True
            await self._sanitize_schedule_storage()
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

    async def _sanitize_schedule_storage(self) -> bool:
        path = self.hass.config.path(".storage", SCHEDULE_DOMAIN)

        def _load():
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except FileNotFoundError:
                return None
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to load schedule storage: %s", err)
                return None

        raw = await self.hass.async_add_executor_job(_load)
        if not isinstance(raw, dict):
            return False
        data = raw.get("data")
        if not isinstance(data, dict):
            return False
        items = data.get("items")
        if not isinstance(items, list):
            return False
        changed = False

        for item in items:
            if not isinstance(item, dict):
                continue
            for day in CONF_ALL_DAYS:
                entries = item.get(day)
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    for key in (CONF_FROM, CONF_TO):
                        value = entry.get(key)
                        if not isinstance(value, str) or "." not in value:
                            continue
                        trimmed = value.split(".", 1)[0]
                        try:
                            dt_time.fromisoformat(trimmed)
                        except ValueError:
                            continue
                        entry[key] = trimmed
                        changed = True

        if not changed:
            return False

        def _save():
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(raw, handle, ensure_ascii=False, indent=2)
                return True
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to write schedule storage: %s", err)
                return False

        saved = await self.hass.async_add_executor_job(_save)
        if saved:
            _LOGGER.warning(
                "Sanitized schedule storage times with microseconds for %s", path
            )
        return saved

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
        return bool(self._config_entry.options.get(OPT_SCHEDULE_SYNC_ENABLED, False))

    def _show_off_peak(self) -> bool:
        return False

    def _has_scheduler_bearer(self) -> bool:
        client = getattr(self._coordinator, "client", None)
        if not client:
            return False
        control_headers = None
        control_fn = getattr(client, "_control_headers", None)
        if callable(control_fn):
            if inspect.iscoroutinefunction(control_fn):
                return False
            try:
                control_headers = control_fn()
            except Exception:
                control_headers = None
        if isinstance(control_headers, dict) and control_headers.get("Authorization"):
            return True
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

    def _custom_slot_indexes(
        self, slots: dict[str, dict[str, Any]], tz
    ) -> dict[str, int]:
        custom_index = 0
        indexes: dict[str, int] = {}
        for slot_id, slot in slots.items():
            helper_def = slot_to_helper(slot, tz)
            if helper_def.schedule_type == "OFF_PEAK":
                continue
            custom_index += 1
            indexes[str(slot_id)] = custom_index
        return indexes

    def _default_name(
        self,
        sn: str,
        slot: dict[str, Any],
        helper_def: HelperDefinition,
        index: int | None = None,
    ) -> str:
        charger_name = self._charger_name(sn)
        schedule_type = helper_def.schedule_type or "CUSTOM"
        start = slot.get("startTime")
        end = slot.get("endTime")
        time_window = None
        if start and end:
            time_window = f"{start}-{end}"

        if schedule_type == "OFF_PEAK":
            return f"Enphase {charger_name} Off-Peak (read-only)"

        if time_window:
            return f"Enphase {charger_name} {time_window}"

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
    def _normalized_slot_payload(slot: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_slot_payload(slot)
        normalized.pop("enabled", None)
        for key in list(normalized):
            if normalized[key] is None:
                normalized.pop(key)
        return normalized

    def _slot_payload_changed(
        self, slot_cache: dict[str, Any], slot_patch: dict[str, Any]
    ) -> bool:
        return self._normalized_slot_payload(
            slot_cache
        ) != self._normalized_slot_payload(slot_patch)

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
