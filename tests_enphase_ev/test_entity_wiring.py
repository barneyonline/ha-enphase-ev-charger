def test_entity_naming_and_availability():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {"555555555555"}
            self.site_id = "1234567"
            self.last_update_success = True

    coord = DummyCoord()
    coord.data = {
        "555555555555": {
            "sn": "555555555555",
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

    ent = EnphaseEnergyTodaySensor(coord, "555555555555")
    assert ent.available is True
    # Uses has_entity_name; entity name is the suffix only
    assert ent.name == "Energy Today"
    # Device name comes from coordinator data
    assert ent.device_info["name"] == "Garage EV"
    # Unique ID includes domain, serial, and key
    assert ent.unique_id.endswith("555555555555_energy_today")


def test_device_info_includes_model_name_when_available():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {"482522020944"}
            self.site_id = "3381244"
            self.last_update_success = True

    coord = DummyCoord()
    coord.data = {
        "482522020944": {
            "sn": "482522020944",
            "display_name": "IQ EV Charger",
            "model_name": "IQ-EVSE-EU-3032",
            "connected": True,
        }
    }

    ent = EnphaseEnergyTodaySensor(coord, "482522020944")
    info = ent.device_info
    assert info["name"] == "IQ EV Charger (IQ-EVSE-EU-3032)"
    assert info["model"] == "IQ-EVSE-EU-3032"
