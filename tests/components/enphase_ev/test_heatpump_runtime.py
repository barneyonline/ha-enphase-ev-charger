from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.enphase_ev import parsing_helpers as parsing_helpers_mod
from custom_components.enphase_ev import api
from custom_components.enphase_ev import heatpump_runtime as heatpump_runtime_mod
from custom_components.enphase_ev.heatpump_runtime import HeatpumpRuntime
from custom_components.enphase_ev.parsing_helpers import (
    coerce_optional_bool,
    coerce_optional_float,
    coerce_optional_text,
    heatpump_device_state,
    heatpump_lifecycle_status_text,
    heatpump_member_device_type,
    heatpump_operational_status_text,
    heatpump_pairing_status,
    heatpump_status_bucket,
    heatpump_status_text,
    parse_inverter_last_report,
    type_member_text,
)


def _site_today_payload(
    total_wh: float, *, timestamp: str | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "stats": [{"heatpump": [total_wh]}],
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp
    return payload


def _seed_previous_heatpump_daily_snapshot(
    coord,
    *,
    energy_wh: float,
    timestamp: str,
    day_key: str,
    timezone_name: str,
    device_uid: str | None = "HP-1",
    device_name: str = "Waermepumpe",
) -> None:
    coord._heatpump_daily_consumption = {  # noqa: SLF001
        "split_device_uid": device_uid,
        "split_device_name": device_name,
        "member_name": device_name,
        "member_device_type": "HEAT_PUMP",
        "pairing_status": "PAIRED",
        "device_state": None,
        "daily_energy_wh": energy_wh,
        "split_daily_energy_wh": energy_wh,
        "daily_solar_wh": 0.0,
        "daily_battery_wh": 0.0,
        "daily_grid_wh": 0.0,
        "details": [energy_wh],
        "source": "site_today_heatpump",
        "split_source": (
            f"hems_energy_consumption:{device_uid}"
            if device_uid is not None
            else "hems_energy_consumption"
        ),
        "split_endpoint_type": "hems-device-details",
        "split_endpoint_timestamp": timestamp,
        "day_key": day_key,
        "timezone": timezone_name,
    }


def _heatpump_daily_snapshot(
    *,
    energy_wh: float | None,
    timestamp: str | None,
    day_key: str = "2026-04-02",
    timezone_name: str = "UTC",
    device_uid: str | None = "HP-1",
) -> dict[str, object]:
    return {
        "split_device_uid": device_uid,
        "member_device_type": "HEAT_PUMP",
        "pairing_status": "PAIRED",
        "device_state": None,
        "daily_energy_wh": energy_wh,
        "split_daily_energy_wh": energy_wh,
        "daily_solar_wh": 0.0,
        "daily_battery_wh": 0.0,
        "daily_grid_wh": 0.0,
        "details": [energy_wh],
        "split_endpoint_type": "hems-device-details",
        "split_endpoint_timestamp": timestamp,
        "day_key": day_key,
        "timezone": timezone_name,
    }


@pytest.mark.asyncio
async def test_heatpump_runtime_preflight_without_refresh_kw(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.heatpump_runtime
    calls: list[str] = []
    coord.client._hems_site_supported = None  # noqa: SLF001

    async def system_dashboard_summary():
        calls.append("system_dashboard_summary")
        return {"is_hems": True}

    coord.client.system_dashboard_summary = system_dashboard_summary  # type: ignore[method-assign]

    await runtime._async_refresh_hems_support_preflight(force=True)  # noqa: SLF001

    assert calls == ["system_dashboard_summary"]
    assert coord.client.hems_site_supported is True


@pytest.mark.asyncio
async def test_heatpump_runtime_fetcher_falls_back_when_uninspectable(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().heatpump_runtime

    class BadSignatureFetcher:
        @property
        def __signature__(self):
            raise ValueError("boom")

        async def __call__(self):
            return {"ok": True}

    assert await runtime._async_call_refreshable_fetcher(  # noqa: SLF001
        BadSignatureFetcher(),
        force=True,
    ) == {"ok": True}


@pytest.mark.asyncio
async def test_heatpump_runtime_public_async_wrappers(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().heatpump_runtime
    runtime._async_refresh_hems_support_preflight = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_runtime_state = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_daily_consumption = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_power = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001

    await runtime.async_refresh_hems_support_preflight(force=True)
    await runtime.async_refresh_heatpump_runtime_state(force=True)
    await runtime.async_refresh_heatpump_daily_consumption(force=True)
    await runtime.async_refresh_heatpump_power(force=True)

    runtime._async_refresh_hems_support_preflight.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    runtime._async_refresh_heatpump_runtime_state.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    runtime._async_refresh_heatpump_daily_consumption.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    runtime._async_refresh_heatpump_power.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )


@pytest.mark.asyncio
async def test_heatpump_runtime_preflight_uses_fast_poll_cache_floor(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.heatpump_runtime
    coord.client._hems_site_supported = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value=None)

    await runtime._async_refresh_hems_support_preflight()  # noqa: SLF001

    coord.client.system_dashboard_summary.assert_awaited_once_with()
    assert runtime.hems_support_preflight_cache_ttl_s() == pytest.approx(30.0)
    assert runtime._hems_support_preflight_cache_until is not None  # noqa: SLF001
    assert (
        runtime._hems_support_preflight_cache_until >= time.monotonic() + 29.0
    )  # noqa: SLF001


@pytest.mark.parametrize(
    ("current_snapshot", "previous_snapshot", "expected_validation"),
    [
        (
            _heatpump_daily_snapshot(energy_wh=None, timestamp=None),
            None,
            "accepted_idle_without_delta",
        ),
        (
            _heatpump_daily_snapshot(
                energy_wh=10.0,
                timestamp="2026-04-02T00:05:00Z",
            ),
            None,
            "accepted_idle_seeded",
        ),
        (
            _heatpump_daily_snapshot(
                energy_wh=10.0,
                timestamp="2026-04-02T00:05:00Z",
            ),
            _heatpump_daily_snapshot(
                energy_wh=9.0,
                timestamp="2026-04-02T00:05:00Z",
            ),
            "accepted_idle_repeated_sample",
        ),
        (
            _heatpump_daily_snapshot(
                energy_wh=8.0,
                timestamp="2026-04-02T00:10:00Z",
            ),
            _heatpump_daily_snapshot(
                energy_wh=9.0,
                timestamp="2026-04-02T00:05:00Z",
            ),
            "accepted_idle_reset",
        ),
        (
            _heatpump_daily_snapshot(
                energy_wh=9.2,
                timestamp="2026-04-02T00:10:00Z",
            ),
            _heatpump_daily_snapshot(
                energy_wh=9.0,
                timestamp="2026-04-02T00:05:00Z",
            ),
            "accepted_idle_zero",
        ),
    ],
)
def test_heatpump_power_summary_covers_idle_delta_edge_paths(
    coordinator_factory,
    current_snapshot,
    previous_snapshot,
    expected_validation,
):
    runtime = coordinator_factory(serials=[]).heatpump_runtime
    runtime._heatpump_daily_consumption_previous = previous_snapshot  # noqa: SLF001

    summary = runtime._heatpump_power_summary_from_daily_snapshot(  # noqa: SLF001
        current_snapshot,
        runtime_snapshot={"heatpump_status": "IDLE"},
    )

    assert summary is not None
    assert summary["accepted_value_w"] == pytest.approx(0.0)
    assert summary["validation"] == expected_validation


def test_heatpump_power_summary_smooths_idle_zero_from_history(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory(serials=[]).heatpump_runtime
    runtime._heatpump_power_sample_history = [  # noqa: SLF001
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 100.0,
            "sample_utc": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 105.0,
            "sample_utc": datetime(2026, 4, 2, 0, 15, tzinfo=timezone.utc),
        },
    ]
    runtime._heatpump_daily_consumption_previous = (
        _heatpump_daily_snapshot(  # noqa: SLF001
            energy_wh=105.0,
            timestamp="2026-04-02T00:15:00Z",
        )
    )

    summary = runtime._heatpump_power_summary_from_daily_snapshot(  # noqa: SLF001
        _heatpump_daily_snapshot(
            energy_wh=105.0,
            timestamp="2026-04-02T00:20:00Z",
        ),
        runtime_snapshot={"heatpump_status": "IDLE"},
    )

    assert summary is not None
    assert summary["raw_value_w"] == pytest.approx(0.0)
    assert summary["raw_validation"] == "accepted_idle_zero"
    assert summary["accepted_value_w"] == pytest.approx(15.0)
    assert summary["power_window_seconds"] == pytest.approx(1200.0)
    assert summary["power_validation"] == "smoothed_idle_delta"
    assert summary["smoothed"] is True


def test_heatpump_power_summary_does_not_smooth_non_idle_zero(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory(serials=[]).heatpump_runtime
    runtime._heatpump_power_sample_history = [  # noqa: SLF001
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 100.0,
            "sample_utc": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        }
    ]
    runtime._heatpump_daily_consumption_previous = (
        _heatpump_daily_snapshot(  # noqa: SLF001
            energy_wh=105.0,
            timestamp="2026-04-02T00:15:00Z",
        )
    )

    summary = runtime._heatpump_power_summary_from_daily_snapshot(  # noqa: SLF001
        _heatpump_daily_snapshot(
            energy_wh=105.0,
            timestamp="2026-04-02T00:20:00Z",
        ),
        runtime_snapshot={"heatpump_status": "RUNNING"},
    )

    assert summary is not None
    assert summary["accepted_value_w"] == pytest.approx(0.0)
    assert summary["power_validation"] == "accepted_zero_delta"
    assert summary["smoothed"] is False


@pytest.mark.asyncio
async def test_refresh_heatpump_power_smooths_idle_zero_from_recorded_history(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-04-02"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "statusText": "Normal",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_heatpump_state = AsyncMock(
        return_value={"device_uid": "HP-1", "heatpump_status": "IDLE"}
    )
    coord.client.pv_system_today = AsyncMock(
        side_effect=[
            _site_today_payload(100.0, timestamp="2026-04-02T00:00:00Z"),
            _site_today_payload(105.0, timestamp="2026-04-02T00:15:00Z"),
            _site_today_payload(105.0, timestamp="2026-04-02T00:20:00Z"),
        ]
    )
    coord.client.hems_energy_consumption = AsyncMock(
        side_effect=[
            {
                "type": "hems-device-details",
                "timestamp": "2026-04-02T00:00:00Z",
                "data": {
                    "heat-pump": [
                        {
                            "device_uid": "HP-1",
                            "device_name": "Waermepumpe",
                            "consumption": [{"details": [100.0]}],
                        }
                    ]
                },
            },
            {
                "type": "hems-device-details",
                "timestamp": "2026-04-02T00:15:00Z",
                "data": {
                    "heat-pump": [
                        {
                            "device_uid": "HP-1",
                            "device_name": "Waermepumpe",
                            "consumption": [{"details": [105.0]}],
                        }
                    ]
                },
            },
            {
                "type": "hems-device-details",
                "timestamp": "2026-04-02T00:20:00Z",
                "data": {
                    "heat-pump": [
                        {
                            "device_uid": "HP-1",
                            "device_name": "Waermepumpe",
                            "consumption": [{"details": [105.0]}],
                        }
                    ]
                },
            },
        ]
    )

    await coord.heatpump_runtime._async_refresh_heatpump_power(  # noqa: SLF001
        force=True
    )
    assert coord.heatpump_power_w == pytest.approx(0.0)
    assert coord.heatpump_power_validation == "accepted_idle_seeded"

    await coord.heatpump_runtime._async_refresh_heatpump_power(  # noqa: SLF001
        force=True
    )
    assert coord.heatpump_power_w == pytest.approx(20.0)
    assert coord.heatpump_power_smoothed is False

    await coord.heatpump_runtime._async_refresh_heatpump_power(  # noqa: SLF001
        force=True
    )

    assert coord.client.hems_energy_consumption.await_count == 3
    assert coord.heatpump_power_w == pytest.approx(15.0)
    assert coord.heatpump_power_raw_w == pytest.approx(0.0)
    assert coord.heatpump_power_window_seconds == pytest.approx(1200.0)
    assert coord.heatpump_power_validation == "smoothed_idle_delta"
    assert coord.heatpump_power_smoothed is True
    assert coord.heatpump_power_start_utc == datetime(
        2026, 4, 2, 0, 0, tzinfo=timezone.utc
    )
    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["selected_payload"]["raw_validation"] == (
        "accepted_idle_zero"
    )
    assert power_snapshot["selected_payload"]["smoothed"] is True


def test_heatpump_power_history_helpers_cover_guard_paths(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory(serials=[]).heatpump_runtime
    runtime._heatpump_power_sample_history = "bad-history"  # type: ignore[assignment]  # noqa: SLF001
    assert runtime._heatpump_power_history() == []  # noqa: SLF001
    assert runtime._heatpump_power_history_sample_time(None) is None  # noqa: SLF001

    runtime._record_heatpump_power_history_sample(None)  # noqa: SLF001
    runtime._record_heatpump_power_history_sample(  # noqa: SLF001
        {"split_daily_energy_wh": None, "split_endpoint_timestamp": None}
    )
    assert runtime._heatpump_power_sample_history == []  # noqa: SLF001

    runtime._heatpump_power_sample_history = [  # noqa: SLF001
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 90.0,
            "sample_utc": datetime(2026, 4, 1, 23, 0, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 99.0,
            "sample_utc": "bad-time",
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 100.0,
            "sample_utc": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 101.0,
            "sample_utc": datetime(2026, 4, 2, 0, 5, tzinfo=timezone.utc),
        },
    ]

    runtime._record_heatpump_power_history_sample(  # noqa: SLF001
        _heatpump_daily_snapshot(
            energy_wh=105.0,
            timestamp="2026-04-02T00:05:00Z",
        )
    )

    assert runtime._heatpump_power_sample_history == [  # noqa: SLF001
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 100.0,
            "sample_utc": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 105.0,
            "sample_utc": datetime(2026, 4, 2, 0, 5, tzinfo=timezone.utc),
        },
    ]


def test_heatpump_idle_smoothing_rejects_invalid_history(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory(serials=[]).heatpump_runtime
    current_sample_utc = datetime(2026, 4, 2, 0, 20, tzinfo=timezone.utc)
    snapshot = _heatpump_daily_snapshot(
        energy_wh=105.0,
        timestamp="2026-04-02T00:20:00Z",
    )
    runtime._heatpump_power_sample_history = [  # noqa: SLF001
        {"sample_utc": "bad-time"},
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 90.0,
            "sample_utc": datetime(2026, 4, 1, 23, 40, tzinfo=timezone.utc),
        },
        {
            "device_uid": "OTHER",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 100.0,
            "sample_utc": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-01",
            "timezone": "UTC",
            "energy_wh": 100.0,
            "sample_utc": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "Europe/Berlin",
            "energy_wh": 100.0,
            "sample_utc": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": None,
            "sample_utc": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 100.0,
            "sample_utc": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        },
        {
            "device_uid": "OTHER",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 101.0,
            "sample_utc": datetime(2026, 4, 2, 0, 5, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-01",
            "timezone": "UTC",
            "energy_wh": 101.0,
            "sample_utc": datetime(2026, 4, 2, 0, 6, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "Europe/Berlin",
            "energy_wh": 101.0,
            "sample_utc": datetime(2026, 4, 2, 0, 7, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": None,
            "sample_utc": datetime(2026, 4, 2, 0, 8, tzinfo=timezone.utc),
        },
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 106.0,
            "sample_utc": datetime(2026, 4, 2, 0, 10, tzinfo=timezone.utc),
        },
    ]

    assert (
        runtime._heatpump_idle_smoothed_power(  # noqa: SLF001
            snapshot,
            current_energy_wh=105.0,
            current_sample_utc=current_sample_utc,
            raw_value_w=1.0,
            raw_validation="accepted_idle_delta",
        )
        is None
    )
    assert (
        runtime._heatpump_idle_smoothed_power(  # noqa: SLF001
            snapshot,
            current_energy_wh=105.0,
            current_sample_utc=current_sample_utc,
            raw_value_w=0.0,
            raw_validation="accepted_idle_seeded",
        )
        is None
    )
    assert (
        runtime._heatpump_idle_smoothed_power(  # noqa: SLF001
            snapshot,
            current_energy_wh=105.0,
            current_sample_utc=current_sample_utc,
            raw_value_w=0.0,
            raw_validation="accepted_idle_zero",
        )
        is None
    )

    runtime._heatpump_power_sample_history = [  # noqa: SLF001
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 104.8,
            "sample_utc": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        }
    ]
    assert (
        runtime._heatpump_idle_smoothed_power(  # noqa: SLF001
            snapshot,
            current_energy_wh=105.0,
            current_sample_utc=current_sample_utc,
            raw_value_w=0.0,
            raw_validation="accepted_idle_zero",
        )
        is None
    )

    runtime._heatpump_power_sample_history = [  # noqa: SLF001
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 80.0,
            "sample_utc": datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
        }
    ]
    assert (
        runtime._heatpump_idle_smoothed_power(  # noqa: SLF001
            snapshot,
            current_energy_wh=105.0,
            current_sample_utc=current_sample_utc,
            raw_value_w=0.0,
            raw_validation="accepted_idle_zero",
        )
        is None
    )


@pytest.mark.asyncio
async def test_heatpump_runtime_diagnostics_refreshes_power_snapshot(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory(serials=[]).heatpump_runtime
    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {"type_key": "heatpump", "count": 1, "devices": [{}]}
    }
    runtime._async_refresh_heatpump_runtime_state = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_daily_consumption = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_power = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001

    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)

    runtime._async_refresh_heatpump_runtime_state.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    runtime._async_refresh_heatpump_daily_consumption.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    runtime._async_refresh_heatpump_power.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )


@pytest.mark.asyncio
async def test_heatpump_runtime_diagnostics_logs_power_refresh_failures(
    coordinator_factory, caplog
) -> None:
    raw_uid = "DEVICE-UID-123456789"
    runtime = coordinator_factory(serials=[]).heatpump_runtime
    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "type_key": "heatpump",
            "count": 1,
            "devices": [{"device_uid": raw_uid}],
        }
    }
    runtime._async_refresh_heatpump_runtime_state = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_daily_consumption = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_power = AsyncMock(side_effect=RuntimeError(f"power {raw_uid}"))  # type: ignore[assignment]  # noqa: SLF001

    with caplog.at_level(logging.DEBUG):
        await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)

    assert "Heat pump power diagnostics refresh failed for site" in caplog.text
    assert raw_uid not in caplog.text
    assert "DEVI...6789" in caplog.text


