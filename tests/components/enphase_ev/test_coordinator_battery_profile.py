from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest


def _profile_payload(
    *,
    profile: str = "self-consumption",
    reserve: int = 20,
    subtype: str | None = None,
) -> dict:
    data: dict[str, object] = {
        "profile": profile,
        "batteryBackupPercentage": reserve,
        "stormGuardState": "disabled",
        "evseStormEnabled": False,
        "devices": {
            "iqEvse": [{"uuid": "evse-1", "chargeMode": "MANUAL", "enable": False}]
        },
    }
    if subtype is not None:
        data["operationModeSubType"] = subtype
    return {"data": data}


@pytest.mark.asyncio
async def test_refresh_battery_site_settings_parses_flags(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.battery_site_settings = AsyncMock(
        return_value={
            "data": {
                "showProduction": True,
                "showConsumption": True,
                "showChargeFromGrid": True,
                "showSavingsMode": True,
                "showStormGuard": True,
                "showFullBackup": False,
                "showBatteryBackupPercentage": True,
                "isChargingModesEnabled": True,
                "hasEncharge": True,
                "hasEnpower": False,
                "countryCode": "US",
                "region": "CA",
                "locale": "en-US",
                "timezone": "America/Los_Angeles",
                "featureDetails": {"HEMS_EV_Custom_Schedule": True},
                "userDetails": {"isOwner": True, "isInstaller": False},
                "siteStatus": {"code": "normal", "text": "Normal", "severity": "info"},
            }
        }
    )

    await coord._async_refresh_battery_site_settings(force=True)  # noqa: SLF001

    assert coord.battery_has_encharge is True
    assert coord.battery_has_enpower is False
    assert coord.battery_is_charging_modes_enabled is True
    assert coord.battery_show_storm_guard is True
    assert coord.battery_show_production is True
    assert coord.battery_show_consumption is True
    assert coord.battery_country_code == "US"
    assert coord.battery_region == "CA"
    assert coord.battery_locale == "en-US"
    assert coord.battery_timezone == "America/Los_Angeles"
    assert coord.battery_feature_details == {"HEMS_EV_Custom_Schedule": True}
    assert coord.battery_user_is_owner is True
    assert coord.battery_user_is_installer is False
    assert coord.battery_site_status_code == "normal"
    assert coord.battery_profile_option_keys == ["self-consumption", "cost_savings"]


@pytest.mark.asyncio
async def test_set_system_profile_uses_remembered_reserve(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_profile_reserve_memory["cost_savings"] = 35  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})

    await coord.async_set_system_profile("cost_savings")

    coord.client.set_battery_profile.assert_awaited_once()
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["profile"] == "cost_savings"
    assert kwargs["battery_backup_percentage"] == 35
    assert coord.battery_pending_profile == "cost_savings"
    assert coord.battery_pending_backup_percentage == 35
    assert coord._battery_pending_require_exact_settings is False  # noqa: SLF001

    # Unknown regional profile should remain selectable as passthrough.
    coord.client.set_battery_profile.reset_mock()
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord._battery_profile = "regional_special"  # noqa: SLF001
    await coord.async_set_system_profile("regional_special")
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["profile"] == "regional_special"
    assert kwargs["battery_backup_percentage"] == 20


@pytest.mark.asyncio
async def test_savings_subtype_payload_on_and_off(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_backup_percentage = 21  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})

    await coord.async_set_savings_use_battery_after_peak(True)
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["operation_mode_sub_type"] == "prioritize-energy"
    assert coord._battery_pending_require_exact_settings is True  # noqa: SLF001

    coord.client.set_battery_profile.reset_mock()
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    await coord.async_set_savings_use_battery_after_peak(False)
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["operation_mode_sub_type"] is None


