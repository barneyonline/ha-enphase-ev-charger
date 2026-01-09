"""Tests for the system health helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev import system_health
from custom_components.enphase_ev.const import BASE_URL, DOMAIN


@pytest.mark.asyncio
async def test_async_register_binds_handler(hass) -> None:
    """Ensure the system health handler is registered."""

    class DummyRegister:
        def __init__(self) -> None:
            self.handler = None

        def async_register_info(self, handler):
            self.handler = handler

    register = DummyRegister()
    system_health.async_register(hass, register)
    assert register.handler is system_health.system_health_info


@pytest.mark.asyncio
async def test_system_health_info_reports_state(hass, config_entry, monkeypatch) -> None:
    """The info payload should reflect coordinator attributes."""
    coord = SimpleNamespace()
    coord.last_success_utc = datetime(2024, 1, 1, tzinfo=timezone.utc)
    coord.latency_ms = 120
    coord._last_error = "timeout"
    coord._backoff_until = 42.0
    coord._network_errors = 3
    coord._http_errors = 1
    coord.phase_timings = {"fast": 0.5}
    coord._session_history_cache_ttl = 300

    coord.collect_site_metrics = lambda: {
        "site_id": config_entry.data["site_id"],
        "site_name": "Garage Site",
        "last_success": coord.last_success_utc.isoformat(),
        "latency_ms": coord.latency_ms,
        "last_error": coord._last_error,
        "backoff_active": True,
        "network_errors": coord._network_errors,
        "http_errors": coord._http_errors,
        "phase_timings": coord.phase_timings,
        "session_cache_ttl_s": coord._session_history_cache_ttl,
        "last_failure_status": None,
        "last_failure_description": None,
    }

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    async def can_reach_server(hass, url):
        return url == BASE_URL

    monkeypatch.setattr(
        system_health.system_health,
        "async_check_can_reach_url",
        can_reach_server,
    )

    info = await system_health.system_health_info(hass)

    assert info["site_id"] == config_entry.data["site_id"]
    assert info["site_name"] == "Garage Site"
    assert info["can_reach_server"] is True
    assert info["last_success"] == coord.last_success_utc.isoformat()
    assert info["latency_ms"] == 120
    assert info["last_error"] == "timeout"
    assert info["backoff_active"] is True
    assert info["network_errors"] == 3
    assert info["http_errors"] == 1
    assert info["phase_timings"] == {"fast": 0.5}
    assert info["session_cache_ttl_s"] == 300


@pytest.mark.asyncio
async def test_system_health_info_multiple_entries(
    hass, config_entry, monkeypatch
) -> None:
    """Multiple sites should appear in the aggregated payload."""
    coord1 = SimpleNamespace()
    coord1.last_success_utc = datetime(2024, 1, 1, tzinfo=timezone.utc)
    coord1.latency_ms = 110
    coord1._last_error = None
    coord1._backoff_until = 12.5
    coord1._network_errors = 1
    coord1._http_errors = 0
    coord1.phase_timings = {"status": 0.4}
    coord1._session_history_cache_ttl = 120

    coord2 = SimpleNamespace()
    coord2.last_success_utc = None
    coord2.latency_ms = None
    coord2._last_error = "dns"
    coord2._backoff_until = 0
    coord2._network_errors = 5
    coord2._http_errors = 2
    coord2.phase_timings = {}
    coord2._session_history_cache_ttl = None

    coord1.collect_site_metrics = lambda: {
        "site_id": config_entry.data["site_id"],
        "site_name": "Garage Site",
        "last_success": coord1.last_success_utc.isoformat(),
        "latency_ms": coord1.latency_ms,
        "last_error": None,
        "backoff_active": True,
        "network_errors": coord1._network_errors,
        "http_errors": coord1._http_errors,
        "phase_timings": coord1.phase_timings,
        "session_cache_ttl_s": coord1._session_history_cache_ttl,
        "last_failure_status": None,
        "last_failure_description": None,
    }
    coord2.collect_site_metrics = lambda: {
        "site_id": "second-site",
        "site_name": "Second Site",
        "last_success": None,
        "latency_ms": None,
        "last_error": coord2._last_error,
        "backoff_active": False,
        "network_errors": coord2._network_errors,
        "http_errors": coord2._http_errors,
        "phase_timings": coord2.phase_timings,
        "session_cache_ttl_s": coord2._session_history_cache_ttl,
        "last_failure_status": None,
        "last_failure_description": None,
    }

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord1}

    second_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "site_id": "second-site",
        },
        title="Second Site",
        unique_id="second-site",
    )
    second_entry.add_to_hass(hass)
    hass.data[DOMAIN][second_entry.entry_id] = {"coordinator": coord2}

    async def can_reach_server(hass, url):
        return url == BASE_URL

    monkeypatch.setattr(
        system_health.system_health,
        "async_check_can_reach_url",
        can_reach_server,
    )

    info = await system_health.system_health_info(hass)

    assert info["site_count"] == 2
    assert set(info["site_ids"]) == {config_entry.data["site_id"], "second-site"}
    assert set(info["site_names"]) == {"Garage Site", "Second Site"}
    assert len(info["sites"]) == 2
    ids = {site["site_id"] for site in info["sites"]}
    assert ids == {config_entry.data["site_id"], "second-site"}


@pytest.mark.asyncio
async def test_system_health_fallback_metrics(hass, config_entry, monkeypatch) -> None:
    coord = SimpleNamespace()
    coord.last_success_utc = None
    coord.latency_ms = None
    coord._last_error = "timeout"
    coord._backoff_until = 1.0
    coord._network_errors = 2
    coord._http_errors = 1
    coord.phase_timings = {"status": 0.3}
    coord._session_history_cache_ttl = 120

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    async def can_reach_server(hass_, url):
        return True

    monkeypatch.setattr(
        system_health.system_health,
        "async_check_can_reach_url",
        can_reach_server,
    )

    info = await system_health.system_health_info(hass)

    assert info["site_ids"] == [config_entry.data["site_id"]]
    assert info["site_names"] == []
    sites = info["sites"]
    assert sites[0]["site_id"] == config_entry.data["site_id"]
    assert sites[0]["network_errors"] == 2
    assert sites[0]["phase_timings"] == {"status": 0.3}


@pytest.mark.asyncio
async def test_system_health_missing_site_id_fills_from_entry(
    hass, config_entry, monkeypatch
) -> None:
    """Ensure the fallback assigns the entry site_id when metrics omit it."""
    coord = SimpleNamespace()
    coord.collect_site_metrics = lambda: {
        "site_id": None,
        "site_name": None,
        "last_success": None,
        "latency_ms": None,
        "last_error": None,
        "backoff_active": False,
        "network_errors": None,
        "http_errors": None,
        "phase_timings": {},
        "session_cache_ttl_s": None,
        "last_failure_status": None,
        "last_failure_description": None,
    }

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    async def can_reach_server(hass_, url):
        return True

    monkeypatch.setattr(
        system_health.system_health,
        "async_check_can_reach_url",
        can_reach_server,
    )

    info = await system_health.system_health_info(hass)

    assert info["site_id"] == config_entry.data["site_id"]
    assert info["sites"][0]["site_id"] == config_entry.data["site_id"]


@pytest.mark.asyncio
async def test_system_health_uses_session_manager_ttl(
    hass, config_entry, monkeypatch
) -> None:
    """Ensure fallback metrics report the session manager TTL when available."""
    session_manager = SimpleNamespace(cache_ttl=456)
    coord = SimpleNamespace(
        last_success_utc=None,
        latency_ms=None,
        _last_error=None,
        _backoff_until=0.0,
        _network_errors=0,
        _http_errors=0,
        phase_timings={},
        session_history=session_manager,
    )

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    async def can_reach_server(hass_, url):
        return True

    monkeypatch.setattr(
        system_health.system_health,
        "async_check_can_reach_url",
        can_reach_server,
    )

    info = await system_health.system_health_info(hass)
    assert info["session_cache_ttl_s"] == 456
    assert info["sites"][0]["session_cache_ttl_s"] == 456
