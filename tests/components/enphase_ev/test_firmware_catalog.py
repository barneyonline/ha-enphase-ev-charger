from __future__ import annotations

from datetime import datetime, timedelta
import time
from types import SimpleNamespace

import pytest

from custom_components.enphase_ev import firmware_catalog


class _FakeResponse:
    def __init__(self, *, status: int, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):  # noqa: ARG002
        return self._payload


class _FakeSession:
    def __init__(self, actions):
        self._actions = list(actions)

    def get(self, _url, timeout):  # noqa: ARG002
        if not self._actions:
            raise RuntimeError("no fake actions left")
        action = self._actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


@pytest.mark.asyncio
async def test_catalog_manager_caches_and_uses_stale_on_error(monkeypatch) -> None:
    payload = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {"envoy": {}, "microinverter": {}},
    }
    fake_session = _FakeSession(
        [
            _FakeResponse(status=200, payload=payload),
            RuntimeError("network down"),
        ]
    )
    monkeypatch.setattr(
        firmware_catalog,
        "async_get_clientsession",
        lambda _hass: fake_session,
    )

    manager = firmware_catalog.FirmwareCatalogManager(SimpleNamespace())

    first = await manager.async_get_catalog(force_refresh=True)
    assert first == payload

    cached = await manager.async_get_catalog()
    assert cached == payload

    stale = await manager.async_get_catalog(force_refresh=True)
    assert stale == payload

    status = manager.status_snapshot()
    assert status["using_stale"] is True
    assert status["last_error"] == "network down"
    assert status["catalog_generated_at"] == "2026-03-01T00:00:00Z"
    assert manager.cached_catalog == payload


@pytest.mark.asyncio
async def test_catalog_manager_handles_http_error_and_validation_errors(monkeypatch) -> None:
    bad_payload = {"schema_version": 2, "devices": {}}
    fake_session = _FakeSession(
        [
            _FakeResponse(status=503, payload={}),
            _FakeResponse(status=200, payload=bad_payload),
        ]
    )
    monkeypatch.setattr(
        firmware_catalog,
        "async_get_clientsession",
        lambda _hass: fake_session,
    )
    manager = firmware_catalog.FirmwareCatalogManager(SimpleNamespace(), ttl_seconds=1)

    assert await manager.async_get_catalog(force_refresh=True) is None
    assert manager.status_snapshot()["last_error"] == "HTTP 503"

    assert await manager.async_get_catalog(force_refresh=True) is None
    assert "unsupported schema_version" in (manager.status_snapshot()["last_error"] or "")


@pytest.mark.asyncio
async def test_catalog_manager_cold_start_failure_honors_backoff(monkeypatch) -> None:
    fake_session = _FakeSession([RuntimeError("network down")])
    monkeypatch.setattr(
        firmware_catalog,
        "async_get_clientsession",
        lambda _hass: fake_session,
    )

    manager = firmware_catalog.FirmwareCatalogManager(
        SimpleNamespace(),
        retry_backoff_seconds=1800,
    )

    assert await manager.async_get_catalog(force_refresh=True) is None
    assert await manager.async_get_catalog() is None
    assert manager.status_snapshot()["last_error"] == "network down"


@pytest.mark.asyncio
async def test_catalog_manager_lock_recheck_returns_cached_without_fetch(monkeypatch) -> None:
    payload = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {"envoy": {}, "microinverter": {}},
    }
    manager = firmware_catalog.FirmwareCatalogManager(SimpleNamespace())

    class _RaceLock:
        async def __aenter__(self_inner):
            manager._catalog = payload
            manager._expires_mono = time.monotonic() + 60
            return None

        async def __aexit__(self_inner, exc_type, exc, tb):
            return False

    manager._lock = _RaceLock()
    monkeypatch.setattr(
        firmware_catalog,
        "async_get_clientsession",
        lambda _hass: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )

    result = await manager.async_get_catalog()
    assert result == payload


def test_resolve_country_and_locale_priority() -> None:
    coord = SimpleNamespace(
        battery_country_code="AU",
        battery_locale="en-AU",
    )
    hass = SimpleNamespace(config=SimpleNamespace(country="US", language="fr-fr"))

    country, locale = firmware_catalog.resolve_country_and_locale(coord, hass)
    assert country == "AU"
    assert locale == "en-au"

    coord = SimpleNamespace(
        battery_country_code=None,
        battery_locale=None,
    )
    country, locale = firmware_catalog.resolve_country_and_locale(coord, hass)
    assert country == "US"
    assert locale == "fr-fr"


def test_version_normalization_and_comparison() -> None:
    assert firmware_catalog.normalize_version_token("v04.30.32") == "04.30.32"
    assert firmware_catalog.normalize_version_token("firmware 8.2.4401") == "8.2.4401"
    assert firmware_catalog.normalize_version_token("unknown") is None
    assert firmware_catalog.normalize_version_token("   ") is None

    assert firmware_catalog.compare_versions("8.2.4401", "8.2.4300") is True
    assert firmware_catalog.compare_versions("8.2.4401", "8.2.4401") is False
    assert firmware_catalog.compare_versions("8.2.4401", None) is None


