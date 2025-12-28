from datetime import datetime, timedelta, timezone

import pytest

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

pytest.importorskip("homeassistant")
from homeassistant.components.sensor import SensorStateClass


def _mk_coord_with(sn: str, payload: dict):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: payload}
    coord.serials = {sn}
    coord.last_set_amps = {}
    coord.site_id = "site"
    coord.last_update_success = True
    return coord


def test_charging_level_fallback():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "session_start": None,
        },
    )
    coord.set_last_set_amps = lambda s, a: None  # no-op
    coord.pick_start_amps = lambda s: 30
    coord.last_set_amps[sn] = 30

    sensor = EnphaseChargingLevelSensor(coord, sn)
    assert sensor.native_value == 30


def test_charging_level_attributes_include_limits():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging_level": 32,
            "min_amp": " 6 ",
            "max_amp": 40,
            "max_current": "48",
            "amp_granularity": "2",
        },
    )

    sensor = EnphaseChargingLevelSensor(coord, sn)
    attrs = sensor.extra_state_attributes
    assert attrs == {
        "min_amp": 6,
        "max_amp": 40,
        "max_current": 48,
        "amp_granularity": 2,
    }

    coord.data[sn]["min_amp"] = "bad"
    coord.data[sn]["max_amp"] = None
    coord.data[sn]["max_current"] = ""
    coord.data[sn]["amp_granularity"] = "bad"
    attrs = sensor.extra_state_attributes
    assert attrs == {
        "min_amp": None,
        "max_amp": None,
        "max_current": None,
        "amp_granularity": None,
    }


def test_charging_level_invalid_value_falls_back():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging_level": "bad",
        },
    )
    coord.pick_start_amps = lambda s: 18

    sensor = EnphaseChargingLevelSensor(coord, sn)
    assert sensor.native_value == 18


def test_electrical_phase_sensor_formats_state_and_attributes():
    from custom_components.enphase_ev.sensor import EnphaseElectricalPhaseSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "phase_mode": "3",
            "dlb_enabled": "true",
            "dlb_active": 1,
        },
    )

    sensor = EnphaseElectricalPhaseSensor(coord, sn)
    assert sensor.native_value == "Three Phase"
    attrs = sensor.extra_state_attributes
    assert attrs["phase_mode_raw"] == "3"
    assert attrs["dlb_enabled"] is True
    assert attrs["dlb_active"] is True

    coord.data[sn]["phase_mode"] = " "
    coord.data[sn]["dlb_enabled"] = None
    coord.data[sn]["dlb_active"] = None
    assert sensor.native_value is None
    attrs = sensor.extra_state_attributes
    assert attrs["phase_mode_raw"] is None
    assert attrs["dlb_enabled"] is None
    assert attrs["dlb_active"] is None


def test_power_sensor_uses_lifetime_delta():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 10.0,
            "last_reported_at": "2025-09-09T10:00:00Z[UTC]",
            "charging": True,
        },
    )

    sensor = EnphasePowerSensor(coord, sn)
    assert sensor.native_value == 0

    coord.data[sn]["lifetime_kwh"] = 10.6  # +0.6 kWh
    coord.data[sn]["last_reported_at"] = "2025-09-09T10:05:00Z[UTC]"
    val = sensor.native_value
    assert val == 7200
    assert sensor.extra_state_attributes["last_window_seconds"] == pytest.approx(300)

    coord.data[sn]["lifetime_kwh"] = 10.6
    coord.data[sn]["last_reported_at"] = "2025-09-09T10:06:00Z[UTC]"
    assert sensor.native_value == 7200


def test_power_sensor_zero_when_idle():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 5.0,
            "last_reported_at": "2025-09-09T09:00:00Z",
            "charging": True,
        },
    )
    sensor = EnphasePowerSensor(coord, sn)
    assert sensor.native_value == 0

    coord.data[sn]["lifetime_kwh"] = 5.5
    coord.data[sn]["last_reported_at"] = "2025-09-09T09:05:00Z"
    assert sensor.native_value == 6000

    coord.data[sn]["charging"] = False
    coord.data[sn]["last_reported_at"] = "2025-09-09T09:06:00Z"
    assert sensor.native_value == 0


