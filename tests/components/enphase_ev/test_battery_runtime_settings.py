from __future__ import annotations

import time
from datetime import datetime, time as dt_time, timezone
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest


def test_parse_battery_settings_payload_maps_mode_and_controls(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    runtime.parse_battery_settings_payload(
        {
            "data": {
                "batteryGridMode": "ImportExport",
                "hideChargeFromGrid": False,
                "envoySupportsVls": True,
                "chargeFromGrid": True,
                "chargeFromGridScheduleEnabled": True,
                "chargeBeginTime": 120,
                "chargeEndTime": 300,
                "acceptedItcDisclaimer": "2026-02-08T10:00:00+00:00",
                "veryLowSoc": 15,
                "veryLowSocMin": 10,
                "veryLowSocMax": 25,
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "batteryBackupPercentageMin": 8,
                "batteryBackupPercentageMax": 95,
                "stormGuardState": "enabled",
                "cfgControl": {
                    "show": True,
                    "enabled": True,
                    "scheduleSupported": True,
                    "forceScheduleSupported": True,
                },
                "devices": {
                    "iqEvse": {
                        "useBatteryFrSelfConsumption": True,
                    }
                },
            }
        }
    )

    assert coord.battery_mode_display == "Import and Export"
    assert coord.battery_charge_from_grid_allowed is True
    assert coord.battery_discharge_to_grid_allowed is True
    assert coord.battery_charge_from_grid_enabled is True
    assert coord.battery_charge_from_grid_schedule_enabled is True
    assert coord.battery_charge_from_grid_start_time == dt_time(2, 0)
    assert coord.battery_charge_from_grid_end_time == dt_time(5, 0)
    assert coord.battery_shutdown_level == 15
    assert coord.battery_shutdown_level_min == 10
    assert coord.battery_shutdown_level_max == 25
    assert coord.battery_shutdown_level_available is True
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_effective_backup_percentage == 20
    assert coord.battery_reserve_min == 8
    assert coord.battery_reserve_max == 95
    assert coord.storm_guard_state == "enabled"
    assert coord.battery_cfg_control_show is True
    assert coord.battery_cfg_control_enabled is True
    assert coord.battery_cfg_control_schedule_supported is True
    assert coord.battery_cfg_control_force_schedule_supported is True
    assert coord.battery_use_battery_for_self_consumption is True


def test_parse_battery_settings_payload_clears_pending_when_matching(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 35  # noqa: SLF001
    coord._battery_operation_mode_sub_type = None  # noqa: SLF001

    runtime.parse_battery_settings_payload(
        {
            "data": {
                "profile": "cost_savings",
                "batteryBackupPercentage": 20,
                "operationModeSubType": "prioritize-energy",
            }
        }
    )

    assert coord.battery_profile_pending is False
    assert coord.battery_pending_profile is None


def test_battery_soc_min_floor_applies_to_reserve_and_shutdown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc_min = None  # noqa: SLF001
    coord._battery_very_low_soc_max = None  # noqa: SLF001

    assert coord.battery_reserve_min == 5
    assert coord.battery_shutdown_level_min == 5
    assert coord.battery_shutdown_level_available is True
    assert (
        coord._normalize_battery_reserve_for_profile("self-consumption", 1) == 5
    )  # noqa: SLF001

    coord._battery_very_low_soc_min = 7  # noqa: SLF001
    assert coord.battery_reserve_min == 7
    assert coord.battery_shutdown_level_min == 7
    assert (
        coord._normalize_battery_reserve_for_profile("cost_savings", 1) == 7
    )  # noqa: SLF001

    coord._battery_profile = "backup_only"  # noqa: SLF001
    assert coord.battery_reserve_min == 100
    assert coord.battery_reserve_max == 100


def test_parse_battery_settings_unknown_grid_mode_uses_none_permissions(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_battery_settings_payload(
        {"data": {"batteryGridMode": "RegionalSpecial"}}
    )

    assert coord.battery_grid_mode == "RegionalSpecial"
    assert coord.battery_mode_display == "Regionalspecial"
    assert coord.battery_charge_from_grid_allowed is None
    assert coord.battery_discharge_to_grid_allowed is None


@pytest.mark.asyncio
async def test_refresh_battery_settings_caches_and_redacts(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": {"batteryGridMode": "ImportOnly", "chargeFromGrid": False},
            "userId": "123",
            "token": "secret-token",
        }
    )

    await coord.battery_runtime.async_refresh_battery_settings(force=True)

    assert coord.battery_grid_mode == "ImportOnly"
    assert coord._battery_settings_payload is not None  # noqa: SLF001
    assert coord._battery_settings_payload["userId"] == "[redacted]"  # noqa: SLF001
    assert coord._battery_settings_payload["token"] == "[redacted]"  # noqa: SLF001

    coord._battery_settings_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord.client.battery_settings_details.reset_mock()
    await coord.battery_runtime.async_refresh_battery_settings()
    coord.client.battery_settings_details.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_battery_settings_bypasses_cache_when_profile_pending(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_settings_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_backup_percentage = 100  # noqa: SLF001
    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
            }
        }
    )

    await coord.battery_runtime.async_refresh_battery_settings()

    coord.client.battery_settings_details.assert_awaited_once()
    assert coord.battery_profile_pending is False


