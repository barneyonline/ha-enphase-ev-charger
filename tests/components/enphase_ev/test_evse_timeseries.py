from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev import evse_timeseries as ts_mod
from custom_components.enphase_ev.api import EVSETimeseriesUnavailable
from custom_components.enphase_ev.evse_timeseries import EVSETimeseriesManager


@pytest.mark.asyncio
async def test_evse_timeseries_manager_refresh_and_merge(hass) -> None:
    client = SimpleNamespace(
        evse_timeseries_daily_energy=AsyncMock(
            return_value={
                "EV-1": {
                    "energy_kwh": 2.5,
                    "interval_minutes": 1440.0,
                    "last_report_date": "2026-03-11T10:00:00+00:00",
                }
            }
        ),
        evse_timeseries_lifetime_energy=AsyncMock(
            return_value={
                "EV-1": {
                    "energy_kwh": 12.75,
                    "last_report_date": "2026-03-11T11:00:00+00:00",
                }
            }
        ),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    day_local = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)

    await manager.async_refresh(day_local=day_local)

    payloads = {"EV-1": {"sn": "EV-1"}}
    manager.merge_charger_payloads(payloads, day_local=day_local)

    assert payloads["EV-1"]["evse_daily_energy_kwh"] == pytest.approx(2.5)
    assert payloads["EV-1"]["evse_lifetime_energy_kwh"] == pytest.approx(12.75)
    assert payloads["EV-1"]["evse_timeseries_interval_minutes"] == pytest.approx(1440.0)
    assert payloads["EV-1"]["evse_timeseries_last_reported_at"] == "2026-03-11T11:00:00+00:00"
    assert payloads["EV-1"]["evse_timeseries_source"] == "evse_timeseries"
    diagnostics = manager.diagnostics()
    assert diagnostics["available"] is True
    assert diagnostics["daily_cache_days"] == ["2026-03-11"]
    assert diagnostics["lifetime_serial_count"] == 1


@pytest.mark.asyncio
async def test_evse_timeseries_manager_retains_cache_on_unavailable(hass) -> None:
    client = SimpleNamespace(
        evse_timeseries_daily_energy=AsyncMock(
            return_value={"EV-1": {"energy_kwh": 1.5}}
        ),
        evse_timeseries_lifetime_energy=AsyncMock(
            return_value={"EV-1": {"energy_kwh": 10.0}}
        ),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    day_local = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)

    await manager.async_refresh(day_local=day_local)

    client.evse_timeseries_daily_energy = AsyncMock(
        side_effect=EVSETimeseriesUnavailable("daily down")
    )
    client.evse_timeseries_lifetime_energy = AsyncMock(
        side_effect=EVSETimeseriesUnavailable("lifetime down")
    )

    await manager.async_refresh(day_local=day_local, force=True)

    payloads = {"EV-1": {"sn": "EV-1"}}
    manager.merge_charger_payloads(payloads, day_local=day_local)

    assert payloads["EV-1"]["evse_daily_energy_kwh"] == pytest.approx(1.5)
    assert payloads["EV-1"]["evse_lifetime_energy_kwh"] == pytest.approx(10.0)
    diagnostics = manager.diagnostics()
    assert diagnostics["available"] is False
    assert diagnostics["failures"] == 2
    assert diagnostics["last_error"] == "daily down"
    assert diagnostics["daily"]["last_error"] == "daily down"
    assert diagnostics["lifetime"]["last_error"] == "lifetime down"


@pytest.mark.asyncio
async def test_evse_timeseries_manager_helper_branches(hass, monkeypatch) -> None:
    client = SimpleNamespace(
        evse_timeseries_daily_energy=AsyncMock(return_value={"EV-1": {"energy_kwh": 1.0}}),
        evse_timeseries_lifetime_energy=AsyncMock(return_value={"EV-1": {"energy_kwh": 2.0}}),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)

    manager._daily_cache["2026-03-11"] = ("bad", {})  # type: ignore[assignment]
    manager._lifetime_cache = ("bad", {})  # type: ignore[assignment]
    assert manager.daily_cache_age == {}
    assert manager.lifetime_cache_age is None
    assert manager._lifetime_cache_fresh() is False  # noqa: SLF001
    assert manager._daily_cache_fresh("2026-03-11") is False  # noqa: SLF001

    utcnow_calls = iter([datetime(2026, 3, 11, tzinfo=timezone.utc), RuntimeError("boom")])

    def _fake_utcnow():
        value = next(utcnow_calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(ts_mod.dt_util, "utcnow", _fake_utcnow)
    manager._note_service_unavailable("daily", "down")  # noqa: SLF001
    assert manager.service_backoff_ends_utc is None

    day_local = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)
    manager._endpoint_state["daily"]["backoff_until"] = 999999999999.0  # noqa: SLF001
    manager._endpoint_state["lifetime"]["backoff_until"] = 999999999999.0  # noqa: SLF001
    await manager.async_refresh(day_local=day_local)
    client.evse_timeseries_daily_energy.assert_not_awaited()
    client.evse_timeseries_lifetime_energy.assert_not_awaited()

    manager._endpoint_state["daily"]["backoff_until"] = None  # noqa: SLF001
    manager._endpoint_state["lifetime"]["backoff_until"] = None  # noqa: SLF001
    manager._daily_cache = {"2026-03-11": (0.0, {"EV-1": {"energy_kwh": 1.0}})}
    manager._lifetime_cache = (0.0, {"EV-1": {"energy_kwh": 2.0, "interval_minutes": 60}})
    monkeypatch.setattr(ts_mod.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(ts_mod.dt_util, "now", lambda: day_local)
    monkeypatch.setattr(ts_mod.dt_util, "as_local", lambda value: value)
    await manager.async_refresh(day_local=None)
    await manager.async_refresh(day_local=day_local)
    assert client.evse_timeseries_daily_energy.await_count == 0
    assert client.evse_timeseries_lifetime_energy.await_count == 0

    payloads = {"EV-1": {"sn": "EV-1"}}
    manager.merge_charger_payloads(payloads, day_local=day_local)
    assert payloads["EV-1"]["evse_timeseries_interval_minutes"] == 60


@pytest.mark.asyncio
async def test_evse_timeseries_endpoint_backoff_is_independent(hass) -> None:
    client = SimpleNamespace(
        evse_timeseries_daily_energy=AsyncMock(
            side_effect=EVSETimeseriesUnavailable("daily down")
        ),
        evse_timeseries_lifetime_energy=AsyncMock(
            return_value={"EV-1": {"energy_kwh": 4.0}}
        ),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    day_local = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)

    await manager.async_refresh(day_local=day_local, force=True)

    assert manager.daily_available is False
    assert manager.lifetime_available is True
    assert manager.service_available is True
