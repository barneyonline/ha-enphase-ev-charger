"""Shared pytest fixtures for the Enphase EV custom integration."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.enphase_ev.const import (
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REMEMBER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SERIALS,
    CONF_SESSION_ID,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    CONF_TOKEN_EXPIRES_AT,
    DOMAIN,
)

from .random_ids import RANDOM_SERIAL, RANDOM_SITE_ID

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(hass):
    """Ensure custom integrations remain enabled for each test run."""
    try:
        from homeassistant import loader

        hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def ensure_hass_stopped(stop_hass):
    """Ensure Home Assistant instances are shut down cleanly after tests."""
    yield



@pytest.fixture
def load_fixture() -> Callable[[str], dict[str, Any]]:
    """Return helper for loading JSON fixtures with anonymised IDs."""

    def _load(name: str) -> dict[str, Any]:
        raw = (FIXTURE_DIR / name).read_text(encoding="utf-8")
        scrubbed = (
            raw.replace("482522020944", RANDOM_SERIAL)
            .replace("3381244", RANDOM_SITE_ID)
        )
        return json.loads(scrubbed)

    return _load


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Provide a fully-populated config entry added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SITE_ID: RANDOM_SITE_ID,
            CONF_SITE_NAME: "Garage Site",
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_REMEMBER_PASSWORD: True,
            CONF_SERIALS: [RANDOM_SERIAL],
            CONF_SCAN_INTERVAL: 15,
            CONF_COOKIE: "cookie=1",
            CONF_EAUTH: "token123",
            CONF_SESSION_ID: "session-123",
            CONF_TOKEN_EXPIRES_AT: 1_700_000_000,
        },
        title="Garage Site",
        unique_id=RANDOM_SITE_ID,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def stub_coordinator_session_history(monkeypatch, request) -> None:
    """Avoid network lookups for session history unless explicitly requested."""
    from custom_components.enphase_ev import coordinator as coord_mod

    if request.node.get_closest_marker("session_history_real"):
        return

    async def _fake_sessions(self, sn: str, *, day_local=None):
        return []

    monkeypatch.setattr(
        coord_mod.EnphaseCoordinator,
        "_async_fetch_sessions_today",
        _fake_sessions,
    )
