from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.enphase_ev.const import OPT_BATTERY_SCHEDULES_ENABLED
from custom_components.enphase_ev.number import (
    BatteryCfgScheduleLimitNumber,
    BatteryDischargeToGridScheduleLimitNumber,
    BatteryRestrictBatteryDischargeScheduleLimitNumber,
    BatteryShutdownLevelNumber,
    BatteryReserveNumber,
    ChargingAmpsNumber,
    async_setup_entry,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _enable_battery_schedule_limits(coord) -> None:
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord.client.battery_schedules = AsyncMock()
    coord.client.create_battery_schedule = AsyncMock()
    coord.client.update_battery_schedule = AsyncMock()
    coord.client.delete_battery_schedule = AsyncMock()
    capability = coord.battery_runtime._parse_battery_control_capability  # noqa: SLF001
    coord._battery_cfg_control = capability(  # noqa: SLF001
        {"show": True, "showDaySchedule": True, "scheduleSupported": True}
    )
    coord._battery_cfg_schedule_id = "sched-cfg"  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_dtg_control = capability(  # noqa: SLF001
        {"show": True, "showDaySchedule": True, "scheduleSupported": True}
    )
    coord._battery_dtg_schedule_id = "sched-dtg"  # noqa: SLF001
    coord._battery_dtg_begin_time = 60  # noqa: SLF001
    coord._battery_dtg_end_time = 120  # noqa: SLF001
    coord._battery_rbd_control = capability(  # noqa: SLF001
        {"show": True, "showDaySchedule": True, "scheduleSupported": True}
    )
    coord._battery_rbd_schedule_id = "sched-rbd"  # noqa: SLF001
    coord._battery_rbd_begin_time = 180  # noqa: SLF001
    coord._battery_rbd_end_time = 240  # noqa: SLF001


def test_evse_resolved_charge_mode_handles_data_access_failure() -> None:
    from custom_components.enphase_ev.entity import evse_resolved_charge_mode

    class _BoomCoord:
        @property
        def data(self):
            raise RuntimeError("boom")

    assert evse_resolved_charge_mode(_BoomCoord(), RANDOM_SERIAL) is None


def test_number_battery_write_access_confirmed_falls_back_to_false() -> None:
    from custom_components.enphase_ev import number as number_mod

    coord = SimpleNamespace(
        battery_write_access_confirmed=None,
        battery_user_is_owner=None,
        battery_user_is_installer=None,
    )

    assert number_mod._battery_write_access_confirmed(coord) is False


def test_cfg_schedule_edit_available_uses_schedule_id_and_public_times() -> None:
    from custom_components.enphase_ev import number as number_mod

    coord = SimpleNamespace(
        charge_from_grid_schedule_available=False,
        charge_from_grid_control_available=True,
        charge_from_grid_schedule_supported=True,
        battery_cfg_schedule_limit=None,
        _battery_cfg_schedule_id="sched-1",
        battery_charge_from_grid_start_time=1,
        battery_charge_from_grid_end_time=2,
        _battery_charge_begin_time=None,
        _battery_charge_end_time=None,
    )

    assert number_mod._cfg_schedule_edit_available(coord) is True

    coord.battery_charge_from_grid_start_time = None
    coord.battery_charge_from_grid_end_time = None
    coord._battery_charge_begin_time = 60
    coord._battery_charge_end_time = 120

    assert number_mod._cfg_schedule_edit_available(coord) is True

    coord._battery_cfg_schedule_id = None
    coord.battery_charge_from_grid_start_time = 1
    coord.battery_charge_from_grid_end_time = 2
    coord._battery_charge_begin_time = None
    coord._battery_charge_end_time = None

    assert number_mod._cfg_schedule_edit_available(coord) is False


def test_cfg_schedule_edit_available_rejects_when_control_unavailable() -> None:
    from custom_components.enphase_ev import number as number_mod

    coord = SimpleNamespace(
        charge_from_grid_schedule_available=False,
        charge_from_grid_control_available=False,
        charge_from_grid_schedule_supported=True,
        _battery_cfg_schedule_id="sched-1",
        battery_charge_from_grid_start_time=1,
        battery_charge_from_grid_end_time=2,
        _battery_charge_begin_time=60,
        _battery_charge_end_time=120,
    )

    assert number_mod._cfg_schedule_edit_available(coord) is False


def test_dtg_and_rbd_schedule_edit_available_cover_control_window_fallbacks() -> None:
    from custom_components.enphase_ev import number as number_mod

    dtg = SimpleNamespace(
        discharge_to_grid_schedule_available=False,
        discharge_to_grid_schedule_supported=True,
        battery_discharge_to_grid_start_time=None,
        battery_discharge_to_grid_end_time=None,
        _battery_dtg_begin_time=None,
        _battery_dtg_end_time=None,
        _battery_dtg_control_begin_time=60,
        _battery_dtg_control_end_time=120,
    )
    assert number_mod._dtg_schedule_edit_available(dtg) is True
    dtg.discharge_to_grid_schedule_supported = False
    assert number_mod._dtg_schedule_edit_available(dtg) is False

    rbd = SimpleNamespace(
        restrict_battery_discharge_schedule_available=False,
        restrict_battery_discharge_schedule_supported=True,
        battery_restrict_battery_discharge_start_time=None,
        battery_restrict_battery_discharge_end_time=None,
        _battery_rbd_begin_time=None,
        _battery_rbd_end_time=None,
        _battery_rbd_control_begin_time=60,
        _battery_rbd_control_end_time=120,
    )
    assert number_mod._rbd_schedule_edit_available(rbd) is True
    rbd._battery_rbd_control_end_time = None
    assert number_mod._rbd_schedule_edit_available(rbd) is False

    rbd.restrict_battery_discharge_schedule_supported = False
    assert number_mod._rbd_schedule_edit_available(rbd) is False

    rbd.restrict_battery_discharge_schedule_supported = True
    rbd.battery_restrict_battery_discharge_start_time = 1
    rbd.battery_restrict_battery_discharge_end_time = 2
    assert number_mod._rbd_schedule_edit_available(rbd) is True


def test_number_helper_fallbacks_and_retained_site_unique_ids() -> None:
    from custom_components.enphase_ev import number as number_mod

    entry = SimpleNamespace(options={OPT_BATTERY_SCHEDULES_ENABLED: True})
    coord = SimpleNamespace(
        site_id="site",
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "encharge"
        ),
        battery_write_access_confirmed=None,
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_reserve_editable=True,
        battery_shutdown_level_available=True,
        charge_from_grid_schedule_available=False,
        charge_from_grid_control_available=True,
        charge_from_grid_schedule_supported=True,
        _battery_cfg_schedule_id="sched-cfg",
        battery_charge_from_grid_start_time=1,
        battery_charge_from_grid_end_time=2,
        _battery_charge_begin_time=None,
        _battery_charge_end_time=None,
        discharge_to_grid_schedule_available=False,
        discharge_to_grid_schedule_supported=True,
        battery_discharge_to_grid_start_time=3,
        battery_discharge_to_grid_end_time=4,
        _battery_dtg_begin_time=None,
        _battery_dtg_end_time=None,
        _battery_dtg_control_begin_time=None,
        _battery_dtg_control_end_time=None,
        restrict_battery_discharge_schedule_available=False,
        restrict_battery_discharge_schedule_supported=True,
        battery_restrict_battery_discharge_start_time=5,
        battery_restrict_battery_discharge_end_time=6,
        _battery_rbd_begin_time=None,
        _battery_rbd_end_time=None,
        _battery_rbd_control_begin_time=None,
        _battery_rbd_control_end_time=None,
    )

    assert number_mod._type_available(coord, "encharge") is True
    assert number_mod._battery_write_access_confirmed(coord) is True
    assert number_mod._retained_site_number_unique_ids(coord, entry) == {
        "enphase_ev_site_site_battery_reserve",
        "enphase_ev_site_site_battery_shutdown_level",
        "enphase_ev_site_site_battery_cfg_schedule_limit",
        "enphase_ev_site_site_battery_dtg_schedule_limit",
        "enphase_ev_site_site_battery_rbd_schedule_limit",
    }

    coord.battery_write_access_confirmed = False
    assert number_mod._battery_write_access_confirmed(coord) is True
    assert number_mod._retained_site_number_unique_ids(coord, entry) == {
        "enphase_ev_site_site_battery_reserve",
        "enphase_ev_site_site_battery_shutdown_level",
        "enphase_ev_site_site_battery_cfg_schedule_limit",
        "enphase_ev_site_site_battery_dtg_schedule_limit",
        "enphase_ev_site_site_battery_rbd_schedule_limit",
    }

    coord.battery_user_is_owner = False
    coord.battery_user_is_installer = False
    assert number_mod._battery_write_access_confirmed(coord) is False
    assert number_mod._retained_site_number_unique_ids(coord, entry) == set()


