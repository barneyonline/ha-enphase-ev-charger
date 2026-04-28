"""Read-only tariff parsing and refresh helpers for Enphase sites."""

from __future__ import annotations

import calendar
import copy
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
import logging
import math
import re
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.util import dt as dt_util

from .api import InvalidPayloadError, OptionalEndpointUnavailable
from .const import DOMAIN
from .service_validation import raise_translated_service_validation

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

TARIFF_ENDPOINT_FAMILY = "tariff"
_EXPORT_RATE_KEY_RE = re.compile(r"[^a-z0-9]+")
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class TariffRateLocator:
    """Stable location for an editable tariff rate in the raw payload."""

    branch: str
    kind: str
    season_index: int
    season_id: str | None = None
    day_index: int | None = None
    day_group_id: str | None = None
    period_index: int | None = None
    period_id: str | None = None
    period_type: str | None = None
    tier_index: int | None = None
    tier_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable locator."""

        return {
            key: value
            for key, value in {
                "branch": self.branch,
                "kind": self.kind,
                "season_index": self.season_index,
                "season_id": self.season_id,
                "day_index": self.day_index,
                "day_group_id": self.day_group_id,
                "period_index": self.period_index,
                "period_id": self.period_id,
                "period_type": self.period_type,
                "tier_index": self.tier_index,
                "tier_id": self.tier_id,
            }.items()
            if value is not None
        }

    @classmethod
    def from_object(cls, value: object) -> "TariffRateLocator | None":
        """Parse a locator from entity state attributes or service data."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        branch = _clean_text(value.get("branch"))
        kind = _clean_text(value.get("kind"))
        season_index = _int_or_none(value.get("season_index"))
        if branch not in {"purchase", "buyback"} or kind not in {
            "period",
            "tier",
            "off_peak",
        }:
            return None
        if season_index is None or season_index < 1:
            return None
        return cls(
            branch=branch,
            kind=kind,
            season_index=season_index,
            season_id=_clean_text(value.get("season_id")),
            day_index=_int_or_none(value.get("day_index")),
            day_group_id=_clean_text(value.get("day_group_id")),
            period_index=_int_or_none(value.get("period_index")),
            period_id=_clean_text(value.get("period_id")),
            period_type=_clean_text(value.get("period_type")),
            tier_index=_int_or_none(value.get("tier_index")),
            tier_id=_clean_text(value.get("tier_id")),
        )


@dataclass(slots=True, frozen=True)
class TariffBillingSnapshot:
    """Normalized billing-cycle metadata."""

    start_date: str | None
    billing_frequency: str | None
    billing_interval_value: int | None
    billing_cycle: str | None

    @property
    def state(self) -> str | None:
        """Return the concise billing-cycle label."""

        return self.billing_cycle

    @property
    def attributes(self) -> dict[str, object]:
        """Return Home Assistant state attributes."""

        return {
            "start_date": self.start_date,
            "billing_frequency": self.billing_frequency,
            "billing_interval_value": self.billing_interval_value,
            "billing_cycle": self.billing_cycle,
        }


@dataclass(slots=True, frozen=True)
class TariffRateSnapshot:
    """Normalized tariff branch metadata."""

    state: str | None
    rate_structure: str | None
    variation_type: str | None
    source: str | None
    currency: str | None
    export_plan: str | None
    seasons: tuple[dict[str, object], ...]
    branch_key: str | None = None

    @property
    def attributes(self) -> dict[str, object]:
        """Return Home Assistant state attributes."""

        attrs: dict[str, object] = {
            "rate_structure": self.rate_structure,
            "variation_type": self.variation_type,
            "source": self.source,
            "currency": self.currency,
            "seasons": [dict(season) for season in self.seasons],
        }
        if self.export_plan is not None:
            attrs["export_plan"] = self.export_plan
        return attrs


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _rate_structure_label(value: object) -> str | None:
    type_id = (_clean_text(value) or "").lower()
    return {
        "flat": "Flat",
        "tou": "Time of use",
        "tiered": "Tiered",
    }.get(type_id, _clean_text(value))