@pytest.mark.asyncio
async def test_heatpump_runtime_power_failure_logs_truncated_device_uid(
    coordinator_factory, caplog
) -> None:
    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord.heatpump_runtime._async_refresh_hems_support_preflight = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.client.hems_energy_consumption = AsyncMock(
        side_effect=RuntimeError("fetch failed for HP-1")
    )

    with caplog.at_level(logging.DEBUG):
        await coord.heatpump_runtime._async_refresh_heatpump_power(
            force=True
        )  # noqa: SLF001

    assert (
        "Heat pump power daily-consumption payload unavailable for site" in caplog.text
    )
    assert "HP-1" not in caplog.text
    assert "H...1" in caplog.text


@pytest.mark.asyncio
async def test_heatpump_runtime_power_logs_fetch_plan_and_selection_summary(
    coordinator_factory, caplog
) -> None:
    primary_uid = "DEVICE-UID-123456789"
    meter_uid = "METER-UID-987654321"
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": primary_uid,
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": meter_uid,
                        "statusText": "Recommended",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-03-13T00:05:00Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": primary_uid,
                        "device_name": "Primary",
                        "consumption": [{"details": [550.0]}],
                    }
                ]
            },
        }
    )
    coord.client.pv_system_today = AsyncMock(
        return_value=_site_today_payload(275.0, timestamp="2026-03-20T08:00:00Z")
    )
    coord.client.hems_heatpump_state = AsyncMock(
        return_value={"device_uid": primary_uid, "heatpump_status": "RUNNING"}
    )
    _seed_previous_heatpump_daily_snapshot(
        coord,
        device_uid=primary_uid,
        energy_wh=504.1666666667,
        timestamp="2026-03-13T00:00:00Z",
        day_key="2026-03-13",
        timezone_name="UTC",
        device_name="Primary",
    )

    with caplog.at_level(logging.DEBUG):
        await coord.heatpump_runtime._async_refresh_heatpump_power(  # noqa: SLF001
            force=True
        )

    assert "Heat pump power fetch plan for site" in caplog.text
    assert "Heat pump power selected payload for site" in caplog.text
    assert primary_uid not in caplog.text
    assert meter_uid not in caplog.text
    assert "DEVI...6789" in caplog.text
    assert "'accepted_value_w': 550.0" in caplog.text
    assert coord.heatpump_power_device_uid == primary_uid
    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["compare_all"] is False
    assert power_snapshot["previous_device_ref"] is None
    assert power_snapshot["outcome"] == "selected_sample"
    assert (
        power_snapshot["selected_source"] == "hems_energy_consumption_delta:DEVI...6789"
    )
    assert power_snapshot["selected_payload"]["resolved_device_ref"] == "DEVI...6789"
    assert power_snapshot["selected_payload"]["accepted_value_w"] == pytest.approx(
        550.0
    )
    assert power_snapshot["selected_sample_at_utc"] is not None
    assert len(power_snapshot["attempts"]) == 1


@pytest.mark.asyncio
async def test_heatpump_runtime_power_logs_when_no_candidate_payload_is_usable(
    coordinator_factory, caplog
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(return_value=None)

    with caplog.at_level(logging.DEBUG):
        await coord.heatpump_runtime._async_refresh_heatpump_power(  # noqa: SLF001
            force=True
        )

    assert coord.heatpump_power_w is None
    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["outcome"] == "no_usable_payload"
    assert power_snapshot["attempts"] == [
        {
            "source": "hems_energy_consumption",
            "error": "No usable HEMS daily split payload",
        }
    ]


@pytest.mark.asyncio
async def test_heatpump_runtime_power_snapshot_handles_selected_payload_without_uid(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "statusText": "Normal"}],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-03-13T00:05:00Z",
            "data": {"heat-pump": [{"consumption": [{"details": [125.0]}]}]},
        }
    )
    coord.client.hems_heatpump_state = AsyncMock(
        return_value={"heatpump_status": "RUNNING"}
    )
    _seed_previous_heatpump_daily_snapshot(
        coord,
        device_uid=None,
        energy_wh=114.5833333333,
        timestamp="2026-03-13T00:00:00Z",
        day_key="2026-03-13",
        timezone_name="UTC",
        device_name="Heat Pump",
    )

    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(125.0)
    assert coord.heatpump_power_device_uid is None
    assert coord.heatpump_power_source == "hems_energy_consumption_delta"
    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["selected_source"] == "hems_energy_consumption_delta"


@pytest.mark.asyncio
async def test_heatpump_runtime_power_snapshot_redacts_identifier_bearing_errors(
    coordinator_factory,
) -> None:
    raw_uid = "DEVICE-UID-123456789"
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": raw_uid,
                        "serial_number": "SERIAL-123456",
                    }
                ],
            }
        },
        ["heatpump"],
    )

    async def _raise_fetch_error(
        *, start_at=None, end_at=None, timezone=None, step=None
    ):
        raise RuntimeError(f"fetch failed for {raw_uid} serial SERIAL-123456")

    coord.client.hems_energy_consumption = _raise_fetch_error  # type: ignore[method-assign]
    coord.heatpump_runtime._async_refresh_hems_support_preflight = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001

    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001

    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["outcome"] == "no_usable_payload"
    attempt_error = power_snapshot["attempts"][0]["error"]
    assert raw_uid not in attempt_error
    assert "SERIAL-123456" not in attempt_error
    assert "DEVI...6789" in attempt_error
    assert "SERI...3456" in attempt_error
    assert raw_uid not in power_snapshot["last_error"]
    assert "DEVI...6789" in power_snapshot["last_error"]


