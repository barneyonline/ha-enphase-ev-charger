import datetime as _dt

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def test_energy_today_from_lifetime_monotonic(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: {"sn": sn, "name": "IQ EV Charger", "lifetime_kwh": 100.0}}
    coord.serials = {sn}

    # Freeze time to a specific day
    day1 = _dt.datetime(2025, 9, 9, 10, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day1)

    ent = EnphaseEnergyTodaySensor(coord, sn)

    # First read establishes baseline; should be 0.0
    assert ent.native_value == 0.0

    # Increase lifetime by 1.5 kWh → today should reflect delta
    coord.data[sn]["lifetime_kwh"] = 101.5
    assert ent.native_value == 1.5

    # Minor jitter down should not decrease today's value
    coord.data[sn]["lifetime_kwh"] = 101.49
    assert ent.native_value == 1.5

    # Next day: baseline resets and value starts from 0 again
    day2 = _dt.datetime(2025, 9, 10, 0, 1, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day2)
    coord.data[sn]["lifetime_kwh"] = 103.0
    assert ent.native_value == 0.0
    coord.data[sn]["lifetime_kwh"] = 104.2
    assert ent.native_value == 1.2


def test_energy_today_resets_without_lifetime(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    session_end = _dt.datetime(2025, 9, 9, 23, 55, 0, tzinfo=_dt.timezone.utc)
    coord.data = {
        sn: {
            "sn": sn,
            "name": "IQ EV Charger",
            "lifetime_kwh": 100.0,
            "session_kwh": 4.0,
            "session_end": session_end.timestamp(),
            "charging": False,
        }
    }
    coord.serials = {sn}

    day1 = _dt.datetime(2025, 9, 9, 23, 56, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day1)
    monkeypatch.setattr(dt_util, "utcnow", lambda: day1)

    ent = EnphaseEnergyTodaySensor(coord, sn)

    assert ent.native_value == 0.0

    coord.data[sn]["lifetime_kwh"] = 104.0
    assert ent.native_value == 4.0

    # Lifetime becomes temporarily unavailable but session info still present
    coord.data[sn]["lifetime_kwh"] = None

    day2 = _dt.datetime(2025, 9, 10, 0, 10, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day2)
    monkeypatch.setattr(dt_util, "utcnow", lambda: day2)

    # The sensor should reset to zero for the new day even without lifetime data
    assert ent.native_value == 0.0


def test_energy_today_rollover_handles_bad_last_total(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: {"sn": sn, "name": "IQ EV Charger", "lifetime_kwh": 50.0}}
    coord.serials = {sn}

    day1 = _dt.datetime(2025, 9, 12, 9, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day1)
    monkeypatch.setattr(dt_util, "utcnow", lambda: day1)

    ent = EnphaseEnergyTodaySensor(coord, sn)
    assert ent.native_value == 0.0

    coord.data[sn]["lifetime_kwh"] = 52.0
    assert ent.native_value == 2.0

    # Force a rollover with a non-castable last_total to cover the exception path
    ent._last_total = object()
    day2 = _dt.datetime(2025, 9, 13, 8, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day2)
    monkeypatch.setattr(dt_util, "utcnow", lambda: day2)
    # Call rollover directly to exercise the exception branch
    ent._rollover_if_new_day()
    ent._last_total = None
    coord.data[sn]["lifetime_kwh"] = 53.5
    assert ent.native_value == 0.0


def test_energy_today_status_rollover_with_reference(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.serials = {sn}
    coord.data = {
        sn: {
            "sn": sn,
            "name": "IQ EV Charger",
            "lifetime_kwh": 10.0,
        }
    }

    day1 = _dt.datetime(2025, 9, 20, 18, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day1)
    monkeypatch.setattr(dt_util, "utcnow", lambda: day1)

    ent = EnphaseEnergyTodaySensor(coord, sn)
    assert ent.native_value == 0.0
    coord.data[sn]["lifetime_kwh"] = 12.0
    assert ent.native_value == 2.0

    # Lose lifetime telemetry; rely on session values on the next day
    coord.data[sn]["lifetime_kwh"] = None
    coord.data[sn]["session_kwh"] = 1.2
    coord.data[sn]["session_plug_out_at"] = "2025-09-20T17:45:00.000Z[UTC]"
    coord.data[sn]["charging"] = False

    day2 = _dt.datetime(2025, 9, 21, 6, 30, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day2)
    monkeypatch.setattr(dt_util, "utcnow", lambda: day2)

    assert ent.native_value == 0.0


def test_energy_today_resolve_session_local_day(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.serials = {sn}
    coord.data = {sn: {"sn": sn, "name": "IQ"}}

    ent = EnphaseEnergyTodaySensor(coord, sn)
    ent._baseline_day = "2025-09-21"

    base = _dt.datetime(2025, 9, 21, 7, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)

    data = {
        "session_end": base.timestamp(),
        "session_plug_out_at": "2025-09-21T06:30:00.000Z[UTC]",
        "session_start": "2025-09-20T18:00:00Z",
        "session_plug_in_at": "bad",
    }
    day = ent._resolve_session_local_day(data)
    assert day == "2025-09-21"

    # Remove parseable fields to cover failure path
    data = {"session_plug_in_at": "bad-format"}
    assert ent._resolve_session_local_day(data) is None
