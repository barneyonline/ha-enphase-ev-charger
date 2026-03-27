from __future__ import annotations

import inspect
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as _tz
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .device_types import (
    member_is_retired as device_member_is_retired,
    normalize_type_key,
    sanitize_member,
    type_display_label,
)
from .log_redaction import redact_site_id, redact_text
from .state_models import install_state_descriptors

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)

HEMS_SUPPORT_PREFLIGHT_CACHE_TTL = 15.0
DEVICES_INVENTORY_CACHE_TTL = 300.0
HEMS_DEVICES_STALE_AFTER_S = 90.0
HEMS_DEVICES_CACHE_TTL = 15.0
SYSTEM_DASHBOARD_DIAGNOSTIC_TYPES: tuple[str, ...] = (
    "envoys",
    "meters",
    "enpowers",
    "encharges",
    "modems",
    "inverters",
)


@dataclass(frozen=True)
class CoordinatorTopologySnapshot:
    charger_serials: tuple[str, ...]
    battery_serials: tuple[str, ...]
    inverter_serials: tuple[str, ...]
    active_type_keys: tuple[str, ...]
    gateway_iq_router_keys: tuple[str, ...]
    inventory_ready: bool


class InventoryRuntime:
    """Inventory, topology, and system-dashboard runtime helpers."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self.discovery_state = coordinator.discovery_state
        self.refresh_state = coordinator.refresh_state
        self.inventory_state = coordinator.inventory_state
        self.heatpump_state = coordinator.heatpump_state

    @property
    def client(self):
        return self.coordinator.client

    @property
    def site_id(self) -> str:
        return self.coordinator.site_id

    @property
    def include_inverters(self) -> bool:
        return self.coordinator.include_inverters

    def iter_serials(self) -> list[str]:
        return self.coordinator.iter_serials()

    def iter_battery_serials(self) -> list[str]:
        return self.coordinator.iter_battery_serials()

    def iter_type_keys(self) -> list[str]:
        return self.coordinator.iter_type_keys()

    def gateway_iq_energy_router_records(self) -> list[dict[str, object]]:
        return self.coordinator.gateway_iq_energy_router_records()

    def type_bucket(self, type_key: object) -> dict[str, object] | None:
        return self.coordinator.type_bucket(type_key)

    def _type_member_text(self, member: dict[str, object], *keys: str) -> str | None:
        return self.coordinator._type_member_text(member, *keys)

    def _coerce_optional_text(self, value: object) -> str | None:
        return self.coordinator._coerce_optional_text(value)

    def _coerce_optional_bool(self, value: object) -> bool | None:
        return self.coordinator._coerce_optional_bool(value)

    def _coerce_int(self, value: object, *, default: int = 0) -> int:
        return self.coordinator._coerce_int(value, default=default)

    def _copy_diagnostics_value(self, value: object) -> object:
        return self.coordinator._copy_diagnostics_value(value)

    def _normalize_iso_date(self, value: object) -> str | None:
        return self.coordinator._normalize_iso_date(value)

    def _site_local_current_date(self) -> str:
        return self.coordinator._site_local_current_date()

    def _redact_battery_payload(self, payload: object) -> object:
        return self.coordinator._redact_battery_payload(payload)

    def _debug_log_summary_if_changed(
        self, summary_key: str, log_label: str, summary: object
    ) -> None:
        self.coordinator._debug_log_summary_if_changed(summary_key, log_label, summary)

    def _debug_devices_inventory_summary(
        self,
        grouped: dict[str, dict[str, object]],
        ordered_keys: list[str],
    ) -> dict[str, object]:
        return self.coordinator._debug_devices_inventory_summary(grouped, ordered_keys)

    def _debug_hems_inventory_summary(self) -> dict[str, object]:
        return self.coordinator._debug_hems_inventory_summary()

    def _debug_system_dashboard_summary(
        self,
        tree_payload: dict[str, object] | None,
        details_payloads: dict[str, dict[str, dict[str, object]]],
        type_summaries: dict[str, dict[str, object]],
        hierarchy_summary: dict[str, object],
    ) -> dict[str, object]:
        return self.coordinator._debug_system_dashboard_summary(
            tree_payload,
            details_payloads,
            type_summaries,
            hierarchy_summary,
        )

    def _debug_topology_summary(
        self, snapshot: CoordinatorTopologySnapshot
    ) -> dict[str, object]:
        return self.coordinator._debug_topology_summary(snapshot)

    def _build_system_dashboard_summaries(
        self,
        tree_payload: dict[str, object] | None,
        details_payloads: dict[str, dict[str, dict[str, object]]],
    ) -> tuple[
        dict[str, dict[str, object]],
        dict[str, object],
        dict[str, dict[str, object]],
    ]:
        return self.coordinator._build_system_dashboard_summaries(
            tree_payload, details_payloads
        )

    def _system_dashboard_type_key(self, raw_type: object) -> str | None:
        return self.coordinator._system_dashboard_type_key(raw_type)

    def topology_snapshot(self) -> CoordinatorTopologySnapshot:
        """Return the latest cached topology snapshot."""
        return self._topology_snapshot_cache

    def gateway_inventory_summary(self) -> dict[str, object]:
        source = self._gateway_inventory_summary_marker()
        summary = getattr(self, "_gateway_inventory_summary_cache", {}) or {}
        if not summary or source != self._gateway_inventory_summary_source:
            summary = self._build_gateway_inventory_summary()
            self._gateway_inventory_summary_cache = summary
            self._gateway_inventory_summary_source = source
        return dict(summary)

    def microinverter_inventory_summary(self) -> dict[str, object]:
        source = self._microinverter_inventory_summary_marker()
        summary = getattr(self, "_microinverter_inventory_summary_cache", {}) or {}
        if not summary or source != self._microinverter_inventory_summary_source:
            summary = self._build_microinverter_inventory_summary()
            self._microinverter_inventory_summary_cache = summary
            self._microinverter_inventory_summary_source = source
        return dict(summary)

    def heatpump_inventory_summary(self) -> dict[str, object]:
        source = self._heatpump_inventory_summary_marker()
        summary = getattr(self, "_heatpump_inventory_summary_cache", {}) or {}
        if not summary or source != self._heatpump_inventory_summary_source:
            summary = self._build_heatpump_inventory_summary()
            self._heatpump_inventory_summary_cache = summary
            self._heatpump_inventory_summary_source = source
        return dict(summary)

    def heatpump_type_summary(self, device_type: str) -> dict[str, object]:
        try:
            normalized = str(device_type).strip().upper()
        except Exception:  # noqa: BLE001
            normalized = ""
        source = self._heatpump_inventory_summary_marker()
        summaries = getattr(self, "_heatpump_type_summaries_cache", {}) or {}
        if source != self._heatpump_type_summaries_source or (
            normalized and normalized not in summaries
        ):
            summaries = self._build_heatpump_type_summaries()
            self._heatpump_type_summaries_cache = summaries
            self._heatpump_type_summaries_source = source
        summary = summaries.get(normalized, {})
        return dict(summary) if isinstance(summary, dict) else {}

    def gateway_iq_energy_router_summary_records(self) -> list[dict[str, object]]:
        source = self._gateway_iq_energy_router_records_marker()
        records = getattr(self, "_gateway_iq_energy_router_records_cache", [])
        if not records or source != self._gateway_iq_energy_router_records_source:
            records = self._gateway_iq_energy_router_summary_records(
                self.gateway_iq_energy_router_records()
            )
            self._gateway_iq_energy_router_records_cache = records
            self._gateway_iq_energy_router_records_source = source
            self._gateway_iq_energy_router_records_by_key_cache = {
                record["key"]: record
                for record in records
                if isinstance(record, dict) and isinstance(record.get("key"), str)
            }
        return [dict(record) for record in records if isinstance(record, dict)]

    @staticmethod
    def _router_record_key(record: object) -> str | None:
        if not isinstance(record, dict):
            return None
        key = record.get("key")
        if key is None:
            return None
        try:
            key_text = str(key).strip()
        except Exception:  # noqa: BLE001
            return None
        return key_text or None

    def gateway_iq_energy_router_record(
        self, router_key: object
    ) -> dict[str, object] | None:
        try:
            key = str(router_key).strip()
        except Exception:  # noqa: BLE001
            return None
        if not key:
            return None
        self.gateway_iq_energy_router_summary_records()
        record = getattr(
            self, "_gateway_iq_energy_router_records_by_key_cache", {}
        ).get(key)
        return dict(record) if isinstance(record, dict) else None

    def _current_topology_snapshot(self) -> CoordinatorTopologySnapshot:
        router_records = self.gateway_iq_energy_router_summary_records()
        router_keys = tuple(
            key
            for key in (self._router_record_key(record) for record in router_records)
            if key
        )
        return CoordinatorTopologySnapshot(
            charger_serials=tuple(self.iter_serials()),
            battery_serials=tuple(self.iter_battery_serials()),
            inverter_serials=tuple(self.iter_inverter_serials()),
            active_type_keys=tuple(self.iter_type_keys()),
            gateway_iq_router_keys=router_keys,
            inventory_ready=bool(
                getattr(self, "_devices_inventory_ready", False)
                or getattr(self, "_hems_inventory_ready", False)
            ),
        )

    @callback
    def _notify_topology_listeners(self) -> None:
        for listener in list(self._topology_listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Topology listener failed for site %s",
                    redact_site_id(self.site_id),
                    exc_info=True,
                )

    @callback
    def _refresh_cached_topology(self) -> bool:
        if self._topology_refresh_suppressed > 0:
            self._topology_refresh_pending = True
            return False
        try:
            self._rebuild_inventory_summary_caches()
            snapshot = self._current_topology_snapshot()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Skipping topology cache rebuild for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            return False
        if snapshot == self._topology_snapshot_cache:
            return False
        self._topology_snapshot_cache = snapshot
        self._debug_log_summary_if_changed(
            "topology",
            "Discovery topology summary updated",
            self._debug_topology_summary(snapshot),
        )
        self._notify_topology_listeners()
        return True

    @callback
    def _begin_topology_refresh_batch(self) -> None:
        self._topology_refresh_suppressed += 1

    @callback
    def _end_topology_refresh_batch(self) -> bool:
        if self._topology_refresh_suppressed > 0:
            self._topology_refresh_suppressed -= 1
        if self._topology_refresh_suppressed > 0 or not self._topology_refresh_pending:
            return False
        self._topology_refresh_pending = False
        override = getattr(self.coordinator, "__dict__", {}).get(
            "_refresh_cached_topology"
        )
        if callable(override):
            return bool(override())
        return self._refresh_cached_topology()

    async def async_ensure_system_dashboard_diagnostics(self) -> None:
        if (
            self._system_dashboard_type_summaries
            or self._system_dashboard_hierarchy_summary
        ):
            return
        override = getattr(self.coordinator, "__dict__", {}).get(
            "_async_refresh_system_dashboard"
        )
        if callable(override):
            await override(force=True)
            return
        await self._async_refresh_system_dashboard(force=True)

    @staticmethod
    async def _async_call_refreshable_fetcher(
        fetcher, *, force: bool = False
    ) -> object:
        if not force:
            return await fetcher()
        try:
            signature = inspect.signature(fetcher)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            if "refresh_data" in signature.parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            ):
                return await fetcher(refresh_data=True)
            return await fetcher()
        return await fetcher()

    async def _async_refresh_hems_support_preflight(
        self, *, force: bool = False
    ) -> None:
        if getattr(self.client, "hems_site_supported", None) is not None:
            return

        now = time.monotonic()
        if not force and self._hems_support_preflight_cache_until is not None:
            if now < self._hems_support_preflight_cache_until:
                return

        fetcher = getattr(self.client, "system_dashboard_summary", None)
        if not callable(fetcher):
            self._hems_support_preflight_cache_until = (
                now + HEMS_SUPPORT_PREFLIGHT_CACHE_TTL
            )
            return

        try:
            payload = await self._async_call_refreshable_fetcher(fetcher, force=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "HEMS support preflight failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            self._hems_support_preflight_cache_until = (
                now + HEMS_SUPPORT_PREFLIGHT_CACHE_TTL
            )
            return

        if isinstance(payload, dict):
            is_hems = self._coerce_optional_bool(payload.get("is_hems"))
            if is_hems is not None:
                self.client._hems_site_supported = is_hems  # noqa: SLF001

        self._hems_support_preflight_cache_until = (
            now + HEMS_SUPPORT_PREFLIGHT_CACHE_TTL
        )

    def _parse_devices_inventory_payload(
        self, payload: object
    ) -> tuple[bool, dict[str, dict[str, object]], list[str]]:
        if isinstance(payload, list):
            result = payload
        elif isinstance(payload, dict):
            result = payload.get("result")
        else:
            return False, {}, []
        if not isinstance(result, list):
            return False, {}, []

        grouped: dict[str, dict[str, object]] = {}
        seen_per_type: dict[str, set[str]] = {}
        ordered_keys: list[str] = []

        def _clean_text(value: object) -> str | None:
            if value is None:
                return None
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                return None
            return text or None

        def _dry_contact_member_dedupe_key(
            raw_type: object,
            member: dict[str, object],
            member_index: int,
        ) -> str:
            source_type = _clean_text(raw_type)
            serial = _clean_text(
                member.get("serial_number")
                if member.get("serial_number") is not None
                else (
                    member.get("serial")
                    if member.get("serial") is not None
                    else (
                        member.get("serialNumber")
                        if member.get("serialNumber") is not None
                        else member.get("device_sn")
                    )
                )
            )
            identity_parts: list[str] = []
            for key in (
                "device_uid",
                "device-uid",
                "uid",
                "contact_id",
                "contactId",
                "id",
                "channel_type",
                "channelType",
                "meter_type",
            ):
                value = _clean_text(member.get(key))
                if value is None:
                    continue
                identity_parts.append(f"{key}:{value}")
            if serial is not None:
                identity_parts.append(f"serial:{serial}")
            if identity_parts:
                if source_type is not None:
                    identity_parts.insert(0, f"source:{source_type}")
                return "|".join(identity_parts)

            fingerprint_parts: list[str] = []
            for key in sorted(member):
                value = member.get(key)
                if value is None or not isinstance(value, (str, int, float, bool)):
                    continue
                fingerprint_parts.append(f"{key}:{value}")
            if fingerprint_parts:
                fingerprint = "|".join(fingerprint_parts)
                if source_type is not None:
                    return f"source:{source_type}|{fingerprint}|idx:{member_index}"
                return f"{fingerprint}|idx:{member_index}"

            if source_type is not None:
                return f"source:{source_type}|idx:{member_index}"
            return f"idx:{member_index}:dry_contact"

        for bucket in result:
            if not isinstance(bucket, dict):
                continue
            raw_type = bucket.get("type")
            if raw_type is None:
                raw_type = bucket.get("deviceType")
            if raw_type is None:
                raw_type = bucket.get("device_type")
            type_key = normalize_type_key(raw_type)
            devices = bucket.get("devices")
            if not isinstance(devices, list):
                devices = bucket.get("items")
            if not isinstance(devices, list):
                devices = bucket.get("members")
            if not type_key or not isinstance(devices, list):
                continue
            if type_key not in grouped:
                grouped[type_key] = {
                    "type_key": type_key,
                    "type_label": type_display_label(type_key),
                    "count": 0,
                    "devices": [],
                }
                seen_per_type[type_key] = set()
                ordered_keys.append(type_key)
            members: list[dict[str, object]] = grouped[type_key]["devices"]  # type: ignore[assignment]
            seen_keys = seen_per_type[type_key]
            for member_index, member in enumerate(devices):
                if not isinstance(member, dict):
                    continue
                if self.member_is_retired(member):
                    continue
                sanitized = sanitize_member(member)
                if not sanitized:
                    continue
                if type_key == "dry_contact":
                    dedupe_key = _dry_contact_member_dedupe_key(
                        raw_type, sanitized, member_index
                    )
                else:
                    serial = sanitized.get("serial_number")
                    name = sanitized.get("name")
                    if isinstance(serial, str) and serial.strip():
                        dedupe_key = f"sn:{serial.strip()}"
                    elif isinstance(name, str) and name.strip():
                        dedupe_key = f"name:{name.strip()}"
                    else:
                        dedupe_key = f"idx:{len(members)}:{type_key}"
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                members.append(sanitized)

        valid = True
        for type_key, bucket in grouped.items():
            members = bucket.get("devices")
            count = len(members) if isinstance(members, list) else 0
            bucket["count"] = count
            bucket["type_label"] = bucket.get("type_label") or type_display_label(
                type_key
            )
            if type_key == "encharge" and isinstance(members, list):
                name_counts: dict[str, int] = {}
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    raw_name = member.get("name")
                    if raw_name is None:
                        continue
                    try:
                        name_text = str(raw_name).strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if not name_text:
                        continue
                    name_counts[name_text] = name_counts.get(name_text, 0) + 1
                if name_counts:
                    bucket["model_counts"] = dict(name_counts)
                    summary = self._format_inverter_model_summary(name_counts)
                    if isinstance(summary, str) and summary.strip():
                        bucket["model_summary"] = summary

        return valid, dict(grouped), list(dict.fromkeys(ordered_keys))

    def _set_type_device_buckets(
        self,
        grouped: dict[str, dict[str, object]],
        ordered_keys: list[str],
        *,
        authoritative: bool = True,
    ) -> None:
        normalized_order = [
            key
            for key in ordered_keys
            if key in grouped
            and isinstance(grouped[key].get("devices"), list)
            and int(grouped[key].get("count", 0)) > 0
        ]
        self._type_device_buckets = {
            key: value
            for key, value in grouped.items()
            if int(value.get("count", 0)) > 0
        }
        self._type_device_order = normalized_order
        if authoritative:
            self._devices_inventory_ready = True

    @staticmethod
    def _devices_inventory_buckets(payload: object) -> list[dict[str, object]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        result = payload.get("result")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        wrapped = payload.get("value")
        if isinstance(wrapped, dict):
            wrapped_result = wrapped.get("result")
            if isinstance(wrapped_result, list):
                return [item for item in wrapped_result if isinstance(item, dict)]
        return []

    @staticmethod
    def _hems_devices_groups(payload: object) -> list[dict[str, object]]:
        if not isinstance(payload, dict):
            return []
        result = payload.get("result")
        if isinstance(result, dict):
            devices = result.get("devices")
            if isinstance(devices, list):
                return [grouped for grouped in devices if isinstance(grouped, dict)]
            if isinstance(devices, dict):
                return [devices]
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        hems_devices = (
            data.get("hems-devices")
            if data.get("hems-devices") is not None
            else data.get("hems_devices")
        )
        if not isinstance(hems_devices, dict):
            return []
        return [hems_devices]

    @classmethod
    def _legacy_hems_devices_groups(cls, payload: object) -> list[dict[str, object]]:
        groups: list[dict[str, object]] = []
        for bucket in cls._devices_inventory_buckets(payload):
            bucket_type = cls._hems_bucket_type(
                bucket.get("type")
                if bucket.get("type") is not None
                else (
                    bucket.get("deviceType")
                    if bucket.get("deviceType") is not None
                    else bucket.get("device_type")
                )
            )
            if bucket_type != "hemsdevices":
                continue
            grouped_devices = bucket.get("devices")
            if not isinstance(grouped_devices, list):
                continue
            groups.extend(
                grouped for grouped in grouped_devices if isinstance(grouped, dict)
            )
        return groups

    def _hems_grouped_devices(self) -> list[dict[str, object]]:
        groups = self._hems_devices_groups(getattr(self, "_hems_devices_payload", None))
        if groups:
            return groups
        return self._legacy_hems_devices_groups(
            getattr(self, "_devices_inventory_payload", None)
        )

    @staticmethod
    def _normalize_hems_member(member: dict[str, object]) -> dict[str, object]:
        normalized: dict[str, object] = dict(member)
        alias_pairs = (
            ("device-type", "device_type"),
            ("deviceType", "device_type"),
            ("device-uid", "device_uid"),
            ("deviceUid", "device_uid"),
            ("last-report", "last_report"),
            ("lastReport", "last_report"),
            ("last-reported", "last_reported"),
            ("lastReported", "last_reported"),
            ("last-reported-at", "last_reported_at"),
            ("lastReportedAt", "last_reported_at"),
            ("firmware-version", "firmware_version"),
            ("firmwareVersion", "firmware_version"),
            ("software-version", "software_version"),
            ("softwareVersion", "software_version"),
            ("hardware-version", "hardware_version"),
            ("hardwareVersion", "hardware_version"),
            ("hardware-sku", "hardware_sku"),
            ("hardwareSku", "hardware_sku"),
            ("part-number", "part_number"),
            ("partNumber", "part_number"),
            ("hems-device-id", "hems_device_id"),
            ("hems-device-facet-id", "hems_device_facet_id"),
            ("pairing-status", "pairing_status"),
            ("device-state", "device_state"),
            ("iqer-uid", "iqer_uid"),
            ("ip-address", "ip_address"),
            ("created-at", "created_at"),
            ("fvt-time", "fvt_time"),
        )
        for source, dest in alias_pairs:
            if dest not in normalized and source in normalized:
                normalized[dest] = normalized[source]
        if "status_text" not in normalized and "statusText" in normalized:
            normalized["status_text"] = normalized.get("statusText")
        if "serial_number" not in normalized and "serial" in normalized:
            normalized["serial_number"] = normalized.get("serial")
        if "uid" not in normalized and "device_uid" in normalized:
            normalized["uid"] = normalized.get("device_uid")
        return normalized

    @staticmethod
    def _normalize_heatpump_member(member: dict[str, object]) -> dict[str, object]:
        return InventoryRuntime._normalize_hems_member(member)

    def _extract_hems_group_members(
        self,
        groups: list[dict[str, object]],
        requested_keys: set[str],
    ) -> tuple[bool, list[dict[str, object]]]:
        members: list[dict[str, object]] = []
        seen_keys: set[str] = set()
        found_group = False
        for grouped in groups:
            for group_key in requested_keys:
                if group_key in grouped:
                    found_group = True
                raw_members = grouped.get(group_key)
                if not isinstance(raw_members, list):
                    continue
                for raw_member in raw_members:
                    if not isinstance(raw_member, dict):
                        continue
                    if self.member_is_retired(raw_member):
                        continue
                    normalized = self._normalize_hems_member(raw_member)
                    if not normalized:
                        continue
                    dedupe = (
                        self._type_member_text(
                            normalized, "device_uid", "uid", "serial_number", "name"
                        )
                        or f"idx:{len(members)}"
                    )
                    if dedupe in seen_keys:
                        continue
                    seen_keys.add(dedupe)
                    members.append(normalized)
        return found_group, members

    def _hems_group_members(self, *group_keys: str) -> list[dict[str, object]]:
        requested_keys = {key for key in group_keys if key}
        dedicated_found, dedicated_members = self._extract_hems_group_members(
            self._hems_devices_groups(getattr(self, "_hems_devices_payload", None)),
            requested_keys,
        )
        if dedicated_found:
            return dedicated_members
        _legacy_found, legacy_members = self._extract_hems_group_members(
            self._legacy_hems_devices_groups(
                getattr(self, "_devices_inventory_payload", None)
            ),
            requested_keys,
        )
        return legacy_members

    @staticmethod
    def _hems_bucket_type(raw_type: object) -> str | None:
        normalized = normalize_type_key(raw_type)
        if normalized:
            return normalized.replace("_", "")
        try:
            text = str(raw_type).strip().lower()
        except Exception:
            return None
        if not text:
            return None
        return "".join(ch for ch in text if ch.isalnum())

    @staticmethod
    def _heatpump_member_device_type(member: dict[str, object] | None) -> str | None:
        if not isinstance(member, dict):
            return None
        raw = (
            member.get("device_type")
            if member.get("device_type") is not None
            else member.get("device-type")
        )
        if raw is None:
            return None
        try:
            text = str(raw).strip()
        except Exception:
            return None
        return text.upper() if text else None

    @staticmethod
    def _heatpump_worst_status_text(status_counts: dict[str, int]) -> str | None:
        if int(status_counts.get("error", 0) or 0) > 0:
            return "Error"
        if int(status_counts.get("warning", 0) or 0) > 0:
            return "Warning"
        if int(status_counts.get("not_reporting", 0) or 0) > 0:
            return "Not Reporting"
        if int(status_counts.get("unknown", 0) or 0) > 0:
            return "Unknown"
        if int(status_counts.get("normal", 0) or 0) > 0:
            return "Normal"
        return None

    def _merge_heatpump_type_bucket(self) -> None:
        ready_before = bool(getattr(self, "_devices_inventory_ready", False))
        buckets = dict(getattr(self, "_type_device_buckets", {}) or {})
        ordered = list(getattr(self, "_type_device_order", []) or [])
        key = "heatpump"

        members_out = self._hems_group_members("heat-pump", "heat_pump", "heatpump")
        if members_out:
            status_counts: dict[str, int] = {
                "total": len(members_out),
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            }
            device_type_counts: dict[str, int] = {}
            model_counts: dict[str, int] = {}
            firmware_counts: dict[str, int] = {}
            latest_reported: datetime | None = None
            latest_reported_device: dict[str, object] | None = None
            overall_status_text: str | None = None

            for member in members_out:
                device_type = self._heatpump_member_device_type(member) or "UNKNOWN"
                device_type_counts[device_type] = (
                    device_type_counts.get(device_type, 0) + 1
                )
                status_text = self._heatpump_status_text(member)
                normalized_status = self._normalize_inverter_status(status_text)
                status_counts[normalized_status] = (
                    int(status_counts.get(normalized_status, 0)) + 1
                )
                if device_type == "HEAT_PUMP" and status_text:
                    overall_status_text = status_text

                model = self._type_member_text(
                    member,
                    "model",
                    "model_id",
                    "sku_id",
                    "part_number",
                    "hardware_sku",
                )
                if model:
                    model_counts[model] = model_counts.get(model, 0) + 1
                firmware = self._type_member_text(
                    member,
                    "firmware_version",
                    "sw_version",
                    "software_version",
                    "application_version",
                )
                if firmware:
                    firmware_counts[firmware] = firmware_counts.get(firmware, 0) + 1

                parsed_last = self._parse_inverter_last_report(
                    self._type_member_text(
                        member,
                        "last_report",
                        "last_reported",
                        "last_reported_at",
                    )
                )
                if parsed_last is not None and (
                    latest_reported is None or parsed_last > latest_reported
                ):
                    latest_reported = parsed_last
                    latest_reported_device = {
                        "device_type": device_type,
                        "device_uid": self._type_member_text(
                            member, "device_uid", "uid", "serial_number"
                        ),
                        "name": self._type_member_text(member, "name"),
                        "status": status_text,
                    }

            if not overall_status_text:
                overall_status_text = self._heatpump_worst_status_text(status_counts)

            buckets[key] = {
                "type_key": key,
                "type_label": "Heat Pump",
                "count": len(members_out),
                "devices": members_out,
                "status_counts": status_counts,
                "status_summary": self._format_inverter_status_summary(status_counts),
                "device_type_counts": device_type_counts,
                "model_counts": model_counts,
                "model_summary": self._format_inverter_model_summary(model_counts),
                "firmware_counts": firmware_counts,
                "firmware_summary": self._format_inverter_model_summary(
                    firmware_counts
                ),
                "overall_status_text": overall_status_text,
                "latest_reported_utc": (
                    latest_reported.isoformat() if latest_reported is not None else None
                ),
                "latest_reported_device": latest_reported_device,
            }
            if key not in ordered:
                if "iqevse" in ordered:
                    ordered.insert(ordered.index("iqevse") + 1, key)
                else:
                    ordered.append(key)
        else:
            buckets.pop(key, None)
            ordered = [item for item in ordered if item != key]

        self._set_type_device_buckets(buckets, ordered)
        if not ready_before:
            self._devices_inventory_ready = False
        self._refresh_cached_topology()

    @staticmethod
    def _summary_text(value: object) -> str | None:
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @classmethod
    def _summary_identity(cls, value: object) -> str | None:
        text = cls._summary_text(value)
        if not text:
            return None
        normalized = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        return normalized or None

    def _summary_type_bucket_source(self, type_key: object) -> dict[str, object] | None:
        return self.coordinator._summary_type_bucket_source(type_key)

    def _gateway_inventory_summary_marker(self) -> tuple[object, ...]:
        return self.coordinator._gateway_inventory_summary_marker()

    def _microinverter_inventory_summary_marker(self) -> tuple[object, ...]:
        return self.coordinator._microinverter_inventory_summary_marker()

    def _heatpump_inventory_summary_marker(self) -> tuple[object, ...]:
        return self.coordinator._heatpump_inventory_summary_marker()

    def _gateway_iq_energy_router_records_marker(self) -> tuple[object, ...]:
        return self.coordinator._gateway_iq_energy_router_records_marker()

    @staticmethod
    def _heatpump_status_text(member: dict[str, object] | None) -> str | None:
        if not isinstance(member, dict):
            return None
        status_text = (
            member.get("statusText")
            if member.get("statusText") is not None
            else member.get("status_text")
        )
        text = InventoryRuntime._summary_text(status_text)
        if text:
            return text
        raw = InventoryRuntime._summary_text(member.get("status"))
        if not raw:
            return None
        return raw.replace("_", " ").replace("-", " ").title()

    @classmethod
    def _gateway_iq_energy_router_summary_records(
        cls, members: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        key_counts: dict[str, int] = {}
        for member in members:
            index = len(records) + 1
            base_key = None
            for key in ("device-uid", "device_uid", "uid"):
                base_key = cls._summary_identity(member.get(key))
                if base_key:
                    break
            if base_key is None:
                name_identity = cls._summary_identity(member.get("name"))
                base_key = (
                    f"name_{name_identity}" if name_identity else f"index_{index}"
                )
            key_counts[base_key] = key_counts.get(base_key, 0) + 1
            key = base_key
            if key_counts[base_key] > 1:
                key = f"{base_key}_{key_counts[base_key]}"
            records.append(
                {
                    "key": key,
                    "index": index,
                    "name": cls._summary_text(member.get("name"))
                    or f"IQ Energy Router_{index}",
                    "member": dict(member),
                }
            )
        return records

    def _build_gateway_inventory_summary(self) -> dict[str, object]:
        return self.coordinator._build_gateway_inventory_summary()

    def _build_microinverter_inventory_summary(self) -> dict[str, object]:
        return self.coordinator._build_microinverter_inventory_summary()

    def _build_heatpump_inventory_summary(self) -> dict[str, object]:
        return self.coordinator._build_heatpump_inventory_summary()

    def _build_heatpump_type_summaries(self) -> dict[str, dict[str, object]]:
        return self.coordinator._build_heatpump_type_summaries()

    @callback
    def _rebuild_inventory_summary_caches(self) -> None:
        gateway_source = self._gateway_inventory_summary_marker()
        micro_source = self._microinverter_inventory_summary_marker()
        heatpump_source = self._heatpump_inventory_summary_marker()
        router_source = self._gateway_iq_energy_router_records_marker()
        gateway_summary = self._build_gateway_inventory_summary()
        micro_summary = self._build_microinverter_inventory_summary()
        heatpump_summary = self._build_heatpump_inventory_summary()
        heatpump_type_summaries = self._build_heatpump_type_summaries()
        router_records = self._gateway_iq_energy_router_summary_records(
            self.gateway_iq_energy_router_records()
        )
        self._gateway_inventory_summary_cache = gateway_summary
        self._gateway_inventory_summary_source = gateway_source
        self._microinverter_inventory_summary_cache = micro_summary
        self._microinverter_inventory_summary_source = micro_source
        self._heatpump_inventory_summary_cache = heatpump_summary
        self._heatpump_inventory_summary_source = heatpump_source
        self._heatpump_type_summaries_cache = heatpump_type_summaries
        self._heatpump_type_summaries_source = heatpump_source
        self._gateway_iq_energy_router_records_cache = router_records
        self._gateway_iq_energy_router_records_source = router_source
        self._gateway_iq_energy_router_records_by_key_cache = {
            record["key"]: record
            for record in router_records
            if isinstance(record, dict) and isinstance(record.get("key"), str)
        }

    async def _async_refresh_devices_inventory(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._devices_inventory_cache_until:
            if now < self._devices_inventory_cache_until:
                return
        fetcher = getattr(self.client, "devices_inventory", None)
        if not callable(fetcher):
            return
        try:
            payload = await self._async_call_refreshable_fetcher(fetcher, force=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Device inventory fetch failed: %s",
                redact_text(err, site_ids=(self.site_id,)),
            )
            return
        override = getattr(self.coordinator, "__dict__", {}).get(
            "_parse_devices_inventory_payload"
        )
        if callable(override):
            valid, grouped, ordered = override(payload)
        else:
            valid, grouped, ordered = self._parse_devices_inventory_payload(payload)
        if not valid:
            _LOGGER.debug(
                "Device inventory payload shape was invalid: %s",
                redact_text(payload, site_ids=(self.site_id,)),
            )
            return
        summary = self._debug_devices_inventory_summary(grouped, ordered)
        if not grouped:
            self._debug_log_summary_if_changed(
                "devices_inventory",
                "Device inventory discovery summary",
                summary,
            )
            return
        has_active_members = False
        for bucket in grouped.values():
            try:
                if int(bucket.get("count", 0)) > 0:
                    has_active_members = True
                    break
            except Exception:
                continue
        if not has_active_members:
            _LOGGER.debug(
                "Device inventory refresh returned no active members; keeping previous type mapping: %s",
                redact_text(summary, site_ids=(self.site_id,)),
            )
            self._devices_inventory_cache_until = now + DEVICES_INVENTORY_CACHE_TTL
            return
        self._set_type_device_buckets(grouped, ordered)
        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._devices_inventory_payload = redacted_payload
        else:
            self._devices_inventory_payload = {"value": redacted_payload}
        self._merge_heatpump_type_bucket()
        self._devices_inventory_cache_until = now + DEVICES_INVENTORY_CACHE_TTL
        self._debug_log_summary_if_changed(
            "devices_inventory",
            "Device inventory discovery summary",
            summary,
        )

    async def _async_refresh_hems_devices(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._hems_devices_cache_until:
            if now < self._hems_devices_cache_until:
                return
        await self._async_refresh_hems_support_preflight(force=force)
        if getattr(self.client, "hems_site_supported", None) is False:
            self._hems_devices_payload = None
            self._hems_devices_using_stale = False
            self._hems_inventory_ready = True
            self._merge_heatpump_type_bucket()
            self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
            self._debug_log_summary_if_changed(
                "hems_inventory",
                "HEMS discovery summary",
                self._debug_hems_inventory_summary(),
            )
            return
        fetcher = getattr(self.client, "hems_devices", None)
        if not callable(fetcher):
            return
        previous_payload = getattr(self, "_hems_devices_payload", None)

        stale_allowed = False
        if previous_payload is not None:
            last_success = getattr(self, "_hems_devices_last_success_mono", None)
            if isinstance(last_success, (int, float)):
                stale_allowed = now - float(last_success) <= HEMS_DEVICES_STALE_AFTER_S

        try:
            payload = await fetcher(refresh_data=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "HEMS device inventory fetch failed: %s",
                redact_text(err, site_ids=(self.site_id,)),
            )
            if getattr(self.client, "hems_site_supported", None) is False:
                self._hems_devices_payload = None
                self._hems_devices_using_stale = False
                self._hems_inventory_ready = True
                self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
            elif stale_allowed:
                self._hems_devices_payload = previous_payload
                self._hems_devices_using_stale = True
                self._hems_inventory_ready = True
                self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
            else:
                self._hems_devices_payload = None
                self._hems_devices_using_stale = False
                self._hems_inventory_ready = False
                self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
            self._merge_heatpump_type_bucket()
            self._debug_log_summary_if_changed(
                "hems_inventory",
                "HEMS discovery summary",
                self._debug_hems_inventory_summary(),
            )
            return

        if not isinstance(payload, dict):
            if getattr(self.client, "hems_site_supported", None) is False:
                self._hems_devices_payload = None
                self._hems_devices_using_stale = False
                self._hems_inventory_ready = True
                self._merge_heatpump_type_bucket()
                self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
                self._debug_log_summary_if_changed(
                    "hems_inventory",
                    "HEMS discovery summary",
                    self._debug_hems_inventory_summary(),
                )
                return
            if stale_allowed:
                self._hems_devices_payload = previous_payload
                self._hems_devices_using_stale = True
                self._hems_inventory_ready = True
                self._merge_heatpump_type_bucket()
                self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
                self._debug_log_summary_if_changed(
                    "hems_inventory",
                    "HEMS discovery summary",
                    self._debug_hems_inventory_summary(),
                )
                return
            self._hems_devices_payload = None
            self._hems_devices_using_stale = False
            self._hems_inventory_ready = False
            self._merge_heatpump_type_bucket()
            self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
            self._debug_log_summary_if_changed(
                "hems_inventory",
                "HEMS discovery summary",
                self._debug_hems_inventory_summary(),
            )
            return

        redacted_payload = self._redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            self._hems_devices_payload = redacted_payload
        else:
            self._hems_devices_payload = {"value": redacted_payload}
        self._hems_devices_last_success_mono = now
        self._hems_devices_last_success_utc = dt_util.utcnow()
        self._hems_devices_using_stale = False
        self._hems_inventory_ready = True
        self._merge_heatpump_type_bucket()
        self._hems_devices_cache_until = now + HEMS_DEVICES_CACHE_TTL
        self._debug_log_summary_if_changed(
            "hems_inventory",
            "HEMS discovery summary",
            self._debug_hems_inventory_summary(),
        )

    def _system_dashboard_detail_records(
        self,
        payloads: dict[str, object],
        *source_types: str,
    ) -> list[dict[str, object]]:
        return self.coordinator._system_dashboard_detail_records(
            payloads,
            *source_types,
        )

    def _system_dashboard_meter_kind(self, payload: dict[str, object]) -> str | None:
        return self.coordinator._system_dashboard_meter_kind(payload)

    def _system_dashboard_battery_detail_subset(
        self,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        return self.coordinator._system_dashboard_battery_detail_subset(payload)

    async def _async_refresh_system_dashboard(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._system_dashboard_cache_until:
            if now < self._system_dashboard_cache_until:
                return
        tree_fetcher = getattr(self.client, "devices_tree", None)
        details_fetcher = getattr(self.client, "devices_details", None)
        if not callable(tree_fetcher) and not callable(details_fetcher):
            return

        tree_payload = getattr(self, "_system_dashboard_devices_tree_raw", None)
        if callable(tree_fetcher):
            try:
                tree_payload = await tree_fetcher()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "System dashboard devices-tree fetch failed: %s",
                    redact_text(err, site_ids=(self.site_id,)),
                )

        details_payloads = (
            dict(getattr(self, "_system_dashboard_devices_details_raw", {}) or {})
            if isinstance(
                getattr(self, "_system_dashboard_devices_details_raw", {}), dict
            )
            else {}
        )
        if callable(details_fetcher):
            for source_type in SYSTEM_DASHBOARD_DIAGNOSTIC_TYPES:
                try:
                    payload = await details_fetcher(source_type)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "System dashboard devices_details fetch failed for type %s: %s",
                        source_type,
                        redact_text(err, site_ids=(self.site_id,)),
                    )
                    continue
                if not isinstance(payload, dict):
                    continue
                canonical_type = self._system_dashboard_type_key(source_type)
                if not canonical_type:
                    continue
                details_payloads.setdefault(canonical_type, {})[source_type] = payload

        (
            type_summaries,
            hierarchy_summary,
            hierarchy_index,
        ) = self._build_system_dashboard_summaries(tree_payload, details_payloads)
        self._system_dashboard_devices_tree_raw = (
            tree_payload if isinstance(tree_payload, dict) else None
        )
        self._system_dashboard_devices_details_raw = {
            canonical_type: {
                str(source_type): dict(payload)
                for source_type, payload in payloads.items()
                if isinstance(payload, dict)
            }
            for canonical_type, payloads in details_payloads.items()
            if isinstance(payloads, dict)
        }
        if isinstance(self._system_dashboard_devices_tree_raw, dict):
            redacted_tree = self._redact_battery_payload(
                self._system_dashboard_devices_tree_raw
            )
            self._system_dashboard_devices_tree_payload = (
                redacted_tree if isinstance(redacted_tree, dict) else None
            )
        else:
            self._system_dashboard_devices_tree_payload = None

        redacted_details: dict[str, dict[str, object]] = {}
        for (
            canonical_type,
            payloads_by_source,
        ) in self._system_dashboard_devices_details_raw.items():
            merged: dict[str, object] = {}
            for source_type, payload in payloads_by_source.items():
                redacted = self._redact_battery_payload(payload)
                if isinstance(redacted, dict):
                    merged[source_type] = redacted
            redacted_details[canonical_type] = merged
        self._system_dashboard_devices_details_payloads = redacted_details
        self._system_dashboard_type_summaries = type_summaries
        self._system_dashboard_hierarchy_summary = hierarchy_summary
        self._system_dashboard_hierarchy_index = hierarchy_index
        self._system_dashboard_cache_until = now + DEVICES_INVENTORY_CACHE_TTL
        self._debug_log_summary_if_changed(
            "system_dashboard",
            "System dashboard discovery summary",
            self._debug_system_dashboard_summary(
                self._system_dashboard_devices_tree_raw,
                self._system_dashboard_devices_details_raw,
                type_summaries,
                hierarchy_summary,
            ),
        )

    def _inverter_start_date(self) -> str | None:
        return self.coordinator._inverter_start_date()

    @staticmethod
    def _format_inverter_model_summary(model_counts: dict[str, int]) -> str | None:
        clean: dict[str, int] = {}
        for model, count in (model_counts or {}).items():
            name = str(model).strip()
            if not name:
                continue
            try:
                count_int = int(count)
            except (TypeError, ValueError):
                continue
            if count_int <= 0:
                continue
            clean[name] = count_int
        if not clean:
            return None
        ordered = sorted(clean.items(), key=lambda item: (-item[1], item[0]))
        return ", ".join(f"{name} x{count}" for name, count in ordered)

    @staticmethod
    def _format_inverter_status_summary(summary_counts: dict[str, int]) -> str:
        normal = int(summary_counts.get("normal", 0))
        warning = int(summary_counts.get("warning", 0))
        error = int(summary_counts.get("error", 0))
        not_reporting = int(summary_counts.get("not_reporting", 0))
        summary = (
            f"Normal {normal} | Warning {warning} | "
            f"Error {error} | Not Reporting {not_reporting}"
        )
        unknown = int(summary_counts.get("unknown", 0))
        if unknown > 0:
            summary = f"{summary} | Unknown {unknown}"
        return summary

    @staticmethod
    def _normalize_inverter_status(value: object) -> str:
        if value is None:
            return "unknown"
        try:
            normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        except Exception:
            return "unknown"
        if not normalized:
            return "unknown"
        if any(token in normalized for token in ("fault", "error", "critical")):
            return "error"
        if "warn" in normalized:
            return "warning"
        if any(
            token in normalized
            for token in ("not_reporting", "offline", "disconnected")
        ):
            return "not_reporting"
        if any(
            token in normalized
            for token in ("normal", "online", "connected", "ok", "recommended")
        ):
            return "normal"
        return "unknown"

    @staticmethod
    def _inverter_connectivity_state(summary_counts: dict[str, int]) -> str | None:
        total = int(summary_counts.get("total", 0))
        not_reporting = int(summary_counts.get("not_reporting", 0))
        unknown = int(summary_counts.get("unknown", 0))
        reporting = max(0, total - not_reporting - unknown)
        if total <= 0:
            return None
        if reporting >= total:
            return "online"
        if reporting == 0 and unknown > 0 and not_reporting <= 0:
            return "unknown"
        if reporting > 0:
            return "degraded"
        return "offline"

    @staticmethod
    def _parse_inverter_last_report(value: object) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=_tz.utc)
        epoch_value: float | None = None
        if isinstance(value, (int, float)):
            epoch_value = float(value)
        else:
            try:
                text = str(value).strip()
            except Exception:
                return None
            if not text:
                return None
            if text.endswith("[UTC]"):
                text = text[:-5]
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt_value = datetime.fromisoformat(text)
                return dt_value if dt_value.tzinfo else dt_value.replace(tzinfo=_tz.utc)
            except Exception:
                try:
                    epoch_value = float(text)
                except Exception:
                    return None
        if epoch_value is None:
            return None
        if epoch_value > 1_000_000_000_000:
            epoch_value /= 1000.0
        try:
            return datetime.fromtimestamp(epoch_value, tz=_tz.utc)
        except Exception:
            return None

    def _merge_microinverter_type_bucket(self) -> None:
        self.coordinator._merge_microinverter_type_bucket()

    async def _async_refresh_inverters(self) -> None:
        """Refresh inverter metadata/status/production and build serial snapshots."""
        if not self.include_inverters:
            self._inverters_inventory_payload = None
            self._inverter_status_payload = None
            self._inverter_production_payload = None
            self._inverter_data = {}
            self._inverter_order = []
            self._inverter_panel_info = None
            self._inverter_status_type_counts = {}
            self._inverter_model_counts = {}
            self._inverter_summary_counts = {
                "total": 0,
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
            }
            self._merge_microinverter_type_bucket()
            self._merge_heatpump_type_bucket()
            return

        fetch_inventory = getattr(self.client, "inverters_inventory", None)
        fetch_status = getattr(self.client, "inverter_status", None)
        fetch_production = getattr(self.client, "inverter_production", None)
        if not callable(fetch_inventory) or not callable(fetch_status):
            return

        async def _fetch_inventory_page(offset: int) -> dict[str, object] | None:
            try:
                payload = await fetch_inventory(
                    limit=1000,
                    offset=offset,
                    search="",
                )
            except TypeError:
                if offset != 0:
                    return None
                payload = await fetch_inventory()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Inverters inventory fetch failed for site %s: %s",
                    redact_site_id(self.site_id),
                    redact_text(err, site_ids=(self.site_id,)),
                )
                return None
            if not isinstance(payload, dict):
                return None
            return payload

        inventory_payload = await _fetch_inventory_page(0)
        if inventory_payload is None:
            return

        inverters_raw = inventory_payload.get("inverters")
        if not isinstance(inverters_raw, list):
            inverters_raw = []
        inverters_list = [item for item in inverters_raw if isinstance(item, dict)]
        total_expected = self._coerce_int(
            inventory_payload.get("total"), default=len(inverters_list)
        )
        if total_expected > len(inverters_list):
            merged = list(inverters_list)
            next_offset = len(merged)
            while next_offset < total_expected:
                next_payload = await _fetch_inventory_page(next_offset)
                if next_payload is None:
                    break
                next_raw = next_payload.get("inverters")
                if not isinstance(next_raw, list):
                    break
                next_items = [item for item in next_raw if isinstance(item, dict)]
                if not next_items:
                    break
                merged.extend(next_items)
                total_candidate = self._coerce_int(
                    next_payload.get("total"), default=total_expected
                )
                if total_candidate > total_expected:
                    total_expected = total_candidate
                page_size = len(next_items)
                next_offset += page_size
            inventory_payload = dict(inventory_payload)
            inventory_payload["inverters"] = merged
            inverters_list = merged

        try:
            status_payload = await fetch_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Inverter status fetch failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            status_payload = {}
        if not isinstance(status_payload, dict):
            status_payload = {}

        start_date = self._inverter_start_date()
        end_date = self._site_local_current_date()
        production_payload: dict[str, object] = {}
        if callable(fetch_production) and start_date is not None:
            try:
                production_payload = await fetch_production(
                    start_date=start_date, end_date=end_date
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Inverter production fetch failed for site %s: %s",
                    redact_site_id(self.site_id),
                    redact_text(err, site_ids=(self.site_id,)),
                )
                production_payload = {}
        elif start_date is None:
            _LOGGER.debug(
                "Skipping inverter production fetch for site %s: start date unknown",
                redact_site_id(self.site_id),
            )
        if not isinstance(production_payload, dict):
            production_payload = {}
        production_raw = production_payload.get("production")
        if not isinstance(production_raw, dict):
            production_raw = {}

        status_by_serial: dict[str, dict[str, object]] = {}
        for inverter_id, payload in status_payload.items():
            if not isinstance(payload, dict):
                continue
            serial = str(payload.get("serialNum") or "").strip()
            if not serial:
                continue
            item = dict(payload)
            item["inverter_id"] = str(inverter_id)
            status_by_serial[serial] = item

        previous_data = getattr(self, "_inverter_data", None)
        if not isinstance(previous_data, dict):
            previous_data = {}

        inverter_data: dict[str, dict[str, object]] = {}
        inverter_order: list[str] = []
        model_counts: dict[str, int] = {}
        status_type_counts: dict[str, int] = {}
        derived_status_counts: dict[str, int] = {
            "normal": 0,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
        }
        for item in inverters_list:
            if self.member_is_retired(item):
                continue
            serial = str(item.get("serial_number") or "").strip()
            if not serial:
                continue
            previous_item = previous_data.get(serial)
            if not isinstance(previous_item, dict):
                previous_item = {}
            status_item = status_by_serial.get(serial, {})
            inverter_id = (
                str(
                    status_item.get("inverter_id")
                    or previous_item.get("inverter_id")
                    or ""
                ).strip()
                or None
            )
            production_wh = None
            if inverter_id:
                try:
                    raw_val = production_raw.get(inverter_id)
                    production_wh = float(raw_val) if raw_val is not None else None
                except (TypeError, ValueError):
                    production_wh = None
            prev_wh: float | None = None
            try:
                raw_prev_wh = previous_item.get("lifetime_production_wh")
                prev_wh = float(raw_prev_wh) if raw_prev_wh is not None else None
            except (TypeError, ValueError):
                prev_wh = None
            if production_wh is None or production_wh < 0:
                production_wh = prev_wh
            elif prev_wh is not None and production_wh < prev_wh:
                production_wh = prev_wh

            query_start = self._normalize_iso_date(production_payload.get("start_date"))
            query_end = self._normalize_iso_date(production_payload.get("end_date"))
            if query_start is None:
                query_start = self._normalize_iso_date(
                    previous_item.get("lifetime_query_start_date")
                )
            if query_end is None:
                query_end = self._normalize_iso_date(
                    previous_item.get("lifetime_query_end_date")
                )
            if query_start is None:
                query_start = start_date
            if query_end is None:
                query_end = end_date

            model_name = str(item.get("name") or "").strip()
            if model_name:
                model_counts[model_name] = model_counts.get(model_name, 0) + 1
            status_type = status_item.get("type")
            if status_type is not None:
                try:
                    status_type_text = str(status_type).strip()
                except Exception:
                    status_type_text = ""
                if status_type_text:
                    status_type_counts[status_type_text] = (
                        status_type_counts.get(status_type_text, 0) + 1
                    )
            status_bucket = self._normalize_inverter_status(
                status_item.get("statusCode")
                if status_item.get("statusCode") is not None
                else (
                    status_item.get("status")
                    if status_item.get("status") is not None
                    else item.get("status")
                )
            )
            if status_bucket in derived_status_counts:
                derived_status_counts[status_bucket] += 1
            inverter_data[serial] = {
                "serial_number": serial,
                "name": item.get("name"),
                "array_name": item.get("array_name"),
                "sku_id": item.get("sku_id"),
                "part_num": item.get("part_num"),
                "sku": item.get("sku"),
                "status": item.get("status"),
                "status_text": item.get("statusText"),
                "last_report": item.get("last_report"),
                "fw1": item.get("fw1"),
                "fw2": item.get("fw2"),
                "warranty_end_date": item.get("warranty_end_date"),
                "inverter_id": inverter_id,
                "device_id": status_item.get(
                    "deviceId", previous_item.get("device_id")
                ),
                "inverter_type": status_item.get(
                    "type", previous_item.get("inverter_type")
                ),
                "status_code": status_item.get(
                    "statusCode", previous_item.get("status_code")
                ),
                "show_sig_str": status_item.get(
                    "show_sig_str", previous_item.get("show_sig_str")
                ),
                "emu_version": status_item.get(
                    "emu_version", previous_item.get("emu_version")
                ),
                "issi": status_item.get("issi", previous_item.get("issi")),
                "rssi": status_item.get("rssi", previous_item.get("rssi")),
                "lifetime_production_wh": production_wh,
                "lifetime_query_start_date": query_start,
                "lifetime_query_end_date": query_end,
            }
            inverter_order.append(serial)

        total_count = len(inverter_data)
        normal_count = int(
            derived_status_counts.get("normal")
            or self._coerce_int(inventory_payload.get("normal_count"), default=0)
        )
        warning_count = int(
            derived_status_counts.get("warning")
            or self._coerce_int(inventory_payload.get("warning_count"), default=0)
        )
        error_count = int(
            derived_status_counts.get("error")
            or self._coerce_int(inventory_payload.get("error_count"), default=0)
        )
        not_reporting_count = int(
            derived_status_counts.get("not_reporting")
            or self._coerce_int(inventory_payload.get("not_reporting"), default=0)
        )
        normal_count = max(0, normal_count)
        warning_count = max(0, warning_count)
        error_count = max(0, error_count)
        not_reporting_count = max(0, not_reporting_count)
        counts = {
            "normal": normal_count,
            "warning": warning_count,
            "error": error_count,
            "not_reporting": not_reporting_count,
        }
        known_total = sum(counts.values())
        if known_total > total_count:
            overflow = known_total - total_count
            for key in ("not_reporting", "error", "warning", "normal"):
                if overflow <= 0:
                    break
                reducible = min(counts[key], overflow)
                counts[key] -= reducible
                overflow -= reducible
        known_total = sum(counts.values())
        unknown_count = max(0, total_count - known_total)

        summary_counts = {
            "total": total_count,
            "normal": counts["normal"],
            "warning": counts["warning"],
            "error": counts["error"],
            "not_reporting": counts["not_reporting"],
            "unknown": unknown_count,
        }
        panel_info_out: dict[str, object] | None = None
        panel_info_raw = inventory_payload.get("panel_info")
        if isinstance(panel_info_raw, dict):
            panel_info_out = {}
            for key, value in panel_info_raw.items():
                if value is None:
                    continue
                if isinstance(value, (str, int, float, bool)):
                    if isinstance(value, str):
                        value = value.strip()
                        if not value:
                            continue
                    panel_info_out[str(key)] = value
            if not panel_info_out:
                panel_info_out = None

        self._inverters_inventory_payload = inventory_payload
        self._inverter_status_payload = status_payload
        self._inverter_production_payload = production_payload
        self._inverter_data = inverter_data
        self._inverter_order = inverter_order
        self._inverter_panel_info = panel_info_out
        self._inverter_status_type_counts = status_type_counts
        self._inverter_model_counts = model_counts
        self._inverter_summary_counts = summary_counts
        self._merge_microinverter_type_bucket()
        self._merge_heatpump_type_bucket()

    def iter_inverter_serials(self) -> list[str]:
        """Return currently active inverter serials in a stable order."""
        order = getattr(self, "_inverter_order", None) or []
        data = getattr(self, "_inverter_data", None)
        if not isinstance(data, dict):
            return []
        serials = [str(sn) for sn in order if sn in data]
        serials.extend(str(sn) for sn in data.keys())
        return [sn for sn in dict.fromkeys(serials) if sn]

    def inverter_data(self, serial: str) -> dict[str, object] | None:
        """Return normalized inverter snapshot for a serial."""
        data = getattr(self, "_inverter_data", None)
        if not isinstance(data, dict):
            return None
        try:
            key = str(serial).strip()
        except Exception:
            return None
        if not key:
            return None
        payload = data.get(key)
        if not isinstance(payload, dict):
            return None
        return dict(payload)

    def inverter_diagnostics_payloads(self) -> dict[str, object]:
        """Return inverter-related payload snapshots used by diagnostics."""
        bucket_snapshot = self.type_bucket("microinverter")
        return {
            "enabled": bool(getattr(self, "include_inverters", True)),
            "summary_counts": getattr(self, "_inverter_summary_counts", None),
            "model_counts": getattr(self, "_inverter_model_counts", None),
            "status_type_counts": getattr(self, "_inverter_status_type_counts", None),
            "panel_info": getattr(self, "_inverter_panel_info", None),
            "inventory_payload": getattr(self, "_inverters_inventory_payload", None),
            "status_payload": getattr(self, "_inverter_status_payload", None),
            "production_payload": getattr(self, "_inverter_production_payload", None),
            "bucket_snapshot": bucket_snapshot,
        }

    def _system_dashboard_raw_payloads(
        self, canonical_type: str
    ) -> dict[str, dict[str, object]]:
        payloads = getattr(self, "_system_dashboard_devices_details_raw", None)
        if not isinstance(payloads, dict):
            return {}
        raw = payloads.get(canonical_type)
        if not isinstance(raw, dict):
            return {}
        return {
            str(source_type): dict(payload)
            for source_type, payload in raw.items()
            if isinstance(payload, dict)
        }

    def system_dashboard_envoy_detail(self) -> dict[str, object] | None:
        records = self._system_dashboard_detail_records(
            self._system_dashboard_raw_payloads("envoy"),
            "envoys",
            "envoy",
        )
        if not records:
            return None
        record = records[0]
        out: dict[str, object] = {}
        for key in (
            "status",
            "statusText",
            "connected",
            "last_report",
            "last_interval_end_date",
            "envoy_sw_version",
            "ap_mode",
            "sku_id",
        ):
            value = record.get(key)
            if value is not None:
                out[key] = value
        return out or None

    def system_dashboard_meter_detail(
        self, meter_kind: str
    ) -> dict[str, object] | None:
        for record in self._system_dashboard_detail_records(
            self._system_dashboard_raw_payloads("envoy"),
            "meters",
            "meter",
        ):
            if self._system_dashboard_meter_kind(record) != meter_kind:
                continue
            out: dict[str, object] = {}
            for key in (
                "name",
                "serial_number",
                "channel_type",
                "status",
                "statusText",
                "last_report",
                "meter_state",
                "config_type",
                "meter_type",
            ):
                value = record.get(key)
                if value is not None:
                    out[key] = value
            return out or None
        return None

    def system_dashboard_battery_detail(self, serial: str) -> dict[str, object] | None:
        snapshots = getattr(self, "_battery_storage_data", None)
        snapshot = snapshots.get(serial) if isinstance(snapshots, dict) else None
        candidates: set[str] = set()
        for value in (
            serial,
            snapshot.get("serial_number") if isinstance(snapshot, dict) else None,
            snapshot.get("identity") if isinstance(snapshot, dict) else None,
            snapshot.get("battery_id") if isinstance(snapshot, dict) else None,
            snapshot.get("id") if isinstance(snapshot, dict) else None,
        ):
            text = self._coerce_optional_text(value)
            if text:
                candidates.add(text)
        if not candidates:
            return None
        for record in self._system_dashboard_detail_records(
            self._system_dashboard_raw_payloads("encharge"),
            "encharges",
            "encharge",
        ):
            record_serial = self._coerce_optional_text(record.get("serial_number"))
            record_id = self._coerce_optional_text(record.get("id"))
            if record_serial not in candidates and record_id not in candidates:
                continue
            detail = self._system_dashboard_battery_detail_subset(record)
            return detail or None
        return None

    def system_dashboard_diagnostics(self) -> dict[str, object]:
        devices_tree_payload = getattr(
            self, "_system_dashboard_devices_tree_payload", None
        )
        devices_details_payloads = getattr(
            self, "_system_dashboard_devices_details_payloads", None
        )
        hierarchy_summary = getattr(self, "_system_dashboard_hierarchy_summary", None)
        type_summaries = getattr(self, "_system_dashboard_type_summaries", None)
        out: dict[str, object] = {
            "devices_tree_payload": (
                self._copy_diagnostics_value(devices_tree_payload)
                if isinstance(devices_tree_payload, dict)
                else None
            ),
            "devices_details_payloads": (
                self._copy_diagnostics_value(devices_details_payloads)
                if isinstance(devices_details_payloads, dict)
                else {}
            ),
            "hierarchy_summary": (
                self._copy_diagnostics_value(hierarchy_summary)
                if isinstance(hierarchy_summary, dict)
                else {}
            ),
            "type_summaries": (
                self._copy_diagnostics_value(type_summaries)
                if isinstance(type_summaries, dict)
                else {}
            ),
        }
        return out

    @staticmethod
    def member_is_retired(member: dict[str, object]) -> bool:
        return device_member_is_retired(member)


install_state_descriptors(InventoryRuntime)
