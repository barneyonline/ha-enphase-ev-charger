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
    coord.site_id = "123456"
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
    callbacks: list[Any] = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_listener = fake_add_listener  # type: ignore[assignment]
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added = []

    def _async_add_entities(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _async_add_entities)
    assert any(ent.unique_id.endswith("_energy_today") for ent in added)
    assert any(ent.unique_id.endswith("_last_rpt") for ent in added)
    assert any(ent.unique_id.endswith("_electrical_phase") for ent in added)
    assert any(ent.unique_id.endswith("_charger_authentication") for ent in added)
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 11

    sync_chargers_cb = next(cb for cb in callbacks if cb.__name__ == "_async_sync_chargers")
    sync_chargers_cb()
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 11

    new_sn = "NEWSN123"
    coord.data[new_sn] = dict(coord.data[RANDOM_SERIAL], sn=new_sn)
    coord.serials.add(new_sn)
    sync_chargers_cb()
    assert len({ent._sn for ent in added if hasattr(ent, "_sn")}) == 2


@pytest.mark.asyncio
async def test_async_setup_entry_skips_battery_entities_without_battery(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryModeSensor,
        EnphaseStormAlertSensor,
        EnphaseStormGuardStateSensor,
        EnphaseSystemProfileStatusSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_has_encharge = False  # noqa: SLF001
    coord.data[RANDOM_SERIAL].update(
        {
            "connector_status": "AVAILABLE",
            "charge_mode": "IMMEDIATE",
            "plugged": True,
            "charging_level": 32,
        }
    )
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added: list = []

    def _async_add_entities(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _async_add_entities)

    assert not any(isinstance(ent, EnphaseStormAlertSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseBatteryModeSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseSystemProfileStatusSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseStormGuardStateSensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_site_energy_entities(
    hass, config_entry, coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.energy.site_energy = {
        "grid_import": SimpleNamespace(
            value_kwh=1.0,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=None,
            update_pending=False,
            source_unit="Wh",
        )
    }

    callbacks: list = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_listener = fake_add_listener  # type: ignore[assignment]
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added: list = []

    def _async_add_entities(entities, update_before_add=False):
        added.extend(entities)

    created: list = []

    class StubSiteEnergy(sensor_mod.EnphaseSiteEnergySensor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(sensor_mod, "EnphaseSiteEnergySensor", StubSiteEnergy)

    await async_setup_entry(hass, config_entry, _async_add_entities)
    for cb in callbacks:
        cb()
    assert created, "Expected site energy sensor to be created"
    assert any(ent._flow_key == "consumption" for ent in created)
    assert any(ent.translation_key == "site_consumption" for ent in created)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_type_inventory_sensors(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import (
        EnphaseTypeInventorySensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "wind_turbine": {
                "type_key": "wind_turbine",
                "type_label": "Wind Turbine",
                "count": 2,
                "devices": [{"name": "Wind 1"}, {"name": "Wind 2"}],
            },
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"name": "Battery 1"}],
            },
        },
        ["wind_turbine", "encharge"],
    )
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    type_entities = [ent for ent in added if isinstance(ent, EnphaseTypeInventorySensor)]
    assert len(type_entities) == 2
    wind = next(ent for ent in type_entities if ent._type_key == "wind_turbine")  # noqa: SLF001
    assert wind.native_value == 2
    assert wind.extra_state_attributes["type_label"] == "Wind Turbine"
    assert wind.device_info["name"] == "Wind Turbine (2)"


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
    assert attrs["session_id"] is None
    assert attrs["session_started_at"] is None
    assert attrs["session_ended_at"] is None
    assert attrs["active_charge_time_s"] is None
    assert attrs["avg_cost_per_kwh"] is None
    assert attrs["cost_calculated"] is None
    assert attrs["session_cost_state"] is None
    assert attrs["manual_override"] is None
    assert attrs["charge_profile_stack_level"] is None


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
