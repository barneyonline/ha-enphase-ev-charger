from types import SimpleNamespace

import aiohttp
import pytest

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


@pytest.mark.asyncio
async def test_rate_limit_issue_created_on_repeated_429(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev import coordinator_diagnostics as diag_mod

    # Stub HA session
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(hass, cfg)

    # Stub ClientResponseError for 429
    class StubRespErr(aiohttp.ClientResponseError):
        def __init__(self, status, headers=None):
            req = aiohttp.RequestInfo(
                url=aiohttp.client.URL("https://example"),
                method="GET",
                headers={},
                real_url=aiohttp.client.URL("https://example"),
            )
            super().__init__(
                request_info=req,
                history=(),
                status=status,
                message="",
                headers=headers or {},
            )

    class StubClient:
        async def status(self):
            raise StubRespErr(429, headers={"Retry-After": "1"})

    coord.client = StubClient()

    # Capture issue creation calls
    created = []
    deleted = []
    monkeypatch.setattr(
        diag_mod.ir,
        "async_create_issue",
        lambda *args, **kwargs: created.append(kwargs),
    )
    monkeypatch.setattr(
        diag_mod.ir,
        "async_delete_issue",
        lambda _hass, _domain, issue_id: deleted.append(issue_id),
    )

    # First 429 -> backoff, no issue yet
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert created == []

    # Second 429 -> create rate_limited issue
    # Clear backoff to force a second call that hits 429 again
    coord._backoff_until = None
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    rate_limit_issues = [
        kwargs for kwargs in created if kwargs.get("translation_key") == "rate_limited"
    ]
    assert len(rate_limit_issues) == 1
    assert coord._rate_limit_issue_reported is True
    assert "backoff_ends" in rate_limit_issues[0]["translation_placeholders"]

    coord._backoff_until = None
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    rate_limit_issues = [
        kwargs for kwargs in created if kwargs.get("translation_key") == "rate_limited"
    ]
    assert len(rate_limit_issues) == 1

    coord._record_status_refresh_success(SimpleNamespace(status_used_stale=False))
    assert "rate_limited" in deleted
    assert coord._rate_limit_issue_reported is False

    coord._record_status_refresh_success(SimpleNamespace(status_used_stale=False))
    assert deleted.count("rate_limited") == 1

    deleted.clear()
    coord._rate_limit_issue_clear_checked = False
    coord._record_status_refresh_success(SimpleNamespace(status_used_stale=False))
    assert deleted == ["rate_limited"]
    assert coord._rate_limit_issue_clear_checked is True

    coord._record_status_refresh_success(SimpleNamespace(status_used_stale=False))
    assert deleted == ["rate_limited"]


@pytest.mark.asyncio
async def test_backoff_blocks_updates(hass, monkeypatch):
    import time

    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(hass, cfg)

    # Force a backoff window
    coord._backoff_until = time.monotonic() + 100

    with pytest.raises(UpdateFailed):
        # Should raise immediately due to backoff without calling client
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_latency_ms_set_on_success_and_failure(hass, monkeypatch):
    import asyncio

    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(hass, cfg)

    class GoodClient:
        async def status(self):
            return {"evChargerData": []}

    coord.client = GoodClient()
    await coord._async_update_data()
    assert isinstance(coord.latency_ms, int)
    assert coord.latency_ms >= 0

    class BadClient:
        async def status(self):
            await asyncio.sleep(0)
            raise asyncio.TimeoutError()

    coord.client = BadClient()
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    # latency should still be set in finally
    assert isinstance(coord.latency_ms, int)
