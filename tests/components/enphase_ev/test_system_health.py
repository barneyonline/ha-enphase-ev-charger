"""Tests for the system health helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_components.enphase_ev import system_health
from custom_components.enphase_ev.const import BASE_URL, DOMAIN
from tests.components.enphase_ev.random_ids import RANDOM_SITE_ID


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

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    monkeypatch.setattr(
        system_health.system_health,
        "async_check_can_reach_url",
        lambda hass, url: url == BASE_URL,
    )

    info = await system_health.system_health_info(hass)

    assert info["site_id"] == config_entry.data["site_id"]
    assert info["can_reach_server"] is True
    assert info["last_success"] == coord.last_success_utc.isoformat()
    assert info["latency_ms"] == 120
    assert info["last_error"] == "timeout"
    assert info["backoff_active"] is True
    assert info["network_errors"] == 3
    assert info["http_errors"] == 1
    assert info["phase_timings"] == {"fast": 0.5}
    assert info["session_cache_ttl_s"] == 300
