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
        "phase_mode": "3",
        "dlb_enabled": "true",
    }
    sensor = ConnectedBinarySensor(_dummy_coord(payload), RANDOM_SERIAL)
    assert sensor.device_class == BinarySensorDeviceClass.CONNECTIVITY
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC

    attrs = sensor.extra_state_attributes
    assert attrs["connection"] == "ethernet"
    assert attrs["ip_address"] == "192.0.2.10"
    assert attrs["phase_mode"] == "Three Phase"
    assert attrs["phase_mode_raw"] == "3"
    assert attrs["dlb_enabled"] is True
    assert attrs["dlb_status"] == "enabled"

    payload["connection"] = None
    payload["ip_address"] = ""
    payload["phase_mode"] = ""
    payload["dlb_enabled"] = None
    attrs_blank = sensor.extra_state_attributes
    assert attrs_blank["connection"] is None
    assert attrs_blank["ip_address"] is None
    assert attrs_blank["phase_mode"] is None
    assert attrs_blank["phase_mode_raw"] == ""
    assert attrs_blank["dlb_enabled"] is None
    assert attrs_blank["dlb_status"] is None


def test_connected_binary_sensor_phase_mode_edge_cases():
    coord = _dummy_coord({"phase_mode": None})
    sensor = ConnectedBinarySensor(coord, RANDOM_SERIAL)

    # None maps to empty friendly values
    assert sensor._friendly_phase_mode() == (None, None)

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord.data[RANDOM_SERIAL]["phase_mode"] = BadStr()
    friendly, raw = sensor._friendly_phase_mode()
    assert friendly is None
    assert isinstance(raw, BadStr)

    coord.data[RANDOM_SERIAL]["phase_mode"] = "bad"
    friendly, raw = sensor._friendly_phase_mode()
    assert friendly == "bad"
    assert raw == "bad"

    coord.data[RANDOM_SERIAL]["phase_mode"] = "1"
    friendly, raw = sensor._friendly_phase_mode()
    assert friendly == "Single Phase"
    assert raw == "1"


def test_connected_binary_sensor_dlb_bool_errors():
    class BadBool:
        def __bool__(self):
            raise RuntimeError("cannot bool")

    coord = _dummy_coord(
        {
            "connected": True,
            "connection": "wifi",
            "ip_address": "198.51.100.10",
            "phase_mode": "3",
            "dlb_enabled": BadBool(),
        }
    )
    sensor = ConnectedBinarySensor(coord, RANDOM_SERIAL)

    attrs = sensor.extra_state_attributes
    assert attrs["dlb_enabled"] is None
    assert attrs["dlb_status"] is None