def test_last_session_sensor_tracks_session_and_persists(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    start = datetime(2025, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "session_kwh": 4.25,
            "session_start": int(start.timestamp()),
            "session_end": int(end.timestamp()),
            "charging": False,
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.state_class == SensorStateClass.TOTAL
    monkeypatch.setattr(dt_util, "utcnow", lambda: end)
    assert sensor.native_value == pytest.approx(4.25)
    attrs = sensor.extra_state_attributes
    assert attrs["session_duration_min"] == 120

    coord.data[sn]["session_kwh"] = None
    coord.data[sn]["session_start"] = None
    coord.data[sn]["session_end"] = None
    assert sensor.native_value == pytest.approx(4.25)


def test_last_session_sensor_uses_history_when_available(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "energy_today_sessions": [
                {
                    "session_id": "a1",
                    "start": "2025-10-01T01:00:00+00:00",
                    "end": "2025-10-01T02:00:00+00:00",
                    "energy_kwh_total": 6.5,
                    "active_charge_time_s": 3600,
                    "avg_cost_per_kwh": 0.25,
                    "cost_calculated": True,
                    "session_cost_state": "calculated",
                    "manual_override": False,
                    "charge_profile_stack_level": 2,
                }
            ],
            "energy_today_sessions_kwh": 6.5,
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(6.5)
    attrs = sensor.extra_state_attributes
    assert attrs["energy_consumed_kwh"] == pytest.approx(6.5)
    assert attrs["energy_consumed_wh"] == pytest.approx(6500.0)
    assert attrs["session_id"] == "a1"
    assert attrs["session_started_at"] == "2025-10-01T01:00:00+00:00"
    assert attrs["session_ended_at"] == "2025-10-01T02:00:00+00:00"
    assert attrs["active_charge_time_s"] == 3600
    assert attrs["avg_cost_per_kwh"] == pytest.approx(0.25)
    assert attrs["cost_calculated"] is True
    assert attrs["session_cost_state"] == "calculated"
    assert attrs["manual_override"] is False
    assert attrs["charge_profile_stack_level"] == 2


def test_last_session_merges_history_metadata(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging": True,
            "session_kwh": 1.25,
            "session_start": "2025-10-01T03:00:00+00:00",
            "session_end": "2025-10-01T04:00:00+00:00",
            "energy_today_sessions": [
                {
                    "session_id": "old",
                    "start": "2025-10-01T01:00:00+00:00",
                    "end": "2025-10-01T02:00:00+00:00",
                    "energy_kwh_total": 2.0,
                },
                {
                    "start": "2025-10-01T03:00:00+00:00",
                    "end": "2025-10-01T04:00:00+00:00",
                    "energy_kwh_total": 4.5,
                    "session_cost": 1.2,
                    "avg_cost_per_kwh": 0.24,
                    "manual_override": True,
                    "session_cost_state": "COST_CALCULATED",
                    "charge_profile_stack_level": 3,
                },
            ],
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(1.25)
    attrs = sensor.extra_state_attributes
    assert attrs["energy_consumed_kwh"] == pytest.approx(1.25)
    assert attrs["session_id"] is None
    assert attrs["session_started_at"] == "2025-10-01T03:00:00+00:00"
    assert attrs["session_ended_at"] == "2025-10-01T04:00:00+00:00"
    assert attrs["session_cost"] == pytest.approx(1.2)
    assert attrs["avg_cost_per_kwh"] == pytest.approx(0.24)
    assert attrs["manual_override"] is True
    assert attrs["session_cost_state"] == "COST_CALCULATED"
    assert attrs["charge_profile_stack_level"] == 3


def test_last_session_attributes_convert_units_and_duration(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.const import UnitOfLength
    from homeassistant.util import dt as dt_util
    from homeassistant.util.unit_conversion import DistanceConverter

    sn = RANDOM_SERIAL
    plug_in = "2025-10-24T20:00:00.000Z[UTC]"
    plug_out = "2025-10-24T22:30:15.000Z[UTC]"
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "session_energy_wh": 3561.53,
            "session_plug_in_at": plug_in,
            "session_plug_out_at": plug_out,
            "session_start": datetime(2025, 10, 24, 20, 0, 0, tzinfo=timezone.utc).timestamp(),
            "session_end": datetime(2025, 10, 24, 22, 30, 15, tzinfo=timezone.utc).timestamp(),
            "session_miles": 14.35368,
            "session_cost": 4.75,
            "session_charge_level": 32,
        },
    )

    base_utc = datetime(2025, 10, 24, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_utc)
    monkeypatch.setattr(
        dt_util,
        "as_local",
        lambda dt: dt.replace(tzinfo=timezone.utc),  # type: ignore[override]
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)

    class DummyUnits:
        length_unit = UnitOfLength.KILOMETERS

    class DummyConfig:
        units = DummyUnits()

    class DummyHass:
        config = DummyConfig()

    sensor.hass = DummyHass()  # type: ignore[assignment]

    assert sensor.native_value == round(3561.53 / 1000.0, 3)
    attrs = sensor.extra_state_attributes
    assert attrs["plugged_in_at"] == "2025-10-24T20:00:00+00:00"
    assert attrs["plugged_out_at"] == "2025-10-24T22:30:15+00:00"
    assert attrs["energy_consumed_wh"] == pytest.approx(3561.53)
    assert attrs["session_started_at"] == "2025-10-24T20:00:00+00:00"
    assert attrs["session_ended_at"] == "2025-10-24T22:30:15+00:00"
    expected_km = round(
        DistanceConverter.convert(
            14.35368, UnitOfLength.MILES, UnitOfLength.KILOMETERS
        ),
        3,
    )
    assert attrs["range_added"] == expected_km
    assert attrs["session_cost"] == 4.75
    assert attrs["session_charge_level"] == 32
    assert attrs["session_duration_min"] == 150


def test_last_session_restore_data_roundtrip():
    from custom_components.enphase_ev.sensor import _LastSessionRestoreData

    restored = _LastSessionRestoreData.from_dict(
        {
            "last_session_kwh": "2.5",
            "last_session_wh": "2500",
            "last_session_start": "100",
            "last_session_end": "200",
            "session_key": "abc",
            "last_duration_min": "45",
        }
    )
    assert restored.last_session_kwh == pytest.approx(2.5)
    assert restored.last_session_wh == pytest.approx(2500)
    assert restored.last_session_start == pytest.approx(100)
    assert restored.last_session_end == pytest.approx(200)
    assert restored.session_key == "abc"
    assert restored.last_duration_min == 45

    empty = _LastSessionRestoreData.from_dict(None)
    assert empty.last_session_kwh is None
    assert empty.session_key is None


def test_last_reported_sensor_exposes_reporting_interval():
    from custom_components.enphase_ev.sensor import EnphaseLastReportedSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "last_reported_at": "2025-09-07T11:38:31Z[UTC]",
            "reporting_interval": " 300 ",
        },
    )

    sensor = EnphaseLastReportedSensor(coord, sn)
    assert sensor.native_value is not None

    attrs = sensor.extra_state_attributes
    assert attrs["reporting_interval"] == 300

    coord.data[sn]["reporting_interval"] = 150
    assert sensor.extra_state_attributes["reporting_interval"] == 150

    coord.data[sn]["reporting_interval"] = "not-int"
    assert sensor.extra_state_attributes["reporting_interval"] is None


def test_last_reported_sensor_handles_missing_and_invalid_values():
    from custom_components.enphase_ev.sensor import EnphaseLastReportedSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "last_reported_at": None,
        },
    )

    sensor = EnphaseLastReportedSensor(coord, sn)
    assert sensor.native_value is None

    coord.data[sn]["last_reported_at"] = "not-a-timestamp"
    assert sensor.native_value is None


