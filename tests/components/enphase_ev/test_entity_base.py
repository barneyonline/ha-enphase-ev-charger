from __future__ import annotations

import logging

from custom_components.enphase_ev.entity import EnphaseBaseEntity
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


class DummyEntity(EnphaseBaseEntity):
    """Concrete subclass for exercising base entity logic."""


def test_base_entity_data_falls_back_to_coordinator(coordinator_factory):
    coord = coordinator_factory()
    entity = DummyEntity(coord, RANDOM_SERIAL)
    delattr(entity, "_data")
    coord.data[RANDOM_SERIAL]["hw_version"] = "1.2"

    assert entity.data["hw_version"] == "1.2"


def test_base_entity_data_handles_missing_coord():
    class RaisesAttributeError:
        @property
        def data(self):
            raise AttributeError("boom")

    entity = object.__new__(EnphaseBaseEntity)
    entity._coord = RaisesAttributeError()  # type: ignore[attr-defined]
    entity._sn = "nope"  # type: ignore[attr-defined]

    assert entity.data == {}


def test_base_entity_logs_transitions(coordinator_factory, caplog):
    coord = coordinator_factory(data={RANDOM_SERIAL: {"sn": RANDOM_SERIAL, "name": "Garage"}})
    entity = DummyEntity(coord, RANDOM_SERIAL)
    entity.async_write_ha_state = lambda *args, **kwargs: None  # type: ignore[attr-defined]

    caplog.set_level(logging.INFO)
    entity._unavailable_logged = True  # type: ignore[attr-defined]
    coord.data = {RANDOM_SERIAL: {"sn": RANDOM_SERIAL}}
    entity._handle_coordinator_update()
    assert "data available again" in caplog.text
    assert entity._unavailable_logged is False  # type: ignore[attr-defined]

    caplog.clear()
    entity._ever_had_data = True  # type: ignore[attr-defined]
    entity._unavailable_logged = False  # type: ignore[attr-defined]
    entity._has_data = True  # type: ignore[attr-defined]
    coord.data = {}
    coord._last_error = "timeout"  # type: ignore[attr-defined]
    entity._handle_coordinator_update()
    assert "data unavailable (timeout)" in caplog.text
    assert entity._unavailable_logged is True  # type: ignore[attr-defined]

    caplog.clear()
    entity._unavailable_logged = False  # type: ignore[attr-defined]
    entity._has_data = True  # type: ignore[attr-defined]
    coord._last_error = None  # type: ignore[attr-defined]
    entity._handle_coordinator_update()
    assert "data unavailable" in caplog.text
    assert entity._unavailable_logged is True  # type: ignore[attr-defined]
