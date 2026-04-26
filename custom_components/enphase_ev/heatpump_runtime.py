"""Coordinate HEMS heat-pump runtime refreshes and diagnostics snapshots."""

from __future__ import annotations

import inspect
import logging
import time
from datetime import datetime, time as dt_time, timedelta
from datetime import timezone as _tz
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util

from .const import (
    HEMS_SUPPORT_PREFLIGHT_CACHE_TTL,
    HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL,
    HEATPUMP_DAILY_CONSUMPTION_FAILURE_BACKOFF_S,
    HEATPUMP_DAILY_CONSUMPTION_STALE_AFTER_S,
    HEATPUMP_POWER_STALE_AFTER_S,
    HEATPUMP_RUNTIME_DIAGNOSTICS_CACHE_TTL,
    HEATPUMP_RUNTIME_STATE_CACHE_TTL,
    HEATPUMP_RUNTIME_STATE_FAILURE_BACKOFF_S,
    HEATPUMP_RUNTIME_STATE_STALE_AFTER_S,
)
from .device_types import normalize_type_key
from .log_redaction import redact_site_id, redact_text, truncate_identifier
from .parsing_helpers import (
    coerce_optional_bool,
    coerce_optional_float,
    coerce_optional_text,
    heatpump_device_state,
    heatpump_member_device_type,
    heatpump_pairing_status,
    heatpump_status_text,
    parse_inverter_last_report,
    type_member_text,
)
from .runtime_helpers import (
    copy_diagnostics_value,
    redact_battery_payload,
    resolve_site_local_current_date,
    resolve_site_timezone_name,
)
from .state_models import install_state_descriptors

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)

# The power endpoints report cumulative buckets, so derived watts need a delta window.
_HEATPUMP_IDLE_POWER_MAX_W = 20.0
_HEATPUMP_POWER_DEFAULT_WINDOW_S = 300.0
_HEATPUMP_POWER_MIN_DELTA_WH = 0.5
_HEATPUMP_IDLE_SMOOTHING_MIN_WINDOW_S = 15 * 60.0
_HEATPUMP_IDLE_SMOOTHING_MAX_WINDOW_S = 30 * 60.0


