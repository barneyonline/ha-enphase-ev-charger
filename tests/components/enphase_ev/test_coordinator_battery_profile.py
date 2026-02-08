from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

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
        "devices": {"iqEvse": [{"uuid": "evse-1", "chargeMode": "MANUAL", "enable": False}]},
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
                "showChargeFromGrid": True,
                "showSavingsMode": True,
                "showFullBackup": False,
                "showBatteryBackupPercentage": True,
                "isChargingModesEnabled": True,
                "hasEncharge": True,
            }
        }
    )

    await coord._async_refresh_battery_site_settings(force=True)  # noqa: SLF001

    assert coord.battery_has_encharge is True
    assert coord.battery_is_charging_modes_enabled is True
    assert coord.battery_profile_option_keys == ["self-consumption", "cost_savings"]


def test_profile_option_passthrough_for_unknown_mode(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "regional_special"  # noqa: SLF001

    options = coord.battery_profile_option_keys
    labels = coord.battery_profile_option_labels

    assert "self-consumption" in options
    assert "regional_special" in options
    assert labels["regional_special"] == "Regional Special"


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


def test_pending_profile_clears_when_effective_state_matches(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_reserve = 22  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001

    coord._parse_battery_profile_payload(  # noqa: SLF001
        _profile_payload(
            profile="cost_savings",
            reserve=22,
            subtype="prioritize-energy",
        )
    )

    assert coord.battery_profile_pending is False
    assert coord.battery_pending_profile is None


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

    coord.client.set_battery_profile.reset_mock()
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    await coord.async_set_savings_use_battery_after_peak(False)
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["operation_mode_sub_type"] is None


@pytest.mark.asyncio
async def test_cancel_pending_profile_change(coordinator_factory) -> None:
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


def test_pending_profile_timeout_issue_lifecycle(
    coordinator_factory, mock_issue_registry
) -> None:
    from custom_components.enphase_ev.const import (
        DOMAIN,
        ISSUE_BATTERY_PROFILE_PENDING,
    )
    from custom_components.enphase_ev.coordinator import (
        BATTERY_PROFILE_PENDING_TIMEOUT_S,
    )

    coord = coordinator_factory()
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc) - timedelta(  # noqa: SLF001
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


@pytest.mark.asyncio
async def test_set_battery_reserve_rejects_full_backup(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_backup_percentage = 100  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="fixed at 100%"):
        await coord.async_set_battery_reserve(50)


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

    coord._remember_battery_reserve(None, 20)  # noqa: SLF001

    class BadProfile:
        def __str__(self):
            raise ValueError("boom")

    coord._remember_battery_reserve(BadProfile(), 20)  # noqa: SLF001
    coord._remember_battery_reserve("regional_special", 20)  # noqa: SLF001


def test_pending_profile_issue_noop_when_already_reported(
    coordinator_factory, mock_issue_registry
) -> None:
    from custom_components.enphase_ev.coordinator import (
        BATTERY_PROFILE_PENDING_TIMEOUT_S,
    )

    coord = coordinator_factory()
    coord._battery_profile_issue_reported = True  # noqa: SLF001
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc) - timedelta(  # noqa: SLF001
        seconds=BATTERY_PROFILE_PENDING_TIMEOUT_S + 60
    )

    coord._sync_battery_profile_pending_issue()  # noqa: SLF001

    assert mock_issue_registry.created == []
    assert mock_issue_registry.deleted == []


def test_parse_battery_payload_branches_and_helpers(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._parse_battery_profile_payload([])  # noqa: SLF001
    coord._parse_battery_site_settings([])  # noqa: SLF001
    coord._parse_battery_site_settings({"showChargeFromGrid": True})  # noqa: SLF001

    coord._parse_battery_profile_payload(  # noqa: SLF001
        {
            "profile": "cost_savings",
            "batteryBackupPercentage": 15,
            "operationModeSubType": "prioritize-energy",
            "pollingInterval": 45,
            "devices": {
                "iqEvse": [
                    "bad",
                    {"chargeMode": "MANUAL"},
                    {"uuid": "evse-1", "chargeMode": "MANUAL", "enable": 0},
                ]
            },
        }
    )
    assert coord.battery_profile == "cost_savings"
    assert coord._battery_polling_interval_s == 45  # noqa: SLF001

    coord._parse_battery_profile_payload(  # noqa: SLF001
        {"profile": "backup_only", "batteryBackupPercentage": 80}
    )
    assert coord.battery_effective_backup_percentage == 100
    assert coord._battery_profile_devices == []  # noqa: SLF001

    coord._battery_profile_devices = [  # noqa: SLF001
        {"chargeMode": "MANUAL", "enable": True},
        {"uuid": "1", "enable": False},
        {"uuid": "2", "chargeMode": "MANUAL", "enable": True},
        {"uuid": "3", "chargeMode": "SCHEDULED", "enable": None},
    ]
    payload = coord._battery_profile_devices_payload()  # noqa: SLF001
    assert payload is not None
    assert len(payload) == 3
    assert payload[0].get("chargeMode") is None
    assert payload[1]["chargeMode"] == "MANUAL"
    assert payload[2]["chargeMode"] == "SCHEDULED"
    assert "enable" not in payload[2]

    assert coord._target_reserve_for_profile("backup_only") == 100  # noqa: SLF001
    coord._battery_profile_reserve_memory.pop("cost_savings", None)  # noqa: SLF001
    assert coord._target_reserve_for_profile("cost_savings") == 20  # noqa: SLF001
    assert coord._target_reserve_for_profile("regional_special") == 20  # noqa: SLF001
    assert coord._current_savings_sub_type() is None  # noqa: SLF001
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    assert coord._current_savings_sub_type() == "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_sub_type = "other"  # noqa: SLF001
    assert coord._current_savings_sub_type() is None  # noqa: SLF001


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
    assert kwargs["battery_backup_percentage"] == 10
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
    assert coord._battery_site_settings_payload["userId"] == "[redacted]"  # noqa: SLF001
    assert coord._battery_profile_payload is not None  # noqa: SLF001
    assert coord._battery_profile_payload["token"] == "[redacted]"  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_payload_snapshots_wrap_non_dict_payloads(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_site_settings = AsyncMock(return_value=["unexpected"])
    coord.client.storm_guard_profile = AsyncMock(return_value=["unexpected"])

    await coord._async_refresh_battery_site_settings(force=True)  # noqa: SLF001
    await coord._async_refresh_storm_guard_profile(force=True)  # noqa: SLF001

    assert coord._battery_site_settings_payload == {"value": ["unexpected"]}  # noqa: SLF001
    assert coord._battery_profile_payload == {"value": ["unexpected"]}  # noqa: SLF001
