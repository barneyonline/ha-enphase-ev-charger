"""Comprehensive tests for EnphaseEVClient behavior."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.enphase_ev import api
from custom_components.enphase_ev.const import (
    AUTH_APP_SETTING,
    AUTH_RFID_SETTING,
    GREEN_BATTERY_SETTING,
)


def _make_cre(status: int, message: str = "error") -> aiohttp.ClientResponseError:
    req_info = SimpleNamespace(real_url="https://example.test/path")
    return aiohttp.ClientResponseError(
        request_info=req_info, history=(), status=status, message=message
    )


class _FakeResponse:
    def __init__(self, *, status: int, json_body: object, text_body: str = "") -> None:
        self.status = status
        self._json_body = json_body
        self._text_body = text_body
        self.request_info = SimpleNamespace(real_url="https://example.test/path")
        self.history: tuple = ()
        self.reason = "reason"
        self.headers: dict[str, str] = {}

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def json(self):
        return self._json_body

    async def text(self) -> str:
        if isinstance(self._text_body, Exception):
            raise self._text_body
        return self._text_body


class _FakeSession:
    """Session that returns pre-seeded responses."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []
        self.cookie_jar = aiohttp.CookieJar()

    def request(self, method: str, url: str, **kwargs):
        if not self._responses:
            raise AssertionError("No response prepared")
        resp = self._responses.pop(0)
        self.calls.append((method, url, kwargs))
        return resp


class _BadCookie:
    def split(self, *_args, **_kwargs):
        raise RuntimeError("cannot split")


def _make_client(
    session: _FakeSession | MagicMock | None = None,
) -> api.EnphaseEVClient:
    session = session or MagicMock()
    return api.EnphaseEVClient(session, "SITE", "EAUTH", "COOKIE")


def _make_token(payload: dict) -> str:
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    payload_b64 = payload_b64.rstrip("=")
    return f"header.{payload_b64}.sig"


def test_update_credentials_manages_headers() -> None:
    client = _make_client()
    client.update_credentials(
        eauth="TOKEN",
        cookie="a=1; XSRF-TOKEN=xsrf; enlighten_manager_token_production=bearer",
    )
    assert client._h["Cookie"].startswith("a=1")
    assert client._h["e-auth-token"] == "TOKEN"
    assert client._h["X-CSRF-Token"] == "xsrf"

    client.update_credentials(cookie="")
    assert "Cookie" not in client._h
    assert "X-CSRF-Token" not in client._h

    client._cookie = _BadCookie()
    client.update_credentials()
    assert "X-CSRF-Token" not in client._h


def test_bearer_extraction_prefers_cookie() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=jwt-token; other=value"
    )
    assert client._bearer() == "jwt-token"

    client._cookie = _BadCookie()
    assert client._bearer() is None


def test_control_headers_fallbacks() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=jwt-token; other=value",
        eauth="EAUTH",
    )
    assert client._control_headers() == {"Authorization": "Bearer jwt-token"}

    client.update_credentials(cookie="")
    assert client._control_headers() == {"Authorization": "Bearer EAUTH"}

    client.update_credentials(eauth="")
    assert client._control_headers() == {}


def test_redact_headers_masks_sensitive_fields() -> None:
    headers = {
        "Cookie": "secret",
        "Authorization": "Bearer secret",
        "X-Test": "value",
        "e-auth-token": "token",
    }
    redacted = api.EnphaseEVClient._redact_headers(headers)
    assert redacted["Cookie"] == "[redacted]"
    assert redacted["Authorization"] == "[redacted]"
    assert redacted["e-auth-token"] == "[redacted]"
    assert redacted["X-Test"] == "value"


@pytest.mark.asyncio
async def test_json_merges_headers_and_returns_payload() -> None:
    session = _FakeSession(
        [_FakeResponse(status=200, json_body={"ok": True})]
    )
    client = api.EnphaseEVClient(session, "SITE", None, "COOKIE")
    payload = await client._json(
        "GET",
        "https://example.test",
        headers={"Extra": "1"},
        params={"q": "1"},
    )
    assert payload == {"ok": True}
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert kwargs["headers"]["Extra"] == "1"
    assert kwargs["headers"]["Cookie"] == "COOKIE"


@pytest.mark.asyncio
async def test_json_raises_unauthorized() -> None:
    session = _FakeSession([_FakeResponse(status=401, json_body={})])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(api.Unauthorized):
        await client._json("GET", "https://example.test")


