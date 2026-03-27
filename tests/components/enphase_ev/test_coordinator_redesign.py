from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.exceptions import ServiceValidationError

from custom_components.enphase_ev.refresh_plan import (
    FOLLOWUP_STAGE,
    FOLLOWUP_PLAN,
    bind_refresh_stage,
    bind_refresh_plan,
    post_session_followup_plan,
    warmup_plan,
)
from custom_components.enphase_ev.state_models import (
    RefreshHealthState,
    StateBackedAttribute,
    install_state_descriptors,
)


def test_coordinator_state_models_proxy_runtime_attributes(coordinator_factory) -> None:
    coord = coordinator_factory()

    coord._network_errors = 3  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._devices_inventory_cache_until = 42.0  # noqa: SLF001
    coord._hems_devices_last_success_utc = datetime.now(UTC)  # noqa: SLF001
    coord._charge_mode_cache = {"ABC": ("GREEN_CHARGING", 1.0)}  # noqa: SLF001

    assert coord._network_errors == 3  # noqa: SLF001
    assert coord.refresh_state._network_errors == 3
    assert "_network_errors" not in coord.__dict__

    assert coord._battery_profile == "cost_savings"  # noqa: SLF001
    assert coord.battery_state._battery_profile == "cost_savings"
    assert "_battery_profile" not in coord.__dict__

    assert coord._devices_inventory_cache_until == 42.0  # noqa: SLF001
    assert coord.inventory_state._devices_inventory_cache_until == 42.0

    assert (
        coord._hems_devices_last_success_utc
        == coord.heatpump_state._hems_devices_last_success_utc
    )  # noqa: SLF001
    assert (
        coord._charge_mode_cache == coord.evse_state._charge_mode_cache
    )  # noqa: SLF001
    assert coord.coerce_optional_int("7") == 7


def test_state_backed_attribute_falls_back_to_instance_dict() -> None:
    class _Owner:
        direct = StateBackedAttribute("refresh_state", "_network_errors")

        def __init__(self) -> None:
            self.__dict__["_network_errors"] = 7

    owner = _Owner()

    assert owner.direct == 7

    owner.direct = 11

    assert owner.direct == 11
    assert owner.__dict__["_network_errors"] == 11
    assert isinstance(_Owner.direct, StateBackedAttribute)


def test_state_backed_attribute_uses_runtime_state_when_available() -> None:
    class _Owner:
        direct = StateBackedAttribute("refresh_state", "_network_errors")

        def __init__(self) -> None:
            self.refresh_state = RefreshHealthState()

    owner = _Owner()

    owner.direct = 5

    assert owner.direct == 5
    assert owner.refresh_state._network_errors == 5
    assert "_network_errors" not in owner.__dict__


def test_state_backed_attribute_raises_for_missing_value() -> None:
    class _Owner:
        direct = StateBackedAttribute("refresh_state", "_network_errors")

    with pytest.raises(AttributeError, match="_network_errors"):
        _ = _Owner().direct


def test_install_state_descriptors_preserves_existing_attributes() -> None:
    class _Owner:
        _network_errors = "keep"
        existing = "keep"

    install_state_descriptors(_Owner)

    assert _Owner.existing == "keep"
    assert _Owner._network_errors == "keep"