@pytest.mark.asyncio
async def test_cancel_pending_profile_change(
    coordinator_factory, mock_issue_registry
) -> None:
    from custom_components.enphase_ev.const import (
        DOMAIN,
        ISSUE_BATTERY_PROFILE_PENDING,
    )

    coord = coordinator_factory()
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.client.cancel_battery_profile_update = AsyncMock(
        return_value={"message": "success"}
    )

    await coord.async_cancel_pending_profile_change()

    coord.client.cancel_battery_profile_update.assert_awaited_once()
    assert coord.battery_profile_pending is False

    # Idempotent: no pending => no backend call, pending issue is cleared.
    coord.client.cancel_battery_profile_update.reset_mock()
    coord._battery_profile_issue_reported = True  # noqa: SLF001
    await coord.async_cancel_pending_profile_change()
    coord.client.cancel_battery_profile_update.assert_not_called()
    assert (DOMAIN, ISSUE_BATTERY_PROFILE_PENDING) in mock_issue_registry.deleted


def test_pending_profile_timeout_issue_lifecycle(
    coordinator_factory, mock_issue_registry
) -> None:
    from custom_components.enphase_ev.const import (
        BATTERY_PROFILE_PENDING_TIMEOUT_S,
        DOMAIN,
        ISSUE_BATTERY_PROFILE_PENDING,
    )

    coord = coordinator_factory()
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(
        timezone.utc
    ) - timedelta(  # noqa: SLF001
        seconds=BATTERY_PROFILE_PENDING_TIMEOUT_S + 30
    )

    coord._sync_battery_profile_pending_issue()  # noqa: SLF001

    assert mock_issue_registry.created
    domain, issue_id, payload = mock_issue_registry.created[-1]
    assert domain == DOMAIN
    assert issue_id == ISSUE_BATTERY_PROFILE_PENDING
    assert payload["translation_placeholders"]["pending_timeout_minutes"] == "15"

    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord._sync_battery_profile_pending_issue()  # noqa: SLF001
    assert (DOMAIN, ISSUE_BATTERY_PROFILE_PENDING) in mock_issue_registry.deleted


@pytest.mark.asyncio
async def test_battery_profile_write_lock_blocks_parallel_updates(
    coordinator_factory,
) -> None:
    import asyncio

    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    await coord._battery_profile_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord._async_apply_battery_profile(  # noqa: SLF001
                profile="self-consumption",
                reserve=20,
            )
    finally:
        coord._battery_profile_write_lock.release()  # noqa: SLF001

    # Concurrent writes: second caller should be rejected while first is in flight.
    gate = asyncio.Event()
    coord._battery_profile_last_write_mono = None  # noqa: SLF001

    async def _slow_set(**_kwargs):
        await gate.wait()
        return {"message": "success"}

    coord.client.set_battery_profile = AsyncMock(side_effect=_slow_set)
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    task1 = asyncio.create_task(
        coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=20,
        )
    )
    await asyncio.sleep(0)
    with pytest.raises(ServiceValidationError, match="already in progress|too quickly"):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="cost_savings",
            reserve=30,
        )
    gate.set()
    await task1
    assert coord.client.set_battery_profile.await_count == 1


