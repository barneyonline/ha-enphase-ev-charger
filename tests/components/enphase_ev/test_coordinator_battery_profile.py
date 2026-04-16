from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call

import aiohttp
import pytest

from custom_components.enphase_ev.const import FAST_TOGGLE_POLL_HOLD_S
from custom_components.enphase_ev.state_models import BatteryControlCapability


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
                "showAiOptiSavingsMode": True,
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
    assert coord.battery_profile_option_keys == [
        "self-consumption",
        "cost_savings",
        "ai_optimisation",
    ]


@pytest.mark.asyncio
async def test_refresh_battery_site_settings_parses_ai_optimisation_flag(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_site_settings = AsyncMock(
        return_value={
            "data": {
                "showChargeFromGrid": True,
                "showSavingsMode": False,
                "showAiOptiSavingsMode": True,
                "isEmea": False,
                "showFullBackup": True,
                "showBatteryBackupPercentage": True,
                "hasEncharge": True,
                "hasEnpower": True,
                "userDetails": {"isOwner": True, "isInstaller": False},
            }
        }
    )

    await coord._async_refresh_battery_site_settings(force=True)  # noqa: SLF001

    assert coord._battery_show_ai_optimisation_mode is True  # noqa: SLF001
    assert coord.battery_is_emea is False
    assert coord.battery_profile_option_keys == [
        "self-consumption",
        "ai_optimisation",
        "backup_only",
    ]
    assert coord.battery_profile_option_labels["ai_optimisation"] == "AI Optimisation"


@pytest.mark.asyncio
async def test_set_system_profile_uses_remembered_reserve(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_ai_opti_savings_mode = True  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_profile_reserve_memory["cost_savings"] = 35  # noqa: SLF001
    coord._battery_profile_reserve_memory["ai_optimisation"] = 12  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})

    await coord.battery_runtime.async_set_system_profile("cost_savings")

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
    await coord.battery_runtime.async_set_system_profile("regional_special")
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["profile"] == "regional_special"
    assert kwargs["battery_backup_percentage"] == 20

    coord.client.set_battery_profile.reset_mock()
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    await coord.battery_runtime.async_set_system_profile("ai_optimisation")
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["profile"] == "ai_optimisation"
    assert kwargs["battery_backup_percentage"] == 12
    assert kwargs["operation_mode_sub_type"] == "prioritize-energy"


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
                "showAiOptiSavingsMode": True,
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
        "ai_optimisation",
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
async def test_battery_profile_write_refreshes_permission_when_role_unknown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord.client.battery_site_settings = AsyncMock(
        side_effect=lambda: {
            "data": {"userDetails": {"isOwner": True, "isInstaller": False}}
        }
    )
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    await coord.async_set_battery_reserve(30)

    coord.client.battery_site_settings.assert_awaited_once()
    coord.client.set_battery_profile.assert_awaited_once()


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
async def test_battery_profile_modern_inventory_uses_official_web_lean_shape(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [
                    {
                        "serial_number": "BAT-5P",
                        "name": "IQ Battery 5P",
                        "sku_id": "B05-T02-US00-1-3",
                    }
                ],
            }
        },
        ["encharge"],
    )
    coord.client._acquire_xsrf_token = AsyncMock(return_value="cfg-token")
    coord.client._battery_config_auth_context = MagicMock(
        return_value=("bearer-token", "88")
    )
    coord.client._battery_config_params = MagicMock(
        return_value={"userId": "88", "source": "enho"}
    )
    coord.client._xsrf_token = MagicMock(return_value="cfg-token")
    coord.client._h = {"User-Agent": "Mozilla/5.0"}
    coord.client._cookie = "session=abc; BP-XSRF-Token=stale"
    coord.client._json = AsyncMock(return_value={"message": "success"})

    await coord.async_set_battery_reserve(30)

    coord.client._acquire_xsrf_token.assert_awaited_once_with("cfg")
    coord.client._json.assert_awaited_once()
    args, kwargs = coord.client._json.await_args
    assert args[0] == "PUT"
    assert "batteryConfig/api/v1/profile/" in args[1]
    assert kwargs["params"] == {"userId": "88", "source": "enho"}
    assert kwargs["headers"]["Authorization"] == "Bearer bearer-token"
    assert kwargs["headers"]["e-auth-token"] == "EAUTH"
    assert kwargs["headers"]["Username"] == "88"
    assert kwargs["headers"]["X-XSRF-Token"] == "cfg-token"
    assert kwargs["headers"]["X-CSRF-Token"] == "cfg-token"
    assert kwargs["headers"]["Cookie"] == "session=abc; BP-XSRF-Token=cfg-token"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["debug_auth_source"] == "official_web_lean"


