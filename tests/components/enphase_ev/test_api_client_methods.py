"""Comprehensive tests for EnphaseEVClient behavior."""

from __future__ import annotations

import base64
import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from multidict import CIMultiDict

from custom_components.enphase_ev import api
from custom_components.enphase_ev.const import (
    AUTH_APP_SETTING,
    AUTH_RFID_SETTING,
    GREEN_BATTERY_SETTING,
)

TEST_EVSE_SERIAL = "EVSE-SERIAL-0001"


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
        if isinstance(self._json_body, Exception):
            raise self._json_body
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


def test_update_credentials_handles_xsrf_extractor_exception(monkeypatch) -> None:
    client = _make_client()
    client._h["X-CSRF-Token"] = "stale"

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "_xsrf_token", _boom)
    client.update_credentials(cookie="a=1")
    assert "X-CSRF-Token" not in client._h


def test_extract_xsrf_token_branches(monkeypatch) -> None:
    class BadTokenValue:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert (
        api._extract_xsrf_token(
            {"XSRF-TOKEN": BadTokenValue(), "BP-XSRF-Token": "bp%3Dtoken"}
        )
        == "bp=token"
    )
    assert api._extract_xsrf_token({"XSRF-TOKEN": '""', "BP-XSRF-Token": "bp"}) == "bp"

    monkeypatch.setattr(
        api,
        "unquote",
        lambda _value: (_ for _ in ()).throw(ValueError("decode-fail")),
    )
    assert api._extract_xsrf_token({"XSRF-TOKEN": "raw-token"}) == "raw-token"


def test_xsrf_token_handles_empty_and_decode_fallback(monkeypatch) -> None:
    client = _make_client()
    client.update_credentials(cookie='XSRF-TOKEN=""; BP-XSRF-Token=bp%3Dtoken')
    assert client._xsrf_token() == "bp=token"

    monkeypatch.setattr(
        api,
        "unquote",
        lambda _value: (_ for _ in ()).throw(ValueError("decode-fail")),
    )
    client.update_credentials(cookie="XSRF-TOKEN=raw-token")
    assert client._xsrf_token() == "raw-token"

def test_battery_config_auth_helpers_cover_token_and_cookie_fallback() -> None:
    token = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(eauth=token, cookie="")
    client._bp_xsrf_token = "dynamic-token"  # noqa: SLF001

    headers = client._battery_config_headers(include_xsrf=True)  # noqa: SLF001

    assert client._battery_config_auth_token() == token  # noqa: SLF001
    assert client._xsrf_token() == "dynamic-token"  # noqa: SLF001
    assert headers["Cookie"] == "BP-XSRF-Token=dynamic-token"


def test_battery_config_headers_preserve_original_eauth_and_replace_stale_xsrf() -> None:
    bearer = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(
        eauth="session-token",
        cookie=(
            "session=1; BP-XSRF-Token=stale-token; other=1; "
            f"enlighten_manager_token_production={bearer}"
        ),
    )
    client._bp_xsrf_token = "fresh-token"  # noqa: SLF001

    headers = client._battery_config_headers(include_xsrf=True)  # noqa: SLF001

    assert headers["Authorization"] == f"Bearer {bearer}"
    assert headers["e-auth-token"] == "session-token"
    assert headers["Cookie"] == (
        "session=1; other=1; "
        f"enlighten_manager_token_production={bearer}; BP-XSRF-Token=fresh-token"
    )


def test_battery_config_headers_drop_cookie_when_none_available() -> None:
    client = _make_client()
    client.update_credentials(cookie="session=1")
    client._cookie = ""  # noqa: SLF001

    headers = client._battery_config_headers()  # noqa: SLF001

    assert "Cookie" not in headers


def test_system_dashboard_query_type_helper_branches() -> None:
    class _BadText:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert api._system_dashboard_query_type(None) is None
    assert api._system_dashboard_query_type(_BadText()) is None
    assert api._system_dashboard_query_type(" - ") is None
    assert api._system_dashboard_query_type("System Controller") == "system_controller"


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


def test_public_scheduler_and_header_helpers() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=jwt-token; other=value",
        eauth="EAUTH",
    )
    assert client.scheduler_bearer() == "jwt-token"
    assert client.has_scheduler_bearer() is True
    assert client.base_header_names() == sorted(client._h.keys())
    assert client.control_headers() == {"Authorization": "Bearer jwt-token"}


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


def test_evse_timeseries_unavailable_helper_branches() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert (
        api.is_evse_timeseries_unavailable_error(
            "Service unavailable", 503, "https://x/service/timeseries/evse/timeseries/daily_energy"
        )
        is True
    )
    assert api.is_evse_timeseries_unavailable_error(
        "EVSE timeseries unavailable",
        None,
        None,
    )
    assert api.is_evse_timeseries_unavailable_error(
        "daily_energy service unavailable",
        None,
        None,
    )
    assert api.is_evse_timeseries_unavailable_error(
        "lifetime_energy service unavailable",
        None,
        None,
    )
    assert api.is_evse_timeseries_unavailable_error(BadStr(), None, BadStr()) is False


def test_evse_timeseries_normalizer_helpers(monkeypatch) -> None:
    class BadSerial:
        def __str__(self) -> str:
            raise ValueError("bad-serial")

    class BadUnit:
        def __str__(self) -> str:
            raise ValueError("bad-unit")

    class BadFloat(float):
        def __float__(self):
            raise ValueError("bad-float")

    client = _make_client()

    assert client._normalize_evse_timeseries_serial(BadSerial()) is None
    now_dt = datetime.datetime(2026, 3, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)
    assert client._parse_evse_timeseries_date_key(now_dt) == "2026-03-11"
    assert client._parse_evse_timeseries_date_key(now_dt.date()) == "2026-03-11"
    assert client._parse_evse_timeseries_date_key(1_700_000_000_000) == "2023-11-14"
    assert client._parse_evse_timeseries_date_key(BadFloat(1.0)) is None
    assert client._parse_evse_timeseries_date_key([]) is None
    assert client._parse_evse_timeseries_date_key("") is None
    assert client._parse_evse_timeseries_date_key("2026-03-11 not-iso") == "2026-03-11"
    assert client._parse_evse_timeseries_date_key("2026/03/11 broken") is None
    assert client._parse_evse_timeseries_date_key("bad") is None

    assert client._coerce_evse_timeseries_energy("bad") is None
    assert client._coerce_evse_timeseries_energy("1000", unit_hint=BadUnit()) == pytest.approx(1000.0)
    assert client._coerce_evse_timeseries_energy("1000", unit_hint="Wh") == pytest.approx(1.0)
    assert client._normalize_evse_timeseries_metadata([]) == {}


