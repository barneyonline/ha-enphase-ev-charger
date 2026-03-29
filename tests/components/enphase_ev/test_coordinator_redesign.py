from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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


@pytest.mark.asyncio
async def test_coordinator_public_runtime_commands_delegate(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    battery_runtime = MagicMock()
    battery_runtime.async_set_charge_from_grid = AsyncMock()
    battery_runtime.async_refresh_grid_control_check = AsyncMock()
    battery_runtime.async_refresh_storm_guard_profile = AsyncMock()
    battery_runtime.async_refresh_storm_alert = AsyncMock()
    battery_runtime.async_opt_out_all_storm_alerts = AsyncMock()
    battery_runtime.async_set_grid_mode = AsyncMock()
    battery_runtime.async_set_grid_connection = AsyncMock()
    battery_runtime.async_set_storm_guard_enabled = AsyncMock()
    battery_runtime.async_set_storm_evse_enabled = AsyncMock()
    inventory_runtime = MagicMock()
    inventory_runtime.async_ensure_system_dashboard_diagnostics = AsyncMock()
    coord.battery_runtime = battery_runtime
    coord.inventory_runtime = inventory_runtime

    await coord.async_set_charge_from_grid(True)
    await coord.async_refresh_grid_control_check(force=True)
    await coord.async_refresh_storm_guard_profile(force=True)
    await coord.async_refresh_storm_alert(force=False)
    await coord.async_opt_out_all_storm_alerts()
    await coord.async_set_grid_mode("import_only", "123456")
    await coord.async_set_grid_connection(False, otp="123456")
    await coord.async_set_storm_guard_enabled(True)
    await coord.async_set_storm_evse_enabled(False)
    await coord.async_ensure_system_dashboard_diagnostics()

    battery_runtime.async_set_charge_from_grid.assert_awaited_once_with(True)
    battery_runtime.async_refresh_grid_control_check.assert_awaited_once_with(
        force=True
    )
    battery_runtime.async_refresh_storm_guard_profile.assert_awaited_once_with(
        force=True
    )
    battery_runtime.async_refresh_storm_alert.assert_awaited_once_with(force=False)
    battery_runtime.async_opt_out_all_storm_alerts.assert_awaited_once_with()
    battery_runtime.async_set_grid_mode.assert_awaited_once_with(
        "import_only",
        "123456",
    )
    battery_runtime.async_set_grid_connection.assert_awaited_once_with(
        False,
        otp="123456",
    )
    battery_runtime.async_set_storm_guard_enabled.assert_awaited_once_with(True)
    battery_runtime.async_set_storm_evse_enabled.assert_awaited_once_with(False)
    inventory_runtime.async_ensure_system_dashboard_diagnostics.assert_awaited_once_with()


def test_coordinator_public_runtime_properties_delegate(coordinator_factory) -> None:
    coord = coordinator_factory()
    sample_utc = datetime.now(UTC)
    start_utc = datetime.now(UTC)
    coord.heatpump_runtime = SimpleNamespace(
        heatpump_runtime_state={"device_uid": "HP-1"},
        heatpump_runtime_state_last_error="runtime boom",
        heatpump_daily_consumption={"daily_energy_wh": 10.0},
        heatpump_daily_consumption_last_error="daily boom",
        heatpump_power_w=550.0,
        heatpump_power_sample_utc=sample_utc,
        heatpump_power_start_utc=start_utc,
        heatpump_power_device_uid="HP-1",
        heatpump_power_source="hems_power_timeseries:HP-1",
        heatpump_power_last_error="power boom",
        heatpump_runtime_diagnostics=lambda: {
            "power_snapshot": {"outcome": "selected_sample"}
        },
    )
    coord.inventory_runtime = SimpleNamespace(
        inverter_diagnostics_payloads=lambda: {"summary_counts": {"total": 1}},
        system_dashboard_diagnostics=lambda: {"hierarchy_summary": {"total_nodes": 1}},
    )

    assert coord.heatpump_runtime_state == {"device_uid": "HP-1"}
    assert coord.heatpump_runtime_state_last_error == "runtime boom"
    assert coord.heatpump_daily_consumption == {"daily_energy_wh": 10.0}
    assert coord.heatpump_daily_consumption_last_error == "daily boom"
    assert coord.heatpump_power_w == 550.0
    assert coord.heatpump_power_sample_utc == sample_utc
    assert coord.heatpump_power_start_utc == start_utc
    assert coord.heatpump_power_device_uid == "HP-1"
    assert coord.heatpump_power_source == "hems_power_timeseries:HP-1"
    assert coord.heatpump_power_last_error == "power boom"
    assert coord.heatpump_runtime_diagnostics()["power_snapshot"]["outcome"] == (
        "selected_sample"
    )
    assert coord.inverter_diagnostics_payloads()["summary_counts"]["total"] == 1
    assert coord.system_dashboard_diagnostics()["hierarchy_summary"]["total_nodes"] == 1


@pytest.mark.asyncio
async def test_coordinator_diagnostics_and_gateway_summary_delegation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.inventory_runtime = SimpleNamespace(
        _gateway_inventory_summary_marker=lambda: "marker",
        _build_gateway_inventory_summary=lambda: {"gateway": 1},
    )
    coord._battery_status_payload = {"status": "cached"}  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001

    await coord.async_ensure_battery_status_diagnostics()
    coord._async_refresh_battery_status.assert_not_awaited()  # noqa: SLF001

    coord._battery_status_payload = None  # noqa: SLF001
    await coord.async_ensure_battery_status_diagnostics()
    coord._async_refresh_battery_status.assert_awaited_once_with(
        force=True
    )  # noqa: SLF001

    assert coord.gateway_inventory_summary() == {"gateway": 1}
    assert coord.gateway_inventory_summary() == {"gateway": 1}


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
