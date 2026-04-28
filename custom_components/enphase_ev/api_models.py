"""Typed API boundary models for the Enphase EV client."""

from dataclasses import dataclass

type CookieJar = dict[str, str]
type HeaderMap = dict[str, str]


@dataclass(slots=True)
class AuthTokens:
    """Container for Enlighten authentication state."""

    cookie: str
    session_id: str | None = None
    access_token: str | None = None
    token_expires_at: int | None = None
    raw_cookies: CookieJar | None = None


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


@dataclass(slots=True, frozen=True)
class TextResponse:
    """Raw text response returned by browser-compatible endpoints."""

    status: int
    text: str
    url: str
    headers: HeaderMap
    location: str | None = None