def test_evse_timeseries_payload_normalizer_branches(monkeypatch) -> None:
    client = _make_client()

    mapping_days, current_value = client._daily_values_from_mapping(
        {"skip": "value", "2026-03-11": "bad", "energy_wh": 500}
    )
    assert mapping_days == {}
    assert current_value == pytest.approx(0.5)

    original_parser = client._parse_evse_timeseries_date_key
    monkeypatch.setattr(
        api.EnphaseEVClient,
        "_parse_evse_timeseries_date_key",
        staticmethod(lambda value: "bad-date" if value == "force-bad" else original_parser(value)),
    )
    values, current = client._daily_values_from_sequence(
        [
            {"energy_kwh": "bad"},
            {"energy_kwh": 1.25},
            "bad",
            2.5,
        ],
        start_date_value="force-bad",
    )
    assert values == {}
    assert current == pytest.approx(2.5)

    values, current = client._daily_values_from_sequence(
        [1.0, 2.0],
        start_date_value="2026-03-10",
    )
    assert values["2026-03-10"] == pytest.approx(1.0)
    assert values["2026-03-11"] == pytest.approx(2.0)
    assert current is None

    daily_entry = client._normalize_evse_daily_entry(
        "SERIAL-1",
        {"serial": "SERIAL-2", "data": {"2026-03-11": 3.0}},
    )
    assert daily_entry["serial"] == "SERIAL-2"
    assert daily_entry["energy_kwh"] == pytest.approx(3.0)
    assert client._normalize_evse_daily_entry("SERIAL-1", [4.0])["current_value_kwh"] == pytest.approx(4.0)
    assert client._normalize_evse_daily_entry("SERIAL-1", "bad") is None

    lifetime_entry = client._normalize_evse_lifetime_entry(
        "SERIAL-1",
        {"serial_number": "SERIAL-2", "values": [1.5]},
    )
    assert lifetime_entry["serial"] == "SERIAL-2"
    assert lifetime_entry["energy_kwh"] == pytest.approx(1.5)
    assert client._normalize_evse_lifetime_entry("SERIAL-1", 9.5)["energy_kwh"] == pytest.approx(9.5)
    assert client._normalize_evse_lifetime_entry("SERIAL-1", {"data": {}}) is None

    payload = client._normalize_evse_timeseries_payload(
        {
            "data": {
                "results": [
                    "skip",
                    {"energy_kwh": 1.0},
                    {"serial": "SERIAL-3", "energy_kwh": 5.0},
                ]
            }
        },
        daily=False,
    )
    assert payload["SERIAL-3"]["serial"] == "SERIAL-3"
    assert payload["SERIAL-3"]["energy_kwh"] == pytest.approx(5.0)

    class BadKey:
        def __str__(self) -> str:
            raise ValueError("bad-key")

    payload = client._normalize_evse_timeseries_payload(
        {BadKey(): {"energy_kwh": 1.0}, "SERIAL-4": {}},
        daily=False,
    )
    assert payload == {}
    assert client._normalize_evse_timeseries_payload("bad", daily=False) is None


def test_invalid_payload_error_defaults_summary_when_blank() -> None:
    err = api.InvalidPayloadError("   ")
    assert err.summary == "Invalid JSON response from Enphase endpoint"
    assert str(err) == "Invalid JSON response from Enphase endpoint"


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
async def test_json_reauth_retry_rebuilds_callable_headers() -> None:
    session = _FakeSession(
        [
            _FakeResponse(status=401, json_body={}),
            _FakeResponse(status=200, json_body={"ok": True}),
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", "OLD-EAUTH", "OLD-COOKIE")

    async def _reauth() -> bool:
        client.update_credentials(
            cookie="enlighten_manager_token_production=NEW-BEAR; XSRF-TOKEN=new-xsrf",
            eauth="NEW-EAUTH",
        )
        return True

    client.set_reauth_callback(_reauth)
    payload = await client._json("GET", "https://example.test", headers=client._hems_headers)

    assert payload == {"ok": True}
    first_headers = session.calls[0][2]["headers"]
    second_headers = session.calls[1][2]["headers"]
    assert first_headers["Authorization"] == "Bearer OLD-EAUTH"
    assert first_headers["e-auth-token"] == "OLD-EAUTH"
    assert first_headers["Cookie"] == "OLD-COOKIE"
    assert second_headers["Authorization"] == "Bearer NEW-BEAR"
    assert second_headers["e-auth-token"] == "NEW-EAUTH"
    assert second_headers["Cookie"] == (
        "enlighten_manager_token_production=NEW-BEAR; XSRF-TOKEN=new-xsrf"
    )
    assert second_headers["X-CSRF-Token"] == "new-xsrf"


@pytest.mark.asyncio
async def test_evse_fw_details_returns_list_payload() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                json_body=[
                    {
                        "serialNumber": TEST_EVSE_SERIAL,
                        "currentFwVersion": "25.37.1.13",
                        "targetFwVersion": "25.37.1.14",
                    },
                    "bad",
                ],
            )
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", "EAUTH", "COOKIE")

    payload = await client.evse_fw_details()
    assert payload == [
        {
            "serialNumber": TEST_EVSE_SERIAL,
            "currentFwVersion": "25.37.1.13",
            "targetFwVersion": "25.37.1.14",
        }
    ]
    assert session.calls[0][0] == "GET"
    assert session.calls[0][1].endswith("/service/evse_management/fwDetails/SITE")


@pytest.mark.asyncio
async def test_evse_fw_details_normalizes_null_payload_to_empty_list() -> None:
    session = _FakeSession([_FakeResponse(status=200, json_body=None)])
    client = api.EnphaseEVClient(session, "SITE", "EAUTH", "COOKIE")

    assert await client.evse_fw_details() == []


@pytest.mark.asyncio
async def test_evse_fw_details_rejects_non_list_payload() -> None:
    session = _FakeSession([_FakeResponse(status=200, json_body={"serialNumber": "bad"})])
    client = api.EnphaseEVClient(session, "SITE", "EAUTH", "COOKIE")

    with pytest.raises(api.InvalidPayloadError, match="payload must be a list"):
        await client.evse_fw_details()


@pytest.mark.asyncio
async def test_evse_feature_flags_uses_endpoint_and_optional_country() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": {"evse_charging_mode": True}})

    result = await client.evse_feature_flags(country="DE")

    assert result == {"data": {"evse_charging_mode": True}}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/service/evse_management/api/v1/config/feature-flags?site_id=SITE&country=DE",
    )


@pytest.mark.asyncio
async def test_evse_feature_flags_returns_none_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    assert await client.evse_feature_flags() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [api.Unauthorized(), _make_cre(403), _make_cre(404)])
async def test_evse_feature_flags_optional_errors_return_none(error) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    assert await client.evse_feature_flags() is None


@pytest.mark.asyncio
async def test_evse_feature_flags_reraises_unexpected_http_error() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(500))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.evse_feature_flags()


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
async def test_json_raises_invalid_payload_with_sanitized_summary() -> None:
    response = _FakeResponse(
        status=200,
        json_body=ValueError("decode failed"),
        text_body="<html>gateway failure</html>",
    )
    response.headers = {"Content-Type": "text/html"}
    session = _FakeSession([response])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(api.InvalidPayloadError) as err:
        await client._json("GET", "https://example.test")
    assert err.value.status == 200
    assert err.value.content_type == "text/html"
    assert "Invalid JSON response" in err.value.summary
    assert "content_type=text/html" in err.value.summary
    assert "endpoint=/" in err.value.summary
    assert "decode_error=ValueError" in err.value.summary
    assert "gateway failure" not in err.value.summary