@pytest.mark.asyncio
async def test_json_reauth_retry(monkeypatch) -> None:
    session = _FakeSession(
        [
            _FakeResponse(status=401, json_body={}),
            _FakeResponse(status=200, json_body={"ok": True}),
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", None, None)
    attempts: list[bool] = []

    async def _reauth() -> bool:
        attempts.append(True)
        return True

    client.set_reauth_callback(_reauth)
    payload = await client._json("GET", "https://example.test")
    assert payload == {"ok": True}
    assert len(attempts) == 1
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_json_reauth_failure_falls_back() -> None:
    session = _FakeSession([_FakeResponse(status=401, json_body={})])
    client = api.EnphaseEVClient(session, "SITE", None, None)

    async def _reauth() -> bool:
        return False

    client.set_reauth_callback(_reauth)
    with pytest.raises(api.Unauthorized):
        await client._json("GET", "https://example.test")


@pytest.mark.asyncio
async def test_json_returns_empty_on_no_content() -> None:
    session = _FakeSession([_FakeResponse(status=204, json_body=None)])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    payload = await client._json("POST", "https://example.test")
    assert payload == {}


@pytest.mark.asyncio
async def test_json_truncates_long_error_messages() -> None:
    long_body = "x" * 600
    session = _FakeSession(
        [_FakeResponse(status=400, json_body={}, text_body=long_body)]
    )
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client._json("GET", "https://example.test")
    assert len(err.value.message) == 513  # 512 chars + ellipsis


@pytest.mark.asyncio
async def test_json_handles_text_failure() -> None:
    session = _FakeSession(
        [_FakeResponse(status=422, json_body={}, text_body=RuntimeError("boom"))]
    )
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client._json("GET", "https://example.test")
    assert err.value.message == "reason"


@pytest.mark.asyncio
async def test_devices_inventory_uses_devices_json_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"result": []})

    result = await client.devices_inventory()

    assert result == {"result": []}
    client._json.assert_awaited_once_with(
        "GET", f"{api.BASE_URL}/app-api/SITE/devices.json"
    )


@pytest.mark.asyncio
async def test_devices_inventory_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["not", "a", "dict"])

    result = await client.devices_inventory()

    assert result == {}


@pytest.mark.asyncio
async def test_battery_backup_history_uses_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"histories": []})

    result = await client.battery_backup_history()

    assert result == {"histories": []}
    client._json.assert_awaited_once_with(
        "GET", f"{api.BASE_URL}/app-api/SITE/battery_backup_history.json"
    )


@pytest.mark.asyncio
async def test_battery_backup_history_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.battery_backup_history()

    assert result == {}


@pytest.mark.asyncio
async def test_battery_status_uses_battery_status_json_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"current_charge": "48%"})

    result = await client.battery_status()

    assert result == {"current_charge": "48%"}
    client._json.assert_awaited_once_with(
        "GET", f"{api.BASE_URL}/pv/settings/SITE/battery_status.json"
    )


@pytest.mark.asyncio
async def test_battery_status_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.battery_status()

    assert result == {}


@pytest.mark.asyncio
async def test_inverters_inventory_uses_inverters_json_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"inverters": []})

    result = await client.inverters_inventory(limit=30, offset=0, search="")

    assert result == {"inverters": []}
    awaited = client._json.await_args
    assert awaited.args[0] == "GET"
    assert "/app-api/SITE/inverters.json" in awaited.args[1]
    assert "limit=30" in awaited.args[1]
    assert "offset=0" in awaited.args[1]


@pytest.mark.asyncio
async def test_inverters_inventory_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["not", "dict"])

    result = await client.inverters_inventory()

    assert result == {}


@pytest.mark.asyncio
async def test_inverter_status_normalizes_keyed_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "1": {"serialNum": "A", "deviceId": 10},
            "2": "invalid",
            "": {"serialNum": "B"},
        }
    )

    result = await client.inverter_status()

    assert result == {"1": {"serialNum": "A", "deviceId": 10}}


@pytest.mark.asyncio
async def test_inverter_status_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.inverter_status()

    assert result == {}


@pytest.mark.asyncio
async def test_inverter_production_normalizes_values() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "production": {"a": 100, "b": "200.5", "c": "bad"},
            "start_date": "2022-01-01",
            "end_date": "2026-01-01",
        }
    )

    result = await client.inverter_production(
        start_date="2022-01-01", end_date="2026-01-01"
    )

    assert result["production"] == {"a": 100.0, "b": 200.5}
    assert result["start_date"] == "2022-01-01"
    assert result["end_date"] == "2026-01-01"


@pytest.mark.asyncio
async def test_inverter_production_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.inverter_production(
        start_date="2022-01-01", end_date="2026-01-01"
    )

    assert result == {}


@pytest.mark.asyncio
async def test_inverter_production_skips_blank_keys() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "production": {"": 100, "good": 50},
            "start_date": "2022-01-01",
            "end_date": "2026-01-01",
        }
    )

    result = await client.inverter_production(
        start_date="2022-01-01", end_date="2026-01-01"
    )

    assert result["production"] == {"good": 50.0}


@pytest.mark.asyncio
async def test_status_normalizes_charger_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "chargers": [
                    {
                        "sn": "EV123",
                        "name": "Garage",
                        "displayName": "Garage EV",
                        "connected": True,
                        "pluggedIn": False,
                        "charging": True,
                        "faulted": False,
                        "commissioned": 1,
                        "offGrid": "ON_GRID",
                        "evManufacturerName": "Example Motors",
                        "smartEV": {"hasToken": True, "hasEVDetails": False},
                        "sch_d": {"status": 0, "info": [{"type": "CUSTOM"}]},
                        "connectors": [
                            {
                                "pluggedIn": True,
                                "connectorStatusType": "READY",
                                "dlbActive": False,
                            }
                        ],
                        "session_d": {"e_c": 5, "strt_chrg": "1000", "auth_type": "APP"},
                    },
                    {
                        "sn": "EV124",
                        "name": "Driveway",
                        "connected": True,
                        "pluggedIn": False,
                        "charging": False,
                        "faulted": False,
                        "connectors": [],
                        "session_d": {"e_c": 1, "strt_chrg": 2000},
                    }
                ]
            },
            "meta": {"serverTimeStamp": 123456},
        }
    )
    data = await client.status()
    assert data["ts"] == 123456
    assert data["evChargerData"][0]["pluggedIn"] is True
    assert data["evChargerData"][0]["connectors"][0]["dlbActive"] is False
    assert data["evChargerData"][0]["session_d"]["start_time"] == 1
    assert data["evChargerData"][0]["session_d"]["e_c"] == 5
    assert data["evChargerData"][0]["session_d"]["auth_type"] == "APP"
    assert data["evChargerData"][0]["displayName"] == "Garage EV"
    assert data["evChargerData"][0]["evManufacturerName"] == "Example Motors"
    assert data["evChargerData"][0]["smartEV"]["hasToken"] is True
    assert data["evChargerData"][0]["sch_d"]["status"] == 0
    assert data["evChargerData"][1]["connectors"] == []


