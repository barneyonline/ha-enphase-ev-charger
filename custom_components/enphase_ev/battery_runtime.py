from __future__ import annotations

import logging
import time
from datetime import time as dt_time, timedelta
from datetime import timezone as _tz
from http import HTTPStatus
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from .const import (
    BATTERY_BACKUP_HISTORY_CACHE_TTL,
    BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL,
    BATTERY_MIN_SOC_FALLBACK,
    BATTERY_PROFILE_DEFAULT_RESERVE,
    BATTERY_PROFILE_WRITE_DEBOUNCE_S,
    BATTERY_SETTINGS_CACHE_TTL,
    BATTERY_SETTINGS_WRITE_DEBOUNCE_S,
    BATTERY_SITE_SETTINGS_CACHE_TTL,
    DRY_CONTACT_SETTINGS_CACHE_TTL,
    DRY_CONTACT_SETTINGS_FAILURE_CACHE_TTL,
    DRY_CONTACT_SETTINGS_STALE_AFTER_S,
    DOMAIN,
    FAST_TOGGLE_POLL_HOLD_S,
    GRID_CONTROL_CHECK_CACHE_TTL,
    GRID_CONTROL_CHECK_STALE_AFTER_S,
    SAVINGS_OPERATION_MODE_SUBTYPE,
    STORM_ALERT_CACHE_TTL,
    STORM_ALERT_INACTIVE_STATUSES,
    STORM_GUARD_CACHE_TTL,
    STORM_GUARD_PENDING_HOLD_S,
)
from .device_types import member_is_retired, sanitize_member
from .labels import battery_profile_label as translated_battery_profile_label
from .log_redaction import redact_identifier, redact_site_id, redact_text
from .parsing_helpers import (
    coerce_optional_bool,
    coerce_optional_float,
    coerce_optional_text,
)
from .runtime_helpers import coerce_int, coerce_optional_int
from .service_validation import raise_translated_service_validation
from .state_models import BatteryControlCapability

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)


