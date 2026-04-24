"""Typed API boundary models for the Enphase EV client."""

from __future__ import annotations

from dataclasses import dataclass


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


@dataclass(slots=True, frozen=True)
class TextResponse:
    """Raw text response returned by browser-compatible endpoints."""

    status: int
    text: str
    url: str
    headers: dict[str, str]
    location: str | None = None
