from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.const import (
    DEFAULT_CHARGE_LEVEL_SETTING,
    PHASE_SWITCH_CONFIG_SETTING,
)
from custom_components.enphase_ev.session_history import SessionCacheView

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _prepare_refresh_target(
    coord,
    monkeypatch,
    *,
    charging: bool,
    first_refresh: bool,
    session_view: SessionCacheView,
) -> None:
    now_local = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    coord._has_successful_refresh = not first_refresh  # noqa: SLF001
    coord._scheduler_available = True  # noqa: SLF001
    coord.data = {RANDOM_SERIAL: {"display_name": "Garage EV"}}
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "charging": charging,
                    "pluggedIn": charging,
                    "charge_mode": "IMMEDIATE",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_kwargs: False,
        async_fetch=AsyncMock(
            return_value=[{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]
        ),
        invalidate=lambda: None,
    )
    coord.evse_timeseries = SimpleNamespace(
        refresh_due=MagicMock(return_value=False),
        async_refresh=AsyncMock(),
        merge_charger_payloads=MagicMock(),
        diagnostics=lambda: {},
    )
    coord._async_run_post_status_refresh_pipeline = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value=None
    )
    coord._async_run_post_session_refresh_pipeline = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value=None
    )
    coord._async_resolve_green_battery_settings = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={}
    )
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._async_resolve_charger_config = AsyncMock(return_value={})  # noqa: SLF001
    coord.session_history.get_cache_view = MagicMock(return_value=session_view)  # type: ignore[assignment]
    coord._async_enrich_sessions = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={RANDOM_SERIAL: [{"session_id": "fresh", "energy_kwh": 2.0}]}
    )
    coord._schedule_session_enrichment = MagicMock()  # type: ignore[assignment]  # noqa: SLF001
    coord._prune_runtime_caches = MagicMock()  # type: ignore[assignment]  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._sync_site_energy_issue = MagicMock()  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001


@pytest.mark.asyncio
async def test_post_status_evse_enrichment_uses_hot_caches_without_followup_io(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    now = time.monotonic()
    coord._green_battery_cache["EV1"] = (True, True, now)  # noqa: SLF001
    coord._auth_settings_cache["EV1"] = (True, False, True, True, now)  # noqa: SLF001
    coord._charger_config_cache["EV1"] = (  # noqa: SLF001
        {
            DEFAULT_CHARGE_LEVEL_SETTING: 32,
            PHASE_SWITCH_CONFIG_SETTING: "three_phase",
        },
        now,
    )
    coord.evse_runtime.async_get_green_battery_setting = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("green battery lookup should stay cached")
    )
    coord.evse_runtime.async_get_auth_settings = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("auth settings lookup should stay cached")
    )
    coord.client.charger_config = AsyncMock(
        side_effect=AssertionError("charger config lookup should stay cached")
    )

    charge_modes, green_settings, auth_settings, charger_config = (
        await coord._async_resolve_post_status_evse_enrichments(  # noqa: SLF001
            {},
            records=[("EV1", {"sn": "EV1"})],
            charge_mode_candidates=[],
            first_refresh=False,
        )
    )

    assert charge_modes == {}
    assert green_settings == {"EV1": (True, True)}
    assert auth_settings == {"EV1": (True, False, True, True)}
    assert charger_config == {
        "EV1": {
            DEFAULT_CHARGE_LEVEL_SETTING: 32,
            PHASE_SWITCH_CONFIG_SETTING: "three_phase",
        }
    }
    coord.evse_runtime.async_get_green_battery_setting.assert_not_awaited()
    coord.evse_runtime.async_get_auth_settings.assert_not_awaited()
    coord.client.charger_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_get_charger_config_returns_none_during_backoff_without_cache(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    coord._charger_config_backoff_until["EV1"] = time.monotonic() + 60  # noqa: SLF001
    coord.client.charger_config = AsyncMock(
        side_effect=AssertionError("charger config lookup should be backoff-gated")
    )

    result = await coord.evse_runtime.async_get_charger_config(
        "EV1",
        keys=[DEFAULT_CHARGE_LEVEL_SETTING],
    )

    assert result is None
    coord.client.charger_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_hems_refresh_skips_preflight_when_device_cache_is_hot(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._hems_devices_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=AssertionError("preflight should be skipped")
    )
    coord.client.hems_devices = AsyncMock(
        side_effect=AssertionError("device refresh should be skipped")
    )

    await runtime._async_refresh_hems_devices()

    coord.heatpump_runtime.async_refresh_hems_support_preflight.assert_not_awaited()
    coord.client.hems_devices.assert_not_awaited()


@pytest.mark.asyncio
async def test_hems_refresh_reuses_preflight_cache_across_due_device_refreshes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.client._hems_site_supported = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value={"is_hems": True})
    coord.client.hems_devices = AsyncMock(
        return_value={"data": {"hems-devices": {"heat-pump": []}}}
    )

    await runtime._async_refresh_hems_devices()

    assert runtime._hems_support_preflight_cache_until is not None  # noqa: SLF001
    coord.client._hems_site_supported = None  # noqa: SLF001
    runtime._hems_devices_cache_until = None  # noqa: SLF001

    await runtime._async_refresh_hems_devices()

    coord.client.system_dashboard_summary.assert_awaited_once()
    assert coord.client.hems_devices.await_count == 2


@pytest.mark.parametrize(
    ("first_refresh", "cache_age", "expected_inline", "expected_background"),
    [
        (True, 180.0, False, True),
        (False, 180.0, False, True),
        (False, 10_000.0, True, False),
    ],
)
@pytest.mark.asyncio
async def test_session_history_refresh_classifies_inline_vs_background_paths(
    coordinator_factory,
    monkeypatch,
    first_refresh: bool,
    cache_age: float,
    expected_inline: bool,
    expected_background: bool,
) -> None:
    coord = coordinator_factory()
    _prepare_refresh_target(
        coord,
        monkeypatch,
        charging=True,
        first_refresh=first_refresh,
        session_view=SessionCacheView(
            sessions=[{"session_id": "cached", "energy_kwh": 1.25}],
            cache_age=cache_age,
            needs_refresh=True,
            blocked=False,
            state="valid",
            has_valid_cache=True,
            last_error=None,
        ),
    )

    data = await coord._async_update_data()  # noqa: SLF001

    if expected_inline:
        coord._async_enrich_sessions.assert_awaited_once()  # type: ignore[attr-defined]  # noqa: SLF001
        coord._schedule_session_enrichment.assert_not_called()  # type: ignore[attr-defined]  # noqa: SLF001
        assert data[RANDOM_SERIAL]["energy_today_sessions"] == [
            {"session_id": "fresh", "energy_kwh": 2.0}
        ]
        assert (
            coord._async_enrich_sessions.await_args.kwargs["max_cache_age"]  # type: ignore[attr-defined]  # noqa: SLF001
            == 120.0
        )
        return

    if expected_background:
        coord._async_enrich_sessions.assert_not_awaited()  # type: ignore[attr-defined]  # noqa: SLF001
        coord._schedule_session_enrichment.assert_called_once()  # type: ignore[attr-defined]  # noqa: SLF001
        assert data[RANDOM_SERIAL]["energy_today_sessions"] == [
            {"session_id": "cached", "energy_kwh": 1.25}
        ]
        assert (
            coord._schedule_session_enrichment.call_args.kwargs["max_cache_age"]  # type: ignore[attr-defined]  # noqa: SLF001
            == 120.0
        )