@pytest.mark.asyncio
async def test_status_normalizes_start_time_variants() -> None:
    client = _make_client()
    huge = 1_700_000_000_123
    client._json = AsyncMock(
        return_value={
            "data": {
                "chargers": [
                    {
                        "sn": "EV1",
                        "name": "One",
                        "connectors": [{}],
                        "session_d": {"start_time": huge},
                    },
                    {
                        "sn": "EV2",
                        "name": "Two",
                        "connectors": [{}],
                        "session_d": {"start_time": str(huge)},
                    },
                ]
            }
        }
    )

    data = await client.status()
    assert data["evChargerData"][0]["session_d"]["start_time"] == huge
    assert data["evChargerData"][1]["session_d"]["start_time"] == str(huge)


@pytest.mark.asyncio
async def test_status_handles_bad_start_time_values() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "chargers": [
                    {
                        "sn": "EV1",
                        "name": "One",
                        "connectors": [{}],
                        "session_d": {"start_time": float("nan")},
                    },
                    {
                        "sn": "EV2",
                        "name": "Two",
                        "connectors": [{}],
                        "session_d": {"start_time": "9" * 5000},
                    },
                ]
            }
        }
    )

    data = await client.status()
    assert str(data["evChargerData"][0]["session_d"]["start_time"]) == "nan"
    assert data["evChargerData"][1]["session_d"]["start_time"] == "9" * 5000


@pytest.mark.asyncio
async def test_get_schedules_normalizes_payload() -> None:
    client = _make_client()
    payload = {
        "meta": {"serverTimeStamp": "ts"},
        "data": {"config": {"name": "config"}, "slots": [{"id": "slot-1"}]},
    }
    client._json = AsyncMock(return_value=payload)

    data = await client.get_schedules("SN123")

    assert data["meta"] == payload["meta"]
    assert data["config"] == {"name": "config"}
    assert data["slots"] == [{"id": "slot-1"}]

    method, url = client._json.call_args.args[:2]
    headers = client._json.call_args.kwargs["headers"]
    assert method == "GET"
    assert url.endswith("/charging-mode/SCHEDULED_CHARGING/SITE/SN123/schedules")
    assert "Authorization" in headers


@pytest.mark.asyncio
async def test_get_schedules_handles_bad_payloads() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value="bad")
    data = await client.get_schedules("SN123")
    assert data == {"meta": None, "config": None, "slots": []}

    client._json = AsyncMock(return_value={"meta": {"serverTimeStamp": "ts"}, "data": "bad"})
    data = await client.get_schedules("SN123")
    assert data["meta"] == {"serverTimeStamp": "ts"}
    assert data["config"] is None
    assert data["slots"] == []


@pytest.mark.asyncio
async def test_patch_schedules_builds_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"ok": True})

    data = await client.patch_schedules(
        "SN123",
        server_timestamp="2025-01-01T00:00:00.000+00:00",
        slots=[{"id": "slot-1"}],
    )

    assert data == {"ok": True}

    method, url = client._json.call_args.args[:2]
    payload = client._json.call_args.kwargs["json"]
    assert method == "PATCH"
    assert url.endswith("/charging-mode/SCHEDULED_CHARGING/SITE/SN123/schedules")
    assert payload["meta"]["serverTimeStamp"] == "2025-01-01T00:00:00.000+00:00"
    assert payload["meta"]["rowCount"] == 1
    assert payload["data"] == [{"id": "slot-1"}]


@pytest.mark.asyncio
async def test_patch_schedule_states_builds_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"ok": True})

    data = await client.patch_schedule_states(
        "SN123",
        slot_states={"slot-1": True, "slot-2": False},
    )

    assert data == {"ok": True}

    method, url = client._json.call_args.args[:2]
    payload = client._json.call_args.kwargs["json"]
    assert method == "PATCH"
    assert url.endswith("/charging-mode/SCHEDULED_CHARGING/SITE/SN123/schedules")
    assert payload == {"slot-1": "ENABLED", "slot-2": "DISABLED"}


@pytest.mark.asyncio
async def test_patch_schedule_builds_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"ok": True})
    slot = {"id": "slot-1", "startTime": "11:00"}

    data = await client.patch_schedule("SN123", "slot-1", slot)

    assert data == {"ok": True}

    method, url = client._json.call_args.args[:2]
    payload = client._json.call_args.kwargs["json"]
    assert method == "PATCH"
    assert url.endswith(
        "/charging-mode/SCHEDULED_CHARGING/SITE/SN123/schedule/slot-1"
    )
    assert payload == slot


@pytest.mark.asyncio
async def test_status_does_not_call_legacy_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"evChargerData": []})
    data = await client.status()
    assert data == {"evChargerData": []}
    assert client._json.call_count == 1


