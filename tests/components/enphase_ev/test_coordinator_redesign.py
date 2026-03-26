from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.enphase_ev.refresh_plan import (
    FOLLOWUP_STAGE,
    bind_refresh_stage,
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


class _RefreshOwner:
    def __init__(self) -> None:
        self.calls: list[str] = []

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