def test_power_sensor_caps_max_output():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 100.0,
            "last_reported_at": "2025-09-09T08:00:00Z",
            "charging": True,
        },
    )
    sensor = EnphasePowerSensor(coord, sn)
    assert sensor.native_value == 0

    coord.data[sn]["lifetime_kwh"] = 110.0
    coord.data[sn]["last_reported_at"] = "2025-09-09T08:05:00Z"
    assert sensor.native_value == 19200


def test_power_sensor_fallback_window_when_timestamp_missing(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphasePowerSensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 1.0,
            "charging": True,
        },
    )
    sensor = EnphasePowerSensor(coord, sn)

    anchor = datetime(2025, 9, 9, 7, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: anchor)
    monkeypatch.setattr(dt_util, "now", lambda: anchor)
    assert sensor.native_value == 0

    monkeypatch.setattr(dt_util, "utcnow", lambda: anchor + timedelta(minutes=5))
    monkeypatch.setattr(dt_util, "now", lambda: anchor + timedelta(minutes=5))
    coord.data[sn]["lifetime_kwh"] = 1.5
    coord.data[sn].pop("last_reported_at", None)
    assert sensor.native_value == 6000


def test_lifetime_energy_accepts_resets(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseLifetimeEnergySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    payload = {"sn": sn, "name": "Garage EV", "lifetime_kwh": 200.5}
    coord = _mk_coord_with(sn, payload)

    sensor = EnphaseLifetimeEnergySensor(coord, sn)
    assert sensor.native_value == pytest.approx(200.5)

    coord.data[sn]["lifetime_kwh"] = 200.75
    assert sensor.native_value == pytest.approx(200.75)

    coord.data[sn]["lifetime_kwh"] = 200.74
    assert sensor.native_value == pytest.approx(200.75)

    reset_time = datetime(2025, 9, 9, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: reset_time)
    coord.data[sn]["lifetime_kwh"] = 0
    assert sensor.native_value == pytest.approx(0)
    attrs = sensor.extra_state_attributes
    assert attrs["last_reset_value"] == pytest.approx(0)
    assert attrs["last_reset_at"] is not None

    coord.data[sn]["lifetime_kwh"] = 0.4
    assert sensor.native_value == pytest.approx(0.4)

    coord.data[sn]["lifetime_kwh"] = 0.38
    assert sensor.native_value == pytest.approx(0.4)

    coord.data[sn]["lifetime_kwh"] = 0.9
    assert sensor.native_value == pytest.approx(0.9)


def test_status_sensor_exposes_attributes(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseStatusSensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "status": "CONNECTED",
            "faulted": True,
            "commissioned": False,
            "suspended_by_evse": True,
            "offline_since": "2025-01-02T03:04:05Z",
        },
    )
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    sensor = EnphaseStatusSensor(coord, sn)
    assert sensor.native_value == "CONNECTED"
    attrs = sensor.extra_state_attributes
    assert attrs["commissioned"] is False
    assert attrs["charger_problem"] is True
    assert attrs["suspended_by_evse"] is True
    assert attrs["offline_since"] == "2025-01-02T03:04:05+00:00"


def test_connector_status_reports_reason_attribute():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "AVAILABLE",
            "connector_reason": "INSUFFICIENT_SOLAR",
            "connector_status_info": "  see manual ",
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {
        "status_reason": "INSUFFICIENT_SOLAR",
        "connector_status_info": "see manual",
    }


def test_connector_status_reason_absent_returns_empty_attributes():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "AVAILABLE",
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {
        "status_reason": None,
        "connector_status_info": None,
    }


def test_connector_status_reason_numeric_gets_stringified():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "SUSPENDED",
            "connector_reason": 123,
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {
        "status_reason": "123",
        "connector_status_info": None,
    }


def test_connector_status_reason_handles_non_string_value():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    sentinel = BadStr()
    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "FAULTED",
            "connector_reason": sentinel,
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {
        "status_reason": sentinel,
        "connector_status_info": None,
    }