@pytest.mark.asyncio
async def test_status_handles_mapping_failure() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": {"chargers": ["bad-entry"]}})
    data = await client.status()
    assert data == {"data": {"chargers": ["bad-entry"]}}


@pytest.mark.asyncio
async def test_start_charging_success_and_cache() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    out = await client.start_charging("SN", 32, connector_id=1)
    assert out == {"status": "ok"}
    assert client._start_variant_idx == 0


@pytest.mark.asyncio
async def test_start_charging_include_level_strict_requires_payload(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_start_charging_candidates",
        lambda *args, **kwargs: [
            ("POST", "https://example/start", {"connectorId": 1}),
            ("POST", "https://example/start_alt", None),
        ],
    )
    with pytest.raises(aiohttp.ClientError):
        await client.start_charging(
            "SN", 32, include_level=True, strict_preference=True
        )


@pytest.mark.asyncio
async def test_start_charging_exclude_level_strict_requires_payload(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_start_charging_candidates",
        lambda sn, level, connector_id: [
            ("POST", "https://example/start", {"chargingLevel": level}),
            ("POST", "https://example/start_alt", {"charging_level": level}),
        ],
    )
    with pytest.raises(aiohttp.ClientError):
        await client.start_charging(
            "SN", 32, include_level=False, strict_preference=True
        )


@pytest.mark.asyncio
async def test_start_charging_uses_cached_variant() -> None:
    client = _make_client()
    client._start_variant_idx = 5
    client._json = AsyncMock(return_value={"status": "ok"})
    await client.start_charging("SN", 32, connector_id=2)
    args, kwargs = client._json.await_args
    assert "ev_chargers" in args[1]
    assert kwargs.get("json") is None


@pytest.mark.asyncio
async def test_start_charging_not_ready_on_409() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(409), {"status": "ok"}])
    out = await client.start_charging("SN", 32, connector_id=1)
    assert out == {"status": "not_ready"}
    assert client._start_variant_idx == 0


@pytest.mark.asyncio
async def test_start_charging_interprets_errors() -> None:
    body = {
        "error": {
            "errorMessageCode": "iqevc_ms-10012",
            "displayMessage": "Charger already in charging state",
        }
    }
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[_make_cre(400, message=json.dumps(body)), {"status": "ok"}]
    )
    out = await client.start_charging("SN", 32, connector_id=1)
    assert out == {"status": "already_charging"}


@pytest.mark.asyncio
async def test_start_charging_error_code_maps_to_already_charging() -> None:
    message = '{"error":{"errorMessageCode":"iqevc_ms-10012"}}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)])
    out = await client.start_charging("SN", 32)
    assert out == {"status": "already_charging"}


@pytest.mark.asyncio
async def test_start_charging_error_code_maps_to_not_ready() -> None:
    message = '{"error":{"errorMessageCode":"iqevc_ms-10008"}}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)])
    out = await client.start_charging("SN", 32)
    assert out == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_start_charging_display_message_fallback() -> None:
    message = '{"error":{"displayMessage":"\\u004eot plugged into vehicle"}}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)])
    out = await client.start_charging("SN", 32)
    assert out == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_start_charging_display_message_already_charging() -> None:
    message = '{"error":{"message":"\\u0041lready in charging state"}}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)])
    out = await client.start_charging("SN", 32)
    assert out == {"status": "already_charging"}


@pytest.mark.asyncio
async def test_start_charging_parses_single_quoted_payload() -> None:
    payload = '{"errorMessageCode":"iqevc_ms-10008"}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message=f"'{payload}'")])
    out = await client.start_charging("SN", 32, connector_id=1)
    assert out == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_start_charging_prefers_cached_level_variant(monkeypatch) -> None:
    client = _make_client()

    def _candidates(sn, level, connector_id):
        return [
            ("POST", "https://example/start0", {"connectorId": connector_id}),
            ("POST", "https://example/start1", {"chargingLevel": level}),
            ("POST", "https://example/start2", {"chargingLevel": level}),
        ]

    monkeypatch.setattr(client, "_start_charging_candidates", _candidates)
    client._start_variant_idx_with_level = 2
    client._json = AsyncMock(return_value={"status": "ok"})

    await client.start_charging("SN", 40, include_level=True)

    args, kwargs = client._json.await_args
    assert args[1].endswith("/start2")
    assert kwargs["json"] == {"chargingLevel": 40}
    assert client._start_variant_idx_with_level == 2


@pytest.mark.asyncio
async def test_start_charging_prefers_cached_no_level_variant(monkeypatch) -> None:
    client = _make_client()

    def _candidates(sn, level, connector_id):
        return [
            ("POST", "https://example/start0", {"chargingLevel": level}),
            ("POST", "https://example/start1", None),
            ("POST", "https://example/start2", {"connectorId": connector_id}),
        ]

    monkeypatch.setattr(client, "_start_charging_candidates", _candidates)
    client._start_variant_idx_no_level = 2
    client._json = AsyncMock(return_value={"status": "ok"})

    await client.start_charging("SN", 24, include_level=False)
    args, kwargs = client._json.await_args
    assert args[1].endswith("/start2")
    assert kwargs["json"] == {"connectorId": 1}
    assert client._start_variant_idx_no_level == 2


