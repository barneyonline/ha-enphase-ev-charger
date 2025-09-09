def test_entity_naming_and_availability():
    from custom_components.enphase_ev.sensor import EnphaseSessionEnergySensor
    
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
            "name": "Garage EV",
            "connected": True,
            "plugged": True,
            "charging": False,
            "faulted": False,
            "connector_status": "AVAILABLE",
            "session_kwh": 0.0,
            "session_start": None,
        }
    }

    ent = EnphaseSessionEnergySensor(coord, "482522020944")
    assert ent.available is True
    # Uses has_entity_name; entity name is the suffix only
    assert ent.name == "Session Energy"
    # Device name comes from coordinator data
    assert ent.device_info["name"] == "Garage EV"
    # Unique ID includes domain, serial, and key
    assert ent.unique_id.endswith("482522020944_session_kwh")
