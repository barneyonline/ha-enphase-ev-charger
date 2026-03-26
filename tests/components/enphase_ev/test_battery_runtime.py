from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.battery_runtime import BatteryRuntime
from custom_components.enphase_ev.const import SAVINGS_OPERATION_MODE_SUBTYPE


@pytest.fixture(autouse=True)
def _force_utc_timezone() -> None:
    dt_util.set_default_time_zone(UTC)


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


def test_battery_runtime_transition_helpers_cover_private_and_fallback_paths() -> None:
    private_sync = Mock()
    private_raise = Mock()
    coordinator = SimpleNamespace(
        _normalize_battery_sub_type=lambda value: f"private:{value}",
        _sync_battery_profile_pending_issue=private_sync,
        _coerce_int=lambda value, default=0: 41 if value == "41" else default,
        _coerce_optional_bool=lambda value: True if value == "yes" else None,
        _coerce_optional_text=lambda value: str(value).strip().upper(),
        _current_charge_from_grid_schedule_window=lambda: (11, 22),
        _raise_grid_validation=private_raise,
    )
    runtime = BatteryRuntime(coordinator)

    assert runtime._normalize_battery_sub_type("x") == "private:x"
    runtime._sync_battery_profile_pending_issue()
    private_sync.assert_called_once_with()
    assert runtime._coerce_int("41", default=-1) == 41
    assert runtime._coerce_optional_bool("yes") is True
    assert runtime._coerce_optional_text(" a ") == "A"
    assert runtime._current_schedule_window_from_coordinator() == (11, 22)

    runtime.raise_grid_validation("grid_control_unavailable")
    private_raise.assert_called_once_with(
        "grid_control_unavailable",
        placeholders=None,
        message=None,
    )


def test_battery_runtime_transition_helpers_cover_public_paths() -> None:
    public_sync = Mock()
    public_raise = Mock()
    coordinator = SimpleNamespace(
        normalize_battery_sub_type=lambda value: f"public:{value}",
        sync_battery_profile_pending_issue=public_sync,
        coerce_int=lambda value, default=0: 52 if value == "52" else default,
        coerce_optional_bool=lambda value: False if value == "no" else None,
        coerce_optional_text=lambda value: str(value).strip().lower(),
        current_charge_from_grid_schedule_window=lambda: (33, 44),
        raise_grid_validation=public_raise,
    )
    runtime = BatteryRuntime(coordinator)

    assert runtime._normalize_battery_sub_type("X") == "public:X"
    runtime._sync_battery_profile_pending_issue()
    public_sync.assert_called_once_with()
    assert runtime._coerce_int("52", default=-1) == 52
    assert runtime._coerce_optional_bool("no") is False
    assert runtime._coerce_optional_text(" A ") == "a"
    assert runtime._current_schedule_window_from_coordinator() == (33, 44)

    runtime.raise_grid_validation("grid_control_unavailable")
    public_raise.assert_called_once_with(
        "grid_control_unavailable",
        placeholders=None,
        message=None,
    )


def test_battery_runtime_transition_helpers_cover_plain_fallback_paths() -> None:
    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    runtime = BatteryRuntime(SimpleNamespace())

    assert runtime._normalize_battery_sub_type("x") is None
    runtime._sync_battery_profile_pending_issue()
    assert runtime._coerce_int("7", default=-1) == 7
    assert runtime._coerce_int(_BadStr(), default=-1) == -1
    assert runtime._coerce_optional_bool("x") is None
    assert runtime._coerce_optional_text(None) is None
    assert runtime._coerce_optional_text(_BadStr()) is None
    assert runtime._coerce_optional_text("   ") is None
    assert runtime._current_schedule_window_from_coordinator() == (120, 300)

    with pytest.raises(ServiceValidationError):
        runtime.raise_grid_validation("grid_control_unavailable")


def test_battery_runtime_current_schedule_window_plain_fallback_normalizes_values() -> (
    None
):
    runtime = BatteryRuntime(
        SimpleNamespace(
            _battery_charge_begin_time="45",
            _battery_charge_end_time="1800",
        )
    )

    assert runtime.current_charge_from_grid_schedule_window() == (45, 300)


def test_battery_runtime_current_schedule_window_plain_fallback_handles_invalid_value() -> (
    None
):
    runtime = BatteryRuntime(
        SimpleNamespace(
            _battery_charge_begin_time="invalid",
            _battery_charge_end_time="180",
        )
    )

    assert runtime.current_charge_from_grid_schedule_window() == (120, 180)