@pytest.mark.asyncio
async def test_battery_profile_write_debounce_applies_to_set_and_cancel(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.client.cancel_battery_profile_update = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord._battery_profile_last_write_mono = time.monotonic()  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="too quickly"):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=20,
        )

    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="too quickly"):
        await coord.async_cancel_pending_profile_change()

    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    await coord.async_cancel_pending_profile_change()
    coord.client.cancel_battery_profile_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_site_only_update_refreshes_battery_profile_and_settings(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock()  # noqa: SLF001
    coord.client.battery_site_settings = AsyncMock(
        return_value={
            "data": {
                "showChargeFromGrid": True,
                "showSavingsMode": True,
                "showFullBackup": True,
                "showBatteryBackupPercentage": True,
                "hasEncharge": True,
            }
        }
    )
    coord.client.storm_guard_profile = AsyncMock(
        return_value=_profile_payload(profile="self-consumption", reserve=20)
    )
    coord.client.storm_guard_alert = AsyncMock(
        return_value={"criticalAlertActive": False, "stormAlerts": []}
    )

    result = await coord._async_update_data()  # noqa: SLF001

    assert result == {}
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_profile_option_keys == [
        "self-consumption",
        "cost_savings",
        "backup_only",
    ]
    # Stale caches should keep site-only updates stable without extra fetches.
    coord._battery_site_settings_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord._storm_guard_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord._storm_alert_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord.client.battery_site_settings.reset_mock()
    coord.client.storm_guard_profile.reset_mock()
    coord.client.storm_guard_alert.reset_mock()
    result_cached = await coord._async_update_data()  # noqa: SLF001
    assert result_cached == {}
    coord.client.battery_site_settings.assert_not_called()
    coord.client.storm_guard_profile.assert_not_called()
    coord.client.storm_guard_alert.assert_not_called()
    assert coord.battery_controls_available is True


@pytest.mark.asyncio
async def test_set_battery_reserve_rejects_full_backup(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_backup_percentage = 100  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="fixed at 100%"):
        await coord.async_set_battery_reserve(50)


@pytest.mark.asyncio
async def test_battery_profile_write_blocked_for_read_only_user(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})

    assert coord.battery_reserve_editable is False
    with pytest.raises(ServiceValidationError, match="not permitted"):
        await coord.async_set_battery_reserve(30)
    coord.client.set_battery_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_profile_forbidden_translates_to_validation_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="HTTP 403 Forbidden"):
        await coord.async_set_battery_reserve(30)


@pytest.mark.asyncio
async def test_battery_profile_forbidden_read_only_user_translates_to_permission_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="not permitted"):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=30,
        )


@pytest.mark.asyncio
async def test_battery_profile_forbidden_after_permission_change_returns_permission_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001

    async def _forbidden_after_role_change(**_kwargs):
        coord._battery_user_is_owner = False  # noqa: SLF001
        coord._battery_user_is_installer = False  # noqa: SLF001
        raise aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )

    coord.client.set_battery_profile = AsyncMock(
        side_effect=_forbidden_after_role_change
    )

    with pytest.raises(ServiceValidationError, match="not permitted"):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=30,
        )


@pytest.mark.asyncio
async def test_battery_profile_unauthorized_translates_to_reauth_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=30,
        )


@pytest.mark.asyncio
async def test_battery_profile_unexpected_http_error_reraises(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=30,
        )


