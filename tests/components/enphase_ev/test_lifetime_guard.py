import time

import pytest


@pytest.mark.asyncio
async def test_lifetime_guard_ignores_transient_zero(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID

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

    first = coord.energy._apply_lifetime_guard(RANDOM_SERIAL, 320590.0, None)
    assert first == pytest.approx(320.59, abs=1e-3)

    coord._summary_cache = (time.monotonic(), [], coord._summary_ttl)
    second = coord.energy._apply_lifetime_guard(
        RANDOM_SERIAL,
        0.0,
        {"lifetime_kwh": first},
    )
    assert second == pytest.approx(320.59, abs=1e-3)
    assert coord._summary_cache is None

    state = coord.energy._lifetime_guard[RANDOM_SERIAL]
    assert state.pending_count == 1
    assert state.last == pytest.approx(320.59, abs=1e-3)

    coord._summary_cache = (time.monotonic(), [], coord._summary_ttl)
    third = coord.energy._apply_lifetime_guard(
        RANDOM_SERIAL,
        320700.0,
        {"lifetime_kwh": first},
    )
    assert third == pytest.approx(320.7, abs=1e-3)
    state = coord.energy._lifetime_guard[RANDOM_SERIAL]
    assert state.pending_count == 0
    assert state.last == pytest.approx(320.7, abs=1e-3)


@pytest.mark.asyncio
async def test_lifetime_guard_accepts_persistent_reset(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID

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

    baseline = coord.energy._apply_lifetime_guard(RANDOM_SERIAL, 320590.0, None)
    assert baseline == pytest.approx(320.59, abs=1e-3)

    coord._summary_cache = (time.monotonic(), [], coord._summary_ttl)
    _ = coord.energy._apply_lifetime_guard(
        RANDOM_SERIAL,
        0.0,
        {"lifetime_kwh": baseline},
    )

    coord._summary_cache = (time.monotonic(), [], coord._summary_ttl)
    accepted = coord.energy._apply_lifetime_guard(
        RANDOM_SERIAL,
        0.0,
        {"lifetime_kwh": baseline},
    )
    assert accepted == 0.0

    state = coord.energy._lifetime_guard[RANDOM_SERIAL]
    assert state.last == 0.0
    assert state.pending_count == 0

    follow_up = coord.energy._apply_lifetime_guard(
        RANDOM_SERIAL,
        1200.0,
        {"lifetime_kwh": 0.0},
    )
    assert follow_up == pytest.approx(1.2, abs=1e-3)