@pytest.mark.asyncio
async def test_battery_profile_legacy_inventory_uses_external_compatible_shape(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [
                    {
                        "serial_number": "BAT-3T",
                        "name": "IQ Battery 3T",
                        "sku_id": "IQ-BAT-3T",
                    }
                ],
            }
        },
        ["encharge"],
    )
    coord.client._acquire_xsrf_token = AsyncMock(return_value="cfg-token")
    coord.client._battery_config_headers = MagicMock(
        return_value={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://battery-profile-ui.enphaseenergy.com",
            "Referer": "https://battery-profile-ui.enphaseenergy.com/",
            "User-Agent": "Mozilla/5.0",
            "Authorization": None,
            "X-Requested-With": None,
            "Cookie": None,
            "e-auth-token": None,
            "X-CSRF-Token": None,
            "requestid": None,
            "X-XSRF-Token": "cfg-token",
        }
    )
    coord.client._battery_config_params = MagicMock(
        return_value={"userId": "88", "source": "enho"}
    )
    coord.client._battery_config_single_auth_token = MagicMock(
        return_value="retry-token"
    )
    coord.client._battery_config_user_id_for_token = MagicMock(return_value="88")
    coord.client._json = AsyncMock(return_value={"message": "success"})

    await coord.async_set_battery_reserve(30)

    coord.client._json.assert_awaited_once()
    args, kwargs = coord.client._json.await_args
    assert args[0] == "PUT"
    assert "batteryConfig/api/v1/profile/" in args[1]
    assert kwargs["params"] == {"userId": "88"}
    assert kwargs["headers"]["Cookie"] == "BP-XSRF-Token=cfg-token"
    assert kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert kwargs["headers"]["e-auth-token"] == "retry-token"
    assert kwargs["headers"]["Username"] == "88"
    assert "source" not in kwargs["params"]
    assert kwargs["debug_auth_source"] == "external_compatible_profile_write"


@pytest.mark.asyncio
async def test_battery_profile_unknown_inventory_fallbacks_and_caches_external_compat(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.client._acquire_xsrf_token = AsyncMock(return_value="cfg-token")
    coord.client._battery_config_headers = MagicMock(
        return_value={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://battery-profile-ui.enphaseenergy.com",
            "Referer": "https://battery-profile-ui.enphaseenergy.com/",
            "User-Agent": "Mozilla/5.0",
            "Authorization": None,
            "X-Requested-With": None,
            "Cookie": None,
            "e-auth-token": None,
            "X-CSRF-Token": None,
            "requestid": None,
            "X-XSRF-Token": "cfg-token",
        }
    )
    coord.client._battery_config_params = MagicMock(
        return_value={"userId": "88", "source": "enho"}
    )
    coord.client._battery_config_single_auth_token = MagicMock(
        return_value="retry-token"
    )
    coord.client._battery_config_user_id_for_token = MagicMock(return_value="88")
    calls: list[str] = []

    async def _json_side_effect(*_args, **kwargs):
        calls.append(kwargs["debug_auth_source"])
        if kwargs["debug_auth_source"] == "official_web_lean" and len(calls) == 1:
            raise aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=403,
                message="Forbidden",
            )
        return {"message": "success"}

    coord.client._json = AsyncMock(side_effect=_json_side_effect)

    await coord.async_set_battery_reserve(30)

    assert calls == [
        "official_web_lean",
        "external_compatible_profile_retry",
    ]
    assert (
        coord.battery_runtime._battery_profile_write_mode_cache == "external_compatible"
    )

    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    await coord.async_set_battery_reserve(31)

    assert calls == [
        "official_web_lean",
        "external_compatible_profile_retry",
        "external_compatible_profile_cached",
    ]


