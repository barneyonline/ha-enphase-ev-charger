import asyncio
import logging
import copy
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.const import (
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_INCLUDE_INVERTERS,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_TYPE_KEYS,
    CONF_SERIALS,
    CONF_SITE_ID,
    CONF_SITE_ONLY,
    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
    DOMAIN,
    ISSUE_DNS_RESOLUTION,
    OPT_NOMINAL_VOLTAGE,
    OPT_SESSION_HISTORY_INTERVAL,
)
from custom_components.enphase_ev.coordinator import (
    BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL,
    FAST_TOGGLE_POLL_HOLD_S,
    HEMS_DEVICES_CACHE_TTL,
    HEMS_DEVICES_STALE_AFTER_S,
    HEMS_SUPPORT_PREFLIGHT_CACHE_TTL,
    HEATPUMP_POWER_CACHE_TTL,
    ServiceValidationError,
)
from custom_components.enphase_ev.voltage import resolve_nominal_voltage_for_hass

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


def _make_coordinator(hass, monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev import coordinator as coord_mod

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
        CONF_SITE_ONLY: False,
        CONF_INCLUDE_INVERTERS: True,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )
    coord = EnphaseCoordinator(hass, cfg)
    coord.client.storm_guard_profile = AsyncMock(return_value={"data": {}})
    coord.client.storm_guard_alert = AsyncMock(
        return_value={"criticalAlertActive": False, "stormAlerts": []}
    )
    return coord


def _client_response_error(status: int, *, message: str = "", headers=None):
    req = aiohttp.RequestInfo(
        url=aiohttp.client.URL("https://example"),
        method="GET",
        headers={},
        real_url=aiohttp.client.URL("https://example"),
    )
    return aiohttp.ClientResponseError(
        request_info=req,
        history=(),
        status=status,
        message=message,
        headers=headers or {},
    )


@pytest.mark.asyncio
async def test_coordinator_init_normalizes_serials_and_options(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    class BadSerial:
        def __str__(self):
            raise ValueError("boom")

    config = {
        CONF_SITE_ID: "12345",
        CONF_SERIALS: [None, " EV01 ", "", "EV02", "EV01", BadSerial()],
        CONF_EAUTH: "token",
        CONF_COOKIE: "cookie",
        CONF_SCAN_INTERVAL: 30,
    }

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        options={
            OPT_NOMINAL_VOLTAGE: "bad",
            OPT_SESSION_HISTORY_INTERVAL: "not-a-number",
        },
    )
    entry.add_to_hass(hass)

    captured_tasks: list = []
    monkeypatch.setattr(
        hass, "async_create_task", lambda coro: captured_tasks.append(coro)
    )
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    class DummyClient:
        def __init__(self, *args, **kwargs) -> None:
            self.callbacks: list = []

        def set_reauth_callback(self, cb):
            async def _runner():
                self.callbacks.append(cb)

            return _runner()

    monkeypatch.setattr(coord_mod, "EnphaseEVClient", DummyClient)

    coord = EnphaseCoordinator(hass, config, config_entry=entry)

    assert coord.serials == {"EV01", "EV02"}
    assert coord._serial_order == ["EV01", "EV02"]
    assert coord._configured_serials == {"EV01", "EV02"}
    assert coord._nominal_v == resolve_nominal_voltage_for_hass(hass)
    assert coord._session_history_interval_min == DEFAULT_SESSION_HISTORY_INTERVAL_MIN
    assert coord._session_history_cache_ttl == DEFAULT_SESSION_HISTORY_INTERVAL_MIN * 60
    assert captured_tasks, "set_reauth_callback coroutine should be scheduled"
    await captured_tasks[0]


def test_coordinator_init_handles_invalid_selected_type_keys(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    config = {
        CONF_SITE_ID: "78901",
        CONF_SERIALS: " EV42 ",
        CONF_EAUTH: None,
        CONF_COOKIE: None,
        CONF_SCAN_INTERVAL: 60,
        CONF_SITE_ONLY: False,
        CONF_SELECTED_TYPE_KEYS: 123,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "EnphaseEVClient",
        lambda *args, **kwargs: SimpleNamespace(set_reauth_callback=lambda *_: None),
    )

    coord = EnphaseCoordinator(hass, config)

    assert coord._selected_type_keys is None  # noqa: SLF001
    assert coord._type_is_selected(None) is False  # noqa: SLF001

    config[CONF_SELECTED_TYPE_KEYS] = "iqevse"
    coord_with_string = EnphaseCoordinator(hass, config)
    assert coord_with_string._selected_type_keys == {"iqevse"}  # noqa: SLF001


def test_coordinator_init_handles_single_serial(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    config = {
        CONF_SITE_ID: "78901",
        CONF_SERIALS: " EV42 ",
        CONF_EAUTH: None,
        CONF_COOKIE: None,
        CONF_SCAN_INTERVAL: 60,
        CONF_SITE_ONLY: False,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "EnphaseEVClient",
        lambda *args, **kwargs: SimpleNamespace(set_reauth_callback=lambda *_: None),
    )

    coord = EnphaseCoordinator(hass, config)

    assert coord.serials == {"EV42"}
    assert coord._serial_order == ["EV42"]


def test_devices_inventory_parser_filters_retired_and_normalizes_types(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    payload = {
        "result": [
            {
                "type": "wind-turbine",
                "devices": [
                    {"name": "Wind 1", "status": "normal"},
                    {"name": "Retired Wind", "statusText": "Retired"},
                ],
            },
            {
                "type": "encharge",
                "devices": [
                    {"serial_number": "BAT-1", "name": "Battery 1", "status": "Normal"},
                    {
                        "serial_number": "BAT-2",
                        "name": "Battery 2",
                        "status": "retired",
                    },
                ],
            },
            {
                "type": "encharge",
                "devices": [
                    {"serial_number": "BAT-1", "name": "Battery 1 duplicate"},
                ],
            },
            {
                "type": "generator",
                "devices": [{"name": "Generator 1", "status": "RETIRED"}],
            },
        ]
    }

    valid, grouped, ordered = coord._parse_devices_inventory_payload(
        payload
    )  # noqa: SLF001

    assert valid is True
    assert ordered == ["wind_turbine", "encharge", "generator"]
    coord._set_type_device_buckets(grouped, ordered)  # noqa: SLF001

    assert coord.iter_type_keys() == ["wind_turbine", "encharge"]
    assert coord.type_device_name("wind-turbine") == "Wind Turbine"
    assert coord.type_bucket("encharge")["count"] == 1
    assert coord.has_type("generator") is False


def test_devices_inventory_parser_builds_battery_model_summary_from_name(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    payload = {
        "result": [
            {
                "type": "encharge",
                "devices": [
                    {"serial_number": "BAT-1", "name": "IQ Battery 5P"},
                    {"serial_number": "BAT-2", "name": "IQ Battery 5P"},
                    {"serial_number": "BAT-3", "name": "   "},
                ],
            }
        ]
    }

    valid, grouped, ordered = coord._parse_devices_inventory_payload(
        payload
    )  # noqa: SLF001
    assert valid is True
    coord._set_type_device_buckets(grouped, ordered)  # noqa: SLF001
    bucket = coord.type_bucket("encharge")
    assert bucket is not None
    assert bucket["model_summary"] == "IQ Battery 5P x2"
    assert bucket["model_counts"] == {"IQ Battery 5P": 2}
    assert coord.type_device_name("encharge") == "IQ Battery"
    assert coord.type_device_model("encharge") == "IQ Battery 5P"


def test_devices_inventory_parser_supports_legacy_bucket_field_names(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    payload = {
        "result": [
            {
                "deviceType": "inverters",
                "members": [
                    {"serial_number": "INV-1", "name": "Micro 1"},
                    {"serial_number": "INV-2", "name": "Micro 2"},
                ],
            },
            {
                "device_type": "microinverter",
                "items": [
                    {"serial_number": "INV-3", "name": "Micro 3"},
                ],
            },
        ]
    }

    valid, grouped, ordered = coord._parse_devices_inventory_payload(
        payload
    )  # noqa: SLF001

    assert valid is True
    assert ordered == ["microinverter"]
    coord._set_type_device_buckets(grouped, ordered)  # noqa: SLF001
    bucket = coord.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["count"] == 3


def test_devices_inventory_parser_battery_summary_handles_weird_member_shapes(
    hass, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)

    class _BadName:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    class _WeirdSanitized:
        def get(self, key, default=None):
            if key == "name":
                return "Weird Battery"
            return default

    def _fake_sanitize(member):
        marker = member.get("name")
        if marker == "WEIRD_NON_DICT":
            return _WeirdSanitized()
        if marker == "WEIRD_BAD_STR":
            return {"name": _BadName()}
        return {"name": "IQ Battery 5P"}

    monkeypatch.setattr(coord_mod, "sanitize_member", _fake_sanitize)

    valid, grouped, _ordered = coord._parse_devices_inventory_payload(
        {
            "result": [
                {
                    "type": "encharge",
                    "devices": [
                        {"name": "WEIRD_NON_DICT"},
                        {"name": "WEIRD_BAD_STR"},
                        {"name": "IQ Battery 5P"},
                    ],
                }
            ]
        }
    )

    assert valid is True
    bucket = grouped["encharge"]
    # The malformed members are ignored while valid members still contribute summary.
    assert bucket["count"] == 3
    assert bucket["model_summary"] == "IQ Battery 5P x1"


def test_devices_inventory_parser_merges_meter_and_enpower_into_gateway(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    payload = {
        "result": [
            {
                "type": "envoy",
                "devices": [{"serial_number": "GW-1", "name": "Gateway 1"}],
            },
            {
                "type": "meter",
                "devices": [{"serial_number": "MTR-1", "name": "Meter 1"}],
            },
            {
                "type": "enpower",
                "devices": [{"serial_number": "SC-1", "name": "System Controller 1"}],
            },
        ]
    }

    valid, grouped, ordered = coord._parse_devices_inventory_payload(
        payload
    )  # noqa: SLF001

    assert valid is True
    assert ordered == ["envoy"]
    assert list(grouped) == ["envoy"]
    assert grouped["envoy"]["count"] == 3

    coord._set_type_device_buckets(grouped, ordered)  # noqa: SLF001
    assert coord.iter_type_keys() == ["envoy"]
    bucket = coord.type_bucket("envoy")
    assert bucket is not None
    assert bucket["count"] == 3
    assert coord.type_bucket("meter") == bucket
    assert coord.type_bucket("enpower") == bucket
    assert coord.type_identifier("meter") == coord.type_identifier("envoy")
    assert coord.type_identifier("enpower") == coord.type_identifier("envoy")


def test_devices_inventory_parser_keeps_distinct_relay_buckets_when_normalized(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    payload = {
        "result": [
            {"type": "NC1", "devices": [{"name": "Inventory"}]},
            {"type": "NC2", "devices": [{"name": "Inventory"}]},
            {"type": "NO1", "devices": [{"name": "Inventory"}]},
        ]
    }

    valid, grouped, ordered = coord._parse_devices_inventory_payload(
        payload
    )  # noqa: SLF001

    assert valid is True
    assert ordered == ["dry_contact"]
    bucket = grouped["dry_contact"]
    assert bucket["count"] == 3
    assert bucket["devices"] == [
        {"name": "Inventory"},
        {"name": "Inventory"},
        {"name": "Inventory"},
    ]


def test_devices_inventory_parser_keeps_ambiguous_dry_contact_members(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    payload = {
        "result": [
            {
                "type": "drycontactloads",
                "devices": [
                    {"name": "Inventory"},
                    {"name": "Inventory"},
                ],
            }
        ]
    }

    valid, grouped, ordered = coord._parse_devices_inventory_payload(
        payload
    )  # noqa: SLF001

    assert valid is True
    assert ordered == ["dry_contact"]
    bucket = grouped["dry_contact"]
    assert bucket["count"] == 2
    assert bucket["devices"] == [
        {"name": "Inventory"},
        {"name": "Inventory"},
    ]


def test_devices_inventory_parser_dry_contact_dedupe_uses_serial_and_identity_fields(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    payload = {
        "result": [
            {
                "type": "drycontactloads",
                "devices": [
                    {"serial_number": "DRY-1", "name": "Inventory"},
                    {"serial_number": "DRY-1", "name": "Inventory"},
                    {"channel_type": "NC1", "meta": {"ignored": True}},
                    {"channel_type": "NC1", "meta": {"ignored": True}},
                    {"id": "2"},
                ],
            }
        ]
    }

    valid, grouped, ordered = coord._parse_devices_inventory_payload(
        payload
    )  # noqa: SLF001

    assert valid is True
    assert ordered == ["dry_contact"]
    bucket = grouped["dry_contact"]
    assert bucket["count"] == 3
    assert bucket["devices"] == [
        {"name": "Inventory", "serial_number": "DRY-1"},
        {"channel_type": "NC1"},
        {"id": "2"},
    ]


def test_devices_inventory_parser_dry_contact_dedupe_handles_bad_source_and_empty_identity(
    hass, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    values = iter(
        [
            {"name": "Inventory"},
            {"name": "Inventory"},
            {"nested": {"ignored": True}},
        ]
    )

    monkeypatch.setattr(coord_mod, "normalize_type_key", lambda _raw: "dry_contact")
    monkeypatch.setattr(coord_mod, "type_display_label", lambda _raw: "Dry Contacts")
    monkeypatch.setattr(coord_mod, "sanitize_member", lambda _member: next(values))

    valid, grouped, ordered = coord._parse_devices_inventory_payload(
        {"result": [{"type": BadStr(), "devices": [{}, {}, {}]}]}
    )

    assert valid is True
    assert ordered == ["dry_contact"]
    bucket = grouped["dry_contact"]
    assert bucket["count"] == 3
    assert bucket["devices"] == [
        {"name": "Inventory"},
        {"name": "Inventory"},
        {"nested": {"ignored": True}},
    ]


def test_devices_inventory_parser_dry_contact_dedupe_handles_empty_identity_with_source(
    hass, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)

    values = iter(
        [
            {"nested": {"ignored": True}},
            {"nested": {"ignored": True}},
        ]
    )

    monkeypatch.setattr(coord_mod, "sanitize_member", lambda _member: next(values))

    valid, grouped, ordered = coord._parse_devices_inventory_payload(
        {"result": [{"type": "drycontactloads", "devices": [{}, {}]}]}
    )

    assert valid is True
    assert ordered == ["dry_contact"]
    bucket = grouped["dry_contact"]
    assert bucket["count"] == 2
    assert bucket["devices"] == [
        {"nested": {"ignored": True}},
        {"nested": {"ignored": True}},
    ]


@pytest.mark.asyncio
async def test_devices_inventory_helpers_cover_edge_paths(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    assert coord.has_type_for_entities("envoy") is True

    assert coord._parse_devices_inventory_payload("bad") == (False, {}, [])
    assert coord._parse_devices_inventory_payload({}) == (False, {}, [])

    valid, grouped, ordered = coord._parse_devices_inventory_payload(
        [
            "bad-bucket",
            {"type": "encharge", "devices": "bad"},
            {
                "type": "encharge",
                "devices": [
                    "bad-member",
                    {"status": "retired"},
                    {"status": "normal"},
                    {"nested": {"skip": True}},
                ],
            },
        ]
    )
    assert valid is True
    assert "encharge" in grouped
    assert ordered == ["encharge"]

    coord._set_type_device_buckets(grouped, ordered)
    assert coord.type_device_name("encharge") == "IQ Battery"
    assert coord.has_type_for_entities("encharge") is True
    assert coord.has_type_for_entities("envoy") is False

    coord._type_device_order = ("bad-order",)  # noqa: SLF001
    assert coord.iter_type_keys() == []

    coord._type_device_buckets = None  # type: ignore[assignment]  # noqa: SLF001
    assert coord.has_type("encharge") is False
    assert coord.type_bucket("encharge") is None

    coord._type_device_buckets = {
        "encharge": {"count": object(), "devices": []}
    }  # noqa: SLF001
    assert coord.has_type(None) is False
    assert coord.has_type("encharge") is False
    assert coord.has_type_for_entities(None) is False
    assert coord.type_bucket(None) is None
    bucket = coord.type_bucket("encharge")
    assert bucket is not None
    assert bucket["devices"] == []
    coord._type_device_buckets = {
        "encharge": {"count": 1, "devices": "bad"}
    }  # noqa: SLF001
    bucket = coord.type_bucket("encharge")
    assert bucket is not None
    assert bucket["devices"] == []
    assert coord.type_label(None) is None
    assert coord.type_label("unknown_type") == "Unknown Type"

    coord._type_device_buckets = {
        "encharge": {"count": "bad", "devices": []}
    }  # noqa: SLF001
    assert coord.type_identifier(None) is None
    assert coord.type_device_name(None) is None
    assert coord.type_device_name("missing") is None
    coord._type_device_buckets = {
        "encharge": {"count": 1, "devices": [], "type_label": 1}
    }  # noqa: SLF001
    assert coord.type_device_name("encharge") == "IQ Battery"
    coord._type_device_buckets = {
        "encharge": {"count": "bad", "devices": []}
    }  # noqa: SLF001
    assert coord.type_device_name("encharge") == "IQ Battery"
    assert coord.type_device_info("unknown") is None
    assert coord.parse_type_identifier("bad") is None

    coord._type_device_order = ["encharge", "missing"]  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "encharge": {"count": "bad", "devices": [], "type_label": "Battery"},
    }
    metrics = coord.collect_site_metrics()
    assert metrics["type_device_counts"]["encharge"] == 0


@pytest.mark.asyncio
async def test_devices_inventory_refresh_cache_and_exception_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    assert coord.has_type_for_entities("envoy") is True

    coord._devices_inventory_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.devices_inventory = AsyncMock(side_effect=AssertionError("no fetch"))
    await coord._async_refresh_devices_inventory()

    coord._devices_inventory_cache_until = None  # noqa: SLF001
    coord.client.devices_inventory = AsyncMock(return_value={})
    await coord._async_refresh_devices_inventory()
    assert coord.has_type_for_entities("envoy") is True

    monkeypatch.setattr(coord, "_redact_battery_payload", lambda payload: "raw")
    coord.client.devices_inventory = AsyncMock(
        return_value={
            "result": [{"type": "envoy", "devices": [{"name": "IQ Gateway"}]}]
        }
    )
    await coord._async_refresh_devices_inventory(force=True)
    assert coord._devices_inventory_payload == {"value": "raw"}  # noqa: SLF001

    monkeypatch.setattr(coord, "_redact_battery_payload", lambda payload: payload)
    await coord._async_refresh_devices_inventory(force=True)
    assert coord._devices_inventory_payload == {
        "result": [{"type": "envoy", "devices": [{"name": "IQ Gateway"}]}]
    }
    assert coord.has_type("envoy") is True

    coord.client.devices_inventory = AsyncMock(return_value={"result": []})
    await coord._async_refresh_devices_inventory(force=True)
    assert coord.has_type("envoy") is True
    assert coord.has_type_for_entities("envoy") is True

    coord._devices_inventory_cache_until = None  # noqa: SLF001
    coord.client.devices_inventory = AsyncMock(
        return_value={"result": [{"type": "envoy"}]}
    )
    monkeypatch.setattr(
        coord,
        "_parse_devices_inventory_payload",
        lambda payload: (
            True,
            {"envoy": {"type_key": "envoy", "count": object(), "devices": [{}]}},
            ["envoy"],
        ),
    )
    await coord._async_refresh_devices_inventory(force=True)
    assert coord._devices_inventory_cache_until is not None  # noqa: SLF001


@pytest.mark.asyncio
async def test_hems_devices_refresh_cache_and_exception_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    assert coord.has_type_for_entities("envoy") is True

    coord._hems_devices_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    await coord._async_refresh_hems_devices()

    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = None
    await coord._async_refresh_hems_devices()

    coord.client._hems_site_supported = False  # noqa: SLF001
    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    await coord._async_refresh_hems_devices()
    coord.client.hems_devices.assert_not_awaited()
    assert coord._hems_devices_payload is None  # noqa: SLF001
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001

    coord.client._hems_site_supported = None  # noqa: SLF001
    coord._hems_support_preflight_cache_until = None  # noqa: SLF001
    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value={"is_hems": False})
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    await coord._async_refresh_hems_devices()
    coord.client.system_dashboard_summary.assert_awaited_once()
    coord.client.hems_devices.assert_not_awaited()
    assert coord.client.hems_site_supported is False
    assert coord._hems_devices_payload is None  # noqa: SLF001
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001

    coord.client._hems_site_supported = None  # noqa: SLF001
    coord._hems_support_preflight_cache_until = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value=None)
    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await coord._async_refresh_hems_devices()
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001
    assert coord._hems_devices_payload is None  # noqa: SLF001

    monkeypatch.setattr(coord, "_redact_battery_payload", lambda payload: "raw")
    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(
        return_value={"data": {"hems-devices": {"heat-pump": []}}}
    )
    await coord._async_refresh_hems_devices(force=True)
    assert coord._hems_devices_payload == {"value": "raw"}  # noqa: SLF001

    monkeypatch.setattr(coord, "_redact_battery_payload", lambda payload: payload)
    await coord._async_refresh_hems_devices(force=True)
    assert coord._hems_devices_payload == {  # noqa: SLF001
        "data": {"hems-devices": {"heat-pump": []}}
    }
    coord.client.hems_devices.assert_awaited_with(refresh_data=True)

    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord.client._hems_site_supported = True  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await coord._async_refresh_hems_devices()
    assert coord._hems_devices_payload == {  # noqa: SLF001
        "data": {"hems-devices": {"heat-pump": []}}
    }
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001
    assert coord._hems_devices_using_stale is True  # noqa: SLF001

    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._async_refresh_hems_devices(force=True)
    assert coord._hems_devices_payload == {  # noqa: SLF001
        "data": {"hems-devices": {"heat-pump": []}}
    }
    assert coord._hems_devices_using_stale is True  # noqa: SLF001

    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord._hems_devices_last_success_mono = (  # noqa: SLF001
        time.monotonic() - HEMS_DEVICES_STALE_AFTER_S - 1
    )
    coord.client._hems_site_supported = True  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await coord._async_refresh_hems_devices()
    assert coord._hems_devices_payload is None  # noqa: SLF001
    assert coord._hems_devices_using_stale is False  # noqa: SLF001
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001

    coord.client._hems_site_supported = False  # noqa: SLF001
    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await coord._async_refresh_hems_devices()
    assert coord._hems_devices_payload is None  # noqa: SLF001
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001

    coord.client._hems_site_supported = False  # noqa: SLF001
    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord._hems_devices_payload = {
        "data": {"hems-devices": {"heat-pump": []}}
    }  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._async_refresh_hems_devices(force=True)
    assert coord._hems_devices_payload is None  # noqa: SLF001
    assert coord._hems_devices_using_stale is False  # noqa: SLF001
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001

    coord.client._hems_site_supported = False  # noqa: SLF001
    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord._hems_devices_payload = {
        "data": {"hems-devices": {"heat-pump": []}}
    }  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await coord._async_refresh_hems_devices()
    assert coord._hems_devices_payload is None  # noqa: SLF001
    assert coord._hems_devices_using_stale is False  # noqa: SLF001
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001

    coord.client._hems_site_supported = None  # noqa: SLF001
    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord._hems_devices_payload = {
        "data": {"hems-devices": {"heat-pump": []}}
    }  # noqa: SLF001
    coord._hems_devices_last_success_mono = "bad"  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await coord._async_refresh_hems_devices()
    assert coord._hems_devices_payload is None  # noqa: SLF001

    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord._hems_devices_payload = {
        "data": {"hems-devices": {"heat-pump": []}}
    }  # noqa: SLF001
    coord._hems_devices_last_success_mono = "bad"  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._async_refresh_hems_devices(force=True)
    assert coord._hems_devices_payload is None  # noqa: SLF001
    assert coord._hems_devices_using_stale is False  # noqa: SLF001
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001

    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord._hems_devices_payload = {
        "data": {"hems-devices": {"heat-pump": []}}
    }  # noqa: SLF001
    coord._hems_devices_last_success_mono = time.monotonic() + 30  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await coord._async_refresh_hems_devices()
    assert coord._hems_devices_payload == {  # noqa: SLF001
        "data": {"hems-devices": {"heat-pump": []}}
    }
    assert coord._hems_devices_using_stale is True  # noqa: SLF001

    coord.client._hems_site_supported = False  # noqa: SLF001
    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord._hems_devices_payload = {
        "data": {"hems-devices": {"heat-pump": []}}
    }  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await coord._async_refresh_hems_devices(force=True)
    assert coord._hems_devices_payload is None  # noqa: SLF001
    assert coord._hems_devices_using_stale is False  # noqa: SLF001
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001

    coord.client._hems_site_supported = None  # noqa: SLF001
    coord._hems_support_preflight_cache_until = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value=None)
    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord._hems_devices_payload = {
        "data": {"hems-devices": {"heat-pump": []}}
    }  # noqa: SLF001

    async def _mark_unsupported_and_raise(*, refresh_data=False):
        del refresh_data
        coord.client._hems_site_supported = False  # noqa: SLF001
        raise RuntimeError("unsupported")

    coord.client.hems_devices = AsyncMock(side_effect=_mark_unsupported_and_raise)
    await coord._async_refresh_hems_devices(force=True)
    assert coord._hems_devices_payload is None  # noqa: SLF001
    assert coord._hems_devices_using_stale is False  # noqa: SLF001
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001

    coord.client._hems_site_supported = None  # noqa: SLF001
    coord._hems_support_preflight_cache_until = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value=None)
    coord._hems_devices_cache_until = None  # noqa: SLF001
    coord._hems_devices_payload = {
        "data": {"hems-devices": {"heat-pump": []}}
    }  # noqa: SLF001

    async def _mark_unsupported_and_return_none(*, refresh_data=False):
        del refresh_data
        coord.client._hems_site_supported = False  # noqa: SLF001
        return None

    coord.client.hems_devices = AsyncMock(side_effect=_mark_unsupported_and_return_none)
    await coord._async_refresh_hems_devices(force=True)
    assert coord._hems_devices_payload is None  # noqa: SLF001
    assert coord._hems_devices_using_stale is False  # noqa: SLF001
    assert coord._hems_inventory_ready is True  # noqa: SLF001
    assert coord._hems_devices_cache_until is not None  # noqa: SLF001


def test_type_bucket_includes_extra_summary_fields(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"serial_number": "INV1", "sku_id": "IQ7A-SKU"}],
            "model_summary": "IQ7A x1",
            "status_summary": "Normal 1 | Warning 0 | Error 0 | Not Reporting 0",
            "status_counts": {
                "normal": 1,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
            },
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001

    bucket = coord.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["model_summary"] == "IQ7A x1"
    assert "status_counts" in bucket
    assert coord.type_device_model("microinverter") == "IQ7A-SKU"
    assert coord.type_device_hw_version("microinverter") == "IQ7A-SKU"


def test_type_device_envoy_prefers_system_controller_metadata(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 3,
                "devices": [
                    {
                        "name": "IQ System Controller 3 INT",
                        "channelType": "enpower_controller_v3",
                        "serial_number": "SC-123",
                        "sku_id": "SC-SKU",
                        "sw_version": "8.2.1",
                    },
                    {
                        "name": "Consumption Meter",
                        "channel_type": "consumption_meter",
                        "serial_number": "CM-123",
                    },
                    {
                        "name": "Production Meter",
                        "channel_type": "production_meter",
                        "serial_number": "PM-123",
                    },
                ],
            }
        },
        ["envoy"],
    )

    assert coord.type_device_name("envoy") == "IQ Gateway"
    assert coord.type_device_model("envoy") == "IQ System Controller 3 INT"
    assert coord.type_device_serial_number("envoy") == "SC-123"
    assert coord.type_device_model_id("envoy") == "SC-SKU"
    assert coord.type_device_sw_version("envoy") == "8.2.1"

    info = coord.type_device_info("envoy")
    assert info is not None
    assert info["name"] == "IQ Gateway"
    assert info["model"] == "IQ System Controller 3 INT"
    assert info["serial_number"] == "SC-123"
    assert info["model_id"] == "SC-SKU"
    assert info["sw_version"] == "8.2.1"

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway", "serial_number": "GW-1"}],
            }
        },
        ["envoy"],
    )
    assert coord.type_device_name("envoy") == "IQ Gateway"
    assert coord.type_device_model("envoy") == "IQ Gateway"
    assert (
        coord._envoy_member_kind({"name": "System Controller Main"}) == "controller"
    )  # noqa: SLF001
    assert (
        coord._envoy_member_kind(
            {"channel_type": "system_controller_3"}
        )  # noqa: SLF001
        == "controller"
    )
    assert (
        coord._envoy_member_kind({"name": "Main Controller"}) == "controller"
    )  # noqa: SLF001
    assert (
        coord._envoy_member_kind({"name": "Production Meter"}) == "production"
    )  # noqa: SLF001
    assert (
        coord._envoy_member_kind({"name": "Consumption Meter"}) == "consumption"
    )  # noqa: SLF001


