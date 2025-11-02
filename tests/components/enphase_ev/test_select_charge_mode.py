import pytest

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


def test_charge_mode_select_current_option_paths(coordinator_factory):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "GREEN_CHARGING"
    coord.data[RANDOM_SERIAL]["charge_mode"] = "MANUAL_CHARGING"

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert sel.current_option == "Green"

    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = ""
    coord.data[RANDOM_SERIAL]["charge_mode"] = "experimental_mode"
    assert sel.current_option == "Experimental_Mode"

    coord.data[RANDOM_SERIAL]["charge_mode"] = ""
    assert sel.current_option is None


@pytest.mark.asyncio
async def test_select_platform_async_setup_entry_filters_known_serials(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.select import ChargeModeSelect, async_setup_entry

    coord = coordinator_factory(serials=["1111"])
    added: list[list[ChargeModeSelect]] = []
    listeners: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_listener(callback, *, context=None):
        listeners.append(callback)

        def _remove():
            listeners.remove(callback)

        return _remove

    coord.async_add_listener = capture_listener  # type: ignore[attr-defined]
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    await async_setup_entry(hass, config_entry, capture_add)
    assert len(added) == 1
    assert isinstance(added[0][0], ChargeModeSelect)
    assert added[0][0]._sn == "1111"
    assert len(listeners) == 1

    added.clear()
    listeners[0]()
    assert added == []

    coord._ensure_serial_tracked("2222")
    coord.data["2222"] = {"sn": "2222", "name": "Driveway"}
    listeners[0]()

    assert len(added) == 1
    assert {entity._sn for entity in added[0]} == {"2222"}