@pytest.mark.asyncio
async def test_heatpump_runtime_public_api_access(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = MagicMock()
    runtime._heatpump_primary_member.return_value = {"device_uid": "HP-1"}
    runtime._heatpump_primary_device_uid.return_value = "HP-1"
    runtime._heatpump_runtime_device_uid.return_value = "HP-RUNTIME"
    runtime._heatpump_daily_window.return_value = (
        "2026-03-27T00:00:00.000Z",
        "2026-03-27T23:59:59.999Z",
        "UTC",
        ("2026-03-27", "UTC"),
    )
    runtime._build_heatpump_daily_consumption_snapshot.return_value = {
        "daily_energy_wh": 123.0
    }
    runtime._heatpump_power_candidate_device_uids.return_value = ["HP-1", None]
    runtime._heatpump_member_for_uid.return_value = {"device_uid": "HP-1"}
    runtime._heatpump_member_primary_id.return_value = "PRIMARY-1"
    runtime._heatpump_member_parent_id.return_value = "PARENT-1"
    runtime._heatpump_member_alias_map.return_value = {"HP-1": "HP-1"}
    runtime._heatpump_power_inventory_marker.return_value = ()
    runtime._heatpump_power_fetch_plan.return_value = (["HP-1"], False, ())
    runtime._heatpump_power_candidate_is_recommended.return_value = True
    runtime._heatpump_power_candidate_type_rank.return_value = 3
    runtime._heatpump_power_selection_key.return_value = (1, 1, 1, 3, 500.0, 1, 0)
    runtime.async_refresh_hems_support_preflight = AsyncMock()
    runtime.async_ensure_heatpump_runtime_diagnostics = AsyncMock()
    runtime.async_refresh_heatpump_runtime_state = AsyncMock()
    runtime.async_refresh_heatpump_daily_consumption = AsyncMock()
    runtime.async_refresh_heatpump_power = AsyncMock()
    runtime.heatpump_runtime_diagnostics.return_value = {
        "runtime_state": {},
        "event_summary": {
            "known_event_counts": {},
            "unknown_event_keys": [],
        },
    }
    runtime.heatpump_runtime_state = {"device_uid": "HP-1"}
    runtime.heatpump_runtime_state_last_error = "runtime boom"
    runtime.heatpump_daily_consumption = {"daily_energy_wh": 123.0}
    runtime.heatpump_daily_consumption_last_error = "daily boom"
    runtime.heatpump_power_w = 640.0
    runtime.heatpump_power_sample_utc = datetime(2026, 3, 27, tzinfo=timezone.utc)
    runtime.heatpump_power_start_utc = datetime(2026, 3, 27, tzinfo=timezone.utc)
    runtime.heatpump_power_device_uid = "HP-1"
    runtime.heatpump_power_source = "hems_energy_consumption:HP-1"
    runtime.heatpump_power_last_error = "power boom"
    coord.heatpump_runtime = runtime

    assert runtime._heatpump_primary_member() == {"device_uid": "HP-1"}  # noqa: SLF001
    assert runtime._heatpump_primary_device_uid() == "HP-1"  # noqa: SLF001
    assert runtime._heatpump_runtime_device_uid() == "HP-RUNTIME"  # noqa: SLF001
    assert runtime._heatpump_daily_window() == (  # noqa: SLF001
        "2026-03-27T00:00:00.000Z",
        "2026-03-27T23:59:59.999Z",
        "UTC",
        ("2026-03-27", "UTC"),
    )
    assert runtime._build_heatpump_daily_consumption_snapshot(
        {"data": {}},
        _site_today_payload(123.0),
    ) == {  # noqa: SLF001
        "daily_energy_wh": 123.0
    }
    assert runtime._heatpump_power_candidate_device_uids() == [
        "HP-1",
        None,
    ]  # noqa: SLF001
    assert runtime._heatpump_member_for_uid("HP-1") == {
        "device_uid": "HP-1"
    }  # noqa: SLF001
    assert (
        runtime._heatpump_member_primary_id({"device_uid": "PRIMARY-1"})  # noqa: SLF001
        == "PRIMARY-1"
    )
    assert (
        runtime._heatpump_member_parent_id({"parent": "PARENT-1"}) == "PARENT-1"
    )  # noqa: SLF001
    assert runtime._heatpump_member_alias_map() == {"HP-1": "HP-1"}  # noqa: SLF001
    assert runtime._heatpump_power_inventory_marker() == ()  # noqa: SLF001
    assert runtime._heatpump_power_fetch_plan() == (["HP-1"], False, ())  # noqa: SLF001
    assert (
        runtime._heatpump_power_candidate_is_recommended("HP-1") is True
    )  # noqa: SLF001
    assert (
        runtime._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "HP-1",
            is_recommended=True,
        )
        == 3
    )
    assert runtime._heatpump_power_selection_key(  # noqa: SLF001
        {},
        requested_uid="HP-1",
        sample=(0, 500.0),
    ) == (1, 1, 1, 3, 500.0, 1, 0)

    await runtime.async_refresh_hems_support_preflight(force=True)
    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)
    await runtime.async_refresh_heatpump_runtime_state(force=True)
    await runtime.async_refresh_heatpump_daily_consumption(force=True)
    await runtime.async_refresh_heatpump_power(force=True)

    assert runtime.heatpump_runtime_diagnostics()["runtime_state"] == {}
    assert runtime.heatpump_runtime_diagnostics()["event_summary"] == {
        "known_event_counts": {},
        "unknown_event_keys": [],
    }
    assert runtime.heatpump_runtime_state == {"device_uid": "HP-1"}
    assert runtime.heatpump_runtime_state_last_error == "runtime boom"
    assert runtime.heatpump_daily_consumption == {"daily_energy_wh": 123.0}
    assert runtime.heatpump_daily_consumption_last_error == "daily boom"
    assert runtime.heatpump_power_w == 640.0
    assert runtime.heatpump_power_sample_utc == datetime(
        2026, 3, 27, tzinfo=timezone.utc
    )
    assert runtime.heatpump_power_start_utc == datetime(
        2026, 3, 27, tzinfo=timezone.utc
    )
    assert runtime.heatpump_power_device_uid == "HP-1"
    assert runtime.heatpump_power_source == "hems_energy_consumption:HP-1"
    assert runtime.heatpump_power_last_error == "power boom"

    runtime._heatpump_primary_member.assert_called_once_with()
    runtime._heatpump_primary_device_uid.assert_called_once_with()
    runtime._heatpump_runtime_device_uid.assert_called_once_with()
    runtime._heatpump_daily_window.assert_called_once_with()
    runtime._build_heatpump_daily_consumption_snapshot.assert_called_once_with(
        {"data": {}},
        _site_today_payload(123.0),
    )
    runtime._heatpump_power_candidate_device_uids.assert_called_once_with()
    runtime._heatpump_member_for_uid.assert_called_once_with("HP-1")
    runtime._heatpump_member_alias_map.assert_called_once_with()
    runtime._heatpump_power_inventory_marker.assert_called_once_with()
    runtime._heatpump_power_fetch_plan.assert_called_once_with()
    runtime._heatpump_power_candidate_is_recommended.assert_called_once_with("HP-1")
    runtime._heatpump_power_candidate_type_rank.assert_called_once_with(
        {},
        "HP-1",
        is_recommended=True,
    )
    runtime._heatpump_power_selection_key.assert_called_once_with(
        {},
        requested_uid="HP-1",
        sample=(0, 500.0),
    )
    runtime.async_refresh_hems_support_preflight.assert_awaited_once_with(force=True)
    runtime.async_ensure_heatpump_runtime_diagnostics.assert_awaited_once_with(
        force=True
    )
    runtime.async_refresh_heatpump_runtime_state.assert_awaited_once_with(force=True)
    runtime.async_refresh_heatpump_daily_consumption.assert_awaited_once_with(
        force=True
    )
    runtime.async_refresh_heatpump_power.assert_awaited_once_with(force=True)


def test_heatpump_and_parsing_helper_guards() -> None:
    class BadString:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    class BadFloat:
        def __float__(self) -> float:
            raise RuntimeError("boom")

    class BadFloatSubclass(float):
        def __float__(self) -> float:
            raise RuntimeError("boom")

    ts = parse_inverter_last_report(1711843200)
    assert ts == datetime.fromtimestamp(1711843200, tz=timezone.utc)
    assert parse_inverter_last_report(BadFloatSubclass(1.0)) is None
    assert parse_inverter_last_report(BadString()) is None
    assert parse_inverter_last_report("") is None
    assert parse_inverter_last_report("not-a-date") is None
    assert parse_inverter_last_report("1711843200000") == datetime(
        2024, 3, 31, 0, 0, tzinfo=timezone.utc
    )
    assert parse_inverter_last_report("2026-03-27T12:00:00") == datetime(
        2026, 3, 27, 12, 0, tzinfo=timezone.utc
    )
    assert parse_inverter_last_report("2026-03-27T12:00:00[UTC]") == datetime(
        2026, 3, 27, 12, 0, tzinfo=timezone.utc
    )


def test_heatpump_runtime_helper_edge_branches(coordinator_factory) -> None:
    runtime = coordinator_factory().heatpump_runtime

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {"devices": [{"device_type": "ENERGY_METER"}]}
    }
    assert runtime._heatpump_primary_member() == {
        "device_type": "ENERGY_METER"
    }  # noqa: SLF001

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "devices": [
                {"device_type": "ENERGY_METER"},
                {"device_type": "SG_READY_GATEWAY", "device_uid": "GW-1"},
                {
                    "device_type": "HEAT_PUMP",
                    "device_uid": "HP-1",
                    "uid": "HP-ALIAS",
                    "serial_number": "SER-1",
                    "parent": "GW-1",
                    "statusText": "Recommended",
                },
                {
                    "device_type": "HEAT_PUMP",
                    "uid": "HP-2",
                    "statusText": "Recommended",
                },
            ]
        }
    }

    assert runtime._heatpump_primary_device_uid() == "HP-1"  # noqa: SLF001
    assert runtime._heatpump_runtime_device_uid() == "HP-1"  # noqa: SLF001
    assert runtime._heatpump_member_for_uid("missing") is None  # noqa: SLF001
    assert runtime._heatpump_member_aliases(None) == []  # noqa: SLF001
    assert runtime._heatpump_member_alias_map()["HP-ALIAS"] == "HP-1"  # noqa: SLF001
    marker = runtime._heatpump_power_inventory_marker()  # noqa: SLF001
    assert marker[0][0] == "GW-1"
    assert (
        runtime._heatpump_power_candidate_is_recommended(None) is False
    )  # noqa: SLF001
    assert (
        runtime._heatpump_power_candidate_is_recommended("HP-1") is True
    )  # noqa: SLF001
    assert (
        runtime._heatpump_power_candidate_is_recommended("GW-1") is True
    )  # noqa: SLF001

    runtime._type_device_buckets = {"heatpump": {"devices": []}}  # noqa: SLF001
    assert (
        runtime._heatpump_power_candidate_is_recommended("HP-1") is False
    )  # noqa: SLF001

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {"devices": [{"device_uid": "FALLBACK-1"}]}
    }
    assert runtime._heatpump_primary_device_uid() == "FALLBACK-1"  # noqa: SLF001

    runtime._type_device_buckets = {"heatpump": {"devices": [{}]}}  # noqa: SLF001
    assert runtime._heatpump_primary_device_uid() is None  # noqa: SLF001
    assert runtime._heatpump_power_inventory_marker()[0][0] == "idx:0"  # noqa: SLF001