def test_retained_site_number_unique_ids_hide_dedicated_schedule_limits_when_editor_active() -> (
    None
):
    from custom_components.enphase_ev import number as number_mod

    entry = SimpleNamespace(options={OPT_BATTERY_SCHEDULES_ENABLED: True})
    coord = SimpleNamespace(
        site_id="site",
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "encharge"
        ),
        client=SimpleNamespace(
            battery_schedules=lambda: None,
            create_battery_schedule=lambda: None,
            update_battery_schedule=lambda: None,
            delete_battery_schedule=lambda: None,
        ),
        battery_write_access_confirmed=None,
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_reserve_editable=True,
        battery_shutdown_level_available=True,
        charge_from_grid_schedule_available=False,
        charge_from_grid_control_available=True,
        charge_from_grid_schedule_supported=True,
        _battery_cfg_schedule_id="sched-cfg",
        battery_charge_from_grid_start_time=1,
        battery_charge_from_grid_end_time=2,
        _battery_charge_begin_time=None,
        _battery_charge_end_time=None,
        discharge_to_grid_schedule_available=False,
        discharge_to_grid_schedule_supported=True,
        battery_discharge_to_grid_start_time=3,
        battery_discharge_to_grid_end_time=4,
        _battery_dtg_begin_time=None,
        _battery_dtg_end_time=None,
        _battery_dtg_control_begin_time=None,
        _battery_dtg_control_end_time=None,
        restrict_battery_discharge_schedule_available=False,
        restrict_battery_discharge_schedule_supported=True,
        battery_restrict_battery_discharge_start_time=5,
        battery_restrict_battery_discharge_end_time=6,
        _battery_rbd_begin_time=None,
        _battery_rbd_end_time=None,
        _battery_rbd_control_begin_time=None,
        _battery_rbd_control_end_time=None,
    )

    assert number_mod._retained_site_number_unique_ids(coord, entry) == {
        "enphase_ev_site_site_battery_reserve",
        "enphase_ev_site_site_battery_shutdown_level",
        "enphase_ev_site_site_battery_schedule_edit_limit",
    }


