from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from custom_components.enphase_ev.ac_battery_support import (
    AC_BATTERY_SOC_OPTIONS,
    ac_battery_control_available,
    ac_battery_device_info,
    ac_battery_entities_available,
    ac_battery_last_reported_snapshot,
    ac_battery_storage_snapshot,
    ac_battery_soc_option_label,
    ac_battery_type_available,
)


def test_ac_battery_soc_option_label_resolves_known_band() -> None:
    class BadInt:
        def __int__(self) -> int:
            raise ValueError("bad-int")

    assert AC_BATTERY_SOC_OPTIONS[0] == (0, "0-5%")
    assert ac_battery_soc_option_label(25) == "25-30%"
    assert ac_battery_soc_option_label(None) is None
    assert ac_battery_soc_option_label(101) is None
    assert ac_battery_soc_option_label(BadInt()) is None


def test_ac_battery_availability_helpers_require_capability_and_type() -> None:
    coord = SimpleNamespace(
        battery_has_acb=True,
        battery_write_access_confirmed=None,
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "ac_battery"
        ),
    )

    assert ac_battery_entities_available(coord) is True
    assert ac_battery_control_available(coord) is True

    coord.battery_user_is_owner = False
    coord.battery_user_is_installer = False
    assert ac_battery_entities_available(coord) is True
    assert ac_battery_control_available(coord) is False


def test_ac_battery_device_info_uses_inventory_when_available() -> None:
    marker = object()
    coord = SimpleNamespace(
        site_id="site",
        inventory_view=SimpleNamespace(type_device_info=lambda _type_key: marker),
    )

    assert ac_battery_device_info(coord) is marker


def test_ac_battery_device_info_falls_back_to_shared_type_device() -> None:
    coord = SimpleNamespace(
        site_id="site",
        inventory_view=SimpleNamespace(type_device_info=lambda _type_key: None),
    )

    info = ac_battery_device_info(coord)

    assert info["manufacturer"] == "Enphase"
    assert info["name"] == "AC Battery"
    assert ("enphase_ev", "type:site:ac_battery") in info["identifiers"]


def test_ac_battery_support_defensive_helpers() -> None:
    coord = SimpleNamespace(
        inventory_view=SimpleNamespace(),
        ac_battery_storage=lambda _serial: "bad",
        iter_ac_battery_serials=lambda: ["BAT-1"],
    )

    assert ac_battery_entities_available(coord) is False
    assert ac_battery_control_available(coord) is False
    assert ac_battery_type_available(object()) is False
    assert (
        ac_battery_storage_snapshot(SimpleNamespace(ac_battery_storage=None), "BAT-1")
        is None
    )
    assert (
        ac_battery_device_info(
            SimpleNamespace(
                site_id="site",
                inventory_view=SimpleNamespace(type_device_info="bad"),
            )
        )["name"]
        == "AC Battery"
    )
    assert ac_battery_last_reported_snapshot(coord)["latest_reported_device"] is None


def test_ac_battery_last_reported_snapshot_summarizes_members() -> None:
    coord = SimpleNamespace(
        iter_ac_battery_serials=lambda: ["BAT-1", "BAT-2"],
        ac_battery_storage=lambda serial: (
            {
                "serial_number": "BAT-1",
                "status_text": "Warning",
                "sleep_state": "pending",
                "last_reported": datetime(2026, 4, 9, 3, 15, tzinfo=timezone.utc),
            }
            if serial == "BAT-1"
            else {"serial_number": "BAT-2"}
        ),
    )

    snapshot = ac_battery_last_reported_snapshot(coord)

    assert snapshot["total_batteries"] == 2
    assert snapshot["without_last_report_count"] == 1
    assert snapshot["latest_reported_utc"] == "2026-04-09T03:15:00+00:00"
    assert snapshot["latest_reported_device"] == {
        "serial_number": "BAT-1",
        "status": "Warning",
        "sleep_state": "pending",
    }


def test_ac_battery_last_reported_snapshot_prefers_identity_and_status_fallback() -> (
    None
):
    coord = SimpleNamespace(
        iter_ac_battery_serials=lambda: ["BAT-1", "BAT-2"],
        ac_battery_storage=lambda serial: (
            {
                "identity": "ID-1",
                "status_normalized": "normal",
                "battery_id": "BATTERY-ID",
                "last_reported": datetime(2026, 4, 9, 3, 15, tzinfo=timezone.utc),
            }
            if serial == "BAT-1"
            else None
        ),
    )

    snapshot = ac_battery_last_reported_snapshot(coord)

    assert snapshot["without_last_report_count"] == 1
    assert snapshot["latest_reported_device"] == {
        "serial_number": "ID-1",
        "status": "normal",
        "sleep_state": None,
    }
