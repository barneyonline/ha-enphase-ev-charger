from datetime import timedelta


def test_cloud_latency_sensor_value():
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseCloudLatencySensor

    # Minimal coordinator stub
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.site_id = "3381244"
    coord.latency_ms = 123
    coord.update_interval = timedelta(seconds=15)

    s = EnphaseCloudLatencySensor(coord)
    assert s.native_value == 123


def test_site_cloud_reachable_binary_sensor_states():
    from datetime import datetime, timezone

    from custom_components.enphase_ev.binary_sensor import SiteCloudReachableBinarySensor
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.site_id = "3381244"
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
