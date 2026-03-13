from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

import aiohttp
from yarl import URL

from .const import (
    AUTH_APP_SETTING,
    AUTH_RFID_SETTING,
    BASE_URL,
    DEFAULT_AUTH_TIMEOUT,
    ENTREZ_URL,
    GREEN_BATTERY_SETTING,
    LOGIN_URL,
    MFA_RESEND_URL,
    MFA_VALIDATE_URL,
    SITE_SEARCH_URL,
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


class SchedulerUnavailable(Exception):
    """Raised when the scheduler service is unavailable."""


class SessionHistoryUnavailable(Exception):
    """Raised when the session history service is unavailable."""


class SiteEnergyUnavailable(Exception):
    """Raised when the site energy service is unavailable."""


class EVSETimeseriesUnavailable(Exception):
    """Raised when the EVSE timeseries service is unavailable."""


class AuthSettingsUnavailable(Exception):
    """Raised when the charger auth settings service is unavailable."""


class InvalidPayloadError(aiohttp.ClientError):
    """Raised when an endpoint returns malformed or non-JSON payload data."""

    def __init__(
        self,
        summary: str,
        *,
        status: int | None = None,
        content_type: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        compact = " ".join(str(summary or "").split()).strip()
        if not compact:
            compact = "Invalid JSON response from Enphase endpoint"
        if len(compact) > 256:
            compact = f"{compact[:256]}…"
        self.summary = compact
        self.status = status
        self.content_type = content_type
        self.endpoint = endpoint
        super().__init__(self.summary)


def _is_optional_non_json_payload(err: InvalidPayloadError) -> bool:
    """Return True when an optional endpoint returned a non-JSON success page."""

    try:
        status = int(err.status or 0)
    except Exception:
        status = 0
    if status < 200 or status >= 300:
        return False
    content_type = str(err.content_type or "").lower()
    return "json" not in content_type


def _is_hems_invalid_site_error(err: aiohttp.ClientResponseError) -> bool:
    """Return True when HEMS reports the site is unsupported for HEMS endpoints."""

    try:
        if int(err.status or 0) != 550:
            return False
    except Exception:
        return False

    message = str(err.message or "").strip()
    if not message:
        return False
    try:
        payload = json.loads(message)
    except Exception:
        text = message.lower()
        return "invalid_site" in text or "not a valid hems site" in text
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if isinstance(error, dict):
        status = str(error.get("status") or "").strip().upper()
        code = error.get("code")
        message_text = str(error.get("message") or "").strip().lower()
        if status == "INVALID_SITE":
            return True
        if str(code).strip() == "900" and "valid hems site" in message_text:
            return True
    return False


@dataclass(slots=True)
class AuthTokens:
    """Container for Enlighten authentication state."""

    cookie: str
    session_id: str | None = None
    access_token: str | None = None
    token_expires_at: int | None = None
    raw_cookies: dict[str, str] | None = None


@dataclass(slots=True)
class SiteInfo:
    """Basic representation of an Enlighten site."""

    site_id: str
    name: str | None = None


@dataclass(slots=True)
class ChargerInfo:
    """Metadata about a charger discovered for a site."""

    serial: str
    name: str | None = None


_SYSTEM_DASHBOARD_DETAIL_QUERY_MAP: dict[str, str] = {
    "envoy": "envoys",
    "envoys": "envoys",
    "meter": "meters",
    "meters": "meters",
    "enpower": "enpowers",
    "enpowers": "enpowers",
    "encharge": "encharges",
    "encharges": "encharges",
    "modem": "modems",
    "modems": "modems",
    "microinverter": "inverters",
    "inverters": "inverters",
}


def _system_dashboard_query_type(type_key: object) -> str | None:
    """Normalize a dashboard query type to the observed endpoint value."""

    if type_key is None:
        return None
    try:
        text = str(type_key).strip().lower()
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None
    normalized = "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")
    if not normalized:
        return None
    return _SYSTEM_DASHBOARD_DETAIL_QUERY_MAP.get(normalized)


def _request_label(method: object, url: object) -> str:
    """Return a compact request label for debug logging."""

    try:
        method_text = str(method).strip().upper()
    except Exception:  # noqa: BLE001 - defensive casting
        method_text = "REQUEST"

    path = ""
    try:
        url_obj = url if isinstance(url, URL) else URL(str(url))
    except Exception:  # noqa: BLE001 - fallback to raw text
        try:
            raw = str(url).strip()
        except Exception:  # noqa: BLE001
            raw = ""
        if raw:
            return f"{method_text} {raw}"
        return method_text

    if url_obj.path:
        path = url_obj.path
    if url_obj.query_string:
        path = f"{path}?{url_obj.query_string}" if path else f"?{url_obj.query_string}"
    if path:
        return f"{method_text} {path}"
    return method_text


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


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without validation."""

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001 - defensive parsing
        return None
    return payload if isinstance(payload, dict) else None


def _jwt_user_id(token: str | None) -> str | None:
    """Extract user_id from a JWT payload when available."""

    if not token:
        return None
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    for key in ("user_id", "userId", "userid"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("user_id", "userId", "userid"):
            value = data.get(key)
            if value is not None:
                return str(value)
    return None


def _jwt_session_id(token: str | None) -> str | None:
    """Extract session_id from a JWT payload when available."""

    if not token:
        return None
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    for key in ("session_id", "sessionId", "session"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("session_id", "sessionId", "session"):
            value = data.get(key)
            if value is not None:
                return str(value)
    return None


def _extract_xsrf_token(cookies: dict[str, str] | None) -> str | None:
    """Return the XSRF token value from the cookie jar map."""

    if not cookies:
        return None
    for preferred in ("xsrf-token", "bp-xsrf-token"):
        for name, value in cookies.items():
            if name and name.lower() == preferred:
                try:
                    token = str(value).strip()
                except Exception:  # noqa: BLE001 - defensive parsing
                    continue
                if token.startswith('"') and token.endswith('"') and len(token) >= 2:
                    token = token[1:-1]
                if not token:
                    continue
                try:
                    return unquote(token)
                except Exception:  # noqa: BLE001 - defensive decoding
                    return token
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


def is_scheduler_unavailable_error(
    message: str | None,
    status: int | None = None,
    url: str | URL | None = None,
) -> bool:
    """Return True if the error payload indicates scheduler unavailability."""

    try:
        text = str(message or "").lower()
    except Exception:
        text = ""
    url_text = ""
    if url:
        try:
            url_text = str(url).lower()
        except Exception:
            url_text = ""

    scheduler_tokens = ("iqevc-scheduler", "scheduler ms", "evse_scheduler")
    status_tokens = (500, 502, 503, 504)
    if url_text and "/evse_scheduler/" in url_text and status in status_tokens:
        return True
    if any(token in text for token in scheduler_tokens):
        if (
            status in status_tokens
            or "service unavailable" in text
            or "refused" in text
        ):
            return True
        if "unavailable" in text:
            return True
    if "scheduler" in text and (
        "service unavailable" in text or "refused" in text or "unavailable" in text
    ):
        return True
    if "schedules/status" in text and "service unavailable" in text:
        return True
    return False


def is_session_history_unavailable_error(
    message: str | None,
    status: int | None = None,
    url: str | URL | None = None,
) -> bool:
    """Return True if the error payload indicates session history unavailability."""
    try:
        text = str(message or "").lower()
    except Exception:
        text = ""
    url_text = ""
    if url:
        try:
            url_text = str(url).lower()
        except Exception:
            url_text = ""
    if (
        url_text
        and "/enho_historical_events_ms/" in url_text
        and status
        in (
            500,
            502,
            503,
            504,
            550,
        )
    ):
        return True
    if "historical_events" in text and "service unavailable" in text:
        return True
    if "session history" in text and "unavailable" in text:
        return True
    return False


def is_site_energy_unavailable_error(
    message: str | None,
    status: int | None = None,
    url: str | URL | None = None,
) -> bool:
    """Return True if the error payload indicates site energy unavailability."""
    try:
        text = str(message or "").lower()
    except Exception:
        text = ""
    url_text = ""
    if url:
        try:
            url_text = str(url).lower()
        except Exception:
            url_text = ""
    if url_text and "/pv/systems/" in url_text and "lifetime_energy" in url_text:
        if status in (500, 502, 503, 504):
            return True
    if "lifetime_energy" in text and "service unavailable" in text:
        return True
    return False


def is_evse_timeseries_unavailable_error(
    message: str | None,
    status: int | None = None,
    url: str | URL | None = None,
) -> bool:
    """Return True if the error payload indicates EVSE timeseries unavailability."""

    try:
        text = str(message or "").lower()
    except Exception:
        text = ""
    url_text = ""
    if url:
        try:
            url_text = str(url).lower()
        except Exception:
            url_text = ""
    if (
        url_text
        and "/service/timeseries/evse/timeseries/" in url_text
        and status in (500, 502, 503, 504)
    ):
        return True
    if "evse" in text and "timeseries" in text and "unavailable" in text:
        return True
    if "daily_energy" in text and "service unavailable" in text:
        return True
    if "lifetime_energy" in text and "service unavailable" in text:
        return True
    return False


def is_auth_settings_unavailable_error(
    message: str | None,
    status: int | None = None,
    url: str | URL | None = None,
) -> bool:
    """Return True if the error payload indicates auth settings unavailability."""
    try:
        text = str(message or "").lower()
    except Exception:
        text = ""
    url_text = ""
    if url:
        try:
            url_text = str(url).lower()
        except Exception:
            url_text = ""
    if (
        url_text
        and "/evse_controller/api/v1/" in url_text
        and "ev_charger_config" in url_text
    ):
        if status in (500, 502, 503, 504):
            return True
    if "ev_charger_config" in text and "service unavailable" in text:
        return True
    return False


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

    async with asyncio.timeout(timeout):
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

    async with asyncio.timeout(timeout):
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
    seen: dict[str, SiteInfo] = {}

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
        name = (
            item.get("name")
            or item.get("site_name")
            or item.get("siteName")
            or item.get("title")
            or item.get("displayName")
            or item.get("display_name")
        )
        if site_id is None:
            continue
        site_id_str = str(site_id)
        existing = seen.get(site_id_str)
        if existing:
            if not existing.name and name:
                existing.name = str(name)
            continue
        info = SiteInfo(site_id=site_id_str, name=str(name) if name else None)
        seen[site_id_str] = info
        sites.append(info)
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
    for url in (SITE_SEARCH_URL,):
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


async def async_fetch_devices_inventory(
    session: aiohttp.ClientSession,
    site_id: str,
    tokens: AuthTokens,
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> dict[str, object] | None:
    """Fetch a site devices inventory payload for config-flow category selection."""

    if not site_id:
        return {}

    client = EnphaseEVClient(
        session,
        site_id,
        tokens.access_token,
        tokens.cookie,
        timeout=timeout,
    )
    try:
        payload = await client.devices_inventory()
    except Exception as err:  # noqa: BLE001 - best-effort for flow UX
        _LOGGER.debug("Failed to fetch devices inventory for site %s: %s", site_id, err)
        return None
    if isinstance(payload, dict):
        return payload
    return None


async def async_fetch_inverters_inventory(
    session: aiohttp.ClientSession,
    site_id: str,
    tokens: AuthTokens,
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> dict[str, object] | None:
    """Fetch legacy inverter inventory for config-flow microinverter discovery."""

    if not site_id:
        return {}

    client = EnphaseEVClient(
        session,
        site_id,
        tokens.access_token,
        tokens.cookie,
        timeout=timeout,
    )

    def _payload_inverters(payload: dict[str, object]) -> tuple[list[dict[str, object]], str]:
        inverters = payload.get("inverters")
        if isinstance(inverters, list):
            return ([item for item in inverters if isinstance(item, dict)], "root")
        result = payload.get("result")
        if isinstance(result, dict):
            inverters = result.get("inverters")
            if isinstance(inverters, list):
                return ([item for item in inverters if isinstance(item, dict)], "result")
        return ([], "")

    def _payload_total(payload: dict[str, object], default: int) -> int:
        raw_total = payload.get("total")
        try:
            total = int(raw_total)
        except (TypeError, ValueError):
            return default
        return total if total >= 0 else default

    async def _fetch_page(offset: int) -> dict[str, object] | None:
        try:
            payload = await client.inverters_inventory(limit=1000, offset=offset, search="")
        except TypeError:
            if offset != 0:
                return None
            try:
                payload = await client.inverters_inventory()
            except Exception as err:  # noqa: BLE001 - best-effort for flow UX
                _LOGGER.debug(
                    "Failed to fetch inverter inventory for site %s: %s", site_id, err
                )
                return None
        except Exception as err:  # noqa: BLE001 - best-effort for flow UX
            _LOGGER.debug(
                "Failed to fetch inverter inventory for site %s: %s", site_id, err
            )
            return None
        if isinstance(payload, dict):
            return payload
        return None

    try:
        payload = await _fetch_page(0)
        if payload is None:
            return None

        inverters, storage_key = _payload_inverters(payload)
        total_expected = _payload_total(payload, len(inverters))
        if storage_key and total_expected > len(inverters):
            merged = list(inverters)
            next_offset = len(merged)
            while next_offset < total_expected:
                next_payload = await _fetch_page(next_offset)
                if next_payload is None:
                    break
                next_inverters, _ = _payload_inverters(next_payload)
                if not next_inverters:
                    break
                merged.extend(next_inverters)
                total_expected = max(
                    total_expected,
                    _payload_total(next_payload, total_expected),
                )
                next_offset += len(next_inverters)
            payload = dict(payload)
            if storage_key == "root":
                payload["inverters"] = merged
            else:
                result = payload.get("result")
                result_dict = dict(result) if isinstance(result, dict) else {}
                result_dict["inverters"] = merged
                payload["result"] = result_dict
        return payload
    except Exception as err:  # noqa: BLE001 - best-effort for flow UX
        _LOGGER.debug("Failed to assemble inverter inventory for site %s: %s", site_id, err)
        return None


async def async_fetch_hems_devices(
    session: aiohttp.ClientSession,
    site_id: str,
    tokens: AuthTokens,
    *,
    refresh_data: bool = False,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> dict[str, object] | None:
    """Fetch dedicated HEMS device inventory for config-flow discovery."""

    if not site_id:
        return {}

    client = EnphaseEVClient(
        session,
        site_id,
        tokens.access_token,
        tokens.cookie,
        timeout=timeout,
    )
    try:
        payload = await client.hems_devices(refresh_data=refresh_data)
    except Exception as err:  # noqa: BLE001 - best-effort for flow UX
        _LOGGER.debug("Failed to fetch HEMS devices for site %s: %s", site_id, err)
        return None
    if isinstance(payload, dict):
        return payload
    return None


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
        self._bp_xsrf_token: str | None = None
        self._cookie = cookie or ""
        self._eauth = eauth or None
        self._hems_site_supported: bool | None = None
        self._reauth_cb: Callable[[], Awaitable[bool]] | None = reauth_callback
        self._last_unauthorized_request: str | None = None
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

    @property
    def last_unauthorized_request(self) -> str | None:
        """Return the most recent request that received a 401 response."""

        return self._last_unauthorized_request

    def update_credentials(
        self,
        *,
        eauth: str | None = None,
        cookie: str | None = None,
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

        # If XSRF cookies are present, add matching CSRF header some endpoints expect.
        try:
            xsrf = self._xsrf_token()
            if xsrf:
                self._h["X-CSRF-Token"] = xsrf
            else:
                self._h.pop("X-CSRF-Token", None)
        except Exception:  # noqa: BLE001 - defensive: header should never break setup
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

    def scheduler_bearer(self) -> str | None:
        """Public bearer accessor for scheduler feature checks."""

        return self._bearer()

    def has_scheduler_bearer(self) -> bool:
        """Return True when scheduler bearer auth can be derived."""

        return bool(self.scheduler_bearer())

    @property
    def hems_site_supported(self) -> bool | None:
        """Return whether HEMS has been positively identified for this site."""

        return self._hems_site_supported

    def base_header_names(self) -> list[str]:
        """Return base header names without exposing values."""

        return sorted(self._h.keys())

    def _history_bearer(self) -> str | None:
        """Return the preferred bearer token for session history calls."""

        return self._eauth or self._bearer()

    def _session_history_username(self) -> str | None:
        """Return the user id expected by the session history service."""

        return _jwt_user_id(self._history_bearer())

    def _session_history_headers(
        self, request_id: str | None, username: str | None
    ) -> dict[str, str]:
        """Return headers for session history endpoints."""

        headers = dict(self._h)
        bearer = self._history_bearer()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        session_id = _jwt_session_id(bearer)
        if session_id:
            headers["e-auth-token"] = session_id
        else:
            headers.pop("e-auth-token", None)
        if request_id:
            headers["requestid"] = request_id
        if username:
            headers["username"] = username
        return headers

    def _evse_timeseries_headers(
        self,
        request_id: str | None,
        username: str | None,
    ) -> dict[str, str]:
        """Return headers for EVSE timeseries endpoints."""

        return self._session_history_headers(request_id, username)

    def _control_headers(self) -> dict[str, str]:
        """Return Authorization header overrides for control-plane requests."""

        bearer = self._bearer() or self._eauth
        if bearer:
            return {"Authorization": f"Bearer {bearer}"}
        return {}

    def control_headers(self) -> dict[str, str]:
        """Public control header helper for read-only diagnostics checks."""

        return self._control_headers()

    def _system_dashboard_headers(self) -> dict[str, str]:
        """Return headers for system dashboard read endpoints."""

        headers = dict(self._h)
        headers.update(self._control_headers())
        return headers

    @staticmethod
    def _system_dashboard_is_optional_error(err: Exception) -> bool:
        """Return True when a dashboard route should fall back or soft-fail."""

        if isinstance(err, Unauthorized):
            return True
        if isinstance(err, InvalidPayloadError):
            return _is_optional_non_json_payload(err)
        if isinstance(err, aiohttp.ClientResponseError):
            return err.status in (401, 403, 404)
        return False

    async def _system_dashboard_get(
        self,
        modern_url: str,
        legacy_url: str,
    ) -> dict | None:
        """Fetch a system dashboard payload from the modern route with fallback."""

        headers = self._system_dashboard_headers()
        for url in (modern_url, legacy_url):
            try:
                data = await self._json("GET", url, headers=headers)
            except Exception as err:  # noqa: BLE001
                if self._system_dashboard_is_optional_error(err):
                    continue
                raise
            return data if isinstance(data, dict) else None

        return None

    def _hems_headers(self) -> dict[str, str]:
        """Return headers for HEMS read endpoints."""

        headers = dict(self._h)
        headers.update(self._control_headers())
        return headers

    def _battery_config_user_id(self) -> str | None:
        """Return the user id for BatteryConfig requests when available."""

        _token, user_id = self._battery_config_auth_context()
        return user_id

    def _battery_config_auth_context(self) -> tuple[str | None, str | None]:
        """Return preferred BatteryConfig auth token and resolved user id.

        Preference order follows captured browser behavior:
        1) manager bearer cookie token when it contains a usable user id
        2) access-token fallback when it contains a usable user id
        3) first available token when user id cannot be resolved
        """

        candidates: list[str] = []
        bearer = self._bearer()
        if bearer:
            candidates.append(bearer)
        if self._eauth and self._eauth not in candidates:
            candidates.append(self._eauth)

        fallback_token: str | None = None
        for token in candidates:
            if fallback_token is None:
                fallback_token = token
            user_id = _jwt_user_id(token)
            if user_id:
                return token, user_id
        return fallback_token, None

    def _battery_config_auth_token(self) -> str | None:
        """Return bearer token to use for BatteryConfig requests."""

        token, _user_id = self._battery_config_auth_context()
        return token

    def _battery_config_cookie(self, *, include_xsrf: bool = False) -> str | None:
        """Return a normalized BatteryConfig cookie header value.

        BatteryConfig write endpoints reject stale duplicate ``BP-XSRF-Token``
        cookies, so always strip any existing token from the base cookie string
        before optionally appending the current one.
        """

        try:
            cookie_str = str(self._cookie) if self._cookie else ""
        except Exception:  # noqa: BLE001 - defensive parsing
            cookie_str = ""

        parts = [
            part.strip()
            for part in cookie_str.split(";")
            if part.strip() and not part.strip().startswith("BP-XSRF-Token=")
        ]
        if include_xsrf:
            xsrf = self._xsrf_token()
            if xsrf:
                parts.append(f"BP-XSRF-Token={xsrf}")
        if not parts:
            return None
        return "; ".join(parts)

    def _battery_config_headers(
        self,
        *,
        include_xsrf: bool = False,
    ) -> dict[str, str]:
        """Return headers for BatteryConfig read/write calls."""

        headers = dict(self._h)
        token, user_id = self._battery_config_auth_context()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if user_id:
            headers["Username"] = user_id
        headers["Origin"] = "https://battery-profile-ui.enphaseenergy.com"
        headers["Referer"] = "https://battery-profile-ui.enphaseenergy.com/"
        cookie = self._battery_config_cookie(include_xsrf=include_xsrf)
        if cookie:
            headers["Cookie"] = cookie
        else:
            headers.pop("Cookie", None)
        if include_xsrf:
            xsrf = self._xsrf_token()
            if xsrf:
                headers["X-XSRF-Token"] = xsrf
        return headers

    def _battery_config_params(
        self,
        *,
        include_source: bool = False,
        locale: str | None = None,
    ) -> dict[str, str]:
        """Return query parameters for BatteryConfig calls."""

        params: dict[str, str] = {}
        user_id = self._battery_config_user_id()
        if user_id:
            params["userId"] = user_id
        if include_source:
            params["source"] = "enho"
        if locale:
            params["locale"] = locale
        return params

    def _xsrf_token(self) -> str | None:
        """Return the XSRF token value.

        Checks the dynamically acquired BP-XSRF-Token first, then falls back
        to extracting from the cookie string.
        """

        if self._bp_xsrf_token:
            return self._bp_xsrf_token

        try:
            parts = [p.strip() for p in (self._cookie or "").split(";")]
        except Exception:  # noqa: BLE001 - defensive parsing
            return None
        for key in ("XSRF-TOKEN", "BP-XSRF-Token"):
            for part in parts:
                if part.startswith(f"{key}="):
                    token = part.split("=", 1)[1].strip()
                    if token.startswith('"') and token.endswith('"') and len(token) >= 2:
                        token = token[1:-1]
                    if not token:
                        continue
                    try:
                        return unquote(token)
                    except Exception:  # noqa: BLE001 - defensive decoding
                        return token
        return None

    async def _acquire_xsrf_token(self) -> str | None:
        """Acquire a BP-XSRF-Token by POSTing to the schedules isValid endpoint.

        The Enphase BatteryConfig API requires an XSRF token for write operations.
        This token is obtained from the ``Set-Cookie`` header in the response to
        a POST to ``/schedules/isValid``.
        """

        url = (
            f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
            f"{self._site}/schedules/isValid"
        )
        token, user_id = self._battery_config_auth_context()
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://battery-profile-ui.enphaseenergy.com",
            "Referer": "https://battery-profile-ui.enphaseenergy.com/",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self._eauth:
            headers["e-auth-token"] = self._eauth
        if user_id:
            headers["Username"] = user_id
        cookie = self._battery_config_cookie()
        if cookie:
            headers["Cookie"] = cookie
        payload = {
            "scheduleType": "cfg",
            "forceScheduleOpted": True,
        }

        try:
            async with asyncio.timeout(self._timeout):
                async with self._s.request(
                    "POST", url, json=payload, headers=headers
                ) as r:
                    # Extract BP-XSRF-Token from Set-Cookie header
                    set_cookie = r.headers.get("Set-Cookie", "")
                    match = re.search(r"BP-XSRF-Token=([^;]+)", set_cookie)
                    if match:
                        self._bp_xsrf_token = match.group(1)
                        _LOGGER.debug("Acquired BP-XSRF-Token from isValid endpoint")
                        return self._bp_xsrf_token

                    # Fallback: check all Set-Cookie headers
                    for value in r.headers.getall("Set-Cookie", []):
                        match = re.search(r"BP-XSRF-Token=([^;]+)", value)
                        if match:
                            self._bp_xsrf_token = match.group(1)
                            _LOGGER.debug(
                                "Acquired BP-XSRF-Token from Set-Cookie header"
                            )
                            return self._bp_xsrf_token

                    _LOGGER.warning(
                        "isValid endpoint did not return BP-XSRF-Token cookie"
                    )
                    return None
        except Exception:  # noqa: BLE001 - XSRF acquisition is best-effort
            _LOGGER.warning("Failed to acquire XSRF token", exc_info=True)
            return None

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
        ``headers`` may also be a zero-argument callable so retries can rebuild
        auth-sensitive headers after a successful reauthentication callback.
        """
        extra_headers = kwargs.pop("headers", None)
        attempt = 0
        request_label = _request_label(method, url)
        while True:
            base_headers = dict(self._h)
            if callable(extra_headers):
                attempt_headers = extra_headers()
            else:
                attempt_headers = extra_headers
            if isinstance(attempt_headers, dict):
                base_headers.update(attempt_headers)

            async with asyncio.timeout(self._timeout):
                async with self._s.request(
                    method, url, headers=base_headers, **kwargs
                ) as r:
                    if r.status == 401:
                        self._last_unauthorized_request = request_label
                        if self._reauth_cb and attempt == 0:
                            _LOGGER.debug(
                                "Received 401 for %s; attempting stored-credential refresh",
                                request_label,
                            )
                            attempt += 1
                            reauth_ok = await self._reauth_cb()
                            if reauth_ok:
                                _LOGGER.debug(
                                    "Stored-credential refresh succeeded for %s; retrying request",
                                    request_label,
                                )
                                continue
                            _LOGGER.debug(
                                "Stored-credential refresh failed for %s",
                                request_label,
                            )
                        else:
                            _LOGGER.debug(
                                "Received 401 for %s with no stored-credential refresh available",
                                request_label,
                            )
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
                    try:
                        return await r.json()
                    except (aiohttp.ContentTypeError, ValueError) as err:
                        status = int(getattr(r, "status", 0) or 0)
                        content_type = ""
                        try:
                            content_type = str(r.headers.get("Content-Type", "")).strip()
                        except Exception:  # noqa: BLE001 - defensive header parsing
                            content_type = ""
                        endpoint = ""
                        try:
                            endpoint = URL(url).path
                        except Exception:  # noqa: BLE001 - defensive URL parsing
                            endpoint = ""
                        detail_parts: list[str] = [f"status={status}"]
                        if content_type:
                            detail_parts.append(f"content_type={content_type}")
                        if endpoint:
                            detail_parts.append(f"endpoint={endpoint}")
                        detail_parts.append(f"decode_error={err.__class__.__name__}")
                        summary = f"Invalid JSON response ({', '.join(detail_parts)})"
                        raise InvalidPayloadError(
                            summary,
                            status=status or None,
                            content_type=content_type or None,
                            endpoint=endpoint or None,
                        ) from err

    async def status(self) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/status"
        data = await self._json("GET", url)

        # If response is { data: { chargers: [...] } }, map to evChargerData
        try:
            inner = data.get("data") if isinstance(data, dict) else None
            chargers = inner.get("chargers") if isinstance(inner, dict) else None
            if isinstance(chargers, list) and chargers:
                out = []
                for c in chargers:
                    conn = (c.get("connectors") or [{}])[0]
                    raw_session = c.get("session_d")
                    sess = dict(raw_session) if isinstance(raw_session, dict) else {}
                    connectors = c.get("connectors")
                    if not connectors:
                        connectors = [conn] if conn else []
                    # Derive start_time in seconds (strt_chrg appears in ms)
                    start_raw = sess.get("start_time")
                    from_strt_chrg = False
                    if start_raw is None:
                        start_raw = sess.get("strt_chrg")
                        from_strt_chrg = start_raw is not None
                    start_sec: int | None = None
                    if isinstance(start_raw, (int, float)):
                        try:
                            start_val = int(start_raw)
                            if from_strt_chrg:
                                start_val = int(start_val / 1000)
                            elif start_val > 10**12:
                                start_val = start_val // 1000
                            start_sec = start_val
                        except Exception:
                            start_sec = None
                    elif isinstance(start_raw, str):
                        text = start_raw.strip()
                        if text.isdigit():
                            try:
                                start_val = int(text)
                                if from_strt_chrg:
                                    start_val = int(start_val / 1000)
                                elif start_val > 10**12:
                                    start_val = start_val // 1000
                                start_sec = start_val
                            except Exception:
                                start_sec = None
                    if start_sec is not None and sess.get("start_time") is None:
                        sess["start_time"] = start_sec
                    sch_raw = c.get("sch_d")
                    sch = dict(sch_raw) if isinstance(sch_raw, dict) else {}
                    smart_ev = c.get("smartEV")
                    if not isinstance(smart_ev, dict):
                        smart_ev = {}
                    out.append(
                        {
                            "sn": c.get("sn"),
                            "name": c.get("name"),
                            "displayName": c.get("displayName"),
                            "connected": bool(c.get("connected")),
                            "pluggedIn": bool(
                                c.get("pluggedIn") or conn.get("pluggedIn")
                            ),
                            "charging": bool(c.get("charging")),
                            "faulted": bool(c.get("faulted")),
                            "commissioned": c.get("commissioned"),
                            "mode": c.get("mode"),
                            "offGrid": c.get("offGrid"),
                            "offlineAt": c.get("offlineAt"),
                            "evManufacturerName": c.get("evManufacturerName"),
                            "isEVDetailsSet": c.get("isEVDetailsSet"),
                            "smartEV": smart_ev,
                            "sch_d": sch,
                            "chargingLevel": c.get("chargingLevel"),
                            "connectorStatusType": conn.get("connectorStatusType"),
                            "connectors": connectors,
                            "session_d": sess,
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
        try:
            data = await self._json("GET", url, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise
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
        try:
            return await self._json("PUT", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise

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
        try:
            payload = await self._json("GET", url, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise
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
        try:
            return await self._json("PUT", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise

    async def storm_guard_alert(self) -> dict:
        """Return Storm Guard alert status for the site.

        GET /service/batteryConfig/api/v1/stormGuard/<site_id>/stormAlert
        """
        url = f"{BASE_URL}/service/batteryConfig/api/v1/stormGuard/{self._site}/stormAlert"
        headers = self._battery_config_headers()
        return await self._json("GET", url, headers=headers)

    async def opt_out_storm_alert(self, *, alert_id: str, name: str) -> dict:
        """Opt out of a specific Storm Guard alert.

        PUT /service/batteryConfig/api/v1/stormGuard/<site_id>/stormAlert
        Body: {
          "stormAlerts": [
            {"id": "<alert_id>", "name": "<alert_name>", "status": "opted-out"}
          ]
        }
        """
        url = f"{BASE_URL}/service/batteryConfig/api/v1/stormGuard/{self._site}/stormAlert"
        headers = self._battery_config_headers(include_xsrf=True)
        payload = {
            "stormAlerts": [
                {
                    "id": str(alert_id),
                    "name": str(name),
                    "status": "opted-out",
                }
            ]
        }
        return await self._json("PUT", url, json=payload, headers=headers)

    async def storm_guard_profile(self, *, locale: str | None = None) -> dict:
        """Return Storm Guard state and EVSE settings for the site.

        GET /service/batteryConfig/api/v1/profile/<site_id>?source=enho&userId=<user_id>&locale=<locale>
        """
        return await self.battery_profile_details(locale=locale)

    async def battery_site_settings(self) -> dict:
        """Return BatteryConfig site settings and feature flags."""

        url = f"{BASE_URL}/service/batteryConfig/api/v1/siteSettings/{self._site}"
        params = self._battery_config_params()
        headers = self._battery_config_headers()
        return await self._json("GET", url, headers=headers, params=params)

    async def battery_profile_details(self, *, locale: str | None = None) -> dict:
        """Return BatteryConfig profile details for system + EVSE settings."""

        url = f"{BASE_URL}/service/batteryConfig/api/v1/profile/{self._site}"
        params = self._battery_config_params(include_source=True, locale=locale)
        headers = self._battery_config_headers()
        return await self._json("GET", url, headers=headers, params=params)

    async def battery_settings_details(self) -> dict:
        """Return BatteryConfig battery details for charge-grid and shutdown controls."""

        url = f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{self._site}"
        params = self._battery_config_params(include_source=True)
        headers = self._battery_config_headers()
        return await self._json("GET", url, headers=headers, params=params)

    async def set_battery_settings(self, payload: dict[str, Any]) -> dict:
        """Update BatteryConfig battery detail settings using a partial payload."""

        await self._acquire_xsrf_token()

        try:
            url = f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{self._site}"
            params = self._battery_config_params(include_source=True)
            headers = self._battery_config_headers(include_xsrf=True)
            body = payload if isinstance(payload, dict) else {}
            return await self._json(
                "PUT", url, json=body, headers=headers, params=params
            )
        finally:
            self._bp_xsrf_token = None

    async def set_battery_profile(
        self,
        *,
        profile: str,
        battery_backup_percentage: int,
        operation_mode_sub_type: str | None = None,
        devices: list[dict[str, Any]] | None = None,
    ) -> dict:
        """Update the site battery profile and reserve percentage."""

        await self._acquire_xsrf_token()

        try:
            url = f"{BASE_URL}/service/batteryConfig/api/v1/profile/{self._site}"
            params = self._battery_config_params(include_source=True)
            headers = self._battery_config_headers(include_xsrf=True)
            payload: dict[str, Any] = {
                "profile": str(profile),
                "batteryBackupPercentage": int(battery_backup_percentage),
            }
            if operation_mode_sub_type:
                payload["operationModeSubType"] = str(operation_mode_sub_type)
            if devices:
                payload["devices"] = [
                    item for item in devices if isinstance(item, dict)
                ]
            return await self._json(
                "PUT", url, json=payload, headers=headers, params=params
            )
        finally:
            self._bp_xsrf_token = None

    async def cancel_battery_profile_update(self) -> dict:
        """Cancel a pending site battery profile change."""

        await self._acquire_xsrf_token()

        try:
            url = f"{BASE_URL}/service/batteryConfig/api/v1/cancel/profile/{self._site}"
            params = self._battery_config_params(include_source=True)
            headers = self._battery_config_headers(include_xsrf=True)
            return await self._json("PUT", url, json={}, headers=headers, params=params)
        finally:
            self._bp_xsrf_token = None

    async def set_storm_guard(self, *, enabled: bool, evse_enabled: bool) -> dict:
        """Toggle Storm Guard and the EVSE charge-to-100% option.

        PUT /service/batteryConfig/api/v1/stormGuard/toggle/<site_id>?userId=<user_id>
        """
        await self._acquire_xsrf_token()

        try:
            url = f"{BASE_URL}/service/batteryConfig/api/v1/stormGuard/toggle/{self._site}"
            params = self._battery_config_params(include_source=True)
            headers = self._battery_config_headers(include_xsrf=True)
            payload = {
                "stormGuardState": "enabled" if enabled else "disabled",
                "evseStormEnabled": bool(evse_enabled),
            }
            return await self._json(
                "PUT", url, json=payload, headers=headers, params=params
            )
        finally:
            self._bp_xsrf_token = None

    # ------------------------------------------------------------------
    # Battery schedule CRUD (newer /battery/sites/{id}/schedules API)
    # ------------------------------------------------------------------

    async def battery_schedules(self) -> dict:
        """Return all battery schedules for the site.

        GET /service/batteryConfig/api/v1/battery/sites/{site_id}/schedules

        Response contains ``cfg``, ``dtg``, and ``rbd`` schedule families,
        each with a ``details`` list of individual schedules.
        """

        url = (
            f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
            f"{self._site}/schedules"
        )
        headers = self._battery_config_headers()
        return await self._json("GET", url, headers=headers)

    async def create_battery_schedule(
        self,
        *,
        schedule_type: str,
        start_time: str,
        end_time: str,
        limit: int,
        days: list[int],
        timezone: str = "UTC",
    ) -> dict:
        """Create a new battery schedule.

        POST /service/batteryConfig/api/v1/battery/sites/{site_id}/schedules

        Parameters:
            schedule_type: ``CFG`` (charge from grid), ``DTG`` (discharge to grid),
                           or ``RBD`` (restrict battery discharge).
            start_time: ``HH:MM`` format.
            end_time: ``HH:MM`` format.
            limit: Target SoC percentage (0-100).
            days: List of weekday numbers (1=Mon … 7=Sun).
            timezone: IANA timezone string.
        """

        await self._acquire_xsrf_token()

        try:
            url = (
                f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
                f"{self._site}/schedules"
            )
            headers = self._battery_config_headers(include_xsrf=True)
            headers["Content-Type"] = "application/json"
            payload = {
                "timezone": timezone,
                "startTime": start_time[:5],
                "endTime": end_time[:5],
                "limit": int(limit),
                "scheduleType": str(schedule_type).upper(),
                "days": [int(d) for d in days],
            }
            return await self._json("POST", url, json=payload, headers=headers)
        finally:
            self._bp_xsrf_token = None

    async def delete_battery_schedule(self, schedule_id: str | int) -> dict:
        """Delete a battery schedule by ID.

        POST /service/batteryConfig/api/v1/battery/sites/{site_id}/schedules/{id}/delete
        """

        await self._acquire_xsrf_token()

        try:
            url = (
                f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
                f"{self._site}/schedules/{schedule_id}/delete"
            )
            headers = self._battery_config_headers(include_xsrf=True)
            headers["Content-Type"] = "application/json"
            return await self._json("POST", url, json={}, headers=headers)
        finally:
            self._bp_xsrf_token = None

    async def validate_battery_schedule(
        self, schedule_type: str = "cfg"
    ) -> dict:
        """Validate a battery schedule configuration.

        POST /service/batteryConfig/api/v1/battery/sites/{site_id}/schedules/isValid

        Also useful as a side-effect to acquire a fresh XSRF token.
        """

        url = (
            f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
            f"{self._site}/schedules/isValid"
        )
        headers = self._battery_config_headers()
        headers["Content-Type"] = "application/json"
        payload = {
            "scheduleType": schedule_type,
            "forceScheduleOpted": True,
        }
        return await self._json("POST", url, json=payload, headers=headers)

    async def charger_auth_settings(self, sn: str) -> list[dict[str, Any]]:
        """Return authentication settings for the charger.

        POST /service/evse_controller/api/v1/<site>/<sn>/ev_charger_config
        Body: [{ "key": "rfidSessionAuthentication" }, { "key": "sessionAuthentication" }]
        """
        url = (
            f"{BASE_URL}/service/evse_controller/api/v1/{self._site}/ev_chargers/"
            f"{sn}/ev_charger_config"
        )
        headers = dict(self._h)
        headers.update(self._control_headers())
        payload = [
            {"key": AUTH_RFID_SETTING},
            {"key": AUTH_APP_SETTING},
        ]
        try:
            response = await self._json("POST", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_auth_settings_unavailable_error(err.message, err.status, url):
                raise AuthSettingsUnavailable(str(err)) from err
            raise
        if not isinstance(response, dict):
            return []
        data = response.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def set_app_authentication(self, sn: str, *, enabled: bool) -> dict:
        """Enable or disable session authentication via app.

        PUT /service/evse_controller/api/v1/<site>/<sn>/ev_charger_config
        Body: [{ "key": "sessionAuthentication", "value": "enabled" | "disabled" }]
        """
        url = (
            f"{BASE_URL}/service/evse_controller/api/v1/{self._site}/ev_chargers/"
            f"{sn}/ev_charger_config"
        )
        headers = dict(self._h)
        headers.update(self._control_headers())
        payload = [
            {
                "key": AUTH_APP_SETTING,
                "value": "enabled" if enabled else "disabled",
            }
        ]
        try:
            return await self._json("PUT", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_auth_settings_unavailable_error(err.message, err.status, url):
                raise AuthSettingsUnavailable(str(err)) from err
            raise

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
        try:
            payload = await self._json("GET", url, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise
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
        try:
            return await self._json("PATCH", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise

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
        try:
            return await self._json("PATCH", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise

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
        try:
            return await self._json("PATCH", url, json=slot, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise

    async def lifetime_energy(self) -> dict | None:
        """Return lifetime energy buckets for the configured site.

        GET /pv/systems/<site_id>/lifetime_energy
        """
        url = f"{BASE_URL}/pv/systems/{self._site}/lifetime_energy"
        try:
            data = await self._json("GET", url)
        except aiohttp.ClientResponseError as err:
            if is_site_energy_unavailable_error(err.message, err.status, url):
                raise SiteEnergyUnavailable(str(err)) from err
            raise
        return self._normalize_lifetime_energy_payload(data)

    @classmethod
    def _normalize_latest_power_payload(cls, payload: object) -> dict[str, object] | None:
        """Normalize app-api latest power payloads into a common shape."""

        data = payload
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data.get("data")
        if not isinstance(data, dict):
            return None

        latest = data.get("latest_power")
        if not isinstance(latest, dict):
            latest = data

        value = cls._coerce_non_boolean_number(latest.get("value"))
        if value is None:
            return None

        normalized: dict[str, object] = {"value": value}

        units = latest.get("units")
        if units is not None:
            try:
                units_text = str(units).strip()
            except Exception:  # noqa: BLE001
                units_text = ""
            if units_text:
                normalized["units"] = units_text

        precision = cls._coerce_non_boolean_number(latest.get("precision"))
        if precision is not None:
            try:
                precision_int = int(precision)
            except Exception:  # noqa: BLE001
                precision_int = None
            if precision_int is not None:
                normalized["precision"] = precision_int

        sample_time = latest.get("time")
        if sample_time is not None:
            sample_time_val = cls._coerce_non_boolean_number(sample_time)
            if sample_time_val is not None:
                if sample_time_val > 10**12:
                    sample_time_val /= 1000.0
                try:
                    sample_time_int = int(sample_time_val)
                except Exception:  # noqa: BLE001
                    sample_time_int = None
                if sample_time_int is not None:
                    normalized["time"] = sample_time_int

        return normalized

    async def latest_power(self) -> dict[str, object] | None:
        """Return the latest site power sample for the configured site.

        GET /app-api/<site_id>/get_latest_power
        """

        url = f"{BASE_URL}/app-api/{self._site}/get_latest_power"
        data = await self._json("GET", url)
        return self._normalize_latest_power_payload(data)

    @staticmethod
    def _normalize_evse_timeseries_serial(value: object) -> str | None:
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @staticmethod
    def _parse_evse_timeseries_date_key(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, (int, float)):
            try:
                ts_val = float(value)
                if ts_val > 10**12:
                    ts_val /= 1000.0
                return datetime.fromtimestamp(ts_val, tz=timezone.utc).date().isoformat()
            except Exception:  # noqa: BLE001
                return None
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if len(cleaned) >= 10:
            try:
                return datetime.fromisoformat(
                    cleaned.replace("Z", "+00:00")
                ).date().isoformat()
            except Exception:  # noqa: BLE001
                pass
            try:
                datetime.strptime(cleaned[:10], "%Y-%m-%d")
                return cleaned[:10]
            except Exception:  # noqa: BLE001
                pass
        return None

    @classmethod
    def _coerce_evse_timeseries_energy(
        cls,
        value: object,
        *,
        key_hint: str | None = None,
        unit_hint: object | None = None,
    ) -> float | None:
        numeric = cls._coerce_lifetime_energy_value(value)
        if numeric is None:
            return None
        try:
            unit_text = str(unit_hint).strip().lower() if unit_hint is not None else ""
        except Exception:  # noqa: BLE001
            unit_text = ""
        hint = (key_hint or "").lower()
        if "wh" in hint and "kwh" not in hint:
            return round(numeric / 1000.0, 6)
        if unit_text in {"wh", "watt_hour", "watt-hours", "watt_hours"}:
            return round(numeric / 1000.0, 6)
        return round(numeric, 6)

    @classmethod
    def _normalize_evse_timeseries_metadata(cls, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            return {}
        interval = cls._coerce_lifetime_energy_value(
            payload.get("interval_minutes")
            or payload.get("interval")
            or payload.get("interval_min")
            or payload.get("intervalMinutes")
        )
        metadata: dict[str, object] = {}
        if interval is not None and interval > 0:
            metadata["interval_minutes"] = interval
        last_report = (
            payload.get("last_report_date")
            or payload.get("lastReportDate")
            or payload.get("last_reported_at")
            or payload.get("lastReportedAt")
        )
        if last_report is not None:
            metadata["last_report_date"] = last_report
        return metadata

    @classmethod
    def _daily_values_from_mapping(
        cls,
        payload: dict[str, object],
    ) -> tuple[dict[str, float], float | None]:
        day_values: dict[str, float] = {}
        current_value: float | None = None
        unit_hint = payload.get("unit") or payload.get("source_unit")
        for key, raw in payload.items():
            day_key = cls._parse_evse_timeseries_date_key(key)
            if day_key is None:
                continue
            numeric = cls._coerce_evse_timeseries_energy(
                raw, key_hint=str(key), unit_hint=unit_hint
            )
            if numeric is None:
                continue
            day_values[day_key] = numeric
        for key in (
            "energy_kwh",
            "value_kwh",
            "daily_energy_kwh",
            "daily_kwh",
            "energy",
            "value",
            "energy_wh",
            "daily_energy_wh",
        ):
            if key not in payload:
                continue
            current_value = cls._coerce_evse_timeseries_energy(
                payload.get(key), key_hint=key, unit_hint=unit_hint
            )
            break
        return day_values, current_value

    @classmethod
    def _daily_values_from_sequence(
        cls,
        values: list[object],
        *,
        start_date_value: object | None = None,
        unit_hint: object | None = None,
    ) -> tuple[dict[str, float], float | None]:
        day_values: dict[str, float] = {}
        current_value: float | None = None
        start_day = cls._parse_evse_timeseries_date_key(start_date_value)
        start_dt = None
        if start_day is not None:
            try:
                start_dt = datetime.fromisoformat(start_day)
            except Exception:  # noqa: BLE001
                start_dt = None
        for idx, item in enumerate(values):
            if isinstance(item, dict):
                day_key = cls._parse_evse_timeseries_date_key(
                    item.get("date")
                    or item.get("day")
                    or item.get("start_date")
                    or item.get("startDate")
                    or item.get("timestamp")
                    or item.get("time")
                )
                item_unit = item.get("unit") or unit_hint
                for key in (
                    "energy_kwh",
                    "value_kwh",
                    "daily_energy_kwh",
                    "energy",
                    "value",
                    "energy_wh",
                    "daily_energy_wh",
                ):
                    if key not in item:
                        continue
                    numeric = cls._coerce_evse_timeseries_energy(
                        item.get(key), key_hint=key, unit_hint=item_unit
                    )
                    if numeric is None:
                        continue
                    if day_key is not None:
                        day_values[day_key] = numeric
                    else:
                        current_value = numeric
                    break
                continue
            numeric = cls._coerce_evse_timeseries_energy(item, unit_hint=unit_hint)
            if numeric is None:
                continue
            if start_dt is not None:
                day_values[(start_dt + timedelta(days=idx)).date().isoformat()] = numeric
            else:
                current_value = numeric
        return day_values, current_value

    @classmethod
    def _normalize_evse_daily_entry(
        cls,
        serial: str,
        payload: object,
        *,
        base_metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        metadata = dict(base_metadata or {})
        day_values: dict[str, float] = {}
        current_value: float | None = None
        if isinstance(payload, dict):
            metadata.update(cls._normalize_evse_timeseries_metadata(payload))
            record_serial = cls._normalize_evse_timeseries_serial(
                payload.get("serial")
                or payload.get("serial_number")
                or payload.get("device_serial")
                or payload.get("charger_serial")
                or payload.get("sn")
            )
            if record_serial and record_serial != serial:
                serial = record_serial
            nested = (
                payload.get("days")
                or payload.get("daily")
                or payload.get("values")
                or payload.get("series")
                or payload.get("data")
            )
            if isinstance(nested, list):
                day_values, current_value = cls._daily_values_from_sequence(
                    nested,
                    start_date_value=payload.get("start_date")
                    or payload.get("startDate"),
                    unit_hint=payload.get("unit") or payload.get("source_unit"),
                )
            elif isinstance(nested, dict):
                day_values, current_value = cls._daily_values_from_mapping(nested)
            else:
                day_values, current_value = cls._daily_values_from_mapping(payload)
        elif isinstance(payload, list):
            day_values, current_value = cls._daily_values_from_sequence(payload)
        else:
            current_value = cls._coerce_evse_timeseries_energy(payload)
        if not day_values and current_value is None:
            return None
        current_day = max(day_values) if day_values else None
        return {
            "serial": serial,
            "day_values_kwh": day_values,
            "energy_kwh": (
                day_values.get(current_day) if current_day is not None else current_value
            ),
            "current_value_kwh": current_value,
            **metadata,
        }

    @classmethod
    def _normalize_evse_lifetime_entry(
        cls,
        serial: str,
        payload: object,
        *,
        base_metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        metadata = dict(base_metadata or {})
        energy_kwh: float | None = None
        if isinstance(payload, dict):
            metadata.update(cls._normalize_evse_timeseries_metadata(payload))
            record_serial = cls._normalize_evse_timeseries_serial(
                payload.get("serial")
                or payload.get("serial_number")
                or payload.get("device_serial")
                or payload.get("charger_serial")
                or payload.get("sn")
            )
            if record_serial and record_serial != serial:
                serial = record_serial
            unit_hint = payload.get("unit") or payload.get("source_unit")
            for key in (
                "energy_kwh",
                "value_kwh",
                "lifetime_energy_kwh",
                "lifetime_kwh",
                "total_kwh",
                "energy_wh",
                "lifetime_energy_wh",
                "value_wh",
                "energy",
                "value",
            ):
                if key not in payload:
                    continue
                energy_kwh = cls._coerce_evse_timeseries_energy(
                    payload.get(key), key_hint=key, unit_hint=unit_hint
                )
                if energy_kwh is not None:
                    break
            if energy_kwh is None:
                values = payload.get("values") or payload.get("series") or payload.get("data")
                if isinstance(values, list):
                    for item in reversed(values):
                        if isinstance(item, dict):
                            for key in (
                                "energy_kwh",
                                "value_kwh",
                                "lifetime_energy_kwh",
                                "energy_wh",
                                "value_wh",
                                "value",
                            ):
                                if key not in item:
                                    continue
                                energy_kwh = cls._coerce_evse_timeseries_energy(
                                    item.get(key),
                                    key_hint=key,
                                    unit_hint=item.get("unit") or unit_hint,
                                )
                                if energy_kwh is not None:
                                    break
                            if energy_kwh is not None:
                                break
                        else:
                            energy_kwh = cls._coerce_evse_timeseries_energy(
                                item, unit_hint=unit_hint
                            )
                            if energy_kwh is not None:
                                break
        else:
            energy_kwh = cls._coerce_evse_timeseries_energy(payload)
        if energy_kwh is None:
            return None
        return {"serial": serial, "energy_kwh": energy_kwh, **metadata}

    @classmethod
    def _normalize_evse_timeseries_payload(
        cls,
        payload: object,
        *,
        daily: bool,
    ) -> dict[str, dict[str, object]] | None:
        data = payload
        if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
            data = data.get("data")
        base_metadata = cls._normalize_evse_timeseries_metadata(
            payload if isinstance(payload, dict) else {}
        )
        if isinstance(data, dict):
            candidates = (
                data.get("results")
                or data.get("chargers")
                or data.get("devices")
                or data.get("timeseries")
            )
            if isinstance(candidates, list):
                data = candidates
        normalized: dict[str, dict[str, object]] = {}
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                serial = cls._normalize_evse_timeseries_serial(
                    item.get("serial")
                    or item.get("serial_number")
                    or item.get("device_serial")
                    or item.get("charger_serial")
                    or item.get("sn")
                )
                if not serial:
                    continue
                entry = (
                    cls._normalize_evse_daily_entry(
                        serial, item, base_metadata=base_metadata
                    )
                    if daily
                    else cls._normalize_evse_lifetime_entry(
                        serial, item, base_metadata=base_metadata
                    )
                )
                if entry is not None:
                    normalized[serial] = entry
            return normalized
        if not isinstance(data, dict):
            return None
        for key, value in data.items():
            serial = cls._normalize_evse_timeseries_serial(key)
            if not serial:
                continue
            entry = (
                cls._normalize_evse_daily_entry(
                    serial, value, base_metadata=base_metadata
                )
                if daily
                else cls._normalize_evse_lifetime_entry(
                    serial, value, base_metadata=base_metadata
                )
            )
            if entry is None:
                continue
            normalized[serial] = entry
        return normalized

    async def evse_timeseries_daily_energy(
        self,
        *,
        request_id: str | None = None,
        username: str | None = None,
    ) -> dict[str, dict[str, object]] | None:
        """Return EVSE daily timeseries keyed by charger serial."""

        request_id = request_id or str(uuid.uuid4())
        if username is None:
            username = self._session_history_username()
        query = {"site_id": self._site, "source": "evse", "requestId": request_id}
        if username:
            query["username"] = username
        url = URL(f"{BASE_URL}/service/timeseries/evse/timeseries/daily_energy").with_query(query)
        headers = self._evse_timeseries_headers(request_id, username)
        try:
            data = await self._json("GET", str(url), headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_evse_timeseries_unavailable_error(err.message, err.status, url):
                raise EVSETimeseriesUnavailable(str(err)) from err
            raise
        return self._normalize_evse_timeseries_payload(data, daily=True)

    async def evse_timeseries_lifetime_energy(
        self,
        *,
        request_id: str | None = None,
        username: str | None = None,
    ) -> dict[str, dict[str, object]] | None:
        """Return EVSE lifetime timeseries keyed by charger serial."""

        request_id = request_id or str(uuid.uuid4())
        if username is None:
            username = self._session_history_username()
        query = {"site_id": self._site, "source": "evse", "requestId": request_id}
        if username:
            query["username"] = username
        url = URL(f"{BASE_URL}/service/timeseries/evse/timeseries/lifetime_energy").with_query(query)
        headers = self._evse_timeseries_headers(request_id, username)
        try:
            data = await self._json("GET", str(url), headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_evse_timeseries_unavailable_error(err.message, err.status, url):
                raise EVSETimeseriesUnavailable(str(err)) from err
            raise
        return self._normalize_evse_timeseries_payload(data, daily=False)

    @staticmethod
    def _coerce_lifetime_energy_value(value: object) -> float | None:
        """Normalize numeric lifetime-energy values into float samples."""

        if isinstance(value, (int, float)):
            try:
                return float(value)
            except Exception:  # noqa: BLE001
                return None
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return None
            try:
                return float(cleaned)
            except Exception:  # noqa: BLE001
                return None
        return None

    @classmethod
    def _coerce_non_boolean_number(cls, value: object) -> float | None:
        """Normalize numeric values while rejecting JSON booleans."""

        if isinstance(value, bool):
            return None
        return cls._coerce_lifetime_energy_value(value)

    @classmethod
    def _normalize_lifetime_energy_payload(cls, payload: object) -> dict | None:
        """Normalize site/HEMS lifetime-energy payloads into a common shape."""

        data = payload
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
        array_field_aliases = {
            "evse_charging": "evse",
            "heat_pump": "heatpump",
            "heat-pump": "heatpump",
            "waterheater": "water_heater",
            "water-heater": "water_heater",
            "water_heater_consumption": "water_heater",
        }
        metadata_fields = {"start_date", "last_report_date", "update_pending", "system_id"}
        metadata_aliases = {
            "startDate": "start_date",
            "lastReportDate": "last_report_date",
            "updatePending": "update_pending",
            "systemId": "system_id",
        }

        normalized: dict[str, object] = {}
        for key, value in data.items():
            canonical_key = array_field_aliases.get(key, key)
            if canonical_key in array_fields:
                # Prefer canonical keys when both canonical and alias variants exist.
                if (
                    canonical_key != key
                    and canonical_key in data
                    and canonical_key in normalized
                ):
                    continue
                if isinstance(value, list):
                    normalized[canonical_key] = [
                        cls._coerce_lifetime_energy_value(v) for v in value
                    ]
                else:
                    normalized[canonical_key] = []
                continue
            if key in metadata_fields:
                normalized[key] = value
                continue
            canonical_meta = metadata_aliases.get(key)
            if canonical_meta and canonical_meta not in normalized:
                normalized[canonical_meta] = value

        interval_minutes = cls._coerce_lifetime_energy_value(
            data.get("interval_minutes")
            or data.get("interval")
            or data.get("interval_min")
            or data.get("intervalMinutes")
        )
        if interval_minutes is not None and interval_minutes > 0:
            normalized["interval_minutes"] = interval_minutes
        return normalized

    async def hems_consumption_lifetime(self) -> dict | None:
        """Return HEMS lifetime consumption buckets when available.

        GET /systems/<site_id>/hems_consumption_lifetime
        """

        url = f"{BASE_URL}/systems/{self._site}/hems_consumption_lifetime"
        try:
            data = await self._json("GET", url, headers=self._hems_headers)
            self._hems_site_supported = True
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err):
                _LOGGER.debug(
                    "HEMS lifetime endpoint unavailable for site %s (%s)",
                    self._site,
                    err.summary,
                )
                return None
            raise
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403, 404) or _is_hems_invalid_site_error(err):
                if _is_hems_invalid_site_error(err):
                    self._hems_site_supported = False
                _LOGGER.debug(
                    "HEMS lifetime endpoint unavailable for site %s (status=%s)",
                    self._site,
                    err.status,
                )
                return None
            raise
        return self._normalize_lifetime_energy_payload(data)

    @classmethod
    def _normalize_hems_power_timeseries_payload(cls, payload: object) -> dict | None:
        """Normalize HEMS heat-pump power timeseries payloads."""

        data = payload
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data.get("data")
        if not isinstance(data, dict):
            return None

        raw_values: object | None = None
        fallback_non_list: object | None = None
        for key in (
            "heat_pump_consumption",
            "heatpump_consumption",
            "heat-pump-consumption",
            "heatPumpConsumption",
            "heatpumpConsumption",
            "heat_pump",
            "heat-pump",
            "heatpump",
        ):
            value = data.get(key)
            if value is None:
                continue
            if isinstance(value, list):
                raw_values = value
                break
            if fallback_non_list is None:
                fallback_non_list = value
        if raw_values is None:
            for key, value in data.items():
                key_text = str(key).strip().lower()
                normalized_key = "".join(ch for ch in key_text if ch.isalnum())
                if "heatpump" not in normalized_key:
                    continue
                if "consumption" not in normalized_key and not normalized_key.endswith(
                    "heatpump"
                ):
                    continue
                if not isinstance(value, list):
                    if fallback_non_list is None:
                        fallback_non_list = value
                    continue
                raw_values = value
                break
        if raw_values is None:
            raw_values = fallback_non_list
        values: list[float | None]
        if isinstance(raw_values, list):
            values = [cls._coerce_lifetime_energy_value(item) for item in raw_values]
        else:
            values = []

        normalized: dict[str, object] = {
            "heat_pump_consumption": values,
        }
        start_date = data.get("start_date")
        if start_date is None:
            start_date = data.get("startDate")
        if start_date is not None:
            normalized["start_date"] = start_date
        interval_minutes = cls._coerce_lifetime_energy_value(
            data.get("interval_minutes")
            or data.get("interval")
            or data.get("interval_min")
            or data.get("intervalMinutes")
        )
        if interval_minutes is not None and interval_minutes > 0:
            normalized["interval_minutes"] = interval_minutes
        return normalized

    @staticmethod
    def _is_hems_invalid_date_error(err: aiohttp.ClientResponseError) -> bool:
        """Return True when HEMS reports a date-validation 422 response."""

        if err.status != 422:
            return False
        try:
            text = str(err.message or "").lower()
        except Exception:  # noqa: BLE001 - defensive casting
            text = ""
        return any(
            token in text
            for token in (
                "valid date",
                "invalid date",
                "date valide",
                "saisissez une date",
            )
        )

    async def hems_power_timeseries(self, device_uid: str | None = None) -> dict | None:
        """Return HEMS heat-pump power timeseries when available.

        GET /systems/<site_id>/hems_power_timeseries[?device-uid=<device_uid>]
        """

        base_url = f"{BASE_URL}/systems/{self._site}/hems_power_timeseries"
        url = base_url
        if device_uid:
            url = str(URL(url).update_query({"device-uid": str(device_uid)}))
        try:
            data = await self._json("GET", url, headers=self._hems_headers)
            self._hems_site_supported = True
        except Unauthorized:
            _LOGGER.debug(
                "HEMS power endpoint unavailable for site %s (unauthorized)",
                self._site,
            )
            return None
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err):
                _LOGGER.debug(
                    "HEMS power endpoint unavailable for site %s (%s)",
                    self._site,
                    err.summary,
                )
                return None
            raise
        except aiohttp.ClientResponseError as err:
            if self._is_hems_invalid_date_error(err):
                if not device_uid:
                    _LOGGER.debug(
                        "HEMS power endpoint rejected date for site %s (status=%s): %s",
                        self._site,
                        err.status,
                        err.message,
                    )
                    return None
                _LOGGER.debug(
                    "HEMS power endpoint rejected filtered request for site %s; retrying unfiltered: %s",
                    self._site,
                    err.message,
                )
                try:
                    data = await self._json("GET", base_url, headers=self._hems_headers)
                    self._hems_site_supported = True
                except Unauthorized:
                    _LOGGER.debug(
                        "HEMS power endpoint unavailable for site %s (unauthorized)",
                        self._site,
                    )
                    return None
                except InvalidPayloadError as retry_err:
                    if _is_optional_non_json_payload(retry_err):
                        _LOGGER.debug(
                            "HEMS power endpoint unavailable for site %s (%s)",
                            self._site,
                            retry_err.summary,
                        )
                        return None
                    raise
                except aiohttp.ClientResponseError as retry_err:
                    if (
                        retry_err.status in (401, 403, 404)
                        or _is_hems_invalid_site_error(retry_err)
                        or self._is_hems_invalid_date_error(
                            retry_err
                        )
                    ):
                        if _is_hems_invalid_site_error(retry_err):
                            self._hems_site_supported = False
                        _LOGGER.debug(
                            "HEMS power endpoint unavailable for site %s (status=%s)",
                            self._site,
                            retry_err.status,
                        )
                        return None
                    raise
                return self._normalize_hems_power_timeseries_payload(data)
            if err.status in (401, 403, 404) or _is_hems_invalid_site_error(err):
                if _is_hems_invalid_site_error(err):
                    self._hems_site_supported = False
                _LOGGER.debug(
                    "HEMS power endpoint unavailable for site %s (status=%s)",
                    self._site,
                    err.status,
                )
                return None
            raise
        return self._normalize_hems_power_timeseries_payload(data)

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

    async def evse_fw_details(self) -> list[dict[str, Any]] | None:
        """Fetch EVSE firmware details for the current site.

        GET /service/evse_management/fwDetails/<site_id>
        Returns a list of charger firmware-detail objects keyed by serialNumber.
        """

        url = f"{BASE_URL}/service/evse_management/fwDetails/{self._site}"
        try:
            data = await self._json("GET", url)
        except Unauthorized:
            _LOGGER.debug(
                "EVSE firmware details endpoint unavailable for site %s (unauthorized)",
                self._site,
            )
            return None
        except aiohttp.ClientResponseError as err:
            if err.status in (403, 404):
                _LOGGER.debug(
                    "EVSE firmware details endpoint unavailable for site %s (status=%s)",
                    self._site,
                    err.status,
                )
                return None
            raise

        if data is None:
            return []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        raise InvalidPayloadError(
            "EVSE firmware details payload must be a list",
            endpoint=f"/service/evse_management/fwDetails/{self._site}",
        )

    async def evse_feature_flags(self, *, country: str | None = None) -> dict | None:
        """Return EVSE feature flags and UI gating details for the site.

        GET /service/evse_management/api/v1/config/feature-flags?site_id=<site_id>[&country=<country>]
        """

        url = str(
            URL(f"{BASE_URL}/service/evse_management/api/v1/config/feature-flags").update_query(
                {
                    key: value
                    for key, value in {
                        "site_id": self._site,
                        "country": country,
                    }.items()
                    if value is not None
                }
            )
        )
        try:
            data = await self._json("GET", url)
        except Unauthorized:
            _LOGGER.debug(
                "EVSE feature flags endpoint unavailable for site %s (unauthorized)",
                self._site,
            )
            return None
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403, 404):
                _LOGGER.debug(
                    "EVSE feature flags endpoint unavailable for site %s (status=%s)",
                    self._site,
                    err.status,
                )
                return None
            raise
        return data if isinstance(data, dict) else None

    async def devices_inventory(self) -> dict:
        """Return site device inventory grouped by hardware type.

        GET /app-api/<site_id>/devices.json
        """
        url = f"{BASE_URL}/app-api/{self._site}/devices.json"
        data = await self._json("GET", url)
        if isinstance(data, dict):
            return data
        return {}

    async def devices_tree(self) -> dict | None:
        """Return the system dashboard device hierarchy when available.

        GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices-tree
        Fallback: GET /pv/systems/<site_id>/system_dashboard/devices-tree
        """

        modern_url = (
            f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/"
            f"{self._site}/devices-tree"
        )
        legacy_url = f"{BASE_URL}/pv/systems/{self._site}/system_dashboard/devices-tree"
        return await self._system_dashboard_get(modern_url, legacy_url)

    async def devices_details(self, type_key: str) -> dict | None:
        """Return system dashboard per-type device details when available.

        GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=<observed_type>
        Fallback: GET /pv/systems/<site_id>/system_dashboard/devices_details?type=<observed_type>
        """

        normalized = _system_dashboard_query_type(type_key)
        if not normalized:
            return None
        modern_url = str(
            URL(
                f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/{self._site}/devices_details"
            ).update_query({"type": normalized})
        )
        legacy_url = str(
            URL(
                f"{BASE_URL}/pv/systems/{self._site}/system_dashboard/devices_details"
            ).update_query({"type": normalized})
        )
        return await self._system_dashboard_get(modern_url, legacy_url)

    async def hems_devices(self, *, refresh_data: bool = False) -> dict | None:
        """Return dedicated HEMS device inventory when available.

        GET https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/hems-devices
        """

        url = str(
            URL(
                f"https://hems-integration.enphaseenergy.com/api/v1/hems/{self._site}/hems-devices"
            ).update_query({"refreshData": str(bool(refresh_data)).lower()})
        )
        try:
            data = await self._json("GET", url, headers=self._hems_headers)
            self._hems_site_supported = True
        except Unauthorized:
            _LOGGER.debug(
                "HEMS devices endpoint unavailable for site %s (unauthorized)",
                self._site,
            )
            return None
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err):
                _LOGGER.debug(
                    "HEMS devices endpoint unavailable for site %s (%s)",
                    self._site,
                    err.summary,
                )
                return None
            raise
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403, 404) or _is_hems_invalid_site_error(err):
                if _is_hems_invalid_site_error(err):
                    self._hems_site_supported = False
                _LOGGER.debug(
                    "HEMS devices endpoint unavailable for site %s (status=%s)",
                    self._site,
                    err.status,
                )
                return None
            raise
        return data if isinstance(data, dict) else None

    async def grid_control_check(self) -> dict:
        """Return site-level grid control eligibility guard flags.

        GET /app-api/<site_id>/grid_control_check.json
        """

        url = f"{BASE_URL}/app-api/{self._site}/grid_control_check.json"
        data = await self._json("GET", url)
        if isinstance(data, dict):
            return data
        return {}

    async def request_grid_toggle_otp(self) -> dict:
        """Request OTP delivery for a site grid-mode toggle.

        GET /app-api/<site_id>/grid_toggle_otp.json
        """

        url = f"{BASE_URL}/app-api/{self._site}/grid_toggle_otp.json"
        headers = dict(self._control_headers())
        data = await self._json("GET", url, headers=headers)
        if isinstance(data, dict):
            return data
        return {}

    async def validate_grid_toggle_otp(self, otp: str) -> bool:
        """Validate a grid-mode OTP for the configured site.

        POST /app-api/grid_toggle_otp.json
        """

        url = f"{BASE_URL}/app-api/grid_toggle_otp.json"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE_URL,
        }
        headers.update(self._control_headers())
        payload = {"otp": str(otp), "site_id": str(self._site)}
        data = await self._json("POST", url, data=payload, headers=headers)
        if not isinstance(data, dict):
            return False
        return data.get("valid") is True

    async def set_grid_state(self, envoy_serial_number: str, state: int) -> dict:
        """Submit a grid relay state-change request.

        POST /pv/settings/grid_state.json
        """

        url = f"{BASE_URL}/pv/settings/grid_state.json"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE_URL,
        }
        headers.update(self._control_headers())
        payload = {
            "envoy_serial_number": str(envoy_serial_number),
            "state": int(state),
        }
        data = await self._json("POST", url, data=payload, headers=headers)
        if isinstance(data, dict):
            return data
        return {}

    async def log_grid_change(
        self,
        envoy_serial_number: str,
        old_state: str,
        new_state: str,
    ) -> dict:
        """Write grid relay transition audit metadata.

        POST /pv/settings/log_grid_change.json
        """

        url = f"{BASE_URL}/pv/settings/log_grid_change.json"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE_URL,
        }
        headers.update(self._control_headers())
        payload = {
            "envoy_serial_number": str(envoy_serial_number),
            "old_state": str(old_state),
            "new_state": str(new_state),
        }
        data = await self._json("POST", url, data=payload, headers=headers)
        if isinstance(data, dict):
            return data
        return {}

    async def battery_backup_history(self) -> dict:
        """Return battery backup outage history for the site.

        GET /app-api/<site_id>/battery_backup_history.json
        """

        url = f"{BASE_URL}/app-api/{self._site}/battery_backup_history.json"
        data = await self._json("GET", url)
        if isinstance(data, dict):
            return data
        return {}

    async def battery_status(self) -> dict:
        """Return battery status payload used by the Enlighten battery card.

        GET /pv/settings/<site_id>/battery_status.json
        """

        url = f"{BASE_URL}/pv/settings/{self._site}/battery_status.json"
        data = await self._json("GET", url)
        if isinstance(data, dict):
            return data
        return {}

    async def dry_contacts_settings(self) -> dict:
        """Return dry-contact settings payload used by site settings views.

        GET /pv/settings/<site_id>/dry_contacts
        """

        url = f"{BASE_URL}/pv/settings/{self._site}/dry_contacts"
        data = await self._json("GET", url)
        if isinstance(data, dict):
            return data
        return {}

    async def inverters_inventory(
        self,
        *,
        limit: int = 1000,
        offset: int = 0,
        search: str = "",
    ) -> dict:
        """Return site inverter inventory used by legacy microinverter views.

        GET /app-api/<site_id>/inverters.json
        """

        url = URL(f"{BASE_URL}/app-api/{self._site}/inverters.json").with_query(
            {
                "limit": int(limit),
                "offset": int(offset),
                "search": str(search),
            }
        )
        data = await self._json("GET", str(url))
        if not isinstance(data, dict):
            return {}
        return data

    async def inverter_status(self) -> dict[str, dict[str, Any]]:
        """Return inverter status map keyed by inverter id.

        GET /systems/<site_id>/inverter_status_x.json
        """

        url = f"{BASE_URL}/systems/{self._site}/inverter_status_x.json"
        data = await self._json("GET", url)
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            key_text = str(key).strip()
            if not key_text:
                continue
            out[key_text] = dict(value)
        return out

    async def inverter_production(
        self,
        *,
        start_date: str,
        end_date: str,
    ) -> dict:
        """Return inverter production totals for a date range.

        GET /systems/<site_id>/inverter_data_x/energy.json?start_date=...&end_date=...
        """

        url = URL(
            f"{BASE_URL}/systems/{self._site}/inverter_data_x/energy.json"
        ).with_query({"start_date": str(start_date), "end_date": str(end_date)})
        data = await self._json("GET", str(url))
        if not isinstance(data, dict):
            return {}
        production_raw = data.get("production")
        production: dict[str, float] = {}
        if isinstance(production_raw, dict):
            for key, value in production_raw.items():
                key_text = str(key).strip()
                if not key_text:
                    continue
                try:
                    production[key_text] = float(value)
                except (TypeError, ValueError):
                    continue
        return {
            "production": production,
            "start_date": data.get("start_date"),
            "end_date": data.get("end_date"),
        }

    async def session_history_filter_criteria(
        self,
        *,
        request_id: str | None = None,
        username: str | None = None,
    ) -> dict:
        """Fetch session history filter criteria for a site."""

        request_id = request_id or str(uuid.uuid4())
        if username is None:
            username = self._session_history_username()
        query = {"source": "evse", "requestId": request_id}
        if username:
            query["username"] = username
        url = URL(
            f"{BASE_URL}/service/enho_historical_events_ms/{self._site}/filter_criteria"
        ).with_query(query)
        headers = self._session_history_headers(request_id, username)
        return await self._json("GET", str(url), headers=headers)

    async def session_history(
        self,
        sn: str,
        *,
        start_date: str,
        end_date: str | None = None,
        offset: int = 0,
        limit: int = 20,
        timezone: str | None = None,
        request_id: str | None = None,
        username: str | None = None,
    ) -> dict:
        """Fetch charging sessions for a charger between the provided dates.

        POST /service/enho_historical_events_ms/<site_id>/sessions/<sn>/history
        Dates must be formatted as DD-MM-YYYY in the site locale.
        """
        url = f"{BASE_URL}/service/enho_historical_events_ms/{self._site}/sessions/{sn}/history"
        request_id = request_id or str(uuid.uuid4())
        if username is None:
            username = self._session_history_username()
        payload: dict[str, Any] = {
            "source": "evse",
            "params": {
                "offset": int(offset),
                "limit": int(limit),
                "startDate": start_date,
                "endDate": end_date or start_date,
            },
        }
        if timezone:
            payload["params"]["timezone"] = timezone
        headers = self._session_history_headers(request_id, username)
        try:
            return await self._json("POST", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_session_history_unavailable_error(err.message, err.status, url):
                raise SessionHistoryUnavailable(str(err)) from err
            raise