def test_retained_site_number_unique_ids_keeps_shutdown_number_when_unavailable() -> (
    None
):
    from custom_components.enphase_ev import number as number_mod

    coord = SimpleNamespace(
        site_id="site",
        client=SimpleNamespace(),
        battery_write_access_confirmed=True,
        battery_reserve_editable=False,
        battery_shutdown_level_available=False,
        charge_from_grid_control_available=False,
        battery_charge_from_grid_start_time=None,
        battery_charge_from_grid_end_time=None,
        _battery_charge_begin_time=None,
        _battery_charge_end_time=None,
        _battery_cfg_control_begin_time=None,
        _battery_cfg_control_end_time=None,
        discharge_to_grid_schedule_available=False,
        discharge_to_grid_schedule_supported=False,
        battery_discharge_to_grid_start_time=None,
        battery_discharge_to_grid_end_time=None,
        _battery_dtg_begin_time=None,
        _battery_dtg_end_time=None,
        _battery_dtg_control_begin_time=None,
        _battery_dtg_control_end_time=None,
        restrict_battery_discharge_schedule_available=False,
        restrict_battery_discharge_schedule_supported=False,
        battery_restrict_battery_discharge_start_time=None,
        battery_restrict_battery_discharge_end_time=None,
        _battery_rbd_begin_time=None,
        _battery_rbd_end_time=None,
        _battery_rbd_control_begin_time=None,
        _battery_rbd_control_end_time=None,
    )
    setattr(coord, "_site_has_battery", True)
    coord._type_entries = {"encharge": [object()]}

    assert number_mod._retained_site_number_unique_ids(coord) == {
        "enphase_ev_site_site_battery_shutdown_level"
    }


def test_retained_site_number_unique_ids_hide_schedule_limits_when_scheduler_disabled() -> (
    None
):
    from custom_components.enphase_ev import number as number_mod

    coord = SimpleNamespace(
        site_id="site",
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "encharge"
        ),
        battery_write_access_confirmed=True,
        battery_reserve_editable=False,
        charge_from_grid_schedule_available=True,
        discharge_to_grid_schedule_available=True,
        restrict_battery_discharge_schedule_available=True,
    )

    assert number_mod._retained_site_number_unique_ids(
        coord, SimpleNamespace(options={OPT_BATTERY_SCHEDULES_ENABLED: False})
    ) == {
        "enphase_ev_site_site_battery_shutdown_level",
        "enphase_ev_site_site_battery_cfg_schedule_limit",
        "enphase_ev_site_site_battery_dtg_schedule_limit",
        "enphase_ev_site_site_battery_rbd_schedule_limit",
    }


@pytest.mark.asyncio
async def test_async_setup_entry_syncs_new_serials(hass, config_entry) -> None:
    coord = SimpleNamespace()
    object.__setattr__(config_entry, "options", {OPT_BATTERY_SCHEDULES_ENABLED: True})
    coord.site_id = "123456"
    coord.battery_write_access_confirmed = True
    coord.serials = {RANDOM_SERIAL}
    coord._serial_order = [RANDOM_SERIAL]
    coord.data = {RANDOM_SERIAL: {"name": "Garage EV"}}

    def iter_serials():
        yield from [RANDOM_SERIAL, "EV2", "", None, "EV2"]

    coord.iter_serials = iter_serials
    added = []

    def capture(entities, update_before_add=False):
        added.extend(entities)

    coord.async_add_listener = MagicMock(return_value=lambda: None)

    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, capture)

    assert coord.async_add_listener.called
    assert {ent._sn for ent in added if hasattr(ent, "_sn")} == {
        RANDOM_SERIAL,
        "EV2",
    }
    assert any(isinstance(ent, BatteryReserveNumber) for ent in added)
    assert any(isinstance(ent, BatteryShutdownLevelNumber) for ent in added)
    assert not any(isinstance(ent, BatteryCfgScheduleLimitNumber) for ent in added)
    assert not any(
        isinstance(ent, BatteryDischargeToGridScheduleLimitNumber) for ent in added
    )
    assert not any(
        isinstance(ent, BatteryRestrictBatteryDischargeScheduleLimitNumber)
        for ent in added
    )
    assert config_entry._on_unload


