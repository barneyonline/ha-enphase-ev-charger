from __future__ import annotations

import aiohttp
import pytest
from yarl import URL

from custom_components.enphase_ev import api


@pytest.mark.asyncio
async def test_async_authenticate_populates_site_headers(monkeypatch):
    site_headers: list[dict[str, str]] = []

    async def _fake_request_json(
        session: aiohttp.ClientSession,
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
                    "enlighten_session": "sess123",
                },
                response_url=URL(api.BASE_URL),
            )
            return {"session_id": "sid123"}
        if url == f"{api.ENTREZ_URL}/tokens":
            return {"token": "token123", "expires_at": 1700000000}
        if url == api.SITE_SEARCH_URL:
            site_headers.append(headers or {})
            return {"sites": [{"id": 7812456, "title": "Garage"}]}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", _fake_request_json)

    class StubSession:
        def __init__(self):
            self.cookie_jar = aiohttp.CookieJar()

    session = StubSession()
    tokens, sites = await api.async_authenticate(session, "user@example.com", "secret")

    assert tokens.access_token == "token123"
    assert sites and sites[0].site_id == "7812456"
    assert site_headers, "Site discovery request headers were not captured"

    captured = site_headers[0]
    assert captured["X-CSRF-Token"] == "xsrf123"
    assert captured["X-Requested-With"] == "XMLHttpRequest"
    assert captured["Referer"] == f"{api.BASE_URL}/"
    assert captured["Authorization"] == "Bearer token123"
    assert captured["e-auth-token"] == "token123"
    # Ensure the caller explicitly sets Cookie so the request works without relying on session defaults
    assert "Cookie" in captured and "enlighten_session" in captured["Cookie"]
