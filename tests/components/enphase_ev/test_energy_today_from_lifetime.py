import datetime as _dt

import pytest

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def test_last_session_value_survives_day_change(monkeypatch):
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    start = _dt.datetime(2025, 9, 9, 22, 0, 0, tzinfo=_dt.timezone.utc)
    end = start + _dt.timedelta(minutes=45)
    coord.data = {
        sn: {
            "sn": sn,
            "name": "IQ EV Charger",
            "session_kwh": 3.5,
            "session_start": int(start.timestamp()),
            "session_end": int(end.timestamp()),
            "charging": False,
        }
    }
    coord.serials = {sn}
    monkeypatch.setattr(dt_util, "utcnow", lambda: start)

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(3.5)

    next_day = start + _dt.timedelta(days=1)
    monkeypatch.setattr(dt_util, "utcnow", lambda: next_day)
    coord.data[sn]["session_kwh"] = None
    coord.data[sn]["session_start"] = None
    coord.data[sn]["session_end"] = None

    assert sensor.native_value == pytest.approx(3.5)


def test_last_session_prefers_active_session_over_history():
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {
        sn: {
            "sn": sn,
            "name": "IQ EV Charger",
            "session_kwh": 1.25,
            "energy_today_sessions": [
                {
                    "session_id": "old",
                    "start": "2025-09-08T10:00:00+00:00",
                    "end": "2025-09-08T11:00:00+00:00",
                    "energy_kwh_total": 6.0,
                }
            ],
            "energy_today_sessions_kwh": 6.0,
            "charging": True,
        }
    }
    coord.serials = {sn}

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(1.25)
    attrs = sensor.extra_state_attributes
    assert attrs["energy_consumed_kwh"] == pytest.approx(1.25)
