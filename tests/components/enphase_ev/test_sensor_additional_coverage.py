"""Additional coverage for Enphase EV sensor helpers."""

from __future__ import annotations

from datetime import timezone
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.enphase_ev import sensor as sensor_mod

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

pytest.importorskip("homeassistant")


def _mk_coord(sn: str, payload: dict[str, Any]) -> Any:
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = SimpleNamespace()
    coord.hass.config = SimpleNamespace(units=SimpleNamespace(length_unit="mi"))
    coord.data = {sn: payload}
    coord.serials = {sn}
    coord.last_set_amps = {}
    coord.site_id = "site-test"
    coord._serial_order = [sn]
    coord.iter_serials = lambda: list(coord.serials)

    def _default_listener(callback, context=None):
        return lambda: None

    coord.async_add_listener = _default_listener  # type: ignore[assignment]
    coord.last_success_utc = None
    coord.last_failure_utc = None
    coord.last_failure_status = None
    coord.last_failure_description = None
    coord.last_failure_source = None
    coord.last_failure_response = None
    coord.backoff_ends_utc = None
    coord.latency_ms = None
    coord.last_update_success = True
    return coord


@pytest.mark.asyncio
async def test_async_setup_entry_registers_entities(
    hass, config_entry, coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.data[RANDOM_SERIAL].update(
        {
            "connector_status": "AVAILABLE",
            "charge_mode": "IMMEDIATE",
            "plugged": True,
            "charging_level": 32,
        }
    )
    callbacks: dict[str, Any] = {}

    def fake_add_listener(cb):
        callbacks["cb"] = cb
        return lambda: None

    coord.async_add_listener = fake_add_listener  # type: ignore[assignment]
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added = []

    def _async_add_entities(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _async_add_entities)
    assert any(ent.unique_id.endswith("_energy_today") for ent in added)
    assert any(ent.unique_id.endswith("_last_rpt") for ent in added)
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 8

    callbacks["cb"]()
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 8

    new_sn = "NEWSN123"
    coord.data[new_sn] = dict(coord.data[RANDOM_SERIAL], sn=new_sn)
    coord.serials.add(new_sn)
    callbacks["cb"]()
    assert len({ent._sn for ent in added if hasattr(ent, "_sn")}) == 2


def test_session_metadata_attributes_handle_blanks():
    attrs = sensor_mod.EnphaseEnergyTodaySensor._session_metadata_attributes(
        {},
        hass=None,
        context={},
        energy_kwh=None,
        energy_wh=None,
        duration_min=None,
        session_key=None,
    )
    assert attrs["energy_consumed_kwh"] is None
    assert attrs["energy_consumed_wh"] is None
    assert attrs["range_added"] is None
    assert attrs["session_cost"] is None
    assert attrs["session_duration_min"] is None


def test_session_metadata_attributes_formats_fields(monkeypatch):
    from homeassistant.const import UnitOfLength
    from homeassistant.util import dt as dt_util

    monkeypatch.setattr(
        dt_util, "as_local", lambda dt: dt.replace(tzinfo=timezone.utc)  # type: ignore[override]
    )
    payload = {
        "session_plug_in_at": 0,
        "session_plug_out_at": "2025-11-01T05:00:00Z",
        "session_miles": 10,
        "session_cost": "2.50",
        "session_charge_level": "24",
    }
    attrs = sensor_mod.EnphaseEnergyTodaySensor._session_metadata_attributes(
        payload,
        hass=SimpleNamespace(config=SimpleNamespace(units=SimpleNamespace(length_unit=UnitOfLength.KILOMETERS))),
        context={"energy_kwh": 1.5, "energy_wh": 1500.0, "session_charge_level": "20"},
        energy_kwh=1.5,
        energy_wh=1500.0,
        duration_min=90,
        session_key="abc",
    )
    assert attrs["plugged_in_at"].startswith("1970-01-01T00:00:00")
    assert attrs["plugged_out_at"] == "2025-11-01T05:00:00+00:00"
    assert attrs["energy_consumed_kwh"] == 1.5
    assert attrs["energy_consumed_wh"] == 1500.0
    assert attrs["session_cost"] == pytest.approx(2.5)
    assert attrs["session_charge_level"] == 20
    assert attrs["session_duration_min"] == 90
