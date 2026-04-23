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


def object_method_task(
    timing_key: str,
    log_label: str,
    object_name: str,
    method_name: str,
    /,
    **kwargs: object,
) -> RefreshTask:
    return RefreshTask(
        timing_key=timing_key,
        log_label=log_label,
        callback_factory=lambda owner: getattr(
            getattr(owner, object_name), method_name
        )(**kwargs),
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
        object_method_task(
            "battery_status_s",
            "battery status",
            "battery_runtime",
            "async_refresh_battery_status",
        ),
        object_method_task(
            "ac_battery_devices_s",
            "AC Battery devices",
            "battery_runtime",
            "async_refresh_ac_battery_devices",
        ),
        object_method_task(
            "devices_inventory_s",
            "device inventory",
            "inventory_runtime",
            "_async_refresh_devices_inventory",
        ),
        object_method_task(
            "hems_devices_s",
            "HEMS inventory",
            "inventory_runtime",
            "_async_refresh_hems_devices",
        ),
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
        object_method_task(
            "ac_battery_telemetry_s",
            "AC Battery telemetry",
            "battery_runtime",
            "async_refresh_ac_battery_telemetry",
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
        object_method_task(
            "grid_control_check_s",
            "grid control",
            "battery_runtime",
            "async_refresh_grid_control_check",
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
        object_method_task(
            "grid_control_check_s",
            "grid control",
            "battery_runtime",
            "async_refresh_grid_control_check",
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
        object_method_task(
            "battery_status_s",
            "battery status",
            "battery_runtime",
            "async_refresh_battery_status",
        ),
        object_method_task(
            "ac_battery_devices_s",
            "AC Battery devices",
            "battery_runtime",
            "async_refresh_ac_battery_devices",
        ),
        object_method_task(
            "devices_inventory_s",
            "device inventory",
            "inventory_runtime",
            "_async_refresh_devices_inventory",
        ),
        object_method_task(
            "hems_devices_s",
            "HEMS inventory",
            "inventory_runtime",
            "_async_refresh_hems_devices",
        ),
        method_task("inverters_s", "inverters", "_async_refresh_inverters"),
    ),
)

FOLLOWUP_STAGE = RefreshStage(
    defer_topology=True,
    parallel_tasks=SITE_ONLY_FOLLOWUP_STAGE.parallel_tasks,
    ordered_tasks=(
        object_method_task(
            "battery_status_s",
            "battery status",
            "battery_runtime",
            "async_refresh_battery_status",
        ),
        object_method_task(
            "ac_battery_devices_s",
            "AC Battery devices",
            "battery_runtime",
            "async_refresh_ac_battery_devices",
        ),
        object_method_task(
            "devices_inventory_s",
            "device inventory",
            "inventory_runtime",
            "_async_refresh_devices_inventory",
        ),
        object_method_task(
            "hems_devices_s",
            "HEMS inventory",
            "inventory_runtime",
            "_async_refresh_hems_devices",
        ),
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


FOLLOWUP_PLAN = RefreshPlan(stages=(FOLLOWUP_STAGE,))


def warmup_energy_stage(working_data: dict[str, dict]) -> RefreshStage:
    return RefreshStage(
        stage_key="energy",
        parallel_tasks=(
            object_method_task(
                "site_energy_s",
                "site energy",
                "refresh_runner",
                "async_refresh_site_energy_for_warmup",
            ),
            callback_task(
                "evse_timeseries_s",
                "EVSE timeseries",
                lambda owner: owner.refresh_runner.async_refresh_evse_timeseries_for_warmup(
                    working_data=working_data
                ),
            ),
            callback_task(
                "sessions_s",
                "session state",
                lambda owner: owner.refresh_runner.async_refresh_session_state_for_warmup(
                    working_data=working_data
                ),
            ),
            callback_task(
                "secondary_evse_state_s",
                "secondary EVSE state",
                lambda owner: owner.refresh_runner.async_refresh_secondary_evse_state_for_warmup(
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


def _plan_from_stages(*stages: RefreshStage | None) -> RefreshPlan:
    filtered = tuple(
        stage
        for stage in stages
        if stage is not None and (stage.parallel_tasks or stage.ordered_tasks)
    )
    return RefreshPlan(stages=filtered)


def _refresh_due(
    target: object,
    predicate_name: str,
    *,
    fallback_due: Callable[[], bool] | None = None,
    **kwargs: object,
) -> bool:
    predicate = getattr(target, predicate_name, None)
    if callable(predicate):
        return bool(predicate(**kwargs))
    if fallback_due is None:
        return False
    return bool(fallback_due())


def _heatpump_power_covers_dependency_refreshes(runtime: object) -> bool:
    has_type = getattr(runtime, "has_type", None)
    if callable(has_type):
        try:
            if not bool(has_type("heatpump")):
                return False
        except Exception:
            return False
    client = getattr(runtime, "client", None)
    if getattr(client, "hems_site_supported", None) is not True:
        return False
    return True


def build_heatpump_followup_plan(
    owner: object, *, force_full: bool = False
) -> RefreshPlan:
    if force_full:
        return HEATPUMP_FOLLOWUP_PLAN
    runtime = getattr(owner, "heatpump_runtime")
    ordered: list[RefreshTask] = []
    power_due = runtime.heatpump_power_refresh_due()
    runtime_due = runtime.heatpump_runtime_state_refresh_due()
    daily_due = runtime.heatpump_daily_consumption_refresh_due()
    if power_due and _heatpump_power_covers_dependency_refreshes(runtime):
        ordered.append(
            method_task(
                "heatpump_power_s",
                "heat pump power",
                "_async_refresh_heatpump_power",
            )
        )
    else:
        if runtime_due:
            ordered.append(
                method_task(
                    "heatpump_runtime_s",
                    "heat pump runtime",
                    "_async_refresh_heatpump_runtime_state",
                )
            )
        if daily_due:
            ordered.append(
                method_task(
                    "heatpump_daily_s",
                    "heat pump daily-consumption",
                    "_async_refresh_heatpump_daily_consumption",
                )
            )
        if power_due:
            ordered.append(
                method_task(
                    "heatpump_power_s",
                    "heat pump power",
                    "_async_refresh_heatpump_power",
                )
            )
    return _plan_from_stages(RefreshStage(ordered_tasks=tuple(ordered)))


def build_followup_plan(owner: object, *, force_full: bool = False) -> RefreshPlan:
    if force_full:
        return FOLLOWUP_PLAN
    battery = getattr(owner, "battery_runtime")
    inventory = getattr(owner, "inventory_runtime")
    current_power = getattr(owner, "current_power_runtime")
    evse_feature_flags = getattr(owner, "evse_feature_flags_runtime")
    parallel: list[RefreshTask] = []
    ordered: list[RefreshTask] = []
    if battery.battery_site_settings_refresh_due():
        parallel.append(
            method_task(
                "battery_site_settings_s",
                "battery site settings",
                "_async_refresh_battery_site_settings",
            )
        )
    if battery.battery_backup_history_refresh_due():
        parallel.append(
            method_task(
                "battery_backup_history_s",
                "battery backup history",
                "_async_refresh_battery_backup_history",
            )
        )
    if battery.battery_settings_refresh_due():
        parallel.append(
            method_task(
                "battery_settings_s",
                "battery settings",
                "_async_refresh_battery_settings",
            )
        )
    if battery.battery_schedules_refresh_due():
        parallel.append(
            method_task(
                "battery_schedules_s",
                "battery schedules",
                "_async_refresh_battery_schedules",
            )
        )
    if battery.storm_guard_refresh_due():
        parallel.append(
            method_task(
                "storm_guard_s",
                "storm guard",
                "_async_refresh_storm_guard_profile",
            )
        )
    if battery.storm_alert_refresh_due():
        parallel.append(
            method_task(
                "storm_alert_s",
                "storm alert",
                "_async_refresh_storm_alert",
            )
        )
    if battery.grid_control_check_refresh_due():
        parallel.append(
            object_method_task(
                "grid_control_check_s",
                "grid control",
                "battery_runtime",
                "async_refresh_grid_control_check",
            )
        )
    if battery.dry_contact_settings_refresh_due():
        parallel.append(
            method_task(
                "dry_contact_settings_s",
                "dry contact settings",
                "_async_refresh_dry_contact_settings",
            )
        )
    if current_power.refresh_due():
        parallel.append(
            method_task(
                "current_power_s",
                "current power consumption",
                "_async_refresh_current_power_consumption",
            )
        )
    if evse_feature_flags.refresh_due():
        parallel.append(
            method_task(
                "evse_feature_flags_s",
                "EVSE feature flags",
                "_async_refresh_evse_feature_flags",
            )
        )
    if battery.battery_status_refresh_due():
        ordered.append(
            object_method_task(
                "battery_status_s",
                "battery status",
                "battery_runtime",
                "async_refresh_battery_status",
            )
        )
    if battery.ac_battery_devices_refresh_due():
        ordered.append(
            object_method_task(
                "ac_battery_devices_s",
                "AC Battery devices",
                "battery_runtime",
                "async_refresh_ac_battery_devices",
            )
        )
    if inventory.devices_inventory_refresh_due():
        ordered.append(
            object_method_task(
                "devices_inventory_s",
                "device inventory",
                "inventory_runtime",
                "_async_refresh_devices_inventory",
            )
        )
    if inventory.hems_devices_refresh_due():
        ordered.append(
            object_method_task(
                "hems_devices_s",
                "HEMS inventory",
                "inventory_runtime",
                "_async_refresh_hems_devices",
            )
        )
    return _plan_from_stages(
        RefreshStage(
            defer_topology=True,
            parallel_tasks=tuple(parallel),
            ordered_tasks=tuple(ordered),
        )
    )


def build_site_only_followup_plan(
    owner: object, *, force_full: bool = False
) -> RefreshPlan:
    if force_full:
        return SITE_ONLY_FOLLOWUP_PLAN
    normal = build_followup_plan(owner, force_full=False)
    stages: tuple[RefreshStage, ...] = normal.stages
    inventory = getattr(owner, "inventory_runtime")
    if inventory.inverters_refresh_due():
        if stages:
            base_stage = stages[0]
            stages = (
                RefreshStage(
                    defer_topology=base_stage.defer_topology,
                    stage_key=base_stage.stage_key,
                    parallel_tasks=base_stage.parallel_tasks,
                    ordered_tasks=base_stage.ordered_tasks
                    + (
                        method_task(
                            "inverters_s",
                            "inverters",
                            "_async_refresh_inverters",
                        ),
                    ),
                ),
            )
        else:
            stages = (
                RefreshStage(
                    defer_topology=True,
                    ordered_tasks=(
                        method_task(
                            "inverters_s",
                            "inverters",
                            "_async_refresh_inverters",
                        ),
                    ),
                ),
            )
    heatpump = build_heatpump_followup_plan(owner, force_full=False)
    return _plan_from_stages(*(stages + heatpump.stages))


def build_post_session_followup_plan(
    owner: object,
    day_local_default: object,
    *,
    force_full: bool = False,
) -> RefreshPlan:
    if force_full:
        return post_session_followup_plan(day_local_default)
    parallel: list[RefreshTask] = []
    evse_timeseries = getattr(owner, "evse_timeseries")
    if _refresh_due(
        evse_timeseries,
        "refresh_due",
        day_local=day_local_default,
        fallback_due=lambda: callable(getattr(evse_timeseries, "async_refresh", None)),
    ):
        parallel.append(
            callback_task(
                "evse_timeseries_s",
                "EVSE timeseries",
                lambda inner_owner: inner_owner.evse_timeseries.async_refresh(
                    day_local=day_local_default
                ),
            )
        )
    energy = getattr(owner, "energy")
    if _refresh_due(
        energy,
        "site_energy_refresh_due",
        fallback_due=lambda: callable(
            getattr(energy, "_async_refresh_site_energy", None)
        ),
    ):
        parallel.append(
            callback_task(
                "site_energy_s",
                "site energy",
                lambda inner_owner: inner_owner.energy._async_refresh_site_energy(),
            )
        )
    inventory = getattr(owner, "inventory_runtime")
    if _refresh_due(
        inventory,
        "inverters_refresh_due",
        fallback_due=lambda: callable(getattr(owner, "_async_refresh_inverters", None)),
    ):
        parallel.append(
            method_task("inverters_s", "inverters", "_async_refresh_inverters")
        )
    return _plan_from_stages(
        RefreshStage(
            defer_topology=True,
            parallel_tasks=tuple(parallel),
        )
    )