class BatteryRuntime:
    """Battery profile selection and pending-state helpers."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator

    @property
    def battery_state(self) -> object:
        """Return the explicit battery state bag when available."""

        return getattr(self.coordinator, "battery_state", self.coordinator)

    def _normalize_battery_sub_type(self, value: object) -> str | None:
        coord = self.coordinator
        func = getattr(coord, "normalize_battery_sub_type", None)
        if callable(func):
            return func(value)
        func = getattr(coord, "_normalize_battery_sub_type", None)
        if callable(func):
            return func(value)
        return None

    def _sync_battery_profile_pending_issue(self) -> None:
        coord = self.coordinator
        func = getattr(coord, "sync_battery_profile_pending_issue", None)
        if callable(func):
            func()
            return
        func = getattr(coord, "_sync_battery_profile_pending_issue", None)
        if callable(func):
            func()

    def _coerce_int(self, value: object, *, default: int = 0) -> int:
        return coerce_int(value, default=default)

    def _coerce_optional_bool(self, value: object) -> bool | None:
        return coerce_optional_bool(value)

    def _coerce_optional_text(self, value: object) -> str | None:
        return coerce_optional_text(value)

    def _coerce_optional_int(self, value: object) -> int | None:
        return coerce_optional_int(value)

    def _coerce_optional_float(self, value: object) -> float | None:
        return coerce_optional_float(value)

    def _coerce_optional_kwh(self, value: object) -> float | None:
        func = getattr(self.coordinator, "_coerce_optional_kwh", None)
        if callable(func):
            return func(value)
        return None

    def _parse_percent_value(self, value: object) -> float | None:
        func = getattr(self.coordinator, "_parse_percent_value", None)
        if callable(func):
            return func(value)
        return None

    def _normalize_battery_status_text(self, value: object) -> str | None:
        func = getattr(self.coordinator, "_normalize_battery_status_text", None)
        if callable(func):
            return func(value)
        return None

    def _battery_status_severity_value(self, status: str | None) -> int:
        func = getattr(self.coordinator, "_battery_status_severity_value", None)
        if callable(func):
            return func(status)
        return 0

    def _battery_storage_key(self, payload: dict[str, object]) -> str | None:
        func = getattr(self.coordinator, "_battery_storage_key", None)
        if callable(func):
            return func(payload)
        return None

    def _normalize_battery_id(self, value: object) -> str | None:
        func = getattr(self.coordinator, "_normalize_battery_id", None)
        if callable(func):
            return func(value)
        return None

    def _refresh_cached_topology(self) -> None:
        func = getattr(self.coordinator, "_refresh_cached_topology", None)
        if callable(func):
            func()

    def _normalize_battery_grid_mode(self, value: object) -> str | None:
        func = getattr(self.coordinator, "_normalize_battery_grid_mode", None)
        if callable(func):
            return func(value)
        return None

    def _normalize_minutes_of_day(self, value: object) -> int | None:
        func = getattr(self.coordinator, "_normalize_minutes_of_day", None)
        if callable(func):
            return func(value)
        return None

    def _copy_dry_contact_settings_entry(
        self, entry: dict[str, object]
    ) -> dict[str, object]:
        copied: dict[str, object] = {}
        for key, value in entry.items():
            if isinstance(value, dict):
                copied[key] = dict(value)
            elif isinstance(value, list):
                copied[key] = [
                    dict(item) if isinstance(item, dict) else item for item in value
                ]
            else:
                copied[key] = value
        return copied

    def _dry_contact_settings_looks_like_entry(self, entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        keys = (
            "serial_number",
            "serial",
            "serialNumber",
            "device_uid",
            "device-uid",
            "deviceUid",
            "uid",
            "contact_id",
            "contactId",
            "id",
            "channel_type",
            "channelType",
            "meter_type",
            "name",
            "displayName",
            "configuredName",
            "overrideSupported",
            "overrideActive",
            "controlMode",
            "pollingInterval",
            "pollingIntervalSeconds",
            "socThreshold",
            "socThresholdMin",
            "socThresholdMax",
            "scheduleWindows",
            "schedule_windows",
            "schedule",
            "schedules",
            "windows",
        )
        return any(key in entry for key in keys)

    def _normalize_dry_contact_schedule_windows(
        self, windows: object
    ) -> list[dict[str, object]]:
        if isinstance(windows, list):
            candidates = [item for item in windows if isinstance(item, dict)]
        elif isinstance(windows, dict):
            candidates = [windows]
        else:
            return []
        normalized_windows: list[dict[str, object]] = []
        seen: set[tuple[str | None, str | None]] = set()
        for item in candidates:
            start = self._coerce_optional_text(
                item.get("start")
                if item.get("start") is not None
                else (
                    item.get("startTime")
                    if item.get("startTime") is not None
                    else (
                        item.get("begin")
                        if item.get("begin") is not None
                        else (
                            item.get("beginTime")
                            if item.get("beginTime") is not None
                            else (
                                item.get("from")
                                if item.get("from") is not None
                                else item.get("windowStart")
                            )
                        )
                    )
                )
            )
            end = self._coerce_optional_text(
                item.get("end")
                if item.get("end") is not None
                else (
                    item.get("endTime")
                    if item.get("endTime") is not None
                    else (
                        item.get("finish")
                        if item.get("finish") is not None
                        else (
                            item.get("finishTime")
                            if item.get("finishTime") is not None
                            else (
                                item.get("to")
                                if item.get("to") is not None
                                else item.get("windowEnd")
                            )
                        )
                    )
                )
            )
            if start is None and end is None:
                continue
            dedupe_key = (start, end)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            normalized: dict[str, object] = {}
            if start is not None:
                normalized["start"] = start
            if end is not None:
                normalized["end"] = end
            normalized_windows.append(normalized)
        return normalized_windows

    def _dry_contact_identity_candidates(
        self,
        value: dict[str, object],
    ) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []

        def _add(identity_key: str, raw_value: object) -> None:
            text = self._coerce_optional_text(raw_value)
            if text is None:
                return
            candidates.append((identity_key, text.casefold()))

        _add(
            "serial_number",
            (
                value.get("serial_number")
                if value.get("serial_number") is not None
                else (
                    value.get("serial")
                    if value.get("serial") is not None
                    else value.get("serialNumber")
                )
            ),
        )
        _add(
            "device_uid",
            (
                value.get("device_uid")
                if value.get("device_uid") is not None
                else (
                    value.get("device-uid")
                    if value.get("device-uid") is not None
                    else (
                        value.get("deviceUid")
                        if value.get("deviceUid") is not None
                        else (
                            value.get("iqer_uid")
                            if value.get("iqer_uid") is not None
                            else value.get("iqer-uid")
                        )
                    )
                )
            ),
        )
        _add("uid", value.get("uid"))
        _add(
            "contact_id",
            (
                value.get("contact_id")
                if value.get("contact_id") is not None
                else (
                    value.get("contactId")
                    if value.get("contactId") is not None
                    else value.get("id")
                )
            ),
        )
        _add(
            "channel_type",
            (
                value.get("channel_type")
                if value.get("channel_type") is not None
                else (
                    value.get("channelType")
                    if value.get("channelType") is not None
                    else (
                        value.get("meter_type")
                        if value.get("meter_type") is not None
                        else value.get("type")
                    )
                )
            ),
        )
        _add(
            "name",
            (
                value.get("configured_name")
                if value.get("configured_name") is not None
                else (
                    value.get("display_name")
                    if value.get("display_name") is not None
                    else (
                        value.get("name")
                        if value.get("name") is not None
                        else (
                            value.get("displayName")
                            if value.get("displayName") is not None
                            else (
                                value.get("configuredName")
                                if value.get("configuredName") is not None
                                else value.get("label")
                            )
                        )
                    )
                )
            ),
        )
        return candidates

    def _dry_contact_identity_map(self, value: dict[str, object]) -> dict[str, str]:
        return dict(self._dry_contact_identity_candidates(value))

    @staticmethod
    def _dry_contact_member_dedupe_key(
        identities: dict[str, str], index: int
    ) -> tuple[tuple[str, str], ...]:
        for keys in (
            ("device_uid", "contact_id"),
            ("device_uid", "channel_type"),
            ("uid", "contact_id"),
            ("uid", "channel_type"),
            ("contact_id", "channel_type"),
            ("serial_number", "channel_type"),
            ("serial_number", "contact_id"),
            ("contact_id",),
            ("channel_type",),
            ("serial_number",),
            ("device_uid",),
            ("uid",),
            ("name",),
        ):
            if all(identities.get(key) is not None for key in keys):
                return tuple((key, identities[key]) for key in keys)
        return (("idx", str(index)),)

    @staticmethod
    def _dry_contact_match_conflicts(
        member_identities: dict[str, str],
        entry_identities: dict[str, str],
    ) -> bool:
        for key in (
            "contact_id",
            "channel_type",
            "serial_number",
            "device_uid",
            "uid",
        ):
            member_value = member_identities.get(key)
            entry_value = entry_identities.get(key)
            if member_value is None or entry_value is None:
                continue
            if member_value != entry_value:
                return True
        return False

    def _dry_contact_member_is_dry_contact(self, member: object) -> bool:
        if not isinstance(member, dict):
            return False
        for key in ("channel_type", "channelType", "meter_type", "type", "name"):
            value = self._coerce_optional_text(member.get(key))
            if value is None:
                continue
            compact = (
                value.casefold().replace("-", "").replace("_", "").replace(" ", "")
            )
            if compact in {"nc1", "nc2", "no1", "no2"}:
                return True
            if "drycontact" in compact:
                return True
            if "relay" in compact and any(token in compact for token in ("nc", "no")):
                return True
        return False

    def _dry_contact_members_for_settings(self) -> list[dict[str, object]]:
        members: list[dict[str, object]] = []
        seen_keys: set[tuple[tuple[str, str], ...]] = set()

        def _append_member(raw_member: object) -> None:
            if not isinstance(raw_member, dict):
                return
            if member_is_retired(raw_member):
                return
            sanitized = sanitize_member(raw_member)
            if not sanitized:
                return
            identities = self._dry_contact_identity_map(sanitized)
            dedupe_key = self._dry_contact_member_dedupe_key(identities, len(members))
            if dedupe_key in seen_keys:
                return
            seen_keys.add(dedupe_key)
            members.append(sanitized)

        coord = self.coordinator
        type_bucket = getattr(coord, "type_bucket", None)
        envoy_bucket = type_bucket("envoy") if callable(type_bucket) else {}
        if envoy_bucket is None:
            envoy_bucket = {}
        envoy_members = (
            envoy_bucket.get("devices") if isinstance(envoy_bucket, dict) else None
        )
        if isinstance(envoy_members, list):
            for member in envoy_members:
                if self._dry_contact_member_is_dry_contact(member):
                    _append_member(member)

        dry_bucket = type_bucket("dry_contact") if callable(type_bucket) else {}
        if dry_bucket is None:
            dry_bucket = {}
        dry_members = (
            dry_bucket.get("devices") if isinstance(dry_bucket, dict) else None
        )
        if isinstance(dry_members, list):
            for member in dry_members:
                _append_member(member)

        return members

    def _match_dry_contact_settings(
        self,
        members: list[dict[str, object]],
        *,
        settings_entries: list[dict[str, object]],
    ) -> tuple[list[dict[str, object] | None], list[dict[str, object]]]:
        members_list = [dict(member) for member in members if isinstance(member, dict)]
        member_identity_maps = [
            self._dry_contact_identity_map(member) for member in members_list
        ]
        index_by_key: dict[str, dict[str, list[int]]] = {
            key: {}
            for key in (
                "contact_id",
                "channel_type",
                "serial_number",
                "device_uid",
                "uid",
                "name",
            )
        }
        for index, identities in enumerate(member_identity_maps):
            for key, mapping in index_by_key.items():
                value = identities.get(key)
                if value is None:
                    continue
                mapping.setdefault(value, []).append(index)

        entries = [
            self._copy_dry_contact_settings_entry(entry)
            for entry in settings_entries
            if isinstance(entry, dict)
        ]
        matches: list[dict[str, object] | None] = [None] * len(members_list)
        unmatched: list[dict[str, object]] = []
        used_member_indexes: set[int] = set()

        for entry in entries:
            entry_identities = self._dry_contact_identity_map(entry)
            matched_member_index: int | None = None
            for key in (
                "contact_id",
                "channel_type",
                "serial_number",
                "device_uid",
                "uid",
                "name",
            ):
                value = entry_identities.get(key)
                if value is None:
                    continue
                candidate_indexes = [
                    index
                    for index in index_by_key[key].get(value, [])
                    if index not in used_member_indexes
                ]
                if len(candidate_indexes) != 1:
                    continue
                candidate_index = candidate_indexes[0]
                if self._dry_contact_match_conflicts(
                    member_identity_maps[candidate_index], entry_identities
                ):
                    continue
                matched_member_index = candidate_index
                break
            if matched_member_index is None:
                unmatched.append(entry)
                continue
            used_member_indexes.add(matched_member_index)
            matches[matched_member_index] = entry
        return matches, unmatched

    def _current_schedule_window_from_coordinator(
        self,
    ) -> tuple[int | None, int | None]:
        coord = self.coordinator
        func = getattr(coord, "current_charge_from_grid_schedule_window", None)
        if callable(func):
            return func()
        func = getattr(coord, "_current_charge_from_grid_schedule_window", None)
        if callable(func):
            return func()
        return self.current_charge_from_grid_schedule_window()

    @staticmethod
    def normalize_battery_profile_key(value: object) -> str | None:
        if value is None:
            return None
        try:
            normalized = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        return normalized or None

    @staticmethod
    def battery_profile_label(
        profile: str | None, hass: object | None = None
    ) -> str | None:
        return translated_battery_profile_label(profile, hass=hass)

    @staticmethod
    def _normalize_pending_sub_type(
        coordinator: EnphaseCoordinator, profile: str, sub_type: str | None
    ) -> str | None:
        if profile not in {"cost_savings", "ai_optimisation"}:
            return None
        func = getattr(coordinator, "normalize_battery_sub_type", None)
        if callable(func):
            return func(sub_type)
        func = getattr(coordinator, "_normalize_battery_sub_type", None)
        if callable(func):
            return func(sub_type)
        return None

    def clear_battery_pending(self) -> None:
        state = self.battery_state
        state._battery_pending_profile = None
        state._battery_pending_reserve = None
        state._battery_pending_sub_type = None
        state._battery_pending_requested_at = None
        state._battery_pending_require_exact_settings = True
        self._sync_battery_profile_pending_issue()

    def set_battery_pending(
        self,
        *,
        profile: str,
        reserve: int,
        sub_type: str | None,
        require_exact_settings: bool = True,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        state._battery_pending_profile = profile
        state._battery_pending_reserve = reserve
        state._battery_pending_sub_type = self._normalize_pending_sub_type(
            coord, profile, sub_type
        )
        state._battery_pending_requested_at = dt_util.utcnow()
        state._battery_pending_require_exact_settings = bool(require_exact_settings)
        self._sync_battery_profile_pending_issue()

    def effective_profile_matches_pending(self) -> bool:
        state = self.battery_state
        pending_profile = getattr(state, "_battery_pending_profile", None)
        if not pending_profile:
            return False
        if getattr(state, "_battery_profile", None) != pending_profile:
            return False
        if not getattr(state, "_battery_pending_require_exact_settings", True):
            return True
        pending_reserve = getattr(state, "_battery_pending_reserve", None)
        if (
            pending_reserve is not None
            and getattr(state, "_battery_backup_percentage", None) != pending_reserve
        ):
            return False
        if pending_profile not in {"cost_savings", "ai_optimisation"}:
            return True
        pending_subtype = self._normalize_battery_sub_type(
            getattr(state, "_battery_pending_sub_type", None)
        )
        effective_subtype = self._normalize_battery_sub_type(
            getattr(state, "_battery_operation_mode_sub_type", None)
        )
        if pending_subtype == SAVINGS_OPERATION_MODE_SUBTYPE:
            return effective_subtype == SAVINGS_OPERATION_MODE_SUBTYPE
        if pending_subtype is None:
            return effective_subtype != SAVINGS_OPERATION_MODE_SUBTYPE
        return pending_subtype == effective_subtype

    def remember_battery_reserve(
        self, profile: str | None, reserve: int | None
    ) -> None:
        if not profile or reserve is None:
            return
        normalized = self.normalize_battery_profile_key(profile)
        if not normalized or normalized not in BATTERY_PROFILE_DEFAULT_RESERVE:
            return
        self.battery_state._battery_profile_reserve_memory[normalized] = int(reserve)

    def target_reserve_for_profile(self, profile: str) -> int:
        remembered = self.battery_state._battery_profile_reserve_memory.get(profile)
        if remembered is not None:
            return self.normalize_battery_reserve_for_profile(profile, remembered)
        default = BATTERY_PROFILE_DEFAULT_RESERVE.get(profile, 20)
        return self.normalize_battery_reserve_for_profile(profile, default)

    def current_savings_sub_type(self) -> str | None:
        selected_subtype = self.coordinator.battery_selected_operation_mode_sub_type
        if selected_subtype == SAVINGS_OPERATION_MODE_SUBTYPE:
            return SAVINGS_OPERATION_MODE_SUBTYPE
        return None

    def target_operation_mode_sub_type(self, profile: str) -> str | None:
        normalized_profile = self.normalize_battery_profile_key(profile)
        if normalized_profile not in {"cost_savings", "ai_optimisation"}:
            return None
        current_sub_type = self.current_savings_sub_type()
        if normalized_profile == "ai_optimisation":
            return current_sub_type or SAVINGS_OPERATION_MODE_SUBTYPE
        return current_sub_type

    def normalize_battery_reserve_for_profile(self, profile: str, reserve: int) -> int:
        if profile == "backup_only":
            return 100
        min_reserve = self.battery_reserve_min_bound()
        max_reserve = self.battery_reserve_max_bound()
        bounded = max(min_reserve, min(max_reserve, int(reserve)))
        return bounded

    def battery_min_soc_floor(self) -> int:
        value = self._coerce_int(
            getattr(self.battery_state, "_battery_very_low_soc_min", None),
            default=None,
        )
        if value is None:
            return BATTERY_MIN_SOC_FALLBACK
        return max(0, min(100, int(value)))

    def battery_reserve_min_bound(self) -> int:
        value = self._coerce_int(
            getattr(self.battery_state, "_battery_backup_percentage_min", None),
            default=None,
        )
        if value is None:
            return self.battery_min_soc_floor()
        return max(0, min(100, int(value)))

    def battery_reserve_max_bound(self) -> int:
        value = self._coerce_int(
            getattr(self.battery_state, "_battery_backup_percentage_max", None),
            default=None,
        )
        if value is None:
            return 100
        return max(0, min(100, int(value)))

    def _parse_battery_control_capability(
        self, raw_control: object
    ) -> BatteryControlCapability | None:
        if not isinstance(raw_control, dict):
            return None
        return BatteryControlCapability(
            show=self._coerce_optional_bool(raw_control.get("show")),
            enabled=self._coerce_optional_bool(raw_control.get("enabled")),
            locked=self._coerce_optional_bool(raw_control.get("locked")),
            show_day_schedule=self._coerce_optional_bool(
                raw_control.get("showDaySchedule")
            ),
            schedule_supported=self._coerce_optional_bool(
                raw_control.get("scheduleSupported")
            ),
            force_schedule_supported=self._coerce_optional_bool(
                raw_control.get("forceScheduleSupported")
            ),
            force_schedule_opted=self._coerce_optional_bool(
                raw_control.get("forceScheduleOpted")
            ),
        )

    def _apply_battery_control_state(
        self, attr_name: str, raw_control: object
    ) -> BatteryControlCapability | None:
        state = self.battery_state
        control = self._parse_battery_control_capability(raw_control)
        setattr(state, attr_name, control)
        if attr_name == "_battery_cfg_control":
            if control is None:
                state._battery_cfg_control_show = None
                state._battery_cfg_control_enabled = None
                state._battery_cfg_control_schedule_supported = None
                state._battery_cfg_control_force_schedule_supported = None
            else:
                state._battery_cfg_control_show = control.show
                state._battery_cfg_control_enabled = control.enabled
                state._battery_cfg_control_schedule_supported = (
                    control.schedule_supported
                )
                state._battery_cfg_control_force_schedule_supported = (
                    control.force_schedule_supported
                )
        return control

    def _apply_battery_capability_blocks(self, data: dict[str, object]) -> None:
        state = self.battery_state
        if "dtgControl" in data:
            self._apply_battery_control_state(
                "_battery_dtg_control", data.get("dtgControl")
            )
        if "cfgControl" in data:
            self._apply_battery_control_state(
                "_battery_cfg_control", data.get("cfgControl")
            )
        if "rbdControl" in data:
            self._apply_battery_control_state(
                "_battery_rbd_control", data.get("rbdControl")
            )
        if "systemTask" in data:
            state._battery_system_task = self._coerce_optional_bool(
                data.get("systemTask")
            )

    def _apply_battery_user_details(self, user_details: object) -> None:
        if not isinstance(user_details, dict):
            return
        state = self.battery_state
        owner = self._coerce_optional_bool(user_details.get("isOwner"))
        installer = self._coerce_optional_bool(user_details.get("isInstaller"))
        if owner is not None:
            state._battery_user_is_owner = owner
        if installer is not None:
            state._battery_user_is_installer = installer

    def _apply_battery_permission_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload
        self._apply_battery_capability_blocks(data)
        self._apply_battery_user_details(data.get("userDetails"))

    def _assert_battery_system_not_busy(
        self, unavailable_message: str = "Battery updates are unavailable."
    ) -> None:
        if self.coordinator.battery_system_task is True:
            raise ServiceValidationError(unavailable_message)

    def _assert_battery_profile_feature_writable(
        self, unavailable_message: str
    ) -> None:
        coord = self.coordinator
        self._assert_battery_system_not_busy(unavailable_message)
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            raise ServiceValidationError(
                "Battery profile updates are not permitted for this account."
            )

    def _assert_battery_settings_feature_writable(
        self, unavailable_message: str
    ) -> None:
        coord = self.coordinator
        self._assert_battery_system_not_busy(unavailable_message)
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            raise ServiceValidationError(
                "Battery settings updates are not permitted for this account."
            )

    def assert_battery_profile_write_allowed(self) -> None:
        coord = self.coordinator
        state = self.battery_state
        lock = getattr(state, "_battery_profile_write_lock", None)
        if lock is not None and lock.locked():
            raise ServiceValidationError(
                "Another battery profile update is already in progress."
            )
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            raise ServiceValidationError(
                "Battery profile updates are not permitted for this account."
            )
        self._assert_battery_system_not_busy("Battery profile updates are unavailable.")

        now = time.monotonic()
        last = getattr(state, "_battery_profile_last_write_mono", None)
        if (
            last is not None
            and now >= last
            and (now - last) < BATTERY_PROFILE_WRITE_DEBOUNCE_S
        ):
            raise ServiceValidationError(
                "Battery profile update requested too quickly. Please wait and try again."
            )

    async def async_ensure_battery_write_access_confirmed(
        self,
        *,
        denied_message: str = "Battery updates are not permitted for this account.",
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        if coord.battery_write_access_confirmed:
            return
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            raise ServiceValidationError(denied_message)
        fetcher = getattr(coord.client, "battery_site_settings", None)
        if callable(fetcher):
            try:
                payload = await fetcher()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Battery write-access refresh failed: %s",
                    redact_text(err, site_ids=(coord.site_id,)),
                )
            else:
                redacted_payload = coord.redact_battery_payload(payload)
                if isinstance(redacted_payload, dict):
                    state._battery_site_settings_payload = redacted_payload
                else:
                    state._battery_site_settings_payload = {"value": redacted_payload}
                self._apply_battery_permission_payload(payload)
        if coord.battery_write_access_confirmed:
            return
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            raise ServiceValidationError(denied_message)
        raise ServiceValidationError(
            "Battery write access could not be confirmed. Refresh and try again."
        )

    async def async_assert_battery_profile_write_allowed(self) -> None:
        self.assert_battery_profile_write_allowed()
        await self.async_ensure_battery_write_access_confirmed()
        self.assert_battery_profile_write_allowed()

    def assert_battery_settings_write_allowed(self) -> None:
        coord = self.coordinator
        state = self.battery_state
        lock = getattr(state, "_battery_settings_write_lock", None)
        if lock is not None and lock.locked():
            raise ServiceValidationError(
                "Another battery settings update is already in progress."
            )
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            raise ServiceValidationError(
                "Battery settings updates are not permitted for this account."
            )
        self._assert_battery_system_not_busy(
            "Battery settings updates are unavailable."
        )
        now = time.monotonic()
        last = getattr(state, "_battery_settings_last_write_mono", None)
        if (
            last is not None
            and now >= last
            and (now - last) < BATTERY_SETTINGS_WRITE_DEBOUNCE_S
        ):
            raise ServiceValidationError(
                "Battery settings update requested too quickly. Please wait and try again."
            )

    async def async_assert_battery_settings_write_allowed(self) -> None:
        self.assert_battery_settings_write_allowed()
        await self.async_ensure_battery_write_access_confirmed()
        self.assert_battery_settings_write_allowed()

    def current_charge_from_grid_schedule_window(self) -> tuple[int, int]:
        state = self.battery_state
        normalize = getattr(self.coordinator, "normalize_minutes_of_day", None)
        if not callable(normalize):
            normalize = getattr(self.coordinator, "_normalize_minutes_of_day", None)
        if not callable(normalize):

            def normalize(value: object) -> int | None:
                if value is None:
                    return None
                try:
                    minutes = int(str(value).strip())
                except Exception:
                    return None
                if minutes < 0 or minutes >= 24 * 60:
                    return None
                return minutes

        begin = normalize(getattr(state, "_battery_charge_begin_time", None))
        end = normalize(getattr(state, "_battery_charge_end_time", None))
        if begin is None:
            begin = 120
        if end is None:
            end = 300
        return begin, end

    def battery_itc_disclaimer_value(self) -> str:
        current = getattr(self.battery_state, "_battery_accepted_itc_disclaimer", None)
        if current:
            return current
        return dt_util.utcnow().isoformat()

    def raise_schedule_update_validation_error(
        self, err: aiohttp.ClientResponseError
    ) -> None:
        if err.status == HTTPStatus.FORBIDDEN:
            raise ServiceValidationError(
                "Schedule update was rejected by Enphase (HTTP 403 Forbidden)."
            ) from err
        if err.status == HTTPStatus.UNAUTHORIZED:
            raise ServiceValidationError(
                "Schedule update could not be authenticated. Reauthenticate and try again."
            ) from err

    async def async_update_battery_schedule(
        self,
        schedule_id: str,
        *,
        start_time: str,
        end_time: str,
        limit: int,
        days: list[int],
        timezone: str,
    ) -> None:
        try:
            await self.coordinator.client.update_battery_schedule(
                schedule_id,
                schedule_type="CFG",
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                days=days,
                timezone=timezone,
            )
        except aiohttp.ClientResponseError as err:
            self.raise_schedule_update_validation_error(err)
            raise

    async def async_apply_battery_profile(
        self,
        *,
        profile: str,
        reserve: int,
        sub_type: str | None = None,
        require_exact_pending_match: bool = True,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        normalized_profile = self.normalize_battery_profile_key(profile)
        if not normalized_profile:
            raise ServiceValidationError("Battery profile is unavailable.")
        await self.async_assert_battery_profile_write_allowed()
        normalized_reserve = self.normalize_battery_reserve_for_profile(
            normalized_profile, reserve
        )
        normalized_sub_type = (
            coord.normalize_battery_sub_type(sub_type)
            if normalized_profile in {"cost_savings", "ai_optimisation"}
            else None
        )
        if (
            normalized_profile == "ai_optimisation"
            and normalized_sub_type != SAVINGS_OPERATION_MODE_SUBTYPE
        ):
            normalized_sub_type = SAVINGS_OPERATION_MODE_SUBTYPE
        async with state._battery_profile_write_lock:
            state._battery_profile_last_write_mono = time.monotonic()
            try:
                await coord.client.set_battery_profile(
                    profile=normalized_profile,
                    battery_backup_percentage=normalized_reserve,
                    operation_mode_sub_type=normalized_sub_type,
                    devices=coord.battery_profile_devices_payload(),
                )
            except aiohttp.ClientResponseError as err:
                if err.status == HTTPStatus.FORBIDDEN:
                    owner = coord.battery_user_is_owner
                    installer = coord.battery_user_is_installer
                    if owner is False and installer is False:
                        raise ServiceValidationError(
                            "Battery profile updates are not permitted for this account."
                        ) from err
                    raise ServiceValidationError(
                        "Battery profile update was rejected by Enphase (HTTP 403 Forbidden)."
                    ) from err
                if err.status == HTTPStatus.UNAUTHORIZED:
                    raise ServiceValidationError(
                        "Battery profile update could not be authenticated. Reauthenticate and try again."
                    ) from err
                raise
        self.remember_battery_reserve(normalized_profile, normalized_reserve)
        self.set_battery_pending(
            profile=normalized_profile,
            reserve=normalized_reserve,
            sub_type=normalized_sub_type,
            require_exact_settings=require_exact_pending_match,
        )
        state._storm_guard_cache_until = None
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    async def async_apply_battery_settings(self, payload: dict[str, object]) -> None:
        coord = self.coordinator
        state = self.battery_state
        if not isinstance(payload, dict) or not payload:
            raise ServiceValidationError("Battery settings payload is unavailable.")
        await self.async_assert_battery_settings_write_allowed()
        async with state._battery_settings_write_lock:
            state._battery_settings_last_write_mono = time.monotonic()
            try:
                await coord.client.set_battery_settings(payload)
            except aiohttp.ClientResponseError as err:
                if err.status == HTTPStatus.FORBIDDEN:
                    raise ServiceValidationError(
                        "Battery settings update was rejected by Enphase (HTTP 403 Forbidden)."
                    ) from err
                if err.status == HTTPStatus.UNAUTHORIZED:
                    raise ServiceValidationError(
                        "Battery settings update could not be authenticated. Reauthenticate and try again."
                    ) from err
                raise
        self.parse_battery_settings_payload(
            payload,
            clear_missing_schedule_times=False,
            clear_missing_reserve_bounds=False,
        )
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    def parse_battery_backup_history_payload(
        self, payload: object
    ) -> list[dict[str, object]] | None:
        coord = self.coordinator
        if not isinstance(payload, dict):
            return None
        histories = payload.get("histories")
        if not isinstance(histories, list):
            return None
        total_records = self._coerce_int(payload.get("total_records"), default=-1)
        total_backup = self._coerce_int(payload.get("total_backup"), default=-1)
        events: list[dict[str, object]] = []
        for item in histories:
            if not isinstance(item, dict):
                continue
            try:
                duration = int(item.get("duration"))
            except (TypeError, ValueError):
                continue
            if duration <= 0:
                continue
            start_raw = item.get("start_time")
            if start_raw is None:
                continue
            try:
                start_text = str(start_raw).strip()
            except Exception:  # noqa: BLE001
                continue
            if not start_text:
                continue
            start = dt_util.parse_datetime(start_text)
            if start is None:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=self.backup_history_tzinfo())
            end = start + timedelta(seconds=duration)
            events.append(
                {
                    "start": start,
                    "end": end,
                    "duration_seconds": duration,
                }
            )
        events.sort(key=lambda item: item["start"])
        if total_records >= 0 and total_records != len(events):
            _LOGGER.debug(
                "Battery backup history total_records mismatch for site %s (payload=%s parsed=%s)",
                redact_site_id(coord.site_id),
                total_records,
                len(events),
            )
        if total_backup >= 0:
            parsed_total_backup = sum(int(item["duration_seconds"]) for item in events)
            if total_backup != parsed_total_backup:
                _LOGGER.debug(
                    "Battery backup history total_backup mismatch for site %s (payload=%s parsed=%s)",
                    redact_site_id(coord.site_id),
                    total_backup,
                    parsed_total_backup,
                )
        return events

    def parse_battery_profile_payload(self, payload: object) -> None:
        state = self.battery_state
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload

        profile = self.normalize_battery_profile_key(data.get("profile"))
        reserve = self._coerce_optional_int(data.get("batteryBackupPercentage"))
        subtype = self._normalize_battery_sub_type(data.get("operationModeSubType"))
        polling_interval = self._coerce_optional_int(data.get("pollingInterval"))
        supports_mqtt = self._coerce_optional_bool(data.get("supportsMqtt"))
        evse_storm_enabled = self._coerce_optional_bool(data.get("evseStormEnabled"))
        storm_state = self.normalize_storm_guard_state(data.get("stormGuardState"))
        self._apply_battery_capability_blocks(data)
        devices: list[dict[str, object]] = []
        profile_evse_device: dict[str, object] | None = None
        raw_devices = data.get("devices")
        if isinstance(raw_devices, dict):
            iq_evse = raw_devices.get("iqEvse")
            if isinstance(iq_evse, list):
                for item in iq_evse:
                    if not isinstance(item, dict):
                        continue
                    uuid = item.get("uuid")
                    if uuid is None:
                        continue
                    try:
                        uuid_text = str(uuid)
                    except Exception:
                        continue
                    devices.append(
                        {
                            "uuid": uuid_text,
                            "chargeMode": item.get("chargeMode"),
                            "enable": self._coerce_optional_bool(item.get("enable")),
                        }
                    )
                    if profile_evse_device is None:
                        profile_evse_device = {
                            "uuid": uuid_text,
                            "device_name": item.get("deviceName"),
                            "profile": self.normalize_battery_profile_key(
                                item.get("profile")
                            )
                            or item.get("profile"),
                            "profile_config": item.get("profileConfig"),
                            "enable": self._coerce_optional_bool(item.get("enable")),
                            "status": self._coerce_optional_int(item.get("status")),
                            "charge_mode": item.get("chargeMode"),
                            "charge_mode_status": item.get("chargeModeStatus"),
                            "updated_at": self._coerce_optional_int(
                                item.get("updatedAt")
                            ),
                        }

        if profile is not None:
            state._battery_profile = profile
        if reserve is not None:
            normalized_reserve = self.normalize_battery_reserve_for_profile(
                profile or state._battery_profile or "self-consumption",
                reserve,
            )
            state._battery_backup_percentage = normalized_reserve
            self.remember_battery_reserve(
                profile or state._battery_profile, normalized_reserve
            )
        if subtype is not None:
            state._battery_operation_mode_sub_type = subtype
        elif profile not in {"cost_savings", "ai_optimisation"}:
            state._battery_operation_mode_sub_type = None
        if supports_mqtt is not None:
            state._battery_supports_mqtt = supports_mqtt
        if polling_interval is not None and polling_interval > 0:
            state._battery_polling_interval_s = polling_interval
        if storm_state is not None:
            state._storm_guard_state = storm_state
        self.sync_storm_guard_pending(storm_state)
        if evse_storm_enabled is not None:
            state._storm_evse_enabled = evse_storm_enabled
        if devices:
            state._battery_profile_devices = devices
        elif profile is not None:
            state._battery_profile_devices = []
        if profile_evse_device is not None:
            state._battery_profile_evse_device = profile_evse_device

        if self.effective_profile_matches_pending():
            self.clear_battery_pending()

    def parse_battery_status_payload(self, payload: object) -> None:
        state = self.battery_state
        if not isinstance(payload, dict):
            state._battery_storage_data = {}
            state._battery_storage_order = []
            state._battery_aggregate_charge_pct = None
            state._battery_aggregate_status = None
            state._battery_aggregate_status_details = {}
            self._refresh_cached_topology()
            return

        storages = payload.get("storages")
        storage_items = storages if isinstance(storages, list) else []
        snapshots: dict[str, dict[str, object]] = {}
        order: list[str] = []
        status_map: dict[str, str] = {}
        raw_status_map: dict[str, str | None] = {}
        status_text_map: dict[str, str | None] = {}
        total_available_energy = 0.0
        total_capacity = 0.0
        contributing_count = 0
        missing_energy_capacity_keys: list[str] = []
        excluded_count = 0
        worst_key: str | None = None
        worst_status: str | None = None
        worst_severity = self._battery_status_severity_value("normal")

        for item in storage_items:
            if not isinstance(item, dict):
                continue
            raw_payload: dict[str, object] = {}
            for raw_key, raw_value in item.items():
                raw_payload[str(raw_key)] = raw_value

            excluded = self._coerce_optional_bool(raw_payload.get("excluded")) is True
            if excluded:
                excluded_count += 1

            key = self._battery_storage_key(raw_payload)
            if not key or excluded:
                continue

            charge_pct = self._parse_percent_value(raw_payload.get("current_charge"))
            available_energy = self._coerce_optional_kwh(
                raw_payload.get("available_energy")
            )
            max_capacity = self._coerce_optional_kwh(raw_payload.get("max_capacity"))
            status_raw = self._coerce_optional_text(raw_payload.get("status"))
            status_text = self._coerce_optional_text(raw_payload.get("statusText"))
            normalized_status_raw = self._normalize_battery_status_text(status_raw)
            normalized_status_text = self._normalize_battery_status_text(status_text)
            normalized_status = normalized_status_raw
            if normalized_status in (
                None,
                "unknown",
            ) and normalized_status_text not in (
                None,
                "unknown",
            ):
                normalized_status = normalized_status_text
            if normalized_status is None:
                normalized_status = "unknown"
            severity = self._battery_status_severity_value(normalized_status)

            snapshot = dict(raw_payload)
            normalized_battery_id = self._normalize_battery_id(raw_payload.get("id"))
            if normalized_battery_id is not None:
                snapshot["id"] = normalized_battery_id
                snapshot["battery_id"] = normalized_battery_id
            snapshot["identity"] = key
            snapshot["current_charge_pct"] = charge_pct
            snapshot["available_energy_kwh"] = available_energy
            snapshot["max_capacity_kwh"] = max_capacity
            snapshot["status_normalized"] = normalized_status
            snapshot["status_text"] = status_text
            snapshots[key] = snapshot
            order.append(key)

            status_map[key] = normalized_status
            raw_status_map[key] = status_raw
            status_text_map[key] = status_text

            if (
                max_capacity is not None
                and max_capacity > 0
                and available_energy is not None
            ):
                total_capacity += max_capacity
                total_available_energy += available_energy
                contributing_count += 1
            else:
                missing_energy_capacity_keys.append(key)

            if severity > worst_severity:
                worst_severity = severity
                worst_status = normalized_status
                worst_key = key
            elif worst_status is None:
                worst_status = normalized_status
                worst_key = key

        aggregate_charge = None
        site_current_charge = self._parse_percent_value(payload.get("current_charge"))
        aggregate_charge_source = "unknown"
        included_count = len(snapshots)
        can_compute_weighted = (
            included_count > 0
            and contributing_count == included_count
            and total_capacity > 0
        )
        if can_compute_weighted:
            aggregate_charge = round((total_available_energy / total_capacity) * 100, 1)
            aggregate_charge_source = "computed"
        elif site_current_charge is not None:
            aggregate_charge = site_current_charge
            aggregate_charge_source = "site_current_charge"

        aggregate_status = worst_status or ("normal" if snapshots else "unknown")

        state._battery_storage_data = snapshots
        state._battery_storage_order = list(dict.fromkeys(order))
        state._battery_aggregate_charge_pct = aggregate_charge
        state._battery_aggregate_status = aggregate_status
        state._battery_aggregate_status_details = {
            "status": aggregate_status,
            "worst_storage_key": worst_key,
            "worst_status": worst_status,
            "per_battery_status": status_map,
            "per_battery_status_raw": raw_status_map,
            "per_battery_status_text": status_text_map,
            "included_count": included_count,
            "contributing_count": contributing_count,
            "aggregate_charge_source": aggregate_charge_source,
            "missing_energy_capacity_keys": list(
                dict.fromkeys(missing_energy_capacity_keys)
            ),
            "excluded_count": excluded_count,
            "available_energy_kwh": round(total_available_energy, 2),
            "max_capacity_kwh": round(total_capacity, 2),
            "site_current_charge_pct": site_current_charge,
            "site_available_energy_kwh": self._coerce_optional_kwh(
                payload.get("available_energy")
            ),
            "site_max_capacity_kwh": self._coerce_optional_kwh(
                payload.get("max_capacity")
            ),
            "site_available_power_kw": self._coerce_optional_float(
                payload.get("available_power")
            ),
            "site_max_power_kw": self._coerce_optional_float(payload.get("max_power")),
            "site_total_micros": self._coerce_optional_int(payload.get("total_micros")),
            "site_active_micros": self._coerce_optional_int(
                payload.get("active_micros")
            ),
            "site_inactive_micros": self._coerce_optional_int(
                payload.get("inactive_micros")
            ),
            "site_included_count": self._coerce_optional_int(
                payload.get("included_count")
            ),
            "site_excluded_count": self._coerce_optional_int(
                payload.get("excluded_count")
            ),
        }
        self._refresh_cached_topology()

    def parse_battery_site_settings_payload(self, payload: object) -> None:
        state = self.battery_state
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload

        def _as_text(value: object) -> str | None:
            if value is None:
                return None
            try:
                text = str(value).strip()
            except Exception:
                return None
            return text or None

        state._battery_show_production = self._coerce_optional_bool(
            data.get("showProduction")
        )
        state._battery_show_consumption = self._coerce_optional_bool(
            data.get("showConsumption")
        )
        state._battery_show_charge_from_grid = self._coerce_optional_bool(
            data.get("showChargeFromGrid")
        )
        state._battery_show_savings_mode = self._coerce_optional_bool(
            data.get("showSavingsMode")
        )
        state._battery_show_ai_opti_savings_mode = self._coerce_optional_bool(
            data.get("showAiOptiSavingsMode")
        )
        state._battery_show_storm_guard = self._coerce_optional_bool(
            data.get("showStormGuard")
        )
        state._battery_show_full_backup = self._coerce_optional_bool(
            data.get("showFullBackup")
        )
        state._battery_show_battery_backup_percentage = self._coerce_optional_bool(
            data.get("showBatteryBackupPercentage")
        )
        state._battery_is_charging_modes_enabled = self._coerce_optional_bool(
            data.get("isChargingModesEnabled")
        )
        state._battery_has_encharge = self._coerce_optional_bool(
            data.get("hasEncharge")
        )
        state._battery_has_enpower = self._coerce_optional_bool(data.get("hasEnpower"))
        state._battery_country_code = _as_text(data.get("countryCode"))
        state._battery_region = _as_text(data.get("region"))
        state._battery_locale = _as_text(data.get("locale"))
        state._battery_timezone = _as_text(data.get("timezone"))
        grid_mode = self._normalize_battery_grid_mode(data.get("batteryGridMode"))
        if grid_mode is not None:
            state._battery_grid_mode = grid_mode
        raw_feature_details = data.get("featureDetails")
        feature_details: dict[str, object] = {}
        if isinstance(raw_feature_details, dict):
            for key, value in raw_feature_details.items():
                key_text = _as_text(key)
                if not key_text:
                    continue
                normalized_bool = self._coerce_optional_bool(value)
                if normalized_bool is not None:
                    feature_details[key_text] = normalized_bool
                    continue
                if isinstance(value, (str, int, float)):
                    feature_details[key_text] = value
        state._battery_feature_details = feature_details
        self._apply_battery_capability_blocks(data)
        self._apply_battery_user_details(data.get("userDetails"))
        site_status = data.get("siteStatus")
        if isinstance(site_status, dict):
            state._battery_site_status_code = _as_text(site_status.get("code"))
            state._battery_site_status_text = _as_text(site_status.get("text"))
            state._battery_site_status_severity = _as_text(site_status.get("severity"))

    def parse_battery_settings_payload(
        self,
        payload: object,
        *,
        clear_missing_schedule_times: bool = True,
        clear_missing_reserve_bounds: bool = True,
    ) -> None:
        state = self.battery_state
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload

        grid_mode = self._normalize_battery_grid_mode(data.get("batteryGridMode"))
        if grid_mode is not None:
            state._battery_grid_mode = grid_mode
        hide_cfg = self._coerce_optional_bool(data.get("hideChargeFromGrid"))
        if hide_cfg is not None:
            state._battery_hide_charge_from_grid = hide_cfg
        supports_vls = self._coerce_optional_bool(data.get("envoySupportsVls"))
        if supports_vls is not None:
            state._battery_envoy_supports_vls = supports_vls
        charge_from_grid = self._coerce_optional_bool(data.get("chargeFromGrid"))
        if charge_from_grid is not None:
            state._battery_charge_from_grid = charge_from_grid
        schedule_enabled = self._coerce_optional_bool(
            data.get("chargeFromGridScheduleEnabled")
        )
        if schedule_enabled is not None:
            state._battery_charge_from_grid_schedule_enabled = schedule_enabled
        begin = self._normalize_minutes_of_day(data.get("chargeBeginTime"))
        if begin is not None:
            state._battery_charge_begin_time = begin
        elif clear_missing_schedule_times:
            state._battery_charge_begin_time = None
        end = self._normalize_minutes_of_day(data.get("chargeEndTime"))
        if end is not None:
            state._battery_charge_end_time = end
        elif clear_missing_schedule_times:
            state._battery_charge_end_time = None
        accepted = data.get("acceptedItcDisclaimer")
        if accepted is not None:
            try:
                state._battery_accepted_itc_disclaimer = str(accepted)
            except Exception:
                state._battery_accepted_itc_disclaimer = None
        very_low_soc = self._coerce_optional_int(data.get("veryLowSoc"))
        if very_low_soc is not None:
            state._battery_very_low_soc = very_low_soc
        very_low_soc_min = self._coerce_optional_int(data.get("veryLowSocMin"))
        if very_low_soc_min is not None:
            state._battery_very_low_soc_min = very_low_soc_min
        very_low_soc_max = self._coerce_optional_int(data.get("veryLowSocMax"))
        if very_low_soc_max is not None:
            state._battery_very_low_soc_max = very_low_soc_max
        settings_profile = self.normalize_battery_profile_key(data.get("profile"))
        if settings_profile is not None:
            state._battery_profile = settings_profile
        settings_reserve = self._coerce_optional_int(
            data.get("batteryBackupPercentage")
        )
        settings_reserve_min = self._coerce_optional_int(
            data.get("batteryBackupPercentageMin")
        )
        if settings_reserve_min is not None:
            state._battery_backup_percentage_min = max(
                0, min(100, int(settings_reserve_min))
            )
        elif clear_missing_reserve_bounds:
            state._battery_backup_percentage_min = None
        settings_reserve_max = self._coerce_optional_int(
            data.get("batteryBackupPercentageMax")
        )
        if settings_reserve_max is not None:
            state._battery_backup_percentage_max = max(
                0, min(100, int(settings_reserve_max))
            )
        elif clear_missing_reserve_bounds:
            state._battery_backup_percentage_max = None
        if settings_reserve is not None:
            state._battery_backup_percentage = (
                self.normalize_battery_reserve_for_profile(
                    settings_profile or state._battery_profile or "self-consumption",
                    settings_reserve,
                )
            )
            self.remember_battery_reserve(
                settings_profile or state._battery_profile,
                state._battery_backup_percentage,
            )
        settings_subtype = self._normalize_battery_sub_type(
            data.get("operationModeSubType")
        )
        if settings_subtype is not None:
            state._battery_operation_mode_sub_type = settings_subtype
        elif (settings_profile or state._battery_profile) not in {
            "cost_savings",
            "ai_optimisation",
        }:
            state._battery_operation_mode_sub_type = None
        storm_state = self.normalize_storm_guard_state(data.get("stormGuardState"))
        if storm_state is not None:
            state._storm_guard_state = storm_state
        self.sync_storm_guard_pending(storm_state)
        self._apply_battery_capability_blocks(data)
        raw_devices = data.get("devices")
        if isinstance(raw_devices, dict):
            iq_evse = raw_devices.get("iqEvse")
            if isinstance(iq_evse, dict):
                use_battery = self._coerce_optional_bool(
                    iq_evse.get("useBatteryFrSelfConsumption")
                )
                if use_battery is not None:
                    state._battery_use_battery_for_self_consumption = use_battery

        if self.effective_profile_matches_pending():
            self.clear_battery_pending()

    def parse_battery_schedules_payload(self, payload: object) -> None:
        state = self.battery_state
        state._battery_cfg_schedule_limit = None
        state._battery_cfg_schedule_id = None
        state._battery_cfg_schedule_days = None
        state._battery_cfg_schedule_timezone = None
        state._battery_cfg_schedule_status = None

        if not isinstance(payload, dict):
            return
        cfg = payload.get("cfg")
        if not isinstance(cfg, dict):
            return
        family_status = cfg.get("scheduleStatus")
        if isinstance(family_status, str) and family_status.strip():
            state._battery_cfg_schedule_status = family_status.strip().lower()

        details = cfg.get("details")
        if not isinstance(details, list) or not details:
            return
        chosen = None
        for entry in details:
            if not isinstance(entry, dict):
                continue
            if chosen is None:
                chosen = entry
            if entry.get("isEnabled") is True:
                chosen = entry
                break
        if chosen is None:
            return

        start_str = chosen.get("startTime")
        end_str = chosen.get("endTime")
        limit = chosen.get("limit")
        schedule_id = chosen.get("scheduleId")
        days = chosen.get("days")
        if isinstance(start_str, str) and ":" in start_str:
            try:
                parts = start_str.split(":")
                state._battery_charge_begin_time = int(parts[0]) * 60 + int(parts[1])
            except (ValueError, IndexError):
                pass
        if isinstance(end_str, str) and ":" in end_str:
            try:
                parts = end_str.split(":")
                state._battery_charge_end_time = int(parts[0]) * 60 + int(parts[1])
            except (ValueError, IndexError):
                pass
        if schedule_id is not None:
            state._battery_cfg_schedule_id = str(schedule_id)
        if isinstance(days, list):
            state._battery_cfg_schedule_days = [int(d) for d in days]
        tz = chosen.get("timezone")
        if not isinstance(tz, str) or not tz.strip():
            tz = payload.get("timezone") if isinstance(payload, dict) else None
        if isinstance(tz, str) and tz.strip():
            state._battery_cfg_schedule_timezone = tz.strip()
        if isinstance(limit, (int, float)):
            state._battery_cfg_schedule_limit = int(limit)
        entry_status = chosen.get("scheduleStatus")
        family_status = cfg.get("scheduleStatus") if isinstance(cfg, dict) else None
        status = entry_status or family_status
        if isinstance(status, str) and status.strip():
            state._battery_cfg_schedule_status = status.strip().lower()

    def parse_dry_contact_settings_payload(self, payload: object) -> None:
        state = self.battery_state
        state._dry_contact_settings_entries = []
        state._dry_contact_unmatched_settings = []
        if not isinstance(payload, dict):
            state._dry_contact_settings_supported = False
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload
        raw_entries: list[dict[str, object]] = []
        visited: set[int] = set()

        def _visit(node: object, depth: int = 0) -> None:
            if depth > 4:
                return
            if isinstance(node, dict):
                node_id = id(node)
                if node_id in visited:
                    return
                visited.add(node_id)
                if self._dry_contact_settings_looks_like_entry(node):
                    raw_entries.append(node)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        _visit(value, depth + 1)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, (dict, list)):
                        _visit(item, depth + 1)

        _visit(data)
        entries: list[dict[str, object]] = []
        seen_signatures: set[tuple[object, ...]] = set()
        for entry in raw_entries:
            normalized: dict[str, object] = {}
            serial_number = self._coerce_optional_text(
                entry.get("serial_number")
                if entry.get("serial_number") is not None
                else (
                    entry.get("serial")
                    if entry.get("serial") is not None
                    else (
                        entry.get("serialNumber")
                        if entry.get("serialNumber") is not None
                        else entry.get("deviceSerial")
                    )
                )
            )
            if serial_number is not None:
                normalized["serial_number"] = serial_number
            device_uid = self._coerce_optional_text(
                entry.get("device_uid")
                if entry.get("device_uid") is not None
                else (
                    entry.get("device-uid")
                    if entry.get("device-uid") is not None
                    else (
                        entry.get("deviceUid")
                        if entry.get("deviceUid") is not None
                        else (
                            entry.get("iqer_uid")
                            if entry.get("iqer_uid") is not None
                            else entry.get("iqer-uid")
                        )
                    )
                )
            )
            if device_uid is not None:
                normalized["device_uid"] = device_uid
            uid = self._coerce_optional_text(entry.get("uid"))
            if uid is not None:
                normalized["uid"] = uid
            contact_id = self._coerce_optional_text(
                entry.get("contact_id")
                if entry.get("contact_id") is not None
                else (
                    entry.get("contactId")
                    if entry.get("contactId") is not None
                    else entry.get("id")
                )
            )
            if contact_id is not None:
                normalized["contact_id"] = contact_id
            channel_type = self._coerce_optional_text(
                entry.get("channel_type")
                if entry.get("channel_type") is not None
                else (
                    entry.get("channelType")
                    if entry.get("channelType") is not None
                    else (
                        entry.get("meter_type")
                        if entry.get("meter_type") is not None
                        else entry.get("type")
                    )
                )
            )
            if channel_type is not None:
                normalized["channel_type"] = channel_type
            configured_name = self._coerce_optional_text(
                entry.get("configured_name")
                if entry.get("configured_name") is not None
                else (
                    entry.get("configuredName")
                    if entry.get("configuredName") is not None
                    else (
                        entry.get("display_name")
                        if entry.get("display_name") is not None
                        else (
                            entry.get("displayName")
                            if entry.get("displayName") is not None
                            else (
                                entry.get("name")
                                if entry.get("name") is not None
                                else entry.get("label")
                            )
                        )
                    )
                )
            )
            if configured_name is not None:
                normalized["configured_name"] = configured_name
            override_supported = self._coerce_optional_bool(
                entry.get("override_supported")
                if entry.get("override_supported") is not None
                else (
                    entry.get("overrideSupported")
                    if entry.get("overrideSupported") is not None
                    else (
                        entry.get("isOverrideSupported")
                        if entry.get("isOverrideSupported") is not None
                        else (
                            entry.get("supportsOverride")
                            if entry.get("supportsOverride") is not None
                            else (
                                entry.get("allowOverride")
                                if entry.get("allowOverride") is not None
                                else entry.get("canOverride")
                            )
                        )
                    )
                )
            )
            if override_supported is not None:
                normalized["override_supported"] = override_supported
            override_active = self._coerce_optional_bool(
                entry.get("override_active")
                if entry.get("override_active") is not None
                else (
                    entry.get("overrideActive")
                    if entry.get("overrideActive") is not None
                    else (
                        entry.get("override")
                        if entry.get("override") is not None
                        else (
                            entry.get("isOverrideActive")
                            if entry.get("isOverrideActive") is not None
                            else entry.get("manualOverride")
                        )
                    )
                )
            )
            if override_active is not None:
                normalized["override_active"] = override_active
            control_mode = self._coerce_optional_text(
                entry.get("control_mode")
                if entry.get("control_mode") is not None
                else (
                    entry.get("controlMode")
                    if entry.get("controlMode") is not None
                    else (
                        entry.get("mode")
                        if entry.get("mode") is not None
                        else entry.get("operatingMode")
                    )
                )
            )
            if control_mode is not None:
                normalized["control_mode"] = control_mode
            polling_interval = self._coerce_optional_int(
                entry.get("polling_interval_seconds")
                if entry.get("polling_interval_seconds") is not None
                else (
                    entry.get("pollingIntervalSeconds")
                    if entry.get("pollingIntervalSeconds") is not None
                    else (
                        entry.get("pollingInterval")
                        if entry.get("pollingInterval") is not None
                        else entry.get("polling_interval")
                    )
                )
            )
            if polling_interval is not None:
                normalized["polling_interval_seconds"] = polling_interval
            soc_threshold = self._coerce_optional_int(
                entry.get("soc_threshold")
                if entry.get("soc_threshold") is not None
                else (
                    entry.get("socThreshold")
                    if entry.get("socThreshold") is not None
                    else (
                        entry.get("thresholdSoc")
                        if entry.get("thresholdSoc") is not None
                        else (
                            entry.get("targetSoc")
                            if entry.get("targetSoc") is not None
                            else (
                                entry.get("setPointSoc")
                                if entry.get("setPointSoc") is not None
                                else entry.get("soc")
                            )
                        )
                    )
                )
            )
            if soc_threshold is not None:
                normalized["soc_threshold"] = soc_threshold
            soc_threshold_min = self._coerce_optional_int(
                entry.get("soc_threshold_min")
                if entry.get("soc_threshold_min") is not None
                else (
                    entry.get("socThresholdMin")
                    if entry.get("socThresholdMin") is not None
                    else (
                        entry.get("minimumSoc")
                        if entry.get("minimumSoc") is not None
                        else (
                            entry.get("minSoc")
                            if entry.get("minSoc") is not None
                            else entry.get("minSocThreshold")
                        )
                    )
                )
            )
            if soc_threshold_min is not None:
                normalized["soc_threshold_min"] = soc_threshold_min
            soc_threshold_max = self._coerce_optional_int(
                entry.get("soc_threshold_max")
                if entry.get("soc_threshold_max") is not None
                else (
                    entry.get("socThresholdMax")
                    if entry.get("socThresholdMax") is not None
                    else (
                        entry.get("maximumSoc")
                        if entry.get("maximumSoc") is not None
                        else (
                            entry.get("maxSoc")
                            if entry.get("maxSoc") is not None
                            else entry.get("maxSocThreshold")
                        )
                    )
                )
            )
            if soc_threshold_max is not None:
                normalized["soc_threshold_max"] = soc_threshold_max
            schedule_windows = self._normalize_dry_contact_schedule_windows(
                entry.get("schedule_windows")
                if entry.get("schedule_windows") is not None
                else (
                    entry.get("scheduleWindows")
                    if entry.get("scheduleWindows") is not None
                    else (
                        entry.get("schedule")
                        if entry.get("schedule") is not None
                        else (
                            entry.get("schedules")
                            if entry.get("schedules") is not None
                            else (
                                entry.get("windows")
                                if entry.get("windows") is not None
                                else entry.get("window")
                            )
                        )
                    )
                )
            )
            if not schedule_windows:
                fallback_start = self._coerce_optional_text(
                    entry.get("scheduleStart")
                    if entry.get("scheduleStart") is not None
                    else (
                        entry.get("schedule_start")
                        if entry.get("schedule_start") is not None
                        else (
                            entry.get("windowStart")
                            if entry.get("windowStart") is not None
                            else entry.get("startTime")
                        )
                    )
                )
                fallback_end = self._coerce_optional_text(
                    entry.get("scheduleEnd")
                    if entry.get("scheduleEnd") is not None
                    else (
                        entry.get("schedule_end")
                        if entry.get("schedule_end") is not None
                        else (
                            entry.get("windowEnd")
                            if entry.get("windowEnd") is not None
                            else entry.get("endTime")
                        )
                    )
                )
                if fallback_start is not None or fallback_end is not None:
                    schedule_window: dict[str, object] = {}
                    if fallback_start is not None:
                        schedule_window["start"] = fallback_start
                    if fallback_end is not None:
                        schedule_window["end"] = fallback_end
                    schedule_windows = [schedule_window]
            if schedule_windows:
                normalized["schedule_windows"] = schedule_windows
            if not normalized:
                continue
            signature = (
                normalized.get("serial_number"),
                normalized.get("device_uid"),
                normalized.get("uid"),
                normalized.get("contact_id"),
                normalized.get("channel_type"),
                normalized.get("configured_name"),
                normalized.get("override_supported"),
                normalized.get("override_active"),
                normalized.get("control_mode"),
                normalized.get("polling_interval_seconds"),
                normalized.get("soc_threshold"),
                normalized.get("soc_threshold_min"),
                normalized.get("soc_threshold_max"),
                tuple(
                    (
                        window.get("start") if isinstance(window, dict) else None,
                        window.get("end") if isinstance(window, dict) else None,
                    )
                    for window in normalized.get("schedule_windows", [])
                    if isinstance(window, dict)
                ),
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            entries.append(normalized)
        matches, unmatched = self._match_dry_contact_settings(
            self._dry_contact_members_for_settings(),
            settings_entries=entries,
        )
        _ = matches
        state._dry_contact_settings_entries = entries
        state._dry_contact_unmatched_settings = unmatched
        state._dry_contact_settings_supported = True

    def dry_contact_settings_matches(
        self,
        members: list[dict[str, object]] | tuple[dict[str, object], ...] | object,
    ) -> tuple[list[dict[str, object] | None], list[dict[str, object]]]:
        matches, unmatched = self._match_dry_contact_settings(
            (
                [dict(member) for member in members if isinstance(member, dict)]
                if isinstance(members, (list, tuple))
                else []
            ),
            settings_entries=[
                entry
                for entry in getattr(
                    self.battery_state, "_dry_contact_settings_entries", []
                )
                if isinstance(entry, dict)
            ],
        )
        return (
            [
                (
                    self._copy_dry_contact_settings_entry(entry)
                    if isinstance(entry, dict)
                    else None
                )
                for entry in matches
            ],
            [
                self._copy_dry_contact_settings_entry(entry)
                for entry in unmatched
                if isinstance(entry, dict)
            ],
        )

    def parse_grid_control_check_payload(self, payload: object) -> None:
        state = self.battery_state
        keys = (
            "disableGridControl",
            "activeDownload",
            "sunlightBackupSystemCheck",
            "gridOutageCheck",
            "userInitiatedGridToggle",
        )
        if not isinstance(payload, dict):
            state._grid_control_supported = False
            state._grid_control_disable = None
            state._grid_control_active_download = None
            state._grid_control_sunlight_backup_system_check = None
            state._grid_control_grid_outage_check = None
            state._grid_control_user_initiated_toggle = None
            return
        data = payload.get("data")
        if isinstance(data, dict):
            payload = data
        if not any(key in payload for key in keys):
            state._grid_control_supported = False
            state._grid_control_disable = None
            state._grid_control_active_download = None
            state._grid_control_sunlight_backup_system_check = None
            state._grid_control_grid_outage_check = None
            state._grid_control_user_initiated_toggle = None
            return
        state._grid_control_supported = True
        state._grid_control_disable = self._coerce_optional_bool(
            payload.get("disableGridControl")
        )
        state._grid_control_active_download = self._coerce_optional_bool(
            payload.get("activeDownload")
        )
        state._grid_control_sunlight_backup_system_check = self._coerce_optional_bool(
            payload.get("sunlightBackupSystemCheck")
        )
        state._grid_control_grid_outage_check = self._coerce_optional_bool(
            payload.get("gridOutageCheck")
        )
        state._grid_control_user_initiated_toggle = self._coerce_optional_bool(
            payload.get("userInitiatedGridToggle")
        )

    def backup_history_tzinfo(self) -> _tz | ZoneInfo:
        tz_name = getattr(self.battery_state, "_battery_timezone", None)
        if isinstance(tz_name, str) and tz_name.strip():
            try:
                return ZoneInfo(tz_name.strip())
            except Exception:  # noqa: BLE001
                pass
        default_tz = getattr(dt_util, "DEFAULT_TIME_ZONE", None)
        if default_tz is not None:
            return default_tz
        return _tz.utc

    async def async_refresh_battery_status(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        _ = force
        fetcher = getattr(coord.client, "battery_status", None)
        if not callable(fetcher):
            return
        payload = await fetcher()
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._battery_status_payload = redacted_payload
        else:
            state._battery_status_payload = {"value": redacted_payload}
        self.parse_battery_status_payload(payload)

    async def async_refresh_battery_backup_history(
        self, *, force: bool = False
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        if not force and state._battery_backup_history_cache_until:
            if now < state._battery_backup_history_cache_until:
                return
        fetcher = getattr(coord.client, "battery_backup_history", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Battery backup history fetch failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(err, site_ids=(coord.site_id,)),
            )
            state._battery_backup_history_cache_until = (
                now + BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL
            )
            return
        parsed = self.parse_battery_backup_history_payload(payload)
        if parsed is None:
            _LOGGER.debug(
                "Battery backup history payload was invalid for site %s",
                redact_site_id(coord.site_id),
            )
            state._battery_backup_history_cache_until = (
                now + BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL
            )
            return
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._battery_backup_history_payload = redacted_payload
        else:
            state._battery_backup_history_payload = {"value": redacted_payload}
        state._battery_backup_history_events = parsed
        state._battery_backup_history_cache_until = (
            now + BATTERY_BACKUP_HISTORY_CACHE_TTL
        )

    async def async_refresh_battery_settings(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        pending_profile = getattr(state, "_battery_pending_profile", None)
        if not force and not pending_profile and state._battery_settings_cache_until:
            if now < state._battery_settings_cache_until:
                return
        fetcher = getattr(coord.client, "battery_settings_details", None)
        if not callable(fetcher):
            return
        payload = await fetcher()
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._battery_settings_payload = redacted_payload
        else:
            state._battery_settings_payload = {"value": redacted_payload}
        self.parse_battery_settings_payload(
            payload,
            clear_missing_schedule_times=True,
        )
        state._battery_settings_cache_until = now + BATTERY_SETTINGS_CACHE_TTL

    async def async_refresh_battery_schedules(self) -> None:
        coord = self.coordinator
        state = self.battery_state
        fetcher = getattr(coord.client, "battery_schedules", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Battery schedules fetch failed: %s",
                redact_text(err, site_ids=(coord.site_id,)),
            )
            return
        if not isinstance(payload, dict):
            return
        redacted = coord.redact_battery_payload(payload)
        if isinstance(redacted, dict):
            state._battery_schedules_payload = redacted
        else:
            state._battery_schedules_payload = {"value": redacted}
        self.parse_battery_schedules_payload(payload)

    async def async_refresh_battery_site_settings(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        if not force and state._battery_site_settings_cache_until:
            if now < state._battery_site_settings_cache_until:
                return
        fetcher = getattr(coord.client, "battery_site_settings", None)
        if not callable(fetcher):
            return
        payload = await fetcher()
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._battery_site_settings_payload = redacted_payload
        else:
            state._battery_site_settings_payload = {"value": redacted_payload}
        self.parse_battery_site_settings_payload(payload)
        state._battery_site_settings_cache_until = now + BATTERY_SITE_SETTINGS_CACHE_TTL

    async def async_refresh_grid_control_check(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        if not force and state._grid_control_check_cache_until:
            if now < state._grid_control_check_cache_until:
                return
        fetcher = getattr(coord.client, "grid_control_check", None)
        if not callable(fetcher):
            state._grid_control_supported = None
            state._grid_control_disable = None
            state._grid_control_active_download = None
            state._grid_control_sunlight_backup_system_check = None
            state._grid_control_grid_outage_check = None
            state._grid_control_user_initiated_toggle = None
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            state._grid_control_check_failures += 1
            last_success = getattr(state, "_grid_control_check_last_success_mono", None)
            if (
                not isinstance(last_success, (int, float))
                or (now - float(last_success)) >= GRID_CONTROL_CHECK_STALE_AFTER_S
            ):
                state._grid_control_supported = None
                state._grid_control_disable = None
                state._grid_control_active_download = None
                state._grid_control_sunlight_backup_system_check = None
                state._grid_control_grid_outage_check = None
                state._grid_control_user_initiated_toggle = None
            state._grid_control_check_cache_until = now + 15.0
            _LOGGER.debug(
                "Grid control check fetch failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(err, site_ids=(coord.site_id,)),
            )
            return
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._grid_control_check_payload = redacted_payload
        else:
            state._grid_control_check_payload = {"value": redacted_payload}
        self.parse_grid_control_check_payload(payload)
        state._grid_control_check_failures = 0
        state._grid_control_check_last_success_mono = now
        state._grid_control_check_cache_until = now + GRID_CONTROL_CHECK_CACHE_TTL

    async def async_refresh_dry_contact_settings(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        if not force and state._dry_contact_settings_cache_until:
            if now < state._dry_contact_settings_cache_until:
                return
        fetcher = getattr(coord.client, "dry_contacts_settings", None)
        if not callable(fetcher):
            state._dry_contact_settings_supported = None
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            state._dry_contact_settings_failures += 1
            last_success = getattr(
                state, "_dry_contact_settings_last_success_mono", None
            )
            if (
                not isinstance(last_success, (int, float))
                or (now - float(last_success)) >= DRY_CONTACT_SETTINGS_STALE_AFTER_S
            ):
                state._dry_contact_settings_supported = None
            state._dry_contact_settings_cache_until = (
                now + DRY_CONTACT_SETTINGS_FAILURE_CACHE_TTL
            )
            _LOGGER.debug(
                "Dry contact settings fetch failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(err, site_ids=(coord.site_id,)),
            )
            return
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._dry_contact_settings_payload = redacted_payload
        else:
            state._dry_contact_settings_payload = {"value": redacted_payload}
        self.parse_dry_contact_settings_payload(payload)
        state._dry_contact_settings_failures = 0
        state._dry_contact_settings_last_success_mono = now
        state._dry_contact_settings_cache_until = now + DRY_CONTACT_SETTINGS_CACHE_TTL

    @staticmethod
    def normalize_storm_guard_state(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return "enabled" if value else "disabled"
        if isinstance(value, (int, float)):
            return "enabled" if value != 0 else "disabled"
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("enabled", "disabled"):
                return normalized
            if normalized in ("true", "1", "yes", "y", "on"):
                return "enabled"
            if normalized in ("false", "0", "no", "n", "off"):
                return "disabled"
        return None

    def clear_storm_guard_pending(self) -> None:
        state = self.battery_state
        state._storm_guard_pending_state = None
        state._storm_guard_pending_expires_mono = None

    def set_storm_guard_pending(self, target_state: str) -> None:
        state = self.battery_state
        normalized = self.normalize_storm_guard_state(target_state)
        if normalized is None:
            self.clear_storm_guard_pending()
            return
        state._storm_guard_pending_state = normalized
        state._storm_guard_pending_expires_mono = (
            time.monotonic() + STORM_GUARD_PENDING_HOLD_S
        )

    def sync_storm_guard_pending(self, effective_state: str | None = None) -> None:
        state = self.battery_state
        pending_state = getattr(state, "_storm_guard_pending_state", None)
        if pending_state is None:
            return
        if effective_state is None:
            effective_state = self.normalize_storm_guard_state(
                getattr(state, "_storm_guard_state", None)
            )
        else:
            effective_state = self.normalize_storm_guard_state(effective_state)
        if effective_state == pending_state:
            self.clear_storm_guard_pending()
            return
        expires_at = getattr(state, "_storm_guard_pending_expires_mono", None)
        if expires_at is None:
            self.clear_storm_guard_pending()
            return
        if time.monotonic() >= float(expires_at):
            self.clear_storm_guard_pending()

    def parse_storm_guard_profile(
        self, payload: object
    ) -> tuple[str | None, bool | None]:
        if not isinstance(payload, dict):
            return None, None
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload
        state = self.normalize_storm_guard_state(data.get("stormGuardState"))
        evse = self._coerce_optional_bool(data.get("evseStormEnabled"))
        return state, evse

    def storm_alert_status_is_inactive(self, status: str | None) -> bool:
        if status is None:
            return False
        return status in STORM_ALERT_INACTIVE_STATUSES

    def storm_alert_is_active(self, alert: dict[str, object]) -> bool:
        coord = self.coordinator
        explicit_active = coord.coerce_optional_bool(alert.get("active"))
        if explicit_active is not None:
            return explicit_active
        status = coord.coerce_optional_text(alert.get("status"))
        if status:
            normalized_status = status.strip().lower().replace("_", "-")
            if normalized_status:
                return not self.storm_alert_status_is_inactive(normalized_status)
        return True

    def parse_storm_alert(self, payload: object) -> bool | None:
        coord = self.coordinator
        state = self.battery_state
        if not isinstance(payload, dict):
            return None
        state._storm_alert_critical_override = coord.coerce_optional_bool(
            payload.get("criticalAlertsOverride")
        )
        alerts = payload.get("stormAlerts")
        normalized_alerts: list[dict[str, object]] = []
        derived_alert_active: bool | None = None
        if isinstance(alerts, list):
            derived_alert_active = False
            for alert in alerts:
                alert_active = False
                if isinstance(alert, dict):
                    normalized: dict[str, object] = {}
                    for key in (
                        "id",
                        "name",
                        "source",
                        "status",
                        "active",
                        "critical",
                        "type",
                        "severity",
                        "message",
                        "startTime",
                        "endTime",
                        "region",
                    ):
                        if key in alert and alert.get(key) is not None:
                            normalized[key] = alert.get(key)
                    if not normalized:
                        for key, value in alert.items():
                            if isinstance(value, (str, int, float, bool)):
                                normalized[str(key)] = value
                    if normalized:
                        normalized_alerts.append(normalized)
                    else:
                        normalized_alerts.append({"active": True})
                    alert_active = self.storm_alert_is_active(alert)
                elif alert is not None:
                    try:
                        normalized_alerts.append({"value": str(alert)})
                    except Exception:  # noqa: BLE001
                        normalized_alerts.append({"active": True})
                    alert_active = True
                if alert_active:
                    derived_alert_active = True
        state._storm_alerts = normalized_alerts
        critical_active = coord.coerce_optional_bool(payload.get("criticalAlertActive"))
        if derived_alert_active is None:
            return critical_active
        if critical_active is None:
            return derived_alert_active
        return critical_active or derived_alert_active

    async def async_refresh_storm_guard_profile(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        pending_profile = getattr(state, "_battery_pending_profile", None)
        if not force and not pending_profile and state._storm_guard_cache_until:
            if now < state._storm_guard_cache_until:
                return
        try:
            locale = getattr(coord.hass.config, "language", None)
        except Exception:  # noqa: BLE001
            locale = None
        fetcher = getattr(coord.client, "storm_guard_profile", None)
        if not callable(fetcher):
            return
        payload = await fetcher(locale=locale)
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._battery_profile_payload = redacted_payload
        else:
            state._battery_profile_payload = {"value": redacted_payload}
        self.parse_battery_profile_payload(payload)
        storm_state, evse = self.parse_storm_guard_profile(payload)
        if storm_state is not None:
            state._storm_guard_state = storm_state
        self.sync_storm_guard_pending(storm_state)
        if evse is not None:
            state._storm_evse_enabled = evse
        state._storm_guard_cache_until = now + STORM_GUARD_CACHE_TTL

    async def async_refresh_storm_alert(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        if not force and state._storm_alert_cache_until:
            if now < state._storm_alert_cache_until:
                return
        fetcher = getattr(coord.client, "storm_guard_alert", None)
        if not callable(fetcher):
            return
        payload = await fetcher()
        active = self.parse_storm_alert(payload)
        if active is not None:
            state._storm_alert_active = active
        state._storm_alert_cache_until = now + STORM_ALERT_CACHE_TTL

    async def async_set_battery_reserve(self, reserve: int) -> None:
        coord = self.coordinator
        profile = coord.battery_selected_profile
        if not profile:
            raise ServiceValidationError("Battery profile is unavailable.")
        if profile == "backup_only":
            raise ServiceValidationError("Full Backup reserve is fixed at 100%.")
        self._assert_battery_profile_feature_writable("Battery reserve is unavailable.")
        if not coord.battery_reserve_editable:
            raise ServiceValidationError("Battery reserve is unavailable.")
        normalized = self.normalize_battery_reserve_for_profile(profile, reserve)
        sub_type = self.target_operation_mode_sub_type(profile)
        await self.async_apply_battery_profile(
            profile=profile,
            reserve=normalized,
            sub_type=sub_type,
        )

    async def async_set_savings_use_battery_after_peak(self, enabled: bool) -> None:
        coord = self.coordinator
        profile = coord.battery_selected_profile
        if profile != "cost_savings":
            raise ServiceValidationError("Savings profile must be active.")
        self._assert_battery_profile_feature_writable(
            "Savings profile settings are unavailable."
        )
        if not coord.savings_use_battery_switch_available:
            raise ServiceValidationError("Savings profile settings are unavailable.")
        reserve = coord.battery_selected_backup_percentage
        if reserve is None:
            reserve = self.target_reserve_for_profile("cost_savings")
        sub_type = SAVINGS_OPERATION_MODE_SUBTYPE if enabled else None
        await self.async_apply_battery_profile(
            profile="cost_savings",
            reserve=reserve,
            sub_type=sub_type,
        )

    async def async_set_system_profile(self, profile_key: str) -> None:
        coord = self.coordinator
        profile = self.normalize_battery_profile_key(profile_key)
        if not profile:
            raise ServiceValidationError("Battery profile is unavailable.")
        if profile not in coord.battery_profile_option_keys:
            raise ServiceValidationError("Selected battery profile is not supported.")
        self._assert_battery_profile_feature_writable("Battery profile is unavailable.")
        reserve = self.target_reserve_for_profile(profile)
        sub_type = self.target_operation_mode_sub_type(profile)
        await self.async_apply_battery_profile(
            profile=profile,
            reserve=reserve,
            sub_type=sub_type,
            require_exact_pending_match=False,
        )

    async def async_cancel_pending_profile_change(self) -> None:
        coord = self.coordinator
        state = self.battery_state
        if not coord.battery_profile_pending:
            self.clear_battery_pending()
            return
        await self.async_assert_battery_profile_write_allowed()
        async with state._battery_profile_write_lock:
            state._battery_profile_last_write_mono = time.monotonic()
            await coord.client.cancel_battery_profile_update()
        self.clear_battery_pending()
        state._storm_guard_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    async def async_set_charge_from_grid(self, enabled: bool) -> None:
        coord = self.coordinator
        self._assert_battery_settings_feature_writable(
            "Charge from grid setting is unavailable."
        )
        if not coord.charge_from_grid_control_available:
            raise ServiceValidationError("Charge from grid setting is unavailable.")
        payload: dict[str, object] = {"chargeFromGrid": bool(enabled)}
        if enabled:
            start, end = self.current_charge_from_grid_schedule_window()
            payload["acceptedItcDisclaimer"] = self.battery_itc_disclaimer_value()
            payload["chargeBeginTime"] = start
            payload["chargeEndTime"] = end
            payload["chargeFromGridScheduleEnabled"] = bool(
                getattr(coord, "_battery_charge_from_grid_schedule_enabled", None)
            )
        await self.async_apply_battery_settings(payload)

    async def async_set_charge_from_grid_schedule_enabled(self, enabled: bool) -> None:
        coord = self.coordinator
        self._assert_battery_settings_feature_writable(
            "Charge from grid schedule is unavailable."
        )
        if not coord.charge_from_grid_force_schedule_supported:
            raise ServiceValidationError("Charge from grid schedule is unavailable.")
        if coord.battery_charge_from_grid_enabled is not True:
            raise ServiceValidationError("Charge from grid must be enabled first.")
        start, end = self.current_charge_from_grid_schedule_window()
        if start == end:
            raise ServiceValidationError(
                "Charge-from-grid schedule start and end times must be different."
            )
        payload: dict[str, object] = {
            "chargeFromGrid": True,
            "chargeFromGridScheduleEnabled": bool(enabled),
            "chargeBeginTime": start,
            "chargeEndTime": end,
            "acceptedItcDisclaimer": self.battery_itc_disclaimer_value(),
        }
        await self.async_apply_battery_settings(payload)

    async def async_set_charge_from_grid_schedule_time(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        self._assert_battery_settings_feature_writable(
            "Charge from grid schedule is unavailable."
        )
        if not coord.charge_from_grid_schedule_supported:
            raise ServiceValidationError("Charge from grid schedule is unavailable.")
        if coord.battery_charge_from_grid_enabled is not True:
            raise ServiceValidationError("Charge from grid must be enabled first.")
        if coord.battery_cfg_schedule_pending:
            raise ServiceValidationError(
                "A schedule change is pending Envoy sync. Please wait."
            )
        current_start, current_end = self.current_charge_from_grid_schedule_window()
        next_start = (
            coord.time_to_minutes_of_day(start) if start is not None else current_start
        )
        next_end = coord.time_to_minutes_of_day(end) if end is not None else current_end
        if next_start is None or next_end is None:
            raise ServiceValidationError("Charge-from-grid schedule time is invalid.")
        if next_start == next_end:
            raise ServiceValidationError(
                "Charge-from-grid schedule start and end times must be different."
            )

        schedule_id = getattr(coord, "_battery_cfg_schedule_id", None)
        if schedule_id and hasattr(coord.client, "update_battery_schedule"):
            await self.async_assert_battery_settings_write_allowed()
            async with state._battery_settings_write_lock:
                state._battery_settings_last_write_mono = time.monotonic()
                start_hhmm = f"{next_start // 60:02d}:{next_start % 60:02d}"
                end_hhmm = f"{next_end // 60:02d}:{next_end % 60:02d}"
                limit = getattr(coord, "_battery_cfg_schedule_limit", None)
                if limit is None:
                    limit = 100
                days = getattr(coord, "_battery_cfg_schedule_days", None) or [
                    1,
                    2,
                    3,
                    4,
                    5,
                    6,
                    7,
                ]
                tz = getattr(coord, "_battery_cfg_schedule_timezone", None) or "UTC"
                await self.async_update_battery_schedule(
                    schedule_id,
                    start_time=start_hhmm,
                    end_time=end_hhmm,
                    limit=limit,
                    days=days,
                    timezone=tz,
                )
            state._battery_charge_begin_time = next_start
            state._battery_charge_end_time = next_end
            state._battery_settings_cache_until = None
            coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
            await coord.async_request_refresh()
            return

        if (
            hasattr(coord.client, "create_battery_schedule")
            and getattr(coord, "_battery_schedules_payload", None) is not None
        ):
            await self.async_assert_battery_settings_write_allowed()
            async with state._battery_settings_write_lock:
                state._battery_settings_last_write_mono = time.monotonic()
                start_hhmm = f"{next_start // 60:02d}:{next_start % 60:02d}"
                end_hhmm = f"{next_end // 60:02d}:{next_end % 60:02d}"
                days = [1, 2, 3, 4, 5, 6, 7]
                tz = getattr(coord, "_battery_cfg_schedule_timezone", None) or "UTC"
                _LOGGER.info(
                    "Creating new CFG schedule: %s-%s limit=100 tz=%s",
                    start_hhmm,
                    end_hhmm,
                    tz,
                )
                await coord.client.create_battery_schedule(
                    schedule_type="CFG",
                    start_time=start_hhmm,
                    end_time=end_hhmm,
                    limit=100,
                    days=days,
                    timezone=tz,
                )
            state._battery_charge_begin_time = next_start
            state._battery_charge_end_time = next_end
            state._battery_cfg_schedule_id = None
            state._battery_settings_cache_until = None
            coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
            await coord.async_request_refresh()
            return

        payload: dict[str, object] = {
            "chargeFromGrid": True,
            "chargeFromGridScheduleEnabled": bool(
                getattr(coord, "_battery_charge_from_grid_schedule_enabled", None)
            ),
            "chargeBeginTime": next_start,
            "chargeEndTime": next_end,
            "acceptedItcDisclaimer": self.battery_itc_disclaimer_value(),
        }
        await self.async_apply_battery_settings(payload)

    async def async_set_cfg_schedule_limit(self, limit: int) -> None:
        coord = self.coordinator
        state = self.battery_state
        self._assert_battery_settings_feature_writable(
            "Charge from grid schedule is unavailable."
        )
        if coord.battery_cfg_control_force_schedule_supported is False:
            raise ServiceValidationError("Charge from grid schedule is unavailable.")
        if not hasattr(coord.client, "update_battery_schedule"):
            raise ServiceValidationError(
                "Schedule API not available on this client version."
            )
        if coord.battery_cfg_schedule_pending:
            raise ServiceValidationError(
                "A schedule change is pending Envoy sync. Please wait."
            )
        if (
            getattr(coord, "_battery_cfg_schedule_id", None) is None
            or getattr(coord, "_battery_cfg_schedule_limit", None) is None
            or getattr(coord, "_battery_charge_begin_time", None) is None
            or getattr(coord, "_battery_charge_end_time", None) is None
        ):
            raise ServiceValidationError(
                "No existing charge-from-grid schedule is available."
            )
        await self.async_assert_battery_settings_write_allowed()
        async with state._battery_settings_write_lock:
            state._battery_settings_last_write_mono = time.monotonic()
            current_start, current_end = self.current_charge_from_grid_schedule_window()
            start_hhmm = f"{current_start // 60:02d}:{current_start % 60:02d}"
            end_hhmm = f"{current_end // 60:02d}:{current_end % 60:02d}"
            days = getattr(coord, "_battery_cfg_schedule_days", None) or [
                1,
                2,
                3,
                4,
                5,
                6,
                7,
            ]
            tz = getattr(coord, "_battery_cfg_schedule_timezone", None) or "UTC"
            await self.async_update_battery_schedule(
                getattr(coord, "_battery_cfg_schedule_id", None),
                start_time=start_hhmm,
                end_time=end_hhmm,
                limit=limit,
                days=days,
                timezone=tz,
            )
        state._battery_cfg_schedule_limit = limit
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    async def async_update_cfg_schedule(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
        limit: int | None = None,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        self._assert_battery_settings_feature_writable(
            "Charge from grid schedule is unavailable."
        )
        if not hasattr(coord.client, "update_battery_schedule"):
            raise ServiceValidationError(
                "Schedule API not available on this client version."
            )
        if coord.battery_cfg_schedule_pending:
            raise ServiceValidationError(
                "A schedule change is pending Envoy sync. Please wait."
            )
        schedule_id = getattr(coord, "_battery_cfg_schedule_id", None)
        if schedule_id is None:
            raise ServiceValidationError(
                "No existing charge-from-grid schedule is available."
            )
        current_start, current_end = self._current_schedule_window_from_coordinator()
        if current_start is None or current_end is None:
            raise ServiceValidationError("Current schedule times are not available.")
        next_start = (
            coord.time_to_minutes_of_day(start) if start is not None else current_start
        )
        next_end = coord.time_to_minutes_of_day(end) if end is not None else current_end
        next_limit = (
            limit
            if limit is not None
            else getattr(coord, "_battery_cfg_schedule_limit", None) or 100
        )
        if next_start is None or next_end is None:
            raise ServiceValidationError("Charge-from-grid schedule time is invalid.")
        if next_start == next_end:
            raise ServiceValidationError(
                "Charge-from-grid schedule start and end times must be different."
            )
        if not 5 <= next_limit <= 100:
            raise ServiceValidationError(
                "Charge-from-grid schedule limit must be between 5 and 100."
            )
        shutdown_floor = coord.battery_shutdown_level
        if shutdown_floor is not None and next_limit < shutdown_floor:
            raise ServiceValidationError(
                "Charge-from-grid schedule limit must be at least "
                f"{shutdown_floor}%."
            )
        await self.async_assert_battery_settings_write_allowed()
        async with state._battery_settings_write_lock:
            state._battery_settings_last_write_mono = time.monotonic()
            start_hhmm = f"{next_start // 60:02d}:{next_start % 60:02d}"
            end_hhmm = f"{next_end // 60:02d}:{next_end % 60:02d}"
            days = getattr(coord, "_battery_cfg_schedule_days", None) or [
                1,
                2,
                3,
                4,
                5,
                6,
                7,
            ]
            tz = getattr(coord, "_battery_cfg_schedule_timezone", None) or "UTC"
            await self.async_update_battery_schedule(
                schedule_id,
                start_time=start_hhmm,
                end_time=end_hhmm,
                limit=next_limit,
                days=days,
                timezone=tz,
            )
        state._battery_charge_begin_time = next_start
        state._battery_charge_end_time = next_end
        state._battery_cfg_schedule_limit = next_limit
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    def raise_grid_validation(
        self,
        key: str,
        *,
        placeholders: dict[str, object] | None = None,
        message: str | None = None,
    ) -> None:
        coord = self.coordinator
        func = getattr(coord, "raise_grid_validation", None)
        if callable(func):
            func(
                key,
                placeholders=placeholders,
                message=message,
            )
            return
        func = getattr(coord, "_raise_grid_validation", None)
        if callable(func):
            func(
                key,
                placeholders=placeholders,
                message=message,
            )
            return
        raise_translated_service_validation(
            translation_domain=DOMAIN,
            translation_key=f"exceptions.{key}",
            translation_placeholders=placeholders,
            message=message,
        )

    def grid_envoy_serial(self) -> str | None:
        bucket = self.coordinator.type_bucket("envoy")
        if not isinstance(bucket, dict):
            return None
        devices = bucket.get("devices")
        if not isinstance(devices, list):
            return None
        for device in devices:
            if not isinstance(device, dict):
                continue
            serial = self.coordinator.coerce_optional_text(device.get("serial_number"))
            if serial:
                return serial
        return None

    async def async_assert_grid_toggle_allowed(self) -> None:
        coord = self.coordinator
        await coord.async_refresh_grid_control_check(force=True)
        if coord.grid_control_supported is not True:
            self.raise_grid_validation("grid_control_unavailable")
        if coord.grid_toggle_allowed is True:
            return
        reasons = coord.grid_toggle_blocked_reasons
        reasons_text = ", ".join(reasons) if reasons else "unknown"
        self.raise_grid_validation(
            "grid_control_blocked",
            placeholders={"reasons": reasons_text},
        )

    async def async_request_grid_toggle_otp(self) -> None:
        coord = self.coordinator
        await self.async_assert_grid_toggle_allowed()
        requester = getattr(coord.client, "request_grid_toggle_otp", None)
        if not callable(requester):
            self.raise_grid_validation("grid_control_unavailable")
        try:
            await requester()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Grid toggle OTP request failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(err, site_ids=(coord.site_id,)),
            )
            self.raise_grid_validation("grid_control_unavailable")

    async def async_set_grid_mode(self, mode: str, otp: str) -> None:
        coord = self.coordinator
        try:
            normalized_mode = str(mode).strip().lower()
        except Exception:
            normalized_mode = ""
        if normalized_mode not in {"on_grid", "off_grid"}:
            self.raise_grid_validation("grid_mode_invalid")

        otp_text = str(otp).strip() if otp is not None else ""
        if not otp_text:
            self.raise_grid_validation("grid_otp_required")
        if len(otp_text) != 4 or not otp_text.isdigit():
            self.raise_grid_validation("grid_otp_invalid_format")

        await self.async_assert_grid_toggle_allowed()

        validator = getattr(coord.client, "validate_grid_toggle_otp", None)
        if not callable(validator):
            self.raise_grid_validation("grid_control_unavailable")
        try:
            valid = await validator(otp_text)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Grid toggle OTP validation call failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(err, site_ids=(coord.site_id,)),
            )
            self.raise_grid_validation("grid_control_unavailable")
        if valid is not True:
            self.raise_grid_validation("grid_otp_invalid")

        envoy_serial = self.grid_envoy_serial()
        if envoy_serial is None:
            self.raise_grid_validation("grid_envoy_serial_missing")

        grid_state = 2 if normalized_mode == "on_grid" else 1
        setter = getattr(coord.client, "set_grid_state", None)
        if not callable(setter):
            self.raise_grid_validation("grid_control_unavailable")
        try:
            await setter(envoy_serial, grid_state)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Grid mode set request failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(
                    err,
                    site_ids=(coord.site_id,),
                    identifiers=(envoy_serial,),
                ),
            )
            self.raise_grid_validation("grid_control_unavailable")

        old_state = (
            "OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD"
            if normalized_mode == "on_grid"
            else "OPER_RELAY_CLOSED"
        )
        new_state = (
            "OPER_RELAY_CLOSED"
            if normalized_mode == "on_grid"
            else "OPER_RELAY_OFFGRID_AC_GRID_PRESENT"
        )
        logger = getattr(coord.client, "log_grid_change", None)
        if callable(logger):
            try:
                await logger(envoy_serial, old_state, new_state)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Grid toggle audit log failed for site %s: %s",
                    redact_site_id(coord.site_id),
                    redact_text(
                        err,
                        site_ids=(coord.site_id,),
                        identifiers=(envoy_serial,),
                    ),
                )

        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()
        await coord.async_refresh_grid_control_check(force=True)

    async def async_set_grid_connection(
        self, enabled: bool, *, otp: str | None = None
    ) -> None:
        coord = self.coordinator
        if not otp:
            self.raise_grid_validation("grid_otp_required")
        mode = "on_grid" if bool(enabled) else "off_grid"
        await coord.async_set_grid_mode(mode, otp)

    async def async_set_battery_shutdown_level(self, level: int) -> None:
        coord = self.coordinator
        self._assert_battery_settings_feature_writable(
            "Battery shutdown level is unavailable."
        )
        if not coord.battery_shutdown_level_available:
            raise ServiceValidationError("Battery shutdown level is unavailable.")
        try:
            normalized = int(level)
        except Exception as err:  # noqa: BLE001
            raise ServiceValidationError("Battery shutdown level is invalid.") from err
        min_level = coord.battery_shutdown_level_min
        max_level = coord.battery_shutdown_level_max
        if normalized < min_level or normalized > max_level:
            raise ServiceValidationError(
                f"Battery shutdown level must be between {min_level} and {max_level}."
            )
        await self.async_apply_battery_settings({"veryLowSoc": normalized})

    async def async_opt_out_all_storm_alerts(self) -> None:
        coord = self.coordinator
        await coord.async_refresh_storm_alert(force=True)

        actionable: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for alert in coord.storm_alerts:
            if not isinstance(alert, dict):
                continue
            alert_id = coord.coerce_optional_text(alert.get("id"))
            if not alert_id or alert_id in seen_ids:
                continue
            if not self.storm_alert_is_active(alert):
                continue
            name = (
                coord.coerce_optional_text(alert.get("name")) or "Storm Alert"
            )  # noqa: SLF001
            actionable.append((alert_id, name))
            seen_ids.add(alert_id)

        if not actionable:
            return

        opt_out = getattr(coord.client, "opt_out_storm_alert", None)
        if not callable(opt_out):
            raise ServiceValidationError("Storm Alert opt-out is unavailable.")

        failures: list[tuple[str, Exception]] = []
        for alert_id, name in actionable:
            try:
                await opt_out(alert_id=alert_id, name=name)
            except Exception as err:  # noqa: BLE001
                failures.append((alert_id, err))
                _LOGGER.warning(
                    "Storm Alert opt-out failed for site %s alert %s: %s",
                    redact_site_id(coord.site_id),
                    redact_identifier(alert_id),
                    redact_text(
                        err,
                        site_ids=(coord.site_id,),
                        identifiers=(alert_id,),
                    ),
                )

        refresh_err: Exception | None = None
        self.battery_state._storm_alert_cache_until = None
        try:
            await coord.async_refresh_storm_alert(force=True)
            coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
            await coord.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            refresh_err = err
            _LOGGER.warning(
                "Storm Alert opt-out refresh failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(err, site_ids=(coord.site_id,)),
            )

        if failures:
            raise ServiceValidationError(
                f"Storm Alert opt-out failed for {len(failures)} alert(s)."
            )
        if refresh_err is not None:
            raise refresh_err

    async def async_set_storm_guard_enabled(self, enabled: bool) -> None:
        coord = self.coordinator
        await self.async_ensure_battery_write_access_confirmed(
            denied_message="Storm Guard updates are not permitted for this account."
        )
        await coord.async_refresh_storm_guard_profile(force=True)
        if getattr(coord, "_storm_evse_enabled", None) is None:
            raise ServiceValidationError("Storm Guard settings are unavailable.")
        target_state = "enabled" if enabled else "disabled"
        self.set_storm_guard_pending(target_state)
        try:
            await coord.client.set_storm_guard(
                enabled=bool(enabled),
                evse_enabled=bool(getattr(coord, "_storm_evse_enabled", None)),
            )
        except aiohttp.ClientResponseError as err:
            self.clear_storm_guard_pending()
            if err.status == HTTPStatus.FORBIDDEN:
                owner = coord.battery_user_is_owner
                installer = coord.battery_user_is_installer
                if owner is False and installer is False:
                    raise ServiceValidationError(
                        "Storm Guard updates are not permitted for this account."
                    ) from err
                raise ServiceValidationError(
                    "Storm Guard update was rejected by Enphase (HTTP 403 Forbidden)."
                ) from err
            if err.status == HTTPStatus.UNAUTHORIZED:
                raise ServiceValidationError(
                    "Storm Guard update could not be authenticated. Reauthenticate and try again."
                ) from err
            raise
        except Exception:
            self.clear_storm_guard_pending()
            raise
        self.battery_state._storm_guard_cache_until = None
        self.sync_storm_guard_pending(getattr(coord, "_storm_guard_state", None))

    async def async_set_storm_evse_enabled(self, enabled: bool) -> None:
        coord = self.coordinator
        await self.async_ensure_battery_write_access_confirmed(
            denied_message="Storm Guard updates are not permitted for this account."
        )
        await coord.async_refresh_storm_guard_profile(force=True)
        if getattr(coord, "_storm_guard_state", None) is None:
            raise ServiceValidationError("Storm Guard settings are unavailable.")
        try:
            await coord.client.set_storm_guard(
                enabled=getattr(coord, "_storm_guard_state", None) == "enabled",
                evse_enabled=bool(enabled),
            )
        except aiohttp.ClientResponseError as err:
            if err.status == HTTPStatus.FORBIDDEN:
                owner = coord.battery_user_is_owner
                installer = coord.battery_user_is_installer
                if owner is False and installer is False:
                    raise ServiceValidationError(
                        "Storm Guard updates are not permitted for this account."
                    ) from err
                raise ServiceValidationError(
                    "Storm Guard update was rejected by Enphase (HTTP 403 Forbidden)."
                ) from err
            if err.status == HTTPStatus.UNAUTHORIZED:
                raise ServiceValidationError(
                    "Storm Guard update could not be authenticated. Reauthenticate and try again."
                ) from err
            raise
        self.battery_state._storm_evse_enabled = bool(enabled)
        self.battery_state._storm_guard_cache_until = (
            time.monotonic() + STORM_GUARD_CACHE_TTL
        )