@pytest.mark.asyncio
async def test_json_invalid_payload_uses_default_summary_when_headers_are_unavailable() -> None:
    class _BadHeaders:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("broken headers")

    response = _FakeResponse(
        status=200,
        json_body=ValueError("decode failed"),
        text_body=RuntimeError("text unavailable"),
    )
    response.headers = _BadHeaders()
    session = _FakeSession([response])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(api.InvalidPayloadError) as err:
        await client._json("GET", "https://example.test")
    assert err.value.status == 200
    assert err.value.content_type is None
    assert "Invalid JSON response" in err.value.summary
    assert "decode_error=ValueError" in err.value.summary


@pytest.mark.asyncio
async def test_json_invalid_payload_sanitizes_long_summary() -> None:
    response = _FakeResponse(
        status=200,
        json_body=ValueError("decode failed"),
        text_body="",
    )
    response.headers = {"Content-Type": "x" * 600}
    session = _FakeSession([response])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(api.InvalidPayloadError) as err:
        await client._json("GET", "https://example.test")
    assert len(err.value.summary) == 257
    assert err.value.summary.endswith("…")


@pytest.mark.asyncio
async def test_json_invalid_payload_handles_unparseable_url() -> None:
    response = _FakeResponse(
        status=200,
        json_body=ValueError("decode failed"),
        text_body="",
    )
    response.headers = {"Content-Type": "application/json"}
    session = _FakeSession([response])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(api.InvalidPayloadError) as err:
        await client._json("GET", object())
    assert "endpoint=" not in err.value.summary
    assert "decode_error=ValueError" in err.value.summary


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
async def test_devices_tree_uses_system_dashboard_endpoint_and_headers() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"devices": []})

    result = await client.devices_tree()

    assert result == {"devices": []}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/pv/systems/SITE/system_dashboard/devices-tree",
        headers=client._system_dashboard_headers(),
    )


@pytest.mark.asyncio
async def test_devices_tree_returns_none_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    assert await client.devices_tree() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [api.Unauthorized(), _make_cre(403), _make_cre(404)])
async def test_devices_tree_optional_errors_return_none(error) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    assert await client.devices_tree() is None


@pytest.mark.asyncio
async def test_devices_tree_non_json_payload_returns_none(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=text/html, endpoint=/pv/systems/SITE/system_dashboard/devices-tree, decode_error=ContentTypeError)",
        status=200,
        content_type="text/html",
        endpoint="/pv/systems/SITE/system_dashboard/devices-tree",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.devices_tree() is None


@pytest.mark.asyncio
async def test_devices_tree_json_invalid_payload_reraises(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=application/json, endpoint=/pv/systems/SITE/system_dashboard/devices-tree, decode_error=ValueError)",
        status=200,
        content_type="application/json",
        endpoint="/pv/systems/SITE/system_dashboard/devices-tree",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(api.InvalidPayloadError):
        await client.devices_tree()


@pytest.mark.asyncio
async def test_devices_tree_reraises_unexpected_http_error() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(500))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.devices_tree()


@pytest.mark.asyncio
async def test_devices_details_uses_system_dashboard_endpoint_and_headers() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"details": []})

    result = await client.devices_details("meter")

    assert result == {"details": []}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/pv/systems/SITE/system_dashboard/devices_details?type=meter",
        headers=client._system_dashboard_headers(),
    )


@pytest.mark.asyncio
async def test_devices_details_returns_none_when_type_invalid_or_payload_bad() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    assert await client.devices_details("") is None
    assert await client.devices_details("encharge") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [api.Unauthorized(), _make_cre(401), _make_cre(404)])
async def test_devices_details_optional_errors_return_none(error) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    assert await client.devices_details("envoy") is None


@pytest.mark.asyncio
async def test_devices_details_non_json_payload_returns_none(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=text/html, endpoint=/pv/systems/SITE/system_dashboard/devices_details, decode_error=ContentTypeError)",
        status=200,
        content_type="text/html",
        endpoint="/pv/systems/SITE/system_dashboard/devices_details",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.devices_details("envoy") is None


@pytest.mark.asyncio
async def test_devices_details_json_invalid_payload_reraises(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=application/json, endpoint=/pv/systems/SITE/system_dashboard/devices_details, decode_error=ValueError)",
        status=200,
        content_type="application/json",
        endpoint="/pv/systems/SITE/system_dashboard/devices_details",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(api.InvalidPayloadError):
        await client.devices_details("envoy")


@pytest.mark.asyncio
async def test_devices_details_reraises_unexpected_http_error() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(500))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.devices_details("envoy")


@pytest.mark.asyncio
async def test_grid_control_check_uses_grid_control_check_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"disableGridControl": False})

    result = await client.grid_control_check()

    assert result == {"disableGridControl": False}
    client._json.assert_awaited_once_with(
        "GET", f"{api.BASE_URL}/app-api/SITE/grid_control_check.json"
    )


@pytest.mark.asyncio
async def test_request_grid_toggle_otp_uses_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"success": "email sent successfully"})

    result = await client.request_grid_toggle_otp()

    assert result == {"success": "email sent successfully"}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/app-api/SITE/grid_toggle_otp.json",
        headers={"Authorization": "Bearer EAUTH"},
    )


@pytest.mark.asyncio
async def test_request_grid_toggle_otp_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.request_grid_toggle_otp()

    assert result == {}


@pytest.mark.asyncio
async def test_validate_grid_toggle_otp_success() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"valid": True})

    result = await client.validate_grid_toggle_otp("1234")

    assert result is True
    client._json.assert_awaited_once_with(
        "POST",
        f"{api.BASE_URL}/app-api/grid_toggle_otp.json",
        data={"otp": "1234", "site_id": "SITE"},
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": api.BASE_URL,
            "Authorization": "Bearer EAUTH",
        },
    )


@pytest.mark.asyncio
async def test_validate_grid_toggle_otp_returns_false_on_non_dict_or_invalid() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[["bad"], {"valid": False}])

    assert await client.validate_grid_toggle_otp("1111") is False
    assert await client.validate_grid_toggle_otp("1111") is False


@pytest.mark.asyncio
async def test_set_grid_state_uses_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"request_id": "req"})

    result = await client.set_grid_state("122447007044", 1)

    assert result == {"request_id": "req"}
    client._json.assert_awaited_once_with(
        "POST",
        f"{api.BASE_URL}/pv/settings/grid_state.json",
        data={"envoy_serial_number": "122447007044", "state": 1},
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": api.BASE_URL,
            "Authorization": "Bearer EAUTH",
        },
    )


@pytest.mark.asyncio
async def test_set_grid_state_returns_empty_on_non_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=None)

    result = await client.set_grid_state("122447007044", 2)

    assert result == {}