@pytest.mark.asyncio
async def test_battery_profile_modern_inventory_fallbacks_and_caches_external_compat(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [
                    {
                        "serial_number": "BAT-5P",
                        "name": "IQ Battery 5P",
                        "sku_id": "B05-T02-ROW00-1-2",
                    }
                ],
            }
        },
        ["encharge"],
    )
    coord.client._acquire_xsrf_token = AsyncMock(return_value="cfg-token")
    coord.client._battery_config_headers = MagicMock(
        return_value={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://battery-profile-ui.enphaseenergy.com",
            "Referer": "https://battery-profile-ui.enphaseenergy.com/",
            "User-Agent": "Mozilla/5.0",
            "Authorization": None,
            "X-Requested-With": None,
            "Cookie": None,
            "e-auth-token": None,
            "X-CSRF-Token": None,
            "requestid": None,
            "X-XSRF-Token": "cfg-token",
        }
    )
    coord.client._battery_config_params = MagicMock(
        return_value={"userId": "88", "source": "enho"}
    )
    coord.client._battery_config_single_auth_token = MagicMock(
        return_value="retry-token"
    )
    coord.client._battery_config_user_id_for_token = MagicMock(return_value="88")
    calls: list[str] = []

    async def _json_side_effect(*_args, **kwargs):
        calls.append(kwargs["debug_auth_source"])
        if kwargs["debug_auth_source"] == "official_web_lean":
            raise aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=403,
                message="Forbidden",
            )
        return {"message": "success"}

    coord.client._json = AsyncMock(side_effect=_json_side_effect)

    await coord.async_set_battery_reserve(30)

    assert calls == [
        "official_web_lean",
        "external_compatible_profile_retry",
    ]
    assert (
        coord.battery_runtime._battery_profile_write_mode_cache == "external_compatible"
    )

    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    await coord.async_set_battery_reserve(31)

    assert calls == [
        "official_web_lean",
        "external_compatible_profile_retry",
        "external_compatible_profile_cached",
    ]


@pytest.mark.asyncio
async def test_battery_profile_legacy_inventory_fallbacks_and_caches_official_web_lean(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"name": "IQ Battery 3T", "sku_id": "IQ-BAT-3T"}],
            }
        },
        ["encharge"],
    )
    coord.client._acquire_xsrf_token = AsyncMock(return_value="cfg-token")
    coord.client._battery_config_headers = MagicMock(
        return_value={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://battery-profile-ui.enphaseenergy.com",
            "Referer": "https://battery-profile-ui.enphaseenergy.com/",
            "User-Agent": "Mozilla/5.0",
            "Authorization": None,
            "X-Requested-With": None,
            "Cookie": None,
            "e-auth-token": None,
            "X-CSRF-Token": None,
            "requestid": None,
            "X-XSRF-Token": "cfg-token",
        }
    )
    coord.client._battery_config_params = MagicMock(
        return_value={"userId": "88", "source": "enho"}
    )
    coord.client._battery_config_single_auth_token = MagicMock(
        return_value="retry-token"
    )
    coord.client._battery_config_user_id_for_token = MagicMock(return_value="88")
    calls: list[str] = []

    async def _json_side_effect(*_args, **kwargs):
        calls.append(kwargs["debug_auth_source"])
        if kwargs["debug_auth_source"] == "external_compatible_profile_write":
            raise aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=403,
                message="Forbidden",
            )
        return {"message": "success"}

    coord.client._json = AsyncMock(side_effect=_json_side_effect)

    await coord.async_set_battery_reserve(30)

    assert calls == [
        "external_compatible_profile_write",
        "official_web_lean_retry",
    ]
    assert (
        coord.battery_runtime._battery_profile_write_mode_cache == "official_web_lean"
    )

    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    await coord.async_set_battery_reserve(31)

    assert calls == [
        "external_compatible_profile_write",
        "official_web_lean_retry",
        "official_web_lean_cached",
    ]


@pytest.mark.asyncio
async def test_battery_profile_false_result_from_preferred_mode_triggers_fallback(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"name": "IQ Battery 3T", "sku_id": "IQ-BAT-3T"}],
            }
        },
        ["encharge"],
    )
    coord.battery_runtime._async_write_battery_profile = AsyncMock(  # noqa: SLF001
        side_effect=[False, True]
    )

    await coord.async_set_battery_reserve(30)

    assert (
        coord.battery_runtime._async_write_battery_profile.await_args_list
        == [  # noqa: SLF001
            call(
                profile="self-consumption",
                reserve=30,
                sub_type=None,
                devices=None,
                mode="external_compatible",
                debug_auth_source="external_compatible_profile_write",
            ),
            call(
                profile="self-consumption",
                reserve=30,
                sub_type=None,
                devices=None,
                mode="official_web_lean",
                debug_auth_source="official_web_lean_retry",
            ),
        ]
    )
    assert (
        coord.battery_runtime._battery_profile_write_mode_cache == "official_web_lean"
    )


@pytest.mark.asyncio
async def test_battery_profile_retries_without_devices_before_switching_modes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "enable": False, "chargeMode": "MANUAL"}
    ]
    coord.battery_runtime._async_write_battery_profile = AsyncMock(  # noqa: SLF001
        side_effect=[
            aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=403,
                message="Forbidden",
            ),
            True,
        ]
    )

    await coord.async_set_battery_reserve(30)

    assert (
        coord.battery_runtime._async_write_battery_profile.await_args_list
        == [  # noqa: SLF001
            call(
                profile="self-consumption",
                reserve=30,
                sub_type=None,
                devices=[
                    {
                        "uuid": "evse-1",
                        "deviceType": "iqEvse",
                        "enable": False,
                        "chargeMode": "MANUAL",
                    }
                ],
                mode="official_web_lean",
                debug_auth_source="official_web_lean",
            ),
            call(
                profile="self-consumption",
                reserve=30,
                sub_type=None,
                devices=None,
                mode="official_web_lean",
                debug_auth_source="official_web_lean_without_devices",
            ),
        ]
    )
    assert coord.battery_runtime._battery_profile_write_without_devices_cache is True


