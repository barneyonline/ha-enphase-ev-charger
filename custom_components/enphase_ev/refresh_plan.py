from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

CallbackFactory = Callable[[Any], object]
BoundRefreshCall = tuple[str, str, Callable[[], object]]


@dataclass(frozen=True, slots=True)
class RefreshTask:
    timing_key: str
    log_label: str
    callback_factory: CallbackFactory


@dataclass(frozen=True, slots=True)
class RefreshStage:
    parallel_tasks: tuple[RefreshTask, ...] = ()
    ordered_tasks: tuple[RefreshTask, ...] = ()
    stage_key: str | None = None
    defer_topology: bool = False


@dataclass(frozen=True, slots=True)
class BoundRefreshStage:
    parallel_calls: tuple[BoundRefreshCall, ...] = ()
    ordered_calls: tuple[BoundRefreshCall, ...] = ()
    stage_key: str | None = None
    defer_topology: bool = False


@dataclass(frozen=True, slots=True)
class RefreshPlan:
    stages: tuple[RefreshStage, ...] = ()


@dataclass(frozen=True, slots=True)
class BoundRefreshPlan:
    stages: tuple[BoundRefreshStage, ...] = ()


def method_task(
    timing_key: str,
    log_label: str,
    method_name: str,
    /,
    **kwargs: object,
) -> RefreshTask:
    return RefreshTask(
        timing_key=timing_key,
        log_label=log_label,
        callback_factory=lambda owner: getattr(owner, method_name)(**kwargs),
    )


def callback_task(
    timing_key: str,
    log_label: str,
    callback_factory: CallbackFactory,
) -> RefreshTask:
    return RefreshTask(
        timing_key=timing_key,
        log_label=log_label,
        callback_factory=callback_factory,
    )


def bind_refresh_tasks(
    owner: object, tasks: tuple[RefreshTask, ...]
) -> tuple[BoundRefreshCall, ...]:
    return tuple(
        (
            task.timing_key,
            task.log_label,
            partial(task.callback_factory, owner),
        )
        for task in tasks
    )


def bind_refresh_stage(owner: object, stage: RefreshStage) -> BoundRefreshStage:
    return BoundRefreshStage(
        parallel_calls=bind_refresh_tasks(owner, stage.parallel_tasks),
        ordered_calls=bind_refresh_tasks(owner, stage.ordered_tasks),
        stage_key=stage.stage_key,
        defer_topology=stage.defer_topology,
    )


def bind_refresh_plan(owner: object, plan: RefreshPlan) -> BoundRefreshPlan:
    return BoundRefreshPlan(
        stages=tuple(bind_refresh_stage(owner, stage) for stage in plan.stages)
    )


WARMUP_DISCOVERY_STAGE = RefreshStage(
    stage_key="discovery",
    defer_topology=True,
    parallel_tasks=(
        method_task(
            "battery_site_settings_s",
            "battery site settings",
            "_async_refresh_battery_site_settings",
        ),
    ),
    ordered_tasks=(
        method_task(
            "battery_status_s", "battery status", "_async_refresh_battery_status"
        ),
        method_task(
            "devices_inventory_s",
            "device inventory",
            "_async_refresh_devices_inventory",
        ),
        method_task("hems_devices_s", "HEMS inventory", "_async_refresh_hems_devices"),
        method_task("inverters_s", "inverters", "_async_refresh_inverters"),
    ),
)

WARMUP_STATE_STAGE = RefreshStage(
    stage_key="state",
    parallel_tasks=(
        method_task(
            "battery_backup_history_s",
            "battery backup history",
            "_async_refresh_battery_backup_history",
        ),
        method_task(
            "battery_settings_s", "battery settings", "_async_refresh_battery_settings"
        ),
        method_task(
            "battery_schedules_s",
            "battery schedules",
            "_async_refresh_battery_schedules",
        ),
        method_task(
            "storm_guard_s", "storm guard", "_async_refresh_storm_guard_profile"
        ),
        method_task("storm_alert_s", "storm alert", "_async_refresh_storm_alert"),
        method_task(
            "grid_control_check_s",
            "grid control",
            "_async_refresh_grid_control_check",
        ),
        method_task(
            "dry_contact_settings_s",
            "dry contact settings",
            "_async_refresh_dry_contact_settings",
        ),
        method_task(
            "evse_feature_flags_s",
            "EVSE feature flags",
            "_async_refresh_evse_feature_flags",
        ),
        method_task(
            "current_power_s",
            "current power consumption",
            "_async_refresh_current_power_consumption",
        ),
    ),
)