@pytest.mark.asyncio
async def test_log_grid_change_uses_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "Grid Change Logged"})

    result = await client.log_grid_change(
        "122447007044",
        "OPER_RELAY_CLOSED",
        "OPER_RELAY_OFFGRID_AC_GRID_PRESENT",
    )

    assert result == {"status": "Grid Change Logged"}
    client._json.assert_awaited_once_with(
        "POST",
        f"{api.BASE_URL}/pv/settings/log_grid_change.json",
        data={
            "envoy_serial_number": "122447007044",
            "old_state": "OPER_RELAY_CLOSED",
            "new_state": "OPER_RELAY_OFFGRID_AC_GRID_PRESENT",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": api.BASE_URL,
            "Authorization": "Bearer EAUTH",
        },
    )


@pytest.mark.asyncio
async def test_log_grid_change_returns_empty_on_non_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value="bad")

    result = await client.log_grid_change("ENV", "OLD", "NEW")

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
async def test_grid_control_check_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.grid_control_check()

    assert result == {}


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
async def test_dry_contacts_settings_uses_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"contacts": []})

    result = await client.dry_contacts_settings()

    assert result == {"contacts": []}
    client._json.assert_awaited_once_with(
        "GET", f"{api.BASE_URL}/pv/settings/SITE/dry_contacts"
    )


@pytest.mark.asyncio
async def test_dry_contacts_settings_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.dry_contacts_settings()

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
    token = _make_token({"user_id": "42"})
    client = _make_client()
    client.update_credentials(
        eauth=token,
        cookie=(
            "enlighten_manager_token_production="
            f"{token}; XSRF-TOKEN=xsrf-token; other=1"
        ),
    )
    client._json = AsyncMock(return_value={"criticalAlertActive": False})
    out = await client.storm_guard_alert()
    assert out == {"criticalAlertActive": False}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "stormGuard" in args[1]
    assert kwargs["headers"]["Authorization"] == f"Bearer {token}"
    assert kwargs["headers"]["Username"] == "42"
    assert kwargs["headers"]["Origin"] == "https://battery-profile-ui.enphaseenergy.com"
    assert kwargs["headers"]["Referer"] == "https://battery-profile-ui.enphaseenergy.com/"


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
    client.update_credentials(
        eauth=token,
        cookie=(
            "enlighten_manager_token_production=cookie-bearer;"
            " XSRF-TOKEN=xsrf-token; other=1"
        ),
    )
    client._json = AsyncMock(return_value={"data": {}})

    out = await client.battery_site_settings()

    assert out == {"data": {}}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "siteSettings" in args[1]
    assert kwargs["params"]["userId"] == "77"
    assert kwargs["headers"]["Authorization"] == f"Bearer {token}"
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
    client.update_credentials(eauth=token, cookie="XSRF-TOKEN=xsrf%3Dtoken; other=1")
    client._json = AsyncMock(return_value={"message": "success"})

    out = await client.set_battery_settings({"veryLowSoc": 15})

    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "batterySettings" in args[1]
    assert kwargs["params"]["userId"] == "88"
    assert kwargs["headers"]["X-XSRF-Token"] == "xsrf=token"
    assert kwargs["json"] == {"veryLowSoc": 15}


@pytest.mark.asyncio
async def test_acquire_xsrf_token_uses_cfg_validation_payload() -> None:
    token = _make_token({"user_id": "88"})
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=fresh-token; Path=/; Secure")]
    )
    session = _FakeSession([response])
    client = _make_client(session)
    client.update_credentials(
        eauth=token,
        cookie="session=1; BP-XSRF-Token=stale-token; other=1",
    )

    out = await client._acquire_xsrf_token()  # noqa: SLF001

    assert out == "fresh-token"
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/service/batteryConfig/api/v1/battery/sites/SITE/schedules/isValid")
    assert kwargs["json"] == {
        "scheduleType": "cfg",
        "forceScheduleOpted": True,
    }
    assert kwargs["headers"]["Cookie"] == "session=1; other=1"
    assert kwargs["headers"]["Username"] == "88"


@pytest.mark.asyncio
async def test_acquire_xsrf_token_preserves_original_eauth_header() -> None:
    bearer = _make_token({"user_id": "88"})
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=fresh-token; Path=/; Secure")]
    )
    session = _FakeSession([response])
    client = _make_client(session)
    client.update_credentials(
        eauth="session-token",
        cookie=f"session=1; enlighten_manager_token_production={bearer}",
    )

    await client._acquire_xsrf_token()  # noqa: SLF001

    assert session.calls[0][2]["headers"]["Authorization"] == f"Bearer {bearer}"
    assert session.calls[0][2]["headers"]["e-auth-token"] == "session-token"


@pytest.mark.asyncio
async def test_acquire_xsrf_token_uses_getall_fallback_and_handles_bad_cookie() -> None:
    token = _make_token({"user_id": "88"})

    class _BadStringCookie:
        def __bool__(self) -> bool:
            return True

        def __str__(self) -> str:
            raise RuntimeError("boom")

    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict(
        [
            ("Set-Cookie", "other=1; Path=/"),
            ("Set-Cookie", "BP-XSRF-Token=fallback-token; Path=/; Secure"),
        ]
    )
    session = _FakeSession([response])
    client = _make_client(session)
    client.update_credentials(eauth=token, cookie="session=1")
    client._cookie = _BadStringCookie()  # noqa: SLF001

    out = await client._acquire_xsrf_token()  # noqa: SLF001

    assert out == "fallback-token"
    assert "Cookie" not in session.calls[0][2]["headers"]


@pytest.mark.asyncio
async def test_acquire_xsrf_token_returns_none_when_cookie_missing() -> None:
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict()
    session = _FakeSession([response])
    client = _make_client(session)

    assert await client._acquire_xsrf_token() is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_schedule_crud_methods_build_requests() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"message": "success"})

    async def _acquire() -> str:
        client._bp_xsrf_token = "fresh-token"  # noqa: SLF001
        return "fresh-token"

    client._acquire_xsrf_token = AsyncMock(side_effect=_acquire)  # noqa: SLF001

    await client.create_battery_schedule(
        schedule_type="cfg",
        start_time="22:30:59",
        end_time="06:45:00",
        limit=95,
        days=["1", 7],
        timezone="Europe/Lisbon",
    )
    client._bp_xsrf_token = None  # noqa: SLF001
    await client.delete_battery_schedule("sched-1")
    await client.validate_battery_schedule("dtg")

    create_call, delete_call, validate_call = client._json.await_args_list
    assert create_call.args == (
        "POST",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/battery/sites/SITE/schedules",
    )
    assert create_call.kwargs["json"] == {
        "timezone": "Europe/Lisbon",
        "startTime": "22:30",
        "endTime": "06:45",
        "limit": 95,
        "scheduleType": "CFG",
        "days": [1, 7],
    }
    assert delete_call.args == (
        "POST",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/battery/sites/SITE/schedules/sched-1/delete",
    )
    assert delete_call.kwargs["json"] == {}
    assert validate_call.args == (
        "POST",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/battery/sites/SITE/schedules/isValid",
    )
    assert validate_call.kwargs["json"] == {
        "scheduleType": "dtg",
        "forceScheduleOpted": True,
    }
    assert client._acquire_xsrf_token.await_count == 2


