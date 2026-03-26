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
    BATTERY_PROFILE_LABELS,
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
from .log_redaction import redact_identifier, redact_site_id, redact_text
from .service_validation import raise_translated_service_validation

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
        coord = self.coordinator
        func = getattr(coord, "coerce_int", None)
        if callable(func):
            return func(value, default=default)
        func = getattr(coord, "_coerce_int", None)
        if callable(func):
            return func(value, default=default)
        try:
            return int(value)
        except Exception:
            return default

    def _coerce_optional_bool(self, value: object) -> bool | None:
        coord = self.coordinator
        func = getattr(coord, "coerce_optional_bool", None)
        if callable(func):
            return func(value)
        func = getattr(coord, "_coerce_optional_bool", None)
        if callable(func):
            return func(value)
        return None

    def _coerce_optional_text(self, value: object) -> str | None:
        coord = self.coordinator
        func = getattr(coord, "coerce_optional_text", None)
        if callable(func):
            return func(value)
        func = getattr(coord, "_coerce_optional_text", None)
        if callable(func):
            return func(value)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

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
    def battery_profile_label(profile: str | None) -> str | None:
        if not profile:
            return None
        if profile in BATTERY_PROFILE_LABELS:
            return BATTERY_PROFILE_LABELS[profile]
        try:
            return str(profile).replace("_", " ").replace("-", " ").title()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_pending_sub_type(
        coordinator: EnphaseCoordinator, profile: str, sub_type: str | None
    ) -> str | None:
        if profile != "cost_savings":
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
        if pending_profile != "cost_savings":
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

    def normalize_battery_reserve_for_profile(self, profile: str, reserve: int) -> int:
        if profile == "backup_only":
            return 100
        min_reserve = self.battery_min_soc_floor()
        bounded = max(min_reserve, min(100, int(reserve)))
        return bounded

    def battery_min_soc_floor(self) -> int:
        value = self._coerce_int(
            getattr(self.battery_state, "_battery_very_low_soc_min", None),
            default=None,
        )
        if value is None:
            return BATTERY_MIN_SOC_FALLBACK
        return max(0, min(100, int(value)))

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
        self.assert_battery_profile_write_allowed()
        normalized_profile = self.normalize_battery_profile_key(profile)
        if not normalized_profile:
            raise ServiceValidationError("Battery profile is unavailable.")
        normalized_reserve = self.normalize_battery_reserve_for_profile(
            normalized_profile, reserve
        )
        normalized_sub_type = (
            coord.normalize_battery_sub_type(sub_type)
            if normalized_profile == "cost_savings"
            else None
        )
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
        self.assert_battery_settings_write_allowed()
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
        coord.parse_battery_settings_payload(
            payload, clear_missing_schedule_times=False
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
        coord.parse_battery_status_payload(payload)

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
        coord.parse_battery_settings_payload(payload, clear_missing_schedule_times=True)
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
        coord.parse_battery_schedules_payload(payload)

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
        coord.parse_battery_site_settings_payload(payload)
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
        coord.parse_grid_control_check_payload(payload)
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
        coord.parse_dry_contact_settings_payload(payload)
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
        coord.parse_battery_profile_payload(payload)
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
        normalized = self.normalize_battery_reserve_for_profile(profile, reserve)
        sub_type = (
            self.current_savings_sub_type() if profile == "cost_savings" else None
        )
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
        reserve = self.target_reserve_for_profile(profile)
        sub_type = (
            self.current_savings_sub_type() if profile == "cost_savings" else None
        )
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
        self.assert_battery_profile_write_allowed()
        async with state._battery_profile_write_lock:
            state._battery_profile_last_write_mono = time.monotonic()
            await coord.client.cancel_battery_profile_update()
        self.clear_battery_pending()
        state._storm_guard_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    async def async_set_charge_from_grid(self, enabled: bool) -> None:
        coord = self.coordinator
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
        if not coord.charge_from_grid_schedule_supported:
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
            self.assert_battery_settings_write_allowed()
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
            self.assert_battery_settings_write_allowed()
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
        self.assert_battery_settings_write_allowed()
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
        self.assert_battery_settings_write_allowed()
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