def test_type_device_summary_helpers_for_battery_and_microinverter(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 3,
                "devices": [
                    {
                        "serial_number": "BAT-1",
                        "sku_id": "IQ-BAT-5P",
                        "sw_version": "1.0",
                    },
                    {
                        "serial_number": "BAT-2",
                        "sku_id": "IQ-BAT-5P",
                        "sw_version": "1.0",
                    },
                    {
                        "serial_number": "BAT-3",
                        "sku_id": "IQ-BAT-3T",
                        "sw_version": "2.0",
                    },
                ],
            },
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 3,
                "devices": [
                    {"serial_number": "INV-1", "sku_id": "IQ8M", "fw1": "4.0"},
                    {"serial_number": "INV-2", "sku_id": "IQ8M", "fw1": "4.0"},
                    {"serial_number": "INV-3", "sku_id": "IQ8A", "fw2": "5.0"},
                ],
            },
        },
        ["encharge", "microinverter"],
    )

    assert coord.type_device_serial_number("encharge") is None
    assert coord.type_device_model_id("encharge") is None
    assert coord.type_device_sw_version("encharge") is None
    assert coord.type_device_model_id("microinverter") is None
    assert coord.type_device_hw_version("microinverter") is None
    assert coord.type_device_sw_version("microinverter") is None


def test_type_device_helper_branches_for_mac_and_labels(hass, monkeypatch) -> None:
    from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

    coord = _make_coordinator(hass, monkeypatch)

    assert (
        coord._type_member_summary([{"name": "A"}, {"name": "A"}], "name") == "A x2"
    )  # noqa: SLF001

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert coord._normalize_mac(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_mac("   ") is None  # noqa: SLF001
    assert coord._normalize_mac("aa:bb:cc") is None  # noqa: SLF001
    assert coord._normalize_mac("001:bb:cc:dd:ee:ff") is None  # noqa: SLF001
    assert coord._normalize_mac("zz:bb:cc:dd:ee:ff") is None  # noqa: SLF001
    assert coord._normalize_mac("a:b:c:d:e:f") == "0a:0b:0c:0d:0e:0f"  # noqa: SLF001
    assert coord._normalize_mac("AABBCCDDEEFF") == "aa:bb:cc:dd:ee:ff"  # noqa: SLF001
    assert coord._normalize_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"  # noqa: SLF001
    assert coord._normalize_mac("aabb.ccdd") is None  # noqa: SLF001
    assert coord._normalize_mac("aabb.ccdd.eefg") is None  # noqa: SLF001
    assert coord._normalize_mac("abcdef") is None  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [
                    {
                        "name": "IQ Gateway",
                        "channel_type": "system_controller",
                        "serial_number": "GW-1",
                        "mac": "aa-bb-cc-dd-ee-ff",
                    }
                ],
            }
        },
        ["envoy"],
    )
    assert coord._envoy_controller_mac() == "aa:bb:cc:dd:ee:ff"  # noqa: SLF001
    assert coord.type_device_info("envoy")["connections"] == {
        (CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff")
    }
    assert (
        coord._envoy_member_kind({"channel_type": "production_meter"}) == "production"
    )  # noqa: SLF001
    assert (
        coord._envoy_member_kind({"channel_type": "site_load"}) == "consumption"
    )  # noqa: SLF001

    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {"count": 1, "devices": []},
        "wind_turbine": {"count": 1, "devices": [], "type_label": 1},
    }
    coord._type_device_order = ["envoy", "wind_turbine"]  # noqa: SLF001
    assert coord._envoy_controller_mac() is None  # noqa: SLF001
    assert coord.type_device_name("wind_turbine") is None
    assert coord.type_device_info(None) is None


def test_type_device_model_prefers_model_identifiers_over_display_name(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Charger",
                "count": 2,
                "devices": [
                    {
                        "name": "Garage Charger",
                        "model_id": "IQ-EVSE-EU-3032-0105-1300",
                    },
                    {
                        "name": "Driveway Charger",
                        "model_id": "IQ-EVSE-EU-3032-0105-1300",
                    },
                ],
            }
        },
        ["iqevse"],
    )

    assert coord.type_device_model("iqevse") == "IQ-EVSE-EU-3032-0105-1300"


def test_type_device_model_id_omits_redundant_variants(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Charger",
                "count": 1,
                "devices": [
                    {
                        "model": "IQ EV Charger (IQ-EVSE-EU-3032)",
                        "model_id": "IQ-EVSE-EU-3032-0105-1300",
                    }
                ],
            },
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"sku_id": "B05-T02-ROW00-1-2"}],
            },
        },
        ["iqevse", "encharge"],
    )

    assert coord.type_device_model_id("iqevse") is None
    assert coord.type_device_model_id("encharge") is None


def test_sum_session_energy_rounds_to_two_decimals_without_session_manager(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    if hasattr(coord, "session_history"):
        delattr(coord, "session_history")

    assert (
        coord._sum_session_energy([{"energy_kwh": 1.234}, {"energy_kwh": 2.345}])
        == 3.58
    )  # noqa: SLF001


def test_coerce_optional_kwh_falls_back_when_round_raises(hass, monkeypatch) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)
    monkeypatch.setattr(
        coord_mod,
        "round",
        lambda _value, _precision: (_ for _ in ()).throw(ValueError("boom")),
        raising=False,
    )
    assert coord._coerce_optional_kwh("1.234") == 1.234  # noqa: SLF001


def test_inverter_helpers_cover_edge_paths(hass, monkeypatch) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("bad")

    class BadLastReport:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    coord = _make_coordinator(hass, monkeypatch)
    assert coord._coerce_int(None, default=7) == 7  # noqa: SLF001
    assert coord._coerce_int(True) == 1  # noqa: SLF001
    assert coord._coerce_int("8.2") == 8  # noqa: SLF001
    assert coord._coerce_int("not-a-number", default=5) == 5  # noqa: SLF001

    assert coord._normalize_iso_date(None) is None  # noqa: SLF001
    assert coord._normalize_iso_date("  ") is None  # noqa: SLF001
    assert coord._normalize_iso_date("2026-02-09") == "2026-02-09"  # noqa: SLF001
    assert coord._normalize_iso_date("bad-date") is None  # noqa: SLF001
    assert coord._normalize_iso_date(BadStr()) is None  # noqa: SLF001

    assert (
        coord._format_inverter_model_summary({"": 1, "IQ7A": "x", "IQ8": 0}) is None
    )  # noqa: SLF001
    assert coord._normalize_inverter_status("normal") == "normal"  # noqa: SLF001
    assert coord._normalize_inverter_status("recommended") == "normal"  # noqa: SLF001
    assert coord._normalize_inverter_status("warning") == "warning"  # noqa: SLF001
    assert coord._normalize_inverter_status("critical error") == "error"  # noqa: SLF001
    assert (
        coord._normalize_inverter_status("not reporting") == "not_reporting"
    )  # noqa: SLF001
    assert coord._normalize_inverter_status("  ") == "unknown"  # noqa: SLF001
    assert coord._normalize_inverter_status("mystery") == "unknown"  # noqa: SLF001
    assert coord._normalize_inverter_status(BadStr()) == "unknown"  # noqa: SLF001
    assert coord._normalize_inverter_status(None) == "unknown"  # noqa: SLF001
    assert (
        coord._inverter_connectivity_state({"total": 2, "not_reporting": 0}) == "online"
    )  # noqa: SLF001
    assert (
        coord._inverter_connectivity_state({"total": 2, "not_reporting": 1})
        == "degraded"
    )  # noqa: SLF001
    assert (
        coord._inverter_connectivity_state({"total": 2, "not_reporting": 2})
        == "offline"
    )  # noqa: SLF001
    assert (
        coord._inverter_connectivity_state(
            {"total": 2, "not_reporting": 0, "unknown": 2}
        )
        == "unknown"
    )  # noqa: SLF001
    assert coord._inverter_connectivity_state({"total": 0}) is None  # noqa: SLF001
    assert coord._parse_inverter_last_report(None) is None  # noqa: SLF001
    assert coord._parse_inverter_last_report("   ") is None  # noqa: SLF001
    assert (
        coord._parse_inverter_last_report("2026-02-09T00:00:00Z") is not None
    )  # noqa: SLF001
    assert (
        coord._parse_inverter_last_report("2026-02-09T00:00:00Z[UTC]") is not None
    )  # noqa: SLF001
    assert (
        coord._parse_inverter_last_report(1_780_000_000_000) is not None
    )  # noqa: SLF001
    assert (
        coord._parse_inverter_last_report(datetime(2026, 2, 9, 0, 0, 0)).tzinfo
        is not None
    )  # noqa: SLF001
    assert coord._parse_inverter_last_report(float("inf")) is None  # noqa: SLF001
    assert coord._parse_inverter_last_report("bad") is None  # noqa: SLF001
    assert coord._parse_inverter_last_report(BadLastReport()) is None  # noqa: SLF001

    class FakeFloatMeta(type):
        def __instancecheck__(cls, instance) -> bool:  # noqa: N805, ANN001
            return isinstance(instance, (int, float))

    class FakeFloat(metaclass=FakeFloatMeta):
        def __new__(cls, _value):  # noqa: ANN204, ANN001
            return None

    monkeypatch.setattr(coord_mod, "float", FakeFloat, raising=False)
    assert coord._parse_inverter_last_report(0) is None  # noqa: SLF001

    coord.energy._site_energy_meta = {}  # noqa: SLF001
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": "bad-payload",
        "INV-B": {"lifetime_query_start_date": "2024-01-01"},
    }
    assert coord._inverter_start_date() == "2024-01-01"  # noqa: SLF001
    coord._inverter_data = {}  # noqa: SLF001
    assert coord._inverter_start_date() is None  # noqa: SLF001

    coord._devices_inventory_payload = {"curr_date_site": "2026-02-08"}  # noqa: SLF001
    assert coord._site_local_current_date() == "2026-02-08"  # noqa: SLF001
    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": ["bad-item", {"curr_date_site": "2026-02-09"}]
    }
    assert coord._site_local_current_date() == "2026-02-09"  # noqa: SLF001
    coord._devices_inventory_payload = {}  # noqa: SLF001
    coord._battery_timezone = "Pacific/Auckland"  # noqa: SLF001
    assert (
        coord._site_local_current_date()
        == datetime.now(ZoneInfo("Pacific/Auckland")).date().isoformat()
    )  # noqa: SLF001
    coord._battery_timezone = "bad/tz"  # noqa: SLF001
    monkeypatch.setattr(
        coord_mod.dt_util,
        "now",
        lambda: datetime(2026, 2, 9, tzinfo=timezone.utc),
    )
    assert coord._site_local_current_date() == "2026-02-09"  # noqa: SLF001
    monkeypatch.setattr(
        coord_mod.dt_util,
        "now",
        lambda: (_ for _ in ()).throw(RuntimeError("clock")),
    )
    assert (
        coord._site_local_current_date()
        == datetime.now(tz=timezone.utc).date().isoformat()
    )  # noqa: SLF001


def test_merge_microinverter_bucket_skips_non_dict_payload(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._devices_inventory_ready = False  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"serial_number": "INV-A"}],
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    coord.iter_inverter_serials = lambda: ["INV-A"]  # type: ignore[assignment]
    coord.inverter_data = lambda _serial: None  # type: ignore[assignment]

    coord._merge_microinverter_type_bucket()  # noqa: SLF001

    assert coord.type_bucket("microinverter") is None
    assert coord._devices_inventory_ready is False  # noqa: SLF001


def test_merge_microinverter_bucket_handles_bad_array_and_firmware_values(
    hass, monkeypatch
) -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("bad")

    coord = _make_coordinator(hass, monkeypatch)
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {
            "serial_number": "INV-A",
            "name": "IQ7A",
            "array_name": BadStr(),
            "fw1": BadStr(),
            "last_report": 1_780_000_000,
        }
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    coord._inverter_model_counts = {"IQ7A": 1}  # noqa: SLF001
    coord._inverter_summary_counts = {  # noqa: SLF001
        "total": 1,
        "normal": 1,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
    }

    coord._merge_microinverter_type_bucket()  # noqa: SLF001

    bucket = coord.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["latest_reported_utc"] is not None
    assert bucket["array_counts"] == {}
    assert bucket["firmware_counts"] == {}


def test_heatpump_helper_static_branches(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert coord._devices_inventory_buckets([{"ok": 1}, "bad"]) == [  # noqa: SLF001
        {"ok": 1}
    ]
    assert coord._devices_inventory_buckets(  # noqa: SLF001
        {"value": {"result": [{"ok": 2}, "bad"]}}
    ) == [{"ok": 2}]
    assert (
        coord._hems_devices_groups({"data": {"hems-devices": []}}) == []
    )  # noqa: SLF001
    assert coord._hems_devices_groups(
        {"data": {"hems_devices": {"gateway": []}}}
    ) == [  # noqa: SLF001
        {"gateway": []}
    ]
    assert coord._hems_devices_groups(  # noqa: SLF001
        {"result": {"devices": [{"heat-pump": []}, "bad"]}}
    ) == [{"heat-pump": []}]
    assert coord._hems_devices_groups(  # noqa: SLF001
        {"result": {"devices": {"gateway": []}}}
    ) == [{"gateway": []}]

    normalized = coord._normalize_heatpump_member(  # noqa: SLF001
        {"device-uid": "HP-1", "serial": "SER-1"}
    )
    assert normalized["uid"] == "HP-1"
    assert normalized["serial_number"] == "SER-1"

    assert coord._hems_bucket_type(BadStr()) is None  # noqa: SLF001
    assert coord._hems_bucket_type("   ") is None  # noqa: SLF001
    assert coord._heatpump_member_device_type(None) is None  # noqa: SLF001
    assert coord._heatpump_member_device_type({}) is None  # noqa: SLF001
    assert (
        coord._heatpump_member_device_type({"device_type": BadStr()}) is None
    )  # noqa: SLF001

    assert coord._heatpump_worst_status_text({"error": 1}) == "Error"  # noqa: SLF001
    assert (
        coord._heatpump_worst_status_text({"warning": 1}) == "Warning"
    )  # noqa: SLF001
    assert (  # noqa: SLF001
        coord._heatpump_worst_status_text({"not_reporting": 1}) == "Not Reporting"
    )
    assert (
        coord._heatpump_worst_status_text({"unknown": 1}) == "Unknown"
    )  # noqa: SLF001
    assert coord._heatpump_worst_status_text({"normal": 1}) == "Normal"  # noqa: SLF001
    assert coord._heatpump_worst_status_text({}) is None  # noqa: SLF001


def test_merge_heatpump_bucket_from_hems_inventory(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
            }
        },
        ["envoy"],
    )
    coord._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "heat-pump": [
                    {
                        "device-type": "SG_READY_GATEWAY",
                        "device-uid": "HP-SG-1",
                        "name": "SG Ready Gateway",
                        "last-report": "2026-02-27T09:14:44Z",
                        "status": "normal",
                        "statusText": "Normal",
                        "model": "Expert Net Control 2302",
                    },
                    {
                        "device-type": "ENERGY_METER",
                        "device-uid": "HP-EM-1",
                        "name": "Energy Meter",
                        "last-report": "2026-02-27T09:15:44Z",
                        "statusText": "Warning",
                        "firmware-version": "3.3",
                        "model": "Energy Manager 420",
                    },
                    {
                        "device-type": "HEAT_PUMP",
                        "device-uid": "HP-1",
                        "name": "Waermepumpe",
                        "statusText": "Normal",
                        "model": "Europa Mini WP",
                        "hardware-sku": "HP-SKU-1",
                    },
                    {
                        "device-type": "ENERGY_METER",
                        "device-uid": "HP-EM-2",
                        "name": "Retired Meter",
                        "statusText": "Retired",
                    },
                ]
            }
        }
    }

    coord._merge_heatpump_type_bucket()  # noqa: SLF001

    bucket = coord.type_bucket("heatpump")
    assert bucket is not None
    assert bucket["count"] == 3
    assert bucket["type_label"] == "Heat Pump"
    assert bucket["status_counts"]["total"] == 3
    assert bucket["status_counts"]["warning"] == 1
    assert bucket["status_summary"].startswith("Normal")
    assert bucket["model_summary"] is not None
    assert bucket["firmware_summary"] == "3.3 x1"
    assert bucket["latest_reported_utc"] is not None
    assert bucket["latest_reported_device"]["device_uid"] == "HP-EM-1"
    assert bucket["overall_status_text"] == "Normal"
    assert "heatpump" in coord.iter_type_keys()
    assert coord.type_device_name("heatpump") == "Heat Pump"
    assert coord.type_device_model("heatpump") == "Europa Mini WP"
    assert coord.type_device_model_id("heatpump") == "HP-SKU-1"
    assert coord.type_device_sw_version("heatpump") == "3.3"
    assert coord.type_device_hw_version("heatpump") == "HP-SKU-1"
    assert coord.type_device_serial_number("heatpump") == "HP-1"
    info = coord.type_device_info("heatpump")
    assert info is not None
    assert info["name"] == "Heat Pump"
    assert info["model"] == "Europa Mini WP"
    assert info["serial_number"] == "HP-1"


def test_merge_heatpump_bucket_from_documented_hems_inventory_shape(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
            }
        },
        ["envoy"],
    )
    coord._hems_devices_payload = {  # noqa: SLF001
        "status": "success",
        "result": {
            "devices": [
                {
                    "heat-pump": [
                        {
                            "device-type": "HEAT_PUMP",
                            "device-uid": "HP-1",
                            "name": "Documented Shape Heat Pump",
                            "statusText": "Normal",
                            "model": "Dedicated Model",
                        }
                    ]
                }
            ]
        },
    }

    coord._merge_heatpump_type_bucket()  # noqa: SLF001

    bucket = coord.type_bucket("heatpump")
    assert bucket is not None
    assert bucket["count"] == 1
    assert bucket["devices"][0]["device_uid"] == "HP-1"
    assert coord.type_device_model("heatpump") == "Dedicated Model"


def test_merge_heatpump_bucket_prefers_dedicated_hems_inventory_over_legacy(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
            }
        },
        ["envoy"],
    )
    coord._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "heat-pump": [
                    {
                        "device-type": "HEAT_PUMP",
                        "device-uid": "HP-NEW",
                        "name": "Dedicated Heat Pump",
                        "statusText": "Normal",
                        "model": "Dedicated Model",
                    }
                ]
            }
        }
    }
    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": [
            {
                "type": "hemsDevices",
                "devices": [
                    {
                        "heat-pump": [
                            {
                                "device-type": "HEAT_PUMP",
                                "device-uid": "HP-OLD",
                                "name": "Legacy Heat Pump",
                                "statusText": "Warning",
                                "model": "Legacy Model",
                            }
                        ]
                    }
                ],
            }
        ]
    }

    coord._merge_heatpump_type_bucket()  # noqa: SLF001

    bucket = coord.type_bucket("heatpump")
    assert bucket is not None
    assert bucket["count"] == 1
    assert bucket["devices"][0]["device_uid"] == "HP-NEW"
    assert coord.type_device_model("heatpump") == "Dedicated Model"


def test_hems_group_members_falls_back_to_legacy_when_dedicated_group_missing(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._hems_devices_payload = {  # noqa: SLF001
        "data": {"hems-devices": {"heat-pump": [{"device-uid": "HP-1"}]}}
    }
    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": [
            {
                "type": "hemsDevices",
                "devices": [
                    {
                        "gateway": [
                            {
                                "device-type": "IQ_ENERGY_ROUTER",
                                "device-uid": "ROUTER-1",
                            }
                        ]
                    }
                ],
            }
        ]
    }

    members = coord._hems_group_members("gateway")  # noqa: SLF001

    assert len(members) == 1
    assert members[0]["device_uid"] == "ROUTER-1"

    coord._hems_devices_payload = {
        "data": {"hems-devices": {"gateway": []}}
    }  # noqa: SLF001
    assert coord._hems_grouped_devices() == [{"gateway": []}]  # noqa: SLF001


def test_hems_grouped_devices_returns_legacy_when_dedicated_missing(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._hems_devices_payload = None  # noqa: SLF001
    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": [
            {
                "type": "hemsDevices",
                "devices": [{"gateway": [{"device-uid": "ROUTER-1"}]}],
            }
        ]
    }

    assert coord._hems_grouped_devices() == [  # noqa: SLF001
        {"gateway": [{"device-uid": "ROUTER-1"}]}
    ]


def test_merge_heatpump_bucket_covers_invalid_groups_and_fallback_status(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
            },
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": "EV-1"}],
            },
        },
        ["envoy", "iqevse"],
    )
    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": [
            {"type": "hemsDevices", "devices": {"bad": "shape"}},
            {
                "type": "hemsDevices",
                "devices": [
                    "not-a-dict",
                    {"heat-pump": "not-a-list"},
                    {
                        "heat-pump": [
                            "bad-member",
                            {},
                            {
                                "device-type": "ENERGY_METER",
                                "device-uid": "HP-EM-1",
                                "statusText": "Warning",
                            },
                            {
                                "device-type": "ENERGY_METER",
                                "device-uid": "HP-EM-1",
                                "statusText": "Warning",
                            },
                            {
                                "device-type": "SG_READY_GATEWAY",
                                "status": "normal",
                            },
                        ]
                    },
                ],
            },
        ]
    }

    coord._merge_heatpump_type_bucket()  # noqa: SLF001

    bucket = coord.type_bucket("heatpump")
    assert bucket is not None
    assert bucket["count"] == 2
    assert bucket["overall_status_text"] == "Warning"
    assert bucket["status_counts"]["warning"] == 1
    assert bucket["status_counts"]["normal"] == 1
    assert coord._type_device_order == ["envoy", "iqevse", "heatpump"]  # noqa: SLF001


def test_merge_heatpump_bucket_removes_stale_bucket_when_no_members(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)
    coord._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "type_key": "heatpump",
            "type_label": "Heat Pump",
            "count": 1,
            "devices": [{"device_uid": "HP-1"}],
        }
    }
    coord._type_device_order = ["heatpump"]  # noqa: SLF001
    coord._devices_inventory_payload = {
        "result": [{"type": "hemsDevices", "devices": []}]
    }  # noqa: SLF001

    coord._merge_heatpump_type_bucket()  # noqa: SLF001

    assert coord.type_bucket("heatpump") is None
    assert "heatpump" not in coord.iter_type_keys()