@pytest.mark.asyncio
async def test_set_battery_settings_reacquires_xsrf_for_each_write() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"message": "success"})
    call_number = 0

    async def _acquire() -> str:
        nonlocal call_number
        call_number += 1
        client._bp_xsrf_token = f"fresh-token-{call_number}"  # noqa: SLF001
        return client._bp_xsrf_token  # noqa: SLF001

    client._acquire_xsrf_token = AsyncMock(side_effect=_acquire)  # noqa: SLF001

    await client.set_battery_settings({"veryLowSoc": 15})
    await client.set_battery_settings({"veryLowSoc": 20})

    first_call, second_call = client._json.await_args_list
    assert first_call.kwargs["headers"]["X-XSRF-Token"] == "fresh-token-1"
    assert second_call.kwargs["headers"]["X-XSRF-Token"] == "fresh-token-2"
    assert client._acquire_xsrf_token.await_count == 2
    assert client._bp_xsrf_token is None  # noqa: SLF001


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
async def test_opt_out_storm_alert_passes_payload_and_xsrf() -> None:
    token = _make_token({"user_id": "99"})
    client = _make_client()
    client.update_credentials(
        eauth=token,
        cookie="XSRF-TOKEN=xsrf-token; other=1",
    )
    client._json = AsyncMock(return_value={"message": "success"})

    out = await client.opt_out_storm_alert(alert_id="IDV21037", name="Severe Weather")

    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "stormGuard/" in args[1]
    assert args[1].endswith("/stormAlert")
    assert kwargs["json"] == {
        "stormAlerts": [
            {"id": "IDV21037", "name": "Severe Weather", "status": "opted-out"}
        ]
    }
    assert kwargs["headers"]["X-XSRF-Token"] == "xsrf-token"
    assert "params" not in kwargs


@pytest.mark.asyncio
async def test_opt_out_storm_alert_handles_missing_xsrf() -> None:
    client = _make_client()
    client.update_credentials(cookie="cookie=1")
    client._json = AsyncMock(return_value={"message": "success"})

    await client.opt_out_storm_alert(alert_id="IDV21037", name="Severe Weather")

    _args, kwargs = client._json.await_args
    assert "X-XSRF-Token" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_battery_config_prefers_cookie_bearer_when_it_has_user_id() -> None:
    eauth_token = _make_token({"user_id": "99"})
    cookie_token = _make_token({"user_id": "123"})
    client = _make_client()
    client.update_credentials(
        eauth=eauth_token,
        cookie=(
            "enlighten_manager_token_production="
            f"{cookie_token}; XSRF-TOKEN=token; other=1"
        ),
    )
    client._json = AsyncMock(return_value={"message": "success"})

    await client.set_storm_guard(enabled=True, evse_enabled=True)

    _args, kwargs = client._json.await_args
    assert kwargs["headers"]["Authorization"] == f"Bearer {cookie_token}"
    assert kwargs["headers"]["Username"] == "123"
    assert kwargs["params"]["userId"] == "123"


@pytest.mark.asyncio
async def test_set_storm_guard_handles_missing_xsrf() -> None:
    client = _make_client()
    client.update_credentials(cookie="cookie=1")
    client._json = AsyncMock(return_value={"message": "success"})

    await client.set_storm_guard(enabled=False, evse_enabled=True)

    _args, kwargs = client._json.await_args
    assert "X-XSRF-Token" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_set_storm_guard_uses_bp_xsrf_cookie_fallback() -> None:
    client = _make_client()
    client.update_credentials(cookie="BP-XSRF-Token=bp%3Dtoken; other=1")
    client._json = AsyncMock(return_value={"message": "success"})

    await client.set_storm_guard(enabled=False, evse_enabled=True)

    _args, kwargs = client._json.await_args
    assert kwargs["headers"]["X-XSRF-Token"] == "bp=token"


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
                        "heatpump": [None, "4.2", "bad"],
                        "water_heater": [0, "15"],
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
    assert payload["heatpump"] == [None, 4.2, None]
    assert payload["water_heater"] == [0.0, 15.0]
    assert payload["interval_minutes"] == 15.0


@pytest.mark.asyncio
async def test_latest_power_normalization() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "latest_power": {
                "value": 752,
                "units": "W",
                "precision": 0,
                "time": 1_773_207_600,
            }
        }
    )

    payload = await client.latest_power()

    assert payload == {
        "value": 752.0,
        "units": "W",
        "precision": 0,
        "time": 1_773_207_600,
    }
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/app-api/SITE/get_latest_power",
    )


def test_normalize_latest_power_payload_rejects_invalid_shapes() -> None:
    client = _make_client()

    assert client._normalize_latest_power_payload("bad") is None  # noqa: SLF001
    assert client._normalize_latest_power_payload({}) is None  # noqa: SLF001
    assert client._normalize_latest_power_payload(  # noqa: SLF001
        {"latest_power": {"units": "W"}}
    ) is None
    assert client._normalize_latest_power_payload(  # noqa: SLF001
        {"latest_power": {"value": "bad"}}
    ) is None
    assert client._normalize_latest_power_payload(  # noqa: SLF001
        {"latest_power": {"value": False}}
    ) is None
    assert client._normalize_latest_power_payload(  # noqa: SLF001
        {"latest_power": {"value": 752, "precision": True, "time": False}}
    ) == {"value": 752.0}

    assert client._normalize_latest_power_payload(  # noqa: SLF001
        {
            "data": {
                "latest_power": {
                    "value": "600.5",
                    "units": "W",
                    "precision": "1",
                    "time": "1773207600000",
                }
            }
        }
    ) == {
        "value": 600.5,
        "units": "W",
        "precision": 1,
        "time": 1_773_207_600,
    }


def test_normalize_latest_power_payload_handles_unstringable_units_and_nan_metadata() -> None:
    client = _make_client()

    class BadString:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert client._normalize_latest_power_payload(  # noqa: SLF001
        {
            "latest_power": {
                "value": 752,
                "units": BadString(),
                "precision": "nan",
                "time": "nan",
            }
        }
    ) == {"value": 752.0}