@pytest.mark.asyncio
async def test_async_setup_entry_does_not_duplicate_rbd_limit_when_schedule_becomes_editable(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    object.__setattr__(config_entry, "options", {OPT_BATTERY_SCHEDULES_ENABLED: True})
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_rbd_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {"show": True, "showDaySchedule": True, "scheduleSupported": True}
        )
    )
    coord._battery_rbd_schedule_id = None  # noqa: SLF001
    coord._battery_rbd_begin_time = None  # noqa: SLF001
    coord._battery_rbd_end_time = None  # noqa: SLF001
    callbacks: list = []
    added: list = []

    monkeypatch.setattr(
        coord,
        "async_add_listener",
        lambda callback: callbacks.append(callback) or (lambda: None),
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    initial_rbd_entities = [
        ent
        for ent in added
        if isinstance(ent, BatteryRestrictBatteryDischargeScheduleLimitNumber)
    ]
    assert len(initial_rbd_entities) == 0

    coord._battery_rbd_schedule_id = "sched-rbd"  # noqa: SLF001
    coord._battery_rbd_begin_time = 60  # noqa: SLF001
    coord._battery_rbd_end_time = 960  # noqa: SLF001

    callbacks[0]()

    later_rbd_entities = [
        ent
        for ent in added
        if isinstance(ent, BatteryRestrictBatteryDischargeScheduleLimitNumber)
    ]
    assert len(later_rbd_entities) == 0


@pytest.mark.asyncio
async def test_async_setup_entry_handles_no_serials(hass, config_entry) -> None:
    """No new serials should short-circuit without adding entities."""
    coord = SimpleNamespace()
    object.__setattr__(config_entry, "options", {OPT_BATTERY_SCHEDULES_ENABLED: True})
    coord.site_id = "123456"
    coord.battery_write_access_confirmed = True
    coord.serials = set()
    coord._serial_order = []
    coord.data = {}
    coord.iter_serials = lambda: []
    coord.async_add_listener = MagicMock(return_value=lambda: None)

    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert len(added) == 2
    assert any(isinstance(ent, BatteryReserveNumber) for ent in added)
    assert any(isinstance(ent, BatteryShutdownLevelNumber) for ent in added)
    assert not any(isinstance(ent, BatteryCfgScheduleLimitNumber) for ent in added)
    assert not any(
        isinstance(ent, BatteryDischargeToGridScheduleLimitNumber) for ent in added
    )
    assert not any(
        isinstance(ent, BatteryRestrictBatteryDischargeScheduleLimitNumber)
        for ent in added
    )


@pytest.mark.asyncio
async def test_async_setup_entry_adds_dedicated_dtg_rbd_limits_when_scheduler_disabled(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _enable_battery_schedule_limits(coord)
    object.__setattr__(config_entry, "options", {OPT_BATTERY_SCHEDULES_ENABLED: False})
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert any(isinstance(ent, BatteryCfgScheduleLimitNumber) for ent in added)
    assert any(
        isinstance(ent, BatteryDischargeToGridScheduleLimitNumber) for ent in added
    )
    assert any(
        isinstance(ent, BatteryRestrictBatteryDischargeScheduleLimitNumber)
        for ent in added
    )


@pytest.mark.asyncio
async def test_async_setup_entry_hides_cfg_limit_when_scheduler_editor_enabled(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _enable_battery_schedule_limits(coord)
    object.__setattr__(config_entry, "options", {OPT_BATTERY_SCHEDULES_ENABLED: True})
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert not any(isinstance(ent, BatteryCfgScheduleLimitNumber) for ent in added)
    assert not any(
        isinstance(ent, BatteryDischargeToGridScheduleLimitNumber) for ent in added
    )
    assert not any(
        isinstance(ent, BatteryRestrictBatteryDischargeScheduleLimitNumber)
        for ent in added
    )


@pytest.mark.asyncio
async def test_async_setup_entry_skips_site_battery_numbers_without_battery(
    hass, config_entry
) -> None:
    coord = SimpleNamespace()
    coord.site_id = "123456"
    coord.battery_write_access_confirmed = True
    coord.serials = {RANDOM_SERIAL}
    coord._serial_order = [RANDOM_SERIAL]
    coord.data = {RANDOM_SERIAL: {"name": "Garage EV"}}
    coord.battery_has_encharge = False
    coord.iter_serials = lambda: [RANDOM_SERIAL]
    coord.async_add_listener = MagicMock(return_value=lambda: None)

    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(ent, ChargingAmpsNumber) for ent in added)
    assert not any(isinstance(ent, BatteryReserveNumber) for ent in added)
    assert not any(isinstance(ent, BatteryShutdownLevelNumber) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_skips_site_numbers_without_battery_type(
    hass, config_entry
) -> None:
    coord = SimpleNamespace()
    coord.site_id = "123456"
    coord.battery_write_access_confirmed = True
    coord.serials = {RANDOM_SERIAL}
    coord._serial_order = [RANDOM_SERIAL]
    coord.data = {RANDOM_SERIAL: {"name": "Garage EV"}}
    coord.iter_serials = lambda: [RANDOM_SERIAL]
    coord.inventory_view.has_type_for_entities = (
        lambda type_key: str(type_key) != "encharge"
    )
    coord.async_add_listener = MagicMock(return_value=lambda: None)

    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(ent, ChargingAmpsNumber) for ent in added)
    assert not any(isinstance(ent, BatteryReserveNumber) for ent in added)
    assert not any(isinstance(ent, BatteryShutdownLevelNumber) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_stale_number_entities_when_inventory_ready(
    hass, config_entry, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = SimpleNamespace()
    coord.site_id = "123456"
    coord.battery_write_access_confirmed = True
    coord.battery_has_encharge = True
    coord._devices_inventory_ready = True
    coord.iter_serials = lambda: []
    coord.async_add_listener = MagicMock(return_value=lambda: None)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "number",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_battery_reserve",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    coord.battery_has_encharge = False
    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    remove_spy.assert_called_with(stale.entity_id)


def _make_coordinator(hass, config_entry, data):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    with patch(
        "custom_components.enphase_ev.coordinator.async_get_clientsession",
        return_value=None,
    ):
        coord = EnphaseCoordinator(hass, config_entry.data, config_entry=config_entry)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"name": "IQ Battery"}],
            },
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
            },
        },
        ["encharge", "iqevse"],
    )
    coord.data = data
    coord.last_set_amps = {}
    coord.async_request_refresh = AsyncMock()
    coord.set_last_set_amps = MagicMock(wraps=coord.set_last_set_amps)
    coord.client = SimpleNamespace()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    return coord