@pytest.mark.asyncio
async def test_start_charging_falls_back_to_general_cache(monkeypatch) -> None:
    client = _make_client()

    def _candidates(sn, level, connector_id):
        return [
            ("POST", "https://example/start0", {"connectorId": connector_id}),
        ]

    monkeypatch.setattr(client, "_start_charging_candidates", _candidates)
    client._json = AsyncMock(return_value={"status": "ok"})

    await client.start_charging("SN", 24, include_level=True)

    # Only general cache should update because payload lacked chargingLevel.
    assert client._start_variant_idx == 0
    assert client._start_variant_idx_with_level is None


@pytest.mark.asyncio
async def test_start_charging_includes_fallback_variants(monkeypatch) -> None:
    client = _make_client()

    def _no_level_candidates(sn, level, connector_id):
        return [
            ("POST", "https://example/start0", None),
        ]

    monkeypatch.setattr(client, "_start_charging_candidates", _no_level_candidates)
    client._json = AsyncMock(return_value={"status": "ok"})

    await client.start_charging("SN", 16, include_level=True, strict_preference=False)
    # Order was extended with fallback entry so the call succeeds.
    assert client._start_variant_idx == 0


@pytest.mark.asyncio
async def test_start_charging_excludes_level_variants_when_requested(monkeypatch) -> None:
    client = _make_client()

    def _level_only_candidates(sn, level, connector_id):
        return [
            ("POST", "https://example/start0", {"chargingLevel": level}),
        ]

    monkeypatch.setattr(client, "_start_charging_candidates", _level_only_candidates)
    client._json = AsyncMock(return_value={"status": "ok"})

    await client.start_charging("SN", 30, include_level=False, strict_preference=False)
    assert client._start_variant_idx_no_level is None
    assert client._start_variant_idx == 0


@pytest.mark.asyncio
async def test_start_charging_raises_when_order_empty(monkeypatch) -> None:
    class TruthyEmpty(list):
        def __bool__(self):
            return True

    client = _make_client()

    def _candidates(sn, level, connector_id):
        return TruthyEmpty()

    monkeypatch.setattr(client, "_start_charging_candidates", _candidates)
    client._json = AsyncMock(return_value={"status": "ok"})

    with pytest.raises(aiohttp.ClientError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_falls_through_and_raises_generic(monkeypatch) -> None:
    class FakeList(list):
        def __bool__(self):
            return True

    client = _make_client()
    monkeypatch.setattr(client, "_start_charging_candidates", lambda *args, **kwargs: [])

    orig_list = list

    class PatchedList(FakeList):
        pass

    def _patched_list(*args, **kwargs):
        return PatchedList(orig_list(*args, **kwargs))

    monkeypatch.setattr("builtins.list", _patched_list)

    with pytest.raises(aiohttp.ClientError):
        await client.start_charging("SN", 16)
@pytest.mark.asyncio
async def test_start_charging_whitespace_error_message() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, "  ")] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_none_error_message() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, None)] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_non_dict_error_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, "[1, 2, 3]")] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_retries_all_and_raises() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, "bad")] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_no_candidates_raises_client_error(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client, "_start_charging_candidates", lambda *args, **kwargs: []
    )

    with pytest.raises(aiohttp.ClientError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_unknown_error_returns_none() -> None:
    message = '{"error":{"details":42}}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_error_list_candidate() -> None:
    message = '{"error":["unexpected"]}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_stop_charging_success_and_cache() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    out = await client.stop_charging("SN")
    assert out == {"status": "ok"}
    assert client._stop_variant_idx == 0


@pytest.mark.asyncio
async def test_stop_charging_reorders_cached_variant() -> None:
    client = _make_client()
    client._stop_variant_idx = 2
    client._json = AsyncMock(return_value={"status": "ok"})
    await client.stop_charging("SN")
    args, _kwargs = client._json.await_args
    assert "/ev_charger/" in args[1]
    assert client._stop_variant_idx == 2


@pytest.mark.asyncio
async def test_stop_charging_handles_noop_status() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(404), {"status": "ok"}])
    out = await client.stop_charging("SN")
    assert out == {"status": "not_active"}
    assert client._stop_variant_idx == 0


@pytest.mark.asyncio
async def test_stop_charging_raises_last_exception() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(500)] * 3)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.stop_charging("SN")


@pytest.mark.asyncio
async def test_stop_charging_handles_payload_variant(monkeypatch) -> None:
    client = _make_client()
    payload = {"stop": True}
    monkeypatch.setattr(
        client,
        "_stop_charging_candidates",
        lambda _sn: [("POST", "https://example.test/stop", payload)],
    )
    client._json = AsyncMock(return_value={"status": "ok"})

    out = await client.stop_charging("SN")

    assert out == {"status": "ok"}
    assert client._json.await_args.kwargs["json"] == payload


@pytest.mark.asyncio
async def test_stop_charging_no_candidates_raises_client_error(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(client, "_stop_charging_candidates", lambda _sn: [])

    with pytest.raises(aiohttp.ClientError):
        await client.stop_charging("SN")


@pytest.mark.asyncio
async def test_trigger_and_stream_helpers_delegate_to_json() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    await client.trigger_message("SN", "HELLO")
    await client.start_live_stream()
    await client.stop_live_stream()
    assert client._json.await_count == 3


@pytest.mark.asyncio
async def test_charge_mode_extracts_enabled_mode() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "modes": {
                    "manualCharging": {"enabled": True, "chargingMode": "MANUAL_CHARGING"}
                }
            }
        }
    )
    mode = await client.charge_mode("SN")
    assert mode == "MANUAL_CHARGING"
    args, kwargs = client._json.await_args
    assert "Authorization" in kwargs["headers"]