@pytest.mark.asyncio
async def test_refresh_battery_settings_clears_schedule_times_when_null_or_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001

    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": {
                "chargeFromGrid": True,
                "chargeBeginTime": None,
                "chargeEndTime": None,
            }
        }
    )
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert coord._battery_charge_begin_time is None  # noqa: SLF001
    assert coord._battery_charge_end_time is None  # noqa: SLF001
    assert coord.charge_from_grid_schedule_supported is False

    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"chargeFromGrid": True}}
    )
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert coord._battery_charge_begin_time is None  # noqa: SLF001
    assert coord._battery_charge_end_time is None  # noqa: SLF001
    assert coord.charge_from_grid_schedule_supported is False


@pytest.mark.asyncio
async def test_set_charge_from_grid_enable_autostamps_and_defaults(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = None  # noqa: SLF001
    coord._battery_charge_begin_time = None  # noqa: SLF001
    coord._battery_charge_end_time = None  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    fixed_now = datetime(2026, 2, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: fixed_now)

    await coord.battery_runtime.async_set_charge_from_grid(True)

    args = coord.client.set_battery_settings.await_args.args
    payload = args[0]
    assert payload["chargeFromGrid"] is True
    assert payload["acceptedItcDisclaimer"] == fixed_now.isoformat()
    assert payload["chargeBeginTime"] == 120
    assert payload["chargeEndTime"] == 300
    assert payload["chargeFromGridScheduleEnabled"] is False
    assert coord.battery_charge_from_grid_enabled is True
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_schedule_toggle_and_time_updates_validate_and_allow_overnight(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    fixed_now = datetime(2026, 2, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: fixed_now)

    with pytest.raises(ServiceValidationError, match="enabled first"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_enabled(True)

    coord._battery_charge_from_grid = True  # noqa: SLF001
    await coord.battery_runtime.async_set_charge_from_grid_schedule_enabled(False)
    args = coord.client.set_battery_settings.await_args.args
    payload = args[0]
    assert payload["chargeFromGridScheduleEnabled"] is False
    assert payload["chargeBeginTime"] == 120
    assert payload["chargeEndTime"] == 300

    coord._battery_settings_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord.client.set_battery_settings.reset_mock()
    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(23, 0), end=dt_time(2, 0)
    )
    args = coord.client.set_battery_settings.await_args.args
    payload = args[0]
    assert payload["chargeBeginTime"] == 1380
    assert payload["chargeEndTime"] == 120

    coord.client.set_battery_settings.reset_mock()
    coord._battery_settings_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="must be different"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(2, 0), end=dt_time(2, 0)
        )


@pytest.mark.asyncio
async def test_set_battery_shutdown_level_validation_and_write_guard(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord._battery_very_low_soc_min = 10  # noqa: SLF001
    coord._battery_very_low_soc_max = 25  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    await coord.battery_runtime.async_set_battery_shutdown_level(20)
    args = coord.client.set_battery_settings.await_args.args
    assert args[0] == {"veryLowSoc": 20}

    with pytest.raises(ServiceValidationError, match="between 10 and 25"):
        await coord.battery_runtime.async_set_battery_shutdown_level(9)

    coord._battery_envoy_supports_vls = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord.battery_runtime.async_set_battery_shutdown_level(12)


@pytest.mark.asyncio
async def test_battery_settings_write_lock_and_debounce(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    await coord._battery_settings_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord.battery_runtime.async_apply_battery_settings(
                {"chargeFromGrid": True}
            )
    finally:
        coord._battery_settings_write_lock.release()  # noqa: SLF001

    coord._battery_settings_last_write_mono = time.monotonic()  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="too quickly"):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )


def test_battery_helper_edge_cases_cover_fallback_paths(coordinator_factory) -> None:
    coord = coordinator_factory()

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    class BadTime:
        @property
        def hour(self):
            raise ValueError("boom")

    assert coord._normalize_battery_grid_mode(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_battery_grid_mode("   ") is None  # noqa: SLF001
    assert coord._battery_grid_mode_key(BadStr()) is None  # noqa: SLF001
    assert coord._battery_grid_mode_label(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_minutes_of_day(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_minutes_of_day(1440) is None  # noqa: SLF001
    assert coord._minutes_of_day_to_time(None) is None  # noqa: SLF001
    assert coord._minutes_of_day_to_time(2000) is None  # noqa: SLF001
    assert coord._time_to_minutes_of_day(None) is None  # noqa: SLF001
    assert coord._time_to_minutes_of_day(BadTime()) is None  # noqa: SLF001


def test_charge_from_grid_control_unavailable_when_no_battery(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = False  # noqa: SLF001
    assert coord.charge_from_grid_control_available is False


def test_charge_from_grid_control_unavailable_for_read_only_user(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    assert coord.charge_from_grid_control_available is False


def test_charge_from_grid_control_uses_cfg_control_when_present(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_cfg_control_show = True  # noqa: SLF001
    coord._battery_cfg_control_enabled = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = True  # noqa: SLF001

    assert coord.charge_from_grid_control_available is True


def test_charge_from_grid_control_honors_cfg_control_false(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_cfg_control_show = True  # noqa: SLF001
    coord._battery_cfg_control_enabled = False  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001

    assert coord.charge_from_grid_control_available is True


def test_charge_from_grid_control_honors_cfg_control_show_false(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_cfg_control_show = False  # noqa: SLF001
    coord._battery_cfg_control_enabled = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001

    assert coord.charge_from_grid_control_available is False


def test_charge_from_grid_control_falls_back_to_legacy_hide_when_cfg_control_absent(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_cfg_control_show = None  # noqa: SLF001
    coord._battery_cfg_control_enabled = None  # noqa: SLF001
    coord._battery_hide_charge_from_grid = True  # noqa: SLF001

    assert coord.charge_from_grid_control_available is False


def test_parse_battery_settings_payload_handles_non_dict_and_bad_disclaimer(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_settings_payload(["bad"])

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord.battery_runtime.parse_battery_settings_payload(
        {"data": {"acceptedItcDisclaimer": BadStr()}}
    )
    assert coord._battery_accepted_itc_disclaimer is None  # noqa: SLF001


def test_parse_battery_settings_payload_clears_missing_reserve_bounds(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_backup_percentage_min = 8  # noqa: SLF001
    coord._battery_backup_percentage_max = 95  # noqa: SLF001
    coord._battery_very_low_soc_min = 10  # noqa: SLF001

    coord.battery_runtime.parse_battery_settings_payload(
        {"data": {"batteryBackupPercentage": 20}}
    )

    assert coord.battery_reserve_min == 10
    assert coord.battery_reserve_max == 100


def test_parse_battery_settings_payload_keeps_reserve_bounds_for_partial_update(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_backup_percentage_min = 8  # noqa: SLF001
    coord._battery_backup_percentage_max = 95  # noqa: SLF001

    coord.battery_runtime.parse_battery_settings_payload(
        {"chargeFromGrid": True},
        clear_missing_reserve_bounds=False,
    )

    assert coord.battery_reserve_min == 8
    assert coord.battery_reserve_max == 95


@pytest.mark.asyncio
async def test_refresh_battery_settings_handles_non_dict_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_settings_details = AsyncMock(return_value=["unexpected"])
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert coord._battery_settings_payload == {"value": ["unexpected"]}  # noqa: SLF001


@pytest.mark.asyncio
async def test_apply_battery_settings_rejects_empty_payload(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    with pytest.raises(ServiceValidationError, match="payload is unavailable"):
        await coord.battery_runtime.async_apply_battery_settings({})


@pytest.mark.asyncio
async def test_apply_battery_settings_partial_payload_keeps_schedule_times(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    await coord.battery_runtime.async_apply_battery_settings({"chargeFromGrid": False})

    assert coord._battery_charge_begin_time == 120  # noqa: SLF001
    assert coord._battery_charge_end_time == 300  # noqa: SLF001
    assert coord.charge_from_grid_schedule_supported is True


@pytest.mark.asyncio
async def test_battery_settings_forbidden_translates_to_validation_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="HTTP 403 Forbidden"):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )


@pytest.mark.asyncio
async def test_battery_settings_forbidden_read_only_user_translates_to_permission_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="not permitted"):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )
    coord.client.set_battery_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_settings_unknown_role_rejects_when_refresh_unresolved(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord.client.battery_site_settings = AsyncMock(
        return_value={"data": {"userDetails": {}}}
    )
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})

    with pytest.raises(ServiceValidationError, match="could not be confirmed"):
        await coord.async_set_charge_from_grid(False)

    coord.client.battery_site_settings.assert_awaited_once()
    coord.client.set_battery_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_settings_unauthorized_translates_to_reauth_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )


@pytest.mark.asyncio
async def test_battery_settings_unexpected_http_error_reraises(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )


@pytest.mark.asyncio
async def test_battery_settings_service_validation_paths(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()

    coord._battery_has_encharge = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="setting is unavailable"):
        await coord.battery_runtime.async_set_charge_from_grid(True)
    with pytest.raises(ServiceValidationError, match="schedule is unavailable"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_enabled(True)
    with pytest.raises(ServiceValidationError, match="schedule is unavailable"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(1, 0)
        )

    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 120  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="must be different"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_enabled(True)

    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="enabled first"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(1, 0)
        )

    coord._battery_charge_from_grid = True  # noqa: SLF001

    class BadTime:
        @property
        def hour(self):
            raise ValueError("boom")

    with pytest.raises(ServiceValidationError, match="time is invalid"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=BadTime()
        )


@pytest.mark.asyncio
async def test_battery_shutdown_level_invalid_type_raises_validation(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    class BadInt:
        def __int__(self):
            raise ValueError("boom")

    coord = coordinator_factory()
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord._battery_very_low_soc_min = 10  # noqa: SLF001
    coord._battery_very_low_soc_max = 25  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="level is invalid"):
        await coord.battery_runtime.async_set_battery_shutdown_level(BadInt())


def test_parse_battery_schedules_payload_uses_entry_timezone_and_clears_stale_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_cfg_schedule_limit = 55  # noqa: SLF001
    coord._battery_cfg_schedule_id = "stale"  # noqa: SLF001
    coord._battery_cfg_schedule_days = [1, 2]  # noqa: SLF001
    coord._battery_cfg_schedule_timezone = "UTC"  # noqa: SLF001

    runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "details": [
                    {
                        "scheduleId": "sched-2",
                        "startTime": "00:00",
                        "endTime": "07:30",
                        "limit": 90,
                        "days": [1, 7],
                        "timezone": "Europe/Lisbon",
                        "isEnabled": True,
                    }
                ]
            }
        }
    )

    assert coord._battery_charge_begin_time == 0  # noqa: SLF001
    assert coord._battery_charge_end_time == 450  # noqa: SLF001
    assert coord.battery_cfg_schedule_limit == 90
    assert coord._battery_cfg_schedule_id == "sched-2"  # noqa: SLF001
    assert coord._battery_cfg_schedule_days == [1, 7]  # noqa: SLF001
    assert coord._battery_cfg_schedule_timezone == "Europe/Lisbon"  # noqa: SLF001

    runtime.parse_battery_schedules_payload({"cfg": {"details": []}})

    assert coord.battery_cfg_schedule_limit is None
    assert coord._battery_cfg_schedule_id is None  # noqa: SLF001
    assert coord._battery_cfg_schedule_days is None  # noqa: SLF001
    assert coord._battery_cfg_schedule_timezone is None  # noqa: SLF001
    assert coord._battery_charge_begin_time == 0  # noqa: SLF001
    assert coord._battery_charge_end_time == 450  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_battery_schedules_handles_non_dict_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_schedules = AsyncMock(return_value="bad")

    await coord.battery_runtime.async_refresh_battery_schedules()

    assert coord._battery_schedules_payload is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_battery_schedules_stores_non_dict_redacted_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_schedules = AsyncMock(
        return_value={
            "cfg": {
                "details": [
                    {
                        "scheduleId": "sched-2",
                        "startTime": "01:00",
                        "endTime": "05:00",
                        "limit": 85,
                        "days": [1, 2, 3],
                        "timezone": "Europe/Lisbon",
                        "isEnabled": True,
                    }
                ]
            }
        }
    )
    coord._redact_battery_payload = MagicMock(return_value="masked")  # noqa: SLF001

    await coord.battery_runtime.async_refresh_battery_schedules()

    assert coord._battery_schedules_payload == {"value": "masked"}  # noqa: SLF001
    assert coord.battery_cfg_schedule_limit == 85


@pytest.mark.asyncio
async def test_refresh_battery_schedules_stores_dict_redacted_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_schedules = AsyncMock(
        return_value={
            "cfg": {
                "details": [
                    {
                        "scheduleId": "sched-2",
                        "startTime": "01:00",
                        "endTime": "05:00",
                        "limit": 85,
                        "days": [1, 2, 3],
                        "timezone": "Europe/Lisbon",
                        "isEnabled": True,
                    }
                ]
            }
        }
    )
    coord._redact_battery_payload = MagicMock(  # noqa: SLF001
        return_value={"value": "masked"}
    )

    await coord.battery_runtime.async_refresh_battery_schedules()

    assert coord._battery_schedules_payload == {"value": "masked"}  # noqa: SLF001
    assert coord.battery_cfg_schedule_limit == 85


def test_parse_battery_schedules_payload_handles_invalid_shapes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_schedules_payload(None)
    assert coord.battery_cfg_schedule_limit is None

    coord.battery_runtime.parse_battery_schedules_payload({"cfg": None})
    assert coord.battery_cfg_schedule_limit is None

    coord.battery_runtime.parse_battery_schedules_payload({"cfg": {"details": [None]}})
    assert coord.battery_cfg_schedule_limit is None


def test_parse_battery_schedules_payload_handles_invalid_times_and_top_level_timezone(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "timezone": "America/Los_Angeles",
            "cfg": {
                "details": [
                    {
                        "scheduleId": "sched-3",
                        "startTime": "aa:bb",
                        "endTime": "11:xx",
                        "limit": 75,
                        "days": [1],
                    }
                ]
            },
        }
    )

    assert coord._battery_charge_begin_time is None  # noqa: SLF001
    assert coord._battery_charge_end_time is None  # noqa: SLF001
    assert coord._battery_cfg_schedule_timezone == "America/Los_Angeles"  # noqa: SLF001


# ---------------------------------------------------------------------------
# CFG schedule CRUD – lock/debounce, restore-on-failure, availability guards
# ---------------------------------------------------------------------------


def _seed_cfg_schedule(coord):
    """Populate coordinator with a valid parsed CFG schedule."""
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_cfg_schedule_id = "sched-1"  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 80  # noqa: SLF001
    coord._battery_cfg_schedule_days = [1, 2, 3, 4, 5, 6, 7]  # noqa: SLF001
    coord._battery_cfg_schedule_timezone = "Europe/Lisbon"  # noqa: SLF001
    coord._battery_schedules_payload = {"cfg": {"details": [{}]}}  # noqa: SLF001
    coord.client.delete_battery_schedule = AsyncMock(return_value={})
    coord.client.create_battery_schedule = AsyncMock(return_value={})
    coord.client.update_battery_schedule = AsyncMock(return_value={})
    coord.client._bp_xsrf_token = "tok"  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_acquires_write_lock(
    coordinator_factory,
) -> None:
    """Schedule time update via /schedules must use the battery write lock."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    # Manually hold the lock → should raise "already in progress".
    await coord._battery_settings_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
                start=dt_time(23, 0), end=dt_time(6, 0)
            )
    finally:
        coord._battery_settings_write_lock.release()  # noqa: SLF001


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_respects_debounce(
    coordinator_factory,
) -> None:
    """Schedule time update via /schedules must respect the debounce window."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    coord._battery_settings_last_write_mono = time.monotonic()  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="too quickly"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_defaults_missing_limit_to_100(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_limit = None  # noqa: SLF001

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(23, 0), end=dt_time(6, 0)
    )

    call = coord.client.update_battery_schedule.await_args
    assert call.kwargs["limit"] == 100


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_uses_in_place_put(
    coordinator_factory,
) -> None:
    """Schedule time update must use in-place PUT, not delete+create."""
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(23, 0), end=dt_time(6, 0)
    )

    coord.client.update_battery_schedule.assert_awaited_once()
    call = coord.client.update_battery_schedule.await_args
    assert call.args[0] == "sched-1"
    assert call.kwargs["start_time"] == "23:00"
    assert call.kwargs["end_time"] == "06:00"
    assert call.kwargs["schedule_type"] == "CFG"
    assert call.kwargs["timezone"] == "Europe/Lisbon"
    # delete+create should NOT be used.
    coord.client.delete_battery_schedule.assert_not_awaited()
    coord.client.create_battery_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_forbidden_translates_to_validation_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="HTTP 403 Forbidden"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_unexpected_client_error_reraises(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="Server Error",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )
    assert err.value.status == 500


@pytest.mark.asyncio
async def test_cfg_schedule_limit_rejects_without_existing_schedule(
    coordinator_factory,
) -> None:
    """Setting CFG limit must fail when no parsed schedule exists."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.update_battery_schedule = AsyncMock()

    # No schedule ID → should reject.
    with pytest.raises(ServiceValidationError, match="No existing"):
        await coord.battery_runtime.async_set_cfg_schedule_limit(90)


@pytest.mark.asyncio
async def test_cfg_schedule_limit_rejects_without_schedule_api(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client = MagicMock(spec=())

    with pytest.raises(ServiceValidationError, match="Schedule API not available"):
        await coord.battery_runtime.async_set_cfg_schedule_limit(90)


@pytest.mark.asyncio
async def test_cfg_schedule_limit_acquires_write_lock(
    coordinator_factory,
) -> None:
    """CFG limit update must use the battery write lock."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    await coord._battery_settings_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord.battery_runtime.async_set_cfg_schedule_limit(90)
    finally:
        coord._battery_settings_write_lock.release()  # noqa: SLF001


@pytest.mark.asyncio
async def test_cfg_schedule_limit_uses_in_place_put(
    coordinator_factory,
) -> None:
    """CFG limit update must use in-place PUT, not delete+create."""
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    await coord.battery_runtime.async_set_cfg_schedule_limit(95)

    coord.client.update_battery_schedule.assert_awaited_once()
    call = coord.client.update_battery_schedule.await_args
    assert call.args[0] == "sched-1"
    assert call.kwargs["limit"] == 95
    assert call.kwargs["start_time"] == "02:00"
    assert call.kwargs["end_time"] == "05:00"
    assert call.kwargs["schedule_type"] == "CFG"
    assert call.kwargs["timezone"] == "Europe/Lisbon"
    assert call.kwargs["days"] == [1, 2, 3, 4, 5, 6, 7]
    # delete+create should NOT be used.
    coord.client.delete_battery_schedule.assert_not_awaited()
    coord.client.create_battery_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_cfg_schedule_limit_updates_state_on_success(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    await coord.battery_runtime.async_set_cfg_schedule_limit(95)

    assert coord._battery_cfg_schedule_limit == 95  # noqa: SLF001
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_unauthorized_translates_to_reauth_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_reraises_unexpected_http_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )


async def test_cfg_schedule_limit_unauthorized_translates_to_reauth_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await coord.battery_runtime.async_set_cfg_schedule_limit(95)


@pytest.mark.asyncio
async def test_cfg_schedule_limit_reraises_unexpected_http_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime.async_set_cfg_schedule_limit(95)


@pytest.mark.asyncio
async def test_atomic_cfg_schedule_update_rejects_limit_below_shutdown_floor(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_very_low_soc = 60  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="at least 60%"):
        await coord.async_update_cfg_schedule(limit=55)

    coord.client.update_battery_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_atomic_cfg_schedule_update_unauthorized_translates_to_reauth_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await coord.async_update_cfg_schedule(limit=95)


@pytest.mark.asyncio
async def test_atomic_cfg_schedule_update_validation_paths(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    class BadTime:
        @property
        def hour(self):
            raise ValueError("boom")

    coord = coordinator_factory()

    coord.client = MagicMock(spec=())
    with pytest.raises(ServiceValidationError, match="Schedule API not available"):
        await coord.async_update_cfg_schedule(limit=95)

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="pending Envoy sync"):
        await coord.async_update_cfg_schedule(limit=95)

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_id = None  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="No existing"):
        await coord.async_update_cfg_schedule(limit=95)

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._current_charge_from_grid_schedule_window = MagicMock(  # noqa: SLF001
        return_value=(None, None)
    )
    with pytest.raises(
        ServiceValidationError, match="Current schedule times are not available"
    ):
        await coord.async_update_cfg_schedule(limit=95)

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    with pytest.raises(ServiceValidationError, match="time is invalid"):
        await coord.async_update_cfg_schedule(start=BadTime())

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    with pytest.raises(ServiceValidationError, match="must be different"):
        await coord.async_update_cfg_schedule(start=dt_time(2, 0), end=dt_time(2, 0))

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    with pytest.raises(ServiceValidationError, match="between 5 and 100"):
        await coord.async_update_cfg_schedule(limit=4)


@pytest.mark.asyncio
async def test_atomic_cfg_schedule_update_reraises_unexpected_http_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.async_update_cfg_schedule(limit=95)


@pytest.mark.asyncio
async def test_atomic_cfg_schedule_update_updates_state_on_success(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    await coord.async_update_cfg_schedule(
        start=dt_time(23, 0), end=dt_time(6, 0), limit=95
    )

    call = coord.client.update_battery_schedule.await_args
    assert call.args[0] == "sched-1"
    assert call.kwargs["start_time"] == "23:00"
    assert call.kwargs["end_time"] == "06:00"
    assert call.kwargs["limit"] == 95
    assert coord._battery_charge_begin_time == 1380  # noqa: SLF001
    assert coord._battery_charge_end_time == 360  # noqa: SLF001
    assert coord._battery_cfg_schedule_limit == 95  # noqa: SLF001
    assert coord._battery_settings_cache_until is None  # noqa: SLF001
    coord.async_request_refresh.assert_awaited_once()
    coord.kick_fast.assert_called_once()


async def test_async_update_data_site_only_ignores_battery_schedule_refresh_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("boom")
    )
    coord._async_refresh_storm_guard_profile = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(return_value=None)  # noqa: SLF001

    assert await coord._async_update_data() == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_update_data_ignores_battery_schedule_refresh_errors(
    coordinator_factory,
) -> None:
    from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

    coord = coordinator_factory()
    coord.summary = MagicMock()
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.summary.invalidate = MagicMock()
    coord.session_history = MagicMock()
    coord.session_history.get_cache_view = MagicMock(
        return_value=MagicMock(sessions=[], needs_refresh=False, blocked=False)
    )
    coord.session_history.sum_energy = MagicMock(return_value=0.0)
    coord.client.status = AsyncMock(
        return_value={
            "ts": "2026-02-28T00:00:00Z",
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "EV",
                    "connectors": [{}],
                    "pluggedIn": False,
                    "charging": False,
                    "faulted": False,
                    "session_d": {},
                }
            ],
        }
    )
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("boom")
    )
    coord._async_refresh_storm_guard_profile = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(return_value=None)  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert RANDOM_SERIAL in result