@pytest.mark.asyncio
async def test_battery_profile_cached_without_devices_skips_device_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "enable": False, "chargeMode": "MANUAL"}
    ]
    coord.battery_runtime._battery_profile_write_without_devices_cache = (
        True  # noqa: SLF001
    )
    coord.battery_runtime._async_write_battery_profile = AsyncMock(  # noqa: SLF001
        return_value=True
    )

    await coord.async_set_battery_reserve(30)

    assert (
        coord.battery_runtime._async_write_battery_profile.await_args_list
        == [  # noqa: SLF001
            call(
                profile="self-consumption",
                reserve=30,
                sub_type=None,
                devices=None,
                mode="official_web_lean",
                debug_auth_source="official_web_lean",
            )
        ]
    )
    assert coord.battery_runtime._battery_profile_write_without_devices_cache is True


@pytest.mark.asyncio
async def test_battery_profile_uses_fallback_mode_without_devices_after_retry_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "enable": False, "chargeMode": "MANUAL"}
    ]
    coord.battery_runtime._async_write_battery_profile = AsyncMock(  # noqa: SLF001
        side_effect=[
            aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=403,
                message="Forbidden",
            ),
            aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=403,
                message="Forbidden",
            ),
            True,
        ]
    )

    await coord.async_set_battery_reserve(30)

    assert (
        coord.battery_runtime._async_write_battery_profile.await_args_list
        == [  # noqa: SLF001
            call(
                profile="self-consumption",
                reserve=30,
                sub_type=None,
                devices=[
                    {
                        "uuid": "evse-1",
                        "deviceType": "iqEvse",
                        "enable": False,
                        "chargeMode": "MANUAL",
                    }
                ],
                mode="official_web_lean",
                debug_auth_source="official_web_lean",
            ),
            call(
                profile="self-consumption",
                reserve=30,
                sub_type=None,
                devices=None,
                mode="official_web_lean",
                debug_auth_source="official_web_lean_without_devices",
            ),
            call(
                profile="self-consumption",
                reserve=30,
                sub_type=None,
                devices=None,
                mode="external_compatible",
                debug_auth_source="external_compatible_profile_retry",
            ),
        ]
    )


@pytest.mark.asyncio
async def test_battery_profile_forbidden_without_compat_prereqs_still_raises(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord.client._acquire_xsrf_token = AsyncMock(return_value="cfg-token")
    coord.client._battery_config_headers = MagicMock(
        return_value={"X-XSRF-Token": "cfg-token"}
    )
    coord.client._battery_config_single_auth_token = MagicMock(return_value=None)
    coord.client._battery_config_user_id_for_token = MagicMock(return_value=None)
    coord.client._battery_config_params = MagicMock(
        return_value={"userId": "88", "source": "enho"}
    )
    coord.client._json = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="HTTP 403 Forbidden"):
        await coord.async_set_battery_reserve(30)

    coord.client._json.assert_awaited_once()


@pytest.mark.asyncio
async def test_battery_profile_legacy_mode_without_compat_helpers_raises(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"name": "IQ Battery 3T", "sku_id": "IQ-BAT-3T"}],
            }
        },
        ["encharge"],
    )
    coord.client._json = None
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


def test_battery_profile_write_mode_from_inventory_handles_invalid_shapes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.inventory_view.type_bucket = None
    assert coord.battery_runtime._battery_profile_write_mode_from_inventory() is None

    coord.inventory_view.type_bucket = lambda *_args: ["bad"]
    assert coord.battery_runtime._battery_profile_write_mode_from_inventory() is None

    coord.inventory_view.type_bucket = lambda *_args: {"devices": []}
    assert coord.battery_runtime._battery_profile_write_mode_from_inventory() is None

    coord.inventory_view.type_bucket = lambda *_args: {"devices": [None]}
    assert coord.battery_runtime._battery_profile_write_mode_from_inventory() is None


