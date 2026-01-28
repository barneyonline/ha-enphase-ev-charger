from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

import aiohttp
import async_timeout
from yarl import URL

from .const import (
    BASE_URL,
    DEFAULT_AUTH_TIMEOUT,
    ENTREZ_URL,
    GREEN_BATTERY_SETTING,
    LOGIN_URL,
    MFA_RESEND_URL,
    MFA_VALIDATE_URL,
)

_LOGGER = logging.getLogger(__name__)


class Unauthorized(Exception):
    pass


class EnlightenAuthError(Exception):
    """Base exception for Enlighten authentication failures."""


class EnlightenAuthInvalidCredentials(EnlightenAuthError):
    """Raised when credentials are rejected."""


class EnlightenAuthMFARequired(EnlightenAuthError):
    """Raised when the API signals multi-factor authentication is required."""

    def __init__(
        self,
        message: str = "Account requires multi-factor authentication",
        tokens: AuthTokens | None = None,
    ) -> None:
        super().__init__(message)
        self.tokens = tokens


class EnlightenAuthInvalidOTP(EnlightenAuthError):
    """Raised when the MFA one-time code is invalid or expired."""


class EnlightenAuthOTPBlocked(EnlightenAuthError):
    """Raised when the MFA flow is blocked."""


class EnlightenAuthUnavailable(EnlightenAuthError):
    """Raised when the service is temporarily unavailable."""


class EnlightenTokenUnavailable(EnlightenAuthError):
    """Raised when a bearer token cannot be obtained for the account."""


@dataclass
class AuthTokens:
    """Container for Enlighten authentication state."""

    cookie: str
    session_id: str | None = None
    access_token: str | None = None
    token_expires_at: int | None = None
    raw_cookies: dict[str, str] | None = None


@dataclass
class SiteInfo:
    """Basic representation of an Enlighten site."""

    site_id: str
    name: str | None = None


@dataclass
class ChargerInfo:
    """Metadata about a charger discovered for a site."""

    serial: str
    name: str | None = None


def _serialize_cookie_jar(
    jar: aiohttp.CookieJar, urls: Iterable[str | URL]
) -> tuple[str, dict[str, str]]:
    """Return a Cookie header string and mapping extracted from the jar."""

    cookies: dict[str, str] = {}
    for url in urls:
        try:
            url_obj = url if isinstance(url, URL) else URL(str(url))
        except Exception:  # noqa: BLE001 - defensive casting
            continue
        try:
            filtered = jar.filter_cookies(url_obj)
        except Exception:  # noqa: BLE001 - defensive: filter_cookies may raise
            continue
        for key, morsel in filtered.items():
            cookies[key] = morsel.value
    header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return header, cookies


def _cookie_header_from_map(cookies: dict[str, str] | None) -> str:
    """Return a Cookie header string from a raw cookie map."""

    if not cookies:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _decode_jwt_exp(token: str) -> int | None:
    """Decode the exp claim from a JWT-like token without validation."""

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001 - defensive parsing
        return None
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return int(exp)
    return None


def _extract_xsrf_token(cookies: dict[str, str] | None) -> str | None:
    """Return the XSRF token value from the cookie jar map."""

    if not cookies:
        return None
    for name, value in cookies.items():
        if name and name.lower() == "xsrf-token":
            return value
    return None


def _seed_cookie_jar(session: aiohttp.ClientSession, cookies: dict[str, str]) -> None:
    """Ensure the session cookie jar contains the supplied cookies."""

    jar = getattr(session, "cookie_jar", None)
    if jar is None or not cookies:
        return
    try:
        jar.update_cookies(cookies, response_url=URL(BASE_URL))
    except Exception:  # noqa: BLE001 - best-effort for config flow cookie handling
        return


def _extract_login_session(payload: Any) -> tuple[str | None, str | None]:
    """Extract session id and manager token from login responses."""

    if not isinstance(payload, dict):
        return None, None
    session_id = (
        payload.get("session_id") or payload.get("sessionId") or payload.get("session")
    )
    manager_token = payload.get("manager_token") or payload.get("managerToken")
    return (
        str(session_id) if session_id else None,
        str(manager_token) if manager_token else None,
    )


async def _request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    timeout: int,
    headers: dict[str, str] | None = None,
    data: Any | None = None,
    json_data: Any | None = None,
) -> Any:
    """Perform an HTTP request returning JSON with timeout handling."""

    req_kwargs: dict[str, Any] = {}
    if headers is not None:
        req_kwargs["headers"] = headers
    if data is not None:
        req_kwargs["data"] = data
    if json_data is not None:
        req_kwargs["json"] = json_data

    async with async_timeout.timeout(timeout):
        async with session.request(
            method, url, allow_redirects=True, **req_kwargs
        ) as resp:
            if resp.status >= 500:
                raise EnlightenAuthUnavailable(f"Server error {resp.status} at {url}")
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "")
            if "json" not in ctype:
                text = await resp.text()
                raise EnlightenAuthUnavailable(
                    f"Unexpected response content-type {ctype!r} at {url}: {text[:120]}"
                )
            return await resp.json()