@pytest.mark.asyncio
async def test_charge_mode_handles_unexpected_shape() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": {"modes": "invalid"}})
    assert await client.charge_mode("SN") is None


@pytest.mark.asyncio
async def test_charge_mode_returns_none_when_no_enabled() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "modes": {
                    "manualCharging": {"enabled": False, "chargingMode": "MANUAL_CHARGING"}
                }
            }
        }
    )
    assert await client.charge_mode("SN") is None


@pytest.mark.asyncio
async def test_set_charge_mode_passes_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    out = await client.set_charge_mode("SN", "GREEN_CHARGING")
    assert out == {"status": "ok"}
    args, kwargs = client._json.await_args
    assert kwargs["json"] == {"mode": "GREEN_CHARGING"}


@pytest.mark.asyncio
async def test_green_charging_settings_filters_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": [
                {"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": True},
                "invalid",
            ]
        }
    )
    settings = await client.green_charging_settings("SN")
    assert settings == [
        {"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": True}
    ]
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "GREEN_CHARGING" in args[1]
    assert "Authorization" in kwargs["headers"]


@pytest.mark.asyncio
async def test_green_charging_settings_handles_non_dict_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])
    assert await client.green_charging_settings("SN") == []


@pytest.mark.asyncio
async def test_green_charging_settings_handles_non_list_data() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": "nope"})
    assert await client.green_charging_settings("SN") == []


@pytest.mark.asyncio
async def test_set_green_battery_setting_passes_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    out = await client.set_green_battery_setting("SN", enabled=True)
    assert out == {"status": "ok"}
    args, kwargs = client._json.await_args
    assert kwargs["json"] == {
        "chargerSettingList": [
            {
                "chargerSettingName": GREEN_BATTERY_SETTING,
                "enabled": True,
                "value": None,
                "loader": False,
            }
        ]
    }


@pytest.mark.asyncio
async def test_storm_guard_alert_passes_headers() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"criticalAlertActive": False})
    out = await client.storm_guard_alert()
    assert out == {"criticalAlertActive": False}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "stormGuard" in args[1]
    assert "Authorization" in kwargs["headers"]


@pytest.mark.asyncio
async def test_storm_guard_profile_passes_params() -> None:
    token = _make_token({"user_id": "55"})
    client = _make_client()
    client.update_credentials(eauth=token)
    client._json = AsyncMock(return_value={"data": {}})
    await client.storm_guard_profile(locale="en-US")
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert kwargs["params"]["source"] == "enho"
    assert kwargs["params"]["userId"] == "55"
    assert kwargs["params"]["locale"] == "en-US"


@pytest.mark.asyncio
async def test_battery_site_settings_passes_params_and_headers() -> None:
    token = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(eauth=token)
    client._json = AsyncMock(return_value={"data": {}})

    out = await client.battery_site_settings()

    assert out == {"data": {}}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "siteSettings" in args[1]
    assert kwargs["params"]["userId"] == "77"
    assert kwargs["headers"]["Username"] == "77"
    assert kwargs["headers"]["Origin"] == "https://battery-profile-ui.enphaseenergy.com"


@pytest.mark.asyncio
async def test_battery_settings_details_passes_params_and_headers() -> None:
    token = _make_token({"user_id": "99"})
    client = _make_client()
    client.update_credentials(eauth=token)
    client._json = AsyncMock(return_value={"data": {"chargeFromGrid": True}})

    out = await client.battery_settings_details()

    assert out == {"data": {"chargeFromGrid": True}}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "batterySettings" in args[1]
    assert kwargs["params"]["source"] == "enho"
    assert kwargs["params"]["userId"] == "99"
    assert kwargs["headers"]["Username"] == "99"


@pytest.mark.asyncio
async def test_set_battery_settings_payload_and_xsrf() -> None:
    token = _make_token({"user_id": "88"})
    client = _make_client()
    client.update_credentials(eauth=token, cookie="XSRF-TOKEN=xsrf-token; other=1")
    client._json = AsyncMock(return_value={"message": "success"})

    out = await client.set_battery_settings({"veryLowSoc": 15})

    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "batterySettings" in args[1]
    assert kwargs["params"]["userId"] == "88"
    assert kwargs["headers"]["X-XSRF-Token"] == "xsrf-token"
    assert kwargs["json"] == {"veryLowSoc": 15}


@pytest.mark.asyncio
async def test_storm_guard_profile_delegates_to_battery_profile_details() -> None:
    client = _make_client()
    client.battery_profile_details = AsyncMock(return_value={"data": {"ok": True}})

    out = await client.storm_guard_profile(locale="en-US")

    assert out == {"data": {"ok": True}}
    client.battery_profile_details.assert_awaited_once_with(locale="en-US")


@pytest.mark.asyncio
async def test_set_battery_profile_payload_variants_and_xsrf() -> None:
    token = _make_token({"user_id": "100"})
    client = _make_client()
    client.update_credentials(
        eauth=token,
        cookie="XSRF-TOKEN=xsrf-token; other=1",
    )
    client._json = AsyncMock(return_value={"message": "success"})

    out = await client.set_battery_profile(
        profile="cost_savings",
        battery_backup_percentage=25,
        operation_mode_sub_type="prioritize-energy",
        devices=[{"uuid": "abc", "deviceType": "iqEvse", "enable": False}],
    )

    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "api/v1/profile" in args[1]
    assert kwargs["params"]["userId"] == "100"
    assert kwargs["headers"]["X-XSRF-Token"] == "xsrf-token"
    assert kwargs["json"] == {
        "profile": "cost_savings",
        "batteryBackupPercentage": 25,
        "operationModeSubType": "prioritize-energy",
        "devices": [{"uuid": "abc", "deviceType": "iqEvse", "enable": False}],
    }


