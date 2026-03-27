from __future__ import annotations

from datetime import UTC, datetime, time as dt_time, timezone
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
    runtime.async_apply_battery_settings = AsyncMock()
    runtime.async_refresh_battery_status = AsyncMock()
    runtime.parse_battery_status_payload = MagicMock()
    runtime.async_refresh_battery_settings = AsyncMock()
    runtime.parse_battery_settings_payload = MagicMock()
    runtime.async_refresh_battery_schedules = AsyncMock()
    runtime.parse_battery_schedules_payload = MagicMock()
    runtime.async_refresh_battery_site_settings = AsyncMock()
    runtime.async_refresh_grid_control_check = AsyncMock()
    runtime.parse_grid_control_check_payload = MagicMock()
    runtime.async_refresh_dry_contact_settings = AsyncMock()
    runtime.parse_dry_contact_settings_payload = MagicMock()
    runtime.async_set_charge_from_grid = AsyncMock()
    runtime.async_set_charge_from_grid_schedule_enabled = AsyncMock()
    runtime.async_set_charge_from_grid_schedule_time = AsyncMock()
    runtime.async_set_cfg_schedule_limit = AsyncMock()
    runtime.async_set_grid_mode = AsyncMock()
    runtime.async_set_grid_connection = AsyncMock()
    runtime.async_set_battery_shutdown_level = AsyncMock()
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
    await coord._async_apply_battery_settings({"chargeFromGrid": True})  # noqa: SLF001
    await coord._async_refresh_battery_status(force=True)  # noqa: SLF001
    coord._parse_battery_status_payload({"storages": []})  # noqa: SLF001
    coord.parse_battery_status_payload({"storages": []})
    await coord._async_refresh_battery_settings(force=True)  # noqa: SLF001
    coord._parse_battery_settings_payload(  # noqa: SLF001
        {"data": {}},
        clear_missing_schedule_times=True,
    )
    coord.parse_battery_settings_payload(
        {"data": {}},
        clear_missing_schedule_times=False,
    )
    await coord._async_refresh_battery_schedules()  # noqa: SLF001
    coord.parse_battery_schedules_payload({"cfg": {"details": []}})
    coord._parse_battery_schedules_payload({"cfg": {"details": []}})  # noqa: SLF001
    await coord._async_refresh_battery_site_settings(force=True)  # noqa: SLF001
    await coord._async_refresh_grid_control_check(force=True)  # noqa: SLF001
    coord.parse_grid_control_check_payload({"disableGridControl": False})
    coord._parse_grid_control_check_payload(
        {"disableGridControl": False}
    )  # noqa: SLF001
    await coord._async_refresh_dry_contact_settings(force=True)  # noqa: SLF001
    coord.parse_dry_contact_settings_payload({"data": {}})
    coord._parse_dry_contact_settings_payload({"data": {}})  # noqa: SLF001
    await coord.async_set_charge_from_grid(True)
    await coord.async_set_charge_from_grid_schedule_enabled(False)
    await coord.async_set_charge_from_grid_schedule_time(  # noqa: SLF001
        start=dt_time(1, 0),
        end=dt_time(2, 0),
    )
    await coord.async_set_cfg_schedule_limit(95)
    await coord.async_set_grid_mode("import_only", "123456")
    await coord.async_set_grid_connection(True, otp="123456")
    await coord.async_set_battery_shutdown_level(20)
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
    runtime.async_apply_battery_settings.assert_awaited_once_with(
        {"chargeFromGrid": True}
    )
    runtime.async_refresh_battery_status.assert_awaited_once_with(force=True)
    runtime.parse_battery_status_payload.assert_any_call({"storages": []})
    runtime.async_refresh_battery_settings.assert_awaited_once_with(force=True)
    runtime.parse_battery_settings_payload.assert_any_call(
        {"data": {}},
        clear_missing_schedule_times=True,
    )
    runtime.parse_battery_settings_payload.assert_any_call(
        {"data": {}},
        clear_missing_schedule_times=False,
    )
    runtime.async_refresh_battery_schedules.assert_awaited_once_with()
    runtime.parse_battery_schedules_payload.assert_any_call({"cfg": {"details": []}})
    runtime.async_refresh_battery_site_settings.assert_awaited_once_with(force=True)
    runtime.async_refresh_grid_control_check.assert_awaited_once_with(force=True)
    runtime.parse_grid_control_check_payload.assert_any_call(
        {"disableGridControl": False}
    )
    runtime.async_refresh_dry_contact_settings.assert_awaited_once_with(force=True)
    runtime.parse_dry_contact_settings_payload.assert_any_call({"data": {}})
    runtime.async_set_charge_from_grid.assert_awaited_once_with(True)
    runtime.async_set_charge_from_grid_schedule_enabled.assert_awaited_once_with(False)
    runtime.async_set_charge_from_grid_schedule_time.assert_awaited_once_with(
        start=dt_time(1, 0),
        end=dt_time(2, 0),
    )
    runtime.async_set_cfg_schedule_limit.assert_awaited_once_with(95)
    runtime.async_set_grid_mode.assert_awaited_once_with("import_only", "123456")
    runtime.async_set_grid_connection.assert_awaited_once_with(True, otp="123456")
    runtime.async_set_battery_shutdown_level.assert_awaited_once_with(20)