def test_coordinator_payload_health_state_delegates_to_diagnostics(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    state = coord._payload_health_state("devices_inventory")  # noqa: SLF001

    assert state is coord.diagnostics.payload_health_state("devices_inventory")
    assert state["available"] is True


def test_coordinator_issue_context_delegates_to_diagnostics(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    metrics, placeholders = coord._issue_context()  # noqa: SLF001

    assert (metrics, placeholders) == coord.diagnostics.issue_context()


def test_coordinator_missing_battery_runtime_raises_attribute_error() -> None:
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)

    with pytest.raises(AttributeError, match="battery_runtime"):
        _ = coord.battery_runtime


def test_coordinator_grid_validation_and_storm_guard_pending_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    with pytest.raises(ServiceValidationError):
        coord._raise_grid_validation("grid_control_unavailable")  # noqa: SLF001

    coord._storm_guard_pending_state = "enabled"  # noqa: SLF001
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._storm_guard_pending_expires_mono = 999.0  # noqa: SLF001
    assert coord.storm_guard_update_pending is False
    assert coord._storm_guard_pending_state is None  # noqa: SLF001

    coord._storm_guard_pending_state = "enabled"  # noqa: SLF001
    coord._storm_guard_state = "disabled"  # noqa: SLF001
    coord._storm_guard_pending_expires_mono = None  # noqa: SLF001
    assert coord.storm_guard_update_pending is False
    assert coord._storm_guard_pending_state is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_coordinator_battery_runtime_wrapper_delegation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = MagicMock()
    runtime.async_assert_grid_toggle_allowed = AsyncMock()
    runtime.clear_storm_guard_pending = MagicMock()
    runtime.set_storm_guard_pending = MagicMock()
    runtime.sync_storm_guard_pending = MagicMock()
    runtime.clear_battery_pending = MagicMock()
    runtime.set_battery_pending = MagicMock()
    runtime.assert_battery_profile_write_allowed = MagicMock()
    runtime.assert_battery_settings_write_allowed = MagicMock()
    runtime.battery_itc_disclaimer_value = MagicMock(return_value="itc")
    runtime.raise_schedule_update_validation_error = MagicMock()
    runtime.async_update_battery_schedule = AsyncMock()
    runtime.backup_history_tzinfo = MagicMock(return_value=UTC)
    runtime.parse_battery_backup_history_payload = MagicMock(
        return_value=[{"ok": True}]
    )
    runtime.storm_alert_is_active = MagicMock(return_value=True)
    coord.battery_runtime = runtime

    await coord._async_assert_grid_toggle_allowed()  # noqa: SLF001
    coord._clear_storm_guard_pending()  # noqa: SLF001
    coord._set_storm_guard_pending("enabled")  # noqa: SLF001
    coord._sync_storm_guard_pending("enabled")  # noqa: SLF001
    coord._clear_battery_pending()  # noqa: SLF001
    coord._set_battery_pending(  # noqa: SLF001
        profile="self-consumption",
        reserve=20,
        sub_type=None,
    )
    coord._assert_battery_profile_write_allowed()  # noqa: SLF001
    coord._assert_battery_settings_write_allowed()  # noqa: SLF001
    assert coord._battery_itc_disclaimer_value() == "itc"  # noqa: SLF001
    marker = object()
    coord._raise_schedule_update_validation_error(marker)  # noqa: SLF001
    await coord._async_update_battery_schedule(  # noqa: SLF001
        "schedule-id",
        start_time="00:00",
        end_time="01:00",
        limit=90,
        days=[1],
        timezone="UTC",
    )
    assert coord._backup_history_tzinfo() == UTC  # noqa: SLF001
    assert coord._parse_battery_backup_history_payload({}) == [  # noqa: SLF001
        {"ok": True}
    ]
    assert coord._storm_alert_is_active({}) is True  # noqa: SLF001

    runtime.async_assert_grid_toggle_allowed.assert_awaited_once_with()
    runtime.clear_storm_guard_pending.assert_called_once_with()
    runtime.set_storm_guard_pending.assert_called_once_with("enabled")
    runtime.sync_storm_guard_pending.assert_called_once_with("enabled")
    runtime.clear_battery_pending.assert_called_once_with()
    runtime.set_battery_pending.assert_called_once_with(
        profile="self-consumption",
        reserve=20,
        sub_type=None,
        require_exact_settings=True,
    )
    runtime.assert_battery_profile_write_allowed.assert_called_once_with()
    runtime.assert_battery_settings_write_allowed.assert_called_once_with()
    runtime.raise_schedule_update_validation_error.assert_called_once_with(marker)
    runtime.async_update_battery_schedule.assert_awaited_once_with(
        "schedule-id",
        start_time="00:00",
        end_time="01:00",
        limit=90,
        days=[1],
        timezone="UTC",
    )


class _RefreshOwner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.evse_timeseries = MagicMock()
        self.evse_timeseries.async_refresh.side_effect = (
            lambda *, day_local: self._record(f"evse_timeseries:{day_local}")
        )
        self.energy = MagicMock()
        self.energy._async_refresh_site_energy.side_effect = lambda: self._record(
            "site_energy"
        )

    def _record(self, value: str) -> str:
        self.calls.append(value)
        return value

    def _async_refresh_battery_site_settings(self) -> str:
        self.calls.append("battery_site_settings")
        return "site-settings"

    def _async_refresh_battery_backup_history(self) -> str:
        self.calls.append("battery_backup_history")
        return "backup-history"

    def _async_refresh_battery_settings(self) -> str:
        self.calls.append("battery_settings")
        return "settings"

    def _async_refresh_battery_schedules(self) -> str:
        self.calls.append("battery_schedules")
        return "schedules"

    def _async_refresh_storm_guard_profile(self) -> str:
        self.calls.append("storm_guard")
        return "storm-guard"

    def _async_refresh_storm_alert(self) -> str:
        self.calls.append("storm_alert")
        return "storm-alert"

    def _async_refresh_grid_control_check(self) -> str:
        self.calls.append("grid_control")
        return "grid-control"

    def _async_refresh_dry_contact_settings(self) -> str:
        self.calls.append("dry_contact")
        return "dry-contact"

    def _async_refresh_current_power_consumption(self) -> str:
        self.calls.append("current_power")
        return "current-power"

    def _async_refresh_battery_status(self) -> str:
        self.calls.append("battery_status")
        return "battery-status"

    def _async_refresh_devices_inventory(self) -> str:
        self.calls.append("devices_inventory")
        return "devices-inventory"

    def _async_refresh_hems_devices(self) -> str:
        self.calls.append("hems_devices")
        return "hems-devices"

    def _async_refresh_inverters(self) -> str:
        self.calls.append("inverters")
        return "inverters"

    def _async_refresh_heatpump_runtime_state(self) -> str:
        self.calls.append("heatpump_runtime")
        return "heatpump-runtime"

    def _async_refresh_heatpump_daily_consumption(self) -> str:
        self.calls.append("heatpump_daily")
        return "heatpump-daily"

    def _async_refresh_heatpump_power(self) -> str:
        self.calls.append("heatpump_power")
        return "heatpump-power"

    def _async_refresh_site_energy_for_warmup(self) -> str:
        self.calls.append("warmup_site_energy")
        return "warmup-site-energy"

    def _async_refresh_evse_timeseries_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]],
    ) -> str:
        self.calls.append(f"warmup_evse_timeseries:{sorted(working_data)}")
        return "warmup-evse-timeseries"

    def _async_refresh_session_state_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]],
    ) -> str:
        self.calls.append(f"warmup_sessions:{sorted(working_data)}")
        return "warmup-sessions"

    def _async_refresh_secondary_evse_state_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]],
    ) -> str:
        self.calls.append(f"warmup_secondary:{sorted(working_data)}")
        return "warmup-secondary"