async def _request_mfa_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    timeout: int,
    headers: dict[str, str] | None = None,
    data: Any | None = None,
) -> Any:
    """Perform an MFA HTTP request with tolerant JSON parsing."""

    req_kwargs: dict[str, Any] = {}
    if headers is not None:
        req_kwargs["headers"] = headers
    if data is not None:
        req_kwargs["data"] = data

    async with async_timeout.timeout(timeout):
        async with session.request(
            method, url, allow_redirects=True, **req_kwargs
        ) as resp:
            if resp.status >= 500:
                raise EnlightenAuthUnavailable(f"Server error {resp.status} at {url}")
            if resp.status in (204, 205):
                return {}
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return await resp.json()
            text = await resp.text()
            if not text.strip():
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError as err:
                raise EnlightenAuthUnavailable(
                    f"Unexpected response content-type {ctype!r} at {url}: {text[:120]}"
                ) from err


def _mfa_headers(cookies: dict[str, str] | None) -> dict[str, str]:
    """Return headers for MFA endpoints with cookie/XSRF handling."""

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": f"{BASE_URL}/",
        "X-Requested-With": "XMLHttpRequest",
    }
    cookie_header = _cookie_header_from_map(cookies)
    if cookie_header:
        headers["Cookie"] = cookie_header
    xsrf_token = _extract_xsrf_token(cookies)
    if xsrf_token:
        headers["X-CSRF-Token"] = xsrf_token
    return headers


def _normalize_sites(payload: Any) -> list[SiteInfo]:
    """Normalize site payloads from various Enlighten APIs."""

    sites: list[SiteInfo] = []

    if isinstance(payload, dict):
        for key in ("sites", "data", "items"):
            nested = payload.get(key)
            if isinstance(nested, list):
                payload = nested
                break

    if isinstance(payload, list):
        items = payload
    else:
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue
        site_id = item.get("site_id") or item.get("siteId") or item.get("id")
        name = item.get("name") or item.get("site_name") or item.get("siteName")
        if site_id is None:
            continue
        sites.append(SiteInfo(site_id=str(site_id), name=str(name) if name else None))
    return sites


def _normalize_chargers(payload: Any) -> list[ChargerInfo]:
    """Normalize charger list payloads into ChargerInfo entries."""

    chargers: list[ChargerInfo] = []

    if isinstance(payload, dict):
        payload = payload.get("data") or payload

    if isinstance(payload, dict):
        # Some responses use { "chargers": [...] }
        payload = payload.get("chargers") or payload.get("evChargerData") or payload

    if isinstance(payload, list):
        items = payload
    else:
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue
        serial = (
            item.get("serial")
            or item.get("serialNumber")
            or item.get("sn")
            or item.get("id")
        )
        if not serial:
            continue
        name = item.get("name") or item.get("displayName") or item.get("display_name")
        chargers.append(
            ChargerInfo(serial=str(serial), name=str(name) if name else None)
        )
    return chargers


async def _build_tokens_and_sites(
    session: aiohttp.ClientSession,
    email: str,
    session_id: str | None,
    *,
    timeout: int,
) -> tuple[AuthTokens, list[SiteInfo]]:
    """Build auth tokens and discover accessible sites from an authenticated session."""

    cookie_header, cookie_map = _serialize_cookie_jar(
        session.cookie_jar, (BASE_URL, ENTREZ_URL)
    )
    tokens = AuthTokens(
        cookie=cookie_header,
        session_id=str(session_id) if session_id else None,
        raw_cookies=cookie_map,
    )

    # Attempt to obtain a bearer/e-auth token. If not available, proceed with cookie-only mode.
    token_payload: Any | None = None
    if tokens.session_id:
        try:
            token_payload = await _request_json(
                session,
                "POST",
                f"{ENTREZ_URL}/tokens",
                timeout=timeout,
                headers={"Accept": "application/json"},
                json_data={"session_id": tokens.session_id, "email": email},
            )
        except aiohttp.ClientResponseError as err:  # noqa: BLE001
            if err.status in (401, 403):
                raise EnlightenAuthInvalidCredentials from err
            if err.status in (404, 422, 429):
                _LOGGER.debug("Token endpoint unavailable (%s): %s", err.status, err)
            else:
                _LOGGER.debug("Token endpoint error (%s): %s", err.status, err)
        except EnlightenAuthUnavailable as err:
            _LOGGER.debug("Token endpoint unavailable: %s", err)
        except aiohttp.ClientError as err:  # noqa: BLE001
            _LOGGER.debug("Token endpoint client error: %s", err)

    if isinstance(token_payload, dict):
        token = (
            token_payload.get("token")
            or token_payload.get("auth_token")
            or token_payload.get("access_token")
        )
        if token:
            tokens.access_token = str(token)
            exp = (
                token_payload.get("expires_at")
                or token_payload.get("expiresAt")
                or token_payload.get("expiration")
            )
            if exp is None:
                exp = _decode_jwt_exp(tokens.access_token)
            tokens.token_expires_at = (
                int(exp) if isinstance(exp, (int, float)) else None
            )

    xsrf_token = _extract_xsrf_token(tokens.raw_cookies)

    # Collect accessible sites for the authenticated user.
    site_headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/",
    }
    if xsrf_token:
        site_headers["X-CSRF-Token"] = xsrf_token
    if tokens.cookie:
        site_headers["Cookie"] = tokens.cookie
    if tokens.access_token:
        site_headers["Authorization"] = f"Bearer {tokens.access_token}"
        site_headers["e-auth-token"] = tokens.access_token

    sites: list[SiteInfo] = []
    for url in (
        f"{BASE_URL}/service/evse_controller/sites",
        f"{BASE_URL}/service/evse_controller/api/v1/sites",
        f"{BASE_URL}/service/evse_controller/sites.json",
    ):
        try:
            site_payload = await _request_json(
                session,
                "GET",
                url,
                timeout=timeout,
                headers=dict(site_headers),
            )
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                raise EnlightenAuthInvalidCredentials from err
            _LOGGER.debug("Site discovery endpoint error (%s): %s", err.status, err)
            continue
        except EnlightenAuthUnavailable as err:
            _LOGGER.debug("Site discovery unavailable: %s", err)
            continue
        except aiohttp.ClientError as err:  # noqa: BLE001
            _LOGGER.debug("Site discovery client error: %s", err)
            continue
        sites = _normalize_sites(site_payload)
        if sites:
            break

    return tokens, sites


