"""Coverage-focused tests for HTTP helpers, authentication, and fetch helpers."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from yarl import URL

from custom_components.enphase_ev import api


def _build_jwt(exp: int) -> str:
    payload = {"exp": exp}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"hdr.{payload_b64.rstrip('=')}.sig"


def _make_cre(status: int, message: str = "error") -> aiohttp.ClientResponseError:
    req_info = SimpleNamespace(real_url="https://example.test")
    return aiohttp.ClientResponseError(
        request_info=req_info, history=(), status=status, message=message
    )


class FakeResponse:
    """Minimal async response object to exercise request helpers."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        json_body: object = None,
        text_body: str = "",
        raise_text: bool = False,
    ) -> None:
        self.status = status
        self._json_body = json_body
        self._text_body = text_body
        self._raise_text = raise_text
        self.headers = headers or {"Content-Type": "application/json"}
        self.reason = "reason"
        self.request_info = SimpleNamespace(real_url="https://example.test/request")
        self.history = ()

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                self.request_info,
                self.history,
                status=self.status,
                message=self.reason,
                headers=self.headers,
            )

    async def json(self):
        return self._json_body

    async def text(self) -> str:
        if self._raise_text:
            raise RuntimeError("text unavailable")
        return self._text_body


class FakeSession:
    """Stub aiohttp.ClientSession."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []
        self.cookie_jar = aiohttp.CookieJar()

    def request(self, method: str, url: str, allow_redirects: bool = True, **kwargs):
        if not self._responses:
            raise AssertionError("No response configured for request")
        resp = self._responses.pop(0)
        resp.request_info = SimpleNamespace(real_url=url)
        self.calls.append((method, url, kwargs))
        return resp


class StubSession:
    """Lightweight object exposing only cookie_jar for auth helper tests."""

    def __init__(self) -> None:
        self.cookie_jar = aiohttp.CookieJar()


@pytest.mark.asyncio
async def test_request_json_success_builds_kwargs() -> None:
    session = FakeSession([FakeResponse(json_body={"ok": True})])
    payload = await api._request_json(
        session,
        "GET",
        "https://example.test",
        timeout=5,
        headers={"X-Test": "1"},
        data={"form": "value"},
        json_data={"payload": "value"},
    )
    assert payload == {"ok": True}
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://example.test"
    assert kwargs["headers"]["X-Test"] == "1"
    assert kwargs["data"] == {"form": "value"}
    assert kwargs["json"] == {"payload": "value"}


@pytest.mark.asyncio
async def test_request_json_raises_on_server_error() -> None:
    session = FakeSession([FakeResponse(status=503)])
    with pytest.raises(api.EnlightenAuthUnavailable):
        await api._request_json(session, "GET", "https://example.test", timeout=5)


@pytest.mark.asyncio
async def test_request_json_rejects_non_json_content() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/plain"},
                json_body={"ignored": True},
                text_body="Unexpected content" * 10,
            )
        ]
    )
    with pytest.raises(api.EnlightenAuthUnavailable) as err:
        await api._request_json(session, "GET", "https://example.test", timeout=5)
    assert "text/plain" in str(err.value)


@pytest.mark.asyncio
async def test_async_authenticate_success_with_jwt_fallback(monkeypatch) -> None:
    site_headers: list[dict[str, str]] = []

    async def fake_request_json(
        session: StubSession,
        method: str,
        url: str,
        *,
        timeout: int,
        headers: dict[str, str] | None = None,
        data=None,
        json_data=None,
    ):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies(
                {
                    "XSRF-TOKEN": "xsrf123",
                    "enlighten_session": "cookie123",
                },
                response_url=URL(api.BASE_URL),
            )
            return {"session_id": "sid123"}
        if url == f"{api.ENTREZ_URL}/tokens":
            token = _build_jwt(1_700_000_001)
            return {"token": token}
        if url.endswith("/service/evse_controller/sites"):
            site_headers.append(headers or {})
            return {"data": []}
        if url.endswith("/service/evse_controller/api/v1/sites"):
            site_headers.append(headers or {})
            return {"items": [{"siteId": "9001", "name": "Garage"}]}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    session = StubSession()
    tokens, sites = await api.async_authenticate(session, "user@example.com", "secret")

    assert tokens.access_token is not None
    assert tokens.token_expires_at == 1_700_000_001
    assert tokens.cookie and "enlighten_session" in tokens.cookie
    assert sites and sites[0].site_id == "9001"
    assert any("Authorization" in hdr for hdr in site_headers)
    assert all(hdr.get("X-CSRF-Token") == "xsrf123" for hdr in site_headers if hdr)


@pytest.mark.asyncio
async def test_async_authenticate_invalid_login_credentials(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(401)

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_authenticate(StubSession(), "user@example.com", "wrong")


@pytest.mark.asyncio
async def test_async_authenticate_client_error(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise aiohttp.ClientConnectionError()

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthUnavailable):
        await api.async_authenticate(StubSession(), "user@example.com", "oops")


@pytest.mark.asyncio
async def test_async_authenticate_re_raises_other_login_errors(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(500)

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(aiohttp.ClientResponseError):
        await api.async_authenticate(StubSession(), "user@example.com", "oops")


@pytest.mark.asyncio
async def test_async_authenticate_requires_mfa(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies(
                {
                    "login_otp_nonce": "nonce123",
                    "_enlighten_4_session": "preauth",
                },
                response_url=URL(api.BASE_URL),
            )
            return {"success": True, "isBlocked": False}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthMFARequired) as err:
        await api.async_authenticate(StubSession(), "user@example.com", "secret")
    assert err.value.tokens is not None
    assert err.value.tokens.raw_cookies


@pytest.mark.asyncio
async def test_async_validate_login_otp_success(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.MFA_VALIDATE_URL:
            session.cookie_jar.update_cookies(
                {"_enlighten_4_session": "auth", "XSRF-TOKEN": "xsrf123"},
                response_url=URL(api.BASE_URL),
            )
            return {
                "message": "success",
                "session_id": "sid123",
                "manager_token": "jwt",
            }
        if url == f"{api.ENTREZ_URL}/tokens":
            return {"token": "token123"}
        if url.endswith("/service/evse_controller/sites"):
            return {"data": [{"site_id": 1}]}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_validate_login_otp(
        StubSession(),
        "user@example.com",
        "123456",
        {"login_otp_nonce": "nonce123"},
    )

    assert tokens.access_token == "token123"
    assert sites and sites[0].site_id == "1"


@pytest.mark.asyncio
async def test_async_validate_login_otp_invalid(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"isValid": False, "isBlocked": False}

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidOTP):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "000000",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_validate_login_otp_blocked(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"isValid": False, "isBlocked": True}

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthOTPBlocked):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "000000",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_resend_login_otp_rotates_cookie(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.MFA_RESEND_URL:
            session.cookie_jar.update_cookies(
                {"login_otp_nonce": "nonce456"},
                response_url=URL(api.BASE_URL),
            )
            return {"success": True, "isBlocked": False}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens = await api.async_resend_login_otp(
        StubSession(), {"login_otp_nonce": "nonce123"}
    )

    assert tokens.raw_cookies
    assert tokens.raw_cookies.get("login_otp_nonce") == "nonce456"


@pytest.mark.asyncio
async def test_async_resend_login_otp_blocked(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"success": False, "isBlocked": True}

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthOTPBlocked):
        await api.async_resend_login_otp(
            StubSession(), {"login_otp_nonce": "nonce123"}
        )


@pytest.mark.asyncio
async def test_async_authenticate_token_endpoint_invalid_credentials(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {"session_id": "sid123"}
        if url == f"{api.ENTREZ_URL}/tokens":
            raise _make_cre(403)
        raise AssertionError("Site discovery should not be reached")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_token_endpoint_missing(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {"session_id": "sid123"}
        if url == f"{api.ENTREZ_URL}/tokens":
            raise _make_cre(404)
        return {"sites": [{"site_id": 1}]}

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(StubSession(), "user@example.com", "secret")
    assert tokens.access_token is None
    assert sites and sites[0].site_id == "1"


@pytest.mark.asyncio
async def test_async_authenticate_token_endpoint_generic_error(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {"session_id": "sid123"}
        if url == f"{api.ENTREZ_URL}/tokens":
            raise _make_cre(500)
        return {"sites": [{"id": 2}]}

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(StubSession(), "user@example.com", "secret")
    assert tokens.access_token is None
    assert sites and sites[0].site_id == "2"


@pytest.mark.asyncio
async def test_async_authenticate_token_endpoint_unavailable(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {"session_id": "sid123"}
        if url == f"{api.ENTREZ_URL}/tokens":
            raise api.EnlightenAuthUnavailable("unavailable")
        return {"data": [{"siteId": "3"}]}

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(StubSession(), "user@example.com", "secret")
    assert tokens.access_token is None
    assert sites and sites[0].site_id == "3"


@pytest.mark.asyncio
async def test_async_authenticate_token_endpoint_client_error(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {"session_id": "sid123"}
        if url == f"{api.ENTREZ_URL}/tokens":
            raise aiohttp.ClientOSError()
        return {"data": [{"siteId": "4"}]}

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(StubSession(), "user@example.com", "secret")
    assert tokens.access_token is None
    assert sites and sites[0].site_id == "4"


@pytest.mark.asyncio
async def test_async_authenticate_site_discovery_invalid_credentials(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {}
        raise _make_cre(401)

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_site_discovery_errors_continue(monkeypatch) -> None:
    calls = {"count": 0}

    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {}
        calls["count"] += 1
        if calls["count"] == 1:
            raise api.EnlightenAuthUnavailable("down")
        if calls["count"] == 2:
            raise aiohttp.ClientError()
        return {"items": [{"siteId": "55"}]}

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(StubSession(), "user@example.com", "secret")
    assert tokens.access_token is None
    assert sites and sites[0].site_id == "55"


@pytest.mark.asyncio
async def test_async_authenticate_site_discovery_handles_client_error(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {}
        if url.endswith("/service/evse_controller/sites"):
            raise _make_cre(404)
        return {"items": [{"siteId": "88"}]}

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(StubSession(), "user@example.com", "secret")
    assert tokens.access_token is None
    assert sites and sites[0].site_id == "88"


@pytest.mark.asyncio
async def test_async_fetch_chargers_requires_site_id() -> None:
    tokens = api.AuthTokens(cookie="")
    assert await api.async_fetch_chargers(MagicMock(), "", tokens) == []


@pytest.mark.asyncio
async def test_async_fetch_chargers_handles_summary_error(monkeypatch) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.summary_v2 = AsyncMock(side_effect=RuntimeError("boom"))

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)

    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    chargers = await api.async_fetch_chargers(MagicMock(), "site", tokens)
    assert chargers == []


@pytest.mark.asyncio
async def test_async_fetch_chargers_returns_normalized(monkeypatch) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.summary_v2 = AsyncMock(
                return_value={"data": [{"serial": "EV123", "name": "Garage"}]}
            )

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)

    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    chargers = await api.async_fetch_chargers(MagicMock(), "site", tokens)
    assert chargers and chargers[0].serial == "EV123"