@pytest.mark.asyncio
async def test_coordinator_inventory_and_heatpump_wrapper_delegation(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.coordinator import (
        HeatpumpRuntime,
        InventoryRuntime,
    )

    coord = coordinator_factory()
    inventory = MagicMock()
    inventory._parse_devices_inventory_payload.return_value = (
        True,
        {"envoy": {}},
        ["envoy"],
    )
    inventory._set_type_device_buckets = MagicMock()
    inventory._hems_grouped_devices.return_value = [{"group": "gateway"}]
    inventory._extract_hems_group_members.return_value = (
        True,
        [{"device_uid": "HP-1"}],
    )
    inventory._hems_group_members.return_value = [{"device_uid": "HP-1"}]
    inventory._async_refresh_system_dashboard = AsyncMock()
    coord.inventory_runtime = inventory

    heatpump = MagicMock()
    heatpump._heatpump_primary_device_uid.return_value = "HP-1"
    heatpump._heatpump_runtime_device_uid.return_value = "HP-RUNTIME"
    heatpump._heatpump_daily_window.return_value = (
        "2026-03-27T00:00:00+00:00",
        "2026-03-28T00:00:00+00:00",
        "UTC",
        ("2026-03-27", "UTC"),
    )
    heatpump._build_heatpump_daily_consumption_snapshot.return_value = {
        "daily_energy_wh": 10.0
    }
    heatpump._heatpump_power_candidate_device_uids.return_value = ["HP-1", None]
    heatpump._heatpump_member_for_uid.return_value = {"device_uid": "HP-1"}
    heatpump._heatpump_member_alias_map.return_value = {"HP-1": "HP-1"}
    heatpump._heatpump_power_inventory_marker.return_value = ()
    heatpump._heatpump_power_fetch_plan.return_value = (["HP-1"], False, ())
    heatpump._heatpump_power_candidate_is_recommended.return_value = True
    heatpump._heatpump_power_candidate_type_rank.return_value = 3
    heatpump._heatpump_power_selection_key.return_value = (1, 1, 1, 1, 10.0, 1, 0)
    heatpump._async_refresh_hems_support_preflight = AsyncMock()
    heatpump.async_ensure_heatpump_runtime_diagnostics = AsyncMock()
    heatpump._async_refresh_heatpump_runtime_state = AsyncMock()
    heatpump._async_refresh_heatpump_daily_consumption = AsyncMock()
    heatpump._async_refresh_heatpump_power = AsyncMock()
    coord.heatpump_runtime = heatpump

    monkeypatch.setattr(
        InventoryRuntime,
        "_devices_inventory_buckets",
        staticmethod(lambda payload: [{"payload": payload}]),
    )
    monkeypatch.setattr(
        InventoryRuntime,
        "_hems_devices_groups",
        staticmethod(lambda payload: [{"groups": payload}]),
    )
    monkeypatch.setattr(
        InventoryRuntime,
        "_legacy_hems_devices_groups",
        staticmethod(lambda payload: [{"legacy": payload}]),
    )
    monkeypatch.setattr(
        InventoryRuntime,
        "_normalize_hems_member",
        staticmethod(lambda member: {"normalized": member}),
    )
    monkeypatch.setattr(
        InventoryRuntime,
        "_normalize_heatpump_member",
        staticmethod(lambda member: {"heatpump": member}),
    )
    monkeypatch.setattr(
        InventoryRuntime,
        "_hems_bucket_type",
        staticmethod(lambda raw_type: "bucket" if raw_type else None),
    )
    monkeypatch.setattr(
        HeatpumpRuntime,
        "_heatpump_latest_power_sample",
        staticmethod(lambda payload: (0, 123.0)),
    )
    monkeypatch.setattr(
        HeatpumpRuntime,
        "_infer_heatpump_interval_minutes",
        staticmethod(lambda start, count, now: 15),
    )
    monkeypatch.setattr(
        HeatpumpRuntime,
        "_heatpump_member_aliases",
        lambda member: ["HP-1", "HP-ALIAS"],
    )

    assert coord._parse_devices_inventory_payload({}) == (
        True,
        {"envoy": {}},
        ["envoy"],
    )  # noqa: SLF001
    coord._set_type_device_buckets({"envoy": {}}, ["envoy"])  # noqa: SLF001
    assert coord._devices_inventory_buckets({"result": []}) == [
        {"payload": {"result": []}}
    ]  # noqa: SLF001
    assert coord._hems_devices_groups({"result": []}) == [
        {"groups": {"result": []}}
    ]  # noqa: SLF001
    assert coord._legacy_hems_devices_groups({"result": []}) == [  # noqa: SLF001
        {"legacy": {"result": []}}
    ]
    assert coord._hems_grouped_devices() == [{"group": "gateway"}]  # noqa: SLF001
    assert coord._normalize_hems_member({"uid": "1"}) == {  # noqa: SLF001
        "normalized": {"uid": "1"}
    }
    assert coord._normalize_heatpump_member({"uid": "1"}) == {  # noqa: SLF001
        "heatpump": {"uid": "1"}
    }
    assert coord._extract_hems_group_members([], {"heat-pump"}) == (  # noqa: SLF001
        True,
        [{"device_uid": "HP-1"}],
    )
    assert coord._hems_group_members("heat-pump") == [
        {"device_uid": "HP-1"}
    ]  # noqa: SLF001
    assert coord._hems_bucket_type("gateway") == "bucket"  # noqa: SLF001
    await coord._async_refresh_system_dashboard(force=True)  # noqa: SLF001
    await coord._async_refresh_hems_support_preflight(force=True)  # noqa: SLF001
    await coord.async_ensure_heatpump_runtime_diagnostics(force=True)
    assert coord._heatpump_primary_device_uid() == "HP-1"  # noqa: SLF001
    assert coord._heatpump_runtime_device_uid() == "HP-RUNTIME"  # noqa: SLF001
    assert coord._heatpump_daily_window() == (  # noqa: SLF001
        "2026-03-27T00:00:00+00:00",
        "2026-03-28T00:00:00+00:00",
        "UTC",
        ("2026-03-27", "UTC"),
    )
    assert coord._build_heatpump_daily_consumption_snapshot({}) == {  # noqa: SLF001
        "daily_energy_wh": 10.0
    }
    assert coord._heatpump_power_candidate_device_uids() == [
        "HP-1",
        None,
    ]  # noqa: SLF001
    assert coord._heatpump_latest_power_sample({"x": 1}) == (0, 123.0)  # noqa: SLF001
    now_utc = datetime.now(timezone.utc)
    assert (
        coord._infer_heatpump_interval_minutes(now_utc, 1, now_utc) == 15
    )  # noqa: SLF001
    assert coord._heatpump_member_for_uid("HP-1") == {
        "device_uid": "HP-1"
    }  # noqa: SLF001
    assert coord._heatpump_member_aliases({"device_uid": "HP-1"}) == [  # noqa: SLF001
        "HP-1",
        "HP-ALIAS",
    ]
    assert coord._heatpump_member_alias_map() == {"HP-1": "HP-1"}  # noqa: SLF001
    assert coord._heatpump_power_inventory_marker() == ()  # noqa: SLF001
    assert coord._heatpump_power_fetch_plan() == (["HP-1"], False, ())  # noqa: SLF001
    assert (
        coord._heatpump_power_candidate_is_recommended("HP-1") is True
    )  # noqa: SLF001
    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {"device_uid": "HP-1"},
            "HP-1",
            is_recommended=True,
        )
        == 3
    )
    assert coord._heatpump_power_selection_key(  # noqa: SLF001
        {"device_uid": "HP-1"},
        requested_uid="HP-1",
        sample=(0, 123.0),
    ) == (1, 1, 1, 1, 10.0, 1, 0)
    await coord._async_refresh_heatpump_runtime_state(force=True)  # noqa: SLF001
    await coord._async_refresh_heatpump_daily_consumption(force=True)  # noqa: SLF001
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    inventory._set_type_device_buckets.assert_called_once_with(
        {"envoy": {}},
        ["envoy"],
        authoritative=True,
    )
    inventory._async_refresh_system_dashboard.assert_awaited_once_with(force=True)
    heatpump._async_refresh_hems_support_preflight.assert_awaited_once_with(force=True)
    heatpump.async_ensure_heatpump_runtime_diagnostics.assert_awaited_once_with(
        force=True
    )
    heatpump._async_refresh_heatpump_runtime_state.assert_awaited_once_with(force=True)
    heatpump._async_refresh_heatpump_daily_consumption.assert_awaited_once_with(
        force=True
    )
    heatpump._async_refresh_heatpump_power.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_coordinator_helper_edge_branches_and_current_power_refresh(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("bad")

    class BadInt:
        def __int__(self) -> int:
            raise ValueError("bad")

    assert coord._coerce_int(True) == 1  # noqa: SLF001
    assert coord._coerce_int(" 7 ") == 7  # noqa: SLF001
    assert coord._coerce_int("bad", default=9) == 9  # noqa: SLF001
    assert coord._normalize_iso_date(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_iso_date(" ") is None  # noqa: SLF001
    assert coord._normalize_iso_date("not-a-date") is None  # noqa: SLF001

    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": ["bad", {"curr_date_site": "2026-03-27"}]
    }
    assert coord._site_local_current_date() == "2026-03-27"  # noqa: SLF001

    coord._devices_inventory_payload = {
        "result": [{"curr_date_site": "bad"}]
    }  # noqa: SLF001
    coord._battery_timezone = "Bad/Zone"  # noqa: SLF001
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.dt_util.now",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert (
        coord._site_local_current_date() == datetime.now(tz=UTC).date().isoformat()
    )  # noqa: SLF001

    coord.client.latest_power = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_w is None

    coord.client.latest_power = AsyncMock(return_value=["bad"])  # type: ignore[list-item]
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_w is None

    coord.client.latest_power = AsyncMock(return_value={"value": object()})
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_w is None

    coord.client.latest_power = AsyncMock(return_value={"value": float("nan")})
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_w is None

    coord.client.latest_power = AsyncMock(
        return_value={
            "value": 752.0,
            "time": "bad",
            "units": BadStr(),
            "precision": BadInt(),
        }
    )
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_w == 752.0
    assert coord.current_power_consumption_sample_utc is None
    assert coord.current_power_consumption_reported_units is None
    assert coord.current_power_consumption_reported_precision is None
    assert coord.current_power_consumption_source == "app-api:get_latest_power"

    coord.client.latest_power = AsyncMock(
        return_value={
            "value": 800.0,
            "time": 1_700_000_000_000,
            "units": "W",
            "precision": 0,
        }
    )
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_sample_utc is not None

    coord._current_power_consumption_w = BadStr()  # type: ignore[assignment]  # noqa: SLF001
    assert coord.current_power_consumption_w is None
    coord._current_power_consumption_w = float("inf")  # noqa: SLF001
    assert coord.current_power_consumption_w is None
    coord._current_power_consumption_reported_units = BadStr()  # type: ignore[assignment]  # noqa: SLF001
    assert coord.current_power_consumption_reported_units is None
    coord._current_power_consumption_reported_precision = BadStr()  # type: ignore[assignment]  # noqa: SLF001
    assert coord.current_power_consumption_reported_precision is None
    coord._current_power_consumption_source = BadStr()  # type: ignore[assignment]  # noqa: SLF001
    assert coord.current_power_consumption_source is None

    assert coord.has_type("") is False
    coord._type_device_buckets = {"envoy": {"count": "bad"}}  # noqa: SLF001
    assert coord.has_type("envoy") is False
    assert coord.has_type_for_entities("") is False
    assert coord.type_bucket("") is None
    coord._type_device_buckets = "bad"  # type: ignore[assignment]  # noqa: SLF001
    assert coord.type_bucket("envoy") is None
    coord._type_device_buckets = {
        "envoy": {"devices": "bad", "count": 1}
    }  # noqa: SLF001
    assert coord.type_bucket("envoy")["devices"] == []
    assert coord.type_label("") is None
    coord._type_device_buckets = {
        "wind_turbine": {"type_label": 1, "count": 1}
    }  # noqa: SLF001
    assert coord.type_label("wind_turbine") == "Wind Turbine"
    assert coord.type_identifier("") is None
    assert coord.type_identifier("envoy") is None
    assert coord.type_device_name("") is None
    coord._type_device_buckets = {}  # noqa: SLF001
    assert coord.type_device_name("wind_turbine") is None


