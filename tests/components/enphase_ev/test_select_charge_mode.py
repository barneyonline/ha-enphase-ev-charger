import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


@pytest.mark.asyncio
async def test_charge_mode_select(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.select import ChargeModeSelect

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)

    # preload coordinator state
    coord.data = {RANDOM_SERIAL: {"charge_mode": "SCHEDULED_CHARGING"}}

    class StubClient:
        async def set_charge_mode(self, sn: str, mode: str):
            return {"status": "accepted", "mode": mode}

    coord.client = StubClient()

    # Avoid exercising Debouncer / hass loop; stub refresh
    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore[attr-defined]

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert "Green" in sel.options
    assert sel.current_option == "Scheduled"

    await sel.async_select_option("Manual")
    # cache should update immediately
    assert coord._charge_mode_cache[RANDOM_SERIAL][0] == "MANUAL_CHARGING"


@pytest.mark.asyncio
async def test_async_setup_entry_falls_back_to_generic_listener_for_selects(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.select import async_setup_entry

    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    callbacks: list = []

    monkeypatch.setattr(coord, "async_add_topology_listener", None, raising=False)
    monkeypatch.setattr(
        coord,
        "async_add_listener",
        lambda callback: callbacks.append(callback) or (lambda: None),
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert callbacks


@pytest.mark.asyncio
async def test_charge_mode_select_scheduled_requires_enabled_schedule(
    hass, monkeypatch
) -> None:
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.select import ChargeModeSelect

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    coord.data = {RANDOM_SERIAL: {"charge_mode": "MANUAL_CHARGING"}}

    error_message = json.dumps(
        {
            "error": {
                "displayMessage": "No Schedules enabled for Scheduled Charging",
                "errorMessageCode": "iqevc_sch_10031",
            }
        }
    )

    class StubClient:
        async def set_charge_mode(self, sn: str, mode: str):
            raise aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url="https://example.test"),
                history=(),
                status=400,
                message=error_message,
            )

    coord.client = StubClient()

    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore[attr-defined]

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    with pytest.raises(
        HomeAssistantError, match="Enable at least one schedule before selecting"
    ):
        await sel.async_select_option("Scheduled")


@pytest.mark.asyncio
async def test_charge_mode_select_reraises_unknown_scheduler_error(
    hass, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.select import ChargeModeSelect

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    coord.data = {RANDOM_SERIAL: {"charge_mode": "MANUAL_CHARGING"}}

    error_message = json.dumps(
        {
            "error": {
                "displayMessage": "Invalid Input",
                "errorMessageCode": "some_other_code",
            }
        }
    )

    class StubClient:
        async def set_charge_mode(self, sn: str, mode: str):
            raise aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url="https://example.test"),
                history=(),
                status=400,
                message=error_message,
            )

    coord.client = StubClient()

    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore[attr-defined]

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    with pytest.raises(aiohttp.ClientResponseError):
        await sel.async_select_option("Scheduled")


def test_parse_scheduler_error_handles_invalid_payloads() -> None:
    from custom_components.enphase_ev.select import _parse_scheduler_error

    assert _parse_scheduler_error("") == (None, None)
    assert _parse_scheduler_error("not-json") == (None, None)
    assert _parse_scheduler_error(json.dumps(["bad"])) == (None, None)
    assert _parse_scheduler_error(json.dumps({"error": "bad"})) == (None, None)


def test_charge_mode_select_current_option_paths(coordinator_factory):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "GREEN_CHARGING"
    coord.data[RANDOM_SERIAL]["charge_mode"] = "MANUAL_CHARGING"

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert sel.current_option == "Green"

    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = ""
    coord.data[RANDOM_SERIAL]["charge_mode"] = "experimental_mode"
    assert sel.current_option is None

    coord.set_charge_mode_cache(RANDOM_SERIAL, "SCHEDULED_CHARGING")
    coord.data[RANDOM_SERIAL]["charge_mode"] = "CUSTOM"
    assert sel.current_option == "Scheduled"

    coord.data[RANDOM_SERIAL]["charge_mode"] = ""
    coord._storm_guard_cache_until = 10.0**12  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "GREEN", "enable": True}
    ]
    coord._charge_mode_cache.clear()  # noqa: SLF001
    assert sel.current_option == "Green"

    coord._storm_guard_cache_until = 0.0  # noqa: SLF001
    coord._charge_mode_cache.clear()  # noqa: SLF001
    assert sel.current_option is None

    coord.data[RANDOM_SERIAL]["schedule_type"] = "greencharging"
    assert sel.current_option == "Green"


def test_charge_mode_select_uses_smart_label_for_smart_mode(coordinator_factory):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "SMART_CHARGING"
    coord.data[RANDOM_SERIAL]["charge_mode"] = "SMART_CHARGING"

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    assert "Smart" in sel.options
    assert "Green" not in sel.options
    assert sel.current_option == "Smart"


