"""Site-level lifetime energy sensors and parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.coordinator import SiteEnergyFlow
from custom_components.enphase_ev.sensor import EnphaseSiteEnergySensor


def test_site_energy_aggregation_with_fallbacks(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "production": [1000, None, 2000, -5],
        "import": [],
        "grid_home": [200, 100],
        "grid_battery": [50],
        "export": [],
        "solar_grid": [600],
        "battery_grid": [100],
        "charge": None,
        "solar_battery": [100],
        "discharge": [],
        "battery_home": [150],
        "start_date": "2023-08-10",
        "last_report_date": 1_700_000_001,
        "update_pending": False,
    }
    flows, meta = coord._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert set(flows) == {
        "solar_production",
        "grid_import",
        "grid_export",
        "battery_charge",
        "battery_discharge",
    }
    assert flows["solar_production"].value_kwh == pytest.approx(3.0)
    assert flows["grid_import"].fields_used == ["grid_home", "grid_battery"]
    assert flows["grid_export"].fields_used == ["solar_grid", "battery_grid"]
    assert flows["battery_charge"].bucket_count == 1
    assert flows["battery_charge"].value_kwh == pytest.approx(0.15)
    assert flows["battery_discharge"].value_kwh == pytest.approx(0.25)
    assert meta["start_date"] == "2023-08-10"
    assert isinstance(meta["last_report_date"], datetime)
    assert meta["update_pending"] is False


def test_site_energy_import_diff_fallback(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "consumption": [500, 500],
        "solar_home": [100, None],
        "start_date": "2024-02-01",
    }
    flows, _meta = coord._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["grid_import"].fields_used == ["consumption", "solar_home"]
    assert flows["grid_import"].value_kwh == pytest.approx(0.9)
    assert flows["grid_import"].bucket_count == 1


def test_site_energy_guard_confirms_reset(coordinator_factory) -> None:
    coord = coordinator_factory()
    base_payload = {
        "production": [1000],
        "start_date": "2024-01-01",
    }
    flows, _ = coord._aggregate_site_energy(base_payload)  # noqa: SLF001
    coord.site_energy = flows

    drop_payload = {
        "production": [100],
        "start_date": "2024-01-01",
    }
    flows_drop, _ = coord._aggregate_site_energy(drop_payload)  # noqa: SLF001
    coord.site_energy = flows_drop
    assert flows_drop["solar_production"].value_kwh == pytest.approx(1.0)
    assert coord._site_energy_force_refresh is True  # noqa: SLF001

    flows_reset, _ = coord._aggregate_site_energy(drop_payload)  # noqa: SLF001
    assert flows_reset["solar_production"].value_kwh == pytest.approx(0.1)
    assert flows_reset["solar_production"].last_reset_at is not None


@pytest.mark.asyncio
async def test_site_energy_cache_respects_ttl(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    monotonic_val = 1000.0

    async def _payload():
        return {"production": [500]}

    coord.client.lifetime_energy = AsyncMock(side_effect=_payload)
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.time.monotonic",
        lambda: monotonic_val,
    )

    await coord._async_refresh_site_energy()  # noqa: SLF001
    assert coord.client.lifetime_energy.call_count == 1

    monotonic_val += 100
    await coord._async_refresh_site_energy()  # noqa: SLF001
    assert coord.client.lifetime_energy.call_count == 1

    coord._site_energy_force_refresh = True  # noqa: SLF001
    await coord._async_refresh_site_energy()  # noqa: SLF001
    assert coord.client.lifetime_energy.call_count == 2

    monotonic_val += coord._site_energy_cache_ttl + 1  # noqa: SLF001
    await coord._async_refresh_site_energy()  # noqa: SLF001
    assert coord.client.lifetime_energy.call_count == 3


@pytest.mark.asyncio
async def test_site_energy_sensor_attributes(hass, coordinator_factory):
    coord = coordinator_factory()
    coord.site_energy = {
        "solar_production": SiteEnergyFlow(
            value_kwh=1.234,
            bucket_count=3,
            fields_used=["production"],
            start_date="2024-01-01",
            last_report_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
            update_pending=False,
            source_unit="Wh",
            last_reset_at="2024-01-03T00:00:00+00:00",
        )
    }
    sensor = EnphaseSiteEnergySensor(
        coord,
        "solar_production",
        "site_solar_production",
        "Solar Production",
    )
    sensor.hass = hass
    await sensor.async_added_to_hass()
    assert sensor.available is True
    assert sensor.native_value == pytest.approx(1.234)
    attrs = sensor.extra_state_attributes
    assert attrs["bucket_count"] == 3
    assert attrs["source_fields"] == ["production"]
    assert attrs["start_date"] == "2024-01-01"
    assert attrs["last_report_date"].startswith("2024-01-02")
    assert attrs["last_reset_at"] == "2024-01-03T00:00:00+00:00"
    assert sensor.entity_registry_enabled_default is False
