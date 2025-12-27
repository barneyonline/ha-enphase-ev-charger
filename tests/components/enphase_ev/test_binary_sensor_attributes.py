from types import SimpleNamespace

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.entity import EntityCategory

from custom_components.enphase_ev.binary_sensor import ConnectedBinarySensor
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _dummy_coord(payload: dict) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.data = {RANDOM_SERIAL: payload}
    coord.serials = {RANDOM_SERIAL}
    coord.site_id = "site"
    coord.iter_serials = lambda: coord.serials
    coord.async_add_listener = lambda cb, context=None: (lambda: None)
    return coord


def test_connected_binary_sensor_attributes_and_defaults():
    payload = {
        "sn": RANDOM_SERIAL,
        "name": "Garage EV",
        "connected": True,
        "connection": " ethernet ",
        "ip_address": " 192.0.2.10 ",
    }
    sensor = ConnectedBinarySensor(_dummy_coord(payload), RANDOM_SERIAL)
    assert sensor.device_class == BinarySensorDeviceClass.CONNECTIVITY
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC

    attrs = sensor.extra_state_attributes
    assert attrs["connection"] == "ethernet"
    assert attrs["ip_address"] == "192.0.2.10"

    payload["connection"] = None
    payload["ip_address"] = ""
    attrs_blank = sensor.extra_state_attributes
    assert attrs_blank["connection"] is None
    assert attrs_blank["ip_address"] is None
