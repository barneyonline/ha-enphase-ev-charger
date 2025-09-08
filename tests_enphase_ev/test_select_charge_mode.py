import pytest


@pytest.mark.asyncio
async def test_charge_mode_select(monkeypatch):
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
        CONF_SITE_ID: "9990001",
        CONF_SERIALS: ["499900000001"],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    coord = EnphaseCoordinator(object(), cfg)

    # preload coordinator state
    coord.data = {"499900000001": {"charge_mode": "SCHEDULED_CHARGING"}}

    class StubClient:
        async def set_charge_mode(self, sn: str, mode: str):
            return {"status": "accepted", "mode": mode}

    coord.client = StubClient()

    # Avoid exercising Debouncer / hass loop; stub refresh
    async def _noop():
        return None
    coord.async_request_refresh = _noop  # type: ignore[attr-defined]

    sel = ChargeModeSelect(coord, "499900000001")
    assert "Green" in sel.options
    assert sel.current_option == "Scheduled"

    await sel.async_select_option("Manual")
    # cache should update immediately
    assert coord._charge_mode_cache["499900000001"][0] == "MANUAL_CHARGING"