async def async_authenticate(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> tuple[AuthTokens, list[SiteInfo]]:
    """Authenticate with Enlighten and return auth tokens and accessible sites."""

    payload = {"user[email]": email, "user[password]": password}
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    try:
        data = await _request_json(
            session,
            "POST",
            LOGIN_URL,
            timeout=timeout,
            headers=headers,
            data=payload,
        )
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            raise EnlightenAuthInvalidCredentials from err
        raise
    except aiohttp.ClientError as err:  # noqa: BLE001
        raise EnlightenAuthUnavailable from err

    cookie_header, cookie_map = _serialize_cookie_jar(
        session.cookie_jar, (BASE_URL, ENTREZ_URL)
    )

    session_id, manager_token = _extract_login_session(data)

    if isinstance(data, dict) and data.get("requires_mfa"):
        tokens = AuthTokens(cookie=cookie_header, raw_cookies=cookie_map)
        raise EnlightenAuthMFARequired(
            "Account requires multi-factor authentication", tokens=tokens
        )

    if isinstance(data, dict) and data.get("isBlocked") is True:
        raise EnlightenAuthInvalidCredentials("Account is blocked")

    if session_id or manager_token:
        if not session_id:
            raise EnlightenAuthInvalidCredentials("Missing session identifier")
        return await _build_tokens_and_sites(
            session, email, session_id, timeout=timeout
        )

    if isinstance(data, dict) and data.get("success") is True:
        if cookie_map.get("login_otp_nonce"):
            tokens = AuthTokens(cookie=cookie_header, raw_cookies=cookie_map)
            raise EnlightenAuthMFARequired(
                "Account requires multi-factor authentication", tokens=tokens
            )
        raise EnlightenAuthInvalidCredentials("MFA challenge missing")

    if isinstance(data, dict) and not data:
        return await _build_tokens_and_sites(session, email, None, timeout=timeout)

    raise EnlightenAuthInvalidCredentials("Unexpected login response")


async def async_validate_login_otp(
    session: aiohttp.ClientSession,
    email: str,
    otp: str,
    cookies: dict[str, str],
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> tuple[AuthTokens, list[SiteInfo]]:
    """Validate an MFA one-time code and return auth tokens and sites."""

    email = email.strip()
    otp = otp.strip()
    if not email or not otp:
        raise EnlightenAuthInvalidCredentials("Missing OTP credentials")

    _seed_cookie_jar(session, cookies)

    payload = {
        "email": base64.b64encode(email.encode("utf-8")).decode("ascii"),
        "otp": base64.b64encode(otp.encode("utf-8")).decode("ascii"),
        "xhrFields[withCredentials]": "true",
    }
    headers = _mfa_headers(cookies)

    try:
        data = await _request_mfa_json(
            session,
            "POST",
            MFA_VALIDATE_URL,
            timeout=timeout,
            headers=headers,
            data=payload,
        )
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            _LOGGER.warning(
                "MFA validation rejected by Enlighten (status=%s)", err.status
            )
            raise EnlightenAuthInvalidCredentials from err
        if err.status == 429:
            _LOGGER.warning("MFA validation rate limited by Enlighten")
            raise EnlightenAuthOTPBlocked("MFA is blocked") from err
        if err.status in (400, 404, 409, 422):
            _LOGGER.warning(
                "MFA validation failed with client error (status=%s)", err.status
            )
            raise EnlightenAuthInvalidOTP("Invalid MFA code") from err
        raise
    except aiohttp.ClientError as err:  # noqa: BLE001
        raise EnlightenAuthUnavailable from err

    if isinstance(data, dict) and data.get("isValid") is False:
        if data.get("isBlocked") is True:
            _LOGGER.warning("MFA validation blocked by Enlighten response")
            raise EnlightenAuthOTPBlocked("MFA is blocked")
        _LOGGER.warning("MFA validation rejected by Enlighten response")
        raise EnlightenAuthInvalidOTP("Invalid MFA code")

    session_id, manager_token = _extract_login_session(data)
    if not session_id and manager_token:
        raise EnlightenAuthInvalidCredentials("Missing session identifier")
    if not session_id:
        looks_successful = False
        if isinstance(data, dict):
            looks_successful = bool(
                data.get("message") == "success"
                or data.get("success") is True
                or data.get("isValid") is True
            )
        if looks_successful or not data:
            _LOGGER.warning(
                "MFA validation missing session id; attempting token recovery"
            )
            try:
                return await _build_tokens_and_sites(
                    session, email, None, timeout=timeout
                )
            except EnlightenAuthInvalidCredentials as err:
                raise EnlightenAuthInvalidOTP("Missing MFA session identifier") from err
        raise EnlightenAuthInvalidOTP("Missing MFA session identifier")

    return await _build_tokens_and_sites(session, email, session_id, timeout=timeout)


async def async_resend_login_otp(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> AuthTokens:
    """Request a new MFA one-time code and return refreshed cookie state."""

    _seed_cookie_jar(session, cookies)

    headers = _mfa_headers(cookies)

    try:
        data = await _request_mfa_json(
            session,
            "POST",
            MFA_RESEND_URL,
            timeout=timeout,
            headers=headers,
            data={"locale": "en"},
        )
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            _LOGGER.warning("MFA resend rejected by Enlighten (status=%s)", err.status)
            raise EnlightenAuthInvalidCredentials from err
        if err.status == 429:
            _LOGGER.warning("MFA resend rate limited by Enlighten")
            raise EnlightenAuthOTPBlocked("MFA is blocked") from err
        raise
    except aiohttp.ClientError as err:  # noqa: BLE001
        raise EnlightenAuthUnavailable from err

    if isinstance(data, dict) and data.get("isBlocked") is True:
        _LOGGER.warning("MFA resend blocked by Enlighten response")
        raise EnlightenAuthOTPBlocked("MFA is blocked")
    if isinstance(data, dict) and data.get("success") is False:
        _LOGGER.warning("MFA resend rejected by Enlighten response")
        raise EnlightenAuthInvalidCredentials("MFA resend rejected")
    if not data:
        _LOGGER.warning("MFA resend returned empty response; using existing cookies")
        data = {"success": True}
    if not (isinstance(data, dict) and data.get("success") is True):
        _LOGGER.warning("MFA resend returned unexpected response")
        raise EnlightenAuthInvalidCredentials("MFA resend rejected")

    cookie_header, cookie_map = _serialize_cookie_jar(
        session.cookie_jar, (BASE_URL, ENTREZ_URL)
    )
    if not cookie_map and cookies:
        _LOGGER.warning("MFA resend did not return updated cookies; reusing existing")
        cookie_map = dict(cookies)
        cookie_header = _cookie_header_from_map(cookie_map)
    return AuthTokens(cookie=cookie_header, raw_cookies=cookie_map)


async def async_fetch_chargers(
    session: aiohttp.ClientSession,
    site_id: str,
    tokens: AuthTokens,
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> list[ChargerInfo]:
    """Fetch chargers for a site using the provided authentication tokens."""

    if not site_id:
        return []

    client = EnphaseEVClient(
        session,
        site_id,
        tokens.access_token,
        tokens.cookie,
        timeout=timeout,
    )
    try:
        payload = await client.summary_v2()
    except Exception as err:  # noqa: BLE001 - propagate as empty list for flow UX
        _LOGGER.debug("Failed to fetch charger summary for site %s: %s", site_id, err)
        return []
    return _normalize_chargers(payload)


class EnphaseEVClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        site_id: str,
        eauth: str | None,
        cookie: str | None,
        timeout: int = 15,
        reauth_callback: Callable[[], Awaitable[bool]] | None = None,
    ):
        self._timeout = int(timeout)
        self._s = session
        self._site = site_id
        # Cache working API variant indexes per action to avoid retries once discovered
        self._start_variant_idx: int | None = None
        self._start_variant_idx_with_level: int | None = None
        self._start_variant_idx_no_level: int | None = None
        self._stop_variant_idx: int | None = None
        self._cookie = cookie or ""
        self._eauth = eauth or None
        self._reauth_cb: Callable[[], Awaitable[bool]] | None = reauth_callback
        self._h = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/pv/systems/{site_id}/summary",
        }
        self.update_credentials(eauth=eauth, cookie=cookie)

    def set_reauth_callback(
        self, callback: Callable[[], Awaitable[bool]] | None
    ) -> None:
        """Register coroutine used to refresh credentials on 401."""

        self._reauth_cb = callback

    def update_credentials(
        self, *, eauth: str | None = None, cookie: str | None = None
    ) -> None:
        """Update headers when auth credentials change."""

        if eauth is not None:
            self._eauth = eauth or None
        if cookie is not None:
            self._cookie = cookie or ""

        if self._cookie:
            self._h["Cookie"] = self._cookie
        else:
            self._h.pop("Cookie", None)

        if self._eauth:
            self._h["e-auth-token"] = self._eauth
        else:
            self._h.pop("e-auth-token", None)

        # If XSRF-TOKEN cookie is present, add matching CSRF header some endpoints expect
        try:
            xsrf = None
            parts = [p.strip() for p in (self._cookie or "").split(";")]
            for p in parts:
                if p.startswith("XSRF-TOKEN="):
                    xsrf = p.split("=", 1)[1]
                    break
            if xsrf:
                self._h["X-CSRF-Token"] = xsrf
            else:
                self._h.pop("X-CSRF-Token", None)
        except Exception:
            self._h.pop("X-CSRF-Token", None)

    def _bearer(self) -> str | None:
        """Extract Authorization bearer token from cookies if present.

        Enlighten sets an `enlighten_manager_token_production` cookie with a JWT the
        frontend uses as an Authorization Bearer token for some scheduler endpoints.
        """
        try:
            parts = [p.strip() for p in (self._cookie or "").split(";")]
            for p in parts:
                if p.startswith("enlighten_manager_token_production="):
                    return p.split("=", 1)[1]
        except Exception:
            return None
        return None

    def _control_headers(self) -> dict[str, str]:
        """Return Authorization header overrides for control-plane requests."""

        bearer = self._bearer() or self._eauth
        if bearer:
            return {"Authorization": f"Bearer {bearer}"}
        return {}

    @staticmethod
    def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
        """Return a copy of headers with sensitive values masked."""

        redacted: dict[str, str] = {}
        for key, value in headers.items():
            if key.lower() in {"cookie", "authorization", "e-auth-token"}:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = value
        return redacted

    async def _json(self, method: str, url: str, **kwargs):
        """Perform an HTTP request returning JSON with sane header handling.

        Accepts optional ``headers`` in kwargs which will be merged with the
        default headers for this client, allowing call-sites to add/override
        fields (e.g. Authorization) without causing duplicate parameter errors.
        """
        # Merge headers: start with client defaults, then apply any overrides
        extra_headers = kwargs.pop("headers", None)
        attempt = 0
        while True:
            base_headers = dict(self._h)
            if isinstance(extra_headers, dict):
                base_headers.update(extra_headers)

            async with async_timeout.timeout(self._timeout):
                async with self._s.request(
                    method, url, headers=base_headers, **kwargs
                ) as r:
                    if r.status == 401:
                        if self._reauth_cb and attempt == 0:
                            attempt += 1
                            reauth_ok = await self._reauth_cb()
                            if reauth_ok:
                                continue
                        raise Unauthorized()
                    if r.status in (204, 205):
                        return {}
                    if r.status >= 400:
                        try:
                            body_text = await r.text()
                        except Exception:  # noqa: BLE001 - fall back to generic message
                            body_text = ""
                        message = (body_text or r.reason or "").strip()
                        if len(message) > 512:
                            message = f"{message[:512]}…"
                        raise aiohttp.ClientResponseError(
                            r.request_info,
                            r.history,
                            status=r.status,
                            message=message or r.reason,
                            headers=r.headers,
                        )
                    return await r.json()

    async def status(self) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/status"
        data = await self._json("GET", url)
        # Normalize alternative shapes
        if not (data.get("evChargerData") or []):
            alt = f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/status"
            try:
                data2 = await self._json("GET", alt)
                if data2:
                    data = data2
            except Exception:
                pass

        # If response is { data: { chargers: [...] } }, map to evChargerData
        try:
            inner = data.get("data") if isinstance(data, dict) else None
            chargers = inner.get("chargers") if isinstance(inner, dict) else None
            if isinstance(chargers, list) and chargers:
                out = []
                for c in chargers:
                    conn = (c.get("connectors") or [{}])[0]
                    sess = c.get("session_d") or {}
                    connectors = c.get("connectors")
                    if not connectors:
                        connectors = [conn] if conn else []
                    # Derive start_time in seconds (strt_chrg appears in ms)
                    start_ms = sess.get("strt_chrg")
                    start_sec = (
                        int(int(start_ms) / 1000) if isinstance(start_ms, int) else None
                    )
                    out.append(
                        {
                            "sn": c.get("sn"),
                            "name": c.get("name"),
                            "connected": bool(c.get("connected")),
                            "pluggedIn": bool(
                                c.get("pluggedIn") or conn.get("pluggedIn")
                            ),
                            "charging": bool(c.get("charging")),
                            "faulted": bool(c.get("faulted")),
                            "connectorStatusType": conn.get("connectorStatusType"),
                            "connectors": connectors,
                            "session_d": {
                                "e_c": sess.get("e_c"),
                                "start_time": start_sec,
                            },
                        }
                    )
                return {
                    "evChargerData": out,
                    "ts": data.get("meta", {}).get("serverTimeStamp"),
                }
        except Exception:
            # If mapping fails, fall back to raw
            pass

        return data

    @staticmethod
    def _payload_has_level(payload: dict | None) -> bool:
        """Return True when a payload explicitly includes a charging level."""

        if not isinstance(payload, dict):
            return False
        return any(key in payload for key in ("chargingLevel", "charging_level"))

    def _start_charging_candidates(
        self, sn: str, level: int, connector_id: int
    ) -> list[tuple[str, str, dict | None]]:
        return [
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"chargingLevel": level, "connectorId": connector_id},
            ),
            (
                "PUT",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"chargingLevel": level, "connectorId": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/start_charging",
                {"chargingLevel": level, "connectorId": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"charging_level": level, "connector_id": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"connectorId": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                None,
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/start_charging",
                None,
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"chargingLevel": level},
            ),
        ]

    async def start_charging(
        self,
        sn: str,
        amps: int,
        connector_id: int = 1,
        *,
        include_level: bool | None = None,
        strict_preference: bool = False,
    ) -> dict:
        """Start charging or set the charging level.

        The Enlighten API has variations across deployments (method, path, and payload keys).
        We try a sequence of known variants until one succeeds.
        When ``include_level`` is provided, variants that explicitly send the charging
        amps are preferred (include_level=True) or avoided (include_level=False).
        """
        level = int(amps)
        candidates = self._start_charging_candidates(sn, level, connector_id)
        if not candidates:
            raise aiohttp.ClientError("start_charging has no request candidates")

        indices = list(range(len(candidates)))
        level_indices = [
            idx for idx in indices if self._payload_has_level(candidates[idx][2])
        ]
        no_level_indices = [idx for idx in indices if idx not in level_indices]

        def _cache_for_preference() -> int | None:
            if include_level is True:
                return self._start_variant_idx_with_level
            if include_level is False:
                return self._start_variant_idx_no_level
            return self._start_variant_idx

        if include_level is True:
            order = list(level_indices)
            if not order and strict_preference:
                raise aiohttp.ClientError(
                    "No start_charging variants support charging level payloads"
                )
            if not strict_preference:
                order += no_level_indices
        elif include_level is False:
            order = list(no_level_indices)
            if not order and strict_preference:
                raise aiohttp.ClientError(
                    "No start_charging variants omit charging level payloads"
                )
            if not strict_preference:
                order += level_indices
        else:
            order = indices

        if not order:
            raise aiohttp.ClientError("No start_charging request candidates available")

        cache_idx = _cache_for_preference()
        if cache_idx is not None and cache_idx in order:
            order.remove(cache_idx)
            order.insert(0, cache_idx)

        def _record_variant(idx: int) -> None:
            payload = candidates[idx][2]
            has_level = self._payload_has_level(payload)
            if include_level is True and has_level:
                self._start_variant_idx_with_level = idx
                return
            if include_level is False and not has_level:
                self._start_variant_idx_no_level = idx
                return
            if include_level is None:
                self._start_variant_idx = idx
                return
            # Fallback: remember last working variant for general calls
            self._start_variant_idx = idx

        def _interpret_start_error(message: str) -> dict | None:
            """Return a benign response when backend reports non-fatal errors."""

            if not message:
                return None
            text = message.strip()
            if not text:
                return None
            lower = text.lower()
            if "already in charging state" in lower:
                return {"status": "already_charging"}
            if "not plugged" in lower:
                return {"status": "not_ready"}

            def _load_payload(raw: str) -> Any:
                try:
                    return json.loads(raw)
                except Exception:
                    stripped = raw.strip("\"'")
                    if stripped == raw:
                        raise
                    return json.loads(stripped)

            try:
                parsed = _load_payload(text)
            except Exception:
                return None
            if not isinstance(parsed, dict):
                return None
            error_obj = parsed.get("error") or parsed

            def _extract_code(obj: Any) -> str | None:
                if isinstance(obj, dict):
                    candidate = obj.get("errorMessageCode") or obj.get("code")
                    if isinstance(candidate, str):
                        return candidate.lower()
                return None

            def _extract_message(obj: Any) -> str | None:
                if not isinstance(obj, dict):
                    return None
                for key in ("displayMessage", "errorMessage", "message"):
                    val = obj.get(key)
                    if isinstance(val, str):
                        return val
                return None

            for candidate in (error_obj, parsed):
                code = _extract_code(candidate)
                if code == "iqevc_ms-10012":
                    return {"status": "already_charging"}
                if code == "iqevc_ms-10008":
                    return {"status": "not_ready"}
                display = _extract_message(candidate)
                if isinstance(display, str):
                    disp_lower = display.lower()
                    if "already in charging state" in disp_lower:
                        return {"status": "already_charging"}
                    if "not plugged" in disp_lower:
                        return {"status": "not_ready"}
            return None

        last_exc: Exception | None = None
        variant_failures: list[dict[str, Any]] = []
        base_headers = dict(self._h)
        extra_headers = self._control_headers()
        base_headers.update(extra_headers)
        for idx in order:
            method, url, payload = candidates[idx]
            headers = dict(extra_headers)
            try:
                if payload is None:
                    result = await self._json(method, url, headers=headers)
                else:
                    result = await self._json(
                        method, url, json=payload, headers=headers
                    )
                # Cache the working variant index for future calls
                _record_variant(idx)
                return result
            except aiohttp.ClientResponseError as e:
                # 409/422 (and similar) often indicate not plugged in or not ready.
                # Treat these as benign no-ops instead of surfacing as errors.
                if e.status in (409, 422):
                    _record_variant(idx)
                    return {"status": "not_ready"}
                if e.status == 400:
                    interpreted = _interpret_start_error(e.message or "")
                    if interpreted is not None:
                        _record_variant(idx)
                        status = interpreted.get("status")
                        _LOGGER.debug(
                            "start_charging treated as benign status %s for charger %s: %s %s payload=%s; response=%s",
                            status,
                            sn,
                            method,
                            url,
                            payload if payload is not None else "<no-body>",
                            e.message,
                        )
                        return interpreted
                    variant_failures.append(
                        {
                            "idx": idx,
                            "method": method,
                            "url": url,
                            "payload": payload if payload is not None else "<no-body>",
                            "response": e.message or "",
                            "headers": self._redact_headers(base_headers),
                        }
                    )
                # 400/404/405 variations likely indicate method/path mismatch; try next.
                last_exc = e
                continue
        if last_exc:
            if (
                isinstance(last_exc, aiohttp.ClientResponseError)
                and last_exc.status == 400
                and variant_failures
            ):
                sample = variant_failures[0]
                attempted = ", ".join(
                    f"{item['method']} idx {item['idx']}"
                    for item in variant_failures[1:]
                )
                attempt_suffix = (
                    f"; other variants tried: {attempted}" if attempted else ""
                )
                _LOGGER.warning(
                    "start_charging rejected (400) for charger %s: %s %s payload=%s; headers=%s; response=%s%s",
                    sn,
                    sample["method"],
                    sample["url"],
                    sample["payload"],
                    sample["headers"],
                    sample["response"],
                    attempt_suffix,
                )
            raise last_exc
        # Should not happen, but keep static analyzer happy
        raise aiohttp.ClientError(
            "start_charging failed with all variants"
        )  # pragma: no cover

    def _stop_charging_candidates(self, sn: str) -> list[tuple[str, str, dict | None]]:
        return [
            (
                "PUT",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/stop_charging",
                None,
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/stop_charging",
                None,
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/stop_charging",
                None,
            ),
        ]

    async def stop_charging(self, sn: str) -> dict:
        """Stop charging; try multiple endpoint variants."""
        candidates = self._stop_charging_candidates(sn)
        order = list(range(len(candidates)))
        if self._stop_variant_idx is not None and 0 <= self._stop_variant_idx < len(
            candidates
        ):
            order.remove(self._stop_variant_idx)
            order.insert(0, self._stop_variant_idx)

        last_exc: Exception | None = None
        extra_headers = self._control_headers()
        for idx in order:
            method, url, payload = candidates[idx]
            try:
                if payload is None:
                    result = await self._json(method, url, headers=extra_headers)
                else:
                    result = await self._json(
                        method, url, json=payload, headers=extra_headers
                    )
                self._stop_variant_idx = idx
                return result
            except aiohttp.ClientResponseError as e:
                # If charger is not plugged in or already stopped, some backends
                # respond with 400/404/409. Treat these as benign no-ops.
                if e.status in (400, 404, 409, 422):
                    self._stop_variant_idx = idx  # cache the working path even if no-op
                    return {"status": "not_active"}
                last_exc = e
                continue
        if last_exc:
            raise last_exc
        raise aiohttp.ClientError("stop_charging failed with all variants")

    async def trigger_message(self, sn: str, requested_message: str) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/trigger_message"
        payload = {"requestedMessage": requested_message}
        return await self._json(
            "POST", url, json=payload, headers=self._control_headers()
        )

    async def start_live_stream(self) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/start_live_stream"
        return await self._json("GET", url, headers=self._control_headers())

    async def stop_live_stream(self) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/stop_live_stream"
        return await self._json("GET", url, headers=self._control_headers())

    async def charge_mode(self, sn: str) -> str | None:
        """Fetch the current charge mode via scheduler API.

        GET /service/evse_scheduler/api/v1/iqevc/charging-mode/<site>/<sn>/preference
        Requires Authorization: Bearer <jwt> in addition to existing cookies.
        Returns one of: GREEN_CHARGING, SCHEDULED_CHARGING, MANUAL_CHARGING when enabled.
        """
        url = f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/{self._site}/{sn}/preference"
        headers = dict(self._h)
        headers.update(self._control_headers())
        data = await self._json("GET", url, headers=headers)
        try:
            modes = (data.get("data") or {}).get("modes") or {}
            # Prefer the mode whose 'enabled' is true
            for key in ("greenCharging", "scheduledCharging", "manualCharging"):
                m = modes.get(key)
                if isinstance(m, dict) and m.get("enabled"):
                    return m.get("chargingMode")
        except Exception:
            return None
        return None

    async def set_charge_mode(self, sn: str, mode: str) -> dict:
        """Set the charging mode via scheduler API.

        PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/<site>/<sn>/preference
        Body: { "mode": "MANUAL_CHARGING" | "SCHEDULED_CHARGING" | "GREEN_CHARGING" }
        """
        url = f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/{self._site}/{sn}/preference"
        headers = dict(self._h)
        headers.update(self._control_headers())
        payload = {"mode": str(mode)}
        return await self._json("PUT", url, json=payload, headers=headers)

    async def green_charging_settings(self, sn: str) -> list[dict[str, Any]]:
        """Return green charging settings for the charger.

        GET /service/evse_scheduler/api/v1/iqevc/charging-mode/GREEN_CHARGING/<site>/<sn>/settings
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"GREEN_CHARGING/{self._site}/{sn}/settings"
        )
        headers = dict(self._h)
        headers.update(self._control_headers())
        payload = await self._json("GET", url, headers=headers)
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def set_green_battery_setting(self, sn: str, *, enabled: bool) -> dict:
        """Toggle green charging battery support.

        PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/GREEN_CHARGING/<site>/<sn>/settings
        Body: {
          "chargerSettingList": [
            { "chargerSettingName": "USE_BATTERY_FOR_SELF_CONSUMPTION", "enabled": true }
          ]
        }
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"GREEN_CHARGING/{self._site}/{sn}/settings"
        )
        headers = dict(self._h)
        headers.update(self._control_headers())
        payload = {
            "chargerSettingList": [
                {
                    "chargerSettingName": GREEN_BATTERY_SETTING,
                    "enabled": bool(enabled),
                    "value": None,
                    "loader": False,
                }
            ]
        }
        return await self._json("PUT", url, json=payload, headers=headers)

    async def get_schedules(self, sn: str) -> dict:
        """Return scheduler config and slots for the charger.

        GET /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedules
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"SCHEDULED_CHARGING/{self._site}/{sn}/schedules"
        )
        headers = dict(self._h)
        headers.update(self._control_headers())
        payload = await self._json("GET", url, headers=headers)
        if not isinstance(payload, dict):
            return {"meta": None, "config": None, "slots": []}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {
            "meta": payload.get("meta"),
            "config": (data or {}).get("config"),
            "slots": (data or {}).get("slots") or [],
        }

    async def patch_schedules(
        self, sn: str, *, server_timestamp: str, slots: list[dict]
    ) -> dict:
        """Patch the scheduler slots for the charger.

        PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedules
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"SCHEDULED_CHARGING/{self._site}/{sn}/schedules"
        )
        headers = dict(self._h)
        headers.update(self._control_headers())
        payload = {
            "meta": {"serverTimeStamp": server_timestamp, "rowCount": len(slots)},
            "data": slots,
        }
        return await self._json("PATCH", url, json=payload, headers=headers)

    async def patch_schedule_states(
        self, sn: str, *, slot_states: dict[str, bool]
    ) -> dict:
        """Patch schedule slot enabled states for the charger.

        PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedules
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"SCHEDULED_CHARGING/{self._site}/{sn}/schedules"
        )
        headers = dict(self._h)
        headers.update(self._control_headers())
        payload = {
            str(slot_id): "ENABLED" if enabled else "DISABLED"
            for slot_id, enabled in slot_states.items()
        }
        return await self._json("PATCH", url, json=payload, headers=headers)

    async def patch_schedule(self, sn: str, slot_id: str, slot: dict) -> dict:
        """Patch a single schedule slot for the charger.

        PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedule/<slot_id>
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"SCHEDULED_CHARGING/{self._site}/{sn}/schedule/{slot_id}"
        )
        headers = dict(self._h)
        headers.update(self._control_headers())
        return await self._json("PATCH", url, json=slot, headers=headers)

    async def lifetime_energy(self) -> dict | None:
        """Return lifetime energy buckets for the configured site.

        GET /pv/systems/<site_id>/lifetime_energy
        """

        def _coerce(val):
            if isinstance(val, (int, float)):
                try:
                    return float(val)
                except Exception:  # noqa: BLE001
                    return None
            if isinstance(val, str):
                s = val.strip()
                if not s:
                    return None
                try:
                    return float(s)
                except Exception:  # noqa: BLE001
                    return None
            return None

        url = f"{BASE_URL}/pv/systems/{self._site}/lifetime_energy"
        data = await self._json("GET", url)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data.get("data")
        if not isinstance(data, dict):
            return None

        array_fields = {
            "production",
            "consumption",
            "solar_home",
            "solar_grid",
            "grid_home",
            "import",
            "export",
            "charge",
            "discharge",
            "solar_battery",
            "battery_home",
            "battery_grid",
            "grid_battery",
            "evse",
            "heatpump",
            "water_heater",
        }
        normalized: dict[str, object] = {}
        for key, value in data.items():
            if key in array_fields:
                if isinstance(value, list):
                    normalized[key] = [_coerce(v) for v in value]
                else:
                    normalized[key] = []
                continue
            if key in {"start_date", "last_report_date", "update_pending", "system_id"}:
                normalized[key] = value
        interval_minutes = _coerce(
            data.get("interval_minutes")
            or data.get("interval")
            or data.get("interval_min")
        )
        if interval_minutes is not None and interval_minutes > 0:
            normalized["interval_minutes"] = interval_minutes

        return normalized

    async def summary_v2(self) -> list[dict] | None:
        """Fetch charger summary v2 list.

        GET /service/evse_controller/api/v2/<site_id>/ev_chargers/summary?filter_retired=true
        Returns a list of charger objects with serialNumber and other properties.
        """
        url = f"{BASE_URL}/service/evse_controller/api/v2/{self._site}/ev_chargers/summary?filter_retired=true"
        data = await self._json("GET", url)
        try:
            return data.get("data") or []
        except Exception:
            return None

    async def session_history(
        self,
        sn: str,
        *,
        start_date: str,
        end_date: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> dict:
        """Fetch charging sessions for a charger between the provided dates.

        POST /service/enho_historical_events_ms/<site_id>/sessions/<sn>/history
        Dates must be formatted as DD-MM-YYYY in the site locale.
        """
        url = f"{BASE_URL}/service/enho_historical_events_ms/{self._site}/sessions/{sn}/history"
        payload = {
            "startDate": start_date,
            "endDate": end_date or start_date,
            "offset": int(offset),
            "limit": int(limit),
        }
        headers = dict(self._h)
        bearer = self._bearer() or self._eauth
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return await self._json("POST", url, json=payload, headers=headers)