@pytest.mark.asyncio
async def test_async_setup_entry_skips_site_numbers_without_confirmed_write_access(
    hass, config_entry
) -> None:
    coord = SimpleNamespace()
    coord.site_id = "123456"
    coord.battery_write_access_confirmed = False
    coord.serials = {RANDOM_SERIAL}
    coord._serial_order = [RANDOM_SERIAL]
    coord.data = {RANDOM_SERIAL: {"name": "Garage EV"}}
    coord.iter_serials = lambda: [RANDOM_SERIAL]
    coord.async_add_listener = MagicMock(return_value=lambda: None)

    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(ent, ChargingAmpsNumber) for ent in added)
    assert not any(isinstance(ent, BatteryReserveNumber) for ent in added)
    assert not any(isinstance(ent, BatteryShutdownLevelNumber) for ent in added)
    assert not any(isinstance(ent, BatteryCfgScheduleLimitNumber) for ent in added)


def test_charging_number_converts_values(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "charging_level": "36",
                "min_amp": "6",
                "max_amp": "48",
            }
        },
    )
    coord.pick_start_amps = MagicMock(return_value=30)

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == 36.0
    assert number.native_min_value == 6.0
    assert number.native_max_value == 48.0
    assert number.native_step == 1.0


def test_charging_number_remains_available_when_feature_flag_disabled(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 32, "charging_amps_supported": False}},
    )

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.available is True


@pytest.mark.parametrize("mode", ["GREEN_CHARGING", "SMART_CHARGING"])
def test_charging_number_remains_available_when_amp_control_not_applicable(
    hass, config_entry, mode
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 32, "charge_mode_pref": mode}},
    )

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.available is True


@pytest.mark.parametrize("mode", ["GREEN_CHARGING", "SMART_CHARGING"])
def test_charging_number_uses_stored_setpoint_when_amp_control_not_applicable(
    hass, config_entry, mode
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "charging_level": 20,
                "charge_mode_pref": mode,
                "min_amp": 6,
                "max_amp": 40,
            }
        },
    )
    coord.last_set_amps[RANDOM_SERIAL] = 26

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)

    assert number.native_value == 26.0


def test_charging_number_fallbacks_to_pick_start(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "charging_level": None,
                "min_amp": "bad",
                "max_amp": "bad",
            }
        },
    )

    coord.pick_start_amps = MagicMock(return_value=28)

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == 28.0
    assert number.native_min_value == 6.0
    assert number.native_max_value == 40.0


def test_charging_number_invalid_level_uses_pick_start(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": "invalid", "min_amp": 6, "max_amp": 40}},
    )
    coord.pick_start_amps = MagicMock(return_value=26)

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == 26.0


def test_charging_number_safe_limit_overrides(hass, config_entry) -> None:
    from custom_components.enphase_ev.const import SAFE_LIMIT_AMPS

    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "charging_level": 32,
                "safe_limit_state": True,
                "charging": True,
                "min_amp": 6,
                "max_amp": 40,
            }
        },
    )
    coord.pick_start_amps = MagicMock(return_value=30)

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == float(SAFE_LIMIT_AMPS)

    coord.data[RANDOM_SERIAL]["charging"] = False
    assert number.native_value == 32.0

    coord.data[RANDOM_SERIAL]["charging"] = "false"
    assert number.native_value == 32.0

    coord.data[RANDOM_SERIAL]["safe_limit_state"] = 0
    assert number.native_value == 32.0


def test_charging_number_safe_limit_invalid_value_ignored(hass, config_entry) -> None:
    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 22, "safe_limit_state": BadStr()}},
    )

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == 22.0


def test_charging_number_charging_active_coercion() -> None:
    assert ChargingAmpsNumber._charging_active(None) is False
    assert ChargingAmpsNumber._charging_active(True) is True
    assert ChargingAmpsNumber._charging_active(1) is True
    assert ChargingAmpsNumber._charging_active(0) is False
    assert ChargingAmpsNumber._charging_active("true") is True
    assert ChargingAmpsNumber._charging_active("0") is False
    assert ChargingAmpsNumber._charging_active("unknown") is False
    assert ChargingAmpsNumber._charging_active(object()) is False


