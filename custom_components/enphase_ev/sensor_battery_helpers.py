"""Battery sensor parsing helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Protocol


class BatteryLastReportedCoordinator(Protocol):
    """Coordinator surface needed to build battery last-reported snapshots."""

    battery_status_payload: object

    def iter_battery_serials(self) -> object: ...

    def battery_storage(self, serial: str) -> object: ...


def battery_parse_timestamp(value: object) -> datetime | None:
    """Parse battery timestamp values into UTC datetimes."""

    if value in (None, ""):
        return None
    try:
        if isinstance(value, datetime):
            dt_value = value
        else:
            epoch_value: float | None = None
            if isinstance(value, (int, float)):
                epoch_value = float(value)
            else:
                text = str(value).strip()
                if not text:
                    return None
                iso_text = text.replace("[UTC]", "")
                if iso_text.endswith("Z"):
                    iso_text = iso_text[:-1] + "+00:00"
                try:
                    dt_value = datetime.fromisoformat(iso_text)
                    if dt_value.tzinfo is None:
                        return dt_value.replace(tzinfo=timezone.utc)
                    return dt_value.astimezone(timezone.utc)
                except Exception:  # noqa: BLE001
                    try:
                        epoch_value = float(text.replace(",", ""))
                    except Exception:  # noqa: BLE001
                        return None
            if (
                epoch_value is None
                or not math.isfinite(epoch_value)
                or epoch_value <= 0
            ):
                return None
            if epoch_value > 1_000_000_000_000:
                epoch_value /= 1000.0
            dt_value = datetime.fromtimestamp(epoch_value, tz=timezone.utc)
        if dt_value.tzinfo is None:
            return dt_value.replace(tzinfo=timezone.utc)
        return dt_value.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def battery_optional_bool(value: object) -> bool | None:
    """Return a tolerant bool for battery payload fields."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "y", "on", "enabled"):
            return True
        if normalized in ("false", "0", "no", "n", "off", "disabled"):
            return False
    return None


def battery_snapshot_last_reported(snapshot: dict[str, object]) -> datetime | None:
    """Return the newest last-report timestamp carried by a battery snapshot."""

    # Battery status API reports storages[].last_report as epoch seconds.
    for key in ("last_report", "last_reported", "last_reported_at", "lastReportedAt"):
        parsed = battery_parse_timestamp(snapshot.get(key))
        if parsed is not None:
            return parsed
    return None


def battery_last_reported_members(
    coord: BatteryLastReportedCoordinator,
) -> list[dict[str, object]]:
    """Return battery members suitable for last-reported aggregation."""

    payload = getattr(coord, "battery_status_payload", None)
    storage_members: list[dict[str, object]] = []
    if isinstance(payload, dict):
        storages = payload.get("storages")
        if isinstance(storages, list):
            for item in storages:
                if not isinstance(item, dict):
                    continue
                if battery_optional_bool(item.get("excluded")) is True:
                    continue
                storage_members.append(dict(item))
            return storage_members

    iter_battery_serials = getattr(coord, "iter_battery_serials", None)
    battery_storage = getattr(coord, "battery_storage", None)
    serials = (
        [serial for serial in iter_battery_serials() if serial]
        if callable(iter_battery_serials)
        else []
    )
    if not callable(battery_storage):
        return [{"serial_number": serial} for serial in serials]
    for serial in serials:
        snapshot = battery_storage(serial)
        if not isinstance(snapshot, dict):
            storage_members.append({"serial_number": serial})
            continue
        storage_members.append(dict(snapshot))
    return storage_members


def battery_last_reported_snapshot(
    coord: BatteryLastReportedCoordinator,
) -> dict[str, object]:
    """Return an aggregate last-reported snapshot for site battery storage."""

    members = battery_last_reported_members(coord)
    latest_reported: datetime | None = None
    latest_reported_device: dict[str, object] | None = None
    without_last_report_count = 0
    for snapshot in members:
        last_reported = battery_snapshot_last_reported(snapshot)
        if last_reported is None:
            without_last_report_count += 1
            continue
        if latest_reported is None or last_reported > latest_reported:
            latest_reported = last_reported
            serial = (
                snapshot.get("serial_number")
                or snapshot.get("identity")
                or snapshot.get("battery_id")
                or snapshot.get("id")
            )
            latest_reported_device = {
                "serial_number": serial,
                "name": snapshot.get("name"),
                "status": (
                    snapshot.get("statusText")
                    if snapshot.get("statusText") is not None
                    else (
                        snapshot.get("status_text")
                        if snapshot.get("status_text") is not None
                        else snapshot.get("status")
                    )
                ),
            }
    return {
        "total_batteries": len(members),
        "without_last_report_count": without_last_report_count,
        "latest_reported": latest_reported,
        "latest_reported_utc": (
            latest_reported.isoformat() if latest_reported is not None else None
        ),
        "latest_reported_device": latest_reported_device,
    }