@pytest.mark.asyncio
async def test_refresh_heatpump_power_tracks_latest_valid_sample(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "statusText": "Normal",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_power_timeseries = AsyncMock(
        return_value={
            "heat_pump_consumption": [None, 400.0, "500.5", None],
            "start_date": "2026-02-27T00:00:00Z",
            "interval_minutes": 5,
        }
    )

    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(500.5)
    assert coord.heatpump_power_device_uid == "HP-1"
    assert coord.heatpump_power_source == "hems_power_timeseries:HP-1"
    assert coord.heatpump_power_start_utc is not None
    assert coord.heatpump_power_sample_utc is not None
    assert coord.heatpump_power_last_error is None
    assert coord._heatpump_power_cache_until is not None  # noqa: SLF001
    first_call = coord.client.hems_power_timeseries.await_args_list[0]
    assert first_call.kwargs["device_uid"] == "HP-1"
    assert first_call.kwargs["site_date"] == "2026-03-13"

    coord._heatpump_power_cache_until = None  # noqa: SLF001
    coord.client.hems_power_timeseries = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    assert coord.heatpump_power_last_error == "boom"
    assert coord._heatpump_power_backoff_until is not None  # noqa: SLF001

    coord._set_type_device_buckets({}, [])  # noqa: SLF001
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    assert coord.heatpump_power_w is None
    assert coord.heatpump_power_source is None


@pytest.mark.asyncio
async def test_refresh_current_power_consumption_tracks_latest_sample(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.client.latest_power = AsyncMock(
        return_value={
            "value": 752.0,
            "units": "W",
            "precision": 0,
            "time": 1_773_207_600,
        }
    )

    await coord._async_refresh_current_power_consumption()  # noqa: SLF001

    assert coord.current_power_consumption_w == pytest.approx(752.0)
    assert coord.current_power_consumption_reported_units == "W"
    assert coord.current_power_consumption_reported_precision == 0
    assert coord.current_power_consumption_source == "app-api:get_latest_power"
    assert coord.current_power_consumption_sample_utc == datetime(
        2026, 3, 11, 5, 40, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_refresh_current_power_consumption_clears_invalid_or_failed_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._current_power_consumption_w = 123.0  # noqa: SLF001
    coord._current_power_consumption_sample_utc = datetime.now(
        timezone.utc
    )  # noqa: SLF001
    coord._current_power_consumption_reported_units = "W"  # noqa: SLF001
    coord._current_power_consumption_reported_precision = 1  # noqa: SLF001
    coord._current_power_consumption_source = "stale"  # noqa: SLF001

    coord.client.latest_power = AsyncMock(return_value=None)
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_w is None
    assert coord.current_power_consumption_source is None

    coord._current_power_consumption_w = 456.0  # noqa: SLF001
    coord.client.latest_power = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_w is None
    assert coord.current_power_consumption_sample_utc is None


@pytest.mark.asyncio
async def test_refresh_current_power_consumption_handles_edge_payload_shapes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])

    coord.client.latest_power = AsyncMock(return_value={"value": object()})
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_w is None

    coord.client.latest_power = AsyncMock(return_value={"value": float("nan")})
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_w is None

    class BadString:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord.client.latest_power = AsyncMock(
        return_value={
            "value": 752.0,
            "units": BadString(),
            "precision": "bad",
            "time": "1773207600000",
        }
    )
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_w == pytest.approx(752.0)
    assert coord.current_power_consumption_sample_utc == datetime(
        2026, 3, 11, 5, 40, tzinfo=timezone.utc
    )
    assert coord.current_power_consumption_reported_units is None
    assert coord.current_power_consumption_reported_precision is None

    coord.client.latest_power = AsyncMock(
        return_value={
            "value": 753.0,
            "units": "   ",
            "precision": object(),
            "time": "nan",
        }
    )
    await coord._async_refresh_current_power_consumption()  # noqa: SLF001
    assert coord.current_power_consumption_w == pytest.approx(753.0)
    assert coord.current_power_consumption_sample_utc is None
    assert coord.current_power_consumption_reported_units is None
    assert coord.current_power_consumption_reported_precision is None


def test_current_power_consumption_property_guards(coordinator_factory) -> None:
    coord = coordinator_factory(serials=[])

    coord._current_power_consumption_w = object()  # noqa: SLF001
    assert coord.current_power_consumption_w is None

    coord._current_power_consumption_w = float("inf")  # noqa: SLF001
    assert coord.current_power_consumption_w is None

    coord._current_power_consumption_reported_units = None  # noqa: SLF001
    assert coord.current_power_consumption_reported_units is None

    class BadString:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord._current_power_consumption_reported_units = BadString()  # noqa: SLF001
    assert coord.current_power_consumption_reported_units is None

    coord._current_power_consumption_reported_precision = None  # noqa: SLF001
    assert coord.current_power_consumption_reported_precision is None
    coord._current_power_consumption_reported_precision = object()  # noqa: SLF001
    assert coord.current_power_consumption_reported_precision is None

    coord._current_power_consumption_source = BadString()  # noqa: SLF001
    assert coord.current_power_consumption_source is None


def test_heatpump_primary_helpers_and_metadata_fallbacks(coordinator_factory) -> None:
    coord = coordinator_factory(serials=[])
    assert coord._heatpump_primary_member() is None  # noqa: SLF001
    assert coord._heatpump_primary_device_uid() is None  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "ENERGY_METER", "name": "No UID"}],
            }
        },
        ["heatpump"],
    )
    assert coord._heatpump_primary_device_uid() is None  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {"device_type": "ENERGY_METER", "name": "Primary meter"},
                    {"device_type": "SG_READY_GATEWAY", "device_uid": "HP-SG-1"},
                ],
            }
        },
        ["heatpump"],
    )
    assert (
        coord._heatpump_primary_member()["device_type"] == "ENERGY_METER"
    )  # noqa: SLF001
    assert coord._heatpump_primary_device_uid() == "HP-SG-1"  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "OTHER", "device_uid": "HP-DEVICE-UID"}],
            }
        },
        ["heatpump"],
    )
    assert coord._heatpump_primary_device_uid() == "HP-DEVICE-UID"  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "OTHER", "uid": "HP-FALLBACK"}],
            }
        },
        ["heatpump"],
    )
    assert coord._heatpump_primary_device_uid() is None  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {"device_type": "ENERGY_METER"},
                    {"device_type": "SG_READY_GATEWAY", "model": "Model-B"},
                ],
            }
        },
        ["heatpump"],
    )
    assert coord.type_device_model("heatpump") == "Model-B x1"

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "ENERGY_METER"}],
            }
        },
        ["heatpump"],
    )
    assert coord.type_device_model("heatpump") == "Heat Pump"

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {"device_type": "HEAT_PUMP"},
                    {"device_type": "SG_READY_GATEWAY", "device_uid": "SER-2"},
                ],
            }
        },
        ["heatpump"],
    )
    assert coord.type_device_serial_number("heatpump") == "SER-2"

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {"device_type": "HEAT_PUMP"},
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "hardware_sku": "SKU-ONLY",
                        "model": "Model X",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    assert coord.type_device_model_id("heatpump") == "SKU-ONLY"

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 3,
                "devices": [
                    {"device_type": "HEAT_PUMP"},
                    {
                        "device_type": "ENERGY_METER",
                        "model_id": "SKU-A",
                        "model": "Alpha",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "model_id": "SKU-B",
                        "model": "Beta",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    model_id_summary = coord.type_device_model_id("heatpump")
    assert model_id_summary is not None
    assert "SKU-A x1" in model_id_summary
    assert "SKU-B x1" in model_id_summary

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {"device_type": "HEAT_PUMP", "firmware_version": "9.1"},
                ],
            }
        },
        ["heatpump"],
    )
    assert coord.type_device_sw_version("heatpump") == "9.1"

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 3,
                "devices": [
                    {"device_type": "HEAT_PUMP"},
                    {"device_type": "ENERGY_METER", "firmware_version": "3.0"},
                    {"device_type": "SG_READY_GATEWAY", "firmware_version": "4.0"},
                ],
            }
        },
        ["heatpump"],
    )
    sw_summary = coord.type_device_sw_version("heatpump")
    assert sw_summary is not None
    assert "3.0 x1" in sw_summary
    assert "4.0 x1" in sw_summary

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {"device_type": "HEAT_PUMP"},
                    {"device_type": "ENERGY_METER", "part_number": "PN-1"},
                ],
            }
        },
        ["heatpump"],
    )
    assert coord.type_device_hw_version("heatpump") == "PN-1"

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 3,
                "devices": [
                    {"device_type": "HEAT_PUMP"},
                    {"device_type": "ENERGY_METER", "part_number": "PN-A"},
                    {"device_type": "SG_READY_GATEWAY", "part_number": "PN-B"},
                ],
            }
        },
        ["heatpump"],
    )
    hw_summary = coord.type_device_hw_version("heatpump")
    assert hw_summary is not None
    assert "PN-A x1" in hw_summary
    assert "PN-B x1" in hw_summary


def test_heatpump_power_helper_guards(coordinator_factory) -> None:
    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP"}],
            }
        },
        ["heatpump"],
    )

    assert coord._heatpump_power_candidate_device_uids() == [None]  # noqa: SLF001
    assert coord._heatpump_latest_power_sample(["not-a-dict"]) is None  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {"device_type": "HEAT_PUMP", "uid": "HP-UID"},
                    {"device_type": "ENERGY_METER", "serial_number": "HP-SERIAL"},
                ],
            }
        },
        ["heatpump"],
    )
    assert coord._heatpump_primary_device_uid() is None  # noqa: SLF001
    assert coord._heatpump_power_candidate_device_uids() == [None]  # noqa: SLF001


def test_heatpump_latest_power_sample_ignores_future_buckets(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    fixed_now = datetime(2026, 2, 27, 0, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: fixed_now)

    assert coord._heatpump_latest_power_sample(  # noqa: SLF001
        {
            "heat_pump_consumption": [560.0, 0.0, 0.0],
            "start_date": "2026-02-27T00:00:00Z",
            "interval_minutes": 5,
        }
    ) == (0, 560.0)

    assert (
        coord._heatpump_latest_power_sample(  # noqa: SLF001
            {
                "heat_pump_consumption": [560.0, 0.0],
                "start_date": "2026-02-28T00:00:00Z",
                "interval_minutes": 5,
            }
        )
        is None
    )


def test_heatpump_latest_power_sample_skips_open_zero_bucket(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    fixed_now = datetime(2026, 2, 27, 0, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: fixed_now)

    assert coord._heatpump_latest_power_sample(  # noqa: SLF001
        {
            "heat_pump_consumption": [560.0, 0.0, 0.0],
            "start_date": "2026-02-27T00:00:00Z",
            "interval_minutes": 5,
        }
    ) == (0, 560.0)


def test_heatpump_latest_power_sample_normalizes_naive_utcnow(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    monkeypatch.setattr(
        coord_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 2, 27, 0, 2),
    )

    assert coord._heatpump_latest_power_sample(  # noqa: SLF001
        {
            "heat_pump_consumption": [560.0, 0.0, 0.0],
            "start_date": "2026-02-27T00:00:00Z",
            "interval_minutes": 5,
        }
    ) == (0, 560.0)


def test_heatpump_latest_power_sample_infers_missing_interval(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    fixed_now = datetime(2026, 3, 18, 11, 24, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: fixed_now)
    values = [0.0] * 672
    values[620] = 610.0

    assert coord._heatpump_latest_power_sample(  # noqa: SLF001
        {
            "heat_pump_consumption": values,
            "start_date": "2026-03-12T00:00:00Z",
        }
    ) == (620, 610.0)


def test_heatpump_latest_power_sample_uses_float_interval_metadata(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    fixed_now = datetime(2026, 3, 18, 11, 24, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: fixed_now)
    values = [0.0] * 672
    values[620] = 610.0

    assert coord._heatpump_latest_power_sample(  # noqa: SLF001
        {
            "heat_pump_consumption": values,
            "start_date": "2026-03-12T00:00:00Z",
            "interval_minutes": 15.0,
        }
    ) == (620, 610.0)


def test_infer_heatpump_interval_minutes_uses_fallback_when_series_is_in_past(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])

    assert (
        coord._infer_heatpump_interval_minutes(  # noqa: SLF001
            datetime(2026, 3, 12, 0, 0, tzinfo=timezone.utc),
            672,
            datetime(2026, 4, 30, 0, 0, tzinfo=timezone.utc),
        )
        == 60
    )


def test_infer_heatpump_interval_minutes_handles_timedelta_failures(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    monkeypatch.setattr(
        coord_mod,
        "timedelta",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bad delta")),
    )

    assert (
        coord._infer_heatpump_interval_minutes(  # noqa: SLF001
            datetime(2026, 3, 12, 0, 0, tzinfo=timezone.utc),
            672,
            datetime(2026, 3, 18, 11, 24, tzinfo=timezone.utc),
        )
        is None
    )


def test_heatpump_power_helper_aliases_and_fetch_plan(coordinator_factory) -> None:
    coord = coordinator_factory(serials=[])

    assert coord._heatpump_member_aliases(None) == []  # noqa: SLF001
    assert coord._heatpump_member_parent_id(None) is None  # noqa: SLF001
    assert (
        coord._heatpump_power_candidate_is_recommended("HP-1") is False
    )  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "hems_device_id": "HP-GROUP",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "parent_uid": "HP-GROUP",
                        "statusText": "Normal",
                    },
                ],
            }
        },
        ["heatpump"],
    )

    marker = coord._heatpump_power_inventory_marker()  # noqa: SLF001
    assert ("HP-METER", "HP-CTRL", "ENERGY_METER", "normal") in marker

    coord._heatpump_power_selection_marker = marker  # noqa: SLF001
    coord._heatpump_power_device_uid = "HP-METER"  # noqa: SLF001
    ordered, compare_all, returned_marker = (
        coord._heatpump_power_fetch_plan()
    )  # noqa: SLF001
    assert compare_all is False
    assert returned_marker == marker
    assert ordered[0] == "HP-METER"


def test_heatpump_power_recommended_helper_parent_matching(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 3,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "hems_device_id": "HP-GROUP",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "parent_uid": "HP-GROUP",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG",
                        "parent_uid": "HP-GROUP",
                        "statusText": "Recommended",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    assert (
        coord._heatpump_power_candidate_is_recommended("HP-METER") is True
    )  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG",
                        "parent_uid": "HP-METER",
                        "statusText": "Recommended",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    assert (
        coord._heatpump_power_candidate_is_recommended("HP-METER") is True
    )  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "statusText": "Recommended",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "parent_uid": "HP-CTRL",
                        "statusText": "Normal",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    assert (
        coord._heatpump_power_candidate_is_recommended("HP-METER") is True
    )  # noqa: SLF001

    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "parent_uid": "HP-CTRL",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG",
                        "parent_uid": "OTHER-PARENT",
                        "statusText": "Recommended",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    assert (
        coord._heatpump_power_candidate_is_recommended("HP-METER") is False
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_heatpump_power_covers_cache_and_payload_edge_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP"}],
            }
        },
        ["heatpump"],
    )

    cached_fetch = AsyncMock(return_value={"heat_pump_consumption": [100.0]})
    coord.client.hems_power_timeseries = cached_fetch
    coord._heatpump_power_cache_until = time.monotonic() + 60  # noqa: SLF001
    await coord._async_refresh_heatpump_power()  # noqa: SLF001
    cached_fetch.assert_not_awaited()

    coord._heatpump_power_cache_until = None  # noqa: SLF001
    coord._heatpump_power_backoff_until = time.monotonic() + 60  # noqa: SLF001
    await coord._async_refresh_heatpump_power()  # noqa: SLF001
    cached_fetch.assert_not_awaited()

    coord._heatpump_power_backoff_until = None  # noqa: SLF001
    coord.client.hems_power_timeseries = None
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    coord.client._hems_site_supported = False  # noqa: SLF001
    blocked_fetch = AsyncMock(side_effect=AssertionError("no fetch"))
    coord.client.hems_power_timeseries = blocked_fetch
    await coord._async_refresh_heatpump_power()  # noqa: SLF001
    blocked_fetch.assert_not_awaited()
    assert coord.heatpump_power_w is None
    assert coord._heatpump_power_cache_until is not None  # noqa: SLF001

    coord.client._hems_site_supported = None  # noqa: SLF001
    coord._hems_support_preflight_cache_until = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value={"is_hems": False})
    blocked_fetch = AsyncMock(side_effect=AssertionError("no fetch"))
    coord.client.hems_power_timeseries = blocked_fetch
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    coord.client.system_dashboard_summary.assert_awaited_once()
    blocked_fetch.assert_not_awaited()
    assert coord.client.hems_site_supported is False
    assert coord.heatpump_power_w is None

    coord.client._hems_site_supported = None  # noqa: SLF001
    coord._hems_support_preflight_cache_until = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value=None)
    coord.client.hems_power_timeseries = AsyncMock(return_value="bad-payload")
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    assert coord.heatpump_power_w is None

    coord.client.hems_power_timeseries = AsyncMock(
        return_value={"uid": "HP-OVERRIDE", "heat_pump_consumption": "bad-values"}
    )
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    assert coord.heatpump_power_device_uid == "HP-OVERRIDE"
    assert coord.heatpump_power_w is None

    coord.client.hems_power_timeseries = AsyncMock(
        return_value={
            "heat_pump_consumption": [None, "not-a-number", float("nan"), float("inf")]
        }
    )
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    assert coord.heatpump_power_w is None


@pytest.mark.asyncio
async def test_refresh_heatpump_power_uses_short_cache_for_empty_payloads(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP"}],
            }
        },
        ["heatpump"],
    )
    mono_now = 1_000.0
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: mono_now)
    coord.client.hems_power_timeseries = AsyncMock(
        side_effect=[
            {"heat_pump_consumption": [None]},
            {"heat_pump_consumption": [725.0]},
        ]
    )

    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    assert coord.heatpump_power_w is None
    assert coord._heatpump_power_cache_until == pytest.approx(  # noqa: SLF001
        mono_now + HEATPUMP_POWER_CACHE_TTL
    )

    mono_now += HEATPUMP_POWER_CACHE_TTL + 1
    await coord._async_refresh_heatpump_power()  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(725.0)
    assert coord.client.hems_power_timeseries.await_count == 2


@pytest.mark.asyncio
async def test_hems_support_preflight_uses_short_live_status_cache(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    mono_now = 1_500.0
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: mono_now)
    coord.client.system_dashboard_summary = AsyncMock(return_value=None)

    await coord._async_refresh_hems_support_preflight()  # noqa: SLF001

    assert coord._hems_support_preflight_cache_until == pytest.approx(  # noqa: SLF001
        mono_now + HEMS_SUPPORT_PREFLIGHT_CACHE_TTL
    )

    mono_now += HEMS_SUPPORT_PREFLIGHT_CACHE_TTL + 1
    await coord._async_refresh_hems_support_preflight()  # noqa: SLF001

    assert coord.client.system_dashboard_summary.await_count == 2


@pytest.mark.asyncio
async def test_hems_devices_refresh_uses_short_cache_for_heatpump_status(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory()
    mono_now = 2_000.0
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: mono_now)
    monkeypatch.setattr(coord, "_redact_battery_payload", lambda payload: payload)
    coord.client._hems_site_supported = True  # noqa: SLF001
    coord._hems_support_preflight_cache_until = mono_now + 60  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(
        side_effect=[
            {
                "data": {
                    "hems-devices": {
                        "heat-pump": [
                            {
                                "device_type": "SG_READY_GATEWAY",
                                "device_uid": "HP-SG-1",
                                "statusText": "Normal",
                            }
                        ]
                    }
                }
            },
            {
                "data": {
                    "hems-devices": {
                        "heat-pump": [
                            {
                                "device_type": "SG_READY_GATEWAY",
                                "device_uid": "HP-SG-1",
                                "statusText": "Recommended",
                            }
                        ]
                    }
                }
            },
        ]
    )

    await coord._async_refresh_hems_devices(force=True)  # noqa: SLF001

    first_member = coord.type_bucket("heatpump")["devices"][0]
    assert first_member["statusText"] == "Normal"
    assert coord._hems_devices_cache_until == pytest.approx(  # noqa: SLF001
        mono_now + HEMS_DEVICES_CACHE_TTL
    )

    mono_now += HEMS_DEVICES_CACHE_TTL + 1
    await coord._async_refresh_hems_devices()  # noqa: SLF001

    second_member = coord.type_bucket("heatpump")["devices"][0]
    assert second_member["statusText"] == "Recommended"
    assert coord.client.hems_devices.await_count == 2


@pytest.mark.asyncio
async def test_refresh_heatpump_power_covers_sample_timestamp_fallbacks(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )

    monkeypatch.setattr(
        coord_mod,
        "timedelta",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bad delta")),
    )
    coord.client.hems_power_timeseries = AsyncMock(
        return_value={
            "heat_pump_consumption": [150.0],
            "start_date": "2026-02-27T00:00:00Z",
            "interval_minutes": 5,
        }
    )
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    assert coord.heatpump_power_w == pytest.approx(150.0)
    assert coord.heatpump_power_sample_utc is None

    fixed_now = datetime(2026, 2, 28, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: fixed_now)
    monkeypatch.setattr(coord_mod, "timedelta", timedelta)
    coord.client.hems_power_timeseries = AsyncMock(
        return_value={"heat_pump_consumption": [300.0]}
    )
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    assert coord.heatpump_power_sample_utc == fixed_now


@pytest.mark.asyncio
async def test_refresh_heatpump_power_ignores_future_zero_buckets(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    fixed_now = datetime(2026, 2, 27, 0, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: fixed_now)
    coord.client.hems_power_timeseries = AsyncMock(
        return_value={
            "device_uid": "HP-1",
            "heat_pump_consumption": [560.0, 0.0, 0.0],
            "start_date": "2026-02-27T00:00:00Z",
            "interval_minutes": 5,
        }
    )

    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(560.0)
    assert coord.heatpump_power_device_uid == "HP-1"
    assert coord.heatpump_power_source == "hems_power_timeseries:HP-1"
    assert coord.heatpump_power_sample_utc == datetime(
        2026, 2, 27, 0, 0, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_refresh_heatpump_power_infers_missing_interval_metadata(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    fixed_now = datetime(2026, 3, 18, 11, 24, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: fixed_now)
    values = [0.0] * 672
    values[620] = 610.0
    coord.client.hems_power_timeseries = AsyncMock(
        return_value={
            "device_uid": "HP-1",
            "heat_pump_consumption": values,
            "start_date": "2026-03-12T00:00:00Z",
        }
    )

    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(610.0)
    assert coord.heatpump_power_device_uid == "HP-1"
    assert coord.heatpump_power_source == "hems_power_timeseries:HP-1"
    assert coord.heatpump_power_sample_utc == datetime(
        2026, 3, 18, 11, 0, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_refresh_heatpump_power_uses_float_interval_metadata(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    fixed_now = datetime(2026, 3, 18, 11, 24, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: fixed_now)
    values = [0.0] * 672
    values[620] = 610.0
    coord.client.hems_power_timeseries = AsyncMock(
        return_value={
            "device_uid": "HP-1",
            "heat_pump_consumption": values,
            "start_date": "2026-03-12T00:00:00Z",
            "interval_minutes": 15.0,
        }
    )

    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(610.0)
    assert coord.heatpump_power_device_uid == "HP-1"
    assert coord.heatpump_power_source == "hems_power_timeseries:HP-1"
    assert coord.heatpump_power_sample_utc == datetime(
        2026, 3, 18, 11, 0, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_refresh_heatpump_power_infers_missing_interval_with_naive_now(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    monkeypatch.setattr(
        coord_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 3, 18, 11, 24),
    )
    values = [0.0] * 672
    values[620] = 610.0
    coord.client.hems_power_timeseries = AsyncMock(
        return_value={
            "device_uid": "HP-1",
            "heat_pump_consumption": values,
            "start_date": "2026-03-12T00:00:00Z",
        }
    )

    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(610.0)
    assert coord.heatpump_power_sample_utc == datetime(
        2026, 3, 18, 11, 0, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_heatpump_runtime_diagnostics_uses_expected_events_namespace(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 3,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG",
                        "name": "SG Ready Gateway",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "name": "Energy Meter",
                    },
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "name": "Waermepumpe",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.show_livestream = AsyncMock(return_value={"live_vitals": True})
    coord.client.heat_pump_events_json = AsyncMock(return_value=[{"kind": "hp"}])
    coord.client.iq_er_events_json = AsyncMock(return_value=[{"kind": "iqer"}])

    await coord.async_ensure_heatpump_runtime_diagnostics(force=True)

    assert coord.client.iq_er_events_json.await_args_list[0].args == ("HP-SG",)
    assert coord.client.iq_er_events_json.await_args_list[1].args == ("HP-METER",)
    assert coord.client.heat_pump_events_json.await_args_list[0].args == ("HP-CTRL",)
    assert (
        coord.heatpump_runtime_diagnostics()["events_payloads"][0]["events_namespace"]
        == "iq_er"
    )
    assert (
        coord.heatpump_runtime_diagnostics()["events_payloads"][2]["events_namespace"]
        == "heat_pump"
    )


@pytest.mark.asyncio
async def test_heatpump_runtime_diagnostics_clears_stale_state_when_type_removed(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._show_livestream_payload = {"live_status": True}  # noqa: SLF001
    coord._heatpump_events_payloads = [{"device_uid": "HP-1"}]  # noqa: SLF001
    coord._heatpump_runtime_diagnostics_error = "stale-error"  # noqa: SLF001
    coord._heatpump_runtime_diagnostics_cache_until = (
        time.monotonic() + 60
    )  # noqa: SLF001

    await coord.async_ensure_heatpump_runtime_diagnostics(force=True)

    assert coord.heatpump_runtime_diagnostics() == {
        "show_livestream_payload": None,
        "events_payloads": [],
        "last_error": None,
    }
    assert coord._heatpump_runtime_diagnostics_cache_until is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_heatpump_runtime_diagnostics_respects_cache(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    mono_now = 8_000.0
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: mono_now)
    coord._heatpump_runtime_diagnostics_cache_until = mono_now + 60  # noqa: SLF001
    coord.client.show_livestream = AsyncMock(side_effect=AssertionError("no fetch"))

    await coord.async_ensure_heatpump_runtime_diagnostics()

    coord.client.show_livestream.assert_not_awaited()


@pytest.mark.asyncio
async def test_heatpump_runtime_diagnostics_show_livestream_failure_clears_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord._show_livestream_payload = {"live_status": True}  # noqa: SLF001
    coord.client.show_livestream = AsyncMock(side_effect=RuntimeError("live boom"))

    await coord.async_ensure_heatpump_runtime_diagnostics(force=True)

    assert coord.heatpump_runtime_diagnostics()["show_livestream_payload"] is None
    assert coord.heatpump_runtime_diagnostics()["last_error"] == "live boom"


@pytest.mark.asyncio
async def test_heatpump_runtime_diagnostics_handles_redaction_variants_and_errors(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 5,
                "devices": [
                    {"device_type": "HEAT_PUMP"},
                    {"device_type": "HEAT_PUMP", "device_uid": "HP-NONE"},
                    {"device_type": "HEAT_PUMP", "device_uid": "HP-SCALAR"},
                    {"device_type": "SG_READY_GATEWAY", "device_uid": "HP-SG"},
                    {"device_type": "SG_READY_GATEWAY", "device_uid": "HP-SG"},
                ],
            }
        },
        ["heatpump"],
    )

    def _redact(payload):
        if payload == "SHOW_NONE":
            return None
        if payload == "SHOW_SCALAR":
            return "live-redacted"
        if payload == "EVENT_NONE":
            return None
        if payload == "EVENT_SCALAR":
            return "event-redacted"
        return payload

    monkeypatch.setattr(coord, "_redact_battery_payload", _redact)
    coord.client.show_livestream = AsyncMock(side_effect=["SHOW_NONE", "SHOW_SCALAR"])
    coord.client.heat_pump_events_json = AsyncMock(
        side_effect=lambda uid: ("EVENT_NONE" if uid == "HP-NONE" else "EVENT_SCALAR")
    )
    coord.client.iq_er_events_json = AsyncMock(side_effect=RuntimeError("events boom"))

    await coord.async_ensure_heatpump_runtime_diagnostics(force=True)
    assert coord.heatpump_runtime_diagnostics()["show_livestream_payload"] is None

    await coord.async_ensure_heatpump_runtime_diagnostics(force=True)

    runtime = coord.heatpump_runtime_diagnostics()
    assert runtime["show_livestream_payload"] == {"value": "live-redacted"}
    assert [
        awaited.args for awaited in coord.client.heat_pump_events_json.await_args_list
    ] == [("HP-NONE",), ("HP-SCALAR",), ("HP-NONE",), ("HP-SCALAR",)]
    assert [
        awaited.args for awaited in coord.client.iq_er_events_json.await_args_list
    ] == [("HP-SG",), ("HP-SG",)]
    assert runtime["events_payloads"][0]["payload"] is None
    assert runtime["events_payloads"][1]["payload"] == {"value": "event-redacted"}
    assert runtime["events_payloads"][2]["error"] == "events boom"


@pytest.mark.asyncio
async def test_refresh_heatpump_power_retries_other_heatpump_uids_and_unfiltered(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {"device_type": "HEAT_PUMP", "device_uid": "HP-CTRL"},
                    {"device_type": "ENERGY_METER", "device_uid": "HP-METER"},
                ],
            }
        },
        ["heatpump"],
    )

    coord.client.hems_power_timeseries = AsyncMock(
        side_effect=[
            {"device_uid": "HP-CTRL", "heat_pump_consumption": [None, None]},
            {"device_uid": "HP-METER", "heat_pump_consumption": [None, 575.0]},
            {"heat_pump_consumption": [525.0]},
        ]
    )
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    first_uids = [
        call.kwargs.get("device_uid")
        for call in coord.client.hems_power_timeseries.await_args_list
    ]
    assert first_uids == ["HP-CTRL", "HP-METER", None]
    assert coord.heatpump_power_w == pytest.approx(575.0)
    assert coord.heatpump_power_device_uid == "HP-METER"
    assert coord.heatpump_power_source == "hems_power_timeseries:HP-METER"

    coord._heatpump_power_cache_until = None  # noqa: SLF001
    coord.client.hems_power_timeseries = AsyncMock(
        side_effect=[
            {"device_uid": "HP-CTRL", "heat_pump_consumption": [None]},
            {"device_uid": "HP-METER", "heat_pump_consumption": [None]},
            {"heat_pump_consumption": [605.0]},
        ]
    )
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    second_uids = [
        call.kwargs.get("device_uid")
        for call in coord.client.hems_power_timeseries.await_args_list
    ]
    assert second_uids == ["HP-METER", "HP-CTRL", None]
    assert coord.heatpump_power_w == pytest.approx(605.0)
    assert coord.heatpump_power_device_uid is None
    assert coord.heatpump_power_source == "hems_power_timeseries"


@pytest.mark.asyncio
async def test_refresh_heatpump_power_prefers_largest_valid_sample(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "statusText": "Normal",
                    },
                ],
            }
        },
        ["heatpump"],
    )

    coord.client.hems_power_timeseries = AsyncMock(
        side_effect=[
            {"device_uid": "HP-CTRL", "heat_pump_consumption": [480.0]},
            {"device_uid": "HP-METER", "heat_pump_consumption": [615.0]},
            {"heat_pump_consumption": [525.0]},
        ]
    )

    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(615.0)
    assert coord.heatpump_power_device_uid == "HP-METER"
    assert coord.heatpump_power_source == "hems_power_timeseries:HP-METER"


