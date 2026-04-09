from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator


AC_BATTERY_TYPE_KEY = "ac_battery"
AC_BATTERY_DEVICE_NAME = "AC Battery"
AC_BATTERY_SOC_OPTIONS: list[tuple[int, str]] = [
    (value, f"{value}-{value + 5}%") for value in range(0, 100, 5)
]


def ac_battery_soc_option_label(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        normalized = int(value)
    except Exception:
        return None
    for lower_bound, label in AC_BATTERY_SOC_OPTIONS:
        if lower_bound == normalized:
            return label
    return None


def ac_battery_has_capability(coord: EnphaseCoordinator) -> bool:
    return getattr(coord, "battery_has_acb", None) is True


def ac_battery_type_available(coord: EnphaseCoordinator) -> bool:
    inventory_view = getattr(coord, "inventory_view", None)
    has_type_for_entities = getattr(inventory_view, "has_type_for_entities", None)
    if not callable(has_type_for_entities):
        return False
    return bool(has_type_for_entities(AC_BATTERY_TYPE_KEY))


def ac_battery_entities_available(coord: EnphaseCoordinator) -> bool:
    return ac_battery_has_capability(coord) and ac_battery_type_available(coord)


def ac_battery_write_access_confirmed(coord: EnphaseCoordinator) -> bool:
    if (
        getattr(coord, "battery_user_is_owner", None) is True
        or getattr(coord, "battery_user_is_installer", None) is True
    ):
        return True
    confirmed = getattr(coord, "battery_write_access_confirmed", None)
    if confirmed is not None:
        return bool(confirmed)
    return False


def ac_battery_control_available(coord: EnphaseCoordinator) -> bool:
    return ac_battery_entities_available(coord) and ac_battery_write_access_confirmed(
        coord
    )


def ac_battery_device_info(coord: EnphaseCoordinator) -> DeviceInfo:
    inventory_view = getattr(coord, "inventory_view", None)
    type_device_info = getattr(inventory_view, "type_device_info", None)
    if callable(type_device_info):
        info = type_device_info(AC_BATTERY_TYPE_KEY)
        if info is not None:
            return info
    return DeviceInfo(
        identifiers={(DOMAIN, f"type:{coord.site_id}:{AC_BATTERY_TYPE_KEY}")},
        manufacturer="Enphase",
        name=AC_BATTERY_DEVICE_NAME,
    )


def ac_battery_storage_snapshot(
    coord: EnphaseCoordinator, serial: str
) -> dict[str, object] | None:
    getter = getattr(coord, "ac_battery_storage", None)
    if not callable(getter):
        return None
    payload = getter(serial)
    if isinstance(payload, dict):
        return payload
    return None


def ac_battery_snapshot_last_reported(snapshot: dict[str, object]) -> datetime | None:
    value = snapshot.get("last_reported")
    if isinstance(value, datetime):
        return value
    return None


def ac_battery_last_reported_members(
    coord: EnphaseCoordinator,
) -> list[dict[str, object]]:
    iter_ac_battery_serials = getattr(coord, "iter_ac_battery_serials", None)
    serials = (
        [serial for serial in iter_ac_battery_serials() if serial]
        if callable(iter_ac_battery_serials)
        else []
    )
    members: list[dict[str, object]] = []
    for serial in serials:
        snapshot = ac_battery_storage_snapshot(coord, serial)
        if snapshot is None:
            members.append({"serial_number": serial})
            continue
        members.append(dict(snapshot))
    return members


def ac_battery_last_reported_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    members = ac_battery_last_reported_members(coord)
    latest_reported: datetime | None = None
    latest_reported_device: dict[str, object] | None = None
    without_last_report_count = 0
    for snapshot in members:
        last_reported = ac_battery_snapshot_last_reported(snapshot)
        if last_reported is None:
            without_last_report_count += 1
            continue
        if latest_reported is None or last_reported > latest_reported:
            latest_reported = last_reported
            latest_reported_device = {
                "serial_number": (
                    snapshot.get("serial_number")
                    or snapshot.get("identity")
                    or snapshot.get("battery_id")
                ),
                "status": (
                    snapshot.get("status_text")
                    if snapshot.get("status_text") is not None
                    else snapshot.get("status_normalized")
                ),
                "sleep_state": snapshot.get("sleep_state"),
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