@pytest.mark.asyncio
async def test_evse_timeseries_daily_energy_normalization() -> None:
    client = _make_client()
    client.update_credentials(eauth=_make_token({"user_id": "user-123"}))
    client._json = AsyncMock(
        return_value={
            "data": {
                TEST_EVSE_SERIAL: {
                    "days": [
                        {"date": "2026-03-10", "energy_wh": 1200},
                        {"date": "2026-03-11", "energy_kwh": "2.5"},
                    ],
                    "intervalMinutes": "1440",
                    "lastReportDate": "2026-03-11T10:00:00+00:00",
                },
                "EVSE-2": {
                    "2026-03-11": "3.1",
                },
            }
        }
    )

    payload = await client.evse_timeseries_daily_energy()

    assert payload[TEST_EVSE_SERIAL]["day_values_kwh"] == {
        "2026-03-10": pytest.approx(1.2),
        "2026-03-11": pytest.approx(2.5),
    }
    assert payload[TEST_EVSE_SERIAL]["energy_kwh"] == pytest.approx(2.5)
    assert payload[TEST_EVSE_SERIAL]["interval_minutes"] == pytest.approx(1440.0)
    assert payload["EVSE-2"]["energy_kwh"] == pytest.approx(3.1)
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "/service/timeseries/evse/timeseries/daily_energy" in args[1]
    assert "siteId=SITE" in args[1]
    assert kwargs["headers"]["username"] == "user-123"


@pytest.mark.asyncio
async def test_evse_timeseries_lifetime_energy_normalization() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": [
                {
                    "serial": TEST_EVSE_SERIAL,
                    "lifetime_energy_wh": 45600,
                    "interval": "60",
                    "last_report_date": 1_700_000_000,
                },
                {
                    "serial_number": "EVSE-2",
                    "values": [{"value_kwh": "12.4"}],
                },
            ]
        }
    )

    payload = await client.evse_timeseries_lifetime_energy()

    assert payload[TEST_EVSE_SERIAL]["energy_kwh"] == pytest.approx(45.6)
    assert payload[TEST_EVSE_SERIAL]["interval_minutes"] == pytest.approx(60.0)
    assert payload["EVSE-2"]["energy_kwh"] == pytest.approx(12.4)


@pytest.mark.asyncio
async def test_evse_timeseries_wraps_unavailable() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(503, "service unavailable"))

    with pytest.raises(api.EVSETimeseriesUnavailable):
        await client.evse_timeseries_daily_energy()

    with pytest.raises(api.EVSETimeseriesUnavailable):
        await client.evse_timeseries_lifetime_energy()


@pytest.mark.asyncio
async def test_evse_timeseries_methods_handle_username_and_reraise() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=None)

    assert await client.evse_timeseries_lifetime_energy(username="user-1") is None
    args, _kwargs = client._json.await_args
    assert "username=user-1" in args[1]

    client._json = AsyncMock(side_effect=_make_cre(400, "bad request"))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.evse_timeseries_daily_energy()

    client._json = AsyncMock(side_effect=_make_cre(400, "bad request"))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.evse_timeseries_lifetime_energy()


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
async def test_hems_consumption_lifetime_normalization() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "production": [100, "200"],
                "evse": [None, "3.2"],
                "heatpump": [0, "8.5", "bad"],
                "water_heater": "skip",
                "start_date": "2024-01-01",
                "last_report_date": 1700000000,
                "update_pending": False,
                "interval": "30",
            }
        }
    )

    payload = await client.hems_consumption_lifetime()

    assert payload["production"] == [100.0, 200.0]
    assert payload["evse"] == [None, 3.2]
    assert payload["heatpump"] == [0.0, 8.5, None]
    assert payload["water_heater"] == []
    assert payload["start_date"] == "2024-01-01"
    assert payload["last_report_date"] == 1700000000
    assert payload["update_pending"] is False
    assert payload["interval_minutes"] == 30.0


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_uses_control_headers() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR; XSRF-TOKEN=xsrf",
        eauth="EAUTH",
    )
    client._json = AsyncMock(return_value={"heatpump": []})

    await client.hems_consumption_lifetime()

    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert args[1].endswith("/systems/SITE/hems_consumption_lifetime")
    assert callable(kwargs["headers"])
    headers = kwargs["headers"]()
    assert headers["Authorization"] == "Bearer BEAR"
    assert headers["e-auth-token"] == "EAUTH"
    assert headers["X-CSRF-Token"] == "xsrf"


@pytest.mark.asyncio
async def test_hems_devices_uses_dedicated_endpoint_and_headers() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR; XSRF-TOKEN=xsrf",
        eauth="EAUTH",
    )
    client._json = AsyncMock(return_value={"data": {"hems-devices": {}}})

    payload = await client.hems_devices()

    assert payload == {"data": {"hems-devices": {}}}
    assert client.hems_site_supported is True
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert args[1].endswith("/api/v1/hems/SITE/hems-devices?refreshData=false")
    assert callable(kwargs["headers"])
    headers = kwargs["headers"]()
    assert headers["Authorization"] == "Bearer BEAR"
    assert headers["e-auth-token"] == "EAUTH"
    assert headers["X-CSRF-Token"] == "xsrf"


@pytest.mark.asyncio
async def test_hems_devices_supports_refresh_data_query_flag() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": {}})

    await client.hems_devices(refresh_data=True)

    args, _kwargs = client._json.await_args
    assert args[1].endswith("/api/v1/hems/SITE/hems-devices?refreshData=true")


@pytest.mark.asyncio
async def test_hems_devices_returns_none_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    assert await client.hems_devices() is None


@pytest.mark.asyncio
async def test_hems_devices_returns_none_on_unauthorized() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=api.Unauthorized("nope"))

    assert await client.hems_devices() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_hems_devices_optional_errors_return_none(monkeypatch, status) -> None:
    client = _make_client()
    err = _make_cre(status, "Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_devices() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        '{"type":"hemsIntegrationError","error":{"code":900,"status":"INVALID_SITE","message":"Site is not a valid HEMS site"}}',
        "INVALID_SITE: Site is not a valid HEMS site",
    ],
)
async def test_hems_devices_invalid_site_error_returns_none(monkeypatch, message) -> None:
    client = _make_client()
    err = _make_cre(550, message)
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_devices() is None
    assert client.hems_site_supported is False


@pytest.mark.asyncio
async def test_hems_devices_non_json_payload_returns_none(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=text/html, endpoint=/api/v1/hems/SITE/hems-devices, decode_error=ContentTypeError)",
        status=200,
        content_type="text/html",
        endpoint="/api/v1/hems/SITE/hems-devices",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_devices() is None


@pytest.mark.asyncio
async def test_hems_devices_json_invalid_payload_reraises(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=application/json, endpoint=/api/v1/hems/SITE/hems-devices, decode_error=ValueError)",
        status=200,
        content_type="application/json",
        endpoint="/api/v1/hems/SITE/hems-devices",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(api.InvalidPayloadError):
        await client.hems_devices()


def test_is_optional_non_json_payload_false_for_invalid_status() -> None:
    err = api.InvalidPayloadError(
        "Invalid JSON response",
        status=200,
        content_type="text/html",
        endpoint="/systems/SITE/hems_power_timeseries",
    )
    err.status = "bad"  # type: ignore[assignment]

    assert api._is_optional_non_json_payload(err) is False


def test_is_optional_non_json_payload_false_for_non_2xx_status() -> None:
    err = api.InvalidPayloadError(
        "Invalid JSON response",
        status=500,
        content_type="text/html",
        endpoint="/systems/SITE/hems_power_timeseries",
    )

    assert api._is_optional_non_json_payload(err) is False


