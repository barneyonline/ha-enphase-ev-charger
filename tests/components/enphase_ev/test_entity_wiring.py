from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SERIAL_ALT, RANDOM_SITE_ID


def test_entity_naming_and_availability():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {RANDOM_SERIAL}
            self.site_id = RANDOM_SITE_ID
            self.last_update_success = True

    coord = DummyCoord()
    coord.data = {
        RANDOM_SERIAL: {
            "sn": RANDOM_SERIAL,
            "name": "Garage EV",
            "connected": True,
            "plugged": True,
            "charging": False,
            "faulted": False,
            "connector_status": "AVAILABLE",
            "lifetime_kwh": 0.0,
            "session_start": None,
        }
    }

    ent = EnphaseEnergyTodaySensor(coord, RANDOM_SERIAL)
    assert ent.available is True
    # Uses has_entity_name; entity name is the suffix only
    assert ent.name == "Last Session"
    # Device name comes from coordinator data
    assert ent.device_info["name"] == "Garage EV"
    # Unique ID includes domain, serial, and key
    assert ent.unique_id.endswith(f"{RANDOM_SERIAL}_energy_today")


def test_device_info_includes_model_name_when_available():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {RANDOM_SERIAL_ALT}
            self.site_id = RANDOM_SITE_ID
            self.last_update_success = True

    coord = DummyCoord()
    coord.data = {
        RANDOM_SERIAL_ALT: {
            "sn": RANDOM_SERIAL_ALT,
            "display_name": "IQ EV Charger",
            "model_name": "IQ-EVSE-EU-3032",
            "connected": True,
        }
    }

    ent = EnphaseEnergyTodaySensor(coord, RANDOM_SERIAL_ALT)
    info = ent.device_info
    assert info["name"] == "IQ EV Charger"
    assert info["model"] == "IQ EV Charger (IQ-EVSE-EU-3032)"