def test_heatpump_runtime_power_helper_edge_branches(monkeypatch) -> None:
    class BadStart:
        def __add__(self, _other):
            raise OverflowError("boom")

    assert (
        HeatpumpRuntime._infer_heatpump_interval_minutes(
            None, 1, datetime.now(timezone.utc)
        )
        is None
    )
    assert (
        HeatpumpRuntime._infer_heatpump_interval_minutes(
            BadStart(), 1, datetime.now(timezone.utc)
        )
        is None
    )
    assert HeatpumpRuntime._heatpump_latest_power_sample("bad") is None
    assert (
        HeatpumpRuntime._heatpump_latest_power_sample({"heat_pump_consumption": "bad"})
        is None
    )

    naive_now = datetime(2026, 3, 27, 12, 0)
    monkeypatch.setattr(heatpump_runtime_mod.dt_util, "utcnow", lambda: naive_now)

    future_payload = {
        "start_date": "3026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [1.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(future_payload) is None

    payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [None, "bad", float("inf"), 0.5, 10.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(payload) == (4, 10.0)

    open_only_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [None, None, 2.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(open_only_payload) == (2, 2.0)

    invalid_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [None, "bad", float("nan")],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(invalid_payload) is None

    monkeypatch.setattr(
        heatpump_runtime_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 3, 27, 2, 30),
    )
    provisional_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [50.0, 2.0, 0.1],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(provisional_payload) == (
        1,
        2.0,
    )

    completed_zero_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [50.0, 0.0, 1.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(completed_zero_payload) == (
        2,
        1.0,
    )

    open_missing_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [50.0, 2.0, None],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(open_missing_payload) == (
        1,
        2.0,
    )

    completed_missing_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [None, None, 3.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(completed_missing_payload) == (
        2,
        3.0,
    )

    open_selected_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [50.0, 2.0, 5.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(open_selected_payload) == (
        2,
        5.0,
    )

    assert (
        HeatpumpRuntime._infer_heatpump_interval_minutes(
            datetime(2026, 3, 27, tzinfo=timezone.utc),
            1,
            datetime(2026, 3, 28, tzinfo=timezone.utc),
        )
        == 60
    )


def test_heatpump_runtime_power_helper_additional_coverage(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 4,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "pairing-status": "PAIRED",
                        "device-state": "ACTIVE",
                        "statusText": "Normal",
                    },
                    {"device_type": "ENERGY_METER", "device_uid": "MTR-1"},
                    {"device_type": "SG_READY_GATEWAY", "device_uid": "SG-1"},
                    {"device_type": "OTHER", "device_uid": "OT-1"},
                ],
            }
        },
        ["heatpump"],
    )

    monkeypatch.setattr(
        heatpump_runtime_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 3, 27, 2, 5, tzinfo=timezone.utc),
    )
    assert HeatpumpRuntime._heatpump_latest_power_sample(
        {
            "start_date": "2026-03-27T00:00:00Z",
            "heat_pump_consumption": [1.0, 2.0],
        }
    ) == (1, 2.0)

    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "MTR-1",
            is_recommended=False,
        )
        == 0
    )
    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "MTR-1",
            is_recommended=True,
        )
        == 3
    )
    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "HP-1",
            is_recommended=True,
        )
        == 2
    )
    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "SG-1",
            is_recommended=True,
        )
        == 1
    )
    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "OT-1",
            is_recommended=True,
        )
        == 0
    )

    assert coord._heatpump_power_selection_key(  # noqa: SLF001
        {"device_uid": "HP-1"},
        requested_uid="REQ-1",
        sample=None,
    ) == (0, 0, 0, 0, float("-inf"), 1, -1)
    assert coord._heatpump_power_selection_key(  # noqa: SLF001
        {"uid": "MTR-1"},
        requested_uid=None,
        sample=(2, 0.0),
    ) == (1, 0, 0, 0, 0.0, 1, 2)

    summary = runtime._heatpump_power_debug_payload_summary(  # noqa: SLF001
        {
            "device_uid": "HP-1",
            "heat_pump_consumption": [None, "bad", 1.0, 2.0, float("inf")],
            "start_date": "2026-03-27T00:00:00Z",
            "interval_minutes": "15",
        },
        requested_uid="REQ-1",
        sample=(3, 2.0),
        selection_key=(1, 1, 1, 2, 2.0, 1, 3),
    )
    assert summary == {
        "requested_device_ref": "R...1",
        "payload_device_ref": "H...1",
        "resolved_device_ref": "H...1",
        "member_device_type": "HEAT_PUMP",
        "pairing_status": "PAIRED",
        "device_state": "ACTIVE",
        "status": "Normal",
        "recommended": False,
        "bucket_count": 5,
        "non_null_bucket_count": 3,
        "sample_tail": [
            {"index": 4, "value_w": float("inf")},
            {"index": 3, "value_w": 2.0},
            {"index": 2, "value_w": 1.0},
        ],
        "latest_sample_index": 3,
        "latest_sample_w": 2.0,
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 15.0,
        "selection_key": [1, 1, 1, 2, 2.0, 1, 3],
    }


@pytest.mark.asyncio
async def test_heatpump_runtime_diagnostics_and_refresh_edge_branches(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {"device_type": "HEAT_PUMP", "device_uid": "HP-1"},
                    {"device_type": "HEAT_PUMP", "device_uid": "HP-2"},
                    {"device_type": "HEAT_PUMP"},
                ],
            }
        },
        ["heatpump"],
    )

    runtime._async_refresh_heatpump_runtime_state = AsyncMock(side_effect=RuntimeError("runtime"))  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_daily_consumption = AsyncMock(side_effect=RuntimeError("daily"))  # type: ignore[assignment]  # noqa: SLF001
    coord.client.show_livestream = AsyncMock(return_value={"live": True})
    coord.client.heat_pump_events_json = AsyncMock(
        side_effect=["EVENT_SCALAR", ["list-payload"]]
    )
    coord.client.iq_er_events_json = AsyncMock(return_value="EVENT_NONE")

    def _redact(payload):
        if payload == "EVENT_SCALAR":
            return "scalar"
        if payload == "EVENT_NONE":
            return None
        return payload

    monkeypatch.setattr(heatpump_runtime_mod, "redact_battery_payload", _redact)

    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)

    diagnostics = coord.heatpump_runtime_diagnostics()
    assert diagnostics["show_livestream_payload"] == {"live": True}
    assert diagnostics["events_payloads"][0]["payload"] == {"value": "scalar"}
    assert diagnostics["events_payloads"][1]["payload"] == ["list-payload"]
    assert diagnostics["event_summary"] == {
        "known_event_counts": {},
        "unknown_event_keys": [],
    }

    runtime._heatpump_runtime_diagnostics_cache_until = None  # noqa: SLF001
    coord.client.show_livestream = AsyncMock(return_value=None)
    coord.client.heat_pump_events_json = None
    coord.client.iq_er_events_json = None
    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)
    assert coord.heatpump_runtime_diagnostics()["show_livestream_payload"] is None

    runtime._heatpump_runtime_diagnostics_cache_until = None  # noqa: SLF001
    coord.client.show_livestream = AsyncMock(side_effect=RuntimeError("live boom"))
    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)
    assert coord.heatpump_runtime_diagnostics()["show_livestream_payload"] is None
    assert coord.heatpump_runtime_diagnostics()["last_error"] == "live boom"

    runtime._async_refresh_heatpump_daily_consumption = HeatpumpRuntime._async_refresh_heatpump_daily_consumption.__get__(  # type: ignore[method-assign]  # noqa: SLF001
        runtime, HeatpumpRuntime
    )

    runtime._heatpump_power_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.hems_energy_consumption = AsyncMock(
        side_effect=AssertionError("no fetch")
    )
    await runtime.async_refresh_heatpump_power()
    coord.client.hems_energy_consumption.assert_not_awaited()

    runtime._heatpump_power_cache_until = None  # noqa: SLF001
    runtime._heatpump_power_backoff_until = time.monotonic() + 60  # noqa: SLF001
    await runtime.async_refresh_heatpump_power()
    coord.client.hems_energy_consumption.assert_not_awaited()

    runtime._heatpump_power_backoff_until = None  # noqa: SLF001
    coord.client._hems_site_supported = False  # noqa: SLF001
    await runtime.async_refresh_heatpump_power(force=True)
    assert coord.heatpump_power_source is None

    coord.client._hems_site_supported = True  # noqa: SLF001
    coord.client.hems_energy_consumption = None
    await runtime.async_refresh_heatpump_power(force=True)
    assert coord.heatpump_power_w is None

    coord.client.hems_energy_consumption = AsyncMock(return_value=None)
    await runtime.async_refresh_heatpump_power(force=True)
    assert coord.heatpump_power_w is None
    assert coord.heatpump_power_last_error == "No usable HEMS daily split payload"

    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "bad",
            "data": {
                "heat-pump": [
                    {"device_uid": "HP-1", "consumption": [{"details": [None]}]}
                ]
            },
        }
    )
    await runtime.async_refresh_heatpump_power(force=True)
    assert coord.heatpump_power_w is None
    assert coord.heatpump_power_last_error == "rejected_missing_energy_baseline"
    assert coord.heatpump_power_sample_utc is None

    class BadFloat:
        def __float__(self) -> float:
            raise RuntimeError("boom")

    class BadString:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert coerce_optional_float(BadFloat()) is None
    assert coerce_optional_float(float("inf")) == float("inf")
    assert coerce_optional_float(True) == 1.0
    assert coerce_optional_float("1,234.5") == pytest.approx(1234.5)
    assert coerce_optional_text(BadString()) is None
    assert coerce_optional_text("  hello  ") == "hello"
    assert coerce_optional_bool("enabled") is True
    assert coerce_optional_bool("disabled") is False
    assert coerce_optional_bool(None) is None
    assert type_member_text(None, "name") is None
    assert HeatpumpRuntime._first_optional_numeric_value("bad") is None
    assert (
        HeatpumpRuntime._first_optional_numeric_value(
            [None, "bad", float("inf"), float("nan")]
        )
        is None
    )
    assert HeatpumpRuntime._first_optional_numeric_value(
        [None, "2.5"]
    ) == pytest.approx(2.5)


def test_heatpump_runtime_recommended_parent_matching(coordinator_factory) -> None:
    runtime = coordinator_factory().heatpump_runtime

    class BadString:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "devices": [
                {"device_uid": "PARENT-1", "statusText": "Recommended"},
                {"device_uid": "CHILD-1", "parent": "PARENT-1"},
                {
                    "device_uid": "REC-CHILD",
                    "parent": "PARENT-1",
                    "statusText": "Recommended",
                },
            ]
        }
    }

    assert (
        runtime._heatpump_power_candidate_is_recommended("CHILD-1") is True
    )  # noqa: SLF001

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "devices": [
                {"device_uid": "CHILD-1", "parent": "PARENT-1"},
                {
                    "device_uid": "REC-CHILD",
                    "parent": "PARENT-1",
                    "statusText": "Recommended",
                },
            ]
        }
    }
    assert (
        runtime._heatpump_power_candidate_is_recommended("CHILD-1") is True
    )  # noqa: SLF001
    assert (
        type_member_text({"name": BadString(), "serial": "SERIAL-1"}, "name", "serial")
        == "SERIAL-1"
    )
    assert heatpump_member_device_type({"device-type": "iq_er"}) == "IQ_ER"
    assert heatpump_member_device_type({"device_type": BadString()}) is None
    assert heatpump_pairing_status(None) is None
    assert heatpump_device_state(None) is None
    assert heatpump_device_state({"device-state": "inactive"}) == "INACTIVE"
    assert parsing_helpers_mod._friendly_heatpump_status(None) is None  # noqa: SLF001
    assert heatpump_lifecycle_status_text(None) is None
    assert heatpump_operational_status_text(None) is None
    assert heatpump_status_bucket("") == "unknown"
    assert heatpump_status_bucket("unpaired") == "not_reporting"
    assert heatpump_status_bucket("pending") == "warning"
    assert heatpump_status_text({"statusText": "Running"}) == "Running"
    assert heatpump_status_text({"status": "not_reporting"}) == "Not Reporting"
    assert (
        heatpump_status_text({"statusText": "Fault", "pairing-status": "UNPAIRED"})
        == "Fault"
    )
    assert (
        heatpump_status_text({"statusText": "Normal", "device-state": "INACTIVE"})
        == "Inactive"
    )
    assert heatpump_status_text({"status": BadString()}) is None
    assert HeatpumpRuntime._sum_optional_values("bad") is None
    assert HeatpumpRuntime._sum_optional_values([1.0, float("inf"), 2.0]) == 3.0


def test_heatpump_runtime_type_helpers_cover_guard_paths(coordinator_factory) -> None:
    runtime = coordinator_factory().heatpump_runtime

    class BadCount:
        def __int__(self) -> int:
            raise RuntimeError("boom")

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {"count": BadCount(), "devices": "bad"},
        "envoy": {"count": 1, "devices": [{"serial": "ENV-1"}, "bad"]},
    }

    assert runtime.has_type(None) is False
    assert runtime._heatpump_runtime_member() is None  # noqa: SLF001
    assert runtime.has_type("heatpump") is False
    assert runtime._type_bucket_members(None) == []  # noqa: SLF001
    assert runtime._type_bucket_members("heatpump") == []  # noqa: SLF001
    assert runtime._type_bucket_members("envoy") == [
        {"serial": "ENV-1"}
    ]  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_heatpump_power_tracks_latest_valid_sample(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord._battery_timezone = "UTC"  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "statusText": "Normal",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-02-27T00:10:00Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-1",
                        "device_name": "Waermepumpe",
                        "consumption": [{"details": [500.5, None]}],
                    }
                ]
            },
        }
    )
    coord.client.hems_heatpump_state = AsyncMock(
        return_value={"device_uid": "HP-1", "heatpump_status": "RUNNING"}
    )
    _seed_previous_heatpump_daily_snapshot(
        coord,
        energy_wh=458.7916666667,
        timestamp="2026-02-27T00:05:00Z",
        day_key="2026-03-13",
        timezone_name="UTC",
    )

    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(500.5)
    assert coord.heatpump_power_device_uid == "HP-1"
    assert coord.heatpump_power_source == "hems_energy_consumption_delta:HP-1"
    assert coord.heatpump_power_start_utc == datetime(
        2026, 2, 27, 0, 5, tzinfo=timezone.utc
    )
    assert coord.heatpump_power_sample_utc == datetime(
        2026, 2, 27, 0, 10, tzinfo=timezone.utc
    )
    assert coord.heatpump_power_last_error is None
    assert coord._heatpump_power_cache_until is not None  # noqa: SLF001
    assert (
        coord._heatpump_power_cache_until
        == coord._heatpump_daily_consumption_cache_until
    )  # noqa: SLF001
    first_call = coord.client.hems_energy_consumption.await_args_list[0]
    assert first_call.kwargs["timezone"] == "UTC"
    assert first_call.kwargs["step"] == "P1D"

    coord._heatpump_power_cache_until = None  # noqa: SLF001
    coord.client.hems_energy_consumption = AsyncMock(side_effect=RuntimeError("boom"))
    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_power_last_error == "boom"
    assert coord.heatpump_runtime.heatpump_power_using_stale is True
    assert coord._heatpump_power_backoff_until is None  # noqa: SLF001
    assert coord._heatpump_power_cache_until is not None  # noqa: SLF001

    coord.inventory_runtime._set_type_device_buckets({}, [])  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_power_w == pytest.approx(500.5)
    assert coord.heatpump_power_source == "hems_energy_consumption_delta:HP-1"
    assert coord.heatpump_power_using_stale is True
    assert (
        coord.heatpump_power_last_error
        == "Heat pump type temporarily missing from inventory"
    )