@pytest.mark.asyncio
async def test_coordinator_diagnostics_and_type_helper_fallback_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._battery_status_payload = None  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001

    await coord.async_ensure_battery_status_diagnostics()
    coord._async_refresh_battery_status.assert_awaited_once_with(
        force=True
    )  # noqa: SLF001

    coord._battery_status_payload = {"ok": True}  # noqa: SLF001
    await coord.async_ensure_battery_status_diagnostics()
    coord._async_refresh_battery_status.assert_awaited_once_with(
        force=True
    )  # noqa: SLF001

    coord._inverter_data = {  # noqa: SLF001
        "INV-B": "bad",
        "INV-A": {"lifetime_query_start_date": "2022-08-10"},
    }
    assert coord._inverter_start_date() == "2022-08-10"  # noqa: SLF001

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("bad")

    coord._inverter_data = {  # noqa: SLF001
        "BAD": "bad",
        "INV-A": {
            "serial_number": "INV-A",
            "name": "IQ7A",
            "status": "normal",
            "status_text": "Normal",
            "last_report": "2026-03-27T00:00:00Z",
            "array_name": BadStr(),
            "fw1": BadStr(),
        },
    }
    coord._inverter_order = ["BAD", "INV-A"]  # noqa: SLF001
    coord._inverter_model_counts = {}  # noqa: SLF001
    coord._inverter_summary_counts = {}  # noqa: SLF001
    coord._merge_microinverter_type_bucket()  # noqa: SLF001
    assert coord.type_bucket("microinverter")["count"] == 1

    coord._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "type_key": "heatpump",
            "type_label": "Heat Pump",
            "count": 2,
            "devices": [
                {"device_type": "HEAT_PUMP"},
                {"name": "Europa Mini WP", "device_uid": "HP-2"},
            ],
        }
    }
    coord._type_device_order = ["heatpump"]  # noqa: SLF001
    assert coord.type_device_model("heatpump") == "Europa Mini WP x1"
    assert coord.type_device_serial_number("heatpump") == "HP-2"

    coord._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "type_key": "heatpump",
            "type_label": "Heat Pump",
            "count": 2,
            "devices": [
                {"device_type": "HEAT_PUMP", "sw_version": "1.0", "hw_version": "A"},
                {"device_type": "ENERGY_METER", "sw_version": "1.0", "hw_version": "A"},
            ],
        }
    }
    assert coord.type_device_sw_version("heatpump") == "1.0"
    assert coord.type_device_hw_version("heatpump") == "A"

    coord._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "type_key": "heatpump",
            "type_label": "Heat Pump",
            "count": 3,
            "devices": [
                {"device_type": "HEAT_PUMP"},
                {"device_type": "ENERGY_METER", "sw_version": "1.0", "hw_version": "A"},
                {
                    "device_type": "SG_READY_GATEWAY",
                    "sw_version": "1.0",
                    "hw_version": "A",
                },
            ],
        }
    }
    assert coord.type_device_sw_version("heatpump") == "1.0"
    assert coord.type_device_hw_version("heatpump") == "A"

    coord._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "type_key": "heatpump",
            "type_label": "Heat Pump",
            "count": 3,
            "devices": [
                {"device_type": "HEAT_PUMP"},
                {"device_type": "ENERGY_METER", "sw_version": "1.0", "hw_version": "A"},
                {"device_type": "ENERGY_METER", "sw_version": "2.0", "hw_version": "B"},
            ],
        }
    }
    assert coord.type_device_sw_version("heatpump") == "1.0 x1, 2.0 x1"
    assert coord.type_device_hw_version("heatpump") == "A x1, B x1"

    coord._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "type_key": "heatpump",
            "type_label": "Heat Pump",
            "count": 1,
            "devices": [{"device_type": "HEAT_PUMP"}],
        }
    }
    assert coord.type_device_model("heatpump") == "Heat Pump"
    assert coord.parse_type_identifier("type:SITE123:meter") == ("SITE123", "envoy")


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
