"""Shared pytest fixtures for the Enphase EV custom integration."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from unittest.mock import AsyncMock, patch

try:
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
except ModuleNotFoundError:  # pragma: no cover - fallback for local test runs
    ROOT = Path(__file__).resolve().parents[3]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
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


@pytest.fixture
def mock_issue_registry(monkeypatch) -> SimpleNamespace:
    """Capture coordinator issue registry calls without touching HA."""
    from custom_components.enphase_ev import coordinator as coord_mod

    created: list[tuple[str, str, dict[str, Any]]] = []
    deleted: list[tuple[str, str]] = []

    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda hass, domain, issue_id, **kwargs: created.append(
            (domain, issue_id, kwargs)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        coord_mod.ir,
        "async_delete_issue",
        lambda hass, domain, issue_id: deleted.append((domain, issue_id)),
        raising=False,
    )
    return SimpleNamespace(created=created, deleted=deleted)


@pytest.fixture(autouse=True)
def mock_clientsession(monkeypatch):
    """Stub out aiohttp client session factory used by the integration."""
    session = object()
    for target in (
        "custom_components.enphase_ev.coordinator.async_get_clientsession",
        "custom_components.enphase_ev.config_flow.async_get_clientsession",
    ):
        monkeypatch.setattr(
            target,
            lambda *args, **kwargs: session,
            raising=False,
        )
    return session


@pytest.fixture
def coordinator_factory(hass, mock_clientsession, mock_issue_registry, monkeypatch):
    """Return a factory that builds a patched EnphaseCoordinator instance."""
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )

    def _factory(
        *,
        config: dict[str, Any] | None = None,
        serials: list[str] | None = None,
        client: Any | None = None,
        data: dict[str, Any] | None = None,
    ) -> EnphaseCoordinator:
        active_serials = serials or [RANDOM_SERIAL]
        cfg = config or {
            CONF_SITE_ID: RANDOM_SITE_ID,
            CONF_SERIALS: active_serials,
            CONF_EAUTH: "EAUTH",
            CONF_COOKIE: "COOKIE",
            CONF_SCAN_INTERVAL: 15,
        }
        coord = EnphaseCoordinator(hass, cfg)
        coord.serials = set(active_serials)
        coord.data = data or {sn: {"sn": sn, "name": f"Charger {sn}"} for sn in coord.serials}
        coord.last_set_amps = getattr(coord, "last_set_amps", {}) or {}
        if client is not None:
            coord.client = client
        return coord

    return _factory


@pytest.fixture
def setup_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_clientsession,
    mock_issue_registry,
):
    """Return helper to fully set up the integration via HA config entries."""
    from custom_components.enphase_ev.const import DOMAIN

    async def _setup(
        *,
        client: Any | None = None,
        first_refresh_result: Any = None,
    ) -> dict[str, Any]:
        forward_calls: list[list[str]] = []
        unload_mock = AsyncMock(return_value=True)
        with ExitStack() as stack:
            if client is not None:
                stack.enter_context(
                    patch(
                        "custom_components.enphase_ev.coordinator.EnphaseEVClient",
                        return_value=client,
                    )
                )
            stack.enter_context(
                patch(
                    "custom_components.enphase_ev.coordinator.EnphaseCoordinator.async_config_entry_first_refresh",
                    AsyncMock(return_value=first_refresh_result),
                )
            )
            stack.enter_context(
                patch.object(
                    hass.config_entries,
                    "async_forward_entry_setups",
                    AsyncMock(
                        side_effect=lambda entry, platforms: forward_calls.append(
                            list(platforms)
                        )
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    hass.config_entries,
                    "async_unload_platforms",
                    unload_mock,
                )
            )
            assert await hass.config_entries.async_setup(config_entry.entry_id)
            await hass.async_block_till_done()
        entry_data = hass.data[DOMAIN][config_entry.entry_id]
        return {
            "entry_data": entry_data,
            "forwarded": forward_calls,
            "unload_mock": unload_mock,
        }

    return _setup
