from types import SimpleNamespace


def test_device_info_uses_display_name_and_model():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity
    from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={
            "555555555555": {
                "display_name": "Garage Charger",
                "model_name": "IQ-EVSE-EU-3032",
                "model_id": "IQ-EVSE-EU-3032-01",
                "mac_address": "00-11-22-33-44-55",
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
    assert info["model_id"] == "IQ-EVSE-EU-3032-01"
    assert info["serial_number"] == "555555555555"
    assert info["connections"] == {(CONNECTION_NETWORK_MAC, "00:11:22:33:44:55")}


def test_device_info_falls_back_to_model_name():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={
            "1234": {
                "model_name": "IQ-EVSE-EU-3032",
                "hw_version": "2.1",
            }
        },
        site_id="7654321",
    )
    entity._sn = "1234"

    info = entity.device_info

    assert info["name"] == "IQ-EVSE-EU-3032"
    assert info["model"] == "IQ-EVSE-EU-3032"
    assert info["identifiers"] == {("enphase_ev", "1234")}


def test_device_info_defaults_when_metadata_missing():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={"321": {}},
        site_id="1001",
    )
    entity._sn = "321"

    info = entity.device_info

    assert info["name"] == "Enphase EV Charger"
    assert info.get("model") is None


def test_device_info_ignores_bad_mac_values():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity

    class BadMac:
        def __str__(self) -> str:
            raise ValueError("boom")

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={
            "321": {
                "display_name": "Garage Charger",
                "mac_address": BadMac(),
            }
        },
        site_id="1001",
    )
    entity._sn = "321"

    info = entity.device_info
    assert "connections" not in info


def test_device_info_uses_display_name_when_model_missing():
    from custom_components.enphase_ev.entity import EnphaseBaseEntity

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = SimpleNamespace(
        data={
            "999": {
                "display_name": "Driveway Charger",
            }
        },
        site_id="1002",
    )
    entity._sn = "999"

    info = entity.device_info

    assert info["name"] == "Driveway Charger"
    assert info["model"] == "Driveway Charger"
