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

    coord._parse_battery_settings_payload(  # noqa: SLF001
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
                "stormGuardState": "enabled",
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
    assert coord.storm_guard_state == "enabled"
    assert coord.battery_use_battery_for_self_consumption is True


def test_parse_battery_settings_unknown_grid_mode_uses_none_permissions(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._parse_battery_settings_payload(  # noqa: SLF001
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

    await coord._async_refresh_battery_settings(force=True)  # noqa: SLF001

    assert coord.battery_grid_mode == "ImportOnly"
    assert coord._battery_settings_payload is not None  # noqa: SLF001
    assert coord._battery_settings_payload["userId"] == "[redacted]"  # noqa: SLF001
    assert coord._battery_settings_payload["token"] == "[redacted]"  # noqa: SLF001

    coord._battery_settings_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord.client.battery_settings_details.reset_mock()
    await coord._async_refresh_battery_settings()  # noqa: SLF001
    coord.client.battery_settings_details.assert_not_called()


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
    await coord._async_refresh_battery_settings(force=True)  # noqa: SLF001
    assert coord._battery_charge_begin_time is None  # noqa: SLF001
    assert coord._battery_charge_end_time is None  # noqa: SLF001
    assert coord.charge_from_grid_schedule_supported is False

    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"chargeFromGrid": True}}
    )
    await coord._async_refresh_battery_settings(force=True)  # noqa: SLF001
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

    await coord.async_set_charge_from_grid(True)

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
        await coord.async_set_charge_from_grid_schedule_enabled(True)

    coord._battery_charge_from_grid = True  # noqa: SLF001
    await coord.async_set_charge_from_grid_schedule_enabled(False)
    args = coord.client.set_battery_settings.await_args.args
    payload = args[0]
    assert payload["chargeFromGridScheduleEnabled"] is False
    assert payload["chargeBeginTime"] == 120
    assert payload["chargeEndTime"] == 300

    coord._battery_settings_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord.client.set_battery_settings.reset_mock()
    await coord.async_set_charge_from_grid_schedule_time(
        start=dt_time(23, 0), end=dt_time(2, 0)
    )
    args = coord.client.set_battery_settings.await_args.args
    payload = args[0]
    assert payload["chargeBeginTime"] == 1380
    assert payload["chargeEndTime"] == 120

    coord.client.set_battery_settings.reset_mock()
    coord._battery_settings_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="must be different"):
        await coord.async_set_charge_from_grid_schedule_time(
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

    await coord.async_set_battery_shutdown_level(20)
    args = coord.client.set_battery_settings.await_args.args
    assert args[0] == {"veryLowSoc": 20}

    with pytest.raises(ServiceValidationError, match="between 10 and 25"):
        await coord.async_set_battery_shutdown_level(9)

    coord._battery_envoy_supports_vls = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord.async_set_battery_shutdown_level(12)


@pytest.mark.asyncio
async def test_battery_settings_write_lock_and_debounce(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    await coord._battery_settings_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord._async_apply_battery_settings({"chargeFromGrid": True})  # noqa: SLF001
    finally:
        coord._battery_settings_write_lock.release()  # noqa: SLF001

    coord._battery_settings_last_write_mono = time.monotonic()  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="too quickly"):
        await coord._async_apply_battery_settings({"chargeFromGrid": False})  # noqa: SLF001


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


def test_parse_battery_settings_payload_handles_non_dict_and_bad_disclaimer(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord._parse_battery_settings_payload(["bad"])  # noqa: SLF001

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord._parse_battery_settings_payload(  # noqa: SLF001
        {"data": {"acceptedItcDisclaimer": BadStr()}}
    )
    assert coord._battery_accepted_itc_disclaimer is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_battery_settings_handles_non_dict_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_settings_details = AsyncMock(return_value=["unexpected"])
    await coord._async_refresh_battery_settings(force=True)  # noqa: SLF001
    assert coord._battery_settings_payload == {"value": ["unexpected"]}  # noqa: SLF001


@pytest.mark.asyncio
async def test_apply_battery_settings_rejects_empty_payload(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    with pytest.raises(ServiceValidationError, match="payload is unavailable"):
        await coord._async_apply_battery_settings({})  # noqa: SLF001


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

    await coord._async_apply_battery_settings({"chargeFromGrid": False})  # noqa: SLF001

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
        await coord._async_apply_battery_settings({"chargeFromGrid": False})  # noqa: SLF001


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
        await coord._async_apply_battery_settings({"chargeFromGrid": False})  # noqa: SLF001


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
        await coord._async_apply_battery_settings({"chargeFromGrid": False})  # noqa: SLF001


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
        await coord._async_apply_battery_settings({"chargeFromGrid": False})  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_settings_service_validation_paths(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()

    coord._battery_has_encharge = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="setting is unavailable"):
        await coord.async_set_charge_from_grid(True)
    with pytest.raises(ServiceValidationError, match="schedule is unavailable"):
        await coord.async_set_charge_from_grid_schedule_enabled(True)
    with pytest.raises(ServiceValidationError, match="schedule is unavailable"):
        await coord.async_set_charge_from_grid_schedule_time(start=dt_time(1, 0))

    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 120  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="must be different"):
        await coord.async_set_charge_from_grid_schedule_enabled(True)

    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="enabled first"):
        await coord.async_set_charge_from_grid_schedule_time(start=dt_time(1, 0))

    coord._battery_charge_from_grid = True  # noqa: SLF001

    class BadTime:
        @property
        def hour(self):
            raise ValueError("boom")

    with pytest.raises(ServiceValidationError, match="time is invalid"):
        await coord.async_set_charge_from_grid_schedule_time(start=BadTime())


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
        await coord.async_set_battery_shutdown_level(BadInt())