@pytest.mark.asyncio
async def test_charging_number_set_value_records_and_refreshes(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 32, "min_amp": 6, "max_amp": 40}},
    )

    coord.schedule_amp_restart = MagicMock()
    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    await number.async_set_native_value(24)

    coord.set_last_set_amps.assert_called_once_with(RANDOM_SERIAL, 24)
    coord.async_request_refresh.assert_awaited_once()
    coord.schedule_amp_restart.assert_not_called()


@pytest.mark.asyncio
async def test_charging_number_set_value_restarts_when_active(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "charging_level": 20,
                "min_amp": 6,
                "max_amp": 40,
                "charging": True,
            }
        },
    )

    coord.schedule_amp_restart = MagicMock()
    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    await number.async_set_native_value(26)

    coord.schedule_amp_restart.assert_called_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_charging_number_set_value_does_not_restart_in_green_or_smart_mode(
    hass, config_entry
) -> None:
    for mode in ("GREEN_CHARGING", "SMART_CHARGING"):
        coord = _make_coordinator(
            hass,
            config_entry,
            {
                RANDOM_SERIAL: {
                    "charging_level": 20,
                    "min_amp": 6,
                    "max_amp": 40,
                    "charging": True,
                    "charge_mode_pref": mode,
                }
            },
        )

        coord.schedule_amp_restart = MagicMock()
        number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
        await number.async_set_native_value(26)

        coord.set_last_set_amps.assert_called_once_with(RANDOM_SERIAL, 26)
        assert number.native_value == 26.0
        coord.schedule_amp_restart.assert_not_called()


def test_battery_reserve_number_dynamic_bounds(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 22}},
    )
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_backup_percentage = 24  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001

    number = BatteryReserveNumber(coord)

    assert number.available is True
    assert number.native_value == 24.0
    assert number.native_min_value == 5.0
    assert number.native_max_value == 100.0

    coord._battery_backup_percentage_min = 8  # noqa: SLF001
    coord._battery_backup_percentage_max = 92  # noqa: SLF001
    assert number.native_min_value == 8.0
    assert number.native_max_value == 92.0

    coord._battery_very_low_soc_min = 12  # noqa: SLF001
    assert number.native_min_value == 8.0


@pytest.mark.asyncio
async def test_battery_reserve_number_sets_value(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 22}},
    )
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord.async_set_battery_reserve = AsyncMock()

    number = BatteryReserveNumber(coord)
    await number.async_set_native_value(30)

    coord.async_set_battery_reserve.assert_awaited_once_with(30)


def test_battery_reserve_number_unavailable_in_full_backup(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 22}},
    )
    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_backup_percentage = 100  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001

    number = BatteryReserveNumber(coord)
    assert number.available is False


def test_battery_reserve_number_handles_super_unavailable_and_none(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": 22}},
    )
    coord.last_update_success = False
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_backup_percentage = None  # noqa: SLF001
    number = BatteryReserveNumber(coord)
    assert number.available is False
    assert number.native_value is None


