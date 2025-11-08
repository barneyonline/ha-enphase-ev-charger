"""Additional coverage for Enphase EV sensor helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

pytest.importorskip("homeassistant")


def _mk_coord(sn: str, payload: dict[str, Any]) -> Any:
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = SimpleNamespace()
    coord.hass.config = SimpleNamespace(
        units=SimpleNamespace(length_unit="mi"),
    )
    coord.data = {sn: payload}
    coord.serials = {sn}
    coord.last_set_amps = {}
    coord.site_id = "site-test"
    coord._serial_order = [sn]
    coord.iter_serials = lambda: list(coord.serials)
    coord.async_add_listener = lambda cb: cb  # type: ignore[assignment]
    coord.last_success_utc = None
    coord.last_failure_utc = None
    coord.last_failure_status = None
    coord.last_failure_description = None
    coord.last_failure_source = None
    coord.last_failure_response = None
    coord.backoff_ends_utc = None
    coord.latency_ms = None
    return coord


@pytest.mark.asyncio
async def test_async_setup_entry_registers_entities(
    hass, config_entry, coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.data[RANDOM_SERIAL].update(
        {
            "connector_status": "AVAILABLE",
            "charge_mode": "IMMEDIATE",
            "plugged": True,
            "charging_level": 32,
        }
    )
    callbacks: dict[str, Any] = {}

    def fake_add_listener(cb):
        callbacks["cb"] = cb
        return lambda: None

    coord.async_add_listener = fake_add_listener  # type: ignore[assignment]
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added = []

    def _async_add_entities(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _async_add_entities)
    assert any(ent.unique_id.endswith("_energy_today") for ent in added)

    # Simulate discovery of a new charger via the registered callback
    coord.serials.add("NEW123")
    coord.data["NEW123"] = {"sn": "NEW123", "name": "Second"}
    callbacks["cb"]()
    assert any(ent.unique_id.endswith("_NEW123_energy_today") for ent in added)


def test_energy_today_restore_data_handles_invalid_payload():
    from custom_components.enphase_ev.sensor import _EnergyTodayRestoreData

    parsed = _EnergyTodayRestoreData.from_dict(
        {
            "baseline_kwh": "bad",
            "baseline_day": 20251030,
            "last_total_kwh": object(),
            "last_reset_at": None,
            "stale_session_kwh": "oops",
            "stale_session_day": "",
            "last_session_kwh": "invalid",
        }
    )
    assert parsed.baseline_kwh is None
    assert parsed.baseline_day == "20251030"
    assert parsed.last_total_kwh is None
    assert parsed.stale_session_day == ""
    assert parsed.last_session_kwh is None


@pytest.mark.asyncio
async def test_energy_today_async_added_restores_state(monkeypatch):
    from homeassistant.helpers.update_coordinator import CoordinatorEntity
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.sensor import (
        EnphaseEnergyTodaySensor,
        _EnergyTodayRestoreData,
    )

    sn = RANDOM_SERIAL
    coord = _mk_coord(sn, {"sn": sn, "name": "Garage", "lifetime_kwh": 12.0})
    sensor = EnphaseEnergyTodaySensor(coord, sn)
    base_time = datetime(2025, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: base_time)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_time)
    async def _noop(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)

    fake_state = SimpleNamespace(
        state="1.23",
        attributes={
            "baseline_kwh": "5.5",
            "baseline_day": base_time.strftime("%Y-%m-%d"),
            "last_total_kwh": "8.0",
            "last_reset_at": "state-reset",
        },
    )

    async def _fake_last_state(self):
        return fake_state

    async def _fake_last_extra(self):
        return _EnergyTodayRestoreData(
            baseline_kwh=4.0,
            baseline_day=base_time.strftime("%Y-%m-%d"),
            last_total_kwh=None,
            last_reset_at=None,
            stale_session_kwh=None,
            stale_session_day="2025-10-29",
            last_session_kwh=0.25,
        )

    monkeypatch.setattr(
        EnphaseEnergyTodaySensor, "async_get_last_state", _fake_last_state
    )
    monkeypatch.setattr(
        EnphaseEnergyTodaySensor, "async_get_last_extra_data", _fake_last_extra
    )

    await sensor.async_added_to_hass()
    assert sensor._baseline_kwh == pytest.approx(4.0)
    assert sensor._last_total == pytest.approx(8.0)
    assert sensor._last_session_kwh == pytest.approx(0.25)


def test_energy_today_native_value_resets_on_session_end():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    payload = {
        "sn": sn,
        "name": "EV",
        "session_kwh": 1.5,
        "session_end": 123456,
        "charging": False,
    }
    coord = _mk_coord(sn, payload)
    sensor = EnphaseEnergyTodaySensor(coord, sn)
    sensor._last_total = None
    assert sensor.native_value == 0.0
    assert sensor._last_value == 0.0


def test_energy_today_status_handles_stale_day(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    day = "2025-10-30"
    payload = {"sn": sn, "name": "EV"}
    coord = _mk_coord(sn, payload)
    sensor = EnphaseEnergyTodaySensor(coord, sn)
    sensor._baseline_day = day
    sensor._last_value = 0.6
    sensor._stale_session_kwh = 0.5
    sensor._stale_session_day = day
    sensor._rollover_reference_kwh = None
    sensor._last_total = None
    base_time = datetime(2025, 10, 30, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_time)
    val = sensor._value_from_status({"session_energy_wh": 500, "charging": False})
    assert val == 0.0


def test_energy_today_lifetime_detects_reset(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = _mk_coord(sn, {"sn": sn, "name": "EV", "lifetime_kwh": 10.0})
    sensor = EnphaseEnergyTodaySensor(coord, sn)
    sensor._baseline_kwh = 8.0
    sensor._baseline_day = "2025-10-30"
    sensor._last_value = 2.0
    sensor._last_total = 10.0
    base_time = datetime(2025, 10, 30, 11, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: base_time)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_time)
    val = sensor._value_from_lifetime({"lifetime_kwh": 3.0})
    assert val == 0.0
    assert sensor._baseline_kwh == pytest.approx(3.0)


def test_energy_today_sessions_reset_on_day_change(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    payload = {
        "sn": sn,
        "name": "EV",
        "energy_today_sessions": [{"energy_kwh": 1}],
        "energy_today_sessions_kwh": 1.0,
        "session_end": "2025-10-29T23:59:00Z",
        "charging": False,
    }
    coord = _mk_coord(sn, payload)
    sensor = EnphaseEnergyTodaySensor(coord, sn)
    sensor._baseline_day = "2025-10-30"
    sensor._rollover_reference_kwh = 10.0
    sensor._last_total = None
    base_time = datetime(2025, 10, 30, 1, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: base_time)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_time)
    assert sensor._value_from_sessions(payload) == 0.0


def test_session_metadata_attributes_formats_fields(monkeypatch):
    from homeassistant.const import UnitOfLength

    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    hass = SimpleNamespace(config=SimpleNamespace(units=SimpleNamespace(length_unit=UnitOfLength.KILOMETERS)))
    attrs = EnphaseEnergyTodaySensor._session_metadata_attributes(
        {
            "session_plug_in_at": "2025-10-30T06:00:00Z[UTC]",
            "session_plug_out_at": 1_700_000_000,
            "session_kwh": "1.5",
            "session_energy_wh": "400",
            "session_cost": "3.4567",
            "session_charge_level": "88",
            "session_miles": "12.5",
        },
        hass=hass,
    )
    assert attrs["plugged_in_at"] is not None
    assert attrs["energy_consumed_wh"] is not None
    assert attrs["session_charge_level"] == 88
    assert attrs["range_added"] == pytest.approx(20.117)


def test_resolve_session_local_day_returns_first_available():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = _mk_coord(sn, {"sn": sn, "name": "EV"})
    sensor = EnphaseEnergyTodaySensor(coord, sn)
    day = sensor._resolve_session_local_day(
        {"session_plug_out_at": "2025-10-30T10:00:00Z", "session_end": 1_700_000_000}
    )
    assert day is not None


def test_connector_status_icon_variants():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    payload = {"sn": sn, "name": "EV", "connector_status": "UNPLUGGED"}
    sensor = EnphaseConnectorStatusSensor(_mk_coord(sn, payload), sn)
    assert sensor.icon == "mdi:power-plug-off"


@pytest.mark.asyncio
async def test_power_sensor_restores_state(monkeypatch):
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL
    payload = {"sn": sn, "name": "EV", "lifetime_kwh": 5.0, "last_reported_at": "2025-10-30T09:55:00Z"}
    coord = _mk_coord(sn, payload)
    sensor = EnphasePowerSensor(coord, sn)
    async def _noop(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)

    fake_state = SimpleNamespace(
        state="bad",
        attributes={
            "last_lifetime_kwh": "10.5",
            "last_energy_ts": "1",
            "last_sample_ts": "2",
            "last_power_w": "3000",
            "last_window_seconds": "5",
            "method": "legacy",
            "last_reset_at": "123",
            "baseline_kwh": "8",
            "last_energy_today_kwh": "1.5",
            "last_ts": "3",
        },
    )

    async def _fake_last_state(self):
        return fake_state

    monkeypatch.setattr(EnphasePowerSensor, "async_get_last_state", _fake_last_state)
    await sensor.async_added_to_hass()
    assert sensor._last_power_w == 3000


def test_power_sensor_parse_timestamp_variants():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    assert EnphasePowerSensor._parse_timestamp(1_700_000_000_000) is not None
    assert EnphasePowerSensor._parse_timestamp("2025-10-30T10:00:00Z[UTC]") is not None
    assert EnphasePowerSensor._parse_timestamp("") is None


def test_power_sensor_resolve_max_throughput_prefers_session_level():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    data = {"session_charge_level": "40", "operating_v": 230}
    sensor = EnphasePowerSensor(_mk_coord(RANDOM_SERIAL, {"sn": RANDOM_SERIAL, "name": "EV"}), RANDOM_SERIAL)
    bounded, source, amps, voltage, unbounded = sensor._resolve_max_throughput(data)
    assert source == "session_charge_level"
    assert amps == pytest.approx(40.0)
    assert bounded <= sensor._STATIC_MAX_WATTS


def test_power_sensor_native_value_handles_drop(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord(sn, {"sn": sn, "name": "EV", "lifetime_kwh": 10.0, "last_reported_at": "2025-10-30T10:00:00Z"})
    sensor = EnphasePowerSensor(coord, sn)
    assert sensor.native_value == 0

    coord.data[sn]["lifetime_kwh"] = 9.0
    coord.data[sn]["last_reported_at"] = "2025-10-30T10:05:00Z"
    assert sensor.native_value == 0

    coord.data[sn]["lifetime_kwh"] = 9.5
    coord.data[sn]["last_reported_at"] = "2025-10-30T10:10:00Z"
    monkeypatch.setattr(dt_util, "now", lambda: datetime(2025, 10, 30, 10, 10, tzinfo=timezone.utc))
    assert sensor.native_value > 0


def test_session_duration_uses_fixed_end(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseSessionDurationSensor

    sn = RANDOM_SERIAL
    start = int(datetime.now(timezone.utc).timestamp()) - 600
    payload = {"sn": sn, "name": "EV", "session_start": start, "session_end": start + 300, "charging": False}
    coord = _mk_coord(sn, payload)
    sensor = EnphaseSessionDurationSensor(coord, sn)
    assert sensor.native_value == 5


def test_charge_mode_sensor_prefers_pref_field():
    from custom_components.enphase_ev.sensor import EnphaseChargeModeSensor

    sn = RANDOM_SERIAL
    payload = {"sn": sn, "name": "EV", "charge_mode_pref": "green_charging"}
    sensor = EnphaseChargeModeSensor(_mk_coord(sn, payload), sn)
    assert sensor.native_value == "green_charging"
    assert sensor.icon == "mdi:leaf"


@pytest.mark.asyncio
async def test_lifetime_energy_restore(monkeypatch):
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev.sensor import EnphaseLifetimeEnergySensor

    sn = RANDOM_SERIAL
    coord = _mk_coord(sn, {"sn": sn, "name": "EV"})
    sensor = EnphaseLifetimeEnergySensor(coord, sn)
    async def _noop(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)

    class _SensorData:
        native_value = "11.0"

    async def _fake_sensor_data(self):
        return _SensorData()

    async def _fake_last_state(self):
        return SimpleNamespace(attributes={"last_reset_value": "5.0", "last_reset_at": "ts"})

    monkeypatch.setattr(
        EnphaseLifetimeEnergySensor, "async_get_last_sensor_data", _fake_sensor_data
    )
    monkeypatch.setattr(
        EnphaseLifetimeEnergySensor, "async_get_last_state", _fake_last_state
    )
    await sensor.async_added_to_hass()
    assert sensor.native_value == pytest.approx(11.0)


def test_lifetime_energy_native_value_handles_drop(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.sensor import EnphaseLifetimeEnergySensor

    sn = RANDOM_SERIAL
    coord = _mk_coord(sn, {"sn": sn, "name": "EV", "lifetime_kwh": 50.0})
    sensor = EnphaseLifetimeEnergySensor(coord, sn)
    sensor._last_value = 40.0
    sensor._boot_filter = False
    coord.data[sn]["lifetime_kwh"] = 10.0
    base_time = datetime(2025, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_time)
    assert sensor.native_value == 10.0
    assert sensor._last_reset_value == 10.0


def test_status_sensor_passthrough():
    from custom_components.enphase_ev.sensor import EnphaseStatusSensor

    sn = RANDOM_SERIAL
    payload = {"sn": sn, "name": "EV", "status": "available"}
    sensor = EnphaseStatusSensor(_mk_coord(sn, payload), sn)
    assert sensor.native_value == "available"


def test_timestamp_from_iso_sensor_parses_values():
    from custom_components.enphase_ev.sensor import _TimestampFromIsoSensor

    sn = RANDOM_SERIAL
    payload = {"sn": sn, "name": "EV", "iso": "2025-10-30T10:00:00Z[UTC]"}
    sensor = _TimestampFromIsoSensor(_mk_coord(sn, payload), sn, "iso", "ISO", "uniq")
    assert sensor.native_value is not None


def test_timestamp_from_epoch_sensor_parses_values():
    from custom_components.enphase_ev.sensor import _TimestampFromEpochSensor

    sn = RANDOM_SERIAL
    payload = {"sn": sn, "name": "EV", "epoch": 1_700_000_000}
    sensor = _TimestampFromEpochSensor(_mk_coord(sn, payload), sn, "epoch", "Epoch", "uniq2")
    assert sensor.native_value is not None


def test_site_base_entity_helpers(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import (
        EnphaseSiteBackoffEndsSensor,
        EnphaseSiteLastErrorCodeSensor,
        EnphaseSiteLastUpdateSensor,
    )

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.site_id = "site"
    coord.last_success_utc = datetime(2025, 10, 30, 8, 0, 0, tzinfo=timezone.utc)
    coord.last_failure_utc = datetime(2025, 10, 30, 9, 0, 0, tzinfo=timezone.utc)
    coord.last_failure_status = None
    coord.last_failure_description = "dns lookup failed"
    coord.last_failure_source = "network"
    coord.last_failure_response = "resp"
    coord.backoff_ends_utc = datetime(2025, 10, 30, 11, 0, 0, tzinfo=timezone.utc)
    coord.latency_ms = 120
    coord.async_add_listener = lambda cb: None

    err_sensor = EnphaseSiteLastErrorCodeSensor(coord)
    assert err_sensor.available is True
    assert err_sensor.native_value == "dns_error"

    update_sensor = EnphaseSiteLastUpdateSensor(coord)
    assert update_sensor.device_info["name"].startswith("Enphase Site")

    backoff_sensor = EnphaseSiteBackoffEndsSensor(coord)
    monkeypatch.setattr(dt_util, "utcnow", lambda: datetime(2025, 10, 30, 10, 30, 0, tzinfo=timezone.utc))
    assert backoff_sensor._backoff_remaining_seconds() >= 1