SITE_ONLY_FOLLOWUP_STAGE = RefreshStage(
    defer_topology=True,
    parallel_tasks=(
        method_task(
            "battery_site_settings_s",
            "battery site settings",
            "_async_refresh_battery_site_settings",
        ),
        method_task(
            "battery_backup_history_s",
            "battery backup history",
            "_async_refresh_battery_backup_history",
        ),
        method_task(
            "battery_settings_s", "battery settings", "_async_refresh_battery_settings"
        ),
        method_task(
            "battery_schedules_s",
            "battery schedules",
            "_async_refresh_battery_schedules",
        ),
        method_task(
            "storm_guard_s", "storm guard", "_async_refresh_storm_guard_profile"
        ),
        method_task("storm_alert_s", "storm alert", "_async_refresh_storm_alert"),
        method_task(
            "grid_control_check_s",
            "grid control",
            "_async_refresh_grid_control_check",
        ),
        method_task(
            "dry_contact_settings_s",
            "dry contact settings",
            "_async_refresh_dry_contact_settings",
        ),
        method_task(
            "current_power_s",
            "current power consumption",
            "_async_refresh_current_power_consumption",
        ),
    ),
    ordered_tasks=(
        method_task(
            "battery_status_s", "battery status", "_async_refresh_battery_status"
        ),
        method_task(
            "devices_inventory_s",
            "device inventory",
            "_async_refresh_devices_inventory",
        ),
        method_task("hems_devices_s", "HEMS inventory", "_async_refresh_hems_devices"),
        method_task("inverters_s", "inverters", "_async_refresh_inverters"),
    ),
)

FOLLOWUP_STAGE = RefreshStage(
    defer_topology=True,
    parallel_tasks=SITE_ONLY_FOLLOWUP_STAGE.parallel_tasks,
    ordered_tasks=(
        method_task(
            "battery_status_s", "battery status", "_async_refresh_battery_status"
        ),
        method_task(
            "devices_inventory_s",
            "device inventory",
            "_async_refresh_devices_inventory",
        ),
        method_task("hems_devices_s", "HEMS inventory", "_async_refresh_hems_devices"),
    ),
)


HEATPUMP_FOLLOWUP_STAGE = RefreshStage(
    ordered_tasks=(
        method_task(
            "heatpump_runtime_s",
            "heat pump runtime",
            "_async_refresh_heatpump_runtime_state",
        ),
        method_task(
            "heatpump_daily_s",
            "heat pump daily-consumption",
            "_async_refresh_heatpump_daily_consumption",
        ),
        method_task(
            "heatpump_power_s",
            "heat pump power",
            "_async_refresh_heatpump_power",
        ),
    ),
)


HEATPUMP_FOLLOWUP_PLAN = RefreshPlan(stages=(HEATPUMP_FOLLOWUP_STAGE,))


SITE_ONLY_FOLLOWUP_PLAN = RefreshPlan(
    stages=(SITE_ONLY_FOLLOWUP_STAGE, HEATPUMP_FOLLOWUP_STAGE)
)


FOLLOWUP_PLAN = RefreshPlan(stages=(FOLLOWUP_STAGE, HEATPUMP_FOLLOWUP_STAGE))


def warmup_energy_stage(working_data: dict[str, dict]) -> RefreshStage:
    return RefreshStage(
        stage_key="energy",
        parallel_tasks=(
            method_task(
                "site_energy_s", "site energy", "_async_refresh_site_energy_for_warmup"
            ),
            callback_task(
                "evse_timeseries_s",
                "EVSE timeseries",
                lambda owner: owner._async_refresh_evse_timeseries_for_warmup(
                    working_data=working_data
                ),
            ),
            callback_task(
                "sessions_s",
                "session state",
                lambda owner: owner._async_refresh_session_state_for_warmup(
                    working_data=working_data
                ),
            ),
            callback_task(
                "secondary_evse_state_s",
                "secondary EVSE state",
                lambda owner: owner._async_refresh_secondary_evse_state_for_warmup(
                    working_data=working_data
                ),
            ),
        ),
    )


def warmup_plan(working_data: dict[str, dict]) -> RefreshPlan:
    return RefreshPlan(
        stages=(
            WARMUP_DISCOVERY_STAGE,
            WARMUP_STATE_STAGE,
            HEATPUMP_FOLLOWUP_STAGE,
            warmup_energy_stage(working_data),
        )
    )


def post_session_followup_stage(day_local_default: object) -> RefreshStage:
    return RefreshStage(
        defer_topology=True,
        parallel_tasks=(
            callback_task(
                "evse_timeseries_s",
                "EVSE timeseries",
                lambda owner: owner.evse_timeseries.async_refresh(
                    day_local=day_local_default
                ),
            ),
            callback_task(
                "site_energy_s",
                "site energy",
                lambda owner: owner.energy._async_refresh_site_energy(),
            ),
            method_task("inverters_s", "inverters", "_async_refresh_inverters"),
        ),
    )


def post_session_followup_plan(day_local_default: object) -> RefreshPlan:
    return RefreshPlan(stages=(post_session_followup_stage(day_local_default),))