@pytest.mark.asyncio
async def test_refresh_heatpump_power_validates_energy_consumption_payload(
    coordinator_factory, caplog
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-04-02"}  # noqa: SLF001
    coord._battery_timezone = "Europe/Berlin"  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "statusText": "Normal",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-04-01T22:31:12.407610009Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-1",
                        "device_name": "Waermepumpe",
                        "consumption": [
                            {
                                "solar": 0,
                                "battery": 0,
                                "grid": 0,
                                "details": [55.0],
                            }
                        ],
                    }
                ]
            },
        }
    )
    coord.client.hems_heatpump_state = AsyncMock(
        return_value={"device_uid": "HP-1", "heatpump_status": "IDLE"}
    )
    _seed_previous_heatpump_daily_snapshot(
        coord,
        energy_wh=45.8333333333,
        timestamp="2026-04-01T22:26:12.407610009Z",
        day_key="2026-04-02",
        timezone_name="Europe/Berlin",
    )

    with caplog.at_level(logging.DEBUG):
        await coord.heatpump_runtime._async_refresh_heatpump_power(
            force=True
        )  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(0.0)
    assert coord.heatpump_power_device_uid == "HP-1"
    assert coord.heatpump_power_source == "hems_energy_consumption_delta:HP-1"
    assert coord.heatpump_power_sample_utc == datetime(
        2026, 4, 1, 22, 31, 12, 407610, tzinfo=timezone.utc
    )
    assert coord.heatpump_power_start_utc == datetime(
        2026, 4, 1, 22, 26, 12, 407610, tzinfo=timezone.utc
    )
    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["outcome"] == "selected_sample"
    assert power_snapshot["selected_source"] == "hems_energy_consumption_delta:H...1"
    assert power_snapshot["selected_payload"]["accepted_value_w"] == pytest.approx(0.0)
    assert (
        power_snapshot["selected_payload"]["validation"] == "coerced_idle_high_to_zero"
    )
    assert "Heat pump power selected payload for site" in caplog.text


@pytest.mark.asyncio
async def test_refresh_heatpump_power_preserves_small_idle_value(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-04-02"}  # noqa: SLF001
    coord._battery_timezone = "Europe/Berlin"  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "statusText": "Normal",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-04-01T22:31:12.407610009Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-1",
                        "device_name": "Waermepumpe",
                        "consumption": [
                            {
                                "solar": 0,
                                "battery": 0,
                                "grid": 0,
                                "details": [4.0],
                            }
                        ],
                    }
                ]
            },
        }
    )
    coord.client.hems_heatpump_state = AsyncMock(
        return_value={"device_uid": "HP-1", "heatpump_status": "IDLE"}
    )
    _seed_previous_heatpump_daily_snapshot(
        coord,
        energy_wh=3.0,
        timestamp="2026-04-01T22:16:12.407610009Z",
        day_key="2026-04-02",
        timezone_name="Europe/Berlin",
    )

    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(4.0)
    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["selected_payload"]["accepted_value_w"] == pytest.approx(4.0)
    assert power_snapshot["selected_payload"]["validation"] == "accepted_idle_delta"


@pytest.mark.asyncio
async def test_runtime_state_preserves_stale_snapshot_on_unusable_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord._heatpump_runtime_state = {
        "device_uid": "HP-1",
        "heatpump_status": "RUNNING",
    }  # noqa: SLF001
    coord._heatpump_runtime_state_last_success_mono = (
        time.monotonic() - 5
    )  # noqa: SLF001
    coord._heatpump_runtime_state_last_success_utc = datetime(  # noqa: SLF001
        2026, 4, 5, 0, 0, tzinfo=timezone.utc
    )
    coord.client.hems_heatpump_state = AsyncMock(return_value=None)

    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001

    assert coord.heatpump_runtime_state["heatpump_status"] == "RUNNING"
    assert coord.heatpump_runtime_state_last_error == "No usable HEMS runtime payload"
    assert coord.heatpump_runtime.heatpump_runtime_state_using_stale is True
    diag = coord.heatpump_runtime.heatpump_runtime_diagnostics()
    assert diag["runtime_state_using_stale"] is True
    assert diag["runtime_state_last_success_utc"] == "2026-04-05T00:00:00+00:00"


@pytest.mark.asyncio
async def test_daily_consumption_preserves_stale_snapshot_on_unusable_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-04-05"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord._heatpump_daily_consumption = {
        "device_uid": None,
        "daily_energy_wh": 55.0,
        "split_device_uid": "HP-1",
        "split_device_name": "Heat Pump",
        "split_daily_energy_wh": 55.0,
        "daily_grid_wh": 50.0,
        "daily_solar_wh": 5.0,
        "daily_battery_wh": 0.0,
        "details": [55.0],
        "source": "site_today_heatpump",
        "split_source": "hems_energy_consumption:HP-1",
        "day_key": "2026-04-05",
        "timezone": "UTC",
    }  # noqa: SLF001
    coord._heatpump_daily_consumption_last_success_mono = (
        time.monotonic() - 5
    )  # noqa: SLF001
    coord._heatpump_daily_consumption_last_success_utc = datetime(  # noqa: SLF001
        2026, 4, 5, 0, 0, tzinfo=timezone.utc
    )
    coord._heatpump_daily_split_last_success_mono = time.monotonic() - 5  # noqa: SLF001
    coord._heatpump_daily_split_last_success_utc = datetime(  # noqa: SLF001
        2026, 4, 5, 0, 0, tzinfo=timezone.utc
    )
    coord.client.hems_energy_consumption = AsyncMock(return_value=None)
    coord.client.pv_system_today = AsyncMock(return_value=_site_today_payload(77.0))

    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001

    assert coord.heatpump_daily_consumption["daily_energy_wh"] == pytest.approx(77.0)
    assert coord.heatpump_daily_consumption["split_daily_energy_wh"] == pytest.approx(
        55.0
    )
    assert coord.heatpump_daily_consumption_last_error is None
    assert coord.heatpump_runtime.heatpump_daily_consumption_using_stale is False
    assert coord.heatpump_runtime.heatpump_daily_split_using_stale is True
    assert (
        coord.heatpump_runtime.heatpump_daily_split_last_error
        == "No usable HEMS daily split payload"
    )
    diag = coord.heatpump_runtime.heatpump_runtime_diagnostics()
    assert diag["daily_consumption_using_stale"] is False
    assert diag["daily_split_using_stale"] is True
    assert diag["daily_split_last_success_utc"] == "2026-04-05T00:00:00+00:00"


@pytest.mark.asyncio
async def test_heatpump_power_preserves_stale_sample_on_no_usable_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-04-05"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord._heatpump_power_w = 520.0  # noqa: SLF001
    coord._heatpump_power_device_uid = "HP-1"  # noqa: SLF001
    coord._heatpump_power_source = "hems_energy_consumption_delta:HP-1"  # noqa: SLF001
    coord._heatpump_power_last_success_mono = time.monotonic() - 5  # noqa: SLF001
    coord._heatpump_power_last_success_utc = datetime(  # noqa: SLF001
        2026, 4, 5, 0, 0, tzinfo=timezone.utc
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-04-05T00:05:00Z",
            "data": {
                "heat-pump": [
                    {"device_uid": "HP-1", "consumption": [{"details": [0.0]}]}
                ]
            },
        }
    )
    coord.client.hems_heatpump_state = AsyncMock(
        return_value={"device_uid": "HP-1", "heatpump_status": "RUNNING"}
    )

    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(520.0)
    assert coord.heatpump_power_last_error == "seeded_waiting_for_delta"
    assert coord.heatpump_runtime.heatpump_power_using_stale is True
    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["using_stale"] is True
    assert power_snapshot["last_success_utc"] == "2026-04-05T00:00:00+00:00"


@pytest.mark.asyncio
async def test_refresh_heatpump_runtime_state_uses_dedicated_heatpump_uid(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 3,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-1",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-EM-1",
                    },
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "name": "Primary Heat Pump",
                        "pairing_status": "PAIRED",
                        "device_state": "ACTIVE",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_heatpump_state = AsyncMock(
        return_value={
            "device_uid": "HP-1",
            "heatpump_status": "RUNNING",
            "sg_ready_mode_raw": "MODE_3",
            "sg_ready_mode_label": "Recommended",
            "sg_ready_active": True,
            "sg_ready_contact_state": "closed",
            "last_report_at": "2026-03-20T08:18:59.604Z",
        }
    )

    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001

    coord.client.hems_heatpump_state.assert_awaited_once()
    assert coord.client.hems_heatpump_state.await_args.kwargs["device_uid"] == "HP-1"
    assert coord.heatpump_runtime_state["device_uid"] == "HP-1"
    assert coord.heatpump_runtime_state["member_name"] == "Primary Heat Pump"
    assert coord.heatpump_runtime_state["member_device_type"] == "HEAT_PUMP"
    assert coord.heatpump_runtime_state["pairing_status"] == "PAIRED"
    assert coord.heatpump_runtime_state["device_state"] == "ACTIVE"
    assert coord.heatpump_runtime_state["source"] == "hems_heatpump_state:HP-1"


@pytest.mark.asyncio
async def test_refresh_heatpump_runtime_state_covers_cache_and_error_paths(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "name": "Primary Heat Pump",
                        "pairing_status": "PAIRED",
                        "device_state": "ACTIVE",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord.heatpump_runtime._async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value=None
    )
    mono_now = 1_000.0
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: mono_now)

    coord.client.hems_heatpump_state = AsyncMock(side_effect=AssertionError("cached"))
    coord._heatpump_runtime_state_cache_until = mono_now + 10  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state()  # noqa: SLF001
    coord.client.hems_heatpump_state.assert_not_awaited()

    coord._heatpump_runtime_state_cache_until = None  # noqa: SLF001
    coord._heatpump_runtime_state_backoff_until = mono_now + 10  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state()  # noqa: SLF001
    coord.client.hems_heatpump_state.assert_not_awaited()

    coord._heatpump_runtime_state_backoff_until = None  # noqa: SLF001
    coord.client._hems_site_supported = False  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_runtime_state == {}
    assert (
        coord.heatpump_runtime_state_last_error
        == "HEMS runtime endpoint unavailable for this site"
    )

    coord.client._hems_site_supported = None  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP"}],
            }
        },
        ["heatpump"],
    )
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_runtime_state == {}

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_heatpump_state = None
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001

    coord.client.hems_heatpump_state = AsyncMock(side_effect=RuntimeError("boom"))
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_runtime_state_last_error == "boom"
    assert coord._heatpump_runtime_state_backoff_until is not None  # noqa: SLF001

    coord._heatpump_runtime_state_backoff_until = None  # noqa: SLF001
    coord.client.hems_heatpump_state = AsyncMock(return_value=None)
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_runtime_state == {}


@pytest.mark.asyncio
async def test_refresh_heatpump_daily_consumption_tracks_site_day(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-20"}  # noqa: SLF001
    coord._battery_timezone = "Europe/Berlin"  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-03-20T07:53:00.739143826Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-1",
                        "device_name": "Waermepumpe",
                        "consumption": [
                            {
                                "solar": 10.0,
                                "battery": 20.0,
                                "grid": 200.0,
                                "details": [230.0],
                            }
                        ],
                    }
                ]
            },
        }
    )
    coord.client.pv_system_today = AsyncMock(
        return_value=_site_today_payload(275.0, timestamp="2026-03-20T08:00:00Z")
    )

    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(  # noqa: SLF001
        force=True
    )

    coord.client.hems_energy_consumption.assert_awaited_once()
    coord.client.pv_system_today.assert_awaited_once()
    kwargs = coord.client.hems_energy_consumption.await_args.kwargs
    assert kwargs["timezone"] == "Europe/Berlin"
    assert kwargs["step"] == "P1D"
    assert kwargs["start_at"] == "2026-03-19T23:00:00.000Z"
    assert kwargs["end_at"] == "2026-03-20T22:59:59.999Z"
    assert coord.heatpump_daily_consumption["daily_energy_wh"] == pytest.approx(275.0)
    assert coord.heatpump_daily_consumption["split_daily_energy_wh"] == pytest.approx(
        230.0
    )
    assert coord.heatpump_daily_consumption["daily_grid_wh"] == pytest.approx(200.0)
    assert coord.heatpump_daily_consumption["source"] == "site_today_heatpump"
    assert (
        coord.heatpump_daily_consumption["split_source"]
        == "hems_energy_consumption:HP-1"
    )
    assert (
        coord.heatpump_daily_consumption["sampled_at_utc"]
        == "2026-03-20T08:00:00+00:00"
    )


