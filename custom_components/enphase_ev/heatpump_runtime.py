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
    HEATPUMP_POWER_CACHE_TTL,
    HEATPUMP_POWER_FAILURE_BACKOFF_S,
    HEATPUMP_RUNTIME_DIAGNOSTICS_CACHE_TTL,
    HEATPUMP_RUNTIME_STATE_CACHE_TTL,
    HEATPUMP_RUNTIME_STATE_FAILURE_BACKOFF_S,
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
                return uid
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
            self._heatpump_runtime_state = None
            self._heatpump_runtime_state_cache_until = None
            self._heatpump_runtime_state_backoff_until = None
            self._heatpump_runtime_state_last_error = None
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
            self._heatpump_runtime_state = None
            self._heatpump_runtime_state_cache_until = (
                now + HEATPUMP_RUNTIME_STATE_CACHE_TTL
            )
            self._heatpump_runtime_state_backoff_until = None
            self._heatpump_runtime_state_last_error = None
            return

        device_uid = self._heatpump_runtime_device_uid()
        if not device_uid:
            self._heatpump_runtime_state = None
            self._heatpump_runtime_state_cache_until = (
                now + HEATPUMP_RUNTIME_STATE_CACHE_TTL
            )
            self._heatpump_runtime_state_backoff_until = None
            self._heatpump_runtime_state_last_error = None
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
            self._heatpump_runtime_state_last_error = (
                redact_text(err, site_ids=(self.site_id,)) or err.__class__.__name__
            )
            self._heatpump_runtime_state_backoff_until = (
                now + HEATPUMP_RUNTIME_STATE_FAILURE_BACKOFF_S
            )
            self._heatpump_runtime_state_cache_until = None
            return

        self._heatpump_runtime_state_cache_until = (
            now + HEATPUMP_RUNTIME_STATE_CACHE_TTL
        )
        self._heatpump_runtime_state_backoff_until = None
        self._heatpump_runtime_state_last_error = None
        if not isinstance(payload, dict):
            self._heatpump_runtime_state = None
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

    async def async_refresh_heatpump_runtime_state(
        self, *, force: bool = False
    ) -> None:
        await self._async_refresh_heatpump_runtime_state(force=force)

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

    def _build_heatpump_daily_consumption_snapshot(
        self, payload: object
    ) -> dict[str, object] | None:
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

        daily_solar_wh = coerce_optional_float(first_bucket.get("solar"))
        daily_battery_wh = coerce_optional_float(first_bucket.get("battery"))
        daily_grid_wh = coerce_optional_float(first_bucket.get("grid"))
        daily_energy_wh = self._sum_optional_values(first_bucket.get("details"))
        if daily_energy_wh is None and all(
            value is not None
            for value in (daily_solar_wh, daily_battery_wh, daily_grid_wh)
        ):
            daily_energy_wh = daily_solar_wh + daily_battery_wh + daily_grid_wh

        return {
            "device_uid": selected.get("device_uid"),
            "device_name": selected.get("device_name"),
            "member_name": (
                type_member_text(member, "name") if isinstance(member, dict) else None
            ),
            "member_device_type": (
                heatpump_member_device_type(member)
                if isinstance(member, dict)
                else None
            ),
            "pairing_status": (
                heatpump_pairing_status(member) if isinstance(member, dict) else None
            ),
            "device_state": (
                heatpump_device_state(member) if isinstance(member, dict) else None
            ),
            "daily_energy_wh": daily_energy_wh,
            "daily_solar_wh": daily_solar_wh,
            "daily_battery_wh": daily_battery_wh,
            "daily_grid_wh": daily_grid_wh,
            "details": (
                list(first_bucket.get("details"))
                if isinstance(first_bucket.get("details"), list)
                else []
            ),
            "source": (
                f"hems_energy_consumption:{selected.get('device_uid')}"
                if selected.get("device_uid")
                else "hems_energy_consumption"
            ),
            "endpoint_type": (
                payload.get("endpoint_type")
                if payload.get("endpoint_type") is not None
                else payload.get("type")
            ),
            "endpoint_timestamp": (
                payload.get("endpoint_timestamp")
                if payload.get("endpoint_timestamp") is not None
                else payload.get("timestamp")
            ),
        }

    async def _async_refresh_heatpump_daily_consumption(
        self, *, force: bool = False
    ) -> None:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            self._heatpump_daily_consumption = None
            self._heatpump_daily_consumption_cache_until = None
            self._heatpump_daily_consumption_backoff_until = None
            self._heatpump_daily_consumption_last_error = None
            self._heatpump_daily_consumption_cache_key = None
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

        await self._async_refresh_hems_support_preflight(force=force)
        if getattr(self.client, "hems_site_supported", None) is False:
            self._heatpump_daily_consumption = None
            self._heatpump_daily_consumption_cache_until = (
                now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL
            )
            self._heatpump_daily_consumption_backoff_until = None
            self._heatpump_daily_consumption_last_error = None
            self._heatpump_daily_consumption_cache_key = marker
            return

        fetcher = getattr(self.client, "hems_energy_consumption", None)
        if not callable(fetcher):
            return

        try:
            payload = await fetcher(
                start_at=start_at,
                end_at=end_at,
                timezone=tz_name,
                step="P1D",
            )
        except Exception as err:  # noqa: BLE001
            self._heatpump_daily_consumption_last_error = (
                redact_text(err, site_ids=(self.site_id,)) or err.__class__.__name__
            )
            self._heatpump_daily_consumption_backoff_until = (
                now + HEATPUMP_DAILY_CONSUMPTION_FAILURE_BACKOFF_S
            )
            self._heatpump_daily_consumption_cache_until = None
            self._heatpump_daily_consumption_cache_key = marker
            return

        self._heatpump_daily_consumption_cache_until = (
            now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL
        )
        self._heatpump_daily_consumption_backoff_until = None
        self._heatpump_daily_consumption_last_error = None
        self._heatpump_daily_consumption_cache_key = marker
        if not isinstance(payload, dict):
            self._heatpump_daily_consumption = None
            return

        snapshot = self._build_heatpump_daily_consumption_snapshot(payload)
        if snapshot is not None:
            snapshot["day_key"] = marker[0]
            snapshot["timezone"] = marker[1]
        self._heatpump_daily_consumption = snapshot

    async def async_refresh_heatpump_daily_consumption(
        self, *, force: bool = False
    ) -> None:
        await self._async_refresh_heatpump_daily_consumption(force=force)

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
            self._heatpump_power_w = None
            self._heatpump_power_sample_utc = None
            self._heatpump_power_start_utc = None
            self._heatpump_power_device_uid = None
            self._heatpump_power_source = None
            self._heatpump_power_snapshot = None
            self._heatpump_power_cache_until = None
            self._heatpump_power_backoff_until = None
            self._heatpump_power_last_error = None
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
            self._heatpump_power_w = None
            self._heatpump_power_sample_utc = None
            self._heatpump_power_start_utc = None
            self._heatpump_power_device_uid = None
            self._heatpump_power_source = None
            self._heatpump_power_snapshot = {
                "site_date": self._site_local_current_date(),
                "force": force,
                "outcome": "unsupported_site",
            }
            self._heatpump_power_cache_until = now + HEATPUMP_POWER_CACHE_TTL
            self._heatpump_power_backoff_until = None
            self._heatpump_power_last_error = None
            self._heatpump_power_selection_marker = None
            return

        fetcher = getattr(self.client, "hems_power_timeseries", None)
        if not callable(fetcher):
            self._heatpump_power_snapshot = {
                "site_date": self._site_local_current_date(),
                "force": force,
                "outcome": "fetcher_unavailable",
            }
            return

        site_date = self._site_local_current_date()
        candidate_uids, compare_all, marker = self._heatpump_power_fetch_plan()
        candidate_summaries = [
            self._heatpump_power_debug_candidate_summary(candidate_uid)
            for candidate_uid in candidate_uids
        ]
        power_snapshot: dict[str, object] = {
            "site_date": site_date,
            "force": force,
            "compare_all": compare_all,
            "previous_device_ref": self._debug_truncate_identifier(
                self._heatpump_power_device_uid
            ),
            "candidates": candidate_summaries,
            "attempts": [],
            "selected_payload": None,
            "selected_source": None,
            "selected_sample_at_utc": None,
            "last_error": None,
            "outcome": "pending",
        }
        self._heatpump_power_snapshot = power_snapshot
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Heat pump power fetch plan for site %s: site_date=%s force=%s compare_all=%s previous_device_uid=%s candidates=%s",
                redact_site_id(self.site_id),
                site_date,
                force,
                compare_all,
                self._debug_truncate_identifier(self._heatpump_power_device_uid)
                or "[redacted]",
                candidate_summaries,
            )
        payload: dict[str, object] | None = None
        sample: tuple[int, float] | None = None
        requested_uid: str | None = None
        selected_key: tuple[int, int, int, int, float, int, int] | None = None
        last_error: Exception | None = None
        for candidate_uid in candidate_uids:
            try:
                current_payload = await fetcher(
                    device_uid=candidate_uid,
                    site_date=site_date,
                )
            except Exception as err:  # noqa: BLE001
                last_error = err
                redacted_error = (
                    redact_text(
                        err,
                        site_ids=(self.site_id,),
                        identifiers=self._heatpump_power_redaction_identifiers(
                            candidate_uid
                        ),
                    )
                    or err.__class__.__name__
                )
                attempts = power_snapshot.setdefault("attempts", [])
                if isinstance(attempts, list):
                    attempts.append(
                        {
                            "requested_device_ref": self._debug_truncate_identifier(
                                candidate_uid
                            ),
                            "error": redacted_error,
                        }
                    )
                _LOGGER.debug(
                    "Heat pump power fetch failed (requested_device_uid=%s): %s",
                    self._debug_truncate_identifier(candidate_uid) or "[redacted]",
                    redacted_error,
                )
                continue
            if not isinstance(current_payload, dict):
                attempts = power_snapshot.setdefault("attempts", [])
                if isinstance(attempts, list):
                    attempts.append(
                        {
                            "requested_device_ref": self._debug_truncate_identifier(
                                candidate_uid
                            ),
                            "payload_type": type(current_payload).__name__,
                            "usable_payload": False,
                        }
                    )
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "Heat pump power fetch returned unusable payload for site %s: requested_device_uid=%s payload_type=%s",
                        redact_site_id(self.site_id),
                        self._debug_truncate_identifier(candidate_uid) or "[redacted]",
                        type(current_payload).__name__,
                    )
                continue
            current_sample = self._heatpump_latest_power_sample(current_payload)
            current_key = self._heatpump_power_selection_key(
                current_payload,
                requested_uid=candidate_uid,
                sample=current_sample,
            )
            current_summary = self._heatpump_power_debug_payload_summary(
                current_payload,
                requested_uid=candidate_uid,
                sample=current_sample,
                selection_key=current_key,
            )
            attempts = power_snapshot.setdefault("attempts", [])
            if isinstance(attempts, list):
                attempts.append(current_summary)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Heat pump power candidate payload for site %s: %s",
                    redact_site_id(self.site_id),
                    current_summary,
                )
            if selected_key is None or current_key > selected_key:
                payload = current_payload
                requested_uid = candidate_uid
                sample = current_sample
                selected_key = current_key

        if payload is None and last_error is not None:
            last_error_text = (
                redact_text(
                    last_error,
                    site_ids=(self.site_id,),
                    identifiers=self._heatpump_power_redaction_identifiers(
                        requested_uid
                    ),
                )
                or last_error.__class__.__name__
            )
            self._heatpump_power_last_error = last_error_text
            power_snapshot["last_error"] = last_error_text
            power_snapshot["outcome"] = "fetch_error"
            self._heatpump_power_backoff_until = now + HEATPUMP_POWER_FAILURE_BACKOFF_S
            self._heatpump_power_cache_until = None
            return

        self._heatpump_power_cache_until = now + HEATPUMP_POWER_CACHE_TTL
        self._heatpump_power_backoff_until = None
        self._heatpump_power_last_error = None
        if payload is not None:
            self._heatpump_power_selection_marker = marker
        self._heatpump_power_w = None
        self._heatpump_power_sample_utc = None
        self._heatpump_power_start_utc = None
        self._heatpump_power_device_uid = requested_uid
        self._heatpump_power_source = "hems_power_timeseries"

        if not isinstance(payload, dict):
            power_snapshot["outcome"] = "no_usable_payload"
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Heat pump power refresh found no usable payload for site %s: site_date=%s candidates=%s",
                    redact_site_id(self.site_id),
                    site_date,
                    candidate_summaries,
                )
            return

        payload_uid = type_member_text(payload, "device_uid", "uid")
        if payload_uid:
            self._heatpump_power_device_uid = payload_uid
        if self._heatpump_power_device_uid:
            self._heatpump_power_source = (
                f"hems_power_timeseries:{self._heatpump_power_device_uid}"
            )
        selected_summary = self._heatpump_power_debug_payload_summary(
            payload,
            requested_uid=requested_uid,
            sample=sample,
            selection_key=selected_key,
        )
        power_snapshot["selected_payload"] = selected_summary
        if self._heatpump_power_device_uid:
            power_snapshot["selected_source"] = (
                "hems_power_timeseries:"
                f"{self._debug_truncate_identifier(self._heatpump_power_device_uid)}"
            )
        else:
            power_snapshot["selected_source"] = self._heatpump_power_source
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Heat pump power selected payload for site %s: %s",
                redact_site_id(self.site_id),
                selected_summary,
            )
        if sample is None:
            power_snapshot["outcome"] = "no_usable_sample"
            return
        sample_index, sample_value = sample
        self._heatpump_power_w = sample_value
        power_snapshot["outcome"] = "selected_sample"

        start_utc = parse_inverter_last_report(payload.get("start_date"))
        self._heatpump_power_start_utc = start_utc
        interval_minutes = coerce_optional_float(payload.get("interval_minutes"))
        if interval_minutes is None:
            values = payload.get("heat_pump_consumption")
            if isinstance(values, list):
                now_utc = dt_util.utcnow()
                if now_utc.tzinfo is None:
                    now_utc = now_utc.replace(tzinfo=_tz.utc)
                interval_minutes = self._infer_heatpump_interval_minutes(
                    start_utc,
                    len(values),
                    now_utc,
                )
        if (
            start_utc is not None
            and interval_minutes is not None
            and interval_minutes > 0
            and sample_index is not None
        ):
            try:
                self._heatpump_power_sample_utc = start_utc + timedelta(
                    minutes=interval_minutes * sample_index
                )
            except Exception:
                self._heatpump_power_sample_utc = None
        elif sample_index is not None:
            self._heatpump_power_sample_utc = dt_util.utcnow()
        power_snapshot["selected_sample_at_utc"] = (
            self._heatpump_power_sample_utc.isoformat()
            if self._heatpump_power_sample_utc is not None
            else None
        )

    async def async_refresh_heatpump_power(self, *, force: bool = False) -> None:
        await self._async_refresh_heatpump_power(force=force)

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
            "runtime_state_last_error": getattr(
                self, "_heatpump_runtime_state_last_error", None
            ),
            "daily_consumption": self._copy_diagnostics_value(
                getattr(self, "_heatpump_daily_consumption", None)
            ),
            "daily_consumption_last_error": getattr(
                self, "_heatpump_daily_consumption_last_error", None
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