def test_followup_refresh_stage_binds_zero_arg_calls() -> None:
    owner = _RefreshOwner()
    bound = bind_refresh_stage(owner, FOLLOWUP_STAGE)

    assert bound.defer_topology is True
    assert [call[0] for call in bound.parallel_calls] == [
        "battery_site_settings_s",
        "battery_backup_history_s",
        "battery_settings_s",
        "battery_schedules_s",
        "storm_guard_s",
        "storm_alert_s",
        "grid_control_check_s",
        "dry_contact_settings_s",
        "current_power_s",
    ]
    assert [call[0] for call in bound.ordered_calls] == [
        "battery_status_s",
        "devices_inventory_s",
        "hems_devices_s",
    ]

    assert bound.parallel_calls[0][2]() == "site-settings"
    assert bound.ordered_calls[-1][2]() == "hems-devices"
    assert owner.calls == ["battery_site_settings", "hems_devices"]


def test_refresh_plans_bind_dynamic_followup_and_warmup_calls() -> None:
    owner = _RefreshOwner()
    working_data = {"SERIAL-1": {"ok": True}}

    bound_warmup = bind_refresh_plan(owner, warmup_plan(working_data))

    assert [stage.stage_key for stage in bound_warmup.stages] == [
        "discovery",
        "state",
        None,
        "energy",
    ]
    assert bound_warmup.stages[2].ordered_calls[0][2]() == "heatpump-runtime"
    assert bound_warmup.stages[3].parallel_calls[0][2]() == "warmup-site-energy"
    assert bound_warmup.stages[3].parallel_calls[1][2]() == "warmup-evse-timeseries"
    assert bound_warmup.stages[3].parallel_calls[2][2]() == "warmup-sessions"
    assert bound_warmup.stages[3].parallel_calls[3][2]() == "warmup-secondary"

    bound_post = bind_refresh_plan(owner, post_session_followup_plan("today"))

    assert [stage.stage_key for stage in bound_post.stages] == [None]
    assert bound_post.stages[0].defer_topology is True
    assert bound_post.stages[0].parallel_calls[0][2]() == "evse_timeseries:today"
    assert bound_post.stages[0].parallel_calls[1][2]() == "site_energy"
    assert bound_post.stages[0].parallel_calls[2][2]() == "inverters"


@pytest.mark.asyncio
async def test_coordinator_refresh_plan_runner_executes_each_stage(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    seen: list[tuple[str | None, bool, int, int]] = []

    async def _run_stage(
        phase_timings: dict[str, float],
        *,
        parallel_calls,
        ordered_calls,
        stage_key=None,
        defer_topology=False,
    ) -> None:
        seen.append(
            (
                stage_key,
                defer_topology,
                len(parallel_calls),
                len(ordered_calls),
            )
        )

    coord._async_run_staged_refresh_calls = _run_stage  # type: ignore[method-assign]  # noqa: SLF001

    await coord._async_run_refresh_plan({}, plan=FOLLOWUP_PLAN)  # noqa: SLF001

    assert seen == [
        (None, True, 9, 3),
        (None, False, 0, 3),
    ]


@pytest.mark.asyncio
async def test_coordinator_run_refresh_calls_tracks_stage_and_topology_batch(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._begin_topology_refresh_batch = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._end_topology_refresh_batch = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._async_run_refresh_call = AsyncMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=(("first_s", 0.1), ("second_s", 0.2))
    )
    phase_timings: dict[str, float] = {}

    await coord._async_run_refresh_calls(  # noqa: SLF001
        phase_timings,
        calls=(
            ("first_s", "first", lambda: None),
            ("second_s", "second", lambda: None),
        ),
        stage_key="refresh",
        defer_topology=True,
    )

    coord._begin_topology_refresh_batch.assert_called_once_with()  # noqa: SLF001
    coord._end_topology_refresh_batch.assert_called_once_with()  # noqa: SLF001
    assert phase_timings["first_s"] == 0.1
    assert phase_timings["second_s"] == 0.2
    assert "refresh_s" in phase_timings