def test_battery_shutdown_level_number_bounds_and_availability(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord._battery_very_low_soc_min = 10  # noqa: SLF001
    coord._battery_very_low_soc_max = 25  # noqa: SLF001

    number = BatteryShutdownLevelNumber(coord)
    assert number.available is True
    assert number.native_value == 15.0
    assert number.native_min_value == 10.0
    assert number.native_max_value == 25.0


@pytest.mark.asyncio
async def test_battery_shutdown_level_number_sets_value(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord._battery_very_low_soc_min = 10  # noqa: SLF001
    coord._battery_very_low_soc_max = 25  # noqa: SLF001
    coord.async_set_battery_shutdown_level = AsyncMock()

    number = BatteryShutdownLevelNumber(coord)
    await number.async_set_native_value(20)

    coord.async_set_battery_shutdown_level.assert_awaited_once_with(20)


def test_battery_shutdown_level_number_unavailable_when_not_supported(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_envoy_supports_vls = False  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord._battery_very_low_soc_min = 10  # noqa: SLF001
    coord._battery_very_low_soc_max = 25  # noqa: SLF001

    number = BatteryShutdownLevelNumber(coord)
    assert number.available is False


def test_battery_shutdown_level_number_super_unavailable_and_none_value(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord.last_update_success = False
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = None  # noqa: SLF001
    coord._battery_very_low_soc_min = 10  # noqa: SLF001
    coord._battery_very_low_soc_max = 25  # noqa: SLF001

    number = BatteryShutdownLevelNumber(coord)
    assert number.available is False
    assert number.native_value is None


def test_battery_cfg_schedule_limit_number_bounds_and_availability(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 80  # noqa: SLF001
    coord._battery_very_low_soc = 12  # noqa: SLF001
    coord._battery_cfg_schedule_id = "sched-1"  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001

    number = BatteryCfgScheduleLimitNumber(coord)

    assert number.available is True
    assert number.native_value == 80.0
    assert number.native_min_value == 12.0


def test_battery_cfg_schedule_limit_number_extra_state_attributes(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 80  # noqa: SLF001
    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001

    attrs = BatteryCfgScheduleLimitNumber(coord).extra_state_attributes

    assert attrs["start_time"] == "02:00"
    assert attrs["end_time"] == "05:00"
    assert attrs["schedule_status"] == "pending"
    assert attrs["schedule_pending"] is True
    assert attrs["schedule_enabled"] is True


@pytest.mark.asyncio
async def test_battery_cfg_schedule_limit_number_sets_value(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 80  # noqa: SLF001
    coord.async_set_cfg_schedule_limit = AsyncMock()

    number = BatteryCfgScheduleLimitNumber(coord)
    await number.async_set_native_value(90)

    coord.async_set_cfg_schedule_limit.assert_awaited_once_with(90)


def test_battery_cfg_schedule_limit_number_unavailable_without_schedule(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_cfg_schedule_limit = None  # noqa: SLF001

    number = BatteryCfgScheduleLimitNumber(coord)

    assert number.available is False
    assert number.native_value is None


def test_battery_cfg_schedule_limit_number_unavailable_without_force_support(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 80  # noqa: SLF001
    coord._battery_cfg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "forceScheduleSupported": False,
            }
        )
    )

    assert BatteryCfgScheduleLimitNumber(coord).available is True


@pytest.mark.asyncio
async def test_battery_cfg_schedule_limit_number_updates_existing_schedule_when_force_toggle_unavailable(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 80  # noqa: SLF001
    coord._battery_cfg_schedule_id = "sched-1"  # noqa: SLF001
    coord._battery_charge_begin_time = 180  # noqa: SLF001
    coord._battery_charge_end_time = 960  # noqa: SLF001
    coord._battery_cfg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "forceScheduleSupported": False,
            }
        )
    )
    coord.async_update_cfg_schedule = AsyncMock()

    number = BatteryCfgScheduleLimitNumber(coord)

    assert number.available is True
    await number.async_set_native_value(95)

    coord.async_update_cfg_schedule.assert_awaited_once_with(limit=95)


def test_battery_cfg_schedule_limit_number_super_unavailable_and_device_info_fallback(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord.last_update_success = False
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 80  # noqa: SLF001
    coord.inventory_view.type_device_info = lambda _type_key: None

    number = BatteryCfgScheduleLimitNumber(coord)

    assert number.available is False
    assert number.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }


def test_battery_cfg_schedule_limit_number_uses_type_device_info_when_available(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 80  # noqa: SLF001
    expected = {"identifiers": {("enphase_ev", "provided")}}
    coord.inventory_view.type_device_info = MagicMock(return_value=expected)

    number = BatteryCfgScheduleLimitNumber(coord)

    assert number.device_info is expected


def test_battery_reserve_and_shutdown_number_device_info_fallbacks(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord.inventory_view.type_device_info = lambda _type_key: None

    reserve = BatteryReserveNumber(coord)
    shutdown = BatteryShutdownLevelNumber(coord)

    assert reserve.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }
    assert shutdown.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }


def test_battery_reserve_and_shutdown_number_use_type_device_info(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    expected = {"identifiers": {("enphase_ev", "provided")}}
    coord.inventory_view.type_device_info = MagicMock(return_value=expected)

    assert BatteryReserveNumber(coord).device_info is expected
    assert BatteryShutdownLevelNumber(coord).device_info is expected


def test_battery_numbers_unavailable_without_confirmed_write_access(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 80  # noqa: SLF001

    assert BatteryReserveNumber(coord).available is False
    assert BatteryShutdownLevelNumber(coord).available is False
    assert BatteryCfgScheduleLimitNumber(coord).available is False


@pytest.mark.asyncio
async def test_base_battery_schedule_limit_number_fallbacks(hass, config_entry) -> None:
    from custom_components.enphase_ev import number as number_mod

    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord.async_custom_schedule_setter = AsyncMock()
    coord.inventory_view.type_device_info = lambda _type_key: None

    number = number_mod._BaseBatteryScheduleLimitNumber(
        coord,
        unique_suffix="custom_limit",
        limit_attr="custom_schedule_limit",
        availability_check=lambda _: True,
        setter_name="async_custom_schedule_setter",
    )

    assert number.native_value is None
    assert number.native_min_value == 0.0
    await number.async_set_native_value(42)
    coord.async_custom_schedule_setter.assert_awaited_once_with(42)
    assert number.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }

    expected = {"identifiers": {("enphase_ev", "provided")}}
    coord.inventory_view.type_device_info = MagicMock(return_value=expected)
    assert number.device_info is expected

    coord.last_update_success = False
    assert number.available is False


def test_base_battery_schedule_limit_number_extra_state_attributes_empty(
    hass, config_entry
) -> None:
    from custom_components.enphase_ev import number as number_mod

    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord.async_custom_schedule_setter = AsyncMock()

    number = number_mod._BaseBatteryScheduleLimitNumber(
        coord,
        unique_suffix="custom_limit",
        limit_attr="custom_schedule_limit",
        availability_check=lambda _: True,
        setter_name="async_custom_schedule_setter",
    )

    assert number._extra_schedule_state_attributes() == {}  # noqa: SLF001
    assert "schedule_status" not in number.extra_state_attributes


@pytest.mark.asyncio
async def test_async_setup_entry_number_prune_active_ids_include_charger_numbers(
    hass, config_entry, monkeypatch
) -> None:
    coord = SimpleNamespace()
    coord.site_id = "123456"
    coord.battery_write_access_confirmed = False
    coord._devices_inventory_ready = True
    coord.data = {RANDOM_SERIAL: {}}
    coord.iter_serials = lambda: [RANDOM_SERIAL]
    coord.async_add_listener = MagicMock(return_value=lambda: None)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    prune_spy = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.number.prune_managed_entities", prune_spy
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    active_unique_ids = prune_spy.call_args.kwargs["active_unique_ids"]
    assert f"enphase_ev_{RANDOM_SERIAL}_amps_number" in active_unique_ids


def test_dtg_schedule_limit_number_bounds_and_availability(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_dtg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_dtg_schedule_id = "sched-dtg"  # noqa: SLF001
    coord._battery_dtg_begin_time = 1080  # noqa: SLF001
    coord._battery_dtg_end_time = 1380  # noqa: SLF001
    coord._battery_dtg_schedule_limit = 25  # noqa: SLF001

    number = BatteryDischargeToGridScheduleLimitNumber(coord)

    assert number.available is True
    assert number.native_value == 25


def test_dtg_schedule_limit_number_extra_state_attributes(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_dtg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_dtg_begin_time = 1080  # noqa: SLF001
    coord._battery_dtg_end_time = 1380  # noqa: SLF001
    coord._battery_dtg_schedule_limit = 75  # noqa: SLF001
    coord._battery_dtg_schedule_enabled = True  # noqa: SLF001
    coord._battery_dtg_schedule_status = "pending"  # noqa: SLF001

    attrs = BatteryDischargeToGridScheduleLimitNumber(coord).extra_state_attributes

    assert attrs["start_time"] == "18:00"
    assert attrs["end_time"] == "23:00"
    assert attrs["schedule_status"] == "pending"
    assert attrs["schedule_pending"] is True
    assert attrs["schedule_enabled"] is True


def test_rbd_schedule_limit_number_extra_state_attributes(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_rbd_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_rbd_begin_time = 60  # noqa: SLF001
    coord._battery_rbd_end_time = 960  # noqa: SLF001
    coord._battery_rbd_schedule_limit = 100  # noqa: SLF001
    coord._battery_rbd_schedule_enabled = False  # noqa: SLF001
    coord._battery_rbd_schedule_status = "active"  # noqa: SLF001

    attrs = BatteryRestrictBatteryDischargeScheduleLimitNumber(
        coord
    ).extra_state_attributes

    assert attrs["start_time"] == "01:00"
    assert attrs["end_time"] == "16:00"
    assert attrs["schedule_status"] == "active"
    assert attrs["schedule_pending"] is False
    assert attrs["schedule_enabled"] is False


def test_dtg_schedule_limit_number_available_from_control_window_without_schedule_id(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord.battery_runtime.parse_battery_settings_payload(
        {
            "data": {
                "batteryGridMode": "ImportExport",
                "dtgControl": {
                    "show": True,
                    "showDaySchedule": True,
                    "scheduleSupported": True,
                    "enabled": False,
                    "locked": False,
                    "startTime": 1140,
                    "endTime": 1320,
                },
            }
        }
    )
    coord._battery_dtg_schedule_limit = 25  # noqa: SLF001

    number = BatteryDischargeToGridScheduleLimitNumber(coord)

    assert number.available is True
    assert number.native_value == 25


@pytest.mark.asyncio
async def test_dtg_schedule_limit_number_sets_value(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_dtg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_dtg_schedule_id = "sched-dtg"  # noqa: SLF001
    coord._battery_dtg_begin_time = 1080  # noqa: SLF001
    coord._battery_dtg_end_time = 1380  # noqa: SLF001
    coord._battery_dtg_schedule_limit = 25  # noqa: SLF001
    coord.async_set_discharge_to_grid_schedule_limit = AsyncMock()

    number = BatteryDischargeToGridScheduleLimitNumber(coord)
    await number.async_set_native_value(15)

    coord.async_set_discharge_to_grid_schedule_limit.assert_awaited_once_with(15)


def test_rbd_schedule_limit_number_bounds_and_availability(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_rbd_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_rbd_schedule_id = "sched-rbd"  # noqa: SLF001
    coord._battery_rbd_begin_time = 60  # noqa: SLF001
    coord._battery_rbd_end_time = 960  # noqa: SLF001
    coord._battery_rbd_schedule_limit = 100  # noqa: SLF001

    number = BatteryRestrictBatteryDischargeScheduleLimitNumber(coord)

    assert number.available is True
    assert number.native_value == 100


@pytest.mark.asyncio
async def test_rbd_schedule_limit_number_sets_value(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_rbd_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_rbd_schedule_id = "sched-rbd"  # noqa: SLF001
    coord._battery_rbd_begin_time = 60  # noqa: SLF001
    coord._battery_rbd_end_time = 960  # noqa: SLF001
    coord._battery_rbd_schedule_limit = 100  # noqa: SLF001
    coord.async_set_restrict_battery_discharge_schedule_limit = AsyncMock()

    number = BatteryRestrictBatteryDischargeScheduleLimitNumber(coord)
    await number.async_set_native_value(80)

    coord.async_set_restrict_battery_discharge_schedule_limit.assert_awaited_once_with(
        80
    )
