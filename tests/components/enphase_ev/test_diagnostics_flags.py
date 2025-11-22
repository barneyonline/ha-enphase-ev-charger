from homeassistant.helpers.entity import EntityCategory


def test_diagnostic_entity_categories():
    from custom_components.enphase_ev.binary_sensor import ConnectedBinarySensor
    from custom_components.enphase_ev.sensor import (
        EnphaseConnectorStatusSensor,
        EnphaseStatusSensor,
    )

    class Dummy:
        data = {}

    s1 = EnphaseConnectorStatusSensor(Dummy(), "sn")
    s2 = EnphaseStatusSensor(Dummy(), "sn")
    b1 = ConnectedBinarySensor(Dummy(), "sn")

    assert s1.entity_category is None
    assert s2.entity_category is EntityCategory.DIAGNOSTIC
    assert b1.entity_category is EntityCategory.DIAGNOSTIC
