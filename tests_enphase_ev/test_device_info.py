from types import SimpleNamespace


def test_device_info_uses_display_name_and_model():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={
            "555555555555": {
                "display_name": "Garage Charger",
                "model_name": "IQ-EVSE-EU-3032",
                "hw_version": "2.0",
                "sw_version": "3.1",
            }
        },
        site_id="1234567",
    )
    entity._sn = "555555555555"

    info = entity.device_info

    assert info["name"] == "Garage Charger"
    assert info["model"] == "Garage Charger (IQ-EVSE-EU-3032)"
    assert info.get("default_model") == "IQ-EVSE-EU-3032"
    assert info["serial_number"] == "555555555555"