class HeatpumpRuntime:
    """HEMS preflight and heat-pump runtime helpers."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self.inventory_state = coordinator.inventory_state
        self.heatpump_state = coordinator.heatpump_state

    @property
    def client(self):
        return self.coordinator.client

    @property
    def site_id(self) -> str:
        return self.coordinator.site_id

    def has_type(self, type_key: object) -> bool:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return False
        bucket = getattr(self, "_type_device_buckets", {}).get(normalized)
        if not isinstance(bucket, dict):
            return False
        try:
            return int(bucket.get("count", 0)) > 0
        except Exception:
            return False

    def _heatpump_mark_known_present(self) -> None:
        self._heatpump_known_present = True

    def heatpump_entities_established(self) -> bool:
        if self.has_type("heatpump"):
            return True
        if bool(getattr(self, "_heatpump_known_present", False)):
            return True
        if isinstance(getattr(self, "_heatpump_runtime_state", None), dict):
            return True
        if isinstance(getattr(self, "_heatpump_daily_consumption", None), dict):
            return True
        return self._heatpump_power_w is not None

    def _type_bucket_members(self, type_key: object) -> list[dict[str, object]]:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return []
        bucket = getattr(self, "_type_device_buckets", {}).get(normalized)
        if not isinstance(bucket, dict):
            return []
        members = bucket.get("devices")
        if not isinstance(members, list):
            return []
        return [dict(item) for item in members if isinstance(item, dict)]

    def _site_timezone_name(self) -> str:
        return resolve_site_timezone_name(
            getattr(self.coordinator, "_battery_timezone", None)
        )

    def _site_local_current_date(self) -> str:
        return resolve_site_local_current_date(
            getattr(self, "_devices_inventory_payload", None),
            getattr(self.coordinator, "_battery_timezone", None),
        )

    def _copy_diagnostics_value(self, value: object) -> object:
        return copy_diagnostics_value(value)

    def _redact_battery_payload(self, payload: object) -> object:
        return redact_battery_payload(payload)

    def _debug_truncate_identifier(self, value: object) -> str | None:
        return truncate_identifier(value)

    def _heatpump_power_redaction_identifiers(
        self, *extra_identifiers: object
    ) -> tuple[str, ...]:
        identifiers: list[str] = []
        seen: set[str] = set()

        def _add(value: object) -> None:
            text = coerce_optional_text(value)
            if not text or text in seen:
                return
            seen.add(text)
            identifiers.append(text)

        for member in self._type_bucket_members("heatpump"):
            for alias in self._heatpump_member_aliases(member):
                _add(alias)
        for value in extra_identifiers:
            _add(value)
        return tuple(identifiers)

    @staticmethod
    def _heatpump_snapshot_is_fresh(
        last_success_mono: object, stale_after_s: float, now: float
    ) -> bool:
        if not isinstance(last_success_mono, (int, float)):
            return False
        age = now - float(last_success_mono)
        return 0 <= age < stale_after_s

    def _heatpump_mark_runtime_state_stale(self, *, now: float, error: str) -> bool:
        if (
            self._heatpump_runtime_state is not None
            and self._heatpump_snapshot_is_fresh(
                self._heatpump_runtime_state_last_success_mono,
                HEATPUMP_RUNTIME_STATE_STALE_AFTER_S,
                now,
            )
        ):
            self._heatpump_runtime_state_using_stale = True
            self._heatpump_runtime_state_last_error = error
            return True
        self._heatpump_runtime_state = None
        self._heatpump_runtime_state_using_stale = False
        self._heatpump_runtime_state_last_error = error
        return False

    def _heatpump_mark_daily_consumption_stale(self, *, now: float, error: str) -> bool:
        if (
            self._heatpump_daily_consumption is not None
            and self._heatpump_snapshot_is_fresh(
                self._heatpump_daily_consumption_last_success_mono,
                HEATPUMP_DAILY_CONSUMPTION_STALE_AFTER_S,
                now,
            )
        ):
            self._heatpump_daily_consumption_using_stale = True
            self._heatpump_daily_consumption_last_error = error
            return True
        self._heatpump_daily_consumption = None
        self._heatpump_daily_consumption_using_stale = False
        self._heatpump_daily_consumption_last_error = error
        return False

    def _heatpump_mark_daily_split_stale(self, *, now: float, error: str) -> bool:
        if self._heatpump_daily_split_available(
            getattr(self, "_heatpump_daily_consumption", None)
        ) and self._heatpump_snapshot_is_fresh(
            self._heatpump_daily_split_last_success_mono,
            HEATPUMP_DAILY_CONSUMPTION_STALE_AFTER_S,
            now,
        ):
            self._heatpump_daily_split_using_stale = True
            self._heatpump_daily_split_last_error = error
            return True
        self._heatpump_daily_split_using_stale = False
        self._heatpump_daily_split_last_error = error
        return False

    def _heatpump_mark_power_stale(
        self,
        *,
        now: float,
        error: str,
        power_snapshot: dict[str, object] | None = None,
    ) -> bool:
        if self._heatpump_power_w is not None and self._heatpump_snapshot_is_fresh(
            self._heatpump_power_last_success_mono,
            HEATPUMP_POWER_STALE_AFTER_S,
            now,
        ):
            self._heatpump_power_using_stale = True
            self._heatpump_power_last_error = error
            if isinstance(power_snapshot, dict):
                power_snapshot["using_stale"] = True
                power_snapshot["last_success_utc"] = (
                    self._heatpump_power_last_success_utc.isoformat()
                    if isinstance(self._heatpump_power_last_success_utc, datetime)
                    else None
                )
            return True
        self._heatpump_power_w = None
        self._heatpump_power_sample_utc = None
        self._heatpump_power_start_utc = None
        self._heatpump_power_device_uid = None
        self._heatpump_power_source = None
        self._heatpump_power_raw_w = None
        self._heatpump_power_window_seconds = None
        self._heatpump_power_validation = None
        self._heatpump_power_smoothed = False
        self._heatpump_power_using_stale = False
        self._heatpump_power_last_error = error
        return False

    def _heatpump_cleanup_due(self, *attrs: str) -> bool:
        for attr in attrs:
            value = getattr(self, attr, None)
            if isinstance(value, bool):
                if value:
                    return True
                continue
            if attr == "_heatpump_power_sample_history" and isinstance(value, list):
                if value:
                    return True
                continue
            if value is not None:
                return True
        return False

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
            is_hems = coerce_optional_bool(payload.get("is_hems"))
            if is_hems is not None:
                self.client._hems_site_supported = is_hems  # noqa: SLF001

        self._hems_support_preflight_cache_until = (
            now + HEMS_SUPPORT_PREFLIGHT_CACHE_TTL
        )

    async def async_refresh_hems_support_preflight(
        self, *, force: bool = False
    ) -> None:
        await self._async_refresh_hems_support_preflight(force=force)

    async def async_ensure_heatpump_runtime_diagnostics(
        self, *, force: bool = False
    ) -> None:
        """Capture optional live/detail payloads used by heat-pump runtime views."""

        if not self.has_type("heatpump"):
            # Home Assistant keeps entities registered after discovery, so clear runtime
            # payloads without treating a missing inventory type as an unload event.
            self._heatpump_runtime_diagnostics_cache_until = None
            self._show_livestream_payload = None
            self._heatpump_events_payloads = []
            self._heatpump_runtime_diagnostics_error = None
            self._heatpump_runtime_state = None
            self._heatpump_runtime_state_cache_until = None
            self._heatpump_runtime_state_backoff_until = None
            self._heatpump_runtime_state_last_error = None
            self._heatpump_daily_consumption = None
            self._heatpump_daily_consumption_cache_until = None
            self._heatpump_daily_consumption_backoff_until = None
            self._heatpump_daily_consumption_last_error = None
            self._heatpump_daily_consumption_cache_key = None
            self._heatpump_daily_split_last_error = None
            self._heatpump_daily_split_last_success_mono = None
            self._heatpump_daily_split_last_success_utc = None
            self._heatpump_daily_split_using_stale = False
            self._heatpump_power_snapshot = None
            return

        now = time.monotonic()
        if not force and self._heatpump_runtime_diagnostics_cache_until is not None:
            if now < self._heatpump_runtime_diagnostics_cache_until:
                return

        try:
            await self._async_refresh_heatpump_runtime_state(force=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Heat pump runtime-state diagnostics refresh failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
        try:
            await self._async_refresh_heatpump_daily_consumption(force=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Heat pump daily-consumption diagnostics refresh failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
        try:
            await self._async_refresh_heatpump_power(force=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Heat pump power diagnostics refresh failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(
                    err,
                    site_ids=(self.site_id,),
                    identifiers=self._heatpump_power_redaction_identifiers(),
                ),
            )

        self._heatpump_runtime_diagnostics_error = None

        show_livestream = getattr(self.client, "show_livestream", None)
        if callable(show_livestream):
            try:
                payload = await show_livestream()
            except Exception as err:  # noqa: BLE001
                self._show_livestream_payload = None
                self._heatpump_runtime_diagnostics_error = (
                    redact_text(err, site_ids=(self.site_id,)) or err.__class__.__name__
                )
            else:
                # Diagnostics may include account-scoped battery data from shared APIs.
                redacted_payload = self._redact_battery_payload(payload)
                if isinstance(redacted_payload, dict):
                    self._show_livestream_payload = redacted_payload
                elif redacted_payload is None:
                    self._show_livestream_payload = None
                else:
                    self._show_livestream_payload = {"value": redacted_payload}

        heat_pump_events_fetcher = getattr(self.client, "heat_pump_events_json", None)
        iq_er_events_fetcher = getattr(self.client, "iq_er_events_json", None)
        if callable(heat_pump_events_fetcher) or callable(iq_er_events_fetcher):
            payloads: list[dict[str, object]] = []
            seen_uids: set[str] = set()
            for member in self._type_bucket_members("heatpump"):
                uid = type_member_text(member, "device_uid", "uid")
                if not uid or uid in seen_uids:
                    continue
                seen_uids.add(uid)
                device_type = heatpump_member_device_type(member)
                payload_entry: dict[str, object] = {
                    "device_uid": uid,
                    "device_type": device_type,
                    "name": type_member_text(member, "name"),
                }

                namespace = "heat_pump" if device_type == "HEAT_PUMP" else "iq_er"
                events_fetcher = (
                    heat_pump_events_fetcher
                    if namespace == "heat_pump"
                    else iq_er_events_fetcher
                )
                payload_entry["events_namespace"] = namespace

                if callable(events_fetcher):
                    try:
                        payload = await events_fetcher(uid)
                    except Exception as err:  # noqa: BLE001
                        payload_entry["error"] = (
                            redact_text(err, site_ids=(self.site_id,))
                            or err.__class__.__name__
                        )
                    else:
                        # Event payloads can include opaque device links and
                        # identifiers.
                        redacted_payload = self._redact_battery_payload(payload)
                        if redacted_payload is None:
                            payload_entry["payload"] = None
                        elif isinstance(redacted_payload, (dict, list)):
                            payload_entry["payload"] = redacted_payload
                        else:
                            payload_entry["payload"] = {"value": redacted_payload}
                payloads.append(payload_entry)
            self._heatpump_events_payloads = payloads

        self._heatpump_runtime_diagnostics_cache_until = (
            now + HEATPUMP_RUNTIME_DIAGNOSTICS_CACHE_TTL
        )

    def _heatpump_primary_member(self) -> dict[str, object] | None:
        members = self._type_bucket_members("heatpump")
        for member in members:
            if heatpump_member_device_type(member) == "HEAT_PUMP":
                return member
        if members:
            return members[0]
        return None

    def _heatpump_primary_device_uid(self) -> str | None:
        members = self._type_bucket_members("heatpump")
        if not members:
            return None
        preferred_types = ("HEAT_PUMP", "ENERGY_METER", "SG_READY_GATEWAY")
        for preferred in preferred_types:
            for member in members:
                if heatpump_member_device_type(member) != preferred:
                    continue
                uid = type_member_text(member, "device_uid")
                if uid:
                    return uid
        for member in members:
            uid = type_member_text(member, "device_uid")
            if uid:
                return uid
        return None

    def _heatpump_runtime_device_uid(self) -> str | None:
        for member in self._type_bucket_members("heatpump"):
            if heatpump_member_device_type(member) != "HEAT_PUMP":
                continue
            uid = type_member_text(member, "device_uid")
            if uid:
                self._heatpump_mark_known_present()
                return uid
        snapshot = getattr(self, "_heatpump_runtime_state", None)
        if isinstance(snapshot, dict):
            return coerce_optional_text(snapshot.get("device_uid"))
        return None

    def _heatpump_runtime_member(self) -> dict[str, object] | None:
        runtime_uid = self._heatpump_runtime_device_uid()
        if runtime_uid:
            member = self._heatpump_member_for_uid(runtime_uid)
            if member is not None:
                return member
        primary = self._heatpump_primary_member()
        if primary is not None:
            return primary
        return None

    async def _async_refresh_heatpump_runtime_state(
        self, *, force: bool = False
    ) -> None:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            if self.heatpump_entities_established():
                self._heatpump_mark_runtime_state_stale(
                    now=now,
                    error="Heat pump type temporarily missing from inventory",
                )
                self._heatpump_runtime_state_cache_until = (
                    now + HEATPUMP_RUNTIME_STATE_CACHE_TTL
                )
                self._heatpump_runtime_state_backoff_until = None
                return
            self._heatpump_runtime_state = None
            self._heatpump_runtime_state_cache_until = None
            self._heatpump_runtime_state_backoff_until = None
            self._heatpump_runtime_state_last_error = None
            self._heatpump_runtime_state_last_success_mono = None
            self._heatpump_runtime_state_last_success_utc = None
            self._heatpump_runtime_state_using_stale = False
            return
        if (
            not force
            and self._heatpump_runtime_state_cache_until is not None
            and now < self._heatpump_runtime_state_cache_until
        ):
            return
        if (
            not force
            and self._heatpump_runtime_state_backoff_until is not None
            and now < self._heatpump_runtime_state_backoff_until
        ):
            return

        await self._async_refresh_hems_support_preflight(force=force)
        if getattr(self.client, "hems_site_supported", None) is False:
            # Enphase returns HEMS-only runtime data for supported sites only.
            self._heatpump_mark_runtime_state_stale(
                now=now,
                error="HEMS runtime endpoint unavailable for this site",
            )
            self._heatpump_runtime_state_cache_until = (
                now + HEATPUMP_RUNTIME_STATE_CACHE_TTL
            )
            self._heatpump_runtime_state_backoff_until = None
            return

        device_uid = self._heatpump_runtime_device_uid()
        if not device_uid:
            self._heatpump_mark_runtime_state_stale(
                now=now,
                error="Heat pump runtime device UID temporarily unavailable",
            )
            self._heatpump_runtime_state_cache_until = (
                now + HEATPUMP_RUNTIME_STATE_CACHE_TTL
            )
            self._heatpump_runtime_state_backoff_until = None
            return

        fetcher = getattr(self.client, "hems_heatpump_state", None)
        if not callable(fetcher):
            return

        try:
            payload = await fetcher(
                device_uid=device_uid,
                timezone=self._site_timezone_name(),
            )
        except Exception as err:  # noqa: BLE001
            error = redact_text(err, site_ids=(self.site_id,)) or err.__class__.__name__
            self._heatpump_mark_runtime_state_stale(now=now, error=error)
            # Backoff preserves recent snapshots while the cloud endpoint is unhealthy.
            self._heatpump_runtime_state_backoff_until = (
                now + HEATPUMP_RUNTIME_STATE_FAILURE_BACKOFF_S
            )
            self._heatpump_runtime_state_cache_until = None
            return

        self._heatpump_runtime_state_cache_until = (
            now + HEATPUMP_RUNTIME_STATE_CACHE_TTL
        )
        self._heatpump_runtime_state_backoff_until = None
        if not isinstance(payload, dict):
            self._heatpump_mark_runtime_state_stale(
                now=now, error="No usable HEMS runtime payload"
            )
            return
        snapshot = dict(payload)
        member = self._heatpump_runtime_member()
        if isinstance(member, dict):
            snapshot.setdefault("member_name", type_member_text(member, "name"))
            snapshot.setdefault(
                "member_device_type", heatpump_member_device_type(member)
            )
            snapshot.setdefault("pairing_status", heatpump_pairing_status(member))
            snapshot.setdefault("device_state", heatpump_device_state(member))
        snapshot["source"] = f"hems_heatpump_state:{device_uid}"
        self._heatpump_runtime_state = snapshot
        self._heatpump_mark_known_present()
        self._heatpump_runtime_state_last_error = None
        self._heatpump_runtime_state_using_stale = False
        self._heatpump_runtime_state_last_success_mono = now
        self._heatpump_runtime_state_last_success_utc = dt_util.utcnow()

    async def async_refresh_heatpump_runtime_state(
        self, *, force: bool = False
    ) -> None:
        await self._async_refresh_heatpump_runtime_state(force=force)

    def heatpump_runtime_state_refresh_due(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            return self._heatpump_cleanup_due(
                "_heatpump_known_present",
                "_heatpump_runtime_state",
                "_heatpump_runtime_state_cache_until",
                "_heatpump_runtime_state_backoff_until",
                "_heatpump_runtime_state_last_error",
                "_heatpump_runtime_state_last_success_mono",
                "_heatpump_runtime_state_last_success_utc",
                "_heatpump_runtime_state_using_stale",
            )
        if (
            not force
            and self._heatpump_runtime_state_cache_until is not None
            and now < self._heatpump_runtime_state_cache_until
        ):
            return False
        if (
            not force
            and self._heatpump_runtime_state_backoff_until is not None
            and now < self._heatpump_runtime_state_backoff_until
        ):
            return False
        fetcher = getattr(self.client, "hems_heatpump_state", None)
        return callable(fetcher)

    def _heatpump_daily_window(self) -> tuple[str, str, str, tuple[str, str]] | None:
        tz_name = self._site_timezone_name()
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz_name = "UTC"
            tz = _tz.utc
        day_key = self._site_local_current_date()
        try:
            day_date = datetime.fromisoformat(day_key).date()
        except Exception:
            return None
        marker = (day_key, tz_name)
        day_start_local = datetime.combine(day_date, dt_time.min, tzinfo=tz)
        day_end_local = day_start_local + timedelta(days=1) - timedelta(milliseconds=1)
        day_start = day_start_local.astimezone(_tz.utc)
        day_end = day_end_local.astimezone(_tz.utc)
        return (
            day_start.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            day_end.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            tz_name,
            marker,
        )

    @staticmethod
    def _sum_optional_values(values: object) -> float | None:
        if not isinstance(values, list):
            return None
        total = 0.0
        found = False
        for item in values:
            if item is None:
                continue
            try:
                numeric = float(item)
            except Exception:
                continue
            if numeric != numeric or numeric in (float("inf"), float("-inf")):
                continue
            total += numeric
            found = True
        return total if found else None

    @staticmethod
    def _first_optional_numeric_value(values: object) -> float | None:
        if not isinstance(values, list):
            return None
        for item in values:
            if item is None:
                continue
            try:
                numeric = float(item)
            except Exception:
                continue
            if numeric != numeric or numeric in (float("inf"), float("-inf")):
                continue
            return numeric
        return None

    def _select_heatpump_energy_consumption_entry(
        self, payload: object
    ) -> tuple[dict[str, object], dict[str, object] | None, dict[str, object]] | None:
        if not isinstance(payload, dict):
            return None
        families = payload.get("data")
        if not isinstance(families, dict):
            return None
        family = families.get("heat-pump")
        if not isinstance(family, list) or not family:
            return None

        preferred_uid = self._heatpump_runtime_device_uid()
        selected: dict[str, object] | None = None
        for entry in family:
            if not isinstance(entry, dict):
                continue
            if preferred_uid and entry.get("device_uid") == preferred_uid:
                selected = entry
                break
            if preferred_uid is None and selected is None:
                selected = entry
        if not isinstance(selected, dict):
            return None

        member = self._heatpump_member_for_uid(selected.get("device_uid"))
        if member is None:
            member = self._heatpump_runtime_member()

        buckets = selected.get("consumption")
        first_bucket = None
        if isinstance(buckets, list):
            for bucket in buckets:
                if isinstance(bucket, dict):
                    first_bucket = bucket
                    break
        if not isinstance(first_bucket, dict):
            return None

        return selected, member, first_bucket

    @staticmethod
    def _heatpump_daily_split_available(snapshot: object) -> bool:
        if not isinstance(snapshot, dict):
            return False
        if snapshot.get("split_source") is not None:
            return True
        for key in (
            "split_device_uid",
            "split_device_name",
            "split_daily_energy_wh",
            "daily_solar_wh",
            "daily_battery_wh",
            "daily_grid_wh",
            "split_endpoint_type",
            "split_endpoint_timestamp",
        ):
            if snapshot.get(key) is not None:
                return True
        details = snapshot.get("details")
        return isinstance(details, list) and bool(details)

    @classmethod
    def _heatpump_clear_daily_split_fields(cls, snapshot: dict[str, object]) -> None:
        snapshot["split_device_uid"] = None
        snapshot["split_device_name"] = None
        snapshot["split_daily_energy_wh"] = None
        snapshot["daily_solar_wh"] = None
        snapshot["daily_battery_wh"] = None
        snapshot["daily_grid_wh"] = None
        snapshot["details"] = []
        snapshot["split_source"] = None
        snapshot["split_endpoint_type"] = None
        snapshot["split_endpoint_timestamp"] = None

    @classmethod
    def _heatpump_copy_daily_split_fields(
        cls, target: dict[str, object], source: object
    ) -> bool:
        if not cls._heatpump_daily_split_available(source):
            cls._heatpump_clear_daily_split_fields(target)
            return False
        assert isinstance(source, dict)
        for key in (
            "split_device_uid",
            "split_device_name",
            "split_daily_energy_wh",
            "daily_solar_wh",
            "daily_battery_wh",
            "daily_grid_wh",
            "split_source",
            "split_endpoint_type",
            "split_endpoint_timestamp",
        ):
            target[key] = source.get(key)
        target["details"] = (
            list(source.get("details"))
            if isinstance(source.get("details"), list)
            else []
        )
        return True

    def _build_heatpump_daily_consumption_snapshot(
        self,
        split_payload: object,
        site_today_payload: object,
    ) -> dict[str, object] | None:
        site_today_total_wh = self._site_today_heatpump_total_wh(site_today_payload)
        if site_today_total_wh is None:
            return None
        site_today_timestamp = (
            site_today_payload.get("timestamp")
            if isinstance(site_today_payload, dict)
            else None
        )
        snapshot: dict[str, object] = {
            "device_uid": None,
            "device_name": None,
            "split_device_uid": None,
            "split_device_name": None,
            "member_name": None,
            "member_device_type": None,
            "pairing_status": None,
            "device_state": None,
            "daily_energy_wh": site_today_total_wh,
            "split_daily_energy_wh": None,
            "daily_solar_wh": None,
            "daily_battery_wh": None,
            "daily_grid_wh": None,
            "details": [],
            "source": "site_today_heatpump",
            "split_source": None,
            "endpoint_type": (
                site_today_payload.get("type")
                if isinstance(site_today_payload, dict)
                else None
            ),
            "endpoint_timestamp": site_today_timestamp,
            "split_endpoint_type": None,
            "split_endpoint_timestamp": None,
            "sampled_at_utc": (
                parsed.isoformat()
                if (parsed := parse_inverter_last_report(site_today_timestamp))
                is not None
                else None
            ),
        }
        selection = self._select_heatpump_energy_consumption_entry(split_payload)
        if selection is None:
            return snapshot

        selected, member, first_bucket = selection
        daily_solar_wh = coerce_optional_float(first_bucket.get("solar"))
        daily_battery_wh = coerce_optional_float(first_bucket.get("battery"))
        daily_grid_wh = coerce_optional_float(first_bucket.get("grid"))
        split_daily_energy_wh = self._sum_optional_values(first_bucket.get("details"))
        if split_daily_energy_wh is None and all(
            value is not None
            for value in (daily_solar_wh, daily_battery_wh, daily_grid_wh)
        ):
            split_daily_energy_wh = daily_solar_wh + daily_battery_wh + daily_grid_wh

        snapshot.update(
            {
                "split_device_uid": selected.get("device_uid"),
                "split_device_name": selected.get("device_name"),
                "member_name": (
                    type_member_text(member, "name")
                    if isinstance(member, dict)
                    else None
                ),
                "member_device_type": (
                    heatpump_member_device_type(member)
                    if isinstance(member, dict)
                    else None
                ),
                "pairing_status": (
                    heatpump_pairing_status(member)
                    if isinstance(member, dict)
                    else None
                ),
                "device_state": (
                    heatpump_device_state(member) if isinstance(member, dict) else None
                ),
                "split_daily_energy_wh": split_daily_energy_wh,
                "daily_solar_wh": daily_solar_wh,
                "daily_battery_wh": daily_battery_wh,
                "daily_grid_wh": daily_grid_wh,
                "details": (
                    list(first_bucket.get("details"))
                    if isinstance(first_bucket.get("details"), list)
                    else []
                ),
                "split_source": (
                    f"hems_energy_consumption:{selected.get('device_uid')}"
                    if selected.get("device_uid")
                    else "hems_energy_consumption"
                ),
                "split_endpoint_type": (
                    split_payload.get("endpoint_type")
                    if isinstance(split_payload, dict)
                    and split_payload.get("endpoint_type") is not None
                    else (
                        split_payload.get("type")
                        if isinstance(split_payload, dict)
                        else None
                    )
                ),
                "split_endpoint_timestamp": (
                    split_payload.get("endpoint_timestamp")
                    if isinstance(split_payload, dict)
                    and split_payload.get("endpoint_timestamp") is not None
                    else (
                        split_payload.get("timestamp")
                        if isinstance(split_payload, dict)
                        else None
                    )
                ),
            }
        )
        return snapshot

    @classmethod
    def _site_today_heatpump_numeric_total(cls, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, dict):
            total = 0.0
            found = False
            for nested in value.values():
                nested_total = cls._site_today_heatpump_numeric_total(nested)
                if nested_total is None:
                    continue
                total += nested_total
                found = True
            return total if found else None
        if isinstance(value, list):
            total = 0.0
            found = False
            for nested in value:
                nested_total = cls._site_today_heatpump_numeric_total(nested)
                if nested_total is None:
                    continue
                total += nested_total
                found = True
            return total if found else None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    @classmethod
    def _site_today_heatpump_total_wh(cls, payload: object) -> float | None:
        if not isinstance(payload, dict):
            return None
        stats = payload.get("stats")
        if not isinstance(stats, list) or not stats:
            return None
        first_stat = next((item for item in stats if isinstance(item, dict)), None)
        if not isinstance(first_stat, dict):
            return None
        for key in ("heatpump", "heat_pump", "heat-pump"):
            total = cls._site_today_heatpump_numeric_total(first_stat.get(key))
            if total is not None:
                return total
        return None

    @staticmethod
    def _heatpump_runtime_mode(snapshot: object) -> str | None:
        if not isinstance(snapshot, dict):
            return None
        raw = coerce_optional_text(snapshot.get("heatpump_status"))
        if not raw:
            return None
        return raw.replace("-", "_").replace(" ", "_").upper()

    def _heatpump_power_history(self) -> list[dict[str, object]]:
        history = getattr(self, "_heatpump_power_sample_history", None)
        if isinstance(history, list):
            return history
        self._heatpump_power_sample_history = []
        return self._heatpump_power_sample_history

    @staticmethod
    def _heatpump_power_history_sample_time(entry: object) -> datetime | None:
        if not isinstance(entry, dict):
            return None
        value = entry.get("sample_utc")
        return value if isinstance(value, datetime) else None

    def _record_heatpump_power_history_sample(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            return
        energy_wh = coerce_optional_float(snapshot.get("split_daily_energy_wh"))
        sample_utc = parse_inverter_last_report(
            snapshot.get("split_endpoint_timestamp")
        )
        if energy_wh is None or sample_utc is None:
            return
        device_uid = coerce_optional_text(snapshot.get("split_device_uid"))
        day_key = coerce_optional_text(snapshot.get("day_key"))
        timezone_name = coerce_optional_text(snapshot.get("timezone"))
        cutoff = sample_utc - timedelta(seconds=_HEATPUMP_IDLE_SMOOTHING_MAX_WINDOW_S)
        history = self._heatpump_power_history()
        retained: list[dict[str, object]] = []
        for entry in history:
            entry_ts = self._heatpump_power_history_sample_time(entry)
            if entry_ts is None or entry_ts < cutoff:
                continue
            if (
                coerce_optional_text(entry.get("device_uid")) == device_uid
                and coerce_optional_text(entry.get("day_key")) == day_key
                and coerce_optional_text(entry.get("timezone")) == timezone_name
                and entry_ts == sample_utc
            ):
                continue
            retained.append(entry)
        retained.append(
            {
                "device_uid": device_uid,
                "day_key": day_key,
                "timezone": timezone_name,
                "energy_wh": energy_wh,
                "sample_utc": sample_utc,
            }
        )
        retained.sort(
            key=lambda entry: self._heatpump_power_history_sample_time(entry)
            or datetime.min.replace(tzinfo=_tz.utc)
        )
        history[:] = retained

    def _heatpump_idle_smoothed_power(
        self,
        snapshot: dict[str, object],
        *,
        current_energy_wh: float,
        current_sample_utc: datetime,
        raw_value_w: float | None,
        raw_validation: str,
    ) -> dict[str, object] | None:
        if raw_value_w is None or raw_value_w > 0:
            return None
        if raw_validation not in {
            "accepted_idle_zero",
            "accepted_idle_repeated_sample",
        }:
            return None
        device_uid = coerce_optional_text(snapshot.get("split_device_uid"))
        day_key = coerce_optional_text(snapshot.get("day_key"))
        timezone_name = coerce_optional_text(snapshot.get("timezone"))
        history = self._heatpump_power_history()
        candidates: list[tuple[datetime, float]] = []
        for entry in history:
            sample_utc = self._heatpump_power_history_sample_time(entry)
            if sample_utc is None:
                continue
            age_s = (current_sample_utc - sample_utc).total_seconds()
            if not (
                _HEATPUMP_IDLE_SMOOTHING_MIN_WINDOW_S
                <= age_s
                <= _HEATPUMP_IDLE_SMOOTHING_MAX_WINDOW_S
            ):
                continue
            if coerce_optional_text(entry.get("device_uid")) != device_uid:
                continue
            if coerce_optional_text(entry.get("day_key")) != day_key:
                continue
            if coerce_optional_text(entry.get("timezone")) != timezone_name:
                continue
            energy_wh = coerce_optional_float(entry.get("energy_wh"))
            if energy_wh is None or energy_wh > current_energy_wh:
                continue
            candidates.append((sample_utc, energy_wh))
        for start_utc, start_energy_wh in sorted(candidates, key=lambda item: item[0]):
            last_energy_wh = start_energy_wh
            monotonic = True
            for entry in history:
                sample_utc = self._heatpump_power_history_sample_time(entry)
                if sample_utc is None or not (
                    start_utc < sample_utc < current_sample_utc
                ):
                    continue
                if coerce_optional_text(entry.get("device_uid")) != device_uid:
                    continue
                if coerce_optional_text(entry.get("day_key")) != day_key:
                    continue
                if coerce_optional_text(entry.get("timezone")) != timezone_name:
                    continue
                energy_wh = coerce_optional_float(entry.get("energy_wh"))
                if energy_wh is None:
                    continue
                if energy_wh < last_energy_wh or energy_wh > current_energy_wh:
                    monotonic = False
                    break
                last_energy_wh = energy_wh
            if not monotonic:
                continue
            window_s = (current_sample_utc - start_utc).total_seconds()
            delta_wh = current_energy_wh - start_energy_wh
            if delta_wh <= _HEATPUMP_POWER_MIN_DELTA_WH:
                continue
            value_w = (delta_wh * 3600.0) / window_s
            if value_w > _HEATPUMP_IDLE_POWER_MAX_W:
                continue
            return {
                "accepted_value_w": value_w,
                "window_seconds": window_s,
                "series_start_utc": start_utc,
                "validation": "smoothed_idle_delta",
            }
        return None

    def _heatpump_power_summary_from_daily_snapshot(
        self,
        snapshot: object,
        *,
        runtime_snapshot: object = None,
    ) -> dict[str, object] | None:
        if not isinstance(snapshot, dict):
            return None
        if not self._heatpump_daily_split_available(snapshot):
            return None
        device_uid = coerce_optional_text(snapshot.get("split_device_uid"))
        day_key = coerce_optional_text(snapshot.get("day_key"))
        runtime_mode = self._heatpump_runtime_mode(runtime_snapshot)
        is_idle = runtime_mode in {"IDLE", "OFF", "STOPPED", "STANDBY"}
        current_energy_wh = coerce_optional_float(snapshot.get("split_daily_energy_wh"))
        current_sample_utc = parse_inverter_last_report(
            snapshot.get("split_endpoint_timestamp")
        )
        previous_snapshot = getattr(self, "_heatpump_daily_consumption_previous", None)
        previous_energy_wh = (
            coerce_optional_float(previous_snapshot.get("split_daily_energy_wh"))
            if isinstance(previous_snapshot, dict)
            else None
        )
        previous_device_uid = (
            coerce_optional_text(previous_snapshot.get("split_device_uid"))
            if isinstance(previous_snapshot, dict)
            else None
        )
        previous_day_key = (
            coerce_optional_text(previous_snapshot.get("day_key"))
            if isinstance(previous_snapshot, dict)
            else None
        )
        previous_sample_utc = (
            parse_inverter_last_report(
                previous_snapshot.get("split_endpoint_timestamp")
            )
            if isinstance(previous_snapshot, dict)
            else None
        )
        accepted_value = None
        rejected = False
        validation = "accepted_delta"
        window_s = None
        series_start_utc = None

        if current_energy_wh is None or current_sample_utc is None:
            if is_idle:
                accepted_value = 0.0
                validation = "accepted_idle_without_delta"
            else:
                rejected = True
                validation = "rejected_missing_energy_baseline"
        elif (
            previous_energy_wh is None
            or previous_sample_utc is None
            or previous_day_key != day_key
            or previous_device_uid != device_uid
        ):
            if is_idle:
                accepted_value = 0.0
                validation = "accepted_idle_seeded"
            else:
                rejected = True
                validation = "seeded_waiting_for_delta"
        else:
            window_s = (current_sample_utc - previous_sample_utc).total_seconds()
            series_start_utc = previous_sample_utc
            if window_s <= 0:
                if is_idle:
                    accepted_value = 0.0
                    validation = "accepted_idle_repeated_sample"
                else:
                    rejected = True
                    validation = "repeated_sample"
            else:
                delta_wh = current_energy_wh - previous_energy_wh
                if delta_wh < 0:
                    if is_idle:
                        accepted_value = 0.0
                        validation = "accepted_idle_reset"
                    else:
                        rejected = True
                        validation = "rejected_energy_reset"
                elif delta_wh <= _HEATPUMP_POWER_MIN_DELTA_WH:
                    accepted_value = 0.0
                    validation = (
                        "accepted_idle_zero" if is_idle else "accepted_zero_delta"
                    )
                else:
                    effective_window_s = (
                        window_s if window_s > 0 else _HEATPUMP_POWER_DEFAULT_WINDOW_S
                    )
                    candidate_value = (delta_wh * 3600.0) / effective_window_s
                    if is_idle and candidate_value > _HEATPUMP_IDLE_POWER_MAX_W:
                        accepted_value = 0.0
                        validation = "coerced_idle_high_to_zero"
                    else:
                        accepted_value = candidate_value
                        if is_idle:
                            validation = "accepted_idle_delta"

        raw_value = accepted_value
        raw_window_s = window_s
        raw_validation = validation
        display_window_s = window_s
        display_validation = validation
        smoothed = False
        if (
            is_idle
            and not rejected
            and current_energy_wh is not None
            and current_sample_utc is not None
        ):
            smoothed_summary = self._heatpump_idle_smoothed_power(
                snapshot,
                current_energy_wh=current_energy_wh,
                current_sample_utc=current_sample_utc,
                raw_value_w=raw_value,
                raw_validation=raw_validation,
            )
            if smoothed_summary is not None:
                accepted_value = coerce_optional_float(
                    smoothed_summary.get("accepted_value_w")
                )
                display_window_s = coerce_optional_float(
                    smoothed_summary.get("window_seconds")
                )
                smoothed_start = smoothed_summary.get("series_start_utc")
                if isinstance(smoothed_start, datetime):
                    series_start_utc = smoothed_start
                display_validation = str(smoothed_summary["validation"])
                validation = display_validation
                smoothed = True

        summary: dict[str, object] = {
            "requested_device_ref": (
                self._debug_truncate_identifier(device_uid) if device_uid else None
            ),
            "payload_device_ref": (
                self._debug_truncate_identifier(device_uid) if device_uid else None
            ),
            "resolved_device_ref": (
                self._debug_truncate_identifier(device_uid) if device_uid else None
            ),
            "member_device_type": snapshot.get("member_device_type"),
            "pairing_status": snapshot.get("pairing_status"),
            "device_state": snapshot.get("device_state"),
            "status": (
                heatpump_status_text(self._heatpump_member_for_uid(device_uid))
                if device_uid
                else None
            ),
            "recommended": self._heatpump_power_candidate_is_recommended(device_uid),
            "detail_count": 1 if current_energy_wh is not None else 0,
            "reported_detail_value": (
                round(current_energy_wh, 3) if current_energy_wh is not None else None
            ),
            "accepted_value_w": (
                round(accepted_value, 3) if accepted_value is not None else None
            ),
            "raw_value_w": round(raw_value, 3) if raw_value is not None else None,
            "previous_detail_value": (
                round(previous_energy_wh, 3) if previous_energy_wh is not None else None
            ),
            "window_seconds": round(window_s, 3) if window_s is not None else None,
            "raw_window_seconds": (
                round(raw_window_s, 3) if raw_window_s is not None else None
            ),
            "power_window_seconds": (
                round(display_window_s, 3) if display_window_s is not None else None
            ),
            "daily_energy_wh": snapshot.get("daily_energy_wh"),
            "split_daily_energy_wh": snapshot.get("split_daily_energy_wh"),
            "daily_solar_wh": snapshot.get("daily_solar_wh"),
            "daily_battery_wh": snapshot.get("daily_battery_wh"),
            "daily_grid_wh": snapshot.get("daily_grid_wh"),
            "runtime_mode": runtime_mode,
            "validation": validation,
            "raw_validation": raw_validation,
            "power_validation": display_validation,
            "smoothed": smoothed,
            "rejected": rejected,
            "series_start_utc": (
                series_start_utc.isoformat() if series_start_utc is not None else None
            ),
            "endpoint_timestamp": snapshot.get("split_endpoint_timestamp"),
            "endpoint_type": snapshot.get("split_endpoint_type"),
            "source": (
                f"hems_energy_consumption_delta:{self._debug_truncate_identifier(device_uid)}"
                if device_uid
                else "hems_energy_consumption_delta"
            ),
        }
        return summary

    async def _async_refresh_heatpump_daily_consumption(
        self, *, force: bool = False
    ) -> None:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            if self.heatpump_entities_established():
                self._heatpump_mark_daily_consumption_stale(
                    now=now,
                    error="Heat pump type temporarily missing from inventory",
                )
                self._heatpump_mark_daily_split_stale(
                    now=now,
                    error="Heat pump type temporarily missing from inventory",
                )
                self._heatpump_daily_consumption_cache_until = (
                    now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL
                )
                self._heatpump_daily_consumption_backoff_until = None
                return
            self._heatpump_daily_consumption = None
            self._heatpump_daily_consumption_cache_until = None
            self._heatpump_daily_consumption_backoff_until = None
            self._heatpump_daily_consumption_last_error = None
            self._heatpump_daily_consumption_cache_key = None
            self._heatpump_daily_consumption_last_success_mono = None
            self._heatpump_daily_consumption_last_success_utc = None
            self._heatpump_daily_consumption_using_stale = False
            self._heatpump_daily_split_last_error = None
            self._heatpump_daily_split_last_success_mono = None
            self._heatpump_daily_split_last_success_utc = None
            self._heatpump_daily_split_using_stale = False
            return

        window = self._heatpump_daily_window()
        if window is None:
            return
        start_at, end_at, tz_name, marker = window
        if (
            not force
            and self._heatpump_daily_consumption_cache_until is not None
            and self._heatpump_daily_consumption_cache_key == marker
            and now < self._heatpump_daily_consumption_cache_until
        ):
            return
        if (
            not force
            and self._heatpump_daily_consumption_backoff_until is not None
            and self._heatpump_daily_consumption_cache_key == marker
            and now < self._heatpump_daily_consumption_backoff_until
        ):
            return

        split_fetcher = getattr(self.client, "hems_energy_consumption", None)
        site_today_fetcher = getattr(self.client, "pv_system_today", None)
        if not callable(site_today_fetcher):
            return

        split_payload: object = None
        split_error: str | None = None
        try:
            site_today_payload = await site_today_fetcher()
        except Exception as err:  # noqa: BLE001
            error = (
                redact_text(
                    err,
                    site_ids=(self.site_id,),
                    identifiers=self._heatpump_power_redaction_identifiers(
                        self._heatpump_runtime_device_uid()
                    ),
                )
                or err.__class__.__name__
            )
            self._heatpump_mark_daily_consumption_stale(now=now, error=error)
            self._heatpump_daily_consumption_backoff_until = (
                now + HEATPUMP_DAILY_CONSUMPTION_FAILURE_BACKOFF_S
            )
            self._heatpump_daily_consumption_cache_until = None
            self._heatpump_daily_consumption_cache_key = marker
            return

        if callable(split_fetcher):
            try:
                split_payload = await split_fetcher(
                    start_at=start_at,
                    end_at=end_at,
                    timezone=tz_name,
                    step="P1D",
                )
            except Exception as err:  # noqa: BLE001
                split_error = (
                    redact_text(
                        err,
                        site_ids=(self.site_id,),
                        identifiers=self._heatpump_power_redaction_identifiers(
                            self._heatpump_runtime_device_uid()
                        ),
                    )
                    or err.__class__.__name__
                )
        else:
            split_error = "HEMS daily split endpoint unavailable"

        self._heatpump_daily_consumption_cache_until = (
            now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL
        )
        self._heatpump_daily_consumption_backoff_until = None
        self._heatpump_daily_consumption_cache_key = marker
        if not isinstance(site_today_payload, dict):
            self._heatpump_mark_daily_consumption_stale(
                now=now,
                error="No usable site today heat-pump payload",
            )
            return

        previous_snapshot = getattr(self, "_heatpump_daily_consumption", None)
        snapshot = self._build_heatpump_daily_consumption_snapshot(
            split_payload,
            site_today_payload,
        )
        if snapshot is None:
            self._heatpump_mark_daily_consumption_stale(
                now=now,
                error="No usable site today heat-pump payload",
            )
            return
        if split_error is None and not self._heatpump_daily_split_available(snapshot):
            split_error = "No usable HEMS daily split payload"
        if split_error is None:
            self._heatpump_daily_split_last_error = None
            self._heatpump_daily_split_using_stale = False
            self._heatpump_daily_split_last_success_mono = now
            self._heatpump_daily_split_last_success_utc = dt_util.utcnow()
        else:
            previous_snapshot = getattr(self, "_heatpump_daily_consumption", None)
            same_day = bool(
                isinstance(previous_snapshot, dict)
                and previous_snapshot.get("day_key") == marker[0]
                and previous_snapshot.get("timezone") == marker[1]
            )
            # Site-level totals remain useful when the optional split endpoint
            # fails.
            used_stale_split = same_day and self._heatpump_mark_daily_split_stale(
                now=now,
                error=split_error,
            )
            if used_stale_split:
                self._heatpump_copy_daily_split_fields(snapshot, previous_snapshot)
            else:
                self._heatpump_clear_daily_split_fields(snapshot)
                if not same_day:
                    self._heatpump_daily_split_using_stale = False
                    self._heatpump_daily_split_last_error = split_error
        snapshot["day_key"] = marker[0]
        snapshot["timezone"] = marker[1]
        self._heatpump_daily_consumption_previous = (
            dict(previous_snapshot) if isinstance(previous_snapshot, dict) else None
        )
        self._heatpump_daily_consumption = snapshot
        self._heatpump_mark_known_present()
        self._heatpump_daily_consumption_last_error = None
        self._heatpump_daily_consumption_using_stale = False
        self._heatpump_daily_consumption_last_success_mono = now
        self._heatpump_daily_consumption_last_success_utc = dt_util.utcnow()

    async def async_refresh_heatpump_daily_consumption(
        self, *, force: bool = False
    ) -> None:
        await self._async_refresh_heatpump_daily_consumption(force=force)

    def heatpump_daily_consumption_refresh_due(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            return self._heatpump_cleanup_due(
                "_heatpump_known_present",
                "_heatpump_daily_consumption",
                "_heatpump_daily_consumption_previous",
                "_heatpump_daily_consumption_cache_until",
                "_heatpump_daily_consumption_backoff_until",
                "_heatpump_daily_consumption_last_error",
                "_heatpump_daily_consumption_cache_key",
                "_heatpump_daily_consumption_last_success_mono",
                "_heatpump_daily_consumption_last_success_utc",
                "_heatpump_daily_consumption_using_stale",
                "_heatpump_daily_split_last_error",
                "_heatpump_daily_split_last_success_mono",
                "_heatpump_daily_split_last_success_utc",
                "_heatpump_daily_split_using_stale",
            )
        window = self._heatpump_daily_window()
        if window is None:
            return False
        _start_at, _end_at, _tz_name, marker = window
        if (
            not force
            and self._heatpump_daily_consumption_cache_until is not None
            and self._heatpump_daily_consumption_cache_key == marker
            and now < self._heatpump_daily_consumption_cache_until
        ):
            return False
        if (
            not force
            and self._heatpump_daily_consumption_backoff_until is not None
            and self._heatpump_daily_consumption_cache_key == marker
            and now < self._heatpump_daily_consumption_backoff_until
        ):
            return False
        fetcher = getattr(self.client, "pv_system_today", None)
        return callable(fetcher)

    def _heatpump_power_candidate_device_uids(self) -> list[str | None]:
        candidates: list[str | None] = []
        seen: set[str] = set()

        def _add(uid: str | None) -> None:
            if uid is None:
                return
            if uid in seen:
                return
            seen.add(uid)
            candidates.append(uid)

        _add(self._heatpump_primary_device_uid())
        for member in self._type_bucket_members("heatpump"):
            _add(type_member_text(member, "device_uid"))
        candidates.append(None)
        return candidates

    @staticmethod
    def _heatpump_latest_power_sample(payload: object) -> tuple[int, float] | None:
        if not isinstance(payload, dict):
            return None
        values = payload.get("heat_pump_consumption")
        if not isinstance(values, list):
            return None
        latest_index = len(values) - 1
        latest_completed_index: int | None = None
        start_utc = parse_inverter_last_report(payload.get("start_date"))
        now_utc = dt_util.utcnow()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=_tz.utc)
        interval_minutes = coerce_optional_float(payload.get("interval_minutes"))
        if interval_minutes is None:
            interval_minutes = HeatpumpRuntime._infer_heatpump_interval_minutes(
                start_utc,
                len(values),
                now_utc,
            )
        if (
            start_utc is not None
            and interval_minutes is not None
            and interval_minutes > 0
        ):
            elapsed_seconds = (now_utc - start_utc).total_seconds()
            if elapsed_seconds < 0:
                return None
            interval_seconds = float(interval_minutes * 60)
            current_index = int(elapsed_seconds // interval_seconds)
            latest_index = min(latest_index, current_index)
            latest_completed_index = min(latest_index, current_index - 1)

        def _sample_in_range(
            start_index: int,
            end_index: int,
        ) -> tuple[int, float] | None:
            for index in range(start_index, end_index - 1, -1):
                raw_value = values[index]
                if raw_value is None:
                    continue
                try:
                    value = float(raw_value)
                except Exception:
                    continue
                if value != value or value in (float("inf"), float("-inf")):
                    continue
                return index, value
            return None

        def _is_provisional_open_bucket(
            open_value: float,
            completed_value: float,
        ) -> bool:
            if open_value <= 0:
                return True
            if completed_value <= 0:
                return False
            return open_value <= 10.0 and open_value <= (completed_value * 0.1)

        if latest_completed_index is not None and latest_index > latest_completed_index:
            open_sample = _sample_in_range(latest_index, latest_completed_index + 1)
            completed_sample = _sample_in_range(latest_completed_index, 0)
            if open_sample is None:
                return completed_sample
            if completed_sample is None:
                return open_sample
            # The newest bucket is often provisional until Enphase closes the interval.
            if _is_provisional_open_bucket(open_sample[1], completed_sample[1]):
                return completed_sample
            return open_sample

        return _sample_in_range(latest_index, 0)

    @staticmethod
    def _infer_heatpump_interval_minutes(
        start_utc: datetime | None,
        bucket_count: int,
        now_utc: datetime,
    ) -> int | None:
        if start_utc is None or bucket_count <= 0:
            return None
        candidate_intervals = (5, 10, 15, 30, 60)
        viable: list[tuple[float, int]] = []
        fallback: list[tuple[float, int]] = []
        for interval_minutes in candidate_intervals:
            try:
                end_utc = start_utc + timedelta(minutes=interval_minutes * bucket_count)
            except Exception:
                continue
            future_window_s = (end_utc - now_utc).total_seconds()
            if future_window_s >= 0:
                viable.append((future_window_s, interval_minutes))
            fallback.append((abs(future_window_s), interval_minutes))
        if viable:
            return min(viable)[1]
        if fallback:
            return min(fallback)[1]
        return None

    @classmethod
    def _heatpump_sample_utc_for_index(
        cls, payload: object, sample_index: int
    ) -> datetime | None:
        if not isinstance(sample_index, int) or sample_index < 0:
            return None
        if not isinstance(payload, dict):
            return None
        start_utc = parse_inverter_last_report(payload.get("start_date"))
        if start_utc is None:
            return None
        values = payload.get("heat_pump_consumption")
        bucket_count = len(values) if isinstance(values, list) else 0
        now_utc = dt_util.utcnow()
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=_tz.utc)
        interval_minutes = coerce_optional_float(payload.get("interval_minutes"))
        if interval_minutes is None:
            interval_minutes = cls._infer_heatpump_interval_minutes(
                start_utc,
                bucket_count,
                now_utc,
            )
        if interval_minutes is None or interval_minutes <= 0:
            return None
        try:
            return start_utc + timedelta(minutes=interval_minutes * sample_index)
        except Exception:
            return None

    def _heatpump_member_for_uid(self, uid: object) -> dict[str, object] | None:
        uid_text = coerce_optional_text(uid)
        if not uid_text:
            return None
        for member in self._type_bucket_members("heatpump"):
            for key in ("device_uid", "uid", "serial_number", "serial"):
                member_uid = type_member_text(member, key)
                if member_uid == uid_text:
                    return member
        return None

    @classmethod
    def _heatpump_member_aliases(cls, member: dict[str, object] | None) -> list[str]:
        if not isinstance(member, dict):
            return []
        aliases: list[str] = []
        seen: set[str] = set()
        for key in (
            "device_uid",
            "uid",
            "serial_number",
            "serial",
            "hems_device_id",
            "hems_device_facet_id",
            "device_id",
            "id",
        ):
            alias = type_member_text(member, key)
            if not alias or alias in seen:
                continue
            seen.add(alias)
            aliases.append(alias)
        return aliases

    @classmethod
    def _heatpump_member_primary_id(
        cls, member: dict[str, object] | None
    ) -> str | None:
        aliases = cls._heatpump_member_aliases(member)
        return aliases[0] if aliases else None

    @classmethod
    def _heatpump_member_parent_id(cls, member: dict[str, object] | None) -> str | None:
        if not isinstance(member, dict):
            return None
        return type_member_text(
            member,
            "parent_uid",
            "parentUid",
            "parent_device_uid",
            "parentDeviceUid",
            "parent_id",
            "parentId",
            "parent",
        )

    def _heatpump_member_alias_map(self) -> dict[str, str]:
        alias_map: dict[str, str] = {}
        for member in self._type_bucket_members("heatpump"):
            primary = self._heatpump_member_primary_id(member)
            if not primary:
                continue
            for alias in self._heatpump_member_aliases(member):
                alias_map[alias] = primary
        return alias_map

    def _heatpump_power_inventory_marker(self) -> tuple[tuple[str, str, str, str], ...]:
        alias_map = self._heatpump_member_alias_map()
        marker_rows: list[tuple[str, str, str, str]] = []
        for index, member in enumerate(self._type_bucket_members("heatpump")):
            primary_id = self._heatpump_member_primary_id(member)
            if not primary_id:
                primary_id = f"idx:{index}"
            parent_id = self._heatpump_member_parent_id(member)
            if parent_id:
                parent_id = alias_map.get(parent_id, parent_id)
            status_text = heatpump_status_text(member)
            marker_rows.append(
                (
                    primary_id,
                    parent_id or "",
                    heatpump_member_device_type(member) or "",
                    status_text.casefold() if isinstance(status_text, str) else "",
                )
            )
        marker_rows.sort()
        return tuple(marker_rows)

    def _heatpump_power_fetch_plan(
        self,
    ) -> tuple[list[str | None], bool, tuple[tuple[str, str, str, str], ...]]:
        marker = self._heatpump_power_inventory_marker()
        candidates = self._heatpump_power_candidate_device_uids()
        compare_all = (
            self._heatpump_power_selection_marker != marker
            or self._heatpump_power_device_uid not in candidates
        )

        ordered: list[str | None] = []
        seen: set[str | None] = set()

        def _add(uid: str | None) -> None:
            if uid in seen:
                return
            seen.add(uid)
            ordered.append(uid)

        if compare_all:
            for candidate in candidates:
                _add(candidate)
            return ordered, True, marker

        _add(self._heatpump_power_device_uid)
        for candidate in candidates:
            _add(candidate)
        return ordered, False, marker

    def _heatpump_power_candidate_is_recommended(self, uid: str | None) -> bool:
        members = self._type_bucket_members("heatpump")
        if not members:
            return False
        alias_map = self._heatpump_member_alias_map()
        candidate = self._heatpump_member_for_uid(uid) if uid else None
        candidate_primary = alias_map.get(uid, uid) if uid else None
        candidate_parent = self._heatpump_member_parent_id(candidate)
        if candidate_parent:
            candidate_parent = alias_map.get(candidate_parent, candidate_parent)

        recommended_members = [
            member
            for member in members
            if isinstance(heatpump_status_text(member), str)
            and heatpump_status_text(member).casefold() == "recommended"
        ]
        if not recommended_members:
            return False

        any_parent_link = False
        for member in recommended_members:
            member_primary = self._heatpump_member_primary_id(member)
            if member_primary:
                member_primary = alias_map.get(member_primary, member_primary)
            member_parent = self._heatpump_member_parent_id(member)
            if member_parent:
                member_parent = alias_map.get(member_parent, member_parent)
            if member_parent or candidate_parent:
                any_parent_link = True
            if candidate_primary and member_primary == candidate_primary:
                return True
            if candidate_primary and member_parent == candidate_primary:
                return True
            if candidate_parent and member_primary == candidate_parent:
                return True
            if candidate_parent and member_parent == candidate_parent:
                return True

        if not any_parent_link:
            return True
        return False

    def _heatpump_power_candidate_type_rank(
        self,
        payload: dict[str, object],
        requested_uid: str | None,
        *,
        is_recommended: bool,
    ) -> int:
        if not is_recommended:
            return 0
        member = self._heatpump_member_for_uid(
            type_member_text(payload, "device_uid", "uid") or requested_uid
        )
        device_type = heatpump_member_device_type(member)
        if device_type == "ENERGY_METER":
            return 3
        if device_type == "HEAT_PUMP":
            return 2
        if device_type == "SG_READY_GATEWAY":
            return 1
        return 0

    def _heatpump_power_selection_key(
        self,
        payload: dict[str, object],
        *,
        requested_uid: str | None,
        sample: tuple[int, float] | None,
    ) -> tuple[int, int, int, int, float, int, int]:
        payload_uid = type_member_text(payload, "device_uid", "uid")
        resolved_uid = payload_uid or requested_uid
        has_positive_sample = 1 if sample is not None and float(sample[1]) > 0 else 0
        is_recommended = (
            1 if self._heatpump_power_candidate_is_recommended(resolved_uid) else 0
        )
        type_rank = self._heatpump_power_candidate_type_rank(
            payload,
            requested_uid,
            is_recommended=bool(is_recommended),
        )
        sample_value = sample[1] if sample is not None else float("-inf")
        sample_index = sample[0] if sample is not None else -1
        return (
            1 if sample is not None else 0,
            has_positive_sample,
            is_recommended,
            type_rank,
            sample_value,
            1 if resolved_uid else 0,
            sample_index,
        )

    def _heatpump_power_debug_candidate_summary(
        self, uid: str | None
    ) -> dict[str, object]:
        member = self._heatpump_member_for_uid(uid) if uid else None
        return {
            "requested_device_ref": (
                self._debug_truncate_identifier(uid) if uid else None
            ),
            "member_device_ref": self._debug_truncate_identifier(
                self._heatpump_member_primary_id(member)
            ),
            "member_parent_ref": self._debug_truncate_identifier(
                self._heatpump_member_parent_id(member)
            ),
            "member_device_type": heatpump_member_device_type(member),
            "pairing_status": heatpump_pairing_status(member),
            "device_state": heatpump_device_state(member),
            "status": heatpump_status_text(member),
            "recommended": self._heatpump_power_candidate_is_recommended(uid),
        }

    def _heatpump_power_debug_payload_summary(
        self,
        payload: dict[str, object],
        *,
        requested_uid: str | None,
        sample: tuple[int, float] | None,
        selection_key: tuple[int, int, int, int, float, int, int] | None,
    ) -> dict[str, object]:
        payload_uid = type_member_text(payload, "device_uid", "uid")
        resolved_uid = payload_uid or requested_uid
        member = self._heatpump_member_for_uid(resolved_uid) if resolved_uid else None
        values = payload.get("heat_pump_consumption")
        bucket_count = 0
        non_null_bucket_count = 0
        sample_tail: list[dict[str, object]] = []
        if isinstance(values, list):
            bucket_count = len(values)
            for index in range(len(values) - 1, -1, -1):
                numeric = coerce_optional_float(values[index])
                if numeric is None:
                    continue
                non_null_bucket_count += 1
                if len(sample_tail) < 3:
                    sample_tail.append(
                        {
                            "index": index,
                            "value_w": round(numeric, 3),
                        }
                    )
        interval_minutes = coerce_optional_float(payload.get("interval_minutes"))
        return {
            "requested_device_ref": (
                self._debug_truncate_identifier(requested_uid)
                if requested_uid
                else None
            ),
            "payload_device_ref": (
                self._debug_truncate_identifier(payload_uid) if payload_uid else None
            ),
            "resolved_device_ref": (
                self._debug_truncate_identifier(resolved_uid) if resolved_uid else None
            ),
            "member_device_type": heatpump_member_device_type(member),
            "pairing_status": heatpump_pairing_status(member),
            "device_state": heatpump_device_state(member),
            "status": heatpump_status_text(member),
            "recommended": self._heatpump_power_candidate_is_recommended(resolved_uid),
            "bucket_count": bucket_count,
            "non_null_bucket_count": non_null_bucket_count,
            "sample_tail": sample_tail,
            "latest_sample_index": sample[0] if sample is not None else None,
            "latest_sample_w": (
                round(float(sample[1]), 3) if sample is not None else None
            ),
            "start_date": coerce_optional_text(payload.get("start_date")),
            "interval_minutes": (
                round(interval_minutes, 3) if interval_minutes is not None else None
            ),
            "selection_key": list(selection_key) if selection_key is not None else None,
        }

    async def _async_refresh_heatpump_power(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            if self.heatpump_entities_established():
                self._heatpump_mark_power_stale(
                    now=now,
                    error="Heat pump type temporarily missing from inventory",
                )
                self._heatpump_power_cache_until = (
                    now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL
                )
                self._heatpump_power_backoff_until = None
                return
            self._heatpump_power_w = None
            self._heatpump_power_sample_utc = None
            self._heatpump_power_start_utc = None
            self._heatpump_power_device_uid = None
            self._heatpump_power_source = None
            self._heatpump_power_snapshot = None
            self._heatpump_power_cache_until = None
            self._heatpump_power_backoff_until = None
            self._heatpump_power_last_error = None
            self._heatpump_power_last_success_mono = None
            self._heatpump_power_last_success_utc = None
            self._heatpump_power_raw_w = None
            self._heatpump_power_window_seconds = None
            self._heatpump_power_validation = None
            self._heatpump_power_smoothed = False
            self._heatpump_power_sample_history = []
            self._heatpump_power_using_stale = False
            self._heatpump_power_selection_marker = None
            return
        if not force and self._heatpump_power_cache_until is not None:
            if now < self._heatpump_power_cache_until:
                return
        if not force and self._heatpump_power_backoff_until is not None:
            if now < self._heatpump_power_backoff_until:
                return

        await self._async_refresh_hems_support_preflight(force=force)
        if getattr(self.client, "hems_site_supported", None) is False:
            error = "HEMS power endpoint unavailable for this site"
            self._heatpump_mark_power_stale(
                now=now,
                error=error,
            )
            self._heatpump_power_snapshot = {
                "site_date": self._site_local_current_date(),
                "force": force,
                "outcome": "unsupported_site",
                "using_stale": bool(
                    getattr(self, "_heatpump_power_using_stale", False)
                ),
                "last_success_utc": (
                    self._heatpump_power_last_success_utc.isoformat()
                    if isinstance(self._heatpump_power_last_success_utc, datetime)
                    else None
                ),
                "last_error": error,
            }
            self._heatpump_power_cache_until = (
                now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL
            )
            self._heatpump_power_backoff_until = None
            self._heatpump_power_selection_marker = None
            return

        site_date = self._site_local_current_date()
        marker = self._heatpump_power_inventory_marker()
        previous_device_uid = self._heatpump_power_device_uid
        power_snapshot: dict[str, object] = {
            "site_date": site_date,
            "force": force,
            "compare_all": False,
            "previous_device_ref": self._debug_truncate_identifier(previous_device_uid),
            "candidates": [
                self._heatpump_power_debug_candidate_summary(
                    self._heatpump_runtime_device_uid()
                )
            ],
            "attempts": [],
            "selected_payload": None,
            "selected_source": None,
            "selected_sample_at_utc": None,
            "last_error": None,
            "outcome": "pending",
            "using_stale": False,
            "last_success_utc": (
                self._heatpump_power_last_success_utc.isoformat()
                if isinstance(self._heatpump_power_last_success_utc, datetime)
                else None
            ),
        }
        self._heatpump_power_snapshot = power_snapshot
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Heat pump power fetch plan for site %s: site_date=%s force=%s previous_device_uid=%s candidates=%s",
                redact_site_id(self.site_id),
                site_date,
                force,
                self._debug_truncate_identifier(previous_device_uid) or "[redacted]",
                power_snapshot["candidates"],
            )

        try:
            await self._async_refresh_heatpump_runtime_state(force=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Heat pump power runtime-state refresh failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )

        try:
            await self._async_refresh_heatpump_daily_consumption(force=force)
        except Exception as err:  # noqa: BLE001
            error = (
                redact_text(
                    err,
                    site_ids=(self.site_id,),
                    identifiers=self._heatpump_power_redaction_identifiers(),
                )
                or err.__class__.__name__
            )
            power_snapshot["last_error"] = error
            power_snapshot["outcome"] = "fetch_error"
            attempts = power_snapshot.setdefault("attempts", [])
            if isinstance(attempts, list):
                attempts.append({"source": "hems_energy_consumption", "error": error})
            self._heatpump_mark_power_stale(
                now=now,
                error=error,
                power_snapshot=power_snapshot,
            )
            self._heatpump_power_backoff_until = (
                now + HEATPUMP_DAILY_CONSUMPTION_FAILURE_BACKOFF_S
            )
            self._heatpump_power_cache_until = None
            _LOGGER.debug(
                "Heat pump power daily-consumption refresh failed for site %s: %s",
                redact_site_id(self.site_id),
                error,
            )
            return

        daily_last_error = coerce_optional_text(
            getattr(self, "_heatpump_daily_split_last_error", None)
        )
        power_summary = self._heatpump_power_summary_from_daily_snapshot(
            getattr(self, "_heatpump_daily_consumption", None),
            runtime_snapshot=getattr(self, "_heatpump_runtime_state", None),
        )
        if power_summary is not None:
            attempts = power_snapshot.setdefault("attempts", [])
            if isinstance(attempts, list):
                attempts.append(dict(power_summary))
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Heat pump power candidate payload for site %s: %s",
                    redact_site_id(self.site_id),
                    power_summary,
                )

        if power_summary is None:
            error = daily_last_error or "No usable HEMS daily-consumption payload"
            power_snapshot["last_error"] = error
            power_snapshot["outcome"] = "no_usable_payload"
            attempts = power_snapshot.setdefault("attempts", [])
            if isinstance(attempts, list) and error and not attempts:
                attempts.append({"source": "hems_energy_consumption", "error": error})
            self._heatpump_mark_power_stale(
                now=now,
                error=error,
                power_snapshot=power_snapshot,
            )
            self._heatpump_power_backoff_until = getattr(
                self, "_heatpump_daily_consumption_backoff_until", None
            )
            self._heatpump_power_cache_until = getattr(
                self, "_heatpump_daily_consumption_cache_until", None
            ) or (now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL)
            _LOGGER.debug(
                "Heat pump power daily-consumption payload unavailable for site %s: %s",
                redact_site_id(self.site_id),
                error,
            )
            return

        if bool(power_summary.get("rejected")):
            error = daily_last_error or str(
                power_summary.get("validation") or "rejected_bad_power_value"
            )
            power_snapshot["last_error"] = error
            power_snapshot["outcome"] = "rejected_value"
            self._heatpump_mark_power_stale(
                now=now,
                error=error,
                power_snapshot=power_snapshot,
            )
            self._heatpump_power_backoff_until = getattr(
                self, "_heatpump_daily_consumption_backoff_until", None
            )
            self._heatpump_power_cache_until = getattr(
                self, "_heatpump_daily_consumption_cache_until", None
            ) or (now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL)
            _LOGGER.debug(
                "Heat pump power rejected daily-consumption payload for site %s: %s",
                redact_site_id(self.site_id),
                power_summary,
            )
            return

        snapshot = getattr(self, "_heatpump_daily_consumption", None)
        device_uid = (
            coerce_optional_text(snapshot.get("split_device_uid"))
            if isinstance(snapshot, dict)
            else None
        )
        endpoint_timestamp = (
            parse_inverter_last_report(snapshot.get("split_endpoint_timestamp"))
            if isinstance(snapshot, dict)
            else None
        )
        self._heatpump_power_w = float(power_summary.get("accepted_value_w") or 0.0)
        self._heatpump_power_sample_utc = endpoint_timestamp
        self._heatpump_power_start_utc = parse_inverter_last_report(
            power_summary.get("series_start_utc")
        )
        self._heatpump_power_device_uid = device_uid
        self._heatpump_power_source = (
            f"hems_energy_consumption_delta:{device_uid}"
            if device_uid
            else "hems_energy_consumption_delta"
        )
        self._heatpump_power_raw_w = coerce_optional_float(
            power_summary.get("raw_value_w")
        )
        self._heatpump_power_window_seconds = coerce_optional_float(
            power_summary.get("power_window_seconds")
        )
        self._heatpump_power_validation = coerce_optional_text(
            power_summary.get("power_validation")
        )
        self._heatpump_power_smoothed = bool(power_summary.get("smoothed"))
        self._heatpump_power_cache_until = getattr(
            self, "_heatpump_daily_consumption_cache_until", None
        ) or (now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL)
        self._heatpump_power_backoff_until = None
        self._heatpump_power_last_error = daily_last_error
        self._heatpump_power_using_stale = bool(
            getattr(self, "_heatpump_daily_split_using_stale", False)
        )
        self._heatpump_power_last_success_mono = getattr(
            self, "_heatpump_daily_split_last_success_mono", now
        )
        self._heatpump_power_last_success_utc = getattr(
            self, "_heatpump_daily_split_last_success_utc", dt_util.utcnow()
        )
        self._heatpump_mark_known_present()
        self._heatpump_power_selection_marker = marker if device_uid else None
        self._record_heatpump_power_history_sample(snapshot)
        power_snapshot["selected_payload"] = dict(power_summary)
        power_snapshot["selected_source"] = (
            f"hems_energy_consumption_delta:{self._debug_truncate_identifier(device_uid)}"
            if device_uid
            else "hems_energy_consumption_delta"
        )
        power_snapshot["selected_sample_at_utc"] = (
            endpoint_timestamp.isoformat() if endpoint_timestamp is not None else None
        )
        power_snapshot["outcome"] = "selected_sample"
        power_snapshot["using_stale"] = self._heatpump_power_using_stale
        power_snapshot["last_success_utc"] = (
            self._heatpump_power_last_success_utc.isoformat()
            if isinstance(self._heatpump_power_last_success_utc, datetime)
            else None
        )
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Heat pump power selected payload for site %s: %s",
                redact_site_id(self.site_id),
                power_summary,
            )
        return

    async def async_refresh_heatpump_power(self, *, force: bool = False) -> None:
        await self._async_refresh_heatpump_power(force=force)

    def heatpump_power_refresh_due(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            return self._heatpump_cleanup_due(
                "_heatpump_known_present",
                "_heatpump_power_w",
                "_heatpump_power_sample_utc",
                "_heatpump_power_start_utc",
                "_heatpump_power_device_uid",
                "_heatpump_power_source",
                "_heatpump_power_raw_w",
                "_heatpump_power_window_seconds",
                "_heatpump_power_validation",
                "_heatpump_power_smoothed",
                "_heatpump_power_sample_history",
                "_heatpump_power_snapshot",
                "_heatpump_power_cache_until",
                "_heatpump_power_backoff_until",
                "_heatpump_power_last_error",
                "_heatpump_power_last_success_mono",
                "_heatpump_power_last_success_utc",
                "_heatpump_power_using_stale",
                "_heatpump_power_selection_marker",
            )
        if not force and self._heatpump_power_cache_until is not None:
            if now < self._heatpump_power_cache_until:
                return False
        if not force and self._heatpump_power_backoff_until is not None:
            if now < self._heatpump_power_backoff_until:
                return False
        fetcher = getattr(self.client, "pv_system_today", None)
        return callable(fetcher)

    @staticmethod
    def _hems_event_entries(payload: object) -> list[dict[str, object]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("events", "data", "result", "items"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return []

    def _heatpump_event_summary(self) -> dict[str, object]:
        known_event_labels = {
            "hems_sgready_mode_changed_to_2": "sg_ready_normal",
            "hems_sgready_mode_changed_to_3": "sg_ready_recommended",
            "hems_sgready_relay_offline": "sg_ready_relay_offline",
            "hems_energy_meter_offline": "energy_meter_offline",
            "hems_iqer_MQTT_offline": "iq_energy_router_mqtt_offline",
        }
        event_counts: dict[str, int] = {}
        unknown_event_keys: set[str] = set()
        for payload_entry in list(getattr(self, "_heatpump_events_payloads", []) or []):
            payload = (
                payload_entry.get("payload")
                if isinstance(payload_entry, dict)
                else None
            )
            for event in self._hems_event_entries(payload):
                event_key = coerce_optional_text(
                    event.get("event_key")
                    if event.get("event_key") is not None
                    else event.get("eventKey")
                )
                if not event_key:
                    continue
                label = known_event_labels.get(event_key)
                if label is None:
                    unknown_event_keys.add(event_key)
                    continue
                event_counts[label] = event_counts.get(label, 0) + 1
        return {
            "known_event_counts": event_counts,
            "unknown_event_keys": sorted(unknown_event_keys),
        }

    def heatpump_runtime_diagnostics(self) -> dict[str, object]:
        return {
            "runtime_state": self._copy_diagnostics_value(
                getattr(self, "_heatpump_runtime_state", None)
            ),
            "runtime_state_using_stale": bool(
                getattr(self, "_heatpump_runtime_state_using_stale", False)
            ),
            "runtime_state_last_success_utc": (
                getattr(
                    self, "_heatpump_runtime_state_last_success_utc", None
                ).isoformat()
                if isinstance(
                    getattr(self, "_heatpump_runtime_state_last_success_utc", None),
                    datetime,
                )
                else None
            ),
            "runtime_state_last_error": getattr(
                self, "_heatpump_runtime_state_last_error", None
            ),
            "heatpump_known_present": bool(
                getattr(self, "_heatpump_known_present", False)
            ),
            "daily_consumption": self._copy_diagnostics_value(
                getattr(self, "_heatpump_daily_consumption", None)
            ),
            "daily_consumption_using_stale": bool(
                getattr(self, "_heatpump_daily_consumption_using_stale", False)
            ),
            "daily_consumption_last_success_utc": (
                getattr(
                    self, "_heatpump_daily_consumption_last_success_utc", None
                ).isoformat()
                if isinstance(
                    getattr(self, "_heatpump_daily_consumption_last_success_utc", None),
                    datetime,
                )
                else None
            ),
            "daily_consumption_last_error": getattr(
                self, "_heatpump_daily_consumption_last_error", None
            ),
            "daily_split_using_stale": bool(
                getattr(self, "_heatpump_daily_split_using_stale", False)
            ),
            "daily_split_last_success_utc": (
                getattr(
                    self, "_heatpump_daily_split_last_success_utc", None
                ).isoformat()
                if isinstance(
                    getattr(self, "_heatpump_daily_split_last_success_utc", None),
                    datetime,
                )
                else None
            ),
            "daily_split_last_error": getattr(
                self, "_heatpump_daily_split_last_error", None
            ),
            "show_livestream_payload": self._copy_diagnostics_value(
                getattr(self, "_show_livestream_payload", None)
            ),
            "events_payloads": self._copy_diagnostics_value(
                list(getattr(self, "_heatpump_events_payloads", []) or [])
            ),
            "power_snapshot": self._copy_diagnostics_value(
                getattr(self, "_heatpump_power_snapshot", None)
            ),
            "power_using_stale": bool(
                getattr(self, "_heatpump_power_using_stale", False)
            ),
            "power_last_success_utc": (
                getattr(self, "_heatpump_power_last_success_utc", None).isoformat()
                if isinstance(
                    getattr(self, "_heatpump_power_last_success_utc", None), datetime
                )
                else None
            ),
            "event_summary": self._heatpump_event_summary(),
            "last_error": getattr(self, "_heatpump_runtime_diagnostics_error", None),
        }

    @property
    def heatpump_runtime_state(self) -> dict[str, object]:
        value = getattr(self, "_heatpump_runtime_state", None)
        if isinstance(value, dict):
            return dict(value)
        return {}

    @property
    def heatpump_runtime_state_last_error(self) -> str | None:
        value = getattr(self, "_heatpump_runtime_state_last_error", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_runtime_state_using_stale(self) -> bool:
        return bool(getattr(self, "_heatpump_runtime_state_using_stale", False))

    @property
    def heatpump_runtime_state_last_success_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_runtime_state_last_success_utc", None)
        return value if isinstance(value, datetime) else None

    @property
    def heatpump_daily_consumption(self) -> dict[str, object]:
        value = getattr(self, "_heatpump_daily_consumption", None)
        if isinstance(value, dict):
            return dict(value)
        return {}

    @property
    def heatpump_daily_consumption_last_error(self) -> str | None:
        value = getattr(self, "_heatpump_daily_consumption_last_error", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_daily_consumption_using_stale(self) -> bool:
        return bool(getattr(self, "_heatpump_daily_consumption_using_stale", False))

    @property
    def heatpump_daily_consumption_last_success_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_daily_consumption_last_success_utc", None)
        return value if isinstance(value, datetime) else None

    @property
    def heatpump_daily_split_last_error(self) -> str | None:
        value = getattr(self, "_heatpump_daily_split_last_error", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_daily_split_using_stale(self) -> bool:
        return bool(getattr(self, "_heatpump_daily_split_using_stale", False))

    @property
    def heatpump_daily_split_last_success_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_daily_split_last_success_utc", None)
        return value if isinstance(value, datetime) else None

    @property
    def heatpump_power_w(self) -> float | None:
        value = getattr(self, "_heatpump_power_w", None)
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    @property
    def heatpump_power_sample_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_power_sample_utc", None)
        if isinstance(value, datetime):
            return value
        return None

    @property
    def heatpump_power_start_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_power_start_utc", None)
        if isinstance(value, datetime):
            return value
        return None

    @property
    def heatpump_power_device_uid(self) -> str | None:
        value = getattr(self, "_heatpump_power_device_uid", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_power_source(self) -> str | None:
        value = getattr(self, "_heatpump_power_source", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_power_raw_w(self) -> float | None:
        value = getattr(self, "_heatpump_power_raw_w", None)
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    @property
    def heatpump_power_window_seconds(self) -> float | None:
        value = getattr(self, "_heatpump_power_window_seconds", None)
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    @property
    def heatpump_power_validation(self) -> str | None:
        value = getattr(self, "_heatpump_power_validation", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_power_smoothed(self) -> bool:
        return bool(getattr(self, "_heatpump_power_smoothed", False))

    @property
    def heatpump_power_using_stale(self) -> bool:
        return bool(getattr(self, "_heatpump_power_using_stale", False))

    @property
    def heatpump_power_last_success_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_power_last_success_utc", None)
        return value if isinstance(value, datetime) else None

    @property
    def heatpump_power_last_error(self) -> str | None:
        value = getattr(self, "_heatpump_power_last_error", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None


install_state_descriptors(HeatpumpRuntime)