@pytest.mark.asyncio
async def test_refresh_heatpump_power_prefers_recommended_member_sample(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG",
                        "statusText": "Recommended",
                    },
                ],
            }
        },
        ["heatpump"],
    )

    coord.client.hems_power_timeseries = AsyncMock(
        side_effect=[
            {"device_uid": "HP-CTRL", "heat_pump_consumption": [610.0]},
            {"device_uid": "HP-METER", "heat_pump_consumption": [550.0]},
            {"device_uid": "HP-SG", "heat_pump_consumption": [None]},
            {"heat_pump_consumption": [725.0]},
        ]
    )

    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(550.0)
    assert coord.heatpump_power_device_uid == "HP-METER"
    assert coord.heatpump_power_source == "hems_power_timeseries:HP-METER"


@pytest.mark.asyncio
async def test_refresh_heatpump_power_reuses_selected_uid_when_inventory_stable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "statusText": "Normal",
                    },
                ],
            }
        },
        ["heatpump"],
    )

    coord.client.hems_power_timeseries = AsyncMock(
        side_effect=[
            {"device_uid": "HP-CTRL", "heat_pump_consumption": [480.0]},
            {"device_uid": "HP-METER", "heat_pump_consumption": [615.0]},
            {"heat_pump_consumption": [525.0]},
        ]
    )
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    coord._heatpump_power_cache_until = None  # noqa: SLF001
    coord.client.hems_power_timeseries = AsyncMock(
        return_value={"device_uid": "HP-METER", "heat_pump_consumption": [600.0]}
    )
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    requested_uids = [
        call.kwargs.get("device_uid")
        for call in coord.client.hems_power_timeseries.await_args_list
    ]
    assert requested_uids == ["HP-METER"]
    assert coord.heatpump_power_w == pytest.approx(600.0)
    assert coord.heatpump_power_device_uid == "HP-METER"


@pytest.mark.asyncio
async def test_async_update_data_site_only_handles_heatpump_refresh_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(
        side_effect=RuntimeError("boom")
    )  # noqa: SLF001

    assert await coord._async_update_data() == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_update_data_ignores_grid_control_and_hems_refresh_errors_site_only(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord._async_refresh_grid_control_check = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("boom")
    )
    coord._async_refresh_hems_devices = AsyncMock(
        side_effect=RuntimeError("boom")
    )  # noqa: SLF001

    assert await coord._async_update_data() == {}


@pytest.mark.asyncio
async def test_async_update_data_site_only_refreshes_hems_before_heatpump_power(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord._has_successful_refresh = True  # noqa: SLF001
    order: list[str] = []
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock(  # noqa: SLF001
        side_effect=lambda: order.append("devices")
    )
    coord._async_refresh_hems_devices = AsyncMock(  # noqa: SLF001
        side_effect=lambda: order.append("hems")
    )
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(  # noqa: SLF001
        side_effect=lambda: order.append("heatpump_power")
    )

    assert await coord._async_update_data() == {}  # noqa: SLF001
    assert order == ["devices", "hems", "heatpump_power"]


@pytest.mark.asyncio
async def test_async_update_data_continues_when_heatpump_refresh_raises(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=MagicMock(),
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *_, **__: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda *_: 0.0,
    )
    coord.client.status = AsyncMock(
        return_value={
            "ts": "2026-02-28T00:00:00Z",
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "EV",
                    "connectors": [{}],
                    "pluggedIn": False,
                    "charging": False,
                    "faulted": False,
                    "session_d": {},
                }
            ],
        }
    )
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(
        side_effect=RuntimeError("boom")
    )  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001
    assert RANDOM_SERIAL in result


@pytest.mark.asyncio
async def test_update_data_clears_success_issues_and_ignores_grid_control_and_hems_errors(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    coord.client.status = AsyncMock(
        return_value={"evChargerData": [], "ts": 1_700_000_000_000}
    )
    coord._unauth_errors = 1  # noqa: SLF001
    coord._dns_issue_reported = True  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("boom")
    )
    coord._async_refresh_hems_devices = AsyncMock(
        side_effect=RuntimeError("boom")
    )  # noqa: SLF001

    await coord._async_update_data()

    assert (DOMAIN, "reauth_required") in mock_issue_registry.deleted
    assert (DOMAIN, ISSUE_DNS_RESOLUTION) in mock_issue_registry.deleted
    assert coord._dns_issue_reported is False  # noqa: SLF001
    assert coord._unauth_errors == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_update_data_success_clears_reauth_and_converts_millisecond_timestamp(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=MagicMock(),
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *_, **__: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda *_: 0.0,
    )
    coord.client.status = AsyncMock(
        return_value={
            "ts": 1_700_000_000_000,
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "EV",
                    "connectors": [{}],
                    "pluggedIn": False,
                    "charging": False,
                    "faulted": False,
                    "session_d": {},
                }
            ],
        }
    )
    coord._unauth_errors = 1  # noqa: SLF001
    coord._last_charging = {RANDOM_SERIAL: True}  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock(return_value=None)  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert RANDOM_SERIAL in result
    assert (DOMAIN, "reauth_required") in mock_issue_registry.deleted
    assert coord._session_end_fix[RANDOM_SERIAL] == 1_700_000_000  # noqa: SLF001


def test_heatpump_power_properties_handle_invalid_internal_values(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])

    class BadFloat:
        def __float__(self) -> float:
            raise ValueError("boom")

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord._heatpump_power_w = BadFloat()  # noqa: SLF001
    assert coord.heatpump_power_w is None
    coord._heatpump_power_w = float("nan")  # noqa: SLF001
    assert coord.heatpump_power_w is None
    coord._heatpump_power_w = float("inf")  # noqa: SLF001
    assert coord.heatpump_power_w is None

    coord._heatpump_power_device_uid = BadStr()  # noqa: SLF001
    coord._heatpump_power_source = BadStr()  # noqa: SLF001
    coord._heatpump_power_last_error = BadStr()  # noqa: SLF001
    assert coord.heatpump_power_device_uid is None
    assert coord.heatpump_power_source is None
    assert coord.heatpump_power_last_error is None


@pytest.mark.asyncio
async def test_refresh_inverters_maps_status_and_production(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 2,
            "not_reporting": 0,
            "normal_count": 2,
            "warning_count": 0,
            "error_count": 0,
            "inverters": [
                {
                    "name": "IQ7A",
                    "array_name": "North",
                    "serial_number": "INV-A",
                    "status": "normal",
                    "statusText": "Normal",
                    "last_report": 1_780_000_000,
                    "fw1": "520-00082-r01-v04.30.32",
                },
                {
                    "name": "IQ7A",
                    "array_name": "West",
                    "serial_number": "INV-B",
                    "status": "normal",
                    "statusText": "Normal",
                    "last_report": 1_770_000_000,
                    "fw1": "520-00082-r01-v04.30.32",
                },
            ],
            "panel_info": {
                "pv_module_manufacturer": "Acme",
                "model_name": "PV-123",
                "stc_rating": 420,
            },
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": {
                "serialNum": "INV-A",
                "deviceId": 11,
                "statusCode": "normal",
                "type": "IQ7A",
            },
            "1002": {
                "serialNum": "INV-B",
                "deviceId": 12,
                "statusCode": "normal",
                "type": "IQ7A",
            },
            "2001": {"serialNum": "BAT-X", "deviceId": 99, "statusCode": "normal"},
        }
    )
    coord.client.inverter_production = AsyncMock(
        return_value={
            "production": {"1001": 1_000_000, "1002": "2_000_000"},
            "start_date": "2022-08-10",
            "end_date": "2026-02-09",
        }
    )

    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord.iter_inverter_serials() == ["INV-A", "INV-B"]
    assert coord.inverter_data("INV-A")["inverter_id"] == "1001"
    assert coord.inverter_data("INV-A")["device_id"] == 11
    assert coord.inverter_data("INV-A")["inverter_type"] == "IQ7A"
    assert coord.inverter_data("INV-A")["lifetime_production_wh"] == 1_000_000.0
    assert coord._inverter_model_counts == {"IQ7A": 2}  # noqa: SLF001
    bucket = coord.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["count"] == 2
    assert bucket["model_summary"] == "IQ7A x2"
    assert bucket["status_counts"]["total"] == 2
    assert bucket["status_counts"]["normal"] == 2
    assert bucket["status_type_counts"]["IQ7A"] == 2
    assert bucket["panel_info"]["pv_module_manufacturer"] == "Acme"
    assert bucket["connectivity_state"] == "online"
    assert bucket["firmware_summary"] == "520-00082-r01-v04.30.32 x2"


@pytest.mark.asyncio
async def test_refresh_inverters_filters_retired_inventory_members(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 2,
            "not_reporting": 0,
            "normal_count": 2,
            "warning_count": 0,
            "error_count": 0,
            "inverters": [
                {
                    "name": "IQ7A",
                    "serial_number": "INV-A",
                    "status": "normal",
                },
                {
                    "name": "IQ7A",
                    "serial_number": "INV-RET",
                    "status": "retired",
                },
            ],
            "panel_info": {"pv_module_manufacturer": "Acme"},
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": {
                "serialNum": "INV-A",
                "deviceId": 11,
                "statusCode": "normal",
                "type": "IQ7A",
            },
            "1002": {
                "serialNum": "INV-RET",
                "deviceId": 12,
                "statusCode": "normal",
                "type": "IQ7A",
            },
        }
    )
    coord.client.inverter_production = AsyncMock(
        return_value={
            "production": {"1001": 1_000_000, "1002": 2_000_000},
            "start_date": "2022-08-10",
            "end_date": "2026-02-09",
        }
    )

    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord.iter_inverter_serials() == ["INV-A"]
    assert coord._inverter_summary_counts["total"] == 1  # noqa: SLF001
    assert coord._inverter_status_type_counts == {"IQ7A": 1}  # noqa: SLF001
    assert coord._inverter_panel_info == {
        "pv_module_manufacturer": "Acme"
    }  # noqa: SLF001
    bucket = coord.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["count"] == 1
    assert bucket["production_start_date"] == "2022-08-10"
    assert bucket["production_end_date"] == "2026-02-09"


@pytest.mark.asyncio
async def test_refresh_inverters_paginates_inventory(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        side_effect=[
            {
                "total": 2,
                "normal_count": 2,
                "warning_count": 0,
                "error_count": 0,
                "not_reporting": 0,
                "inverters": [{"serial_number": "INV-A", "name": "IQ7A"}],
            },
            {
                "total": 2,
                "inverters": [{"serial_number": "INV-B", "name": "IQ7A"}],
            },
        ]
    )
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": {"serialNum": "INV-A", "deviceId": 11},
            "1002": {"serialNum": "INV-B", "deviceId": 12},
        }
    )
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": 100, "1002": 200}}
    )

    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord.client.inverters_inventory.await_count == 2
    assert set(coord.iter_inverter_serials()) == {"INV-A", "INV-B"}


@pytest.mark.asyncio
async def test_refresh_inverters_inventory_typeerror_fallback_and_break(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001

    async def _inventory(*args, **kwargs):
        if kwargs:
            raise TypeError("kwargs unsupported")
        return {
            "total": 1001,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 0,
            "inverters": [{"serial_number": "INV-A", "name": "IQ7A"}],
        }

    coord.client.inverters_inventory = _inventory
    coord.client.inverter_status = AsyncMock(
        return_value={"1001": {"serialNum": "INV-A", "deviceId": 11}}
    )
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": 100}}
    )

    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord.iter_inverter_serials() == ["INV-A"]


@pytest.mark.asyncio
async def test_refresh_inverters_handles_non_dict_inventory(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.inverters_inventory = AsyncMock(return_value=["bad"])
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(return_value={})

    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord.iter_inverter_serials() == []


@pytest.mark.asyncio
async def test_refresh_inverters_handles_shape_edge_cases(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._inverter_data = []  # type: ignore[assignment]  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(return_value={"inverters": {"bad": 1}})
    coord.client.inverter_status = AsyncMock(side_effect=RuntimeError("boom"))
    coord.client.inverter_production = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord.iter_inverter_serials() == []


@pytest.mark.asyncio
async def test_refresh_inverters_handles_non_dict_status_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 0,
            "inverters": [{"serial_number": "INV-A", "name": "IQ7A"}],
        }
    )
    coord.client.inverter_status = AsyncMock(return_value=["bad"])
    coord.client.inverter_production = AsyncMock(return_value={})

    await coord._async_refresh_inverters()  # noqa: SLF001

    payload = coord.inverter_data("INV-A")
    assert payload is not None
    assert payload["inverter_id"] is None


@pytest.mark.asyncio
async def test_refresh_inverters_pagination_breaks_on_invalid_page_shapes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        side_effect=[
            {
                "total": 1001,
                "normal_count": 1,
                "warning_count": 0,
                "error_count": 0,
                "not_reporting": 0,
                "inverters": [{"serial_number": "INV-A", "name": "IQ7A"}],
            },
            {"total": 1001, "inverters": {"bad": "shape"}},
        ]
    )
    coord.client.inverter_status = AsyncMock(
        return_value={"1001": {"serialNum": "INV-A", "deviceId": 11}}
    )
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": 100}}
    )

    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord.iter_inverter_serials() == ["INV-A"]


@pytest.mark.asyncio
async def test_refresh_inverters_pagination_updates_total_and_offset(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    second_page = [
        {"serial_number": f"INV-{idx:04d}", "name": "IQ7A"} for idx in range(1000)
    ]
    coord.client.inverters_inventory = AsyncMock(
        side_effect=[
            {
                "total": 1001,
                "normal_count": 1001,
                "warning_count": 0,
                "error_count": 0,
                "not_reporting": 0,
                "inverters": [{"serial_number": "INV-A", "name": "IQ7A"}],
            },
            {"total": 2001, "inverters": second_page},
            {"total": 2001, "inverters": []},
        ]
    )
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(return_value={})

    await coord._async_refresh_inverters()  # noqa: SLF001

    offsets = [
        call.kwargs.get("offset")
        for call in coord.client.inverters_inventory.await_args_list
    ]
    assert offsets[:3] == [0, 1, 1001]


@pytest.mark.asyncio
async def test_refresh_inverters_skips_production_when_start_unknown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {}  # noqa: SLF001
    coord._inverter_data = {}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 0,
            "inverters": [{"serial_number": "INV-A", "name": "IQ7A"}],
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={"1001": {"serialNum": "INV-A", "deviceId": 11}}
    )
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": 1}}
    )

    await coord._async_refresh_inverters()  # noqa: SLF001

    coord.client.inverter_production.assert_not_awaited()
    payload = coord.inverter_data("INV-A")
    assert payload is not None
    assert payload["lifetime_query_start_date"] is None


@pytest.mark.asyncio
async def test_refresh_inverters_uses_site_local_current_date(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": [{"curr_date_site": "2026-02-08"}]
    }
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 0,
            "inverters": [{"serial_number": "INV-A", "name": "IQ7A"}],
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={"1001": {"serialNum": "INV-A", "deviceId": 11}}
    )
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": 1}}
    )

    await coord._async_refresh_inverters()  # noqa: SLF001

    awaited = coord.client.inverter_production.await_args
    assert awaited.kwargs["end_date"] == "2026-02-08"


@pytest.mark.asyncio
async def test_refresh_inverters_handles_production_exception_with_known_start(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 0,
            "inverters": [{"serial_number": "INV-A", "name": "IQ7A"}],
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={"1001": {"serialNum": "INV-A", "deviceId": 11}}
    )
    coord.client.inverter_production = AsyncMock(side_effect=RuntimeError("boom"))

    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord._inverter_production_payload == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_inverters_handles_item_edge_cases(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {
            "serial_number": "INV-A",
            "inverter_id": "1001",
            "lifetime_production_wh": object(),
            "lifetime_query_start_date": "bad",
            "lifetime_query_end_date": "bad",
        },
        "INV-B": "bad-prev",
    }
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 2,
            "normal_count": 2,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 0,
            "inverters": [
                {"serial_number": "", "name": "IQ7A"},
                {"serial_number": "INV-A", "name": "IQ7A"},
                {"serial_number": "INV-B", "name": "IQ7A"},
            ],
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": "bad",
            "1002": {"serialNum": "", "deviceId": 999},
            "1003": {"serialNum": "INV-B", "deviceId": 12},
        }
    )
    coord.client.inverter_production = AsyncMock(return_value=["bad"])

    await coord._async_refresh_inverters()  # noqa: SLF001

    payload_a = coord.inverter_data("INV-A")
    assert payload_a is not None
    assert payload_a["inverter_id"] == "1001"
    assert payload_a["lifetime_production_wh"] is None
    assert payload_a["lifetime_query_start_date"] == "2022-08-10"
    assert payload_a["lifetime_query_end_date"] is not None

    payload_b = coord.inverter_data("INV-B")
    assert payload_b is not None
    assert payload_b["inverter_id"] == "1003"


@pytest.mark.asyncio
async def test_refresh_inverters_handles_bad_production_value(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {
            "serial_number": "INV-A",
            "inverter_id": "1001",
            "lifetime_production_wh": 1_500_000.0,
        }
    }
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 0,
            "inverters": [{"serial_number": "INV-A", "name": "IQ7A"}],
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={"1001": {"serialNum": "INV-A", "deviceId": 11}}
    )
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": "bad"}}
    )

    await coord._async_refresh_inverters()  # noqa: SLF001

    payload = coord.inverter_data("INV-A")
    assert payload is not None
    assert payload["lifetime_production_wh"] == 1_500_000.0


@pytest.mark.asyncio
async def test_refresh_inverters_clamps_payload_counts_and_sanitizes_panel_info(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 5,
            "normal_count": 5,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 0,
            "inverters": [{"serial_number": "INV-A", "name": "IQ7A", "status": None}],
            "panel_info": {"pv_module_manufacturer": None, "model_name": "   "},
        }
    )
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(return_value={"production": {}})

    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord._inverter_summary_counts["total"] == 1  # noqa: SLF001
    assert coord._inverter_summary_counts["normal"] == 1  # noqa: SLF001
    assert coord._inverter_summary_counts["not_reporting"] == 0  # noqa: SLF001
    assert coord._inverter_summary_counts["unknown"] == 0  # noqa: SLF001
    assert coord._inverter_panel_info is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_inverters_clamps_not_reporting_overflow_with_break(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 3,
            "inverters": [{"serial_number": "INV-A", "name": "IQ7A", "status": None}],
        }
    )
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(return_value={"production": {}})

    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord._inverter_summary_counts["total"] == 1  # noqa: SLF001
    assert coord._inverter_summary_counts["normal"] == 1  # noqa: SLF001
    assert coord._inverter_summary_counts["not_reporting"] == 0  # noqa: SLF001
    assert coord._inverter_summary_counts["unknown"] == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_inverters_backfills_not_reporting_when_counts_missing(
    coordinator_factory,
) -> None:
    class BadType:
        def __str__(self) -> str:
            raise ValueError("bad")

    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "normal_count": 0,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 0,
            "inverters": [{"serial_number": "INV-A", "name": "IQ7A", "status": None}],
            "panel_info": {"stc_rating": 420},
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={"1001": {"serialNum": "INV-A", "type": BadType()}}
    )
    coord.client.inverter_production = AsyncMock(return_value={"production": {}})

    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord._inverter_summary_counts["total"] == 1  # noqa: SLF001
    assert coord._inverter_summary_counts["not_reporting"] == 0  # noqa: SLF001
    assert coord._inverter_summary_counts["unknown"] == 1  # noqa: SLF001
    assert coord._inverter_status_type_counts == {}  # noqa: SLF001
    assert coord._inverter_panel_info == {"stc_rating": 420}  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_inverters_disabled_clears_state(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    coord._inverter_panel_info = {"pv_module_manufacturer": "Acme"}  # noqa: SLF001
    coord._inverter_status_type_counts = {"IQ7A": 1}  # noqa: SLF001
    coord._inverter_model_counts = {"IQ7A": 1}  # noqa: SLF001
    coord._inverter_summary_counts = {  # noqa: SLF001
        "total": 1,
        "normal": 1,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
    }
    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"serial_number": "INV-A"}],
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    coord.include_inverters = False

    await coord._async_refresh_inverters()  # noqa: SLF001

    assert coord.iter_inverter_serials() == []
    assert coord.type_bucket("microinverter") is None
    assert coord._inverters_inventory_payload is None  # noqa: SLF001
    assert coord._inverter_status_payload is None  # noqa: SLF001
    assert coord._inverter_production_payload is None  # noqa: SLF001
    assert coord._inverter_panel_info is None  # noqa: SLF001
    assert coord._inverter_status_type_counts == {}  # noqa: SLF001