def test_select_catalog_entry_country_and_locale_fallback() -> None:
    catalog = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {
            "envoy": {
                "latest_by_country": {
                    "AU": {
                        "version": "8.2.4401",
                        "urls_by_locale": {
                            "en": "https://example.com/en",
                            "fr-fr": "https://example.com/fr",
                        },
                    }
                },
                "latest_global": {
                    "version": "8.2.4300",
                    "urls_by_locale": {"en": "https://example.com/global"},
                },
            }
        },
    }

    selected = firmware_catalog.select_catalog_entry(
        catalog,
        device_type="envoy",
        country="AU",
        locale="fr-fr",
    )
    assert selected.source_scope == "country"
    assert selected.locale_used == "fr-fr"
    assert selected.entry and selected.entry["version"] == "8.2.4401"

    fallback = firmware_catalog.select_catalog_entry(
        catalog,
        device_type="envoy",
        country="AU",
        locale="es-es",
    )
    assert fallback.locale_used == "en"

    global_fallback = firmware_catalog.select_catalog_entry(
        catalog,
        device_type="envoy",
        country="US",
        locale="en-us",
    )
    assert global_fallback.source_scope == "global"
    assert global_fallback.entry and global_fallback.entry["version"] == "8.2.4300"


def test_source_age_seconds_handles_future_and_invalid() -> None:
    now = datetime.now(firmware_catalog.dt_util.UTC)
    future = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    assert firmware_catalog._source_age_seconds(future) == 0.0
    assert firmware_catalog._source_age_seconds("not-a-date") is None
    assert firmware_catalog._source_age_seconds(None) is None


def test_coordinator_metrics_include_firmware_catalog_status(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.firmware_catalog_manager = SimpleNamespace(
        status_snapshot=lambda: {
            "last_fetch_utc": "2026-03-01T00:00:00+00:00",
            "last_success_utc": "2026-03-01T00:00:00+00:00",
            "last_error": None,
            "using_stale": False,
            "catalog_generated_at": "2026-03-01T00:00:00Z",
            "catalog_source_age_seconds": 30.0,
        }
    )

    metrics = coord.collect_site_metrics()

    assert metrics["firmware_catalog_last_fetch_utc"] == "2026-03-01T00:00:00+00:00"
    assert metrics["firmware_catalog_last_success_utc"] == "2026-03-01T00:00:00+00:00"
    assert metrics["firmware_catalog_last_error"] is None
    assert metrics["firmware_catalog_using_stale"] is False
    assert metrics["firmware_catalog_generated_at"] == "2026-03-01T00:00:00Z"
    assert metrics["firmware_catalog_source_age_seconds"] == 30.0


def test_coordinator_metrics_handles_firmware_catalog_status_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.firmware_catalog_manager = SimpleNamespace(
        status_snapshot=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    metrics = coord.collect_site_metrics()

    assert metrics["firmware_catalog_last_fetch_utc"] is None
    assert metrics["firmware_catalog_last_error"] is None


def test_helper_edge_branches() -> None:
    class _BadStr:
        def __str__(self):
            raise ValueError("boom")

    with pytest.raises(ValueError):
        firmware_catalog._validate_catalog("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        firmware_catalog._validate_catalog({"schema_version": 2, "devices": {}})
    with pytest.raises(ValueError):
        firmware_catalog._validate_catalog({"schema_version": 1, "devices": []})

    assert firmware_catalog._catalog_generated_at(None) is None
    assert firmware_catalog._catalog_generated_at({"generated_at": None}) is None
    assert firmware_catalog._catalog_generated_at({"generated_at": _BadStr()}) is None

    assert firmware_catalog._iso_or_none(None) is None

    class _BadDatetime:
        def isoformat(self):
            raise ValueError("boom")

    assert firmware_catalog._iso_or_none(_BadDatetime()) is None  # type: ignore[arg-type]
    assert firmware_catalog._mono_to_utc_iso(time.monotonic() - 1) is None
    assert firmware_catalog._mono_to_utc_iso(time.monotonic() + 1) is not None
    assert firmware_catalog._parse_iso_datetime("") is None
    assert firmware_catalog._parse_iso_datetime("2026-03-01T00:00:00").tzinfo is not None

    assert firmware_catalog.normalize_locale(_BadStr()) == "en"
    assert firmware_catalog.normalize_country("AUS") is None
    assert firmware_catalog.normalize_country(_BadStr()) is None
    assert firmware_catalog.normalize_version_token(None) is None
    assert firmware_catalog.normalize_version_token(_BadStr()) is None
    assert firmware_catalog._parse_version_parts("") is None
    assert firmware_catalog._parse_version_parts("1.a") is None

    empty_selection = firmware_catalog.select_catalog_entry(
        None,
        device_type="envoy",
        country="AU",
        locale="en-au",
    )
    assert empty_selection.entry is None

    no_devices_selection = firmware_catalog.select_catalog_entry(
        {"schema_version": 1, "devices": []},
        device_type="envoy",
        country="AU",
        locale="en-au",
    )
    assert no_devices_selection.entry is None

    no_device_payload = firmware_catalog.select_catalog_entry(
        {"schema_version": 1, "devices": {"envoy": []}},
        device_type="envoy",
        country="AU",
        locale="en-au",
    )
    assert no_device_payload.entry is None

    no_entry_selection = firmware_catalog.select_catalog_entry(
        {
            "schema_version": 1,
            "devices": {"envoy": {"latest_by_country": {}, "latest_global": None}},
        },
        device_type="envoy",
        country="AU",
        locale="en-au",
    )
    assert no_entry_selection.entry is None

    fallback_first_locale = firmware_catalog.select_catalog_entry(
        {
            "schema_version": 1,
            "devices": {
                "envoy": {
                    "latest_by_country": {
                        "AU": {
                            "version": "1.0.0",
                            "urls_by_locale": {
                                "fr-ca": "https://example.com/fr",
                                "de-de": "https://example.com/de",
                            },
                        }
                    },
                    "latest_global": {},
                }
            },
        },
        device_type="envoy",
        country="AU",
        locale="es-es",
    )
    assert fallback_first_locale.locale_used == "fr-ca"