def test_is_hems_invalid_site_error_handles_invalid_status_value() -> None:
    err = _make_cre(550, "INVALID_SITE")
    err.status = "bad"  # type: ignore[assignment]

    assert api._is_hems_invalid_site_error(err) is False


def test_is_hems_invalid_site_error_handles_empty_message() -> None:
    err = _make_cre(550, "")

    assert api._is_hems_invalid_site_error(err) is False


def test_is_hems_invalid_site_error_handles_non_dict_json() -> None:
    err = _make_cre(550, '["INVALID_SITE"]')

    assert api._is_hems_invalid_site_error(err) is False


def test_is_hems_invalid_site_error_handles_non_matching_json_dict() -> None:
    err = _make_cre(550, '{"type":"other","error":{"status":"NOPE"}}')

    assert api._is_hems_invalid_site_error(err) is False


def test_is_hems_invalid_site_error_accepts_missing_type_with_invalid_status() -> None:
    err = _make_cre(
        550,
        '{"error":{"code":900,"status":"INVALID_SITE","message":"Site is not a valid HEMS site"}}',
    )

    assert api._is_hems_invalid_site_error(err) is True


def test_is_hems_invalid_site_error_accepts_code_and_message_fallback() -> None:
    err = _make_cre(
        550,
        '{"type":"hemsIntegrationError","error":{"code":900,"status":"OTHER","message":"Site is not a valid HEMS site"}}',
    )

    assert api._is_hems_invalid_site_error(err) is True


@pytest.mark.asyncio
async def test_hems_devices_reraises_non_optional_error(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(500, "Server Error")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_devices()


@pytest.mark.asyncio
async def test_lifetime_energy_normalization_accepts_alias_fields() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "evse": [1],
                "heat_pump": [10, "20"],
                "water-heater": [30],
                "evse_charging": [40],
                "startDate": "2024-01-02",
                "lastReportDate": 1700000001,
                "updatePending": True,
                "systemId": 12345,
                "intervalMinutes": "45",
            }
        }
    )

    payload = await client.lifetime_energy()

    assert payload["heatpump"] == [10.0, 20.0]
    assert payload["water_heater"] == [30.0]
    # Canonical key wins when alias and canonical are both provided.
    assert payload["evse"] == [1.0]
    assert payload["start_date"] == "2024-01-02"
    assert payload["last_report_date"] == 1700000001
    assert payload["update_pending"] is True
    assert payload["system_id"] == 12345
    assert payload["interval_minutes"] == 45.0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_hems_consumption_lifetime_optional_errors_return_none(
    monkeypatch, status
) -> None:
    client = _make_client()
    err = _make_cre(status, "Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_consumption_lifetime() is None


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_invalid_site_error_returns_none(
    monkeypatch,
) -> None:
    client = _make_client()
    err = _make_cre(
        550,
        '{"type":"hemsIntegrationError","error":{"code":900,"status":"INVALID_SITE","message":"Site is not a valid HEMS site"}}',
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_consumption_lifetime() is None
    assert client.hems_site_supported is False


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_non_json_payload_returns_none(
    monkeypatch,
) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=text/html, endpoint=/systems/SITE/hems_consumption_lifetime, decode_error=ContentTypeError)",
        status=200,
        content_type="text/html",
        endpoint="/systems/SITE/hems_consumption_lifetime",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_consumption_lifetime() is None


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_reraises_non_optional_error(
    monkeypatch,
) -> None:
    client = _make_client()
    err = _make_cre(500, "Server Error")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_consumption_lifetime()


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_json_invalid_payload_reraises(
    monkeypatch,
) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=application/json, endpoint=/systems/SITE/hems_consumption_lifetime, decode_error=ValueError)",
        status=200,
        content_type="application/json",
        endpoint="/systems/SITE/hems_consumption_lifetime",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(api.InvalidPayloadError):
        await client.hems_consumption_lifetime()


@pytest.mark.asyncio
async def test_hems_power_timeseries_normalization() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "heat_pump_consumption": [None, "1200.5", "bad", 900],
            "startDate": "2026-02-27T00:00:00Z",
            "interval": "5",
        }
    )

    payload = await client.hems_power_timeseries(device_uid="HP-1")

    assert payload == {
        "heat_pump_consumption": [None, 1200.5, None, 900.0],
        "start_date": "2026-02-27T00:00:00Z",
        "interval_minutes": 5.0,
    }
    assert client.hems_site_supported is True
    awaited = client._json.await_args
    assert awaited.args[0] == "GET"
    assert awaited.args[1].endswith("/systems/SITE/hems_power_timeseries?device-uid=HP-1")


@pytest.mark.asyncio
async def test_hems_power_timeseries_uses_control_headers() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR; XSRF-TOKEN=xsrf",
        eauth="EAUTH",
    )
    client._json = AsyncMock(return_value={"heat_pump_consumption": []})

    await client.hems_power_timeseries(device_uid="HP-1")

    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert args[1].endswith("/systems/SITE/hems_power_timeseries?device-uid=HP-1")
    assert callable(kwargs["headers"])
    headers = kwargs["headers"]()
    assert headers["Authorization"] == "Bearer BEAR"
    assert headers["e-auth-token"] == "EAUTH"
    assert headers["X-CSRF-Token"] == "xsrf"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_hems_power_timeseries_optional_errors_return_none(
    monkeypatch, status
) -> None:
    client = _make_client()
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=_make_cre(status)))

    assert await client.hems_power_timeseries() is None


@pytest.mark.asyncio
async def test_hems_power_timeseries_invalid_site_error_returns_none(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_json",
        AsyncMock(
            side_effect=_make_cre(
                550,
                '{"type":"hemsIntegrationError","error":{"code":900,"status":"INVALID_SITE","message":"Site is not a valid HEMS site"}}',
            )
        ),
    )

    assert await client.hems_power_timeseries() is None
    assert client.hems_site_supported is False


@pytest.mark.asyncio
async def test_hems_power_timeseries_unauthorized_returns_none(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=api.Unauthorized()))

    assert await client.hems_power_timeseries() is None


@pytest.mark.asyncio
async def test_hems_power_timeseries_non_json_payload_returns_none(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=text/html, endpoint=/systems/SITE/hems_power_timeseries, decode_error=ContentTypeError)",
        status=200,
        content_type="text/html",
        endpoint="/systems/SITE/hems_power_timeseries",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_power_timeseries() is None


@pytest.mark.asyncio
async def test_hems_power_timeseries_json_invalid_payload_reraises(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=application/json, endpoint=/systems/SITE/hems_power_timeseries, decode_error=ValueError)",
        status=200,
        content_type="application/json",
        endpoint="/systems/SITE/hems_power_timeseries",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(api.InvalidPayloadError):
        await client.hems_power_timeseries()


