"""Tests for integration diagnostics."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from homeassistant.helpers import device_registry as dr

from custom_components.enphase_ev import diagnostics
from custom_components.enphase_ev.const import DOMAIN
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


class DummyClient(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__()
        self._h = {"Authorization": "REDACTED", "X-Test": "value"}

    def _bearer(self):
        return "token"


class DummyCoordinator(SimpleNamespace):
    """Coordinator stub exposing diagnostics attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.client = DummyClient()
        self.update_interval = timedelta(seconds=45)
        self._charge_mode_cache = {RANDOM_SERIAL: ("FAST", 0)}
        self.site_id = RANDOM_SITE_ID
        self.serials = {RANDOM_SERIAL}
        self.data = {RANDOM_SERIAL: {"sn": RANDOM_SERIAL, "status": "idle"}}
        self._network_errors = 2
        self._http_errors = 1
        self._backoff_until = 120.0
        self._last_error = "timeout"
        self.phase_timings = {"fast": 0.6}
        self._session_history_cache_ttl = 300
        self._session_history_cache = {"key": []}
        self._session_history_interval_min = 15
        self._session_refresh_in_progress = {"key"}

    def collect_site_metrics(self):
        return {
            "site_id": self.site_id,
            "site_name": "Garage Site",
            "network_errors": self._network_errors,
            "http_errors": self._http_errors,
            "last_error": self._last_error,
            "phase_timings": self.phase_timings,
            "session_cache_ttl_s": self._session_history_cache_ttl,
        }


@pytest.mark.asyncio
async def test_config_entry_diagnostics_includes_coordinator(hass, config_entry) -> None:
    """Validate coordinator diagnostics payload and redaction logic."""
    coord = DummyCoordinator()
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["entry_data"]["cookie"] == "**REDACTED**"
    assert diag["entry_data"]["email"] == "**REDACTED**"
    assert diag["coordinator"]["site_id"] == RANDOM_SITE_ID
    assert diag["coordinator"]["site_metrics"]["site_name"] == "Garage Site"
    assert diag["coordinator"]["headers_info"]["base_header_names"] == [
        "Authorization",
        "X-Test",
    ]
    assert diag["coordinator"]["headers_info"]["has_scheduler_bearer"] is True
    assert diag["coordinator"]["last_scheduler_modes"] == {RANDOM_SERIAL: "FAST"}
    assert diag["coordinator"]["session_history"]["cache_keys"] == 1


@pytest.mark.asyncio
async def test_device_diagnostics_returns_snapshot(
    hass, config_entry
) -> None:
    """Device diagnostics should resolve a serial and return cached data."""
    coord = DummyCoordinator()
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL), (DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Garage Charger",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    assert result["serial"] == RANDOM_SERIAL
    assert result["snapshot"] == coord.data[RANDOM_SERIAL]


@pytest.mark.asyncio
async def test_device_diagnostics_handles_missing_serial(
    hass, config_entry
) -> None:
    """If a device has no serial identifier, report the error."""
    coord = DummyCoordinator()
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
    )

    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    assert result == {"error": "serial_not_resolved"}
