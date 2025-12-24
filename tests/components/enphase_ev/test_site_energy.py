"""Site-level lifetime energy sensors and parsing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.coordinator import SiteEnergyFlow
from custom_components.enphase_ev.sensor import EnphaseSiteEnergySensor


def test_site_energy_aggregation_with_fallbacks(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "production": [1000, None, 2000, -5],
        "consumption": [1500, 500],
        "solar_home": [400, 100],
        "solar_grid": [600, None],
        "grid_battery": [50],
        "battery_grid": [100],
        "charge": None,
        "solar_battery": [100],
        "discharge": [],
        "battery_home": [150],
        "start_date": "2023-08-10",
        "last_report_date": 1_700_000_001,
        "update_pending": False,
        "interval_minutes": 60,
    }
    flows, meta = coord._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert set(flows) == {
        "solar_production",
        "consumption",
        "grid_import",
        "grid_export",
        "battery_charge",
        "battery_discharge",
    }
    assert flows["solar_production"].value_kwh == pytest.approx(3.0)
    assert flows["consumption"].value_kwh == pytest.approx(2.0)
    assert flows["grid_import"].fields_used == ["consumption", "solar_home"]
    assert flows["grid_import"].value_kwh == pytest.approx(1.5)
    assert flows["grid_export"].fields_used == ["solar_grid"]
    assert flows["battery_charge"].bucket_count == 1
    assert flows["battery_charge"].value_kwh == pytest.approx(0.15)
    assert flows["battery_discharge"].value_kwh == pytest.approx(0.25)
    assert meta["start_date"] == "2023-08-10"
    assert isinstance(meta["last_report_date"], datetime)
    assert meta["update_pending"] is False
    assert meta["interval_minutes"] == pytest.approx(60.0)


def test_site_energy_cache_age_and_invalidate(coordinator_factory, monkeypatch) -> None:
    coord = coordinator_factory()
    coord._site_energy_cache_ts = "bad"  # type: ignore[assignment]
    monkeypatch.setattr("custom_components.enphase_ev.coordinator.time.monotonic", lambda: 100.0)
    assert coord._site_energy_cache_age() is None  # noqa: SLF001
    coord._invalidate_site_energy_cache()  # noqa: SLF001
    assert coord._site_energy_cache_ts is None


def test_parse_site_energy_timestamp_variants(coordinator_factory, monkeypatch) -> None:
    coord = coordinator_factory()
    # Milliseconds epoch
    ts = coord._parse_site_energy_timestamp(1_700_000_000_000)  # noqa: SLF001
    assert isinstance(ts, datetime)
    # ISO date fallback
    parsed_date = coord._parse_site_energy_timestamp("2024-05-01")  # noqa: SLF001
    assert isinstance(parsed_date, datetime)
    # Digit string recursion
    ts_digit = coord._parse_site_energy_timestamp("1700000000000")  # noqa: SLF001
    assert isinstance(ts_digit, datetime)
    # ISO datetime parsing
    parsed_dt = coord._parse_site_energy_timestamp("2024-01-01T00:00:00")  # noqa: SLF001
    assert parsed_dt.tzinfo is not None
    # Invalid string
    assert coord._parse_site_energy_timestamp("not-a-date") is None  # noqa: SLF001


def test_coerce_energy_value_exceptions(coordinator_factory) -> None:
    coord = coordinator_factory()

    class Boom:
        def __float__(self):
            raise ValueError("fail")

    assert coord._coerce_energy_value(Boom()) is None  # noqa: SLF001
    assert coord._coerce_energy_value("bad-number") is None  # noqa: SLF001
    assert coord._aggregate_site_energy(None) is None  # noqa: SLF001

    class BoomFloat(float):
        def __float__(self):
            raise ValueError("boom")

    assert coord._coerce_energy_value(BoomFloat(1.0)) is None  # noqa: SLF001
    assert coord._coerce_energy_value("   ") is None  # noqa: SLF001
    assert coord._parse_site_energy_timestamp(["bad"]) is None  # noqa: SLF001


def test_parse_site_energy_timestamp_error_branches(monkeypatch, coordinator_factory) -> None:
    coord = coordinator_factory()

    class BadInt(int):
        def __new__(cls, value=1):
            return super().__new__(cls, value)

        def __int__(self):
            raise ValueError("no-int")

    assert coord._parse_site_energy_timestamp(BadInt()) is None  # noqa: SLF001
    assert coord._parse_site_energy_timestamp("   ") is None  # noqa: SLF001

    with monkeypatch.context() as m:
        m.setattr(
            "custom_components.enphase_ev.coordinator.dt_util.parse_datetime",
            lambda _v: (_ for _ in ()).throw(ValueError("dt boom")),
        )
        m.setattr(
            "custom_components.enphase_ev.coordinator.dt_util.parse_date",
            lambda _v: (_ for _ in ()).throw(ValueError("date boom")),
        )
        assert coord._parse_site_energy_timestamp("2024/01/01") is None  # noqa: SLF001

    with monkeypatch.context() as m:
        m.setattr(
            "custom_components.enphase_ev.coordinator.dt_util.parse_datetime",
            lambda _v: None,
        )
        from datetime import date

        m.setattr(
            "custom_components.enphase_ev.coordinator.dt_util.parse_date",
            lambda _v: date(2024, 1, 2),
        )
        parsed = coord._parse_site_energy_timestamp("2024/01/02")  # noqa: SLF001
        assert parsed.date().isoformat() == "2024-01-02"


def test_diff_energy_fields_when_neg_exceeds(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {"consumption": [100], "solar_home": [200]}
    total, count, fields = coord._diff_energy_fields(
        payload, "consumption", "solar_home", None
    )  # noqa: SLF001
    assert total == 0.0 and count == 0 and fields == []


def test_diff_energy_fields_allows_zero_subtrahend(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {"consumption": [100], "solar_home": [0]}
    total, count, fields = coord._diff_energy_fields(
        payload, "consumption", "solar_home", None
    )  # noqa: SLF001
    assert total == pytest.approx(100.0)
    assert count == 1
    assert fields == ["consumption", "solar_home"]


def test_site_energy_guard_drop_without_reset(coordinator_factory) -> None:
    coord = coordinator_factory()
    # Seed last value
    coord._apply_site_energy_guard("solar_production", 5.0, None)  # noqa: SLF001
    filtered, reset_at = coord._apply_site_energy_guard("solar_production", 4.6, 5.0)  # noqa: SLF001
    assert filtered == pytest.approx(5.0)
    assert reset_at is None
    filtered_inc, _ = coord._apply_site_energy_guard("solar_production", 5.5, 5.0)  # noqa: SLF001
    assert filtered_inc == pytest.approx(5.5)


def test_site_energy_guard_filtered_none_skips_store(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    monkeypatch.setattr(
        coord, "_apply_site_energy_guard", lambda *_args, **_kwargs: (None, None)
    )
    flows, meta = coord._aggregate_site_energy({"production": [1000]})  # noqa: SLF001
    assert flows == {}
    assert meta["interval_minutes"] == pytest.approx(5.0)


def test_site_energy_guard_handles_invalid_sample(coordinator_factory):
    coord = coordinator_factory()
    coord._apply_site_energy_guard("solar_production", 2.0, None)  # noqa: SLF001
    filtered, reset = coord._apply_site_energy_guard("solar_production", "bad", 2.0)  # noqa: SLF001
    assert filtered == pytest.approx(2.0)
    assert reset is None


def test_site_energy_guard_sets_prev_when_missing(coordinator_factory):
    coord = coordinator_factory()
    filtered, reset = coord._apply_site_energy_guard("grid_import", None, 1.5)  # noqa: SLF001
    assert filtered == pytest.approx(1.5)
    assert reset is None


def test_site_energy_default_interval_applied(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {"production": [600]}
    flows, meta = coord._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["solar_production"].value_kwh == pytest.approx(0.6)
    assert flows["solar_production"].interval_minutes == pytest.approx(5.0)
    assert meta["interval_minutes"] == pytest.approx(5.0)


def test_site_energy_interval_hours_edge_cases(monkeypatch, coordinator_factory) -> None:
    coord = coordinator_factory()
    # Non-dict payload falls back to default interval
    hours, minutes = coord._site_energy_interval_hours(None)  # noqa: SLF001
    assert minutes == pytest.approx(5.0)
    assert hours == pytest.approx(5.0 / 60.0)
    # Fallback "interval" key is honored
    hours, minutes = coord._site_energy_interval_hours({"interval": 10})  # noqa: SLF001
    assert minutes == pytest.approx(10.0)
    assert hours == pytest.approx(10.0 / 60.0)

    class BoomFloat:
        def __le__(self, _other):
            return False

        def __float__(self):
            raise ValueError("boom")

    # Force float conversion error branch
    monkeypatch.setattr(coord, "_coerce_energy_value", lambda _v: BoomFloat())
    hours, minutes = coord._site_energy_interval_hours({"interval_minutes": "bad"})  # noqa: SLF001
    assert minutes == pytest.approx(5.0)
    assert hours == pytest.approx(5.0 / 60.0)

    class WeirdZero(float):
        def __le__(self, other):
            return False

    # Bypass initial <=0 check to hit hours<=0 fallback
    monkeypatch.setattr(coord, "_coerce_energy_value", lambda _v: WeirdZero(0.0))
    hours, minutes = coord._site_energy_interval_hours({"interval_minutes": WeirdZero(0.0)})  # noqa: SLF001
    assert minutes == pytest.approx(5.0)
    assert hours == pytest.approx(5.0 / 60.0)


def test_site_energy_store_round_failure(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    call_count = 0
    real_round = round

    def boom_round(val, ndigits=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("round boom")
        return real_round(val, ndigits) if ndigits is not None else real_round(val)

    monkeypatch.setattr("builtins.round", boom_round)
    flows, _meta = coord._aggregate_site_energy({"production": [1000]})  # noqa: SLF001
    assert flows == {}


@pytest.mark.asyncio
async def test_async_refresh_site_energy_handles_missing_attrs(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(return_value=None)
    delattr(coord, "_site_energy_cache_ts")
    delattr(coord, "_site_energy_cache_ttl")
    await coord._async_refresh_site_energy()  # noqa: SLF001
    assert coord._site_energy_cache_ts is None


@pytest.mark.asyncio
async def test_async_refresh_site_energy_parsed_none(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(return_value={"bad": "payload"})
    monkeypatch.setattr(coord, "_aggregate_site_energy", lambda payload: None)
    await coord._async_refresh_site_energy()  # noqa: SLF001
    assert coord.site_energy == {}


def test_collect_site_metrics_includes_site_energy_meta(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._site_energy_meta = {
        "start_date": "2024-01-01",
        "last_report_date": datetime(2024, 1, 2, tzinfo=timezone.utc),
        "update_pending": True,
    }
    coord._site_energy_cache_ts = None
    metrics = coord.collect_site_metrics()
    assert "site_energy" in metrics
    assert metrics["site_energy"]["start_date"] == "2024-01-01"


@pytest.mark.asyncio
async def test_success_clears_reauth_issue(hass, monkeypatch, mock_issue_registry, coordinator_factory):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(return_value={"evChargerData": []})
    coord._fake_unauth = 1

    def _get(self):
        return getattr(self, "_fake_unauth", 0)

    def _set(self, val):
        if val == 0 and getattr(self, "_fake_unauth", 0) > 0:
            return
        self._fake_unauth = val

    monkeypatch.setattr(coord.__class__, "_unauth_errors", property(_get, _set), raising=False)
    await coord._async_update_data()
    assert ("enphase_ev", "reauth_required") in mock_issue_registry.deleted
    assert len([d for d in mock_issue_registry.deleted if d[1] == "reauth_required"]) >= 2


@pytest.mark.asyncio
async def test_summary_ip_address_empty_string(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "SN1",
                    "name": "Test",
                    "networkConfig": {"ipaddr": ""},
                    "connectors": [{}],
                    "session_d": {},
                }
            ]
        }
    )
    coord._configured_serials = {"SN1"}
    coord.serials = {"SN1"}
    coord._serial_order = ["SN1"]
    coord.data = {"SN1": {}}
    coord._amp_restart_tasks = {}
    await coord._async_update_data()
    assert coord.data["SN1"].get("ip_address") is None


@pytest.mark.asyncio
async def test_summary_ip_address_empty_string_list(monkeypatch, coordinator_factory):
    coord = coordinator_factory()

    class FlakyStr(str):
        def __new__(cls, value=""):
            obj = super().__new__(cls, value)
            obj._count = 0
            return obj

        def __bool__(self):
            self._count += 1
            if self._count < 3:
                return True
            return False

    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "SN1",
                    "name": "Test",
                    "connectors": [{}],
                    "session_d": {},
                }
            ]
        }
    )
    coord.summary.async_fetch = AsyncMock(
        return_value=[
            {
                "serialNumber": "SN1",
                "networkConfig": [{"ipaddr": (flaky := FlakyStr(""))}],
                "connectors": [{}],
            }
        ]
    )
    coord._configured_serials = {"SN1"}
    coord.serials = {"SN1"}
    coord._serial_order = ["SN1"]
    coord.data = {"SN1": {}}
    coord._amp_restart_tasks = {}
    await coord._async_update_data()
    assert coord.data["SN1"].get("ip_address") is None
    assert flaky._count >= 3


@pytest.mark.asyncio
async def test_async_update_sets_update_interval_with_exception(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord.config_entry = SimpleNamespace(options={})
    coord.client.status = AsyncMock(return_value={"evChargerData": []})
    coord._fast_until = time.monotonic() + 5

    class BoomCoord(type(coord)):
        def async_set_update_interval(self, *_args, **_kwargs):
            raise ValueError("fail")

    coord.__class__.async_set_update_interval = BoomCoord.async_set_update_interval  # type: ignore[assignment]
    coord.update_interval = timedelta(seconds=1)
    await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_update_handles_async_set_update_interval_error(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord.config_entry = SimpleNamespace(options={})
    coord.client.status = AsyncMock(return_value={"evChargerData": []})
    def boom_update_interval(*_args, **_kwargs):
        raise ValueError("boom")

    coord.async_set_update_interval = boom_update_interval
    coord._fast_until = time.monotonic() + 5
    coord.update_interval = timedelta(seconds=5)
    await coord._async_update_data()


def test_slow_interval_floor_handles_bad_update_interval(monkeypatch, coordinator_factory):
    coord = coordinator_factory()

    class BadInterval:
        def total_seconds(self):
            raise ValueError("bad")

    monkeypatch.setattr(
        coord.__class__, "update_interval", property(lambda self: BadInterval())
    )
    assert coord._slow_interval_floor() >= 1


def test_site_energy_import_diff_fallback(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "consumption": [500, 500],
        "solar_home": [100, None],
        "start_date": "2024-02-01",
        "interval_minutes": 60,
    }
    flows, _meta = coord._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["grid_import"].fields_used == ["consumption", "solar_home"]
    assert flows["grid_import"].value_kwh == pytest.approx(0.9)
    assert flows["grid_import"].bucket_count == 1


def test_site_energy_import_diff_with_battery_home(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "consumption": [1000, 500],
        "solar_home": [500, 100],
        "battery_home": [400, 300],
        "interval_minutes": 60,
    }
    flows, _meta = coord._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    # Base diff: (1500-600)=900 Wh
    assert flows["grid_import"].value_kwh == pytest.approx(0.9)
    assert flows["grid_import"].fields_used == ["consumption", "solar_home"]


def test_site_energy_import_diff_skips_when_battery_overlaps(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "consumption": [500],
        "solar_home": [200],
        "battery_home": [400],
        "interval_minutes": 60,
    }
    flows, _meta = coord._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows["grid_import"].value_kwh == pytest.approx(0.3)


def test_site_energy_import_fallbacks_to_grid_home(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "consumption": [500],
        "grid_home": [500],
        "interval_minutes": 60,
    }
    flows, _meta = coord._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows["grid_import"].value_kwh == pytest.approx(0.5)
    assert flows["grid_import"].fields_used == ["grid_home"]


def test_site_energy_honors_interval_minutes(coordinator_factory) -> None:
    coord = coordinator_factory()
    payload = {
        "production": [1200, 600, None],
        "interval_minutes": 15,
    }
    flows, meta = coord._aggregate_site_energy(payload)  # noqa: SLF001
    assert flows is not None
    assert flows["solar_production"].value_kwh == pytest.approx(1.8)
    assert flows["solar_production"].bucket_count == 2
    assert flows["solar_production"].interval_minutes == pytest.approx(15.0)
    assert meta["interval_minutes"] == pytest.approx(15.0)
    assert flows["solar_production"].source_unit == "Wh"


def test_site_energy_guard_confirms_reset(coordinator_factory) -> None:
    coord = coordinator_factory()
    base_payload = {
        "production": [1000],
        "start_date": "2024-01-01",
        "interval_minutes": 60,
    }
    flows, _ = coord._aggregate_site_energy(base_payload)  # noqa: SLF001
    coord.site_energy = flows

    drop_payload = {
        "production": [100],
        "start_date": "2024-01-01",
        "interval_minutes": 60,
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
        return {"production": [500], "interval_minutes": 60}

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
            interval_minutes=60,
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
    assert attrs["source_unit"] == "Wh"
    assert attrs["interval_minutes"] == 60
    assert sensor.entity_registry_enabled_default is False


@pytest.mark.asyncio
async def test_site_energy_sensor_restoration(monkeypatch, hass, coordinator_factory):
    coord = coordinator_factory()
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_import", "site_grid_import", "Grid Import"
    )
    sensor.hass = hass

    class LastData:
        native_value = "2.5"

    sensor.async_get_last_sensor_data = AsyncMock(return_value=LastData())

    class LastState:
        attributes = {"last_reset_at": "2024-02-01T00:00:00+00:00"}

    sensor.async_get_last_state = AsyncMock(return_value=LastState())
    await sensor.async_added_to_hass()
    assert sensor.native_value == pytest.approx(2.5)
    assert sensor.available is True
    assert sensor.extra_state_attributes["last_reset_at"].startswith("2024-02-01")

    # flow data as dict should be returned verbatim
    coord.site_energy = {"grid_import": {"value_kwh": "bad"}}
    assert sensor._flow_data() == {"value_kwh": "bad"}
    assert sensor._current_value() is None
    sensor._restored_value = 1.0
    coord.site_energy = {}
    attrs = sensor.extra_state_attributes
    assert attrs["last_report_date"] is None
    assert sensor.native_value == 1.0

    coord.site_energy = {"grid_import": {"value_kwh": 3.0, "last_report_date": "soon"}}
    assert sensor.extra_state_attributes["last_report_date"] == "soon"

    sensor2 = EnphaseSiteEnergySensor(
        coord, "grid_export", "site_grid_export", "Grid Export"
    )
    sensor2.hass = hass
    sensor2.async_get_last_sensor_data = AsyncMock(return_value=None)
    sensor2.async_get_last_state = AsyncMock(side_effect=RuntimeError("boom"))
    await sensor2.async_added_to_hass()

    class BadLastData:
        @property
        def native_value(self):
            raise ValueError("boom")

    sensor3 = EnphaseSiteEnergySensor(
        coord, "battery_charge", "site_battery_charge", "Battery Charge"
    )
    sensor3.hass = hass
    sensor3.async_get_last_sensor_data = AsyncMock(return_value=BadLastData())
    sensor3.async_get_last_state = AsyncMock(return_value=None)
    await sensor3.async_added_to_hass()
    sensor3._restored_value = 1.5
    assert sensor3.available is True

    class BadFlow(SiteEnergyFlow):
        def __getattribute__(self, name):
            if name == "__dict__":
                raise RuntimeError("no dict")
            return super().__getattribute__(name)

    coord.site_energy = {"battery_charge": BadFlow(None, 0, [], None, None, None)}
    assert sensor3._flow_data() == {}
    class BadStr:
        def __str__(self):
            raise ValueError("bad str")

    coord.site_energy = {"battery_charge": {"value_kwh": 1.0, "last_report_date": BadStr()}}
    attrs = sensor3.extra_state_attributes
    assert attrs["last_report_date"] is None


@pytest.mark.asyncio
async def test_site_energy_sensor_available_follows_super(hass, coordinator_factory):
    coord = coordinator_factory()
    coord.last_update_success = False
    coord.last_success_utc = None
    coord.site_energy = {}
    sensor = EnphaseSiteEnergySensor(
        coord, "grid_import", "site_grid_import", "Grid Import"
    )
    sensor.hass = hass
    sensor._restored_value = None
    assert sensor.available is False


@pytest.mark.asyncio
async def test_site_energy_direct_arrays(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord.client.lifetime_energy = AsyncMock(
        return_value={
            "consumption": [200],
            "solar_home": [50],
            "solar_grid": [75],
            "charge": [25],
            "discharge": [10],
            "start_date": "2024-01-01",
            "last_report_date": 1_700_000_000,
            "interval_minutes": 60,
        }
    )
    await coord._async_refresh_site_energy()  # noqa: SLF001
    assert set(coord.site_energy) == {
        "consumption",
        "grid_import",
        "grid_export",
        "battery_charge",
        "battery_discharge",
    }