def test_inverter_start_date_returns_none_when_unknown(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {}  # noqa: SLF001
    coord._inverter_data = {}  # noqa: SLF001
    assert coord._inverter_start_date() is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_helper_wrappers_cover_stage_and_topology_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    phase_timings: dict[str, float] = {}
    begin = Mock()
    end = Mock(return_value=False)
    coord._begin_topology_refresh_batch = begin  # type: ignore[assignment]  # noqa: SLF001
    coord._end_topology_refresh_batch = end  # type: ignore[assignment]  # noqa: SLF001

    await coord._async_run_ordered_refresh_calls(  # noqa: SLF001
        phase_timings,
        stage_key="ordered",
        defer_topology=True,
        calls=(("first_s", "first", AsyncMock(return_value=None)),),
    )

    assert "first_s" in phase_timings
    assert "ordered_s" in phase_timings
    begin.assert_called_once()
    end.assert_called_once()

    phase_timings.clear()
    begin.reset_mock()
    end.reset_mock()

    await coord._async_run_staged_refresh_calls(  # noqa: SLF001
        phase_timings,
        stage_key="empty",
    )

    assert phase_timings == {"empty_s": 0.0}
    begin.assert_not_called()
    end.assert_not_called()


def test_summary_type_bucket_source_guards_invalid_inputs(coordinator_factory) -> None:
    coord = coordinator_factory(serials=[])
    coord._type_device_buckets = "bad"  # type: ignore[assignment]  # noqa: SLF001
    assert coord._summary_type_bucket_source("envoy") is None  # noqa: SLF001
    assert coord._summary_type_bucket_source(None) is None  # noqa: SLF001


def test_publish_internal_state_update_copies_current_data(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.async_set_updated_data = Mock()  # type: ignore[assignment]

    coord._publish_internal_state_update()  # noqa: SLF001

    coord.async_set_updated_data.assert_called_once_with(dict(coord.data))


def test_end_topology_refresh_batch_flushes_pending_refresh(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._topology_refresh_suppressed = 1  # noqa: SLF001
    coord._topology_refresh_pending = True  # noqa: SLF001
    coord._refresh_cached_topology = Mock(return_value=True)  # type: ignore[assignment]  # noqa: SLF001

    assert coord._end_topology_refresh_batch() is True  # noqa: SLF001
    coord._refresh_cached_topology.assert_called_once()
    assert coord._topology_refresh_pending is False  # noqa: SLF001


def test_topology_listener_and_summary_helpers_cover_edge_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    calls: list[str] = []

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    remove = coord.async_add_topology_listener(lambda: calls.append("ok"))
    coord.async_add_topology_listener(
        lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    coord._notify_topology_listeners()  # noqa: SLF001
    assert calls == ["ok"]

    remove()
    assert coord.topology_snapshot() == coord._topology_snapshot_cache  # noqa: SLF001
    assert len(coord._topology_listeners) == 1  # noqa: SLF001

    coord._build_heatpump_type_summaries = Mock(return_value={})  # type: ignore[assignment]  # noqa: SLF001
    assert coord.heatpump_type_summary(BadStr()) == {}

    original_router_marker = coord._gateway_iq_energy_router_records_marker
    coord._gateway_iq_energy_router_records_marker = lambda: ("stable",)  # type: ignore[assignment]  # noqa: SLF001
    coord._gateway_iq_energy_router_records_cache = [
        {"key": "router_1", "name": "Router"}
    ]  # noqa: SLF001
    coord._gateway_iq_energy_router_records_source = ("stable",)  # noqa: SLF001
    coord._gateway_iq_energy_router_records_by_key_cache = {  # noqa: SLF001
        "router_1": {"key": "router_1", "name": "Router"}
    }

    assert coord._router_record_key("bad") is None  # noqa: SLF001
    assert coord._router_record_key({"key": None}) is None  # noqa: SLF001
    assert coord._router_record_key({"key": BadStr()}) is None  # noqa: SLF001
    assert coord.gateway_iq_energy_router_record(BadStr()) is None
    assert coord.gateway_iq_energy_router_record(" ") is None
    assert coord.gateway_iq_energy_router_record("router_1") == {
        "key": "router_1",
        "name": "Router",
    }
    coord._gateway_iq_energy_router_records_marker = original_router_marker  # type: ignore[assignment]  # noqa: SLF001

    assert coord._summary_text(BadStr()) is None  # noqa: SLF001
    assert coord._summary_identity("Router Main") == "router_main"  # noqa: SLF001
    assert coord._gateway_iq_energy_router_records_marker() == (  # noqa: SLF001
        id(getattr(coord, "_hems_devices_payload", None)),
        id(getattr(coord, "_devices_inventory_payload", None)),
        id(getattr(coord, "_restored_gateway_iq_energy_router_records", None)),
    )
    assert coord._heatpump_status_text(None) is None  # noqa: SLF001

    records = coord._gateway_iq_energy_router_summary_records(  # noqa: SLF001
        [
            {"name": "Router Main"},
            {"name": "Router Main"},
            {},
        ]
    )
    assert [record["key"] for record in records] == [
        "name_router_main",
        "name_router_main_2",
        "index_3",
    ]


def test_debug_log_summary_if_changed_deduplicates_output(
    coordinator_factory, caplog
) -> None:
    coord = coordinator_factory(serials=[])
    summary = {
        "ordered_type_keys": ["iqevse"],
        "type_count": 1,
        "types": {"iqevse": {"count": 1, "field_keys": ["name", "serial_number"]}},
    }

    with caplog.at_level(logging.DEBUG):
        coord._debug_log_summary_if_changed(  # noqa: SLF001
            "devices_inventory",
            "Device inventory discovery summary",
            summary,
        )
        coord._debug_log_summary_if_changed(  # noqa: SLF001
            "devices_inventory",
            "Device inventory discovery summary",
            summary,
        )

    matches = [
        record
        for record in caplog.records
        if "Device inventory discovery summary" in record.message
    ]
    assert len(matches) == 1
    assert '"type_count": 1' in matches[0].message


@pytest.mark.asyncio
async def test_refresh_devices_inventory_logs_sanitized_discovery_summary(
    coordinator_factory, caplog
) -> None:
    payload = {
        "result": [
            {
                "type": "iqevse",
                "devices": [
                    {
                        "serial_number": "SERIAL-123456",
                        "name": "Driveway Charger",
                        "status": "online",
                        "custom_field": True,
                    }
                ],
            },
            {
                "type": "envoy",
                "devices": [
                    {
                        "serial_number": "ENV-123456",
                        "name": "Back Shed Gateway",
                        "statusText": "online",
                        "connected": True,
                    }
                ],
            },
        ]
    }
    client = SimpleNamespace(devices_inventory=AsyncMock(return_value=payload))
    coord = coordinator_factory(client=client, serials=[])

    with caplog.at_level(logging.DEBUG):
        await coord._async_refresh_devices_inventory(force=True)  # noqa: SLF001
        await coord._async_refresh_devices_inventory(force=True)  # noqa: SLF001

    matches = [
        record
        for record in caplog.records
        if "Device inventory discovery summary" in record.message
    ]
    assert len(matches) == 1
    message = matches[0].message
    assert '"iqevse"' in message
    assert '"custom_field"' in message
    assert "SERIAL-123456" not in message
    assert "Driveway Charger" not in message
    assert "ENV-123456" not in message
    assert "Back Shed Gateway" not in message


@pytest.mark.asyncio
async def test_refresh_hems_devices_logs_sanitized_discovery_summary(
    coordinator_factory, caplog
) -> None:
    payload = {
        "data": {
            "hems-devices": {
                "gateway": [
                    {
                        "device-uid": "GATEWAY-UID-123456789",
                        "name": "Main Gateway",
                        "ip-address": "10.0.0.2",
                    }
                ],
                "heat-pump": [
                    {
                        "device-uid": "DEVICE-UID-123456789",
                        "name": "Living Room Heat Pump",
                        "device-type": "HEAT_PUMP",
                        "status": "Running",
                    }
                ],
            }
        }
    }
    client = SimpleNamespace(
        hems_site_supported=True,
        hems_devices=AsyncMock(return_value=payload),
    )
    coord = coordinator_factory(client=client, serials=[])
    coord._async_refresh_hems_support_preflight = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001

    with caplog.at_level(logging.DEBUG):
        await coord._async_refresh_hems_devices(force=True)  # noqa: SLF001

    matches = [
        record
        for record in caplog.records
        if "HEMS discovery summary" in record.message
    ]
    assert len(matches) == 1
    message = matches[0].message
    assert '"group_keys": ["gateway", "heat-pump"]' in message
    assert '"heatpump_device_type_counts": {"HEAT_PUMP": 1}' in message
    assert "DEVICE-UID-123456789" not in message
    assert "Living Room Heat Pump" not in message
    assert "10.0.0.2" not in message


@pytest.mark.asyncio
async def test_refresh_evse_feature_flags_logs_sanitized_summary(
    coordinator_factory, caplog
) -> None:
    payload = {
        "meta": {"schemaVersion": "1"},
        "data": {
            "allowRemoteStart": True,
            "SERIAL-123456": {
                "plugAndCharge": True,
                "rfid": False,
            },
        },
    }
    client = SimpleNamespace(evse_feature_flags=AsyncMock(return_value=payload))
    coord = coordinator_factory(client=client, serials=[])

    with caplog.at_level(logging.DEBUG):
        await coord._async_refresh_evse_feature_flags(force=True)  # noqa: SLF001

    matches = [
        record
        for record in caplog.records
        if "EVSE feature flag summary" in record.message
    ]
    assert len(matches) == 1
    message = matches[0].message
    assert '"site_flag_keys": ["allowRemoteStart"]' in message
    assert '"charger_flag_keys": ["plugAndCharge", "rfid"]' in message
    assert '"charger_count": 1' in message
    assert "SERIAL-123456" not in message


@pytest.mark.asyncio
async def test_refresh_heatpump_power_failure_logs_truncated_device_uid(
    coordinator_factory, caplog
) -> None:
    client = SimpleNamespace(
        hems_site_supported=True,
        hems_power_timeseries=AsyncMock(side_effect=RuntimeError("boom")),
    )
    coord = coordinator_factory(client=client, serials=[])
    coord.has_type = lambda type_key: type_key == "heatpump"  # type: ignore[assignment]
    coord._async_refresh_hems_support_preflight = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001
    coord._heatpump_power_fetch_plan = lambda: (["DEVICE-UID-123456789"], False, ())  # type: ignore[assignment]  # noqa: SLF001
    coord._site_local_current_date = lambda: "2026-03-13"  # type: ignore[assignment]  # noqa: SLF001

    with caplog.at_level(logging.DEBUG):
        await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    assert "Heat pump power fetch failed" in caplog.text
    assert "DEVICE-UID-123456789" not in caplog.text
    assert "DEVI...6789" in caplog.text


def test_gateway_and_microinverter_summary_builders_cover_string_and_fallback_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.type_bucket = lambda type_key: {  # type: ignore[assignment]
        "envoy": {
            "count": 3,
            "devices": [
                {
                    "name": "Gateway A",
                    "statusText": "online",
                    "connected": "yes",
                    "model": "IQ Gateway",
                    "envoy_sw_version": "8.2.0",
                    "last_report": "2026-02-15T10:00:00Z",
                },
                {
                    "name": "Gateway B",
                    "status": "offline",
                    "connected": "no",
                },
                {
                    "name": "Gateway C",
                    "status": "mystery",
                    "connected": "maybe",
                },
            ],
        },
        "microinverter": {
            "count": 3,
            "devices": [],
            "status_counts": {"total": 3, "unknown": 1},
        },
    }.get(type_key, {})

    gateway_snapshot = coord._build_gateway_inventory_summary()  # noqa: SLF001
    assert gateway_snapshot["connected_devices"] == 1
    assert gateway_snapshot["disconnected_devices"] == 1
    assert gateway_snapshot["unknown_connection_devices"] == 1

    micro_snapshot = coord._build_microinverter_inventory_summary()  # noqa: SLF001
    assert micro_snapshot["connectivity_state"] == "degraded"


def test_inverter_start_date_uses_existing_snapshot_start_date(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {}  # noqa: SLF001
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {"lifetime_query_start_date": "2022-08-10"},
        "INV-B": {"lifetime_query_start_date": "2023-01-01"},
        "INV-C": {"lifetime_query_start_date": "not-a-date"},
    }

    assert coord._inverter_start_date() == "2022-08-10"  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_inverters_preserves_previous_lifetime_on_regression(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {
            "serial_number": "INV-A",
            "inverter_id": "1001",
            "device_id": 11,
            "status_code": "normal",
            "show_sig_str": False,
            "emu_version": "8.3.5232",
            "issi": {"sig_str": 1},
            "rssi": {"sig_str": 2},
            "lifetime_production_wh": 2_000_000.0,
            "lifetime_query_start_date": "2022-08-10",
            "lifetime_query_end_date": "2026-02-09",
        }
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "not_reporting": 0,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "inverters": [
                {
                    "name": "IQ7A",
                    "array_name": "North",
                    "serial_number": "INV-A",
                    "status": "normal",
                    "statusText": "Normal",
                }
            ],
        }
    )
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": 1_000_000}}
    )

    await coord._async_refresh_inverters()  # noqa: SLF001

    payload = coord.inverter_data("INV-A")
    assert payload is not None
    assert payload["inverter_id"] == "1001"
    assert payload["device_id"] == 11
    assert payload["lifetime_production_wh"] == 2_000_000.0
    assert payload["lifetime_query_start_date"] == "2022-08-10"
    assert payload["lifetime_query_end_date"] == "2026-02-09"


def test_type_and_inverter_helpers_cover_remaining_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"sku_id": "IQ7A-SKU"}],
            "status_summary": "Normal 1 | Warning 0 | Error 0 | Not Reporting 0",
            "extra_list": ["a", "b"],
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    bucket = coord.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["extra_list"] == ["a", "b"]

    info = coord.type_device_info("microinverter")
    assert info is not None
    assert info["hw_version"] == "IQ7A-SKU"
    assert info.get("model_id") is None
    assert coord.type_device_model_id("microinverter") is None
    assert coord.type_device_model(None) is None
    assert coord.type_device_serial_number(None) is None
    assert coord.type_device_model_id(None) is None
    assert coord.type_device_sw_version(None) is None
    assert coord.type_device_hw_version(None) is None
    coord._type_device_buckets = {"microinverter": "bad"}  # noqa: SLF001
    assert coord.type_device_hw_version("microinverter") is None

    coord.type_bucket = lambda _key: {"devices": "bad"}  # type: ignore[assignment]
    assert coord._type_bucket_members("envoy") == []  # noqa: SLF001
    coord.type_bucket = type(coord).type_bucket.__get__(coord, type(coord))  # type: ignore[method-assign]

    class BadText:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert coord._type_member_text({"name": BadText()}, "name") is None  # noqa: SLF001
    assert (
        coord._type_summary_from_values(
            [None, BadText(), "  ", "A", "A"]
        )  # noqa: SLF001
        == "A x2"
    )

    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"sku_id": "IQ8M"}],
            "firmware_summary": "4.0 x1",
        }
    }
    assert coord.type_device_sw_version("microinverter") is None
    coord._type_device_buckets = {"encharge": "bad"}  # noqa: SLF001
    assert coord.type_device_hw_version("encharge") is None
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "type_key": "envoy",
            "type_label": "Gateway",
            "count": 1,
            "devices": [{"serial_number": "GW-1"}],
            "status_summary": "Normal 1 | Warning 0 | Error 0 | Not Reporting 0",
        }
    }
    assert coord.type_device_hw_version("envoy") is None

    coord._inverter_data = None  # type: ignore[assignment]  # noqa: SLF001
    assert coord.iter_inverter_serials() == []
    assert coord.inverter_data("INV-A") is None

    class BadSerial:
        def __str__(self) -> str:
            raise ValueError("bad")

    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    assert coord.inverter_data(BadSerial()) is None
    assert coord.inverter_data("") is None


@pytest.mark.asyncio
async def test_update_data_ignores_devices_inventory_refresh_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord._async_refresh_devices_inventory = AsyncMock(
        side_effect=RuntimeError()
    )  # noqa: SLF001
    result = await coord._async_update_data()
    assert result == {}

    coord = coordinator_factory()
    coord.client.status = AsyncMock(return_value={"evChargerData": [], "ts": 0})
    coord._async_refresh_devices_inventory = AsyncMock(
        side_effect=RuntimeError()
    )  # noqa: SLF001
    await coord._async_update_data()


@pytest.mark.asyncio
async def test_update_data_ignores_inverter_refresh_errors_site_only(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord._async_refresh_inverters = AsyncMock(
        side_effect=RuntimeError()
    )  # noqa: SLF001

    result = await coord._async_update_data()

    assert result == {}


@pytest.mark.asyncio
async def test_update_data_ignores_inverter_refresh_errors_non_site_only(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.status = AsyncMock(return_value={"evChargerData": [], "ts": 0})
    coord._async_refresh_inverters = AsyncMock(
        side_effect=RuntimeError()
    )  # noqa: SLF001

    await coord._async_update_data()


def test_battery_property_false_paths(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)

    class _FalseControls(type(coord)):
        @property
        def battery_controls_available(self):  # type: ignore[override]
            return False

    coord.__class__ = _FalseControls
    assert coord.savings_use_battery_switch_available is False
    assert coord.battery_reserve_editable is False

    coord._battery_profile = "backup_only"  # noqa: SLF001
    assert coord.battery_reserve_min == 100

    coord._battery_charge_begin_time = None  # noqa: SLF001
    coord._battery_charge_end_time = None  # noqa: SLF001
    assert coord.charge_from_grid_schedule_available is False

    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = None  # noqa: SLF001
    assert coord.battery_shutdown_level_available is False


@pytest.mark.asyncio
async def test_update_skips_status_when_site_only(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.site_only = True

    client = SimpleNamespace(
        status=AsyncMock(side_effect=AssertionError("should not call status"))
    )
    coord.client = client

    result = await coord._async_update_data()

    assert result == {}
    assert client.status.await_count == 0
    assert coord.last_success_utc is not None
    assert coord._has_successful_refresh is True


@pytest.mark.asyncio
async def test_update_skips_status_when_no_serials(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = set()
    coord._serial_order = []

    client = SimpleNamespace(
        status=AsyncMock(side_effect=AssertionError("should not call status"))
    )
    coord.client = client

    result = await coord._async_update_data()

    assert result == {}
    assert client.status.await_count == 0
    assert coord.last_success_utc is not None
    assert coord._has_successful_refresh is True


@pytest.mark.asyncio
async def test_site_only_clears_issues_and_counters(
    hass, monkeypatch, mock_issue_registry
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.site_only = True
    coord._network_issue_reported = True
    coord._cloud_issue_reported = True
    coord._dns_issue_reported = True
    coord._unauth_errors = 3
    coord._rate_limit_hits = 2
    coord._http_errors = 4
    coord._network_errors = 5
    coord._dns_failures = 6
    coord._last_error = "any error"
    coord.backoff_ends_utc = object()
    coord._backoff_until = 123.0
    cancelled = {"called": False}
    coord._backoff_cancel = lambda: cancelled.__setitem__("called", True)

    await coord._async_update_data()

    assert cancelled["called"] is True
    assert coord._network_issue_reported is False
    assert coord._cloud_issue_reported is False
    assert coord._dns_issue_reported is False
    assert coord._unauth_errors == 0
    assert coord._rate_limit_hits == 0
    assert coord._http_errors == 0
    assert coord._network_errors == 0
    assert coord._dns_failures == 0
    assert coord._last_error is None
    assert coord.backoff_ends_utc is None
    assert coord._backoff_until is None
    assert ("enphase_ev", "cloud_unreachable") in mock_issue_registry.deleted
    assert ("enphase_ev", "cloud_service_unavailable") in mock_issue_registry.deleted
    assert ("enphase_ev", "cloud_dns_resolution") in mock_issue_registry.deleted


@pytest.mark.asyncio
async def test_backoff_on_429(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)

    class StubClient:
        async def status(self):
            raise _client_response_error(429, headers={"Retry-After": "1"})

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None


@pytest.mark.asyncio
async def test_backoff_timer_requests_refresh(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)
    coord.async_request_refresh = AsyncMock()

    captured: dict[str, object] = {}

    def _fake_call_later(hass_obj, delay, cb):
        captured["delay"] = delay
        captured["callback"] = cb

        def _cancel():
            captured["cancelled"] = True

        return _cancel

    monkeypatch.setattr(coord_mod, "async_call_later", _fake_call_later)

    now = datetime(2025, 11, 3, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: now)

    coord._schedule_backoff_timer(2.5)

    assert captured["delay"] == 2.5
    assert coord.backoff_ends_utc == now + timedelta(seconds=2.5)
    assert callable(coord._backoff_cancel)

    await captured["callback"](now + timedelta(seconds=3))

    assert coord.async_request_refresh.await_count == 1
    assert coord.backoff_ends_utc is None
    assert coord._backoff_cancel is None


@pytest.mark.asyncio
async def test_http_error_issue(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import ISSUE_CLOUD_ERRORS
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)

    class FailingClient:
        async def status(self):
            raise _client_response_error(503)

    created = []
    deleted = []
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

    coord.client = FailingClient()

    for _ in range(3):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        coord._backoff_until = None

    matching = [
        kwargs for _, issue_id, kwargs in created if issue_id == ISSUE_CLOUD_ERRORS
    ]
    assert matching
    latest_payload = matching[-1]
    placeholders = latest_payload["translation_placeholders"]
    assert placeholders["site_id"] == coord.site_id
    metrics = latest_payload["data"]["site_metrics"]
    assert metrics["last_error"]

    class SuccessClient:
        async def status(self):
            return {"evChargerData": []}

    coord.client = SuccessClient()
    coord._backoff_until = None
    data = await coord._async_update_data()
    coord.async_set_updated_data(data)

    assert any(issue_id == ISSUE_CLOUD_ERRORS for _, issue_id in deleted)


@pytest.mark.asyncio
async def test_network_issue_includes_metrics(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.const import ISSUE_NETWORK_UNREACHABLE

    coord = _make_coordinator(hass, monkeypatch)
    coord.site_name = "Garage"

    class StubClient:
        async def status(self):
            raise aiohttp.ClientError("connection reset by peer")

    coord.client = StubClient()

    created: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda hass_, domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
        raising=False,
    )

    for _ in range(3):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        coord._backoff_until = None

    issue_map = {issue_id: kwargs for issue_id, kwargs in created}
    assert ISSUE_NETWORK_UNREACHABLE in issue_map
    payload = issue_map[ISSUE_NETWORK_UNREACHABLE]
    placeholders = payload["translation_placeholders"]
    assert placeholders["site_name"] == "Garage"
    metrics = payload["data"]["site_metrics"]
    assert metrics["network_errors"] >= 3


@pytest.mark.asyncio
async def test_dns_issue_includes_metrics(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.const import ISSUE_DNS_RESOLUTION

    coord = _make_coordinator(hass, monkeypatch)

    class StubClient:
        async def status(self):
            raise aiohttp.ClientError("Temporary failure in name resolution")

    coord.client = StubClient()

    created: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda hass_, domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
        raising=False,
    )

    for _ in range(4):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        coord._backoff_until = None

    issue_map = {issue_id: kwargs for issue_id, kwargs in created}
    assert ISSUE_DNS_RESOLUTION in issue_map
    dns_payload = issue_map[ISSUE_DNS_RESOLUTION]
    placeholders = dns_payload["translation_placeholders"]
    assert placeholders["site_id"] == coord.site_id
    metrics = dns_payload["data"]["site_metrics"]
    assert metrics["dns_errors"] >= 2


@pytest.mark.asyncio
async def test_http_error_description_from_json(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)
    payload = '{"error":{"details":[{"description":"Too many requests"}]}}'

    class StubClient:
        async def status(self):
            raise _client_response_error(429, message=payload)

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_status == 429
    assert coord.last_failure_description == "Too many requests"
    assert coord.last_failure_response == payload
    assert coord.last_failure_source == "http"


@pytest.mark.asyncio
async def test_http_error_description_plain_text(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)
    payload = " backend unavailable "

    class StubClient:
        async def status(self):
            raise _client_response_error(500, message=payload)

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_status == 500
    assert coord.last_failure_description == "Internal Server Error"
    assert coord.last_failure_response == "backend unavailable"


@pytest.mark.asyncio
async def test_http_error_description_falls_back_to_status_phrase(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass, monkeypatch)

    class StubClient:
        async def status(self):
            raise _client_response_error(503, message=" ")

    coord.client = StubClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_status == 503
    assert coord.last_failure_description == "Service Unavailable"
    assert coord.last_failure_response == ""


def test_collect_site_metrics_and_placeholders(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    now = datetime(2024, 1, 2, tzinfo=timezone.utc)
    coord.site_name = "Garage Site"
    coord.last_success_utc = now
    coord.last_failure_utc = now
    coord.last_failure_status = 503
    coord.last_failure_description = "Service Unavailable"
    coord.last_failure_source = "http"
    coord.last_failure_response = "response"
    coord.latency_ms = 123
    coord._backoff_until = time.monotonic() + 5
    coord.backoff_ends_utc = now
    coord._network_errors = 2
    coord._http_errors = 1
    coord._rate_limit_hits = 1
    coord._dns_failures = 0
    coord._last_error = "unauthorized"
    coord._phase_timings = {"status_s": 0.5}
    coord._session_history_cache_ttl = 300
    coord._hems_devices_using_stale = True
    coord._hems_devices_last_success_mono = time.monotonic() - 12
    coord._hems_devices_last_success_utc = now

    metrics = coord.collect_site_metrics()
    assert metrics["site_id"] == coord.site_id
    assert metrics["site_name"] == "Garage Site"
    assert metrics["last_success"] == now.isoformat()
    assert metrics["backoff_active"] is True
    assert metrics["phase_timings"] == {"status_s": 0.5}
    assert metrics["hems_devices_data_stale"] is True
    assert metrics["hems_devices_last_success_utc"] == now.isoformat()
    assert metrics["hems_devices_last_success_age_s"] >= 0

    placeholders = coord._issue_translation_placeholders(metrics)
    assert placeholders["site_id"] == coord.site_id
    assert placeholders["site_name"] == "Garage Site"
    assert placeholders["last_error"] == "unauthorized"
    assert placeholders["last_status"] == "503"


def test_collect_site_metrics_skips_negative_hems_age(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord._hems_devices_last_success_mono = time.monotonic() + 5  # noqa: SLF001

    metrics = coord.collect_site_metrics()

    assert "hems_devices_last_success_age_s" not in metrics


@pytest.mark.asyncio
async def test_handle_client_unauthorized_refresh(monkeypatch, hass):
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)
    coord._attempt_auto_refresh = AsyncMock(return_value=True)
    created: list[tuple[str, dict]] = []
    deleted: list[str] = []

    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda *args, **kwargs: created.append((args[2], kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        coord_mod.ir,
        "async_delete_issue",
        lambda hass_, domain, issue_id: deleted.append(issue_id),
        raising=False,
    )

    result = await coord._handle_client_unauthorized()
    assert result is True
    assert coord._unauth_errors == 0
    assert coord._last_error == "unauthorized"
    assert deleted == ["reauth_required"]
    assert created == []


@pytest.mark.asyncio
async def test_handle_client_unauthorized_failure(monkeypatch, hass):
    from homeassistant.exceptions import ConfigEntryAuthFailed
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = _make_coordinator(hass, monkeypatch)
    coord.site_name = "Garage Site"
    coord.last_failure_status = 401
    coord.last_failure_description = "Unauthorized"
    coord._last_error = "stale"
    coord._attempt_auto_refresh = AsyncMock(return_value=False)
    coord._unauth_errors = 1

    created: list[tuple[str, dict]] = []
    deleted: list[str] = []

    monkeypatch.setattr(
        coord_mod.ir,
        "async_create_issue",
        lambda hass_, domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        coord_mod.ir,
        "async_delete_issue",
        lambda hass_, domain, issue_id: deleted.append(issue_id),
        raising=False,
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._handle_client_unauthorized()

    assert deleted == []
    assert coord._unauth_errors >= 2
    issue_id, payload = created[-1]
    assert issue_id == "reauth_required"
    placeholders = payload["translation_placeholders"]
    assert placeholders["site_id"] == coord.site_id
    assert placeholders["site_name"] == "Garage Site"
    assert placeholders["last_status"] == "401"
    assert placeholders["last_error"] == "unauthorized"
    metrics = payload["data"]["site_metrics"]
    assert metrics["site_name"] == "Garage Site"
    assert metrics["last_error"] == "unauthorized"


@pytest.mark.asyncio
async def test_async_start_stop_trigger_paths(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {RANDOM_SERIAL: {"charging_level": 18, "plugged": True}}
    coord.last_set_amps = {}

    coord.require_plugged = MagicMock()
    coord.set_last_set_amps = MagicMock()
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.async_request_refresh = AsyncMock()

    coordinator_data = {RANDOM_SERIAL: {"plugged": False, "charging_level": 20}}
    coord.data = coordinator_data

    async def _trigger_message(sn, message):
        return {"sent": message, "serial": sn}

    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value=None),
        start_live_stream=AsyncMock(
            return_value={"status": "accepted", "duration_s": 900}
        ),
        trigger_message=AsyncMock(side_effect=_trigger_message),
    )

    await coord.async_start_charging(RANDOM_SERIAL, connector_id=None, fallback_amps=24)
    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 20, 1, include_level=None, strict_preference=False
    )
    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, True)

    coord.client.start_charging.reset_mock()
    coord.client.start_charging.return_value = {"status": "not_ready"}
    result = await coord.async_start_charging(
        RANDOM_SERIAL, requested_amps=10, connector_id=2, allow_unplugged=True
    )
    assert result == {"status": "not_ready"}

    await coord.async_stop_charging(RANDOM_SERIAL, allow_unplugged=False)
    coord.client.stop_charging.assert_awaited_once_with(RANDOM_SERIAL)
    coord.require_plugged.assert_called()

    reply = await coord.async_trigger_ocpp_message(RANDOM_SERIAL, "Status")
    coord.client.trigger_message.assert_awaited_once_with(RANDOM_SERIAL, "Status")
    assert reply["sent"] == "Status"


@pytest.mark.asyncio
async def test_async_start_charging_manual_mode_sends_requested_amps(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {
        RANDOM_SERIAL: {
            "plugged": True,
            "charging_level": 26,
            "charge_mode_pref": "MANUAL_CHARGING",
        }
    }
    coord.last_set_amps = {}
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value=None),
        set_charge_mode=AsyncMock(return_value={"status": "ok"}),
        start_live_stream=AsyncMock(
            return_value={"status": "accepted", "duration_s": 900}
        ),
    )
    coord.async_request_refresh = AsyncMock()

    await coord.async_start_charging(RANDOM_SERIAL)

    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 26, 1, include_level=True, strict_preference=True
    )
    coord.client.set_charge_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_start_and_stop_preserve_scheduled_mode(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {
        RANDOM_SERIAL: {
            "plugged": True,
            "charging_level": 18,
            "charge_mode_pref": "SCHEDULED_CHARGING",
        }
    }
    coord.last_set_amps = {}
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value={"status": "ok"}),
        set_charge_mode=AsyncMock(return_value={"status": "ok"}),
        start_live_stream=AsyncMock(
            return_value={"status": "accepted", "duration_s": 900}
        ),
    )
    coord.async_request_refresh = AsyncMock()

    await coord.async_start_charging(RANDOM_SERIAL)
    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 18, 1, include_level=True, strict_preference=True
    )
    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "SCHEDULED_CHARGING"
    )

    coord.client.set_charge_mode.reset_mock()
    await coord.async_stop_charging(RANDOM_SERIAL)
    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "SCHEDULED_CHARGING"
    )