def test_battery_runtime_pending_subtype_falls_back_to_private_normalizer() -> None:
    coordinator = SimpleNamespace(_normalize_battery_sub_type=lambda value: value)

    assert (
        BatteryRuntime._normalize_pending_sub_type(
            coordinator,
            "cost_savings",
            SAVINGS_OPERATION_MODE_SUBTYPE,
        )
        == SAVINGS_OPERATION_MODE_SUBTYPE
    )
    assert (
        BatteryRuntime._normalize_pending_sub_type(
            SimpleNamespace(),
            "cost_savings",
            SAVINGS_OPERATION_MODE_SUBTYPE,
        )
        is None
    )


def test_battery_runtime_backup_history_parser_handles_invalid_entries() -> None:
    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    def _coerce_int(value, default=-1):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    coordinator = SimpleNamespace(
        _coerce_int=_coerce_int,
        _battery_timezone="Invalid/Zone",
        site_id="1234",
    )
    runtime = BatteryRuntime(coordinator)

    assert runtime.parse_battery_backup_history_payload(None) is None
    assert runtime.parse_battery_backup_history_payload({"histories": "bad"}) is None

    events = runtime.parse_battery_backup_history_payload(
        {
            "histories": [
                None,
                {"duration": "bad", "start_time": "2025-01-01T00:00:00"},
                {"duration": 30, "start_time": _BadStr()},
                {"duration": 30, "start_time": " "},
                {"duration": 30, "start_time": "2025-01-01T00:00:00"},
            ],
            "total_records": 9,
            "total_backup": 99,
        }
    )

    assert events is not None
    assert len(events) == 1
    assert events[0]["start"].tzinfo == UTC


def test_battery_runtime_backup_history_tzinfo_falls_back_to_utc(hass) -> None:
    del hass
    runtime = BatteryRuntime(SimpleNamespace(_battery_timezone=None))
    original_tz = dt_util.DEFAULT_TIME_ZONE
    dt_util.DEFAULT_TIME_ZONE = None
    try:
        assert runtime.backup_history_tzinfo() == UTC
    finally:
        dt_util.DEFAULT_TIME_ZONE = original_tz


def test_battery_runtime_storm_guard_pending_and_profile_branches(
    hass,
    monkeypatch,
) -> None:
    del hass
    coord = SimpleNamespace(
        _storm_guard_pending_state=None,
        _storm_guard_pending_expires_mono=None,
        _storm_guard_state=None,
        _coerce_optional_bool=lambda value: None if value is None else bool(value),
    )
    runtime = BatteryRuntime(coord)

    runtime.set_storm_guard_pending("enabled")
    coord._storm_guard_state = "disabled"  # noqa: SLF001
    coord._storm_guard_pending_expires_mono = None  # noqa: SLF001
    runtime.sync_storm_guard_pending()
    assert coord._storm_guard_pending_state is None  # noqa: SLF001

    runtime.set_storm_guard_pending("enabled")
    coord._storm_guard_state = "disabled"  # noqa: SLF001
    expires_at = coord._storm_guard_pending_expires_mono  # noqa: SLF001
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.time.monotonic",
        lambda: float(expires_at) + 1,
    )
    runtime.sync_storm_guard_pending()
    assert coord._storm_guard_pending_state is None  # noqa: SLF001

    assert runtime.parse_storm_guard_profile(None) == (None, None)
    assert runtime.parse_storm_guard_profile(
        {"stormGuardState": "enabled", "evseStormEnabled": 1}
    ) == ("enabled", True)


def test_battery_runtime_raise_grid_validation_uses_message() -> None:
    runtime = BatteryRuntime(SimpleNamespace())

    with pytest.raises(ServiceValidationError, match="custom message"):
        runtime.raise_grid_validation(
            "grid_control_unavailable",
            message="custom message",
        )


@pytest.mark.asyncio
async def test_battery_runtime_async_set_grid_connection_delegates_to_coordinator() -> (
    None
):
    coordinator = SimpleNamespace(async_set_grid_mode=AsyncMock())
    runtime = BatteryRuntime(coordinator)

    await runtime.async_set_grid_connection(True, otp="1234")

    coordinator.async_set_grid_mode.assert_awaited_once_with("on_grid", "1234")