@pytest.mark.asyncio
async def test_charge_mode_select_sets_smart_mode_for_single_evse_profile_context(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord._storm_guard_cache_until = 10.0**12  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": RANDOM_SERIAL, "chargeMode": "SMART", "enable": False}
    ]
    coord.client.set_charge_mode = AsyncMock(return_value={"status": "accepted"})
    coord.async_request_refresh = AsyncMock()

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert "Smart" in sel.options
    await sel.async_select_option("Smart")

    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "SMART_CHARGING"
    )


@pytest.mark.asyncio
async def test_charge_mode_select_keeps_green_for_other_evse_on_ai_site(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import ChargeModeSelect

    other_serial = "SERIAL-2"
    coord = coordinator_factory(serials=[RANDOM_SERIAL, other_serial])
    coord._battery_profile = "ai_optimisation"  # noqa: SLF001
    coord.client.set_charge_mode = AsyncMock(return_value={"status": "accepted"})
    coord.async_request_refresh = AsyncMock()

    sel = ChargeModeSelect(coord, other_serial)

    assert "Green" in sel.options
    assert "Smart" not in sel.options

    await sel.async_select_option("Green")

    coord.client.set_charge_mode.assert_awaited_once_with(
        other_serial, "GREEN_CHARGING"
    )


def test_charge_mode_select_helper_branches(coordinator_factory):
    from custom_components.enphase_ev.select import (
        ChargeModeSelect,
        _smart_charging_context,
    )

    coord = coordinator_factory()
    assert _smart_charging_context(coord) is False
    coord._battery_profile_charge_mode_preference = None  # noqa: SLF001
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False

    class BrokenData:
        def get(self, _sn, default=None):
            raise RuntimeError("boom")

    coord.data = BrokenData()
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False

    coord = coordinator_factory()

    class BadString:
        def __str__(self):
            raise ValueError("boom")

    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = BadString()
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False

    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "SMART_CHARGING"
    assert _smart_charging_context(coord, RANDOM_SERIAL) is True

    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = None
    coord._charge_mode_cache[RANDOM_SERIAL] = (BadString(), 1.0)  # noqa: SLF001
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False

    coord = coordinator_factory()
    coord._battery_profile = "ai_optimisation"  # noqa: SLF001
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False
    coord._battery_profile_charge_mode_preference = lambda _sn: (  # noqa: SLF001
        _ for _ in ()
    ).throw(RuntimeError("boom"))
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False

    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "MANUAL_CHARGING"
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert sel.current_option == "Manual"

    coord._resolve_charge_mode_pref = lambda _sn: "EXPERIMENTAL"  # noqa: SLF001
    assert sel.current_option is None


@pytest.mark.asyncio
async def test_charge_mode_select_unknown_option_falls_back_to_uppercase(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.client.set_charge_mode = AsyncMock(return_value={"status": "accepted"})
    coord.async_request_refresh = AsyncMock()
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    await sel.async_select_option("experimental")

    coord.client.set_charge_mode.assert_awaited_once_with(RANDOM_SERIAL, "EXPERIMENTAL")


def test_charge_mode_select_unavailable_when_scheduler_down(coordinator_factory):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord._scheduler_available = False  # noqa: SLF001
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert sel.available is False


def test_charge_mode_select_ignores_feature_flag_for_availability(coordinator_factory):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.data[RANDOM_SERIAL]["charge_mode_supported"] = False
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert sel.available is True


@pytest.mark.asyncio
async def test_charge_mode_select_blocks_when_scheduler_down(coordinator_factory):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord._scheduler_available = False  # noqa: SLF001
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    with pytest.raises(HomeAssistantError, match="scheduler service is down"):
        await sel.async_select_option("Manual")


@pytest.mark.asyncio
async def test_charge_mode_select_handles_scheduler_unavailable(
    coordinator_factory,
):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.enphase_ev.api import SchedulerUnavailable
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.client.set_charge_mode = AsyncMock(side_effect=SchedulerUnavailable("down"))
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    with pytest.raises(HomeAssistantError, match="scheduler service is down"):
        await sel.async_select_option("Manual")


@pytest.mark.asyncio
async def test_select_platform_async_setup_entry_filters_known_serials(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.select import (
        ChargeModeSelect,
        SystemProfileSelect,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=["1111"])
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    added: list[list[ChargeModeSelect]] = []
    listeners: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_listener(callback, *, context=None):
        listeners.append(callback)

        def _remove():
            listeners.remove(callback)

        return _remove

    coord.async_add_topology_listener = capture_listener  # type: ignore[attr-defined]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, capture_add)
    assert len(added) == 2
    assert isinstance(added[0][0], SystemProfileSelect)
    assert isinstance(added[1][0], ChargeModeSelect)
    assert added[1][0]._sn == "1111"
    assert len(listeners) == 1

    added.clear()
    listeners[0]()
    assert added == []

    coord._ensure_serial_tracked("2222")
    coord.data["2222"] = {"sn": "2222", "name": "Driveway"}
    listeners[0]()

    assert len(added) == 1
    assert {entity._sn for entity in added[0]} == {"2222"}


@pytest.mark.asyncio
async def test_select_platform_skips_system_profile_without_battery(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.select import ChargeModeSelect, async_setup_entry

    coord = coordinator_factory(serials=["1111"])
    coord._battery_has_encharge = False  # noqa: SLF001
    added: list[list[ChargeModeSelect]] = []
    listeners: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_listener(callback, *, context=None):
        listeners.append(callback)
        return lambda: None

    coord.async_add_topology_listener = capture_listener  # type: ignore[attr-defined]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, capture_add)

    assert len(added) == 1
    assert all(isinstance(entity, ChargeModeSelect) for entity in added[0])
    assert len(listeners) == 1


@pytest.mark.asyncio
async def test_select_platform_adds_system_profile_after_permission_refresh(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.select import (
        ChargeModeSelect,
        SystemProfileSelect,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=["1111"])
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    added: list[list[object]] = []
    topology_callbacks: list[object] = []
    update_callbacks: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_topology(callback, *, context=None):
        topology_callbacks.append(callback)
        return lambda: None

    def capture_update(callback, *, context=None):
        update_callbacks.append(callback)
        return lambda: None

    coord.async_add_topology_listener = capture_topology  # type: ignore[attr-defined]
    coord.async_add_listener = capture_update  # type: ignore[attr-defined]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, capture_add)

    assert len(added) == 1
    assert all(isinstance(entity, ChargeModeSelect) for entity in added[0])
    assert len(topology_callbacks) == 1
    assert len(update_callbacks) == 1

    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    update_callbacks[0]()

    assert len(added) == 2
    assert isinstance(added[1][0], SystemProfileSelect)


def test_system_profile_select_options_and_current(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_ai_opti_savings_mode = True  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001

    sel = SystemProfileSelect(coord)

    assert sel.options == [
        "Self-Consumption",
        "Savings",
        "AI Optimisation",
        "Full Backup",
    ]
    assert sel.current_option == "Self-Consumption"


def test_system_profile_select_unavailable_and_none_current(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord.last_update_success = False
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = None  # noqa: SLF001
    sel = SystemProfileSelect(coord)

    assert sel.available is False
    assert sel.current_option is None


def test_system_profile_select_unavailable_for_read_only_user(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    sel = SystemProfileSelect(coord)

    assert sel.available is False


def test_system_profile_select_unavailable_without_confirmed_write_access(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    sel = SystemProfileSelect(coord)

    assert sel.available is False


@pytest.mark.asyncio
async def test_system_profile_select_sets_profile(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_ai_opti_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.async_set_system_profile = AsyncMock()

    sel = SystemProfileSelect(coord)
    await sel.async_select_option("Savings")

    coord.async_set_system_profile.assert_awaited_once_with("cost_savings")

    await sel.async_select_option("AI Optimisation")
    coord.async_set_system_profile.assert_awaited_with("ai_optimisation")


@pytest.mark.asyncio
async def test_system_profile_select_rejects_unknown_option(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="not available"):
        await sel.async_select_option("Not A Mode")


@pytest.mark.asyncio
async def test_system_profile_select_surfaces_validation_error(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.async_set_system_profile = AsyncMock(
        side_effect=ServiceValidationError("Battery profile update was rejected.")
    )
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="rejected"):
        await sel.async_select_option("Savings")


@pytest.mark.asyncio
async def test_system_profile_select_translates_raw_http_forbidden(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.async_set_system_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="HTTP 403 Forbidden"):
        await sel.async_select_option("Savings")


@pytest.mark.asyncio
async def test_system_profile_select_translates_raw_http_unauthorized(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.async_set_system_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await sel.async_select_option("Savings")


@pytest.mark.asyncio
async def test_system_profile_select_translates_raw_http_other_error(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.async_set_system_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="Boom",
        )
    )
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="update failed"):
        await sel.async_select_option("Savings")


@pytest.mark.asyncio
async def test_system_profile_select_translates_raw_network_error(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.async_set_system_profile = AsyncMock(
        side_effect=aiohttp.ClientConnectionError("boom")
    )
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="network error"):
        await sel.async_select_option("Savings")


@pytest.mark.asyncio
async def test_system_profile_select_translates_timeout_error(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.async_set_system_profile = AsyncMock(side_effect=asyncio.TimeoutError())
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="timed out"):
        await sel.async_select_option("Savings")
