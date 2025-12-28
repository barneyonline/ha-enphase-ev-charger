from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev import DOMAIN
from custom_components.enphase_ev.binary_sensor import (
    ChargingBinarySensor,
    ConnectedBinarySensor,
    PluggedInBinarySensor,
    SiteCloudReachableBinarySensor,
    async_setup_entry,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _stub_listener() -> Callable[[], None]:
    """Return a reusable unsubscribe stub for coordinator listeners."""
    return lambda: None


@pytest.mark.asyncio
async def test_async_setup_entry_syncs_binary_sensors(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    """Ensure charger binary sensors add once per serial and register unload."""
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": True,
                "charging": False,
                "faulted": False,
                "connected": True,
                "commissioned": True,
            }
        }
    )
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    callbacks: list[Callable[[], None]] = []

    def _capture_listener(callback: Callable[[], None]) -> Callable[[], None]:
        callbacks.append(callback)
        return _stub_listener()

    monkeypatch.setattr(coord, "async_add_listener", _capture_listener)

    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)

    assert len([ent for ent in added if isinstance(ent, SiteCloudReachableBinarySensor)]) == 1
    per_serial = [ent for ent in added if hasattr(ent, "_sn")]
    assert len(per_serial) == 3
    assert {type(ent) for ent in per_serial} == {
        PluggedInBinarySensor,
        ChargingBinarySensor,
        ConnectedBinarySensor,
    }
    assert config_entry._on_unload and callable(config_entry._on_unload[0])

    sync_cb = next(cb for cb in callbacks if cb.__name__ == "_async_sync_chargers")

    sync_cb()
    assert len(added) == 4

    new_serial = "EV0002"
    coord.data[new_serial] = {
        "sn": new_serial,
        "name": "Second EV",
        "plugged": False,
        "charging": True,
        "faulted": False,
        "connected": True,
        "commissioned": False,
    }
    coord._ensure_serial_tracked(new_serial)

    sync_cb()
    assert len(added) == 7
    assert {ent._sn for ent in added if hasattr(ent, "_sn")} == {RANDOM_SERIAL, new_serial}

    sync_cb()
    assert len(added) == 7


def test_ev_bool_sensors_reflect_coordinator_state(
    coordinator_factory, monkeypatch
) -> None:
    """Validate EV charger binary sensor helpers read from coordinator data."""
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": 0,
                "charging": 1,
                "faulted": True,
                "connected": False,
                "commissioned": True,
                "connection": " wifi ",
                "ip_address": " 192.0.2.10 ",
            }
        }
    )
    monkeypatch.setattr(coord, "async_add_listener", lambda callback: _stub_listener())

    plugged = PluggedInBinarySensor(coord, RANDOM_SERIAL)
    assert plugged.is_on is False

    charging = ChargingBinarySensor(coord, RANDOM_SERIAL)
    assert charging.icon == "mdi:flash"

    coord.data[RANDOM_SERIAL]["charging"] = 0
    assert charging.icon == "mdi:flash-off"

    connected = ConnectedBinarySensor(coord, RANDOM_SERIAL)
    assert connected.device_class == BinarySensorDeviceClass.CONNECTIVITY
    assert connected.entity_category == EntityCategory.DIAGNOSTIC
    attrs = connected.extra_state_attributes
    assert attrs["connection"] == "wifi"
    assert attrs["ip_address"] == "192.0.2.10"


def test_site_cloud_reachable_binary_sensor_metadata(
    coordinator_factory, monkeypatch
) -> None:
    """Exercise availability, attributes, and device info for the site sensor."""
    coord = coordinator_factory(serials=[], data={})
    monkeypatch.setattr(coord, "async_add_listener", lambda callback: _stub_listener())

    sensor = SiteCloudReachableBinarySensor(coord)
    assert sensor.name == "Cloud Reachable"

    coord.last_success_utc = None
    coord.last_update_success = False
    assert sensor.available is False

    now = datetime.now(timezone.utc)
    coord.last_update_success = True
    coord.last_success_utc = now - timedelta(seconds=45)
    coord.update_interval = None
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    assert sensor.available is True
    assert sensor.is_on is True

    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(seconds=61))
    assert sensor.is_on is False

    failure_time = now - timedelta(seconds=15)
    coord.last_success_utc = now
    coord.last_failure_utc = failure_time
    coord.last_failure_status = 503
    coord.last_failure_description = "Gateway error"
    coord.last_failure_response = {"retry": True}
    coord.last_failure_source = "http"
    coord.backoff_ends_utc = now + timedelta(seconds=90)

    attrs = sensor.extra_state_attributes
    assert "last_success_utc" not in attrs
    assert attrs["last_failure_utc"] == failure_time.isoformat()
    assert attrs["last_failure_status"] == 503
    assert attrs["code_description"] == "Gateway error"
    assert attrs["last_failure_response"] == {"retry": True}
    assert attrs["last_failure_source"] == "http"
    assert attrs["backoff_ends_utc"] == coord.backoff_ends_utc.isoformat()

    info = sensor.device_info
    assert info["identifiers"] == {(DOMAIN, f"site:{coord.site_id}")}
    assert info["manufacturer"] == "Enphase"
    assert info["model"] == "Enlighten Cloud"
    assert info["name"] == f"Enphase Site {coord.site_id}"