@pytest.mark.asyncio
async def test_async_start_charging_green_mode_omits_amp_payload(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}
    coord.data = {
        RANDOM_SERIAL: {
            "plugged": True,
            "charging_level": 30,
            "charge_mode_pref": "GREEN_CHARGING",
        }
    }
    coord.last_set_amps = {}
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.client = SimpleNamespace(
        start_charging=AsyncMock(return_value={"status": "ok"}),
        stop_charging=AsyncMock(return_value=None),
        set_charge_mode=AsyncMock(return_value={"status": "ok"}),
        start_live_stream=AsyncMock(
            return_value={"status": "accepted", "duration_s": 900}
        ),
    )
    coord.async_request_refresh = AsyncMock()

    await coord.async_start_charging(RANDOM_SERIAL)

    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 30, 1, include_level=False, strict_preference=True
    )
    coord.client.set_charge_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_amp_restart_cancels_existing_task(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    pending = asyncio.Future()
    coord._amp_restart_tasks[RANDOM_SERIAL] = pending

    calls: list[tuple[str, float]] = []

    async def _fake_restart(sn: str, delay: float) -> None:
        calls.append((sn, delay))

    coord._async_restart_after_amp_change = _fake_restart  # type: ignore[assignment]

    tasks: list[asyncio.Task] = []

    def _capture(coro, name=None):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(hass, "async_create_task", _capture)

    coord.schedule_amp_restart(RANDOM_SERIAL, delay=12)

    assert pending.cancelled()
    assert tasks, "restart task should be scheduled"
    await tasks[0]
    assert calls == [(RANDOM_SERIAL, 12)]
    assert RANDOM_SERIAL not in coord._amp_restart_tasks


@pytest.mark.asyncio
async def test_schedule_amp_restart_handles_typeerror(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)

    calls: list[tuple[str, float]] = []

    async def _fake_restart(sn: str, delay: float) -> None:
        calls.append((sn, delay))

    coord._async_restart_after_amp_change = _fake_restart  # type: ignore[assignment]

    tasks: list[asyncio.Task] = []

    def _create_task(coro, name=None):
        if name is not None:
            coro.close()
            raise TypeError("name kw not supported")
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(hass, "async_create_task", _create_task)

    coord.schedule_amp_restart(RANDOM_SERIAL, delay=8)

    assert tasks, "fallback task should be scheduled without a name kwarg"
    await tasks[0]
    assert calls == [(RANDOM_SERIAL, 8)]


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_flow(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock()
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 5)

    coord.async_stop_charging.assert_awaited_once_with(
        RANDOM_SERIAL, hold_seconds=90.0, fast_seconds=60, allow_unplugged=True
    )
    sleep_mock.assert_awaited_once_with(5.0)
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_handles_start_error(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock(side_effect=ServiceValidationError("oops"))

    sleep_mock = AsyncMock()
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 0)

    sleep_mock.assert_not_awaited()
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("translation_key", "expected_reason"),
    [
        ("exceptions.charger_not_plugged", "not plugged in"),
        ("exceptions.auth_required", "authentication required"),
    ],
)
async def test_async_restart_after_amp_change_validation_reasons(
    hass, monkeypatch, caplog, translation_key, expected_reason
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    try:
        err = ServiceValidationError("oops", translation_key=translation_key)
    except TypeError:
        err = ServiceValidationError("oops")
        err.translation_key = translation_key
    coord.async_start_charging = AsyncMock(side_effect=err)

    caplog.set_level(logging.DEBUG)

    await coord._async_restart_after_amp_change(RANDOM_SERIAL, 0)

    assert f"because {expected_reason}" in caplog.text


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_handles_stop_error(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock(side_effect=RuntimeError("boom"))
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock()
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 10)

    coord.async_start_charging.assert_not_awaited()
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_invalid_delay_defaults(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock()
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, object())

    coord.async_stop_charging.assert_awaited_once_with(
        RANDOM_SERIAL, hold_seconds=90.0, fast_seconds=60, allow_unplugged=True
    )
    sleep_mock.assert_awaited_once_with(30.0)
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_sleep_error(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()

    sleep_mock = AsyncMock(side_effect=RuntimeError("timer boom"))
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 2)

    coord.async_stop_charging.assert_awaited_once()
    sleep_mock.assert_awaited_once_with(2.0)
    coord.async_start_charging.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_handles_generic_start_error(
    hass, monkeypatch
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock(side_effect=RuntimeError("start boom"))

    sleep_mock = AsyncMock()
    with patch("custom_components.enphase_ev.coordinator.asyncio.sleep", sleep_mock):
        await coord._async_restart_after_amp_change(RANDOM_SERIAL, 3)

    coord.async_stop_charging.assert_awaited_once()
    sleep_mock.assert_awaited_once_with(3.0)
    coord.async_start_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_fast_poll_kicked_on_external_toggle(hass, monkeypatch, load_fixture):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    idle_payload = load_fixture("status_idle.json")
    charging_payload = load_fixture("status_charging.json")

    class StubClient:
        def __init__(self, payload):
            self.payload = payload

        async def status(self):
            return copy.deepcopy(self.payload)

    client = StubClient(idle_payload)
    coord.client = client

    await coord._async_update_data()
    assert fast_windows == []

    client.payload = charging_payload
    await coord._async_update_data()
    assert fast_windows == [FAST_TOGGLE_POLL_HOLD_S]

    await coord._async_update_data()
    assert fast_windows == [FAST_TOGGLE_POLL_HOLD_S]

    client.payload = idle_payload
    await coord._async_update_data()
    assert fast_windows == [FAST_TOGGLE_POLL_HOLD_S, FAST_TOGGLE_POLL_HOLD_S]


@pytest.mark.asyncio
async def test_fast_poll_not_triggered_by_expectation_only(
    hass, monkeypatch, load_fixture
):
    coord = _make_coordinator(hass, monkeypatch)
    coord.serials = {RANDOM_SERIAL}

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    idle_payload = load_fixture("status_idle.json")

    class StubClient:
        def __init__(self, payload):
            self.payload = payload

        async def status(self):
            return copy.deepcopy(self.payload)

    client = StubClient(idle_payload)
    coord.client = client

    await coord._async_update_data()
    assert fast_windows == []

    coord.set_charging_expectation(RANDOM_SERIAL, True, hold_for=10)
    await coord._async_update_data()
    assert fast_windows == []


def test_record_actual_charging_clears_none_state(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)
    coord._last_actual_charging[RANDOM_SERIAL] = True

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    coord._record_actual_charging(RANDOM_SERIAL, None)

    assert RANDOM_SERIAL not in coord._last_actual_charging
    assert fast_windows == []


def test_record_actual_charging_ignores_repeated_state(hass, monkeypatch):
    coord = _make_coordinator(hass, monkeypatch)

    fast_windows: list[int] = []

    def _record_fast(duration=60):
        fast_windows.append(duration)

    coord.kick_fast = _record_fast  # type: ignore[assignment]

    coord._record_actual_charging(RANDOM_SERIAL, False)
    coord._record_actual_charging(RANDOM_SERIAL, False)

    assert coord._last_actual_charging[RANDOM_SERIAL] is False
    assert fast_windows == []


@pytest.mark.asyncio
async def test_runtime_serial_discovery(hass, monkeypatch, config_entry):
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

    class DummyClient:
        def __init__(self):
            self._calls = 0

        async def status(self):
            self._calls += 1
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    },
                    {
                        "sn": "NEW123456789",
                        "name": "Workshop EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    },
                ]
            }

        async def summary_v2(self):
            return [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "displayName": "Garage EV",
                    "maxCurrent": 48,
                },
                {
                    "serialNumber": "NEW123456789",
                    "displayName": "Workshop EV",
                    "maxCurrent": 32,
                    "hwVersion": "1.2.3",
                    "swVersion": "5.6.7",
                },
            ]

        async def charge_mode(self, sn: str):
            return None

        async def session_history(self, *args, **kwargs):
            return {"data": {"result": [], "hasMore": False}}

    cfg = dict(config_entry.data)
    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    coord.client = DummyClient()
    await coord.async_refresh()

    assert "NEW123456789" in coord.serials
    assert coord.iter_serials() == [RANDOM_SERIAL, "NEW123456789"]
    assert "NEW123456789" in coord.data
    assert coord.data["NEW123456789"]["display_name"] == "Workshop EV"


@pytest.mark.asyncio
async def test_first_refresh_defers_session_history(hass, monkeypatch, config_entry):
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

    class DummyClient:
        def __init__(self):
            self.history_calls = 0

        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    }
                ]
            }

        async def summary_v2(self):
            return [{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]

        async def charge_mode(self, sn: str):
            return None

        async def session_history(self, *args, **kwargs):
            self.history_calls += 1
            now = datetime.now(timezone.utc)
            epoch = now.timestamp()
            return {
                "data": {
                    "result": [
                        {
                            "sessionId": "42",
                            "startTime": epoch - 600,
                            "endTime": epoch - 300,
                            "aggEnergyValue": 1.234,
                            "activeChargeTime": 900,
                        }
                    ],
                    "hasMore": False,
                }
            }

    scheduled: list[tuple[tuple[str, ...], datetime]] = []
    original_schedule = coord_mod.EnphaseCoordinator._schedule_session_enrichment

    def capture_schedule(self, serials, day_local):
        scheduled.append((tuple(serials), day_local))
        return original_schedule(self, serials, day_local)

    monkeypatch.setattr(
        coord_mod.EnphaseCoordinator,
        "_schedule_session_enrichment",
        capture_schedule,
        raising=False,
    )

    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    client = DummyClient()
    coord.client = client

    await coord.async_refresh()

    assert client.history_calls == 0
    assert "status_s" in coord.phase_timings
    assert coord.data[RANDOM_SERIAL]["energy_today_sessions"] == []

    assert len(scheduled) == 1
    scheduled_serials, scheduled_day = scheduled[0]
    assert scheduled_serials == (RANDOM_SERIAL,)
    assert isinstance(scheduled_day, datetime)


@pytest.mark.asyncio
async def test_first_refresh_defers_warmup_only_calls(
    hass, monkeypatch, config_entry
) -> None:
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev import coordinator as coord_mod

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
        CONF_SITE_ONLY: False,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )

    class DummyClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    }
                ],
                "ts": 1_700_000_000,
            }

        async def summary_v2(self):
            return [{"serialNumber": RANDOM_SERIAL, "displayName": "Garage EV"}]

    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    coord.client = DummyClient()

    deferred_methods = {
        "_async_refresh_battery_site_settings": AsyncMock(),
        "_async_refresh_battery_status": AsyncMock(),
        "_async_refresh_battery_backup_history": AsyncMock(),
        "_async_refresh_battery_settings": AsyncMock(),
        "_async_refresh_battery_schedules": AsyncMock(),
        "_async_refresh_storm_guard_profile": AsyncMock(),
        "_async_refresh_storm_alert": AsyncMock(),
        "_async_refresh_grid_control_check": AsyncMock(),
        "_async_refresh_devices_inventory": AsyncMock(),
        "_async_refresh_dry_contact_settings": AsyncMock(),
        "_async_refresh_hems_devices": AsyncMock(),
        "_async_refresh_inverters": AsyncMock(),
        "_async_refresh_current_power_consumption": AsyncMock(),
        "_async_refresh_heatpump_power": AsyncMock(),
        "_async_resolve_charge_modes": AsyncMock(return_value={}),
        "_async_resolve_green_battery_settings": AsyncMock(return_value={}),
        "_async_resolve_auth_settings": AsyncMock(return_value={}),
    }
    for name, mock in deferred_methods.items():
        monkeypatch.setattr(coord, name, mock)

    monkeypatch.setattr(coord.energy, "_async_refresh_site_energy", AsyncMock())
    monkeypatch.setattr(coord.evse_timeseries, "async_refresh", AsyncMock())

    await coord.async_refresh()

    for mock in deferred_methods.values():
        mock.assert_not_awaited()
    coord.energy._async_refresh_site_energy.assert_not_awaited()
    coord.evse_timeseries.async_refresh.assert_not_awaited()
    assert "status_s" in coord.bootstrap_phase_timings
    assert "summary_s" in coord.bootstrap_phase_timings
    assert "site_energy_s" not in coord.bootstrap_phase_timings


def test_snapshot_helpers_and_discovery_capture_edge_paths(hass, monkeypatch) -> None:
    coord = _make_coordinator(hass, monkeypatch)

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    stamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert coord._snapshot_compatible_value(stamp) == stamp.isoformat()  # noqa: SLF001
    assert coord._snapshot_compatible_value(
        {BadStr(): "skip", "ok": 1}
    ) == {  # noqa: SLF001
        "ok": 1
    }
    assert coord._snapshot_compatible_value(BadStr()) is None  # noqa: SLF001

    assert coord._snapshot_bool(None) is None  # noqa: SLF001
    assert coord._snapshot_bool(1) is True  # noqa: SLF001
    assert coord._snapshot_bool(0.0) is False  # noqa: SLF001
    assert coord._snapshot_bool("enabled") is True  # noqa: SLF001
    assert coord._snapshot_bool("disabled") is False  # noqa: SLF001
    assert coord._snapshot_bool("maybe") is None  # noqa: SLF001

    assert coord.site_energy_channel_known(BadStr()) is False
    assert coord.site_energy_channel_known(" ") is False

    coord.energy = None  # type: ignore[assignment]
    assert coord._live_site_energy_channels() == set()  # noqa: SLF001

    coord.energy = SimpleNamespace(
        site_energy={BadStr(): 1, "grid_import": 1},
        site_energy_meta={
            "bucket_lengths": {
                BadStr(): 1,
                "": 1,
                "heatpump": 1,
                "water_heater": 1,
                "evse": 1,
                "solar_production": 1,
                "consumption": 1,
                "grid_export": 1,
                "battery_charge": 1,
                "battery_discharge": 1,
                "custom_flow": "truthy",
                "skip_empty": None,
                "ignored": 0,
            }
        },
    )
    channels = coord._live_site_energy_channels()  # noqa: SLF001
    assert channels >= {
        "grid_import",
        "heat_pump",
        "water_heater",
        "evse_charging",
        "solar_production",
        "consumption",
        "grid_export",
        "battery_charge",
        "battery_discharge",
        "custom_flow",
    }

    coord._hems_group_members = lambda *_args: [  # type: ignore[assignment]  # noqa: SLF001
        None,
        {"device-type": None},
        {"device-type": BadStr()},
        {"device-type": "IQ_GATEWAY"},
        {"device_type": "IQ_ENERGY_ROUTER", "device-uid": "LIVE-1"},
    ]
    live_records = coord._live_gateway_iq_energy_router_records()  # noqa: SLF001
    assert live_records == [{"device_type": "IQ_ENERGY_ROUTER", "device-uid": "LIVE-1"}]

    coord._hems_group_members = lambda *_args: []  # type: ignore[assignment]  # noqa: SLF001
    coord.energy = SimpleNamespace(
        site_energy={}, site_energy_meta={"bucket_lengths": {}}
    )
    coord._restored_site_energy_channels = {"heat_pump"}  # noqa: SLF001
    coord._restored_gateway_iq_energy_router_records = [  # noqa: SLF001
        {"device-uid": "REST-1", "device-type": "IQ_ENERGY_ROUTER"}
    ]
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._type_device_buckets = {"envoy": {"count": 1, "devices": []}}  # noqa: SLF001
    coord._battery_storage_order = ["BAT-1"]  # noqa: SLF001
    coord._battery_storage_data = {"BAT-1": {"name": "Battery 1"}}  # noqa: SLF001
    coord._inverter_order = ["INV-1"]  # noqa: SLF001
    coord._inverter_data = {"INV-1": {"name": "Inverter 1"}}  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_has_enpower = False  # noqa: SLF001

    snapshot = coord._capture_discovery_snapshot()  # noqa: SLF001
    assert snapshot["site_energy_channels"] == ["heat_pump"]
    assert snapshot["gateway_iq_energy_router_records"] == [
        {"device-uid": "REST-1", "device-type": "IQ_ENERGY_ROUTER"}
    ]

    coord._hems_inventory_ready = True  # noqa: SLF001
    assert coord.gateway_iq_energy_router_records() == []


@pytest.mark.asyncio
async def test_discovery_snapshot_restore_save_and_metrics_edge_paths(
    hass, monkeypatch
) -> None:
    coord = _make_coordinator(hass, monkeypatch)

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    ensure_serial = Mock()
    set_buckets = Mock()
    coord._ensure_serial_tracked = ensure_serial  # type: ignore[assignment]  # noqa: SLF001
    coord._set_type_device_buckets = set_buckets  # type: ignore[assignment]  # noqa: SLF001

    coord._apply_discovery_snapshot(  # noqa: SLF001
        {
            "serial_order": [None, BadStr(), "REST-1"],
            "type_device_buckets": {
                " ": {"devices": []},
                "envoy": {"devices": "bad", "count": "oops"},
                "bad": "bucket",
            },
            "type_device_order": ["envoy"],
            "battery_storage_order": ["", "BAT-1"],
            "battery_storage_data": {
                "": {},
                "BAT-1": {"name": "Battery"},
                "BAT-2": "bad",
            },
            "inverter_order": ["", "INV-1"],
            "inverter_data": {
                "": {},
                "INV-1": {"name": "Inverter"},
                "INV-2": "bad",
            },
            "battery_has_encharge": "enabled",
            "battery_has_enpower": "disabled",
            "site_energy_channels": ["", "heat_pump"],
            "gateway_iq_energy_router_records": [{"device-uid": "REST-1"}, "bad"],
        }
    )

    ensure_serial.assert_called_once_with("REST-1")
    set_buckets.assert_called_once()
    assert set_buckets.call_args.kwargs["authoritative"] is False
    assert coord._battery_storage_order == ["BAT-1"]  # noqa: SLF001
    assert coord._inverter_order == ["INV-1"]  # noqa: SLF001
    assert coord._restored_site_energy_channels == {"heat_pump"}  # noqa: SLF001
    assert coord._restored_gateway_iq_energy_router_records == [  # noqa: SLF001
        {"device-uid": "REST-1"}
    ]

    coord = _make_coordinator(hass, monkeypatch)
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._set_type_device_buckets(  # noqa: SLF001
        {"envoy": {"count": 1, "devices": [{"serial_number": "GW-1"}]}},
        ["envoy"],
        authoritative=False,
    )
    assert coord._devices_inventory_ready is True  # noqa: SLF001

    coord._discovery_snapshot_loaded = True  # noqa: SLF001
    coord._discovery_snapshot_store = SimpleNamespace(  # noqa: SLF001
        async_load=AsyncMock(side_effect=AssertionError("no load"))
    )
    await coord.async_restore_discovery_state()

    coord._discovery_snapshot_loaded = False  # noqa: SLF001
    coord._discovery_snapshot_store = SimpleNamespace(  # noqa: SLF001
        async_load=AsyncMock(side_effect=RuntimeError("boom"))
    )
    await coord.async_restore_discovery_state()
    assert coord._devices_inventory_ready is False  # noqa: SLF001
    assert coord._hems_inventory_ready is False  # noqa: SLF001
    assert coord._site_energy_discovery_ready is False  # noqa: SLF001

    coord._capture_discovery_snapshot = Mock(return_value={"serial_order": []})  # type: ignore[assignment]  # noqa: SLF001
    coord._discovery_snapshot_store = SimpleNamespace(  # noqa: SLF001
        async_save=AsyncMock(side_effect=RuntimeError("boom"))
    )
    await coord._async_save_discovery_snapshot()  # noqa: SLF001

    create_calls: list[object] = []

    def _capture_create_task(coro):
        create_calls.append(coro)
        coro.close()
        return None

    object.__setattr__(coord.hass, "async_create_task", _capture_create_task)
    scheduled: list = []

    def _capture_call_later(_hass, _delay, callback):
        scheduled.append(callback)
        return lambda: None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(coord_mod, "async_call_later", _capture_call_later)
    coord._discovery_snapshot_save_cancel = lambda: None  # type: ignore[assignment]  # noqa: SLF001
    coord._schedule_discovery_snapshot_save()  # noqa: SLF001
    assert scheduled == []

    coord._discovery_snapshot_save_cancel = None  # noqa: SLF001
    coord._schedule_discovery_snapshot_save()  # noqa: SLF001
    assert len(scheduled) == 1
    coord._discovery_snapshot_pending = False  # noqa: SLF001
    scheduled[0](datetime.now(tz=timezone.utc))
    assert create_calls == []

    coord._discovery_snapshot_pending = True  # noqa: SLF001
    scheduled[0](datetime.now(tz=timezone.utc))
    assert len(create_calls) == 1

    coord.energy = None  # type: ignore[assignment]
    coord._sync_site_energy_discovery_state()  # noqa: SLF001
    coord.energy = SimpleNamespace(_site_energy_cache_ts=1)
    coord._sync_site_energy_discovery_state()  # noqa: SLF001
    assert coord._site_energy_discovery_ready is True  # noqa: SLF001

    coord._system_dashboard_type_summaries = {"envoy": {}}  # noqa: SLF001
    coord._async_refresh_system_dashboard = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    await coord.async_ensure_system_dashboard_diagnostics()
    coord._async_refresh_system_dashboard.assert_not_awaited()

    coord._system_dashboard_type_summaries = {}  # noqa: SLF001
    coord._system_dashboard_hierarchy_summary = {}  # noqa: SLF001
    await coord.async_ensure_system_dashboard_diagnostics()
    coord._async_refresh_system_dashboard.assert_awaited_once_with(force=True)

    coord._warmup_in_progress = True  # noqa: SLF001
    coord._warmup_last_error = "boom"  # noqa: SLF001
    coord._bootstrap_phase_timings = {"status_s": 0.1}  # noqa: SLF001
    coord._warmup_phase_timings = {"discovery_s": 0.2}  # noqa: SLF001
    metrics = coord.collect_site_metrics()
    assert metrics["bootstrap_phase_timings"] == {"status_s": 0.1}
    assert metrics["warmup_phase_timings"] == {"discovery_s": 0.2}
    assert metrics["warmup_in_progress"] is True
    assert metrics["warmup_last_error"] == "boom"


@pytest.mark.asyncio
async def test_startup_warmup_runner_and_task_edge_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.async_set_updated_data = Mock(side_effect=RuntimeError("publish"))  # type: ignore[assignment]
    coord._schedule_discovery_snapshot_save = Mock()  # type: ignore[assignment]  # noqa: SLF001
    coord._async_refresh_battery_site_settings = None  # type: ignore[assignment]  # noqa: SLF001

    await coord._async_startup_warmup_runner()  # noqa: SLF001

    assert coord._warmup_last_error == "publish"  # noqa: SLF001
    assert coord._warmup_in_progress is False  # noqa: SLF001
    assert "discovery_s" in coord._warmup_phase_timings  # noqa: SLF001
    coord._schedule_discovery_snapshot_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory()
    coord._schedule_discovery_snapshot_save = Mock()  # type: ignore[assignment]  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await coord._async_startup_warmup_runner()  # noqa: SLF001
    assert coord._warmup_in_progress is False  # noqa: SLF001
    coord._schedule_discovery_snapshot_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory()
    coord._schedule_discovery_snapshot_save = Mock()  # type: ignore[assignment]  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=RuntimeError("heatpump")
    )
    await coord._async_startup_warmup_runner()  # noqa: SLF001
    assert "heatpump_power_s" in coord._warmup_phase_timings  # noqa: SLF001
    coord._schedule_discovery_snapshot_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory()
    coord._schedule_discovery_snapshot_save = Mock()  # type: ignore[assignment]  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await coord._async_startup_warmup_runner()  # noqa: SLF001
    assert coord._warmup_in_progress is False  # noqa: SLF001
    coord._schedule_discovery_snapshot_save.assert_called_once()  # type: ignore[attr-defined]

    coord = coordinator_factory()
    coord._warmup_task = SimpleNamespace(done=lambda: False)  # type: ignore[assignment]  # noqa: SLF001
    create_calls: list[object] = []

    async def _runner() -> None:
        return None

    coord._async_startup_warmup_runner = _runner  # type: ignore[assignment]  # noqa: SLF001

    def _create_task(coro, name=None):
        create_calls.append(name)
        coro.close()
        if name is not None:
            raise TypeError("no name support")
        return "task"

    object.__setattr__(coord.hass, "async_create_task", _create_task)
    await coord.async_start_startup_warmup()
    assert create_calls == []

    coord._warmup_task = None  # noqa: SLF001
    await coord.async_start_startup_warmup()
    assert create_calls == [f"{DOMAIN}_warmup_{coord.site_id}", None]
    assert coord._warmup_task == "task"  # noqa: SLF001