def _variation_label(value: object) -> str | None:
    type_kind = (_clean_text(value) or "").lower()
    return {
        "single": "Single",
        "seasonal": "Seasonal",
        "weekends": "Weekdays and weekends",
        "seasonal-and-weekends": "Seasonal weekdays and weekends",
    }.get(type_kind, _clean_text(value))


def _minutes_to_hhmm(value: object) -> str | None:
    minutes = _int_or_none(value)
    if minutes is None:
        return None
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _normalize_days(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    days: list[int] = []
    for value in values:
        day = _int_or_none(value)
        if day is not None:
            days.append(day)
    return days


def _normalize_period(period: object) -> dict[str, object] | None:
    if not isinstance(period, dict):
        return None
    normalized: dict[str, object] = {
        "id": _clean_text(period.get("id")),
        "type": _clean_text(period.get("type")),
        "rate": _clean_text(period.get("rate")),
        "start_time": _minutes_to_hhmm(period.get("startTime")),
        "end_time": _minutes_to_hhmm(period.get("endTime")),
    }
    return {key: value for key, value in normalized.items() if value is not None}


def _normalize_tier(tier: object) -> dict[str, object] | None:
    if not isinstance(tier, dict):
        return None
    normalized: dict[str, object] = {
        "id": _clean_text(tier.get("id")),
        "rate": _clean_text(tier.get("rate")),
        "start_value": _clean_text(tier.get("startValue")),
    }
    end_value = tier.get("endValue")
    if end_value == -1 or _clean_text(end_value) == "-1":
        normalized["end_value"] = None
        normalized["unbounded"] = True
    else:
        normalized["end_value"] = _clean_text(end_value)
    return {key: value for key, value in normalized.items() if value is not None}


def _normalize_day_group(day_group: object) -> dict[str, object] | None:
    if not isinstance(day_group, dict):
        return None
    periods = [
        item
        for period in day_group.get("periods", [])
        if (item := _normalize_period(period)) is not None
    ]
    normalized: dict[str, object] = {
        "id": _clean_text(day_group.get("id")),
        "days": _normalize_days(day_group.get("days")),
        "periods": periods,
    }
    return {key: value for key, value in normalized.items() if value not in (None, [])}


def _normalize_season(season: object) -> dict[str, object] | None:
    if not isinstance(season, dict):
        return None
    normalized: dict[str, object] = {
        "id": _clean_text(season.get("id")),
        "start_month": _int_or_none(season.get("startMonth")),
        "end_month": _int_or_none(season.get("endMonth")),
    }
    days = [
        item
        for day_group in season.get("days", [])
        if (item := _normalize_day_group(day_group)) is not None
    ]
    tiers = [
        item
        for tier in season.get("tiers", [])
        if (item := _normalize_tier(tier)) is not None
    ]
    if days:
        normalized["days"] = days
    if tiers:
        normalized["tiers"] = tiers
    off_peak = _clean_text(season.get("offPeak"))
    if off_peak is not None:
        normalized["off_peak"] = off_peak
    return {key: value for key, value in normalized.items() if value is not None}


def _slug(value: object, fallback: str) -> str:
    text = (_clean_text(value) or fallback).lower()
    slug = _EXPORT_RATE_KEY_RE.sub("_", text).strip("_")
    return slug or fallback


def _rate_value(rate: object) -> float | None:
    rate_text = _clean_text(rate)
    if rate_text is None:
        return None
    try:
        return float(rate_text)
    except ValueError:
        return None


def _rate_unit(currency: object) -> str | None:
    currency_text = _clean_text(currency)
    if currency_text is None:
        return None
    if len(currency_text) == 3 and currency_text.isalpha():
        currency_text = currency_text.upper()
    return f"{currency_text}/kWh"


def _format_rate(rate: object, currency: object) -> str | None:
    rate_text = _clean_text(rate)
    if rate_text is None:
        return None
    currency_text = _clean_text(currency)
    if currency_text is None:
        return rate_text
    if len(currency_text) == 3 and currency_text.isalpha():
        return f"{rate_text} {currency_text.upper()}"
    return f"{currency_text}{rate_text}"


def _rate_detail_name(*parts: object) -> str:
    names = [_clean_text(part) for part in parts]
    return " ".join(part.title() for part in names if part)


def _month_matches(
    month: int,
    start_month: object,
    end_month: object,
) -> bool:
    start = _int_or_none(start_month)
    end = _int_or_none(end_month)
    if start is None or end is None:
        return True
    if not 1 <= start <= 12 or not 1 <= end <= 12:
        return True
    if start <= end:
        return start <= month <= end
    return month >= start or month <= end


def _time_to_minutes(value: object) -> int | None:
    text = _clean_text(value)
    if text is None:
        return None
    parts = text.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _time_window_matches(
    minute_of_day: int,
    start_time: object,
    end_time: object,
) -> bool:
    start = _time_to_minutes(start_time)
    end = _time_to_minutes(end_time)
    if start is None or end is None or start == end:
        return True
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def current_tariff_rate_sensor_spec(
    snapshot: TariffRateSnapshot | None,
    when: datetime,
) -> dict | None:
    """Return the unambiguous current rate spec for an Energy price sensor."""

    specs = tariff_rate_sensor_specs(snapshot)
    if not specs:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    month = when.month
    weekday = when.isoweekday()
    minute_of_day = when.hour * 60 + when.minute

    matches: list[dict] = []
    for spec in specs:
        attrs = spec.get("attributes") or {}
        if not _month_matches(
            month,
            attrs.get("start_month"),
            attrs.get("end_month"),
        ):
            continue
        days = attrs.get("days")
        if (
            isinstance(days, list)
            and days
            and weekday
            not in {day for item in days if (day := _int_or_none(item)) is not None}
        ):
            continue
        if not _time_window_matches(
            minute_of_day,
            attrs.get("start_time"),
            attrs.get("end_time"),
        ):
            continue
        matches.append(spec)
    if len(matches) != 1:
        return None
    return matches[0]


def next_tariff_rate_change(
    snapshot: TariffRateSnapshot | None,
    when: datetime,
) -> datetime | None:
    """Return the next time the active tariff rate may change."""

    specs = tariff_rate_sensor_specs(snapshot)
    if not specs:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    active = current_tariff_rate_sensor_spec(snapshot, when)
    active_key = None if active is None else active.get("key")
    active_state = None if active is None else active.get("state")
    tzinfo = when.tzinfo
    candidates: set[datetime] = set()
    start_date = when.date()
    for day_offset in range(0, 370):
        day = start_date + timedelta(days=day_offset)
        candidates.add(datetime.combine(day, dt_time.min, tzinfo))
        for spec in specs:
            attrs = spec.get("attributes") or {}
            for attr in ("start_time", "end_time"):
                minute_of_day = _time_to_minutes(attrs.get(attr))
                if minute_of_day is None:
                    continue
                candidates.add(
                    datetime.combine(
                        day,
                        dt_time(minute_of_day // 60, minute_of_day % 60),
                        tzinfo,
                    )
                )

    for candidate in sorted(candidates):
        if candidate <= when:
            continue
        candidate_spec = current_tariff_rate_sensor_spec(snapshot, candidate)
        candidate_key = None if candidate_spec is None else candidate_spec.get("key")
        candidate_state = (
            None if candidate_spec is None else candidate_spec.get("state")
        )
        if (candidate_key, candidate_state) != (active_key, active_state):
            return candidate
    return None


def tariff_rate_sensor_specs(snapshot: TariffRateSnapshot | None) -> tuple[dict, ...]:
    """Return per-rate sensor specs for period and tiered tariffs."""

    if snapshot is None:
        return ()
    specs: list[dict] = []
    used_keys: set[str] = set()

    def _append_spec(base_key: str, name: str, state: float, attrs: dict) -> None:
        key = base_key
        index = 2
        while key in used_keys:
            key = f"{base_key}_{index}"
            index += 1
        used_keys.add(key)
        specs.append(
            {
                "key": key,
                "name": name,
                "state": state,
                "unit": _rate_unit(snapshot.currency),
                "attributes": attrs,
            }
        )

    base_attrs = {
        "rate_structure": snapshot.rate_structure,
        "variation_type": snapshot.variation_type,
        "source": snapshot.source,
        "currency": snapshot.currency,
        "export_plan": snapshot.export_plan,
    }
    branch_key = (
        snapshot.branch_key if snapshot.branch_key in {"purchase", "buyback"} else None
    )
    for season_index, season in enumerate(snapshot.seasons, start=1):
        season_attrs = {
            **base_attrs,
            "season_id": season.get("id"),
            "start_month": season.get("start_month"),
            "end_month": season.get("end_month"),
        }
        off_peak_state = _rate_value(season.get("off_peak"))
        if off_peak_state is not None:
            attrs = {
                **season_attrs,
                "rate": season.get("off_peak"),
                "formatted_rate": _format_rate(
                    season.get("off_peak"), snapshot.currency
                ),
            }
            if branch_key is not None:
                attrs["tariff_locator"] = TariffRateLocator(
                    branch=branch_key,
                    kind="off_peak",
                    season_index=season_index,
                    season_id=_clean_text(season.get("id")),
                ).as_dict()
            _append_spec(
                "_".join(
                    (
                        _slug(season.get("id"), f"season_{season_index}"),
                        "off_peak",
                    )
                ),
                "Off-Peak",
                off_peak_state,
                {attr: value for attr, value in attrs.items() if value is not None},
            )
        for day_index, day_group in enumerate(season.get("days", []), start=1):
            if not isinstance(day_group, dict):
                continue
            day_attrs = {
                **season_attrs,
                "day_group_id": day_group.get("id"),
                "days": day_group.get("days"),
            }
            for period_index, period in enumerate(
                day_group.get("periods", []), start=1
            ):
                if not isinstance(period, dict):
                    continue
                state = _rate_value(period.get("rate"))
                if state is None:
                    continue
                period_type = _clean_text(period.get("type"))
                period_id = _clean_text(period.get("id"))
                name = _rate_detail_name(period_type or period_id) or (
                    f"Period {period_index}"
                )
                key = "_".join(
                    (
                        _slug(season.get("id"), f"season_{season_index}"),
                        _slug(day_group.get("id"), f"days_{day_index}"),
                        _slug(period_type or period_id, f"period_{period_index}"),
                    )
                )
                attrs = {
                    **day_attrs,
                    "period_id": period_id,
                    "period_type": period_type,
                    "start_time": period.get("start_time"),
                    "end_time": period.get("end_time"),
                    "rate": period.get("rate"),
                    "formatted_rate": _format_rate(
                        period.get("rate"), snapshot.currency
                    ),
                }
                if branch_key is not None:
                    attrs["tariff_locator"] = TariffRateLocator(
                        branch=branch_key,
                        kind="period",
                        season_index=season_index,
                        season_id=_clean_text(season.get("id")),
                        day_index=day_index,
                        day_group_id=_clean_text(day_group.get("id")),
                        period_index=period_index,
                        period_id=period_id,
                        period_type=period_type,
                    ).as_dict()
                _append_spec(
                    key,
                    name,
                    state,
                    {attr: value for attr, value in attrs.items() if value is not None},
                )
        for tier_index, tier in enumerate(season.get("tiers", []), start=1):
            if not isinstance(tier, dict):
                continue
            state = _rate_value(tier.get("rate"))
            if state is None:
                continue
            tier_id = _clean_text(tier.get("id"))
            name = _rate_detail_name(tier_id) or f"Tier {tier_index}"
            key = "_".join(
                (
                    _slug(season.get("id"), f"season_{season_index}"),
                    _slug(tier_id, f"tier_{tier_index}"),
                )
            )
            attrs = {
                **season_attrs,
                "tier_id": tier_id,
                "start_value": tier.get("start_value"),
                "end_value": tier.get("end_value"),
                "unbounded": tier.get("unbounded"),
                "rate": tier.get("rate"),
                "formatted_rate": _format_rate(tier.get("rate"), snapshot.currency),
            }
            if branch_key is not None:
                attrs["tariff_locator"] = TariffRateLocator(
                    branch=branch_key,
                    kind="tier",
                    season_index=season_index,
                    season_id=_clean_text(season.get("id")),
                    tier_index=tier_index,
                    tier_id=tier_id,
                ).as_dict()
            _append_spec(
                key,
                name,
                state,
                {attr: value for attr, value in attrs.items() if value is not None},
            )
    return tuple(specs)


def export_rate_sensor_specs(snapshot: TariffRateSnapshot | None) -> tuple[dict, ...]:
    """Return per-export-rate sensor specs for period and tiered tariffs."""

    return tariff_rate_sensor_specs(snapshot)


def _add_months_clamped(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_billing_date(
    snapshot: TariffBillingSnapshot,
    *,
    today: date | None = None,
) -> date | None:
    """Return the next billing date after today for a billing snapshot."""

    if snapshot.start_date is None or snapshot.billing_frequency is None:
        return None
    interval = (
        1
        if snapshot.billing_interval_value is None
        else snapshot.billing_interval_value
    )
    if interval < 1:
        return None
    try:
        start = date.fromisoformat(snapshot.start_date)
    except ValueError:
        return None
    candidate = start
    today = today or dt_util.now().date()
    if snapshot.billing_frequency == "DAY":
        while candidate <= today:
            candidate = candidate + timedelta(days=interval)
        return candidate
    if snapshot.billing_frequency == "MONTH":
        elapsed_months = 0
        while candidate <= today:
            elapsed_months += interval
            candidate = _add_months_clamped(start, elapsed_months)
        return candidate
    return None


def parse_tariff_billing(payload: object) -> TariffBillingSnapshot | None:
    """Return a normalized billing snapshot from a billing-details payload."""

    if not isinstance(payload, dict):
        return None
    frequency = (_clean_text(payload.get("billingFrequency")) or "").upper()
    interval = _int_or_none(payload.get("billingIntervalValue"))
    start_date = _clean_text(payload.get("anyBillPeriodStartDate"))
    if not frequency and interval is None and start_date is None:
        return None
    state: str | None
    if frequency == "MONTH":
        if interval in (None, 1):
            state = "Monthly"
        else:
            state = f"Every {interval} months"
    elif frequency == "DAY":
        if interval in (None, 1):
            state = "Daily"
        else:
            state = f"Every {interval} days"
    else:
        state = _clean_text(payload.get("billingFrequency"))
    return TariffBillingSnapshot(
        start_date=start_date,
        billing_frequency=frequency or None,
        billing_interval_value=interval,
        billing_cycle=state,
    )


def parse_tariff_rate(
    payload: object,
    branch_key: str,
) -> TariffRateSnapshot | None:
    """Return a normalized import/export tariff snapshot."""

    if not isinstance(payload, dict):
        return None
    branch = payload.get(branch_key)
    if not isinstance(branch, dict):
        return None
    seasons = [
        item
        for season in branch.get("seasons", [])
        if (item := _normalize_season(season)) is not None
    ]
    rate_structure = _rate_structure_label(branch.get("typeId"))
    variation_type = _variation_label(branch.get("typeKind"))
    state = rate_structure
    if state is None and not seasons:
        return None
    return TariffRateSnapshot(
        state=state,
        rate_structure=rate_structure,
        variation_type=variation_type,
        source=_clean_text(branch.get("source")),
        currency=_clean_text(payload.get("currency")),
        export_plan=(
            _clean_text(branch.get("exportPlan")) if branch_key == "buyback" else None
        ),
        seasons=tuple(seasons),
        branch_key=branch_key,
    )


def _format_write_rate(value: float) -> str:
    if not math.isfinite(value) or value < 0:
        _raise_tariff_validation(
            "tariff_rate_invalid",
            message="Tariff rate must be a non-negative number.",
        )
    return f"{value:.10f}".rstrip("0").rstrip(".") or "0"


def _raise_tariff_validation(
    key: str,
    *,
    placeholders: dict[str, object] | None = None,
    message: str | None = None,
) -> None:
    raise_translated_service_validation(
        translation_domain=DOMAIN,
        translation_key=f"exceptions.{key}",
        translation_placeholders=placeholders,
        message=message,
    )


def _index_item(items: object, index: int | None) -> dict[str, object] | None:
    if index is None or index < 1 or not isinstance(items, list):
        return None
    try:
        item = items[index - 1]
    except IndexError:
        return None
    return item if isinstance(item, dict) else None


def _matches_text(actual: object, expected: str | None) -> bool:
    return expected is None or _clean_text(actual) == expected


def _locate_tariff_rate(
    payload: dict[str, object],
    locator: TariffRateLocator,
) -> tuple[dict[str, object], str]:
    branch = payload.get(locator.branch)
    if not isinstance(branch, dict):
        _raise_tariff_validation(
            "tariff_rate_target_not_found",
            message="Tariff rate target was not found in the latest tariff payload.",
        )
    season = _index_item(branch.get("seasons"), locator.season_index)
    if season is None or not _matches_text(season.get("id"), locator.season_id):
        _raise_tariff_validation(
            "tariff_rate_target_not_found",
            message="Tariff rate target was not found in the latest tariff payload.",
        )

    if locator.kind == "off_peak":
        if "offPeak" not in season:
            _raise_tariff_validation(
                "tariff_rate_target_not_found",
                message=(
                    "Tariff rate target was not found in the latest tariff payload."
                ),
            )
        return season, "offPeak"

    if locator.kind == "period":
        day_group = _index_item(season.get("days"), locator.day_index)
        if day_group is None or not _matches_text(
            day_group.get("id"), locator.day_group_id
        ):
            _raise_tariff_validation(
                "tariff_rate_target_not_found",
                message=(
                    "Tariff rate target was not found in the latest tariff payload."
                ),
            )
        period = _index_item(day_group.get("periods"), locator.period_index)
        if (
            period is None
            or not _matches_text(period.get("id"), locator.period_id)
            or not _matches_text(period.get("type"), locator.period_type)
        ):
            _raise_tariff_validation(
                "tariff_rate_target_not_found",
                message=(
                    "Tariff rate target was not found in the latest tariff payload."
                ),
            )
        return period, "rate"

    if locator.kind == "tier":
        tier = _index_item(season.get("tiers"), locator.tier_index)
        if tier is None or not _matches_text(tier.get("id"), locator.tier_id):
            _raise_tariff_validation(
                "tariff_rate_target_not_found",
                message=(
                    "Tariff rate target was not found in the latest tariff payload."
                ),
            )
        return tier, "rate"

    _raise_tariff_validation(
        "tariff_rate_target_invalid",
        message="Tariff rate target is invalid.",
    )


class TariffRuntime:
    """Fetch, normalize, and update site tariff data."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator

    def refresh_due(self) -> bool:
        """Return whether tariff data should be refreshed this cycle."""

        return self.coordinator._endpoint_family_should_run(TARIFF_ENDPOINT_FAMILY)

    async def async_refresh(self, *, force: bool = False) -> None:
        """Refresh tariff billing and rate snapshots."""

        coord = self.coordinator
        if not coord._endpoint_family_should_run(TARIFF_ENDPOINT_FAMILY, force=force):
            return
        try:
            site_tariff_bundle = getattr(coord.client, "site_tariff_bundle", None)
            if not callable(site_tariff_bundle):
                raise OptionalEndpointUnavailable("Tariff API is unavailable")
            billing_payload, tariff_payload = await site_tariff_bundle()
            billing = parse_tariff_billing(billing_payload)
            import_rate = parse_tariff_rate(tariff_payload, "purchase")
            export_rate = parse_tariff_rate(tariff_payload, "buyback")
        except (
            aiohttp.ClientError,
            AttributeError,
            InvalidPayloadError,
            OptionalEndpointUnavailable,
            TimeoutError,
        ) as err:
            if self._has_stale_data():
                coord._note_endpoint_family_failure(TARIFF_ENDPOINT_FAMILY, err)
                return
            raise OptionalEndpointUnavailable("Tariff data is unavailable") from err
        if billing is None and import_rate is None and export_rate is None:
            err = OptionalEndpointUnavailable("Tariff payload did not include data")
            if self._has_stale_data():
                coord._note_endpoint_family_failure(TARIFF_ENDPOINT_FAMILY, err)
                return

        refresh_time = dt_util.utcnow()
        coord.tariff_billing = billing
        coord.tariff_import_rate = import_rate
        coord.tariff_export_rate = export_rate
        coord.tariff_last_refresh_utc = refresh_time
        if isinstance(tariff_payload, dict) and tariff_payload:
            coord.tariff_rates_last_refresh_utc = refresh_time
        coord._note_endpoint_family_success(TARIFF_ENDPOINT_FAMILY)

    async def async_set_tariff_rate(
        self,
        locator: TariffRateLocator | dict[str, object],
        value: float,
    ) -> dict:
        """Update one existing tariff rate value."""

        parsed_locator = TariffRateLocator.from_object(locator)
        if parsed_locator is None:
            _raise_tariff_validation(
                "tariff_rate_target_invalid",
                message="Tariff rate target is invalid.",
            )
        try:
            rate_value = float(value)
        except (TypeError, ValueError):
            _raise_tariff_validation(
                "tariff_rate_invalid",
                message="Tariff rate must be a non-negative number.",
            )
        rate = _format_write_rate(rate_value)
        coord = self.coordinator
        site_tariff = getattr(coord.client, "site_tariff", None)
        site_tariff_update = getattr(coord.client, "site_tariff_update", None)
        if not callable(site_tariff) or not callable(site_tariff_update):
            _raise_tariff_validation(
                "tariff_rate_api_unavailable",
                message="Tariff write API is unavailable.",
            )
        payload = await site_tariff()
        if not isinstance(payload, dict):
            _raise_tariff_validation(
                "tariff_rate_api_unavailable",
                message="Tariff write API is unavailable.",
            )
        update_payload = copy.deepcopy(payload)
        target, field = _locate_tariff_rate(update_payload, parsed_locator)
        target[field] = rate
        result = await site_tariff_update(update_payload)

        notifier = getattr(coord.client, "notify_tariff_change", None)
        if callable(notifier):
            try:
                await notifier()
            except (aiohttp.ClientError, AttributeError, TimeoutError) as err:
                _LOGGER.debug(
                    "Tariff change notification failed for site %s: %s",
                    getattr(coord, "site_id", None),
                    err,
                )
        await coord.tariff_runtime.async_refresh(force=True)
        return result

    def _has_stale_data(self) -> bool:
        """Return whether a prior tariff snapshot can stay visible."""

        coord = self.coordinator
        return (
            getattr(coord, "tariff_billing", None) is not None
            or getattr(coord, "tariff_import_rate", None) is not None
            or getattr(coord, "tariff_export_rate", None) is not None
        )

    def diagnostics(self) -> dict[str, object]:
        """Return tariff refresh diagnostics without raw tariff payloads."""

        coord = self.coordinator
        health = coord._endpoint_family_state(TARIFF_ENDPOINT_FAMILY)
        last_refresh_utc = getattr(coord, "tariff_last_refresh_utc", None)
        rates_last_refresh_utc = getattr(coord, "tariff_rates_last_refresh_utc", None)
        return {
            "billing_available": getattr(coord, "tariff_billing", None) is not None,
            "import_rate_available": (
                getattr(coord, "tariff_import_rate", None) is not None
            ),
            "export_rate_available": (
                getattr(coord, "tariff_export_rate", None) is not None
            ),
            "last_refresh_utc": (
                last_refresh_utc.isoformat()
                if hasattr(last_refresh_utc, "isoformat")
                else None
            ),
            "rates_last_refresh_utc": (
                rates_last_refresh_utc.isoformat()
                if hasattr(rates_last_refresh_utc, "isoformat")
                else None
            ),
            "endpoint_family": {
                "support_state": health.support_state,
                "cooldown_active": health.cooldown_active,
                "consecutive_failures": health.consecutive_failures,
                "last_success_utc": (
                    health.last_success_utc.isoformat()
                    if health.last_success_utc is not None
                    else None
                ),
                "last_failure_utc": (
                    health.last_failure_utc.isoformat()
                    if health.last_failure_utc is not None
                    else None
                ),
                "next_retry_utc": (
                    health.next_retry_utc.isoformat()
                    if health.next_retry_utc is not None
                    else None
                ),
                "last_status": health.last_status,
                "last_error": health.last_error,
            },
        }