# ---------------------------------------------------------------------------
# CFG schedule creation – no existing schedule
# ---------------------------------------------------------------------------


def _seed_no_cfg_schedule(coord):
    """Populate coordinator for an EMEA site with no existing CFG schedule."""
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_cfg_schedule_id = None  # noqa: SLF001
    coord._battery_cfg_schedule_limit = None  # noqa: SLF001
    coord._battery_schedules_payload = {"cfg": {"details": []}}  # noqa: SLF001
    coord.client.create_battery_schedule = AsyncMock(return_value={})
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()


@pytest.mark.asyncio
async def test_cfg_schedule_creates_when_none_exists(
    coordinator_factory,
) -> None:
    """When no CFG schedule exists but the API is available, create one."""
    coord = coordinator_factory()
    _seed_no_cfg_schedule(coord)

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(22, 0), end=dt_time(8, 0)
    )

    coord.client.create_battery_schedule.assert_awaited_once_with(
        schedule_type="CFG",
        start_time="22:00",
        end_time="08:00",
        limit=100,
        days=[1, 2, 3, 4, 5, 6, 7],
        timezone="UTC",
    )
    # Legacy set_battery_settings should NOT have been called.
    coord.client.set_battery_settings.assert_not_awaited()
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_cfg_schedule_create_uses_stored_timezone(
    coordinator_factory,
) -> None:
    """New CFG schedule should use the timezone from a previous poll."""
    coord = coordinator_factory()
    _seed_no_cfg_schedule(coord)
    coord._battery_cfg_schedule_timezone = "Europe/Lisbon"  # noqa: SLF001

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(22, 0), end=dt_time(8, 0)
    )

    call = coord.client.create_battery_schedule.await_args
    assert call.kwargs["timezone"] == "Europe/Lisbon"