def test_heatpump_daily_window_handles_dst_transition_day(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._battery_timezone = "Europe/Berlin"  # noqa: SLF001
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-29"}  # noqa: SLF001

    assert coord.heatpump_runtime._heatpump_daily_window() == (  # noqa: SLF001
        "2026-03-28T23:00:00.000Z",
        "2026-03-29T21:59:59.999Z",
        "Europe/Berlin",
        ("2026-03-29", "Europe/Berlin"),
    )


def test_heatpump_daily_helper_and_property_edge_cases(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._battery_timezone = "Not/A-Timezone"  # noqa: SLF001
    assert coord._site_timezone_name() == "UTC"  # noqa: SLF001
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        heatpump_runtime_mod,
        "resolve_site_timezone_name",
        lambda _value: "Not/A-Timezone",
    )
    monkeypatch.setattr(
        heatpump_runtime_mod,
        "resolve_site_local_current_date",
        lambda _payload, _timezone: "bad-date",
    )
    assert coord.heatpump_runtime._heatpump_daily_window() is None  # noqa: SLF001
    monkeypatch.undo()

    assert coord._sum_optional_values("bad") is None  # noqa: SLF001
    assert (
        coord._sum_optional_values([None, "bad", float("inf")]) is None
    )  # noqa: SLF001
    assert coord._sum_optional_values([1.0, "2.5", float("nan")]) == pytest.approx(
        3.5
    )  # noqa: SLF001

    snapshot = coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        ["bad"], _site_today_payload(10.0)
    )
    assert snapshot["daily_energy_wh"] == pytest.approx(10.0)
    assert snapshot["split_source"] is None
    snapshot = coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        {"data": []}, _site_today_payload(10.0)
    )
    assert snapshot["daily_energy_wh"] == pytest.approx(10.0)
    assert snapshot["split_source"] is None
    snapshot = coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        {"data": {"heat-pump": []}},
        _site_today_payload(10.0),
    )
    assert snapshot["daily_energy_wh"] == pytest.approx(10.0)
    assert snapshot["split_source"] is None

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    snapshot = coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        {
            "data": {
                "heat-pump": [
                    "skip-me",
                    {
                        "device_uid": "HP-2",
                        "device_name": "Backup",
                        "consumption": [
                            "skip-me",
                            {
                                "solar": "1.0",
                                "battery": "2.0",
                                "grid": "3.0",
                                "details": [4.0, "bad", None],
                            },
                        ],
                    },
                ]
            }
        },
        _site_today_payload(10.0),
    )
    assert snapshot["daily_energy_wh"] == pytest.approx(10.0)
    assert snapshot["split_source"] is None

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "ENERGY_METER", "device_uid": "HP-EM-1"}],
            }
        },
        ["heatpump"],
    )
    snapshot = coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        {
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-2",
                        "device_name": "Backup",
                        "consumption": [
                            {
                                "solar": "1.0",
                                "battery": "2.0",
                                "grid": "3.0",
                                "details": [4.0, "bad", None],
                            },
                        ],
                    }
                ]
            }
        },
        _site_today_payload(10.0),
    )
    assert snapshot == {
        "device_uid": None,
        "device_name": None,
        "split_device_uid": "HP-2",
        "split_device_name": "Backup",
        "member_name": None,
        "member_device_type": "ENERGY_METER",
        "pairing_status": None,
        "device_state": None,
        "daily_energy_wh": pytest.approx(10.0),
        "split_daily_energy_wh": pytest.approx(4.0),
        "daily_solar_wh": pytest.approx(1.0),
        "daily_battery_wh": pytest.approx(2.0),
        "daily_grid_wh": pytest.approx(3.0),
        "details": [4.0, "bad", None],
        "source": "site_today_heatpump",
        "split_source": "hems_energy_consumption:HP-2",
        "endpoint_type": None,
        "endpoint_timestamp": None,
        "split_endpoint_type": None,
        "split_endpoint_timestamp": None,
        "sampled_at_utc": None,
    }
    snapshot = coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        {
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-2",
                        "device_name": "Backup",
                        "consumption": [
                            {
                                "solar": 0.0,
                                "battery": 0.0,
                                "grid": 0.0,
                                "details": [],
                            },
                        ],
                    }
                ]
            }
        },
        _site_today_payload(0.0),
    )
    assert snapshot == {
        "device_uid": None,
        "device_name": None,
        "split_device_uid": "HP-2",
        "split_device_name": "Backup",
        "member_name": None,
        "member_device_type": "ENERGY_METER",
        "pairing_status": None,
        "device_state": None,
        "daily_energy_wh": pytest.approx(0.0),
        "split_daily_energy_wh": pytest.approx(0.0),
        "daily_solar_wh": pytest.approx(0.0),
        "daily_battery_wh": pytest.approx(0.0),
        "daily_grid_wh": pytest.approx(0.0),
        "details": [],
        "source": "site_today_heatpump",
        "split_source": "hems_energy_consumption:HP-2",
        "endpoint_type": None,
        "endpoint_timestamp": None,
        "split_endpoint_type": None,
        "split_endpoint_timestamp": None,
        "sampled_at_utc": None,
    }
    snapshot = coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        {"data": {"heat-pump": [{"device_uid": "HP-1", "consumption": ["bad"]}]}},
        _site_today_payload(10.0),
    )
    assert snapshot["daily_energy_wh"] == pytest.approx(10.0)
    assert snapshot["split_source"] is None


def test_heatpump_event_summary_classifies_known_event_keys(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.heatpump_runtime._heatpump_events_payloads = [  # noqa: SLF001
        {
            "device_uid": "HP-1",
            "payload": [
                {"event_key": "hems_sgready_mode_changed_to_3"},
                {"eventKey": "hems_energy_meter_offline"},
                {"event_key": "custom_unknown_event"},
            ],
        }
    ]

    assert coord.heatpump_runtime._heatpump_event_summary() == {  # noqa: SLF001
        "known_event_counts": {
            "sg_ready_recommended": 1,
            "energy_meter_offline": 1,
        },
        "unknown_event_keys": ["custom_unknown_event"],
    }
    assert coord.heatpump_runtime._hems_event_entries(  # noqa: SLF001
        {"items": [{"event_key": "hems_sgready_mode_changed_to_2"}, "skip-me"]}
    ) == [{"event_key": "hems_sgready_mode_changed_to_2"}]
    coord.heatpump_runtime._heatpump_events_payloads = [  # noqa: SLF001
        {"payload": {"events": [{"event_key": None}, {"eventKey": ""}]}}
    ]
    assert coord.heatpump_runtime._heatpump_event_summary() == {  # noqa: SLF001
        "known_event_counts": {},
        "unknown_event_keys": [],
    }
    snapshot = coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        {"data": {"heat-pump": ["skip-me"]}},
        _site_today_payload(10.0),
    )
    assert snapshot["daily_energy_wh"] == pytest.approx(10.0)
    assert snapshot["split_source"] is None

    class BadString:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert coord.heatpump_runtime_state == {}
    coord._heatpump_runtime_state_last_error = BadString()  # noqa: SLF001
    assert coord.heatpump_runtime_state_last_error is None
    assert coord.heatpump_daily_consumption == {}
    coord._heatpump_daily_consumption_last_error = BadString()  # noqa: SLF001
    assert coord.heatpump_daily_consumption_last_error is None


def test_heatpump_runtime_additional_helper_edge_cases(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime

    assert runtime.heatpump_entities_established() is False
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    assert runtime.heatpump_entities_established() is True
    coord.inventory_runtime._set_type_device_buckets({}, [])

    coord._heatpump_daily_consumption = {"daily_energy_wh": 12.0}  # noqa: SLF001
    coord._heatpump_daily_consumption_last_success_mono = (
        time.monotonic()
    )  # noqa: SLF001
    assert (
        runtime._heatpump_mark_daily_consumption_stale(  # noqa: SLF001
            now=time.monotonic(),
            error="stale",
        )
        is True
    )

    assert (
        runtime._heatpump_daily_split_available({"daily_grid_wh": 0.0})  # noqa: SLF001
        is True
    )


def test_heatpump_runtime_refresh_due_requests_cleanup_when_type_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime

    coord.inventory_runtime._set_type_device_buckets({}, [])  # noqa: SLF001
    coord._heatpump_runtime_state = {"source": "cached"}  # noqa: SLF001
    coord._heatpump_daily_consumption = {"daily_energy_wh": 12.0}  # noqa: SLF001
    coord._heatpump_power_w = 500.5  # noqa: SLF001

    assert runtime.heatpump_runtime_state_refresh_due() is True
    assert runtime.heatpump_daily_consumption_refresh_due() is True
    assert runtime.heatpump_power_refresh_due() is True


def test_heatpump_runtime_refresh_due_skips_when_type_missing_and_no_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime

    assert runtime.has_type("heatpump") is False
    assert runtime.heatpump_runtime_state_refresh_due() is False
    assert runtime.heatpump_daily_consumption_refresh_due() is False
    assert runtime.heatpump_power_refresh_due() is False
    target = {"split_source": "keep"}
    assert (
        runtime._heatpump_copy_daily_split_fields(
            target, {"details": []}
        )  # noqa: SLF001
        is False
    )
    assert target["split_source"] is None
    assert target["details"] == []

    assert (
        runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
            {"data": {"heat-pump": []}},
            {"stats": []},
        )
        is None
    )

    assert runtime._site_today_heatpump_numeric_total(
        {"a": {"b": 2}}
    ) == pytest.approx(  # noqa: SLF001
        2.0
    )
    assert (
        runtime._site_today_heatpump_numeric_total({"a": "bad"}) is None
    )  # noqa: SLF001
    assert runtime._site_today_heatpump_numeric_total(
        [1, {"a": 2}]
    ) == pytest.approx(  # noqa: SLF001
        3.0
    )
    assert (
        runtime._site_today_heatpump_numeric_total([None, "bad"]) is None
    )  # noqa: SLF001
    assert runtime._site_today_heatpump_numeric_total("bad") is None  # noqa: SLF001
    assert (
        runtime._site_today_heatpump_numeric_total(float("inf")) is None
    )  # noqa: SLF001
    assert runtime._site_today_heatpump_total_wh(None) is None  # noqa: SLF001
    assert runtime._site_today_heatpump_total_wh({"stats": []}) is None  # noqa: SLF001
    assert (
        runtime._site_today_heatpump_total_wh({"stats": ["bad"]}) is None
    )  # noqa: SLF001


