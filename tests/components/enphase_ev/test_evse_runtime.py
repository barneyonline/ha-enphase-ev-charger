from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.evse_runtime import (
    FAST_TOGGLE_POLL_HOLD_S,
    ChargeModeResolution,
    ChargeModeStartPreferences,
    EvseRuntime,
)


@pytest.fixture(autouse=True)
def _force_utc_timezone() -> None:
    dt_util.set_default_time_zone(UTC)


def test_evse_runtime_helper_paths(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.evse_runtime

    coord.data = {
        "EV1": {
            "min_amp": "10",
            "max_amp": "40",
            "charging_level": "18",
            "session_charge_level": "20",
            "plugged": True,
            "charge_mode": "scheduled",
        }
    }
    coord.last_set_amps["EV1"] = 24
    coord._charge_mode_cache["EV1"] = ("GREEN_CHARGING", 10.0)  # noqa: SLF001

    assert runtime.normalize_serials([None, " EV1 ", "EV1"]) == {"EV1"}
    assert runtime.session_history_day(
        {"charging": True}, datetime(2025, 1, 1, tzinfo=UTC)
    ) == datetime(2025, 1, 1, tzinfo=UTC)
    assert runtime.coerce_amp("16") == 16
    assert runtime.amp_limits("EV1") == (10, 40)
    assert runtime.apply_amp_limits("EV1", 50) == 40
    assert runtime.pick_start_amps("EV1", requested=None, fallback=16) == 24
    assert runtime.normalize_charge_mode_preference("scheduled") == "SCHEDULED_CHARGING"
    assert runtime.normalize_charge_mode_preference("smart") == "SMART_CHARGING"
    assert runtime.normalize_effective_charge_mode("idle") == "IDLE"
    assert runtime.resolve_charge_mode_pref("EV1") == "SCHEDULED_CHARGING"
    prefs = runtime.charge_mode_start_preferences("EV1")
    assert prefs == ChargeModeStartPreferences(
        mode="SCHEDULED_CHARGING",
        include_level=True,
        strict=True,
        enforce_mode="SCHEDULED_CHARGING",
    )


def test_evse_runtime_battery_profile_charge_mode_preference_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime

    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    coord._storm_guard_cache_until = 10.0**12  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "GREEN", "enable": True}
    ]
    assert runtime.battery_profile_charge_mode_preference("EV1") == "GREEN_CHARGING"

    coord._storm_guard_cache_until = 0.0  # noqa: SLF001
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    coord._storm_guard_cache_until = 10.0**12  # noqa: SLF001
    coord._battery_profile_devices = ["bad"]  # noqa: SLF001
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "GREEN", "enable": True},
        {"uuid": "evse-2", "chargeMode": "MANUAL", "enable": True},
    ]
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    coord.serials.add("EV2")
    coord._configured_serials = {"EV1"}  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "GREEN", "enable": True}
    ]
    assert runtime.battery_profile_charge_mode_preference("EV1") == "GREEN_CHARGING"

    coord._configured_serials = {"EV1", "EV2"}  # noqa: SLF001
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    coord._configured_serials = {"EV1"}  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "SMART", "enable": True}
    ]
    assert runtime.battery_profile_charge_mode_preference("EV1") == "SMART_CHARGING"


def test_evse_runtime_battery_profile_charge_mode_preference_error_paths() -> None:
    runtime = EvseRuntime(
        SimpleNamespace(
            _configured_serials=set(),
            serials={"EV1", "EV2"},
            _storm_guard_cache_until=10.0**12,
            _battery_profile_devices=[{"chargeMode": "GREEN"}],
        )
    )
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    runtime = EvseRuntime(
        SimpleNamespace(
            _configured_serials={"EV1"},
            serials={"EV1"},
            _storm_guard_cache_until="bad",
            _battery_profile_devices=[{"chargeMode": "GREEN"}],
        )
    )
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    class _BrokenDevices:
        _configured_serials = {"EV1"}
        serials = {"EV1"}
        _storm_guard_cache_until = 10.0**12

        @property
        def _battery_profile_devices(self):
            raise RuntimeError("boom")

    runtime = EvseRuntime(_BrokenDevices())
    assert runtime.battery_profile_charge_mode_preference("EV1") is None


