from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.enphase_ev.battery_runtime import BatteryRuntime
from custom_components.enphase_ev.const import SAVINGS_OPERATION_MODE_SUBTYPE


def test_battery_runtime_normalizes_labels() -> None:
    runtime = BatteryRuntime(SimpleNamespace())

    assert runtime.normalize_battery_profile_key(" Cost_Savings ") == "cost_savings"
    assert runtime.normalize_battery_profile_key(None) is None
    assert runtime.battery_profile_label("backup_only") == "Full Backup"
    assert runtime.battery_profile_label("regional-profile") == "Regional Profile"
    assert runtime.battery_profile_label(None) is None


def test_battery_runtime_pending_helpers_and_matching(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)

    runtime.set_battery_pending(
        profile="cost_savings",
        reserve=25,
        sub_type=SAVINGS_OPERATION_MODE_SUBTYPE,
        require_exact_settings=False,
    )

    assert coord._battery_pending_profile == "cost_savings"  # noqa: SLF001
    assert coord._battery_pending_reserve == 25  # noqa: SLF001
    assert (
        coord._battery_pending_sub_type == SAVINGS_OPERATION_MODE_SUBTYPE
    )  # noqa: SLF001
    assert coord._battery_pending_requested_at is not None  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    assert runtime.effective_profile_matches_pending() is True

    runtime.clear_battery_pending()

    assert coord._battery_pending_profile is None  # noqa: SLF001
    assert coord._battery_pending_requested_at is None  # noqa: SLF001
    assert mock_issue_registry.created == []


def test_battery_runtime_matching_handles_exact_savings_subtype_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_require_exact_settings = True  # noqa: SLF001
    coord._battery_pending_reserve = 30  # noqa: SLF001
    coord._battery_backup_percentage = 30  # noqa: SLF001

    coord._battery_pending_sub_type = SAVINGS_OPERATION_MODE_SUBTYPE  # noqa: SLF001
    coord._battery_operation_mode_sub_type = (
        SAVINGS_OPERATION_MODE_SUBTYPE  # noqa: SLF001
    )
    assert runtime.effective_profile_matches_pending() is True

    coord._battery_operation_mode_sub_type = None  # noqa: SLF001
    assert runtime.effective_profile_matches_pending() is False

    coord._battery_pending_sub_type = None  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "other"  # noqa: SLF001
    assert runtime.effective_profile_matches_pending() is True

    coord._battery_operation_mode_sub_type = (
        SAVINGS_OPERATION_MODE_SUBTYPE  # noqa: SLF001
    )
    assert runtime.effective_profile_matches_pending() is False


def test_battery_runtime_target_reserve_and_current_savings_subtype(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    coord._battery_profile_reserve_memory = {"cost_savings": 35}  # noqa: SLF001
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_sub_type = SAVINGS_OPERATION_MODE_SUBTYPE  # noqa: SLF001

    assert runtime.target_reserve_for_profile("cost_savings") == 35
    assert runtime.target_reserve_for_profile("backup_only") == 100
    assert runtime.current_savings_sub_type() == SAVINGS_OPERATION_MODE_SUBTYPE

    runtime.remember_battery_reserve("self-consumption", 28)
    assert (
        coord._battery_profile_reserve_memory["self-consumption"] == 28
    )  # noqa: SLF001

    runtime.remember_battery_reserve("custom", 44)
    assert "custom" not in coord._battery_profile_reserve_memory


def test_battery_runtime_current_savings_subtype_uses_coordinator_property() -> None:
    coordinator = SimpleNamespace(battery_selected_operation_mode_sub_type="other")
    runtime = BatteryRuntime(coordinator)

    assert runtime.current_savings_sub_type() is None

    coordinator.battery_selected_operation_mode_sub_type = (
        SAVINGS_OPERATION_MODE_SUBTYPE
    )
    assert runtime.current_savings_sub_type() == SAVINGS_OPERATION_MODE_SUBTYPE


def test_battery_runtime_set_pending_records_current_time(monkeypatch) -> None:
    requested_at = datetime.now(UTC) + timedelta(minutes=5)
    coordinator = SimpleNamespace(
        _normalize_battery_sub_type=lambda value: value,
        _sync_battery_profile_pending_issue=lambda: None,
    )
    runtime = BatteryRuntime(coordinator)

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow",
        lambda: requested_at,
    )

    runtime.set_battery_pending(
        profile="self-consumption",
        reserve=15,
        sub_type="ignored",
    )

    assert coordinator._battery_pending_requested_at == requested_at
    assert coordinator._battery_pending_sub_type is None