def test_battery_profile_property_helpers_cover_branches(coordinator_factory) -> None:
    coord = coordinator_factory()

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    assert coord._normalize_battery_profile_key(BadStr()) is None  # noqa: SLF001
    assert coord._battery_profile_label(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_battery_sub_type(BadStr()) is None  # noqa: SLF001
    assert coord._coerce_optional_int(BadStr()) is None  # noqa: SLF001

    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord._battery_backup_percentage = 33  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_reserve = 22  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001

    assert coord.battery_pending_requested_at is not None
    assert coord.battery_effective_backup_percentage == 33
    assert coord.battery_effective_operation_mode_sub_type == "prioritize-energy"
    assert coord.battery_pending_operation_mode_sub_type == "prioritize-energy"
    assert coord.battery_has_encharge is True
    assert coord.battery_show_battery_backup_percentage is True
    assert "cost_savings" in coord.battery_profile_option_keys
    assert coord.battery_profile_display == "Savings"
    assert coord.battery_effective_profile_display == "Self-Consumption"
    assert coord.savings_use_battery_after_peak is True

    coord._battery_has_encharge = False  # noqa: SLF001
    assert coord.battery_controls_available is False
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    assert coord.savings_use_battery_after_peak is None
    coord._battery_show_savings_mode = False  # noqa: SLF001
    assert coord.savings_use_battery_switch_available is False
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = False  # noqa: SLF001
    assert coord.battery_reserve_editable is False
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_pending_profile = None  # noqa: SLF001
    coord._battery_profile = None  # noqa: SLF001
    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_uses_cfg_control_when_present(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_cfg_control_show = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = False  # noqa: SLF001

    assert coord.battery_reserve_editable is True


def test_battery_reserve_editable_honors_cfg_control_false(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_cfg_control_show = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_pending_age_handles_datetime_failures(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory()
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001

    monkeypatch.setattr(
        coord_mod.dt_util,
        "utcnow",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert coord.battery_pending_age_seconds is None


def test_additional_battery_and_storm_property_helpers(coordinator_factory) -> None:
    coord = coordinator_factory()

    coord._battery_feature_details = "bad"  # noqa: SLF001
    assert coord.battery_feature_details == {}

    coord._battery_site_status_text = "Normal"  # noqa: SLF001
    coord._battery_site_status_severity = "warning"  # noqa: SLF001
    assert coord.battery_site_status_text == "Normal"
    assert coord.battery_site_status_severity == "warning"

    coord._storm_alerts = "bad"  # noqa: SLF001
    assert coord.storm_alerts == []
    assert coord.battery_profile_polling_interval is None
    assert coord.battery_profile_evse_device is None

    coord._battery_profile_evse_device = {"uuid": "evse-1"}  # noqa: SLF001
    snapshot = coord.battery_profile_evse_device
    assert snapshot == {"uuid": "evse-1"}
    snapshot["uuid"] = "mutated"
    assert coord._battery_profile_evse_device["uuid"] == "evse-1"  # noqa: SLF001


def test_battery_pending_match_and_memory_branches(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is False  # noqa: SLF001

    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_backup_percentage = 25  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is False  # noqa: SLF001

    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "other"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is False  # noqa: SLF001

    coord._battery_pending_sub_type = None  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is False  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "custom-backend-default"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is True  # noqa: SLF001

    coord._battery_pending_sub_type = "regional-saving"  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "another-regional-saving"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is False  # noqa: SLF001

    coord._remember_battery_reserve(None, 20)  # noqa: SLF001

    class BadProfile:
        def __str__(self):
            raise ValueError("boom")

    coord._remember_battery_reserve(BadProfile(), 20)  # noqa: SLF001
    coord._remember_battery_reserve("regional_special", 20)  # noqa: SLF001


def test_pending_profile_issue_noop_when_already_reported(
    coordinator_factory, mock_issue_registry
) -> None:
    from custom_components.enphase_ev.const import BATTERY_PROFILE_PENDING_TIMEOUT_S

    coord = coordinator_factory()
    coord._battery_profile_issue_reported = True  # noqa: SLF001
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(
        timezone.utc
    ) - timedelta(  # noqa: SLF001
        seconds=BATTERY_PROFILE_PENDING_TIMEOUT_S + 60
    )

    coord._sync_battery_profile_pending_issue()  # noqa: SLF001

    assert mock_issue_registry.created == []
    assert mock_issue_registry.deleted == []


@pytest.mark.asyncio
async def test_battery_profile_setter_validation_and_fallbacks(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.client.battery_site_settings = AsyncMock(return_value={"data": {}})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord._async_apply_battery_profile(profile="", reserve=10)  # noqa: SLF001

    coord._battery_site_settings_cache_until = 10**12  # noqa: SLF001
    await coord._async_refresh_battery_site_settings()  # noqa: SLF001
    coord.client.battery_site_settings.assert_not_called()

    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord.async_set_system_profile("")

    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="not supported"):
        await coord.async_set_system_profile("cost_savings")

    coord._battery_profile = None  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord.async_set_battery_reserve(30)

    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_backup_percentage = 25  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    await coord.async_set_battery_reserve(5)
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["battery_backup_percentage"] == 5
    assert kwargs["operation_mode_sub_type"] == "prioritize-energy"

    coord._clear_battery_pending()  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="Savings profile must be active"):
        await coord.async_set_savings_use_battery_after_peak(False)

    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_backup_percentage = None  # noqa: SLF001
    coord._battery_profile_reserve_memory.pop("cost_savings", None)  # noqa: SLF001
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord.client.set_battery_profile.reset_mock()
    await coord.async_set_savings_use_battery_after_peak(True)
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["battery_backup_percentage"] == 20


def test_profile_only_pending_match_allows_reserve_drift(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_sub_type = None  # noqa: SLF001
    coord._battery_pending_require_exact_settings = False  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 45  # noqa: SLF001

    assert coord._effective_profile_matches_pending() is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_payload_snapshots_are_saved_and_redacted(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_site_settings = AsyncMock(
        return_value={"data": {"showSavingsMode": True}, "userId": "123"}
    )
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "stormGuardState": "enabled",
            },
            "token": "secret-token",
        }
    )

    await coord._async_refresh_battery_site_settings(force=True)  # noqa: SLF001
    await coord._async_refresh_storm_guard_profile(force=True)  # noqa: SLF001

    assert coord._battery_site_settings_payload is not None  # noqa: SLF001
    assert (
        coord._battery_site_settings_payload["userId"] == "[redacted]"
    )  # noqa: SLF001
    assert coord._battery_profile_payload is not None  # noqa: SLF001
    assert coord._battery_profile_payload["token"] == "[redacted]"  # noqa: SLF001
    nested = {
        "userId": "123",
        "device_link": "https://enlighten.example/systems/9990001/envoys/200001",
        "connection_details": {
            "interface_ip": {"ethernet": "192.0.2.10"},
        },
        "nested": {
            "Authorization": "Bearer abc",
            "X-XSRF-Token": "xsrf",
            "refresh-token": "refresh",
            "items": [
                {"cookie": "a=b"},
                {"username": "user@example.com"},
                {
                    "default_route": "192.168.1.1 (Ethernet)",
                    "mac_addr": "00:11:22:33:44:55",
                    "ip_addr": "192.0.2.10",
                    "gateway_ip_addr": "192.0.2.1",
                },
                {"safe": "ok"},
            ],
        },
    }
    redacted_nested = coord._redact_battery_payload(nested)  # noqa: SLF001
    assert redacted_nested["userId"] == "[redacted]"
    assert redacted_nested["nested"]["Authorization"] == "[redacted]"
    assert redacted_nested["nested"]["X-XSRF-Token"] == "[redacted]"
    assert redacted_nested["nested"]["refresh-token"] == "[redacted]"
    assert redacted_nested["device_link"] == "[redacted]"
    assert redacted_nested["connection_details"]["interface_ip"] == "[redacted]"
    assert redacted_nested["nested"]["items"][0]["cookie"] == "[redacted]"
    assert redacted_nested["nested"]["items"][1]["username"] == "[redacted]"
    assert redacted_nested["nested"]["items"][2]["default_route"] == "[redacted]"
    assert redacted_nested["nested"]["items"][2]["mac_addr"] == "[redacted]"
    assert redacted_nested["nested"]["items"][2]["ip_addr"] == "[redacted]"
    assert redacted_nested["nested"]["items"][2]["gateway_ip_addr"] == "[redacted]"
    assert redacted_nested["nested"]["items"][3]["safe"] == "ok"


@pytest.mark.asyncio
async def test_battery_payload_snapshots_wrap_non_dict_payloads(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_site_settings = AsyncMock(return_value=["unexpected"])
    coord.client.storm_guard_profile = AsyncMock(return_value=["unexpected"])

    await coord._async_refresh_battery_site_settings(force=True)  # noqa: SLF001
    await coord._async_refresh_storm_guard_profile(force=True)  # noqa: SLF001

    assert coord._battery_site_settings_payload == {
        "value": ["unexpected"]
    }  # noqa: SLF001
    assert coord._battery_profile_payload == {"value": ["unexpected"]}  # noqa: SLF001
