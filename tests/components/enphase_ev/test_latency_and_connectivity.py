from datetime import datetime, timedelta, timezone

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


def test_site_backoff_sensor_handles_none_and_datetime():
    from custom_components.enphase_ev.sensor import EnphaseSiteBackoffEndsSensor

    coord = _make_site_coord()
    sensor = EnphaseSiteBackoffEndsSensor(coord)

    assert sensor.native_value == "none"

    backoff_until = datetime.now(timezone.utc)
    coord.backoff_ends_utc = backoff_until
    value = sensor.native_value
    assert value == backoff_until.isoformat()
    attrs = sensor.extra_state_attributes
    assert attrs["backoff_ends_utc"] == backoff_until.isoformat()


def test_site_last_update_sensor_reflects_success_timestamp():
    from custom_components.enphase_ev.sensor import EnphaseSiteLastUpdateSensor

    coord = _make_site_coord()
    sensor = EnphaseSiteLastUpdateSensor(coord)
    assert sensor.native_value is None

    success_time = datetime.now(timezone.utc)
    coord.last_success_utc = success_time
    assert sensor.native_value == success_time