@pytest.mark.asyncio
async def test_cfg_schedule_create_acquires_write_lock(
    coordinator_factory,
) -> None:
    """Creating a new CFG schedule must use the battery write lock."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_no_cfg_schedule(coord)

    await coord._battery_settings_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
                start=dt_time(22, 0), end=dt_time(8, 0)
            )
    finally:
        coord._battery_settings_write_lock.release()  # noqa: SLF001


@pytest.mark.asyncio
async def test_legacy_fallback_when_create_api_unavailable(
    coordinator_factory,
) -> None:
    """Without the /schedules API, the legacy batterySettings PUT is used."""
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_cfg_schedule_id = None  # noqa: SLF001
    # No create_battery_schedule on client — legacy path should fire.
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(23, 0), end=dt_time(7, 0)
    )

    coord.client.set_battery_settings.assert_awaited_once()
    args = coord.client.set_battery_settings.await_args.args
    payload = args[0]
    assert payload["chargeBeginTime"] == 1380
    assert payload["chargeEndTime"] == 420


# ---------------------------------------------------------------------------
# Pending state tracking – write guards and status parsing
# ---------------------------------------------------------------------------


def test_parse_battery_schedules_captures_entry_status(
    coordinator_factory,
) -> None:
    """Schedule status from individual entry is parsed and lowercased."""
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001

    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "details": [
                    {
                        "isEnabled": True,
                        "startTime": "22:00",
                        "endTime": "08:00",
                        "limit": 100,
                        "scheduleId": "sched-1",
                        "scheduleStatus": "Pending",
                    }
                ],
            }
        }
    )

    assert coord.battery_cfg_schedule_status == "pending"
    assert coord.battery_cfg_schedule_pending is True


def test_parse_battery_schedules_captures_family_status(
    coordinator_factory,
) -> None:
    """Schedule status from the cfg family level is used as fallback."""
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001

    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "scheduleStatus": "Active",
                "details": [
                    {
                        "isEnabled": True,
                        "startTime": "22:00",
                        "endTime": "08:00",
                        "limit": 100,
                        "scheduleId": "sched-1",
                    }
                ],
            }
        }
    )

    assert coord.battery_cfg_schedule_status == "active"
    assert coord.battery_cfg_schedule_pending is False


def test_parse_battery_schedules_resets_stale_status(
    coordinator_factory,
) -> None:
    """A previously cached pending status is cleared when the next refresh omits it."""
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001

    # Refresh with a payload that has no scheduleStatus anywhere.
    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "details": [
                    {
                        "isEnabled": True,
                        "startTime": "22:00",
                        "endTime": "08:00",
                        "limit": 100,
                        "scheduleId": "sched-1",
                    }
                ],
            }
        }
    )

    assert coord.battery_cfg_schedule_status is None
    assert coord.battery_cfg_schedule_pending is False


def test_parse_battery_schedules_ignores_blank_status(
    coordinator_factory,
) -> None:
    """Blank or whitespace-only scheduleStatus is treated as absent."""
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001

    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "scheduleStatus": "  ",
                "details": [
                    {
                        "isEnabled": True,
                        "startTime": "22:00",
                        "endTime": "08:00",
                        "limit": 100,
                        "scheduleId": "sched-1",
                    }
                ],
            }
        }
    )

    assert coord.battery_cfg_schedule_status is None
    assert coord.battery_cfg_schedule_pending is False


def test_battery_cfg_schedule_status_defaults_to_none(
    coordinator_factory,
) -> None:
    """Without any schedule data, status properties return sensible defaults."""
    coord = coordinator_factory()

    assert coord.battery_cfg_schedule_status is None
    assert coord.battery_cfg_schedule_pending is False


@pytest.mark.asyncio
async def test_schedule_time_write_guard_rejects_when_pending(
    coordinator_factory,
) -> None:
    """Setting schedule time must fail while a change is pending Envoy sync."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="pending Envoy sync"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )

    # No API calls should have been made.
    coord.client.update_battery_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_time_write_guard_allows_when_active(
    coordinator_factory,
) -> None:
    """Setting schedule time must succeed when status is active."""
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_status = "active"  # noqa: SLF001

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(23, 0), end=dt_time(6, 0)
    )

    coord.client.update_battery_schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_cfg_schedule_limit_write_guard_rejects_when_pending(
    coordinator_factory,
) -> None:
    """Setting CFG limit must fail while a change is pending Envoy sync."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="pending Envoy sync"):
        await coord.battery_runtime.async_set_cfg_schedule_limit(90)

    coord.client.update_battery_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_cfg_schedule_limit_write_guard_allows_when_active(
    coordinator_factory,
) -> None:
    """Setting CFG limit must succeed when status is active."""
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_status = "active"  # noqa: SLF001

    await coord.battery_runtime.async_set_cfg_schedule_limit(90)

    coord.client.update_battery_schedule.assert_awaited_once()