def test_heatpump_cleanup_due_treats_true_bool_as_cleanup_needed(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory(serials=[]).heatpump_runtime
    runtime._heatpump_known_present = True  # noqa: SLF001

    assert (
        runtime._heatpump_cleanup_due("_heatpump_known_present") is True
    )  # noqa: SLF001

    runtime._heatpump_power_sample_history = [  # noqa: SLF001
        {
            "device_uid": "HP-1",
            "day_key": "2026-04-02",
            "timezone": "UTC",
            "energy_wh": 100.0,
            "sample_utc": datetime(2026, 4, 2, tzinfo=timezone.utc),
        }
    ]
    assert (
        runtime._heatpump_cleanup_due("_heatpump_power_sample_history") is True
    )  # noqa: SLF001


def test_heatpump_runtime_state_refresh_due_covers_cache_backoff_and_fetcher(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {"heatpump": {"type_key": "heatpump", "count": 1, "devices": [{}]}},
        ["heatpump"],
    )
    monkeypatch.setattr(heatpump_runtime_mod.time, "monotonic", lambda: 100.0)

    runtime._heatpump_runtime_state_cache_until = 150.0  # noqa: SLF001
    assert runtime.heatpump_runtime_state_refresh_due() is False

    runtime._heatpump_runtime_state_cache_until = None  # noqa: SLF001
    runtime._heatpump_runtime_state_backoff_until = 150.0  # noqa: SLF001
    assert runtime.heatpump_runtime_state_refresh_due() is False

    runtime._heatpump_runtime_state_backoff_until = None  # noqa: SLF001
    coord.client.hems_heatpump_state = None
    assert runtime.heatpump_runtime_state_refresh_due() is False

    coord.client.hems_heatpump_state = AsyncMock(return_value={})
    assert runtime.heatpump_runtime_state_refresh_due() is True


def test_heatpump_daily_consumption_refresh_due_covers_window_cache_backoff_and_fetcher(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {"heatpump": {"type_key": "heatpump", "count": 1, "devices": [{}]}},
        ["heatpump"],
    )
    monkeypatch.setattr(heatpump_runtime_mod.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        runtime,
        "_heatpump_daily_window",
        lambda: ("start", "end", "UTC", ("2026-04-23", "UTC")),
    )

    runtime._heatpump_daily_consumption_cache_until = 150.0  # noqa: SLF001
    runtime._heatpump_daily_consumption_cache_key = (
        "2026-04-23",
        "UTC",
    )  # noqa: SLF001
    assert runtime.heatpump_daily_consumption_refresh_due() is False

    runtime._heatpump_daily_consumption_cache_until = None  # noqa: SLF001
    runtime._heatpump_daily_consumption_backoff_until = 150.0  # noqa: SLF001
    assert runtime.heatpump_daily_consumption_refresh_due() is False

    runtime._heatpump_daily_consumption_backoff_until = None  # noqa: SLF001
    coord.client.pv_system_today = None
    assert runtime.heatpump_daily_consumption_refresh_due() is False

    coord.client.pv_system_today = AsyncMock(return_value={})
    assert runtime.heatpump_daily_consumption_refresh_due() is True


def test_heatpump_daily_consumption_refresh_due_returns_false_without_window(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {"heatpump": {"type_key": "heatpump", "count": 1, "devices": [{}]}},
        ["heatpump"],
    )
    monkeypatch.setattr(runtime, "_heatpump_daily_window", lambda: None)

    assert runtime.heatpump_daily_consumption_refresh_due() is False


def test_heatpump_power_refresh_due_covers_cache_backoff_and_fetcher(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {"heatpump": {"type_key": "heatpump", "count": 1, "devices": [{}]}},
        ["heatpump"],
    )
    monkeypatch.setattr(heatpump_runtime_mod.time, "monotonic", lambda: 100.0)

    runtime._heatpump_power_cache_until = 150.0  # noqa: SLF001
    assert runtime.heatpump_power_refresh_due() is False

    runtime._heatpump_power_cache_until = None  # noqa: SLF001
    runtime._heatpump_power_backoff_until = 150.0  # noqa: SLF001
    assert runtime.heatpump_power_refresh_due() is False

    runtime._heatpump_power_backoff_until = None  # noqa: SLF001
    coord.client.pv_system_today = None
    assert runtime.heatpump_power_refresh_due() is False

    coord.client.pv_system_today = AsyncMock(return_value={})
    assert runtime.heatpump_power_refresh_due() is True
    assert (
        runtime._site_today_heatpump_total_wh({"stats": [{"other": 1}]}) is None
    )  # noqa: SLF001

    assert (
        runtime._heatpump_power_summary_from_daily_snapshot(None) is None
    )  # noqa: SLF001
    assert (
        runtime._heatpump_power_summary_from_daily_snapshot(  # noqa: SLF001
            {"daily_energy_wh": 10.0}
        )
        is None
    )

    assert runtime.heatpump_daily_split_last_error is None

    class BadString:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord._heatpump_daily_consumption_last_error = "   "  # noqa: SLF001
    assert coord.heatpump_daily_consumption_last_error is None
    coord._heatpump_daily_split_last_error = BadString()  # noqa: SLF001
    assert coord.heatpump_runtime.heatpump_daily_split_last_error is None


@pytest.mark.asyncio
async def test_refresh_heatpump_daily_consumption_covers_cache_and_error_paths(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord.heatpump_runtime._async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value=None
    )
    mono_now = 2_000.0
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: mono_now)
    marker = ("2026-03-20", "UTC")
    coord.heatpump_runtime._heatpump_daily_window = lambda: (  # type: ignore[assignment]  # noqa: SLF001
        "2026-03-20T00:00:00+00:00",
        "2026-03-21T00:00:00+00:00",
        "UTC",
        marker,
    )

    coord.client.hems_energy_consumption = AsyncMock(
        side_effect=AssertionError("cached")
    )
    coord._heatpump_daily_consumption_cache_key = marker  # noqa: SLF001
    coord._heatpump_daily_consumption_cache_until = mono_now + 10  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption()  # noqa: SLF001
    coord.client.hems_energy_consumption.assert_not_awaited()

    coord._heatpump_daily_consumption_cache_until = None  # noqa: SLF001
    coord._heatpump_daily_consumption_backoff_until = mono_now + 10  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption()  # noqa: SLF001
    coord.client.hems_energy_consumption.assert_not_awaited()

    coord.heatpump_runtime._heatpump_daily_window = lambda: None  # type: ignore[assignment]  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001

    coord.heatpump_runtime._heatpump_daily_window = lambda: (  # type: ignore[assignment]  # noqa: SLF001
        "2026-03-20T00:00:00+00:00",
        "2026-03-21T00:00:00+00:00",
        "UTC",
        marker,
    )
    coord.client._hems_site_supported = False  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_daily_consumption["source"] == "site_today_heatpump"
    assert coord.heatpump_daily_consumption_last_error is None

    coord.client._hems_site_supported = None  # noqa: SLF001
    coord.client.pv_system_today = None  # type: ignore[assignment]
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001

    coord.client.pv_system_today = AsyncMock(side_effect=RuntimeError("site boom"))
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_daily_consumption_last_error == "site boom"
    assert coord._heatpump_daily_consumption_backoff_until is not None  # noqa: SLF001

    coord._heatpump_daily_consumption_backoff_until = None  # noqa: SLF001
    coord.client.pv_system_today = AsyncMock(return_value="bad")
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001
    assert (
        coord.heatpump_daily_consumption_last_error
        == "No usable site today heat-pump payload"
    )

    coord.client.pv_system_today = AsyncMock(return_value={"stats": []})
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001
    assert (
        coord.heatpump_daily_consumption_last_error
        == "No usable site today heat-pump payload"
    )


def test_heatpump_runtime_inventory_merge_and_helper_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    inventory = coord.inventory_runtime

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert inventory._devices_inventory_buckets([{"ok": 1}, "bad"]) == [
        {"ok": 1}
    ]  # noqa: SLF001
    assert (
        inventory._hems_devices_groups({"data": {"hems-devices": []}}) == []
    )  # noqa: SLF001
    normalized = inventory._normalize_hems_member(
        {"device-uid": "HP-1", "serial": "SER-1"}
    )  # noqa: SLF001
    assert normalized["uid"] == "HP-1"
    assert normalized["serial_number"] == "SER-1"
    assert inventory._hems_bucket_type(BadStr()) is None  # noqa: SLF001
    assert (
        inventory._heatpump_worst_status_text({"warning": 1}) == "Warning"
    )  # noqa: SLF001
    assert (
        inventory._heatpump_worst_status_text({"normal": 1}) == "Normal"
    )  # noqa: SLF001

    coord.inventory_runtime._set_type_device_buckets(
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
            }
        },
        ["envoy"],
    )
    inventory._hems_devices_payload = {
        "data": {
            "hems-devices": {
                "heat-pump": [
                    {
                        "device-type": "SG_READY_GATEWAY",
                        "device-uid": "HP-SG-1",
                        "name": "SG Ready Gateway",
                        "last-report": "2026-02-27T09:14:44Z",
                        "status": "normal",
                        "statusText": "Normal",
                        "model": "Expert Net Control 2302",
                    },
                    {
                        "device-type": "ENERGY_METER",
                        "device-uid": "HP-EM-1",
                        "name": "Energy Meter",
                        "last-report": "2026-02-27T09:15:44Z",
                        "statusText": "Warning",
                        "firmware-version": "3.3",
                        "model": "Energy Manager 420",
                    },
                    {
                        "device-type": "HEAT_PUMP",
                        "device-uid": "HP-1",
                        "name": "Waermepumpe",
                        "statusText": "Normal",
                        "model": "Europa Mini WP",
                        "hardware-sku": "HP-SKU-1",
                    },
                ]
            }
        }
    }  # noqa: SLF001
    inventory._merge_heatpump_type_bucket()  # noqa: SLF001
    bucket = coord.inventory_view.type_bucket("heatpump")
    assert bucket is not None
    assert bucket["count"] == 3
    assert bucket["status_counts"]["warning"] == 1
    assert bucket["latest_reported_device"]["device_uid"] == "HP-EM-1"
    assert coord.inventory_view.type_device_model("heatpump") == "Europa Mini WP"
    assert coord.inventory_view.type_device_model_id("heatpump") == "HP-SKU-1"

    inventory._hems_devices_payload = None  # noqa: SLF001
    inventory._devices_inventory_payload = {
        "result": [
            {
                "type": "hemsDevices",
                "devices": [
                    {
                        "gateway": [
                            {
                                "device-type": "IQ_ENERGY_ROUTER",
                                "device-uid": "ROUTER-1",
                            }
                        ]
                    }
                ],
            }
        ]
    }  # noqa: SLF001
    assert (
        inventory._hems_group_members("gateway")[0]["device_uid"] == "ROUTER-1"
    )  # noqa: SLF001


def test_heatpump_runtime_power_helper_paths(coordinator_factory, monkeypatch) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    fixed_now = datetime(2026, 2, 27, 0, 7, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "custom_components.enphase_ev.heatpump_runtime.dt_util.utcnow",
        lambda: fixed_now,
    )

    assert runtime._heatpump_primary_member() is None  # noqa: SLF001
    assert runtime._heatpump_primary_device_uid() is None  # noqa: SLF001

    coord.inventory_runtime._set_type_device_buckets(
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "statusText": "Recommended",
                        "parent_uid": "HP-CTRL",
                    },
                ],
            }
        },
        ["heatpump"],
    )

    assert runtime._heatpump_primary_device_uid() == "HP-CTRL"  # noqa: SLF001
    assert runtime._heatpump_power_candidate_device_uids() == [  # noqa: SLF001
        "HP-CTRL",
        "HP-METER",
        None,
    ]
    assert (
        runtime._heatpump_power_candidate_is_recommended("HP-METER") is True
    )  # noqa: SLF001
    assert runtime._heatpump_power_fetch_plan()[0] == [  # noqa: SLF001
        "HP-CTRL",
        "HP-METER",
        None,
    ]
    assert runtime._heatpump_latest_power_sample(  # noqa: SLF001
        {
            "heat_pump_consumption": [560.0, 0.0, 0.0],
            "start_date": "2026-02-27T00:00:00Z",
            "interval_minutes": 5,
        }
    ) == (0, 560.0)
    assert (
        runtime._infer_heatpump_interval_minutes(  # noqa: SLF001
            datetime(2026, 2, 27, 0, 0, tzinfo=timezone.utc),
            2,
            datetime(2026, 2, 27, 0, 10, tzinfo=timezone.utc),
        )
        == 5
    )


