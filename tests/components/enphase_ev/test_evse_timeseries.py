from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
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

    client.evse_timeseries_daily_energy.assert_awaited_once_with(start_date=day_local)

    payloads = {"EV-1": {"sn": "EV-1"}}
    manager.merge_charger_payloads(payloads, day_local=day_local)

    assert payloads["EV-1"]["evse_daily_energy_kwh"] == pytest.approx(2.5)
    assert payloads["EV-1"]["evse_lifetime_energy_kwh"] == pytest.approx(12.75)
    assert payloads["EV-1"]["evse_timeseries_interval_minutes"] == pytest.approx(1440.0)
    assert (
        payloads["EV-1"]["evse_timeseries_last_reported_at"]
        == "2026-03-11T11:00:00+00:00"
    )
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
    manager = EVSETimeseriesManager(
        hass,
        lambda: client,
        site_id_getter=lambda: "3381244",
    )
    day_local = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)

    await manager.async_refresh(day_local=day_local)

    client.evse_timeseries_daily_energy = AsyncMock(
        side_effect=EVSETimeseriesUnavailable(
            "GET /service/timeseries/evse/timeseries/daily_energy?site_id=3381244&source=evse"
        )
    )
    client.evse_timeseries_lifetime_energy = AsyncMock(
        side_effect=EVSETimeseriesUnavailable(
            "GET /service/timeseries/evse/timeseries/lifetime_energy?site_id=3381244&source=evse"
        )
    )

    await manager.async_refresh(day_local=day_local, force=True)

    payloads = {"EV-1": {"sn": "EV-1"}}
    manager.merge_charger_payloads(payloads, day_local=day_local)

    assert payloads["EV-1"]["evse_daily_energy_kwh"] == pytest.approx(1.5)
    assert payloads["EV-1"]["evse_lifetime_energy_kwh"] == pytest.approx(10.0)
    diagnostics = manager.diagnostics()
    assert diagnostics["available"] is False
    assert diagnostics["failures"] == 2
    assert "3381244" not in str(diagnostics["last_error"])
    assert "3381244" not in str(diagnostics["daily"]["last_error"])
    assert "3381244" not in str(diagnostics["lifetime"]["last_error"])
    assert "site_id=[site]&source=evse" in str(diagnostics["daily"]["last_error"])
    assert "site_id=[site]&source=evse" in str(diagnostics["lifetime"]["last_error"])


def test_evse_timeseries_redaction_ignores_site_getter_errors(hass) -> None:
    def bad_site_id():
        raise RuntimeError("bad site")

    manager = EVSETimeseriesManager(hass, lambda: None, site_id_getter=bad_site_id)

    assert (
        manager._redact_error(
            "GET /service/timeseries/evse/timeseries/daily_energy"
        )  # noqa: SLF001
        == "GET /service/timeseries/evse/timeseries/daily_energy"
    )


@pytest.mark.asyncio
async def test_evse_timeseries_manager_helper_branches(hass, monkeypatch) -> None:
    client = SimpleNamespace(
        evse_timeseries_daily_energy=AsyncMock(
            return_value={"EV-1": {"energy_kwh": 1.0}}
        ),
        evse_timeseries_lifetime_energy=AsyncMock(
            return_value={"EV-1": {"energy_kwh": 2.0}}
        ),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)

    manager._daily_cache["2026-03-11"] = ("bad", {})  # type: ignore[assignment]
    manager._lifetime_cache = ("bad", {})  # type: ignore[assignment]
    assert manager.daily_cache_age == {}
    assert manager.lifetime_cache_age is None
    assert manager._lifetime_cache_fresh() is False  # noqa: SLF001
    assert manager._daily_cache_fresh("2026-03-11") is False  # noqa: SLF001

    utcnow_calls = iter(
        [datetime(2026, 3, 11, tzinfo=timezone.utc), RuntimeError("boom")]
    )

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
    manager._endpoint_state["lifetime"][
        "backoff_until"
    ] = 999999999999.0  # noqa: SLF001
    await manager.async_refresh(day_local=day_local)
    client.evse_timeseries_daily_energy.assert_not_awaited()
    client.evse_timeseries_lifetime_energy.assert_not_awaited()

    manager._endpoint_state["daily"]["backoff_until"] = None  # noqa: SLF001
    manager._endpoint_state["lifetime"]["backoff_until"] = None  # noqa: SLF001
    manager._daily_cache = {"2026-03-11": (0.0, {"EV-1": {"energy_kwh": 1.0}})}
    manager._lifetime_cache = (
        0.0,
        {"EV-1": {"energy_kwh": 2.0, "interval_minutes": 60}},
    )
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


def test_evse_timeseries_refresh_due_defaults_day_and_skips_when_caches_are_fresh(
    hass, monkeypatch
) -> None:
    client = SimpleNamespace(
        evse_timeseries_daily_energy=AsyncMock(),
        evse_timeseries_lifetime_energy=AsyncMock(),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    day_local = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)
    day_key = manager._day_key(day_local)  # noqa: SLF001
    manager._daily_cache[day_key] = (
        95.0,
        {"EV-1": {"energy_kwh": 1.0}},
    )  # noqa: SLF001
    manager._lifetime_cache = (95.0, {"EV-1": {"energy_kwh": 2.0}})  # noqa: SLF001
    monkeypatch.setattr(ts_mod.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(ts_mod.dt_util, "now", lambda: day_local)
    monkeypatch.setattr(ts_mod.dt_util, "as_local", lambda value: value)

    assert manager.refresh_due(day_local=None) is False


def _request_info() -> aiohttp.RequestInfo:
    return aiohttp.RequestInfo(
        url=aiohttp.client.URL("https://example.com/evse"),
        method="GET",
        headers={},
        real_url=aiohttp.client.URL("https://example.com/evse"),
    )


@pytest.mark.asyncio
async def test_evse_timeseries_client_response_error_triggers_backoff(hass) -> None:
    response_error = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=429,
        message="rate limited",
        headers={"Retry-After": "60"},
    )
    client = SimpleNamespace(
        evse_timeseries_daily_energy=AsyncMock(side_effect=response_error),
        evse_timeseries_lifetime_energy=AsyncMock(
            return_value={"EV-1": {"energy_kwh": 4.0}}
        ),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    day_local = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)

    await manager.async_refresh(day_local=day_local, force=True)

    diagnostics = manager.diagnostics()
    assert manager.daily_available is False
    assert manager.daily_backoff_active is True
    assert diagnostics["daily"]["using_stale"] is False
    assert "429" in diagnostics["daily"]["last_error"]
    assert "rate limited" in diagnostics["daily"]["last_error"]


@pytest.mark.asyncio
async def test_evse_timeseries_lifetime_client_response_error_triggers_backoff(
    hass,
) -> None:
    response_error = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=503,
        message="unavailable",
    )
    client = SimpleNamespace(
        evse_timeseries_daily_energy=AsyncMock(
            return_value={"EV-1": {"energy_kwh": 2.0}}
        ),
        evse_timeseries_lifetime_energy=AsyncMock(side_effect=response_error),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    day_local = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)

    await manager.async_refresh(day_local=day_local, force=True)

    diagnostics = manager.diagnostics()
    assert manager.lifetime_available is False
    assert manager.lifetime_backoff_active is True
    assert diagnostics["lifetime"]["using_stale"] is False
    assert "503" in diagnostics["lifetime"]["last_error"]