def test_evse_runtime_schedule_type_charge_mode_preference_paths(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().evse_runtime

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    assert (
        runtime.schedule_type_charge_mode_preference("greencharging")
        == "GREEN_CHARGING"
    )
    assert (
        runtime.schedule_type_charge_mode_preference("GREEN_CHARGING")
        == "GREEN_CHARGING"
    )
    assert runtime.schedule_type_charge_mode_preference("   ") is None
    assert runtime.schedule_type_charge_mode_preference("CUSTOM") is None
    assert runtime.schedule_type_charge_mode_preference(BadStr()) is None


@pytest.mark.asyncio
async def test_evse_runtime_resolvers_use_runtime_methods(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1", "EV2"])
    runtime = coord.evse_runtime

    runtime.async_get_charge_mode = AsyncMock(return_value="GREEN_CHARGING")  # type: ignore[method-assign]
    runtime.async_get_green_battery_setting = AsyncMock(  # type: ignore[method-assign]
        return_value=(True, True)
    )
    runtime.async_get_auth_settings = AsyncMock(  # type: ignore[method-assign]
        return_value=(True, False, True, True)
    )

    assert await runtime.async_resolve_charge_modes(["EV1"]) == {
        "EV1": ChargeModeResolution("GREEN_CHARGING", "scheduler_endpoint")
    }
    assert await runtime.async_resolve_green_battery_settings(["EV1"]) == {
        "EV1": (True, True)
    }
    assert await runtime.async_resolve_auth_settings(["EV1"]) == {
        "EV1": (True, False, True, True)
    }

    runtime.async_get_charge_mode.assert_awaited_once_with("EV1")
    runtime.async_get_green_battery_setting.assert_awaited_once_with("EV1")
    runtime.async_get_auth_settings.assert_awaited_once_with("EV1")


@pytest.mark.asyncio
async def test_evse_runtime_start_stop_and_auto_resume_use_coordinator_hooks(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    coord.data = {
        "EV1": {
            "plugged": True,
            "display_name": "Driveway",
            "charge_mode_pref": "SCHEDULED_CHARGING",
        }
    }
    coord.pick_start_amps = MagicMock(return_value=28)
    coord.set_last_set_amps = MagicMock()
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.async_start_streaming = AsyncMock()
    coord._ensure_charge_mode = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.require_plugged = MagicMock()
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})

    await runtime.async_start_charging("EV1", fallback_amps=24)
    await runtime.async_stop_charging("EV1")
    await runtime.async_auto_resume("EV1", {"plugged": True})

    assert coord.pick_start_amps.call_count == 2
    coord.require_plugged.assert_any_call("EV1")
    coord.set_last_set_amps.assert_any_call("EV1", 28)
    coord.set_desired_charging.assert_any_call("EV1", True)
    coord.set_desired_charging.assert_any_call("EV1", False)
    coord.async_start_streaming.assert_any_await(
        manual=False,
        serial="EV1",
        expected_state=True,
    )
    coord.async_start_streaming.assert_any_await(
        manual=False,
        serial="EV1",
        expected_state=False,
    )
    coord._ensure_charge_mode.assert_awaited()
    assert coord.async_request_refresh.await_count == 3


@pytest.mark.asyncio
async def test_evse_runtime_schedule_amp_restart_uses_coordinator_override(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.evse_runtime
    pending = asyncio.Future()
    coord._amp_restart_tasks["EV1"] = pending  # noqa: SLF001
    calls: list[tuple[str, float]] = []

    async def _fake_restart(sn: str, delay: float) -> None:
        calls.append((sn, delay))

    coord.__dict__["_async_restart_after_amp_change"] = _fake_restart
    tasks: list[asyncio.Task[None]] = []

    def _capture(coro, name=None):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(coord.hass, "async_create_task", _capture)

    runtime.schedule_amp_restart("EV1", delay=12)

    assert pending.cancelled()
    await tasks[0]
    assert calls == [("EV1", 12)]


@pytest.mark.asyncio
async def test_evse_runtime_streaming_and_record_actual_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.evse_runtime
    coord.client.start_live_stream = AsyncMock(return_value={"duration_s": 30})
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "ok"})
    coord.kick_fast = MagicMock()
    coord._schedule_stream_stop = MagicMock()  # noqa: SLF001

    await runtime.async_start_streaming(serial="EV1", expected_state=True)
    runtime.record_actual_charging("EV1", True)
    runtime.record_actual_charging("EV1", False)
    runtime.record_actual_charging("EV1", False)
    await runtime.async_stop_streaming(manual=True)

    coord.kick_fast.assert_called_with(FAST_TOGGLE_POLL_HOLD_S)
    coord._schedule_stream_stop.assert_called_once_with(force=True)
    assert runtime.streaming_active() is False