@pytest.mark.asyncio
async def test_heatpump_runtime_power_and_diagnostics_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 3,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG",
                        "statusText": "Recommended",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-03-13T00:05:00Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-CTRL",
                        "device_name": "Primary",
                        "consumption": [{"details": [610.0]}],
                    }
                ]
            },
        }
    )
    coord.client.hems_heatpump_state = AsyncMock(
        return_value={"device_uid": "HP-CTRL", "heatpump_status": "RUNNING"}
    )
    _seed_previous_heatpump_daily_snapshot(
        coord,
        device_uid="HP-CTRL",
        energy_wh=559.1666666667,
        timestamp="2026-03-13T00:00:00Z",
        day_key="2026-03-13",
        timezone_name="UTC",
        device_name="Primary",
    )
    await runtime._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    assert coord.heatpump_power_w == pytest.approx(610.0)
    assert coord.heatpump_power_device_uid == "HP-CTRL"

    runtime._heatpump_power_cache_until = None  # noqa: SLF001
    runtime._heatpump_power_selection_marker = (
        runtime._heatpump_power_inventory_marker()
    )  # noqa: SLF001
    runtime._heatpump_power_device_uid = "HP-CTRL"  # noqa: SLF001
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-03-13T00:10:00Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-CTRL",
                        "device_name": "Primary",
                        "consumption": [{"details": [0.0]}],
                    }
                ]
            },
        }
    )
    await runtime._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    assert coord.heatpump_power_w == pytest.approx(610.0)
    assert coord.heatpump_power_device_uid == "HP-CTRL"

    runtime._heatpump_runtime_diagnostics_cache_until = (
        time.monotonic() + 60
    )  # noqa: SLF001
    coord.client.show_livestream = AsyncMock(side_effect=AssertionError("no fetch"))
    await runtime.async_ensure_heatpump_runtime_diagnostics()
    coord.client.show_livestream.assert_not_awaited()

    runtime._heatpump_runtime_diagnostics_cache_until = None  # noqa: SLF001

    def _redact(payload):
        if payload == "SHOW_SCALAR":
            return "live-redacted"
        if payload == "EVENT_SCALAR":
            return "event-redacted"
        return None if payload == "EVENT_NONE" else payload

    monkeypatch.setattr(heatpump_runtime_mod, "redact_battery_payload", _redact)
    coord.client.show_livestream = AsyncMock(return_value="SHOW_SCALAR")
    coord.client.heat_pump_events_json = AsyncMock(
        side_effect=lambda uid: "EVENT_NONE" if uid == "HP-CTRL" else "EVENT_SCALAR"
    )
    coord.client.iq_er_events_json = AsyncMock(
        side_effect=api.OptionalEndpointUnavailable("optional boom")
    )
    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)

    diagnostics = coord.heatpump_runtime_diagnostics()
    assert diagnostics["show_livestream_payload"] == {"value": "live-redacted"}
    assert diagnostics["events_payloads"][0]["payload"] is None
    assert [entry.get("payload") for entry in diagnostics["events_payloads"]] == [
        None,
        None,
        None,
    ]
    assert [entry.get("error") for entry in diagnostics["events_payloads"]] == [
        None,
        "optional boom",
        "optional boom",
    ]
    assert diagnostics["event_summary"] == {
        "known_event_counts": {},
        "unknown_event_keys": [],
    }

    runtime._type_device_buckets = {}  # noqa: SLF001
    runtime._type_device_order = []  # noqa: SLF001
    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)
    assert coord.heatpump_runtime_diagnostics()["events_payloads"] == []
    assert coord.heatpump_runtime_diagnostics()["event_summary"] == {
        "known_event_counts": {},
        "unknown_event_keys": [],
    }
    assert coord.heatpump_daily_consumption_last_error is None

    coord.inventory_runtime._set_type_device_buckets(
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "statusText": "Normal",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord.client._hems_site_supported = None  # noqa: SLF001
    coord.client.pv_system_today = AsyncMock(return_value=_site_today_payload(88.0))
    coord.client.hems_energy_consumption = None
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_daily_consumption["daily_energy_wh"] == pytest.approx(88.0)
    assert coord.heatpump_daily_consumption["split_source"] is None
    assert coord.heatpump_daily_consumption_last_error is None
    assert coord.heatpump_runtime.heatpump_daily_split_last_error == (
        "HEMS daily split endpoint unavailable"
    )

    coord.client.hems_energy_consumption = AsyncMock(side_effect=RuntimeError("boom"))
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_daily_consumption["daily_energy_wh"] == pytest.approx(88.0)
    assert coord.heatpump_daily_consumption_last_error is None
    assert coord.heatpump_runtime.heatpump_daily_split_last_error == "boom"
    assert coord._heatpump_daily_consumption_backoff_until is None  # noqa: SLF001

    coord._heatpump_daily_consumption_backoff_until = None  # noqa: SLF001
    coord.client.hems_energy_consumption = AsyncMock(return_value=None)
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_daily_consumption["daily_energy_wh"] == pytest.approx(88.0)
    assert coord.heatpump_daily_consumption["split_source"] is None


@pytest.mark.asyncio
async def test_heatpump_runtime_helper_branches_and_resets(coordinator_factory) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime

    assert (
        runtime._heatpump_snapshot_is_fresh("bad", 10.0, time.monotonic()) is False
    )  # noqa: SLF001

    calls: list[dict[str, object]] = []

    async def _plain_fetcher():
        calls.append({})
        return "ok"

    async def _kwargs_fetcher(**kwargs):
        calls.append(dict(kwargs))
        return "ok"

    async def _refresh_fetcher(*, refresh_data=False):
        calls.append({"refresh_data": refresh_data})
        return "refresh"

    assert (
        await runtime._async_call_refreshable_fetcher(
            _plain_fetcher, force=False
        )  # noqa: SLF001
        == "ok"
    )
    assert calls[-1] == {}
    assert (
        await runtime._async_call_refreshable_fetcher(
            _refresh_fetcher, force=True
        )  # noqa: SLF001
        == "refresh"
    )
    assert calls[-1] == {"refresh_data": True}
    assert (
        await runtime._async_call_refreshable_fetcher(
            _plain_fetcher, force=True
        )  # noqa: SLF001
        == "ok"
    )
    assert calls[-1] == {}
    assert (
        await runtime._async_call_refreshable_fetcher(
            _kwargs_fetcher, force=True
        )  # noqa: SLF001
        == "ok"
    )
    assert calls[-1] == {"refresh_data": True}

    runtime._hems_support_preflight_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(side_effect=AssertionError("cached"))  # type: ignore[assignment]
    await runtime._async_refresh_hems_support_preflight(force=False)  # noqa: SLF001
    coord.client.system_dashboard_summary.assert_not_awaited()

    runtime._hems_support_preflight_cache_until = None  # noqa: SLF001
    coord.client._hems_site_supported = None  # noqa: SLF001
    coord.client.system_dashboard_summary = None  # type: ignore[assignment]
    await runtime._async_refresh_hems_support_preflight(force=True)  # noqa: SLF001
    assert coord._hems_support_preflight_cache_until is not None  # noqa: SLF001

    runtime._type_device_buckets = {}  # noqa: SLF001
    runtime._type_device_order = []  # noqa: SLF001
    now = time.monotonic()
    coord._heatpump_runtime_state = {"heatpump_status": "RUNNING"}  # noqa: SLF001
    coord._heatpump_runtime_state_last_success_mono = (  # noqa: SLF001
        now - heatpump_runtime_mod.HEATPUMP_RUNTIME_STATE_STALE_AFTER_S - 1.0
    )
    coord._heatpump_runtime_state_last_success_utc = datetime.now(
        timezone.utc
    )  # noqa: SLF001
    await runtime._async_refresh_heatpump_runtime_state(force=True)  # noqa: SLF001
    assert coord.heatpump_runtime_state == {}
    assert coord.heatpump_runtime_state_using_stale is False
    assert coord.heatpump_runtime_state_last_success_utc is not None

    coord._heatpump_daily_consumption = {"daily_energy_wh": 12.0}  # noqa: SLF001
    coord._heatpump_daily_consumption_last_success_mono = (  # noqa: SLF001
        now - heatpump_runtime_mod.HEATPUMP_DAILY_CONSUMPTION_STALE_AFTER_S - 1.0
    )
    coord._heatpump_daily_consumption_last_success_utc = datetime.now(
        timezone.utc
    )  # noqa: SLF001
    await runtime._async_refresh_heatpump_daily_consumption(force=True)  # noqa: SLF001
    assert coord.heatpump_daily_consumption == {}
    assert coord.heatpump_daily_consumption_using_stale is False
    assert coord.heatpump_daily_consumption_last_success_utc is not None


def test_heatpump_runtime_misc_helper_guards_and_properties(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime

    assert (
        runtime._heatpump_runtime_mode({"heatpump_status": ""}) is None
    )  # noqa: SLF001

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "devices": [
                {"device_type": "HEAT_PUMP", "device_uid": "HP-1"},
                {"device_type": "HEAT_PUMP", "device_uid": "HP-2"},
                {"device_type": "HEAT_PUMP"},
            ],
            "count": 3,
        }
    }
    runtime._heatpump_power_device_uid = "HP-1"  # noqa: SLF001
    runtime._heatpump_power_selection_marker = (
        runtime._heatpump_power_inventory_marker()
    )  # noqa: SLF001
    ordered, compare_all, marker = runtime._heatpump_power_fetch_plan()  # noqa: SLF001
    assert compare_all is False
    assert ordered[0] == "HP-1"
    assert ordered.count("HP-1") == 1
    assert ordered[-1] is None
    assert marker == runtime._heatpump_power_inventory_marker()  # noqa: SLF001

    assert runtime._heatpump_power_candidate_device_uids() == [
        "HP-1",
        "HP-2",
        None,
    ]  # noqa: SLF001

    assert runtime._heatpump_sample_utc_for_index({}, -1) is None  # noqa: SLF001
    assert runtime._heatpump_sample_utc_for_index("bad", 0) is None  # noqa: SLF001
    assert (
        runtime._heatpump_sample_utc_for_index({"start_date": "bad"}, 0) is None
    )  # noqa: SLF001
    assert (  # noqa: SLF001
        runtime._heatpump_sample_utc_for_index(
            {
                "start_date": "2026-01-01T00:00:00Z",
                "heat_pump_consumption": [1],
                "interval_minutes": 0,
            },
            0,
        )
        is None
    )

    class _BadTimedelta:
        def __rmul__(self, other):
            raise TypeError("bad minutes")

    original_timedelta = heatpump_runtime_mod.timedelta
    heatpump_runtime_mod.timedelta = _BadTimedelta()  # type: ignore[assignment]
    try:
        assert (  # noqa: SLF001
            runtime._heatpump_sample_utc_for_index(
                {
                    "start_date": "2026-01-01T00:00:00Z",
                    "heat_pump_consumption": [1],
                    "interval_minutes": 5,
                },
                1,
            )
            is None
        )
    finally:
        heatpump_runtime_mod.timedelta = original_timedelta  # type: ignore[assignment]

    class _BadFloat:
        def __float__(self):
            raise TypeError("bad float")

    class _BadStr:
        def __str__(self):
            raise TypeError("bad str")

    coord._heatpump_power_w = _BadFloat()  # noqa: SLF001
    coord._heatpump_power_device_uid = _BadStr()  # noqa: SLF001
    coord._heatpump_power_source = _BadStr()  # noqa: SLF001
    coord._heatpump_power_last_error = _BadStr()  # noqa: SLF001
    coord._heatpump_power_last_success_utc = "bad"  # noqa: SLF001
    coord._heatpump_daily_consumption_last_success_utc = "bad"  # noqa: SLF001
    coord._heatpump_runtime_state_last_success_utc = "bad"  # noqa: SLF001
    coord._heatpump_power_start_utc = "bad"  # noqa: SLF001
    assert coord.heatpump_power_w is None
    assert coord.heatpump_power_device_uid is None
    assert coord.heatpump_power_source is None
    assert coord.heatpump_power_last_error is None
    assert coord.heatpump_power_last_success_utc is None
    assert coord.heatpump_daily_consumption_last_success_utc is None
    assert coord.heatpump_runtime_state_last_success_utc is None
    assert coord.heatpump_power_start_utc is None


@pytest.mark.asyncio
async def test_heatpump_runtime_remaining_coverage_branches(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord._devices_inventory_payload = {"curr_date_site": "2026-04-05"}  # noqa: SLF001
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-04-05T00:00:00Z",
            "data": {"heat-pump": [{"device_uid": "HP-1", "consumption": []}]},
        }
    )
    coord.client.pv_system_today = AsyncMock(return_value=_site_today_payload(91.0))
    await runtime._async_refresh_heatpump_daily_consumption(force=True)  # noqa: SLF001
    assert coord.heatpump_daily_consumption["daily_energy_wh"] == pytest.approx(91.0)
    assert coord.heatpump_daily_consumption["split_source"] is None
    assert coord.heatpump_daily_consumption_last_error is None
    assert (
        coord.heatpump_runtime.heatpump_daily_split_last_error
        == "No usable HEMS daily split payload"
    )

    monkeypatch.setattr(
        heatpump_runtime_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 1, 1, 0, 10, 0),
    )
    sample_utc = runtime._heatpump_sample_utc_for_index(  # noqa: SLF001
        {"start_date": "2026-01-01T00:00:00Z", "heat_pump_consumption": [1, 2]},
        1,
    )
    assert sample_utc == datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)

    coord._heatpump_power_w = float("nan")  # noqa: SLF001
    coord._heatpump_power_start_utc = "bad"  # noqa: SLF001
    assert coord.heatpump_power_w is None
    assert coord.heatpump_power_start_utc is None
    coord._heatpump_power_start_utc = datetime(
        2026, 1, 1, tzinfo=timezone.utc
    )  # noqa: SLF001
    assert coord.heatpump_power_start_utc == datetime(2026, 1, 1, tzinfo=timezone.utc)
