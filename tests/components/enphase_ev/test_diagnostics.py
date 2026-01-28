"""Tests for integration diagnostics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        self.session_history = SimpleNamespace(
            cache_ttl=300,
            cache_key_count=1,
            in_progress=1,
        )

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
    coord.schedule_sync = SimpleNamespace(diagnostics=lambda: {"enabled": True})
    coord._scheduler_backoff_ends_utc = datetime(2025, 1, 1, tzinfo=timezone.utc)
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
    assert diag["coordinator"]["schedule_sync"] == {"enabled": True}
    assert diag["coordinator"]["scheduler"]["backoff_ends_utc"] == "2025-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_schedule_sync_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()

    class BadScheduleSync:
        def diagnostics(self):
            raise RuntimeError("boom")

    coord.schedule_sync = BadScheduleSync()
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["schedule_sync"] is None


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_scheduler_backoff_format_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()

    class BadDatetime(datetime):
        def isoformat(self):  # type: ignore[override]
            raise ValueError("boom")

    coord._scheduler_backoff_ends_utc = BadDatetime(2025, 1, 1, tzinfo=timezone.utc)
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["scheduler"]["backoff_ends_utc"] is None


@pytest.mark.asyncio
async def test_config_entry_diagnostics_without_coordinator(hass, config_entry) -> None:
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {}

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert "coordinator" not in diag


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_faulty_coordinator(
    hass, config_entry
) -> None:
    class FaultyClient:
        @property
        def _h(self):
            raise RuntimeError("no headers")

        def _bearer(self):
            raise RuntimeError("no bearer")

    class FaultyCoordinator(DummyCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.update_interval = object()
            self._charge_mode_cache = None
            self.client = FaultyClient()

        def collect_site_metrics(self):
            raise RuntimeError("boom")

    coord = FaultyCoordinator()
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    coordinator = diag["coordinator"]
    assert coordinator["site_metrics"] is None
    assert coordinator["headers_info"]["base_header_names"] == []
    assert coordinator["headers_info"]["has_scheduler_bearer"] is False
    assert coordinator["last_scheduler_modes"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_includes_site_energy(hass, config_entry) -> None:
    coord = DummyCoordinator()
    coord.energy = SimpleNamespace(
        site_energy={
            "grid_import": SimpleNamespace(
                value_kwh=1.0,
                bucket_count=2,
                fields_used=["import"],
                start_date="2024-01-01",
                last_report_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
                update_pending=True,
                source_unit="Wh",
                last_reset_at=None,
            )
        },
        _site_energy_meta={
            "start_date": "2024-01-01",
            "last_report_date": datetime(2024, 1, 3, tzinfo=timezone.utc),
        },
        _site_energy_cache_age=lambda: 1.23,  # noqa: SLF001
    )
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    site_energy = diag["site_energy"]
    assert "grid_import" in site_energy["flows"]
    assert site_energy["meta"]["last_report_date"].startswith("2024-01-03")


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_unexpected_site_energy(hass, config_entry) -> None:
    coord = DummyCoordinator()
    coord.site_energy = {"bad": None, "other": "string"}
    coord._site_energy_meta = {"last_report_date": datetime(2024, 1, 1, tzinfo=timezone.utc)}
    coord._site_energy_cache_age = lambda: 1.23  # noqa: SLF001
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["site_energy"]["flows"] in (None, {})

    class BoomSiteEnergy:
        def items(self):
            raise RuntimeError("boom")

    coord.site_energy = BoomSiteEnergy()
    coord._site_energy_meta = {"last_report_date": datetime(2024, 1, 2, tzinfo=timezone.utc)}
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["site_energy"]["flows"] is None


@pytest.mark.asyncio
async def test_config_entry_diagnostics_cache_age_failure(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.energy = SimpleNamespace(
        site_energy={"grid_import": {"value_kwh": 1.0}},
        _site_energy_meta={
            "last_report_date": datetime(2024, 1, 2, tzinfo=timezone.utc)
        },
        _site_energy_cache_age=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = {"coordinator": coord}
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["site_energy"]["cache_age_s"] is None


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


@pytest.mark.asyncio
async def test_device_diagnostics_device_not_found(hass, config_entry) -> None:
    device = SimpleNamespace(id="missing-device")
    result = await diagnostics.async_get_device_diagnostics(
        hass, config_entry, device
    )
    assert result == {"error": "device_not_found"}


@pytest.mark.asyncio
async def test_device_diagnostics_missing_coordinator(hass, config_entry) -> None:
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
    assert result == {"serial": RANDOM_SERIAL, "snapshot": {}}
