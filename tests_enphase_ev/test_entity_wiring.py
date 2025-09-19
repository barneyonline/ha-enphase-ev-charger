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
