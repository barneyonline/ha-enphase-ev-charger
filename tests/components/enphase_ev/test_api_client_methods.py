"""Comprehensive tests for EnphaseEVClient behavior."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.enphase_ev import api
from custom_components.enphase_ev.const import GREEN_BATTERY_SETTING


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


def _make_client(session: _FakeSession | MagicMock | None = None) -> api.EnphaseEVClient:
    session = session or MagicMock()
    return api.EnphaseEVClient(session, "SITE", "EAUTH", "COOKIE")


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
async def test_status_normalizes_charger_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "chargers": [
                    {
                        "sn": "EV123",
                        "name": "Garage",
                        "connected": True,
                        "pluggedIn": False,
                        "charging": True,
                        "faulted": False,
                        "connectors": [
                            {
                                "pluggedIn": True,
                                "connectorStatusType": "READY",
                                "dlbActive": False,
                            }
                        ],
                        "session_d": {"e_c": 5, "strt_chrg": 1000},
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
    assert data["evChargerData"][1]["connectors"] == []


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
async def test_status_falls_back_to_alt_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            {"evChargerData": []},
            {"evChargerData": [{"sn": "ALT"}]},
        ]
    )
    data = await client.status()
    assert data["evChargerData"][0]["sn"] == "ALT"


@pytest.mark.asyncio
async def test_status_alt_endpoint_failure_is_ignored() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[{"evChargerData": []}, RuntimeError("boom")])
    data = await client.status()
    assert data == {"evChargerData": []}


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
        cookie="enlighten_manager_token_production=BEAR; other=1"
    )
    client._json = AsyncMock(return_value={"sessions": []})
    await client.session_history("SN", start_date="01-01-2024")
    args, kwargs = client._json.await_args
    assert kwargs["headers"]["Authorization"] == "Bearer BEAR"


@pytest.mark.asyncio
async def test_session_history_falls_back_to_eauth() -> None:
    client = _make_client()
    client.update_credentials(cookie="", eauth="EAUTH")
    client._json = AsyncMock(return_value={"sessions": []})
    await client.session_history("SN", start_date="01-01-2024", end_date="02-01-2024")
    args, kwargs = client._json.await_args
    assert kwargs["headers"]["Authorization"] == "Bearer EAUTH"
    assert kwargs["json"]["endDate"] == "02-01-2024"