def test_evse_runtime_require_plugged_and_desired_state(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.evse_runtime
    coord.data = {"EV1": {"name": "Garage", "plugged": False}}

    with pytest.raises(ServiceValidationError):
        runtime.require_plugged("EV1")

    runtime.set_desired_charging("EV1", True)
    assert runtime.get_desired_charging("EV1") is True
    runtime.set_desired_charging("EV1", None)
    assert runtime.get_desired_charging("EV1") is None


def test_coordinator_evse_runtime_wrapper_delegation(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = Mock()
    runtime.sum_session_energy.return_value = 1.5
    runtime.retained_session_history_days.return_value = {"2025-01-01"}
    runtime.prune_serial_runtime_state.return_value = {"EV1"}
    runtime.determine_polling_state.return_value = {"target": 60}
    runtime.streaming_active.return_value = True
    runtime.slow_interval_floor.return_value = 60
    runtime.get_desired_charging.return_value = True
    runtime.amp_limits.return_value = (10, 40)
    runtime.apply_amp_limits.return_value = 32
    runtime.pick_start_amps.return_value = 30
    runtime.resolve_charge_mode_pref.return_value = "GREEN_CHARGING"
    runtime.cached_charge_mode_preference.return_value = "GREEN_CHARGING"
    runtime.normalize_effective_charge_mode.return_value = "IDLE"
    runtime.charge_mode_start_preferences.return_value = ChargeModeStartPreferences()
    coord.evse_runtime = runtime

    assert coord._sum_session_energy([]) == 1.5  # noqa: SLF001
    assert coord._retained_session_history_days() == {"2025-01-01"}  # noqa: SLF001
    coord._set_session_history_cache_shim_entry("EV1", "2025-01-01", [])  # noqa: SLF001
    assert coord._prune_serial_runtime_state(["EV1"]) == {"EV1"}  # noqa: SLF001
    assert coord._determine_polling_state({}) == {"target": 60}  # noqa: SLF001
    assert coord._streaming_active() is True  # noqa: SLF001
    coord._clear_streaming_state()  # noqa: SLF001
    assert coord._slow_interval_floor() == 60  # noqa: SLF001
    assert coord.get_desired_charging("EV1") is True
    assert coord._amp_limits("EV1") == (10, 40)  # noqa: SLF001
    assert coord._apply_amp_limits("EV1", 50) == 32  # noqa: SLF001
    assert coord.pick_start_amps("EV1") == 30
    assert coord._resolve_charge_mode_pref("EV1") == "GREEN_CHARGING"  # noqa: SLF001
    assert (
        coord._cached_charge_mode_preference("EV1") == "GREEN_CHARGING"
    )  # noqa: SLF001
    assert coord._normalize_effective_charge_mode("idle") == "IDLE"  # noqa: SLF001
    assert (
        coord._charge_mode_start_preferences("EV1") == ChargeModeStartPreferences()
    )  # noqa: SLF001

    runtime.sum_session_energy.assert_called_once_with([])
    runtime.retained_session_history_days.assert_called_once_with(None)
    runtime.set_session_history_cache_shim_entry.assert_called_once_with(
        "EV1",
        "2025-01-01",
        [],
    )
    runtime.prune_serial_runtime_state.assert_called_once_with(["EV1"])
    runtime.determine_polling_state.assert_called_once_with({})
    runtime.streaming_active.assert_called_once_with()
    runtime.clear_streaming_state.assert_called_once_with()
    runtime.slow_interval_floor.assert_called_once_with()
    runtime.get_desired_charging.assert_called_once_with("EV1")
    runtime.amp_limits.assert_called_once_with("EV1")
    runtime.apply_amp_limits.assert_called_once_with("EV1", 50)
    runtime.pick_start_amps.assert_called_once_with("EV1", None, 32)
    runtime.resolve_charge_mode_pref.assert_called_once_with("EV1")
    runtime.cached_charge_mode_preference.assert_called_once_with("EV1", now=None)
    runtime.normalize_effective_charge_mode.assert_called_once_with("idle")
    runtime.charge_mode_start_preferences.assert_called_once_with("EV1")