@pytest.mark.asyncio
async def test_startup_warmup_helper_refreshes_cover_fallback_and_merge_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.data = {RANDOM_SERIAL: {"name": "Garage EV"}}
    set_updated = Mock()
    coord.async_set_updated_data = set_updated  # type: ignore[assignment]

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod.dt_util, "as_local", Mock(side_effect=RuntimeError("boom"))
    )

    coord.energy._async_refresh_site_energy = AsyncMock()  # noqa: SLF001
    coord._sync_site_energy_issue = Mock()  # type: ignore[assignment]  # noqa: SLF001
    await coord._async_refresh_site_energy_for_warmup()  # noqa: SLF001
    coord.energy._async_refresh_site_energy.assert_awaited_once()

    coord.evse_timeseries.async_refresh = AsyncMock()
    coord.evse_timeseries.merge_charger_payloads = Mock(
        side_effect=lambda payload, day_local=None: payload[RANDOM_SERIAL].update(
            {"timeseries": True}
        )
    )
    await coord._async_refresh_evse_timeseries_for_warmup()  # noqa: SLF001
    merged_timeseries = set_updated.call_args_list[-1].args[0]
    assert merged_timeseries[RANDOM_SERIAL]["timeseries"] is True

    coord._async_enrich_sessions = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={
            RANDOM_SERIAL: [{"energy_kwh": 1.5}],
            "missing": [{"energy_kwh": 0.2}],
        }
    )
    coord._sum_session_energy = Mock(return_value=1.5)  # type: ignore[assignment]  # noqa: SLF001
    coord._sync_session_history_issue = Mock()  # type: ignore[assignment]  # noqa: SLF001
    await coord._async_refresh_session_state_for_warmup()  # noqa: SLF001
    merged_sessions = set_updated.call_args_list[-1].args[0]
    assert merged_sessions[RANDOM_SERIAL]["energy_today_sessions"] == [
        {"energy_kwh": 1.5}
    ]
    assert merged_sessions[RANDOM_SERIAL]["energy_today_sessions_kwh"] == 1.5
    coord._sync_session_history_issue.assert_called_once()

    coord._async_enrich_sessions = AsyncMock(return_value={})  # type: ignore[assignment]  # noqa: SLF001
    await coord._async_refresh_session_state_for_warmup()  # noqa: SLF001

    coord.iter_serials = lambda: [RANDOM_SERIAL, "SECONDARY"]  # type: ignore[assignment]
    coord._async_resolve_charge_modes = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={RANDOM_SERIAL: "SCHEDULED"}
    )
    coord._async_resolve_green_battery_settings = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={RANDOM_SERIAL: (True, True)}
    )
    coord._async_resolve_auth_settings = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value={RANDOM_SERIAL: (True, False, True, True)}
    )
    await coord._async_refresh_secondary_evse_state_for_warmup()  # noqa: SLF001
    merged_secondary = set_updated.call_args_list[-1].args[0]
    assert merged_secondary[RANDOM_SERIAL]["charge_mode_pref"] == "SCHEDULED"
    assert merged_secondary[RANDOM_SERIAL]["green_battery_enabled"] is True
    assert merged_secondary[RANDOM_SERIAL]["app_auth_supported"] is True
    assert merged_secondary[RANDOM_SERIAL]["rfid_auth_supported"] is True
    assert merged_secondary[RANDOM_SERIAL]["auth_required"] is True

    coord.iter_serials = lambda: [""]  # type: ignore[assignment]
    await coord._async_refresh_secondary_evse_state_for_warmup()  # noqa: SLF001


@pytest.mark.asyncio
async def test_charge_mode_lookup_skipped_when_embedded(
    hass, monkeypatch, config_entry
):
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

    charge_mode_called = False

    async def fake_resolve(self, serials):
        nonlocal charge_mode_called
        charge_mode_called = True
        return {}

    monkeypatch.setattr(
        coord_mod.EnphaseCoordinator,
        "_async_resolve_charge_modes",
        fake_resolve,
        raising=False,
    )

    class DummyClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "chargeMode": "IMMEDIATE",
                        "connectors": [{}],
                        "session_d": {},
                        "sch_d": {},
                        "charging": False,
                    }
                ]
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn: str):
            return "SCHEDULED"

        async def session_history(self, *args, **kwargs):
            return {"data": {"result": [], "hasMore": False}}

    coord = EnphaseCoordinator(hass, cfg, config_entry=config_entry)
    coord.client = DummyClient()

    await coord.async_refresh()

    assert not charge_mode_called


@pytest.mark.asyncio
async def test_restore_discovery_state_applies_snapshot_topology(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._hems_inventory_ready = False  # noqa: SLF001
    coord._discovery_snapshot_store = SimpleNamespace(  # noqa: SLF001
        async_load=AsyncMock(
            return_value={
                "serial_order": [RANDOM_SERIAL, "RESTORED123"],
                "type_device_order": ["envoy", "heatpump", "microinverter"],
                "type_device_buckets": {
                    "envoy": {
                        "type_label": "Gateway",
                        "count": 1,
                        "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
                    },
                    "heatpump": {
                        "type_label": "Heat Pump",
                        "count": 1,
                        "devices": [{"serial_number": "HP-1", "name": "Heat Pump"}],
                    },
                    "microinverter": {
                        "type_label": "Microinverters",
                        "count": 1,
                        "devices": [{"serial_number": "INV-1", "name": "Inverter"}],
                    },
                },
                "battery_storage_order": ["BAT-1"],
                "battery_storage_data": {
                    "BAT-1": {"serial_number": "BAT-1", "name": "Battery 1"}
                },
                "inverter_order": ["INV-1"],
                "inverter_data": {
                    "INV-1": {"serial_number": "INV-1", "name": "Inverter 1"}
                },
                "battery_has_encharge": True,
                "battery_has_enpower": True,
                "site_energy_channels": ["heat_pump", "water_heater"],
                "gateway_iq_energy_router_records": [
                    {
                        "device-uid": "ROUTER-1",
                        "device-type": "IQ_ENERGY_ROUTER",
                        "name": "IQ Energy Router 1",
                    }
                ],
            }
        )
    )

    await coord.async_restore_discovery_state()

    assert coord.iter_serials() == [RANDOM_SERIAL, "RESTORED123"]
    assert coord.type_bucket("heatpump")["count"] == 1
    assert coord._battery_storage_order == ["BAT-1"]  # noqa: SLF001
    assert coord._inverter_order == ["INV-1"]  # noqa: SLF001
    assert coord._devices_inventory_ready is False  # noqa: SLF001
    assert coord.site_energy_channel_known("heat_pump") is True
    assert coord.site_energy_channel_known("water_heater") is True
    router_records = coord.gateway_iq_energy_router_records()
    assert router_records[0]["device-uid"] == "ROUTER-1"


def test_restored_site_energy_and_router_hints_expire_after_authoritative_refresh(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._restored_site_energy_channels = {  # noqa: SLF001
        "heat_pump",
        "water_heater",
    }
    coord._restored_gateway_iq_energy_router_records = [  # noqa: SLF001
        {
            "device-uid": "ROUTER-1",
            "device-type": "IQ_ENERGY_ROUTER",
            "name": "IQ Energy Router 1",
        }
    ]

    assert coord.site_energy_channel_known("heat_pump") is True
    assert coord.gateway_iq_energy_router_records()[0]["device-uid"] == "ROUTER-1"

    coord._site_energy_discovery_ready = True  # noqa: SLF001
    coord._hems_inventory_ready = True  # noqa: SLF001

    assert coord.site_energy_channel_known("heat_pump") is False
    assert coord.site_energy_channel_known("water_heater") is False
    assert coord.gateway_iq_energy_router_records() == []


@pytest.mark.asyncio
async def test_http_backoff_respects_configured_slow_interval(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {OPT_SLOW_POLL_INTERVAL: 300}

        def async_on_unload(self, _cb):
            return None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *_: 1.0)
    scheduled: dict[str, float | object] = {}

    def fake_call_later(_hass, delay, callback):
        scheduled["delay"] = delay
        scheduled["callback"] = callback

        def _cancel():
            scheduled["cancelled"] = True

        return _cancel

    monkeypatch.setattr(coord_mod, "async_call_later", fake_call_later)

    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubRespErr(aiohttp.ClientResponseError):
        def __init__(self):
            req = aiohttp.RequestInfo(
                url=aiohttp.client.URL("https://example"),
                method="GET",
                headers={},
                real_url=aiohttp.client.URL("https://example"),
            )
            super().__init__(
                request_info=req,
                history=(),
                status=503,
                message="",
                headers={},
            )

    class FailingClient:
        async def status(self):
            raise StubRespErr()

    coord.client = FailingClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None
    remaining = coord._backoff_until - time.monotonic()
    assert remaining >= 295
    assert coord._backoff_cancel is not None
    assert scheduled["delay"] >= 300


@pytest.mark.asyncio
async def test_network_backoff_respects_slow_interval(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {OPT_SLOW_POLL_INTERVAL: 200}

        def async_on_unload(self, _cb):
            return None

    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(coord_mod.random, "uniform", lambda *_: 1.0)
    scheduled: dict[str, float | object] = {}

    def fake_call_later(_hass, delay, callback):
        scheduled["delay"] = delay
        scheduled["callback"] = callback

        def _cancel():
            scheduled["cancelled"] = True

        return _cancel

    monkeypatch.setattr(coord_mod, "async_call_later", fake_call_later)

    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class FailingClient:
        async def status(self):
            raise aiohttp.ClientError()

    coord.client = FailingClient()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None
    remaining = coord._backoff_until - time.monotonic()
    assert remaining >= 195
    assert coord._backoff_cancel is not None
    assert scheduled["delay"] >= 200


@pytest.mark.asyncio
async def test_dynamic_poll_switch(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_FAST_POLL_INTERVAL,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    # no extra imports

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    options = {OPT_FAST_POLL_INTERVAL: 5, OPT_SLOW_POLL_INTERVAL: 20}

    class DummyEntry:
        def __init__(self, options):
            self.options = options

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry(options)
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

    # Charging -> fast
    payload_charging = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_charging)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 5

    # Idle -> temporarily stay fast due to recent toggle
    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_idle)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 5

    # Once the boost expires, fall back to the configured slow interval
    coord._fast_until = None
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 20

    # Connector status indicates charging even if flag remains false -> treat as active
    payload_conn_only = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "CHARGING"}],
            }
        ]
    }
    coord.client = StubClient(payload_conn_only)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 5
    assert coord.data[RANDOM_SERIAL]["charging"] is True

    # EVSE-side suspension should be treated as paused (not charging)
    payload_conn_suspended = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "SUSPENDED_EVSE"}],
            }
        ]
    }
    coord.client = StubClient(payload_conn_suspended)
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 5
    assert coord.data[RANDOM_SERIAL]["charging"] is False
    assert coord.data[RANDOM_SERIAL]["suspended_by_evse"] is True

    coord._fast_until = None
    coord.data = await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 20


@pytest.mark.asyncio
async def test_auto_resume_when_evse_suspended(monkeypatch, hass):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    coord = EnphaseCoordinator(hass, cfg)

    class StubClient:
        def __init__(self):
            self.payload = {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                        "connectors": [{"connectorStatusType": "SUSPENDED_EVSE"}],
                    }
                ]
            }
            self.start_calls: list[tuple[str, int, int]] = []

        async def status(self):
            return self.payload

        async def summary_v2(self):
            return []

        async def start_charging(
            self,
            sn,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((sn, amps, connector_id))
            return {"status": "ok"}

    client = StubClient()
    coord.client = client
    coord.async_request_refresh = AsyncMock()

    coord.set_desired_charging(RANDOM_SERIAL, True)
    coord._auto_resume_attempts.clear()

    await coord._async_update_data()
    await hass.async_block_till_done()

    assert client.start_calls == [(RANDOM_SERIAL, 32, 1)]
    assert coord.async_request_refresh.await_count >= 1


@pytest.mark.asyncio
async def test_charging_expectation_hold(monkeypatch, hass):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class TimeKeeper:
        def __init__(self):
            self.value = 1_000.0

        def monotonic(self):
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += float(seconds)

    tk = TimeKeeper()
    monkeypatch.setattr(coord_mod.time, "monotonic", tk.monotonic)
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    coord = EnphaseCoordinator(hass, cfg)

    class StubClient:
        def __init__(self, payload):
            self.payload = payload

        async def status(self):
            return self.payload

        async def summary_v2(self):
            return []

    payload_charging = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
            }
        ]
    }
    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "AVAILABLE"}],
            }
        ]
    }

    client = StubClient(payload_charging)
    coord.client = client
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True

    coord.set_charging_expectation(RANDOM_SERIAL, False, hold_for=90)
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is False

    tk.advance(60)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is False

    tk.advance(40)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True
    assert RANDOM_SERIAL not in coord._pending_charging

    client.payload = payload_idle
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is False

    coord.set_charging_expectation(RANDOM_SERIAL, True, hold_for=90)
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True
    assert RANDOM_SERIAL in coord._pending_charging

    client.payload = payload_charging
    tk.advance(1)
    coord.data = await coord._async_update_data()
    assert coord.data[RANDOM_SERIAL]["charging"] is True
    assert RANDOM_SERIAL not in coord._pending_charging


@pytest.mark.asyncio
async def test_default_fast_interval_used_when_charging(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_FAST_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry()
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn: str):
            return "IMMEDIATE"

    payload = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": True,
                "pluggedIn": True,
                "connectors": [{"connectorStatusType": "AVAILABLE"}],
            }
        ]
    }
    coord.client = StubClient(payload)
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == DEFAULT_FAST_POLL_INTERVAL


@pytest.mark.asyncio
async def test_summary_refresh_speed_up_when_charging(hass, monkeypatch):
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
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry()
    from custom_components.enphase_ev import coordinator as coord_mod

    current = {"value": 1000.0}

    def fake_monotonic():
        return current["value"]

    monkeypatch.setattr(coord_mod.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self):
            self.summary_calls = 0

        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                        "connectors": [{"connectorStatusType": "AVAILABLE"}],
                    }
                ]
            }

        async def summary_v2(self):
            self.summary_calls += 1
            return [
                {
                    "serialNumber": RANDOM_SERIAL,
                    "lifeTimeConsumption": 1000.0,
                    "lastReportedAt": "2025-10-17T12:00:00Z[UTC]",
                }
            ]

        async def charge_mode(self, sn: str):
            return "IMMEDIATE"

    stub = StubClient()
    coord.client = stub

    await coord._async_update_data()
    assert stub.summary_calls == 1

    current["value"] += 15.0
    await coord._async_update_data()
    assert stub.summary_calls == 1

    current["value"] += 15.0
    await coord._async_update_data()
    assert stub.summary_calls == 2

    current["value"] += 70.0
    await coord._async_update_data()
    assert stub.summary_calls == 3


@pytest.mark.asyncio
async def test_streaming_prefers_fast(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_FAST_POLL_INTERVAL,
        OPT_FAST_WHILE_STREAMING,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    # no extra imports

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    options = {
        OPT_FAST_POLL_INTERVAL: 6,
        OPT_SLOW_POLL_INTERVAL: 22,
        OPT_FAST_WHILE_STREAMING: True,
    }

    class DummyEntry:
        def __init__(self, options):
            self.options = options

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry(options)
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_idle)
    coord._streaming = True
    coord._streaming_until = time.monotonic() + 60
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 6


@pytest.mark.asyncio
async def test_streaming_reverts_to_configured_scan_interval(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        OPT_FAST_POLL_INTERVAL,
        OPT_FAST_WHILE_STREAMING,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }
    options = {
        OPT_FAST_POLL_INTERVAL: 6,
        OPT_FAST_WHILE_STREAMING: True,
    }

    class DummyEntry:
        def __init__(self, options):
            self.options = options

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry(options)
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_idle)

    coord._streaming = True
    coord._streaming_until = time.monotonic() + 60
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 6

    coord._streaming = False
    coord._streaming_until = None
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == 15


@pytest.mark.asyncio
async def test_session_history_enrichment(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                    }
                ]
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn):
            return None

    coord.client = StubClient()

    async def _fake_sessions(self, sn, *, day_local=None):
        return [
            {
                "session_id": "stub-1",
                "start": "2025-10-16T00:00:00+00:00",
                "end": "2025-10-16T01:00:00+00:00",
                "energy_kwh_total": 4.5,
                "energy_kwh": 4.5,
                "active_charge_time_s": 3600,
                "auth_type": None,
                "auth_identifier": None,
                "auth_token": None,
                "miles_added": 15.0,
                "session_cost": 1.1,
                "avg_cost_per_kwh": 0.24,
                "cost_calculated": True,
                "manual_override": False,
                "session_cost_state": "COST_CALCULATED",
                "charge_profile_stack_level": 0,
            },
            {
                "session_id": "stub-2",
                "start": "2025-10-16T04:00:00+00:00",
                "end": "2025-10-16T05:30:00+00:00",
                "energy_kwh_total": 2.0,
                "energy_kwh": 2.0,
                "active_charge_time_s": 5400,
                "auth_type": "RFID",
                "auth_identifier": "user",
                "auth_token": "token",
                "miles_added": 8.0,
                "session_cost": 0.6,
                "avg_cost_per_kwh": 0.3,
                "cost_calculated": True,
                "manual_override": True,
                "session_cost_state": "COST_CALCULATED",
                "charge_profile_stack_level": 4,
            },
            {
                "session_id": "stub-3",
                "start": "2025-10-15T23:30:00+00:00",
                "end": "2025-10-16T00:30:00+00:00",
                "energy_kwh_total": 4.0,
                "energy_kwh": 2.0,
                "active_charge_time_s": 3600,
                "auth_type": None,
                "auth_identifier": None,
                "auth_token": None,
                "miles_added": 10.0,
                "session_cost": 0.5,
                "avg_cost_per_kwh": 0.25,
                "cost_calculated": True,
                "manual_override": False,
                "session_cost_state": "COST_CALCULATED",
                "charge_profile_stack_level": 2,
            },
        ]

    coord._async_fetch_sessions_today = _fake_sessions.__get__(coord, coord.__class__)

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    assert st["energy_today_sessions_kwh"] == 0.0
    assert st["energy_today_sessions"] == []

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    assert st["energy_today_sessions_kwh"] == pytest.approx(8.5, abs=1e-3)
    assert len(st["energy_today_sessions"]) == 3
    cross_midnight = st["energy_today_sessions"][2]
    assert cross_midnight["energy_kwh_total"] == pytest.approx(4.0)
    assert cross_midnight["energy_kwh"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_session_history_prefers_last_session_day_when_idle(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )

    now_local = datetime(2025, 12, 31, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    last_end = int((now_local - timedelta(days=1, hours=2)).timestamp())
    last_start = last_end - 3600

    class StubClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                        "connectors": [{}],
                        "session_d": {
                            "start_time": last_start,
                            "plg_out_at": last_end,
                        },
                    }
                ]
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn):
            return None

    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())
    coord.client = StubClient()

    captured: dict[str, object] = {}

    def _capture_schedule(serials, day_local):
        captured["serials"] = list(serials)
        captured["day_local"] = day_local

    coord.session_history.schedule_enrichment = _capture_schedule  # type: ignore[assignment]

    await coord._async_update_data()

    expected_day = datetime.fromtimestamp(last_end, tz=timezone.utc).date()
    assert captured["serials"] == [RANDOM_SERIAL]
    assert captured["day_local"].date() == expected_day


@pytest.mark.asyncio
async def test_session_history_day_handles_bad_timestamps(hass, monkeypatch):
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    class BadFloat:
        def __float__(self):
            raise ValueError("boom")

    await hass.config.async_set_time_zone("UTC")

    now_local = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
    day_local_default = coord_mod.dt_util.as_local(now_local)
    target_ts = int((now_local - timedelta(hours=2)).timestamp())
    orig_as_local = coord_mod.dt_util.as_local

    def _fake_as_local(value):
        if isinstance(value, datetime) and abs(value.timestamp() - target_ts) < 1.0:
            raise ValueError("tz boom")
        return orig_as_local(value)

    monkeypatch.setattr(coord_mod.dt_util, "as_local", _fake_as_local)

    bad_payload = {"charging": False, "session_end": BadFloat()}
    assert (
        EnphaseCoordinator._session_history_day(bad_payload, day_local_default)
        == day_local_default
    )

    overflow_payload = {"charging": False, "session_end": 10**20}
    assert (
        EnphaseCoordinator._session_history_day(overflow_payload, day_local_default)
        == day_local_default
    )

    as_local_payload = {"charging": False, "session_end": target_ts}
    result = EnphaseCoordinator._session_history_day(
        as_local_payload, day_local_default
    )
    assert abs(result.timestamp() - target_ts) < 1.0


@pytest.mark.asyncio
@pytest.mark.session_history_real
async def test_session_history_cross_midnight_split(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    day_now = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: day_now)

    client = coord.client
    calls: list[dict] = []

    async def fake_session_history(
        self, sn, *, start_date, end_date, offset, limit, **_kwargs
    ):
        calls.append(
            {
                "sn": sn,
                "start_date": start_date,
                "end_date": end_date,
                "offset": offset,
                "limit": limit,
            }
        )
        return {
            "data": {
                "result": [
                    {
                        "sessionId": 1,
                        "startTime": "2025-10-15T23:30:00Z[UTC]",
                        "endTime": "2025-10-16T01:30:00Z[UTC]",
                        "aggEnergyValue": 6.0,
                        "activeChargeTime": 7200,
                    },
                    {
                        "sessionId": 2,
                        "startTime": "2025-10-16T04:00:00Z[UTC]",
                        "endTime": "2025-10-16T05:00:00Z[UTC]",
                        "aggEnergyValue": 3.0,
                        "activeChargeTime": 3600,
                    },
                ],
                "hasMore": False,
                "startDate": start_date,
                "endDate": end_date,
            }
        }

    monkeypatch.setattr(
        client,
        "session_history",
        fake_session_history.__get__(client, client.__class__),
        raising=False,
    )

    async def fake_filter_criteria(self, **_kwargs):
        return {"data": [{"id": RANDOM_SERIAL}]}

    monkeypatch.setattr(
        client,
        "session_history_filter_criteria",
        fake_filter_criteria.__get__(client, client.__class__),
        raising=False,
    )

    sessions = await coord._async_fetch_sessions_today(RANDOM_SERIAL, day_local=day_now)
    assert calls, "session_history should have been called"
    assert len(sessions) == 2
    assert len(calls) == 1

    first = sessions[0]
    assert first["energy_kwh_total"] == pytest.approx(6.0)
    # Only 1.5 hours of a 2 hour session occur within the day -> 75%
    assert first["energy_kwh"] == pytest.approx(4.5)
    assert first["active_charge_time_overlap_s"] == 5400

    second = sessions[1]
    assert second["energy_kwh"] == pytest.approx(3.0)

    # Cached result should be reused
    calls.clear()
    again = await coord._async_fetch_sessions_today(RANDOM_SERIAL, day_local=day_now)
    assert not calls
    assert again == sessions


@pytest.mark.asyncio
@pytest.mark.session_history_real
async def test_session_history_unauthorized_falls_back(hass, monkeypatch):
    from custom_components.enphase_ev.api import Unauthorized
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    class StubClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": False,
                        "pluggedIn": True,
                    }
                ],
                "ts": 1757299870275,
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn):
            return None

        async def session_history(self, *args, **kwargs):
            raise Unauthorized()

    coord.client = StubClient()

    data = await coord._async_update_data()
    st = data[RANDOM_SERIAL]
    assert st["energy_today_sessions"] == []
    assert st["energy_today_sessions_kwh"] == 0.0


@pytest.mark.asyncio
@pytest.mark.session_history_real
async def test_session_history_inflight_session_counts_energy(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    await hass.config.async_set_time_zone("UTC")
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=DummyEntry())

    # Fix "now" for the coordinator so the ongoing session overlaps the day
    now_local = datetime(2025, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: now_local)

    class StubClient:
        async def status(self):
            return {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Garage EV",
                        "charging": True,
                        "pluggedIn": True,
                    }
                ],
                "ts": 1757299870275,
            }

        async def summary_v2(self):
            return []

        async def charge_mode(self, sn):
            return None

        async def session_history(self, *args, **kwargs):
            return {
                "data": {
                    "result": [
                        {
                            "sessionId": 99,
                            "startTime": "2025-10-16T09:30:00Z[UTC]",
                            "endTime": None,
                            "aggEnergyValue": 4.0,
                            "activeChargeTime": 7200,
                        }
                    ],
                    "hasMore": False,
                }
            }

    coord.client = StubClient()

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    assert not st["energy_today_sessions"]
    assert st["energy_today_sessions_kwh"] == 0.0

    data = await coord._async_update_data()
    coord.async_set_updated_data(data)
    st = data[RANDOM_SERIAL]
    sessions = st["energy_today_sessions"]
    assert sessions and len(sessions) == 1
    inflight = sessions[0]
    assert inflight["energy_kwh_total"] == pytest.approx(4.0)
    assert inflight["energy_kwh"] == pytest.approx(4.0)
    assert inflight["active_charge_time_overlap_s"] > 0
    assert st["energy_today_sessions_kwh"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_timeout_backoff_issue_recovery(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_SCAN_INTERVAL,
        ISSUE_NETWORK_UNREACHABLE,
    )
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }

    class DummyEntry:
        def __init__(self):
            self.options = {}

        def async_on_unload(self, cb):
            return None

    entry = DummyEntry()
    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        coord_mod,
        "async_call_later",
        lambda *_args, **_kwargs: (lambda: None),
    )

    create_calls: list[tuple[str, str, dict]] = []
    delete_calls: list[tuple[str, str]] = []

    def stub_create_issue(hass_arg, domain, issue_id, **kwargs):
        create_calls.append((domain, issue_id, kwargs))

    def stub_delete_issue(hass_arg, domain, issue_id):
        delete_calls.append((domain, issue_id))

    monkeypatch.setattr(coord_mod.ir, "async_create_issue", stub_create_issue)
    monkeypatch.setattr(coord_mod.ir, "async_delete_issue", stub_delete_issue)

    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class TimeoutClient:
        async def status(self):
            await asyncio.sleep(0)
            raise asyncio.TimeoutError()

    coord.client = TimeoutClient()

    for idx in range(2):
        with pytest.raises(UpdateFailed):
            await coord._async_update_data()
        assert coord._network_errors == idx + 1
        assert coord._backoff_until is not None
        assert not create_calls
        coord._backoff_until = 0

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord._network_errors == 3
    assert create_calls
    assert create_calls[0][0] == "enphase_ev"
    assert create_calls[0][1] == ISSUE_NETWORK_UNREACHABLE
    assert len(create_calls) == 1
    coord._backoff_until = 0

    class SuccessClient:
        async def status(self):
            return {"evChargerData": []}

    coord.client = SuccessClient()
    await coord._async_update_data()
    assert coord._network_errors == 0
    assert coord._last_error is None
    assert delete_calls
    assert delete_calls[-1][1] == ISSUE_NETWORK_UNREACHABLE
    assert len(delete_calls) == 2
    assert coord._backoff_until is None