@pytest.mark.asyncio
async def test_cancel_battery_profile_update_uses_empty_body() -> None:
    token = _make_token({"user_id": "44"})
    client = _make_client()
    client.update_credentials(eauth=token, cookie="XSRF-TOKEN=t; other=1")
    client._json = AsyncMock(return_value={"message": "success"})

    out = await client.cancel_battery_profile_update()

    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "cancel/profile" in args[1]
    assert kwargs["json"] == {}
    assert kwargs["params"]["userId"] == "44"
    assert kwargs["headers"]["X-XSRF-Token"] == "t"


@pytest.mark.asyncio
async def test_set_storm_guard_passes_payload_and_xsrf() -> None:
    token = _make_token({"user_id": "99"})
    client = _make_client()
    client.update_credentials(
        eauth=token,
        cookie="XSRF-TOKEN=xsrf-token; other=1",
    )
    client._json = AsyncMock(return_value={"message": "success"})
    out = await client.set_storm_guard(enabled=True, evse_enabled=False)
    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "stormGuard/toggle" in args[1]
    assert kwargs["json"] == {
        "stormGuardState": "enabled",
        "evseStormEnabled": False,
    }
    assert kwargs["params"]["userId"] == "99"
    assert kwargs["headers"]["X-XSRF-Token"] == "xsrf-token"


@pytest.mark.asyncio
async def test_set_storm_guard_handles_missing_xsrf() -> None:
    client = _make_client()
    client.update_credentials(cookie="cookie=1")
    client._json = AsyncMock(return_value={"message": "success"})

    await client.set_storm_guard(enabled=False, evse_enabled=True)

    _args, kwargs = client._json.await_args
    assert "X-XSRF-Token" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_set_storm_guard_handles_bad_cookie() -> None:
    client = _make_client()
    client._cookie = _BadCookie()
    client._json = AsyncMock(return_value={"message": "success"})

    await client.set_storm_guard(enabled=True, evse_enabled=True)

    _args, kwargs = client._json.await_args
    assert "X-XSRF-Token" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_charger_auth_settings_filters_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": [{"key": AUTH_APP_SETTING, "value": "enabled"}, "invalid"]
        }
    )
    settings = await client.charger_auth_settings("SN")
    assert settings == [{"key": AUTH_APP_SETTING, "value": "enabled"}]
    args, kwargs = client._json.await_args
    assert args[0] == "POST"
    assert "ev_charger_config" in args[1]
    assert kwargs["json"] == [
        {"key": AUTH_RFID_SETTING},
        {"key": AUTH_APP_SETTING},
    ]
    assert "Authorization" in kwargs["headers"]


@pytest.mark.asyncio
async def test_charger_auth_settings_handles_non_dict_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])
    assert await client.charger_auth_settings("SN") == []


@pytest.mark.asyncio
async def test_charger_auth_settings_handles_non_list_data() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": "nope"})
    assert await client.charger_auth_settings("SN") == []


@pytest.mark.asyncio
async def test_set_app_authentication_passes_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    out = await client.set_app_authentication("SN", enabled=False)
    assert out == {"status": "ok"}
    args, kwargs = client._json.await_args
    assert kwargs["json"] == [
        {"key": AUTH_APP_SETTING, "value": "disabled"}
    ]


@pytest.mark.asyncio
async def test_lifetime_energy_normalization() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                json_body={
                    "data": {
                        "production": [1000, "2000", None, -5],
                        "import": ["", "30"],
                        "grid_home": [15],
                        "update_pending": False,
                        "start_date": "2024-01-01",
                        "last_report_date": "1700000000",
                        "evse": "skip",
                        "interval_minutes": "15",
                    }
                },
            )
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", None, "COOKIE")
    payload = await client.lifetime_energy()
    assert payload["production"] == [1000.0, 2000.0, None, -5.0]
    assert payload["import"] == [None, 30.0]
    assert payload["grid_home"] == [15.0]
    assert payload["update_pending"] is False
    assert payload["start_date"] == "2024-01-01"
    assert payload["last_report_date"] == "1700000000"
    assert payload["evse"] == []
    assert payload["interval_minutes"] == 15.0


@pytest.mark.asyncio
async def test_lifetime_energy_handles_non_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["not-a-dict"])
    assert await client.lifetime_energy() is None


@pytest.mark.asyncio
async def test_lifetime_energy_coerce_errors() -> None:
    class BadFloat:
        def __float__(self):
            raise ValueError("boom")

    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "production": [BadFloat(), "bad-number"],
        }
    )
    payload = await client.lifetime_energy()
    assert payload["production"] == [None, None]


@pytest.mark.asyncio
async def test_lifetime_energy_coerce_bad_number_subclass() -> None:
    class BadFloat(float):
        def __float__(self):
            raise ValueError("bad")

    client = _make_client()
    client._json = AsyncMock(return_value={"production": [BadFloat(1.0)]})
    payload = await client.lifetime_energy()
    assert payload["production"] == [None]