def test_battery_profile_write_mode_from_inventory_returns_none_for_mixed_markers(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.inventory_view.type_bucket = lambda *_args: {
        "devices": [
            {"name": "IQ Battery 5P", "sku_id": "IQ-BAT-3T"},
        ]
    }

    assert coord.battery_runtime._battery_profile_write_mode_from_inventory() is None


def test_cache_battery_profile_write_mode_clears_when_it_matches_inventory(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime._battery_profile_write_mode_cache = "external_compatible"

    coord.battery_runtime._cache_battery_profile_write_mode(
        "official_web_lean", inventory_mode="official_web_lean"
    )

    assert coord.battery_runtime._battery_profile_write_mode_cache is None


def test_cache_battery_profile_write_mode_keeps_override_when_it_differs_from_inventory(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime._cache_battery_profile_write_mode(
        "external_compatible", inventory_mode="official_web_lean"
    )

    assert (
        coord.battery_runtime._battery_profile_write_mode_cache == "external_compatible"
    )


def test_battery_profile_write_override_returns_none_without_instance_dict(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class SlotClient:
        __slots__ = ()

    coord.client = SlotClient()

    assert coord.battery_runtime._battery_profile_write_override() is None


@pytest.mark.asyncio
async def test_fetch_legacy_battery_config_jwt_returns_token(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class CookieJar:
        def __init__(self) -> None:
            self.cookies: dict[str, str] | None = None

        def update_cookies(self, cookies, response_url=None) -> None:
            self.cookies = dict(cookies)

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def json(self):
            return {"token": "legacy-token"}

    class Session:
        def __init__(self) -> None:
            self.cookie_jar = CookieJar()
            self.calls: list[tuple[str, str, dict[str, str]]] = []

        def request(self, method, url, headers=None):
            self.calls.append((method, url, dict(headers or {})))
            return Response()

    coord.client._s = Session()
    coord.client._cookie = "session=abc; BP-XSRF-Token=cfg-token"
    coord.client._timeout = 1

    token = await coord.battery_runtime._async_fetch_legacy_battery_config_jwt()

    assert token == "legacy-token"
    assert coord.client._s.calls == [
        (
            "GET",
            "https://enlighten.enphaseenergy.com/app-api/jwt_token.json",
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://enlighten.enphaseenergy.com/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.3.1 Safari/605.1.15",
                "Cookie": "session=abc; BP-XSRF-Token=cfg-token",
            },
        )
    ]
    assert coord.client._s.cookie_jar.cookies == {
        "session": "abc",
        "BP-XSRF-Token": "cfg-token",
    }


@pytest.mark.asyncio
async def test_fetch_legacy_battery_config_jwt_returns_none_for_http_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class Response:
        status = 403

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def json(self):
            raise AssertionError("json should not be called")

    class Session:
        cookie_jar = None

        def request(self, *_args, **_kwargs):
            return Response()

    coord.client._s = Session()
    coord.client._cookie = None
    coord.client._timeout = 1

    token = await coord.battery_runtime._async_fetch_legacy_battery_config_jwt()

    assert token is None


@pytest.mark.asyncio
async def test_fetch_legacy_battery_config_jwt_returns_none_on_request_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class Session:
        cookie_jar = None

        def request(self, *_args, **_kwargs):
            raise TypeError("boom")

    coord.client._s = Session()
    coord.client._cookie = None
    coord.client._timeout = 1

    token = await coord.battery_runtime._async_fetch_legacy_battery_config_jwt()

    assert token is None


@pytest.mark.asyncio
async def test_fetch_legacy_battery_config_jwt_returns_none_for_non_dict_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def json(self):
            return []

    class Session:
        cookie_jar = None

        def request(self, *_args, **_kwargs):
            return Response()

    coord.client._s = Session()
    coord.client._cookie = None
    coord.client._timeout = 1

    token = await coord.battery_runtime._async_fetch_legacy_battery_config_jwt()

    assert token is None


@pytest.mark.asyncio
async def test_fetch_legacy_battery_config_jwt_returns_none_without_token(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def json(self):
            return {}

    class Session:
        cookie_jar = None

        def request(self, *_args, **_kwargs):
            return Response()

    coord.client._s = Session()
    coord.client._cookie = None
    coord.client._timeout = 1

    token = await coord.battery_runtime._async_fetch_legacy_battery_config_jwt()

    assert token is None


@pytest.mark.asyncio
async def test_write_battery_profile_external_compat_returns_false_without_helpers(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client._json = None

    result = await coord.battery_runtime._async_write_battery_profile_external_compat(
        profile="self-consumption",
        reserve=30,
        sub_type=None,
        devices=None,
        debug_auth_source="external_compatible_profile_write",
    )

    assert result is False


@pytest.mark.asyncio
async def test_write_battery_profile_official_web_lean_falls_back_to_client_method(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    calls: list[dict[str, object]] = []

    class Client:
        _acquire_xsrf_token = None
        _battery_config_headers = None
        _battery_config_params = None
        _json = None

        async def set_battery_profile(self, **kwargs):
            calls.append(dict(kwargs))
            return {"message": "success"}

    coord.client = Client()

    await coord.battery_runtime._async_write_battery_profile_official_web_lean(
        profile="self-consumption",
        reserve=30,
        sub_type=None,
        devices=None,
        debug_auth_source="official_web_lean",
    )

    assert calls == [
        {
            "profile": "self-consumption",
            "battery_backup_percentage": 30,
            "operation_mode_sub_type": None,
            "devices": None,
        }
    ]


@pytest.mark.asyncio
async def test_write_battery_profile_official_web_lean_uses_bearer_when_eauth_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class Client:
        _h = {"User-Agent": "Mozilla/5.0"}
        _cookie = "session=abc; BP-XSRF-Token=stale"
        _eauth = None
        _acquire_xsrf_token = AsyncMock(return_value="cfg-token")
        _battery_config_auth_context = MagicMock(return_value=("bearer-token", "88"))
        _battery_config_params = MagicMock(
            return_value={"userId": "88", "source": "enho"}
        )
        _xsrf_token = MagicMock(return_value="cfg-token")
        _json = AsyncMock(return_value={"message": "success"})

    coord.client = Client()

    await coord.battery_runtime._async_write_battery_profile_official_web_lean(
        profile="self-consumption",
        reserve=30,
        sub_type=None,
        devices=None,
        debug_auth_source="official_web_lean",
    )

    kwargs = coord.client._json.await_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer bearer-token"
    assert kwargs["headers"]["e-auth-token"] == "bearer-token"


@pytest.mark.asyncio
async def test_write_battery_profile_external_compat_uses_instance_override(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})

    result = await coord.battery_runtime._async_write_battery_profile_external_compat(
        profile="self-consumption",
        reserve=30,
        sub_type=None,
        devices=None,
        debug_auth_source="external_compatible_profile_write",
    )

    assert result is True
    coord.client.set_battery_profile.assert_awaited_once_with(
        profile="self-consumption",
        battery_backup_percentage=30,
        operation_mode_sub_type=None,
        devices=None,
    )


@pytest.mark.asyncio
async def test_write_battery_profile_external_compat_includes_subtype_and_devices(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client._acquire_xsrf_token = AsyncMock(return_value="cfg-token")
    coord.client._battery_config_headers = MagicMock(
        return_value={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://battery-profile-ui.enphaseenergy.com",
            "Referer": "https://battery-profile-ui.enphaseenergy.com/",
            "User-Agent": "Mozilla/5.0",
            "Authorization": None,
            "X-Requested-With": None,
            "Cookie": None,
            "e-auth-token": None,
            "X-CSRF-Token": None,
            "requestid": None,
            "X-XSRF-Token": "cfg-token",
        }
    )
    coord.client._battery_config_params = MagicMock(
        return_value={"userId": "88", "source": "enho"}
    )
    coord.client._battery_config_single_auth_token = MagicMock(
        return_value="retry-token"
    )
    coord.client._battery_config_user_id_for_token = MagicMock(return_value="88")
    coord.client._json = AsyncMock(return_value={"message": "success"})

    result = await coord.battery_runtime._async_write_battery_profile_external_compat(
        profile="cost_savings",
        reserve=15,
        sub_type="prioritize-energy",
        devices=[{"uuid": "battery-1"}],
        debug_auth_source="external_compatible_profile_write",
    )

    assert result is True
    payload = coord.client._json.await_args.kwargs["json"]
    assert payload["operationModeSubType"] == "prioritize-energy"
    assert payload["devices"] == [{"uuid": "battery-1"}]


@pytest.mark.asyncio
async def test_write_battery_profile_external_compat_prefers_legacy_jwt_token(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client._h = {"User-Agent": "Mozilla/5.0"}
    coord.client._acquire_xsrf_token = AsyncMock(return_value="cfg-token")
    coord.client._battery_config_headers = MagicMock(
        return_value={"X-XSRF-Token": "cfg-token"}
    )
    coord.client._battery_config_params = MagicMock(
        return_value={"userId": "88", "source": "enho"}
    )
    coord.client._battery_config_single_auth_token = MagicMock(
        return_value="retry-token"
    )
    coord.client._battery_config_user_id_for_token = MagicMock(
        return_value="legacy-user"
    )
    coord.client._json = AsyncMock(return_value={"message": "success"})
    coord.battery_runtime._async_fetch_legacy_battery_config_jwt = AsyncMock(
        return_value="legacy-token"
    )  # noqa: SLF001

    result = await coord.battery_runtime._async_write_battery_profile_external_compat(
        profile="self-consumption",
        reserve=30,
        sub_type=None,
        devices=None,
        debug_auth_source="external_compatible_profile_retry",
    )

    assert result is True
    coord.client._battery_config_single_auth_token.assert_not_called()
    coord.client._battery_config_user_id_for_token.assert_called_once_with(
        "legacy-token"
    )
    kwargs = coord.client._json.await_args.kwargs
    assert kwargs["headers"]["e-auth-token"] == "legacy-token"
    assert kwargs["params"] == {"userId": "legacy-user"}


@pytest.mark.asyncio
async def test_write_battery_profile_external_compat_omits_username_without_user_id(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client._h = {"User-Agent": "Mozilla/5.0", "Username": "stale-user"}
    coord.client._acquire_xsrf_token = AsyncMock(return_value="cfg-token")
    coord.client._battery_config_headers = MagicMock(
        return_value={"X-XSRF-Token": "cfg-token"}
    )
    coord.client._battery_config_params = MagicMock(
        return_value={"userId": "88", "source": "enho"}
    )
    coord.client._battery_config_single_auth_token = MagicMock(
        return_value="retry-token"
    )
    coord.client._battery_config_user_id_for_token = MagicMock(return_value=None)
    coord.client._json = AsyncMock(return_value={"message": "success"})
    coord.battery_runtime._async_fetch_legacy_battery_config_jwt = AsyncMock(
        return_value=None
    )  # noqa: SLF001

    result = await coord.battery_runtime._async_write_battery_profile_external_compat(
        profile="self-consumption",
        reserve=30,
        sub_type=None,
        devices=None,
        debug_auth_source="external_compatible_profile_retry",
    )

    assert result is True
    headers = coord.client._json.await_args.kwargs["headers"]
    assert "Username" not in headers


@pytest.mark.asyncio
async def test_battery_profile_forbidden_retry_error_still_translates(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord.client._acquire_xsrf_token = AsyncMock(return_value="cfg-token")
    coord.client._battery_config_headers = MagicMock(
        return_value={
            "X-XSRF-Token": "cfg-token",
            "Cookie": None,
            "X-Requested-With": None,
            "e-auth-token": None,
        }
    )
    coord.client._battery_config_single_auth_token = MagicMock(
        return_value="retry-token"
    )
    coord.client._battery_config_user_id_for_token = MagicMock(return_value="88")
    coord.client._battery_config_params = MagicMock(
        return_value={"userId": "88", "source": "enho"}
    )
    coord.client._json = AsyncMock(
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
async def test_ai_profile_write_defaults_required_subtype(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    await coord._async_apply_battery_profile(  # noqa: SLF001
        profile="ai_optimisation",
        reserve=10,
        sub_type=None,
    )

    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["profile"] == "ai_optimisation"
    assert kwargs["operation_mode_sub_type"] == "prioritize-energy"


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
    coord._battery_show_ai_opti_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001

    assert coord.battery_pending_requested_at is not None
    assert coord.battery_effective_backup_percentage == 33
    assert coord.battery_effective_operation_mode_sub_type == "prioritize-energy"
    assert coord.battery_pending_operation_mode_sub_type == "prioritize-energy"
    assert coord.battery_has_encharge is True
    assert coord.battery_show_battery_backup_percentage is True
    assert "cost_savings" in coord.battery_profile_option_keys
    assert "ai_optimisation" in coord.battery_profile_option_keys
    assert coord.battery_profile_display == "Savings"
    assert coord.battery_effective_profile_display == "Self-Consumption"
    assert coord.savings_use_battery_after_peak is True

    coord._battery_pending_profile = "ai_optimisation"  # noqa: SLF001
    assert coord.battery_selected_operation_mode_sub_type == "prioritize-energy"
    assert coord.savings_use_battery_after_peak is None

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
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    assert coord.savings_use_battery_switch_available is False


def test_battery_reserve_editable_uses_rbd_control_when_present(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=True, locked=False
    )  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = False  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_non_emea_ignores_rbd_control_false(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001

    assert coord.battery_reserve_editable is True


def test_battery_reserve_editable_prefers_reserve_flag_over_cfg_control(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_cfg_control_show = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is True


def test_battery_reserve_editable_emea_prefers_cfg_control(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = True  # noqa: SLF001
    coord._battery_cfg_control_show = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_emea_uses_rbd_when_cfg_control_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = True  # noqa: SLF001
    coord._battery_cfg_control_show = None  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_non_emea_uses_rbd_when_reserve_flag_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = None  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_non_emea_uses_cfg_when_other_flags_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = None  # noqa: SLF001
    coord._battery_cfg_control_show = False  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=None, locked=False
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_non_emea_honors_rbd_when_reserve_not_true(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = False  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_non_emea_rejects_invalid_reserve_flag_value(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = "unexpected"  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_honors_rbd_control_locked(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=True, locked=True
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_profile_selection_available_false_when_system_task_active(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_system_task = True  # noqa: SLF001

    assert coord.battery_profile_selection_available is False


@pytest.mark.asyncio
async def test_battery_profile_write_blocked_when_system_task_active(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_system_task = True  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock()

    with pytest.raises(
        ServiceValidationError, match="Battery profile updates are unavailable"
    ):
        await coord.battery_runtime.async_apply_battery_profile(
            profile="self-consumption",
            reserve=20,
        )
    coord.client.set_battery_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_profile_write_blocked_when_refresh_discovers_system_task(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord.client.battery_site_settings = AsyncMock(
        return_value={
            "data": {
                "userDetails": {"isOwner": True, "isInstaller": False},
                "systemTask": True,
            }
        }
    )
    coord.client.set_battery_profile = AsyncMock()

    with pytest.raises(
        ServiceValidationError, match="Battery profile updates are unavailable"
    ):
        await coord.battery_runtime.async_apply_battery_profile(
            profile="self-consumption",
            reserve=20,
        )
    coord.client.set_battery_profile.assert_not_awaited()


def test_battery_write_access_confirmed_requires_explicit_role(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    assert coord.battery_write_access_confirmed is False

    coord._battery_user_is_owner = True  # noqa: SLF001
    assert coord.battery_write_access_confirmed is True

    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = True  # noqa: SLF001
    assert coord.battery_write_access_confirmed is True


def test_charge_from_grid_control_unavailable_when_system_task_active(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_system_task = True  # noqa: SLF001

    assert coord.charge_from_grid_control_available is False


def test_charge_from_grid_schedule_supported_honors_schedule_supported_false(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=False,
        force_schedule_supported=True,
    )
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001

    assert coord.charge_from_grid_schedule_supported is False


def test_battery_cfg_control_enabled_falls_back_to_legacy_scalar(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        enabled=None,
    )
    coord._battery_cfg_control_enabled = False  # noqa: SLF001

    assert coord.battery_cfg_control_enabled is False


def test_charge_from_grid_force_schedule_supported_falls_back_to_schedule_id(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_schedule_id = "cfg-1"  # noqa: SLF001

    assert coord.charge_from_grid_force_schedule_supported is True


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

    coord._battery_pending_profile = "ai_optimisation"  # noqa: SLF001
    coord._battery_profile = "ai_optimisation"  # noqa: SLF001
    coord._battery_pending_reserve = 18  # noqa: SLF001
    coord._battery_backup_percentage = 18  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is True  # noqa: SLF001

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
        await coord.battery_runtime.async_set_system_profile("")

    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="not supported"):
        await coord.battery_runtime.async_set_system_profile("cost_savings")

    coord._battery_profile = None  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord.async_set_battery_reserve(30)

    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
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

    coord._battery_profile = "ai_optimisation"  # noqa: SLF001
    coord._battery_backup_percentage = 10  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord.client.set_battery_profile.reset_mock()
    await coord.async_set_battery_reserve(5)
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["battery_backup_percentage"] == 5
    assert kwargs["operation_mode_sub_type"] == "prioritize-energy"

    coord._clear_battery_pending()  # noqa: SLF001
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


def test_profile_payload_keeps_recent_local_pending_when_backend_still_mismatched(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "backup_only"  # noqa: SLF001
    coord._battery_pending_reserve = 100  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001

    coord.battery_runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "isBatteryChangePending": False,
            }
        }
    )

    assert coord.battery_profile_pending is True
    assert coord._battery_backend_profile_update_pending is False  # noqa: SLF001
    assert coord._battery_backend_not_pending_observed_at is not None  # noqa: SLF001


def test_settings_payload_clears_stale_local_pending_when_backend_change_completed(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "backup_only"  # noqa: SLF001
    coord._battery_pending_reserve = 100  # noqa: SLF001
    now = datetime.now(timezone.utc)
    coord._battery_pending_requested_at = now - timedelta(
        seconds=FAST_TOGGLE_POLL_HOLD_S + 5
    )  # noqa: SLF001
    coord._battery_backend_not_pending_observed_at = now - timedelta(
        seconds=5
    )  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001

    coord.battery_runtime.parse_battery_settings_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "isBatteryChangePending": False,
            }
        }
    )

    assert coord.battery_profile_pending is False
    assert coord._battery_backend_profile_update_pending is None  # noqa: SLF001
    assert coord._battery_backend_not_pending_observed_at is None  # noqa: SLF001


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
        "device_link": "https://enlighten.example/systems/3381244/envoys/200001",
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