@pytest.mark.asyncio
async def test_parse_battery_status_payload_aggregates_and_skips_excluded(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord._parse_battery_status_payload(  # noqa: SLF001
        {
            "current_charge": "20%",
            "available_energy": 3,
            "max_capacity": 10,
            "available_power": 7.68,
            "max_power": 7.68,
            "included_count": 2,
            "excluded_count": 1,
            "storages": [
                {
                    "id": 1,
                    "serial_number": "BAT-1",
                    "current_charge": "40%",
                    "available_energy": 2,
                    "max_capacity": 5,
                    "status": "normal",
                    "statusText": "Normal",
                    "excluded": False,
                },
                {
                    "id": 2,
                    "serial_number": "BAT-2",
                    "current_charge": "20%",
                    "available_energy": 1,
                    "max_capacity": 5,
                    "status": "warning",
                    "statusText": "Warning",
                    "excluded": False,
                },
                {
                    "id": 3,
                    "serial_number": "BAT-3",
                    "current_charge": "99%",
                    "available_energy": 9,
                    "max_capacity": 10,
                    "status": "error",
                    "statusText": "Error",
                    "excluded": True,
                },
            ],
        }
    )

    assert coord.iter_battery_serials() == ["BAT-1", "BAT-2"]
    assert coord.battery_storage("BAT-1")["current_charge_pct"] == 40
    assert coord.battery_storage("BAT-1")["id"] == "1"
    assert coord.battery_storage("BAT-1")["battery_id"] == "1"
    assert coord.battery_storage("BAT-3") is None
    assert coord.battery_aggregate_charge_pct == 30.0
    assert coord.battery_aggregate_status == "warning"
    details = coord.battery_aggregate_status_details
    assert details["aggregate_charge_source"] == "computed"
    assert details["included_count"] == 2
    assert details["contributing_count"] == 2
    assert details["missing_energy_capacity_keys"] == []
    assert details["excluded_count"] == 1
    assert details["per_battery_status"]["BAT-1"] == "normal"
    assert details["per_battery_status"]["BAT-2"] == "warning"
    assert details["worst_storage_key"] == "BAT-2"


@pytest.mark.asyncio
async def test_parse_battery_status_payload_rounds_kwh_fields(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord._parse_battery_status_payload(  # noqa: SLF001
        {
            "available_energy": "1.239",
            "max_capacity": "2.996",
            "storages": [
                {
                    "serial_number": "BAT-1",
                    "available_energy": "1.239",
                    "max_capacity": "2.996",
                    "status": "normal",
                    "excluded": False,
                }
            ],
        }
    )

    snapshot = coord.battery_storage("BAT-1")
    assert snapshot is not None
    assert snapshot["available_energy_kwh"] == 1.24
    assert snapshot["max_capacity_kwh"] == 3.0
    details = coord.battery_aggregate_status_details
    assert details["available_energy_kwh"] == 1.24
    assert details["max_capacity_kwh"] == 3.0
    assert details["site_available_energy_kwh"] == 1.24
    assert details["site_max_capacity_kwh"] == 3.0


@pytest.mark.asyncio
async def test_parse_battery_status_payload_falls_back_to_site_current_charge(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord._parse_battery_status_payload(  # noqa: SLF001
        {
            "current_charge": "48%",
            "storages": [
                {
                    "id": 1,
                    "serial_number": "BAT-1",
                    "current_charge": "48%",
                    "available_energy": None,
                    "max_capacity": None,
                    "status": "normal",
                    "excluded": False,
                }
            ],
        }
    )

    assert coord.battery_aggregate_charge_pct == 48.0
    assert coord.battery_aggregate_status == "normal"
    details = coord.battery_aggregate_status_details
    assert details["aggregate_charge_source"] == "site_current_charge"
    assert details["included_count"] == 1
    assert details["contributing_count"] == 0
    assert details["missing_energy_capacity_keys"] == ["BAT-1"]


@pytest.mark.asyncio
async def test_parse_battery_status_payload_partial_batteries_use_site_soc(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord._parse_battery_status_payload(  # noqa: SLF001
        {
            "current_charge": "55%",
            "storages": [
                {
                    "id": 1,
                    "serial_number": "BAT-1",
                    "current_charge": "40%",
                    "available_energy": 2.0,
                    "max_capacity": 5.0,
                    "status": "normal",
                    "excluded": False,
                },
                {
                    "id": 2,
                    "serial_number": "BAT-2",
                    "current_charge": "70%",
                    "available_energy": None,
                    "max_capacity": 5.0,
                    "status": "normal",
                    "excluded": False,
                },
            ],
        }
    )

    assert coord.battery_aggregate_charge_pct == 55.0
    details = coord.battery_aggregate_status_details
    assert details["aggregate_charge_source"] == "site_current_charge"
    assert details["included_count"] == 2
    assert details["contributing_count"] == 1
    assert details["missing_energy_capacity_keys"] == ["BAT-2"]


@pytest.mark.asyncio
async def test_parse_battery_status_payload_partial_batteries_without_site_soc_unknown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord._parse_battery_status_payload(  # noqa: SLF001
        {
            "storages": [
                {
                    "id": 1,
                    "serial_number": "BAT-1",
                    "current_charge": "40%",
                    "available_energy": 2.0,
                    "max_capacity": 5.0,
                    "status": "normal",
                    "excluded": False,
                },
                {
                    "id": 2,
                    "serial_number": "BAT-2",
                    "current_charge": "70%",
                    "available_energy": None,
                    "max_capacity": 5.0,
                    "status": "normal",
                    "excluded": False,
                },
            ],
        }
    )

    assert coord.battery_aggregate_charge_pct is None
    details = coord.battery_aggregate_status_details
    assert details["aggregate_charge_source"] == "unknown"
    assert details["included_count"] == 2
    assert details["contributing_count"] == 1
    assert details["missing_energy_capacity_keys"] == ["BAT-2"]


@pytest.mark.asyncio
async def test_refresh_battery_status_stores_redacted_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_status = AsyncMock(
        return_value={
            "current_charge": "48%",
            "token": "secret",
            "storages": [
                {"serial_number": "BAT-1", "current_charge": "48%", "excluded": False}
            ],
        }
    )

    await coord._async_refresh_battery_status()  # noqa: SLF001

    assert coord.battery_status_payload is not None
    assert coord.battery_status_payload["token"] == "[redacted]"
    assert coord.iter_battery_serials() == ["BAT-1"]


@pytest.mark.asyncio
async def test_refresh_battery_backup_history_parses_and_caches(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    payload = {
        "total_records": 4,
        "total_backup": 307,
        "histories": [
            {"start_time": "2025-10-17T14:38:30+11:00", "duration": 121},
            {"start_time": "bad", "duration": 60},
            {"start_time": "2025-10-16T18:30:09+11:00", "duration": 0},
            {"start_time": None, "duration": 74},
        ],
    }
    monkeypatch.setattr(coord, "_redact_battery_payload", lambda value: "raw")
    coord.client.battery_backup_history = AsyncMock(return_value=payload)

    await coord._async_refresh_battery_backup_history(force=True)  # noqa: SLF001

    assert coord._battery_backup_history_payload == {"value": "raw"}  # noqa: SLF001
    events = coord.battery_backup_history_events
    assert len(events) == 1
    assert events[0]["duration_seconds"] == 121
    assert isinstance(events[0]["start"], datetime)
    assert isinstance(events[0]["end"], datetime)
    first_event_end = events[0]["end"]
    assert first_event_end - events[0]["start"] == timedelta(seconds=121)
    assert coord._battery_backup_history_cache_until is not None  # noqa: SLF001

    coord._battery_backup_history_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.battery_backup_history = AsyncMock(
        side_effect=AssertionError("no fetch")
    )
    await coord._async_refresh_battery_backup_history()  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_battery_backup_history_stores_redacted_dict_payload(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    payload = {
        "histories": [{"start_time": "2025-10-17T14:38:30+11:00", "duration": 120}]
    }
    monkeypatch.setattr(coord, "_redact_battery_payload", lambda value: {"safe": True})
    coord.client.battery_backup_history = AsyncMock(return_value=payload)

    await coord._async_refresh_battery_backup_history(force=True)  # noqa: SLF001

    assert coord._battery_backup_history_payload == {"safe": True}  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_battery_backup_history_keeps_last_good_on_invalid_or_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_backup_history_events = [  # noqa: SLF001
        {
            "start": datetime(2025, 10, 17, 3, 38, tzinfo=timezone.utc),
            "end": datetime(2025, 10, 17, 3, 40, tzinfo=timezone.utc),
            "duration_seconds": 120,
        }
    ]
    expected = coord.battery_backup_history_events

    coord.client.battery_backup_history = AsyncMock(return_value={"histories": "bad"})
    await coord._async_refresh_battery_backup_history(force=True)  # noqa: SLF001
    assert coord.battery_backup_history_events == expected
    assert coord._battery_backup_history_cache_until is not None  # noqa: SLF001
    invalid_cache_until = coord._battery_backup_history_cache_until  # noqa: SLF001
    assert invalid_cache_until >= time.monotonic() + (
        BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL - 1
    )

    coord.client.battery_backup_history = AsyncMock(
        side_effect=AssertionError("no fetch")
    )
    await coord._async_refresh_battery_backup_history()  # noqa: SLF001

    coord._battery_backup_history_cache_until = None  # noqa: SLF001
    coord.client.battery_backup_history = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._async_refresh_battery_backup_history(force=True)  # noqa: SLF001
    assert coord.battery_backup_history_events == expected
    assert coord._battery_backup_history_cache_until is not None  # noqa: SLF001


def test_parse_battery_backup_history_uses_site_timezone_for_naive_start_time(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_timezone = "Australia/Sydney"  # noqa: SLF001

    parsed = coord._parse_battery_backup_history_payload(  # noqa: SLF001
        {
            "total_records": 1,
            "total_backup": 120,
            "histories": [{"start_time": "2025-10-17T14:38:30", "duration": 120}],
        }
    )

    assert parsed is not None
    assert len(parsed) == 1
    assert parsed[0]["start"].tzinfo == ZoneInfo("Australia/Sydney")
    assert parsed[0]["end"] - parsed[0]["start"] == timedelta(seconds=120)


def test_battery_backup_history_events_property_filters_non_dict(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_backup_history_events = [  # noqa: SLF001
        {"start": datetime(2025, 10, 17, 3, 38, tzinfo=timezone.utc)},
        "bad",
    ]
    events = coord.battery_backup_history_events

    assert len(events) == 1
    assert isinstance(events[0], dict)

    coord._battery_backup_history_events = None  # type: ignore[assignment]  # noqa: SLF001
    assert coord.battery_backup_history_events == []


def test_backup_history_tzinfo_fallback_to_default_timezone(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_timezone = "Invalid/Timezone"  # noqa: SLF001

    assert coord._backup_history_tzinfo() == dt_util.DEFAULT_TIME_ZONE  # noqa: SLF001


def test_backup_history_tzinfo_fallback_to_utc_when_default_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_timezone = "Invalid/Timezone"  # noqa: SLF001
    dt_util.DEFAULT_TIME_ZONE = None
    try:
        assert coord._backup_history_tzinfo() == timezone.utc  # noqa: SLF001
    finally:
        dt_util.DEFAULT_TIME_ZONE = timezone.utc


def test_parse_battery_backup_history_payload_rejects_non_dict(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    assert coord._parse_battery_backup_history_payload(["bad"]) is None  # noqa: SLF001


def test_parse_battery_backup_history_payload_skips_invalid_rows(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    parsed = coord._parse_battery_backup_history_payload(  # noqa: SLF001
        {
            "total_records": 5,
            "total_backup": 120,
            "histories": [
                "bad",
                {"start_time": "2025-10-17T14:38:30+11:00", "duration": "oops"},
                {"start_time": BadStr(), "duration": 60},
                {"start_time": "   ", "duration": 60},
                {"start_time": "2025-10-17T14:38:30+11:00", "duration": 120},
            ],
        }
    )

    assert parsed is not None
    assert len(parsed) == 1
    assert parsed[0]["duration_seconds"] == 120


@pytest.mark.asyncio
async def test_update_data_site_only_refreshes_battery_status(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord.serials = set()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock()  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001

    await coord._async_update_data()  # noqa: SLF001

    coord._async_refresh_battery_status.assert_awaited_once()
    coord._async_refresh_battery_backup_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_data_site_only_continues_when_backup_history_refresh_fails(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord.serials = set()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("boom")
    )
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock()  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001

    await coord._async_update_data()  # noqa: SLF001

    coord._async_refresh_battery_backup_history.assert_awaited_once()
    coord._async_refresh_battery_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_data_site_only_ignores_optional_refresh_failures(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord.serials = set()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("site settings")
    )
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("schedules")
    )
    coord._async_refresh_storm_guard_profile = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("storm guard")
    )
    coord._async_refresh_storm_alert = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("storm alert")
    )
    coord._async_refresh_grid_control_check = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("grid")
    )
    coord._async_refresh_devices_inventory = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("inventory")
    )
    coord._async_refresh_dry_contact_settings = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("dry contact")
    )
    coord._async_refresh_hems_devices = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("hems")
    )
    coord._async_refresh_inverters = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("inverters")
    )
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("heatpump")
    )

    await coord._async_update_data()  # noqa: SLF001

    assert "site_energy_s" in coord.phase_timings
    assert "total_s" in coord.phase_timings
    coord._async_refresh_heatpump_power.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_data_normal_refreshes_battery_status(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.client.status = AsyncMock(return_value={"evChargerData": []})
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock()  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    await coord._async_update_data()  # noqa: SLF001

    coord._async_refresh_battery_status.assert_awaited_once()
    coord._async_refresh_battery_backup_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_data_normal_continues_when_backup_history_refresh_fails(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.client.status = AsyncMock(return_value={"evChargerData": []})
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("boom")
    )
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock()  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    await coord._async_update_data()  # noqa: SLF001

    coord._async_refresh_battery_backup_history.assert_awaited_once()
    coord._async_refresh_battery_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_data_normal_ignores_optional_refresh_failures(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = False  # noqa: SLF001
    coord._scheduler_backoff_active = lambda: False  # type: ignore[assignment]  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "chargeMode": "IMMEDIATE",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": False,
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord.evse_timeseries.async_refresh = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("timeseries")
    )
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("schedules")
    )
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("grid")
    )
    coord._async_refresh_devices_inventory = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("inventory")
    )
    coord._async_refresh_dry_contact_settings = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("dry contact")
    )
    coord._async_refresh_hems_devices = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("hems")
    )
    coord._async_refresh_inverters = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("inverters")
    )
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("heatpump")
    )
    coord._get_charge_mode = AsyncMock(return_value="IMMEDIATE")  # type: ignore[assignment]  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    await coord._async_update_data()  # noqa: SLF001

    assert "battery_schedules_s" in coord.phase_timings
    assert "grid_control_check_s" in coord.phase_timings
    assert "devices_inventory_s" in coord.phase_timings
    assert "dry_contact_settings_s" in coord.phase_timings
    assert "hems_devices_s" in coord.phase_timings
    assert "evse_timeseries_s" in coord.phase_timings
    assert "site_energy_s" in coord.phase_timings
    assert "inverters_s" in coord.phase_timings
    assert "heatpump_power_s" in coord.phase_timings
    coord._get_charge_mode.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_update_data_normal_ignores_merge_charger_payload_failures(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = False  # noqa: SLF001
    coord._scheduler_backoff_active = lambda: False  # type: ignore[assignment]  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "Garage EV",
                    "chargeMode": "IMMEDIATE",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": False,
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord.evse_timeseries.async_refresh = AsyncMock(return_value=None)  # noqa: SLF001
    coord.evse_timeseries.merge_charger_payloads = Mock(
        side_effect=RuntimeError("boom")
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock()  # noqa: SLF001
    coord._async_refresh_dry_contact_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock()  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock()  # noqa: SLF001
    coord._get_charge_mode = AsyncMock(return_value="IMMEDIATE")  # type: ignore[assignment]  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert RANDOM_SERIAL in result


@pytest.mark.asyncio
async def test_update_data_site_only_orders_topology_mutations_deterministically(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord.serials = set()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001

    order: list[str] = []

    async def _record(name: str) -> None:
        order.append(name)

    async def _record_battery_status() -> None:
        await _record("battery_status")

    async def _record_devices_inventory() -> None:
        await _record("devices_inventory")

    async def _record_hems_devices() -> None:
        await _record("hems_devices")

    async def _record_inverters() -> None:
        await _record("inverters")

    async def _record_heatpump_power() -> None:
        await _record("heatpump_power")

    coord._async_refresh_battery_site_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_dry_contact_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_current_power_consumption = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock(
        side_effect=_record_battery_status
    )  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock(  # noqa: SLF001
        side_effect=_record_devices_inventory
    )
    coord._async_refresh_hems_devices = AsyncMock(
        side_effect=_record_hems_devices
    )  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(
        side_effect=_record_inverters
    )  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(
        side_effect=_record_heatpump_power
    )  # noqa: SLF001

    await coord._async_update_data()  # noqa: SLF001

    assert order == [
        "battery_status",
        "devices_inventory",
        "hems_devices",
        "inverters",
        "heatpump_power",
    ]


def test_inventory_summary_helpers_reuse_stable_cache_markers(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
            },
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 1,
                "devices": [{"serial_number": "INV-1", "name": "Inverter"}],
            },
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"serial_number": "HP-1", "name": "Heat Pump"}],
            },
        },
        ["envoy", "microinverter", "heatpump"],
    )
    coord._system_dashboard_devices_details_raw = {  # noqa: SLF001
        "envoy": {"envoy": {"status": "normal"}}
    }
    coord._hems_devices_payload = {"result": {"devices": []}}  # noqa: SLF001

    gateway_builder = Mock(return_value={"gateway": 1})
    micro_builder = Mock(return_value={"micro": 1})
    heatpump_builder = Mock(return_value={"heatpump": 1})
    heatpump_type_builder = Mock(return_value={"HEAT_PUMP": {"count": 1}})

    monkeypatch.setattr(coord, "_build_gateway_inventory_summary", gateway_builder)
    monkeypatch.setattr(coord, "_build_microinverter_inventory_summary", micro_builder)
    monkeypatch.setattr(coord, "_build_heatpump_inventory_summary", heatpump_builder)
    monkeypatch.setattr(coord, "_build_heatpump_type_summaries", heatpump_type_builder)

    assert coord.gateway_inventory_summary() == {"gateway": 1}
    assert coord.gateway_inventory_summary() == {"gateway": 1}
    assert coord.microinverter_inventory_summary() == {"micro": 1}
    assert coord.microinverter_inventory_summary() == {"micro": 1}
    assert coord.heatpump_inventory_summary() == {"heatpump": 1}
    assert coord.heatpump_inventory_summary() == {"heatpump": 1}
    assert coord.heatpump_type_summary("heat_pump") == {"count": 1}
    assert coord.heatpump_type_summary("heat_pump") == {"count": 1}

    assert gateway_builder.call_count == 1
    assert micro_builder.call_count == 1
    assert heatpump_builder.call_count == 1
    assert heatpump_type_builder.call_count == 1


def test_battery_status_helper_edge_cases(coordinator_factory) -> None:
    coord = coordinator_factory()

    class ExplodingFloat(float):
        def __float__(self):  # type: ignore[override]
            raise ValueError("boom")

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    class BadStrip(str):
        def strip(self, chars=None):  # type: ignore[override]
            raise ValueError("boom")

    assert coord._coerce_optional_float(True) == 1.0  # noqa: SLF001
    assert coord._coerce_optional_float(ExplodingFloat(1.0)) is None  # noqa: SLF001
    assert coord._coerce_optional_float(BadStrip("1")) is None  # noqa: SLF001
    assert coord._coerce_optional_float("   ") is None  # noqa: SLF001
    assert coord._coerce_optional_float("1,234.5") == 1234.5  # noqa: SLF001
    assert coord._coerce_optional_float("bad") is None  # noqa: SLF001
    assert coord._coerce_optional_float(object()) is None  # noqa: SLF001
    assert coord._coerce_optional_text(BadStr()) is None  # noqa: SLF001

    assert coord._parse_percent_value(None) is None  # noqa: SLF001
    assert coord._parse_percent_value(True) == 1.0  # noqa: SLF001
    assert coord._parse_percent_value(ExplodingFloat(2.0)) is None  # noqa: SLF001
    assert coord._parse_percent_value(object()) is None  # noqa: SLF001
    assert coord._parse_percent_value(BadStrip("10%")) is None  # noqa: SLF001
    assert coord._parse_percent_value(" ") is None  # noqa: SLF001
    assert coord._parse_percent_value("not-a-number") is None  # noqa: SLF001
    assert coord._parse_percent_value("48%") == 48.0  # noqa: SLF001

    assert coord._normalize_battery_status_text(None) is None  # noqa: SLF001
    assert coord._normalize_battery_status_text(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_battery_status_text("   ") is None  # noqa: SLF001
    assert coord._normalize_battery_status_text("---___") is None  # noqa: SLF001
    assert coord._normalize_battery_status_text("critical") == "error"  # noqa: SLF001
    assert coord._normalize_battery_status_text("warning") == "warning"  # noqa: SLF001
    assert coord._normalize_battery_status_text("abnormal") == "warning"  # noqa: SLF001
    assert (
        coord._normalize_battery_status_text("not reporting") == "warning"
    )  # noqa: SLF001
    assert (
        coord._normalize_battery_status_text("not normal") == "warning"
    )  # noqa: SLF001
    assert coord._normalize_battery_status_text("mystery") == "unknown"  # noqa: SLF001

    assert coord._battery_status_severity_value(None) >= 0  # noqa: SLF001
    assert coord._battery_storage_key({"id": "7"}) == "id_7"  # noqa: SLF001
    assert coord._battery_storage_key({}) is None  # noqa: SLF001
    assert coord._normalize_battery_id(107247437) == "107247437"  # noqa: SLF001
    assert coord._normalize_battery_id(42.0) == "42"  # noqa: SLF001
    assert coord._normalize_battery_id("107,247,437") == "107247437"  # noqa: SLF001
    assert coord._normalize_battery_id(" +42 ") == "+42"  # noqa: SLF001
    assert coord._normalize_battery_id(1.5) is None  # noqa: SLF001
    assert coord._normalize_battery_id(object()) is None  # noqa: SLF001
    assert coord._normalize_battery_id(BadStrip("7")) is None  # noqa: SLF001
    assert coord._normalize_battery_id("   ") is None  # noqa: SLF001
    assert coord._normalize_battery_id(True) is None  # noqa: SLF001
    assert coord._normalize_battery_id("bad-id") is None  # noqa: SLF001


def test_battery_status_property_edge_cases(coordinator_factory) -> None:
    coord = coordinator_factory()

    class BadFloat:
        def __float__(self):
            raise ValueError("boom")

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord._battery_aggregate_charge_pct = BadFloat()  # noqa: SLF001
    assert coord.battery_aggregate_charge_pct is None
    coord._battery_aggregate_status = BadStr()  # noqa: SLF001
    assert coord.battery_aggregate_status is None
    coord._battery_aggregate_status_details = "bad"  # noqa: SLF001
    assert coord.battery_aggregate_status_details == {}
    coord._battery_status_payload = "bad"  # noqa: SLF001
    assert coord.battery_status_payload is None

    coord._battery_aggregate_status_details = {"included_count": 1}  # noqa: SLF001
    coord._battery_aggregate_charge_pct = 10  # noqa: SLF001
    coord._battery_aggregate_status = "normal"  # noqa: SLF001
    summary = coord.battery_status_summary
    assert summary["aggregate_charge_pct"] == 10.0
    assert summary["aggregate_status"] == "normal"
    assert summary["battery_order"] == []


def test_battery_serial_and_storage_edge_cases(coordinator_factory) -> None:
    coord = coordinator_factory()

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord._battery_storage_order = ("bad",)  # type: ignore[assignment]  # noqa: SLF001
    coord._battery_storage_data = {}  # noqa: SLF001
    assert coord.iter_battery_serials() == []

    coord._battery_storage_order = [BadStr(), "MISSING", "BAT-1"]  # noqa: SLF001
    coord._battery_storage_data = {"BAT-1": {"identity": "BAT-1"}}  # noqa: SLF001
    assert coord.iter_battery_serials() == ["BAT-1"]

    coord._battery_storage_data = None  # type: ignore[assignment]  # noqa: SLF001
    assert coord.battery_storage("BAT-1") is None
    coord._battery_storage_data = {"BAT-1": {"identity": "BAT-1"}}  # noqa: SLF001
    assert coord.battery_storage(BadStr()) is None
    assert coord.battery_storage("   ") is None


def test_parse_battery_status_payload_edge_shapes(coordinator_factory) -> None:
    coord = coordinator_factory()

    coord._parse_battery_status_payload("bad")  # noqa: SLF001
    assert coord.iter_battery_serials() == []
    assert coord.battery_aggregate_status is None

    coord._parse_battery_status_payload(  # noqa: SLF001
        {
            "current_charge": "12%",
            "storages": [
                "bad",
                {"excluded": False},
                {"id": 9, "excluded": False, "statusText": "Unknown"},
                {
                    "id": "10",
                    "serial_number": "BAT-10",
                    "current_charge": "15%",
                    "available_energy": 0.5,
                    "max_capacity": 1.0,
                    "status": None,
                    "statusText": None,
                    "excluded": False,
                },
            ],
        }
    )
    assert "id_9" in coord.iter_battery_serials()
    assert coord.battery_storage("id_9")["status_normalized"] == "unknown"
    assert coord.battery_storage("id_9")["id"] == "9"
    assert coord.battery_aggregate_charge_pct == 12.0
    details = coord.battery_aggregate_status_details
    assert details["aggregate_charge_source"] == "site_current_charge"
    assert details["contributing_count"] == 1
    assert details["missing_energy_capacity_keys"] == ["id_9"]
    assert coord.battery_aggregate_status == "unknown"


def test_parse_battery_status_payload_prefers_status_text_when_raw_unknown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord._parse_battery_status_payload(  # noqa: SLF001
        {
            "storages": [
                {
                    "serial_number": "BAT-1",
                    "current_charge": "50%",
                    "available_energy": 2.5,
                    "max_capacity": 5.0,
                    "status": "mystery_code",
                    "statusText": "Normal",
                    "excluded": False,
                }
            ]
        }
    )

    snapshot = coord.battery_storage("BAT-1")
    assert snapshot is not None
    assert snapshot["status_normalized"] == "normal"
    assert coord.battery_aggregate_status == "normal"


@pytest.mark.asyncio
async def test_refresh_battery_status_wraps_non_dict_redacted_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_status = AsyncMock(return_value=["unexpected"])  # type: ignore[list-item]

    await coord._async_refresh_battery_status()  # noqa: SLF001

    assert coord.battery_status_payload == {"value": ["unexpected"]}