@pytest.mark.asyncio
async def test_summary_v2_normalizes_list() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": [{"serialNumber": "EV"}]})
    data = await client.summary_v2()
    assert data == [{"serialNumber": "EV"}]


@pytest.mark.asyncio
async def test_summary_v2_handles_exception() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value="not-a-dict")
    assert await client.summary_v2() is None


@pytest.mark.asyncio
async def test_session_history_adds_bearer_from_cookie() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR; other=1", eauth=""
    )
    client._json = AsyncMock(return_value={"sessions": []})
    await client.session_history(
        "SN",
        start_date="01-01-2024",
        timezone="UTC",
        request_id="req-1",
        username="2999",
    )
    args, kwargs = client._json.await_args
    assert kwargs["headers"]["Authorization"] == "Bearer BEAR"
    assert kwargs["headers"]["requestid"] == "req-1"
    assert kwargs["headers"]["username"] == "2999"
    assert kwargs["json"]["source"] == "evse"
    assert kwargs["json"]["params"]["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_session_history_falls_back_to_eauth() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR", eauth="EAUTH"
    )
    client._json = AsyncMock(return_value={"sessions": []})
    await client.session_history("SN", start_date="01-01-2024", end_date="02-01-2024")
    args, kwargs = client._json.await_args
    assert kwargs["headers"]["Authorization"] == "Bearer EAUTH"
    assert kwargs["json"]["params"]["endDate"] == "02-01-2024"


@pytest.mark.asyncio
async def test_session_history_uses_session_id_header() -> None:
    client = _make_client()
    token = _make_token({"data": {"session_id": "SID123"}})
    client.update_credentials(eauth=token)
    client._json = AsyncMock(return_value={"sessions": []})
    await client.session_history("SN", start_date="01-01-2024")
    args, kwargs = client._json.await_args
    assert kwargs["headers"]["e-auth-token"] == "SID123"


@pytest.mark.asyncio
async def test_session_history_filter_criteria_builds_headers() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": []})
    await client.session_history_filter_criteria(
        request_id="req-2", username="2999"
    )
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "filter_criteria" in args[1]
    assert "requestId=req-2" in args[1]
    assert "username=2999" in args[1]
    assert kwargs["headers"]["Authorization"] == "Bearer EAUTH"
    assert kwargs["headers"]["requestid"] == "req-2"
    assert kwargs["headers"]["username"] == "2999"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("charge_mode", ("SN",), {}),
        ("set_charge_mode", ("SN", "MANUAL_CHARGING"), {}),
        ("green_charging_settings", ("SN",), {}),
        ("set_green_battery_setting", ("SN",), {"enabled": True}),
        ("get_schedules", ("SN",), {}),
        ("patch_schedules", ("SN",), {"server_timestamp": "ts", "slots": []}),
        ("patch_schedule_states", ("SN",), {"slot_states": {"1": True}}),
        ("patch_schedule", ("SN", "1", {}), {}),
    ],
)
async def test_scheduler_endpoints_wrap_unavailable(
    monkeypatch, method, args, kwargs
) -> None:
    client = _make_client()
    err = _make_cre(503, "Service Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(api.SchedulerUnavailable):
        await getattr(client, method)(*args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("charge_mode", ("SN",), {}),
        ("set_charge_mode", ("SN", "MANUAL_CHARGING"), {}),
        ("green_charging_settings", ("SN",), {}),
        ("set_green_battery_setting", ("SN",), {"enabled": True}),
        ("get_schedules", ("SN",), {}),
        ("patch_schedules", ("SN",), {"server_timestamp": "ts", "slots": []}),
        ("patch_schedule_states", ("SN",), {"slot_states": {"1": True}}),
        ("patch_schedule", ("SN", "1", {}), {}),
    ],
)
async def test_scheduler_endpoints_reraise_non_scheduler_error(
    monkeypatch, method, args, kwargs
) -> None:
    client = _make_client()
    err = _make_cre(400, "Bad Request")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(aiohttp.ClientResponseError):
        await getattr(client, method)(*args, **kwargs)


@pytest.mark.asyncio
async def test_auth_settings_reraise_non_service_errors(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(400, "Bad Request")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.charger_auth_settings("SN")

    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.set_app_authentication("SN", enabled=False)


@pytest.mark.asyncio
async def test_lifetime_energy_reraises_non_service_error(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(400, "Bad Request")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.lifetime_energy()


@pytest.mark.asyncio
async def test_session_history_reraises_non_service_error(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(400, "Bad Request")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.session_history("SN", start_date="01-01-2024")


@pytest.mark.asyncio
async def test_charger_auth_settings_wraps_unavailable(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(503, "Service Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(api.AuthSettingsUnavailable):
        await client.charger_auth_settings("SN")


@pytest.mark.asyncio
async def test_set_app_authentication_wraps_unavailable(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(503, "Service Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(api.AuthSettingsUnavailable):
        await client.set_app_authentication("SN", enabled=True)


@pytest.mark.asyncio
async def test_lifetime_energy_wraps_unavailable(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(503, "Service Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(api.SiteEnergyUnavailable):
        await client.lifetime_energy()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [503, 550])
async def test_session_history_wraps_unavailable(monkeypatch, status) -> None:
    client = _make_client()
    err = _make_cre(status, "Service Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(api.SessionHistoryUnavailable):
        await client.session_history("SN", start_date="01-01-2024")
