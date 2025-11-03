from datetime import datetime, timedelta, timezone

import pytest

from homeassistant.util import dt as dt_util

from tests.components.enphase_ev.random_ids import RANDOM_SITE_ID


def _make_site_coord():
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.site_id = RANDOM_SITE_ID
    coord.update_interval = timedelta(seconds=10)
    coord.last_success_utc = None
    coord.last_failure_utc = None
    coord.last_failure_status = None
    coord.last_failure_description = None
    coord.last_failure_response = None
    coord.last_failure_source = None
    coord.backoff_ends_utc = None
    return coord


def test_cloud_latency_sensor_value():
    from custom_components.enphase_ev.sensor import EnphaseCloudLatencySensor

    # Minimal coordinator stub
    coord = _make_site_coord()
    coord.latency_ms = 123
    coord.update_interval = timedelta(seconds=15)

    s = EnphaseCloudLatencySensor(coord)
    assert s.native_value == 123


def test_site_cloud_reachable_binary_sensor_states():
    from custom_components.enphase_ev.binary_sensor import (
        SiteCloudReachableBinarySensor,
    )
    coord = _make_site_coord()
    coord.update_interval = timedelta(seconds=10)

    bs = SiteCloudReachableBinarySensor(coord)
    # No last success yet -> off
    coord.last_success_utc = None
    assert bs.is_on is False

    # Recent success within 2x interval -> on
    now = datetime.now(timezone.utc)
    coord.last_success_utc = now - timedelta(seconds=15)
    assert bs.is_on is True

    # Stale success beyond 2x interval -> off
    coord.last_success_utc = now - timedelta(seconds=25)
    assert bs.is_on is False

    coord.last_failure_status = 500
    coord.last_failure_description = "Server error"
    payload = "{\"error\":\"server\"}"
    coord.last_failure_response = payload
    coord.last_failure_source = "http"
    coord.last_failure_utc = now

    attrs = bs.extra_state_attributes
    assert attrs["last_failure_status"] == 500
    assert attrs["code_description"] == "Server error"
    assert attrs["last_failure_response"] == payload
    assert attrs["last_failure_source"] == "http"
    assert "last_failure_utc" in attrs


def test_site_error_code_sensor_state_and_attributes():
    from custom_components.enphase_ev.sensor import EnphaseSiteLastErrorCodeSensor

    coord = _make_site_coord()
    sensor = EnphaseSiteLastErrorCodeSensor(coord)

    # No failure yet -> None
    assert sensor.native_value == "none"

    failure_time = datetime.now(timezone.utc)
    coord.last_failure_utc = failure_time
    coord.last_failure_status = 429
    coord.last_failure_description = "Rate limited"
    payload = "{\"error\":{\"code\":429}}"
    coord.last_failure_response = payload
    coord.last_failure_source = "http"

    assert sensor.native_value == "429"
    attrs = sensor.extra_state_attributes
    assert attrs["last_failure_status"] == 429
    assert attrs["code_description"] == "Rate limited"
    assert attrs["last_failure_response"] == payload
    assert attrs["last_failure_source"] == "http"
    assert attrs["last_failure_utc"] == failure_time.isoformat()

    coord.last_success_utc = failure_time + timedelta(seconds=1)
    assert sensor.native_value == "none"

    # DNS failures should surface a dns_error code even without HTTP status
    dns_failure_time = failure_time + timedelta(minutes=5)
    coord.last_success_utc = failure_time  # ensure failure remains active
    coord.last_failure_utc = dns_failure_time
    coord.last_failure_status = None
    coord.last_failure_description = "Timeout while contacting DNS servers"
    coord.last_failure_response = None
    coord.last_failure_source = "network"
    assert sensor.native_value == "dns_error"

    # Generic network errors should surface network_error
    generic_failure_time = dns_failure_time + timedelta(minutes=1)
    coord.last_failure_utc = generic_failure_time
    coord.last_failure_description = "Connection reset by peer"
    assert sensor.native_value == "network_error"

    # Non-network failures without status fall back to none
    coord.last_failure_source = "other"
    assert sensor.native_value == "none"


def test_site_backoff_sensor_handles_none_and_datetime(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseSiteBackoffEndsSensor

    coord = _make_site_coord()
    sensor = EnphaseSiteBackoffEndsSensor(coord)

    assert sensor.native_value == "none"

    now = datetime.now(timezone.utc)
    backoff_until = now + timedelta(seconds=3665)
    coord.backoff_ends_utc = backoff_until
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)

    value = sensor.native_value
    assert value == "1h 1m 5s"
    attrs = sensor.extra_state_attributes
    assert attrs["backoff_ends_utc"] == backoff_until.isoformat()
    assert attrs["backoff_seconds"] == 3665

    # Once the backoff window has elapsed the sensor should reset to none
    monkeypatch.setattr(dt_util, "utcnow", lambda: backoff_until + timedelta(seconds=1))
    assert sensor.native_value == "none"
    # Exception when computing remaining time should fall back to none without attribute
    def _raise():
        raise RuntimeError("utc failure")

    monkeypatch.setattr(dt_util, "utcnow", _raise)
    assert sensor.native_value == "none"
    assert "backoff_seconds" not in sensor.extra_state_attributes


@pytest.mark.asyncio
async def test_site_backoff_sensor_counts_down_and_stops_timer(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev import sensor as sensor_mod

    coord = _make_site_coord()
    start = datetime(2025, 11, 3, 19, 12, 0, tzinfo=timezone.utc)
    coord.backoff_ends_utc = start + timedelta(seconds=3)
    monkeypatch.setattr(dt_util, "utcnow", lambda: start)

    callbacks: list = []

    def _fake_track(hass_obj, cb, interval):
        assert interval == timedelta(seconds=1)
        callbacks.append(cb)

        def _cancel():
            if cb in callbacks:
                callbacks.remove(cb)

        return _cancel

    monkeypatch.setattr(sensor_mod, "async_track_time_interval", _fake_track)

    async def _noop(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)

    sensor = sensor_mod.EnphaseSiteBackoffEndsSensor(coord)
    sensor.hass = hass

    recorded: list[str] = []
    sensor.async_write_ha_state = lambda: recorded.append(sensor.native_value)

    await sensor.async_added_to_hass()
    assert callbacks

    ticker = callbacks[0]

    assert sensor.native_value == "3s"

    monkeypatch.setattr(dt_util, "utcnow", lambda: start + timedelta(seconds=1))
    ticker(start + timedelta(seconds=1))
    monkeypatch.setattr(dt_util, "utcnow", lambda: start + timedelta(seconds=2))
    ticker(start + timedelta(seconds=2))
    monkeypatch.setattr(dt_util, "utcnow", lambda: start + timedelta(seconds=3))
    ticker(start + timedelta(seconds=3))

    assert recorded == ["2s", "1s", "none"]
    assert not callbacks


def test_site_last_update_sensor_reflects_success_timestamp():
    from custom_components.enphase_ev.sensor import EnphaseSiteLastUpdateSensor

    coord = _make_site_coord()
    sensor = EnphaseSiteLastUpdateSensor(coord)
    assert sensor.native_value is None

    success_time = datetime.now(timezone.utc)
    coord.last_success_utc = success_time
    assert sensor.native_value == success_time