@pytest.mark.asyncio
async def test_hems_power_timeseries_retries_without_device_uid_on_date_422() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            _make_cre(422, '{"reason":"Saisissez une date valide."}'),
            {
                "heat_pump_consumption": [100.0, "200.5"],
                "start_date": 1771628400,
                "interval_minutes": 5,
            },
        ]
    )

    payload = await client.hems_power_timeseries(device_uid="HP-1")

    assert payload == {
        "heat_pump_consumption": [100.0, 200.5],
        "start_date": 1771628400,
        "interval_minutes": 5.0,
    }
    assert client._json.await_count == 2
    first_call = client._json.await_args_list[0]
    second_call = client._json.await_args_list[1]
    assert first_call.args[0] == "GET"
    assert first_call.args[1].endswith("/systems/SITE/hems_power_timeseries?device-uid=HP-1")
    assert second_call.args[0] == "GET"
    assert second_call.args[1].endswith("/systems/SITE/hems_power_timeseries")
    assert "device-uid=" not in second_call.args[1]


@pytest.mark.asyncio
async def test_hems_power_timeseries_invalid_date_422_without_device_uid_returns_none(
    monkeypatch,
) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_json",
        AsyncMock(side_effect=_make_cre(422, '{"reason":"Please enter a valid date."}')),
    )

    assert await client.hems_power_timeseries() is None


@pytest.mark.asyncio
async def test_hems_power_timeseries_retry_invalid_date_422_returns_none() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            _make_cre(422, '{"reason":"Please enter a valid date."}'),
            _make_cre(422, '{"reason":"Saisissez une date valide."}'),
        ]
    )

    assert await client.hems_power_timeseries(device_uid="HP-1") is None
    assert client._json.await_count == 2


@pytest.mark.asyncio
async def test_hems_power_timeseries_retry_unauthorized_returns_none() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            _make_cre(422, '{"reason":"Please enter a valid date."}'),
            api.Unauthorized(),
        ]
    )

    assert await client.hems_power_timeseries(device_uid="HP-1") is None
    assert client._json.await_count == 2


@pytest.mark.asyncio
async def test_hems_power_timeseries_retry_invalid_site_returns_none() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            _make_cre(422, '{"reason":"Please enter a valid date."}'),
            _make_cre(
                550,
                '{"type":"hemsIntegrationError","error":{"code":900,"status":"INVALID_SITE","message":"Site is not a valid HEMS site"}}',
            ),
        ]
    )

    assert await client.hems_power_timeseries(device_uid="HP-1") is None
    assert client.hems_site_supported is False
    assert client._json.await_count == 2


@pytest.mark.asyncio
async def test_hems_power_timeseries_retry_non_json_payload_returns_none() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            _make_cre(422, '{"reason":"Please enter a valid date."}'),
            api.InvalidPayloadError(
                "Invalid JSON response (status=200, content_type=text/html, endpoint=/systems/SITE/hems_power_timeseries, decode_error=ContentTypeError)",
                status=200,
                content_type="text/html",
                endpoint="/systems/SITE/hems_power_timeseries",
            ),
        ]
    )

    assert await client.hems_power_timeseries(device_uid="HP-1") is None
    assert client._json.await_count == 2


@pytest.mark.asyncio
async def test_hems_power_timeseries_retry_json_invalid_payload_reraises() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            _make_cre(422, '{"reason":"Please enter a valid date."}'),
            api.InvalidPayloadError(
                "Invalid JSON response (status=200, content_type=application/json, endpoint=/systems/SITE/hems_power_timeseries, decode_error=ValueError)",
                status=200,
                content_type="application/json",
                endpoint="/systems/SITE/hems_power_timeseries",
            ),
        ]
    )

    with pytest.raises(api.InvalidPayloadError):
        await client.hems_power_timeseries(device_uid="HP-1")


@pytest.mark.asyncio
async def test_hems_power_timeseries_retry_non_optional_error_reraises() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            _make_cre(422, '{"reason":"Please enter a valid date."}'),
            _make_cre(500, "server error"),
        ]
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_power_timeseries(device_uid="HP-1")
    assert client._json.await_count == 2


def test_is_hems_invalid_date_error_handles_unstringable_message() -> None:
    class _BadString:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    class _Err:
        status = 422
        message = _BadString()

    assert api.EnphaseEVClient._is_hems_invalid_date_error(_Err()) is False


@pytest.mark.asyncio
async def test_hems_power_timeseries_non_date_422_reraises(monkeypatch) -> None:
    client = _make_client()
    mocked = AsyncMock(side_effect=_make_cre(422, "unprocessable entity"))
    monkeypatch.setattr(client, "_json", mocked)

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_power_timeseries(device_uid="HP-1")
    assert mocked.await_count == 1


@pytest.mark.asyncio
async def test_hems_power_timeseries_reraises_non_optional_error(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=_make_cre(500)))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_power_timeseries()


def test_normalize_hems_power_timeseries_payload_handles_invalid_shapes() -> None:
    client = _make_client()

    assert client._normalize_hems_power_timeseries_payload("bad") is None  # noqa: SLF001
    assert client._normalize_hems_power_timeseries_payload(  # noqa: SLF001
        {"heat_pump_consumption": "not-a-list"}
    ) == {"heat_pump_consumption": []}


def test_normalize_hems_power_timeseries_payload_accepts_alias_keys() -> None:
    client = _make_client()

    assert client._normalize_hems_power_timeseries_payload(  # noqa: SLF001
        {
            "data": {
                "heatpump_consumption": [None, "550.5", "bad"],
                "startDate": "2026-02-27T00:00:00Z",
                "intervalMinutes": "5",
            }
        }
    ) == {
        "heat_pump_consumption": [None, 550.5, None],
        "start_date": "2026-02-27T00:00:00Z",
        "interval_minutes": 5.0,
    }


def test_normalize_hems_power_timeseries_payload_finds_fallback_heatpump_key() -> None:
    client = _make_client()

    assert client._normalize_hems_power_timeseries_payload(  # noqa: SLF001
        {
            "unrelatedSeries": [999.0],
            "heatpump_series": [111.0],
            "customHeatPumpConsumptionUnit": "W",
            "customHeatPumpConsumptionSeries": ["700.0", None, "bad"],
            "startDate": "2026-02-28T00:00:00Z",
            "intervalMinutes": 15,
        }
    ) == {
        "heat_pump_consumption": [700.0, None, None],
        "start_date": "2026-02-28T00:00:00Z",
        "interval_minutes": 15.0,
    }


def test_normalize_hems_power_timeseries_payload_skips_non_list_alias_values() -> None:
    client = _make_client()

    assert client._normalize_hems_power_timeseries_payload(  # noqa: SLF001
        {
            "heatpump": {"unit": "W"},
            "customHeatPumpConsumptionSeries": [None, "525.0"],
            "startDate": "2026-03-01T00:00:00Z",
            "intervalMinutes": 5,
        }
    ) == {
        "heat_pump_consumption": [None, 525.0],
        "start_date": "2026-03-01T00:00:00Z",
        "interval_minutes": 5.0,
    }


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
