"""Tests for helper utilities in the API module."""

from __future__ import annotations

import base64
import json

from aiohttp import CookieJar
from yarl import URL

from custom_components.enphase_ev import api


import pytest


@pytest.mark.asyncio
async def test_serialize_cookie_jar() -> None:
    """Cookie jar serialization should extract cookies for provided URLs."""
    jar = CookieJar()
    jar.update_cookies({"session": "abc123"}, URL("https://enphase.test"))
    header, mapping = api._serialize_cookie_jar(
        jar, ["https://enphase.test", "https://ignored.invalid"]
    )
    assert header == "session=abc123"
    assert mapping == {"session": "abc123"}


def test_decode_jwt_exp_success() -> None:
    """Successfully decode the exp claim from a JWT payload."""
    payload = {"exp": 1_700_000_000}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    payload_b64 = payload_b64.rstrip("=")
    token = f"header.{payload_b64}.sig"
    assert api._decode_jwt_exp(token) == 1_700_000_000


def test_decode_jwt_exp_failure() -> None:
    """Non-JWT strings should produce None."""
    assert api._decode_jwt_exp("not-a-token") is None


def test_extract_xsrf_token() -> None:
    """XSRF token is located case-insensitively."""
    cookies = {"session": "abc", "XSRF-TOKEN": "xsrf-value"}
    assert api._extract_xsrf_token(cookies) == "xsrf-value"


def test_normalize_sites_handles_nested_structures() -> None:
    """Site normalization should handle nested dict responses."""
    payload = {
        "data": [
            {"site_id": 1234, "name": "Garage"},
            {"siteId": "5678", "siteName": "Backup"},
            {"id": 9},
        ]
    }
    sites = api._normalize_sites(payload)
    assert [site.site_id for site in sites] == ["1234", "5678", "9"]
    assert sites[0].name == "Garage"
    assert sites[1].name == "Backup"
    assert sites[2].name is None


def test_normalize_chargers_handles_varied_keys() -> None:
    """Charger normalization should account for different payload keys."""
    payload = {
        "data": {
            "chargers": [
                {"serial": "EV123", "name": "Garage"},
                {"serialNumber": "EV456", "displayName": "Driveway"},
                {"sn": "EV789"},
            ]
        }
    }
    chargers = api._normalize_chargers(payload)
    assert [charger.serial for charger in chargers] == ["EV123", "EV456", "EV789"]
    assert chargers[0].name == "Garage"
    assert chargers[1].name == "Driveway"
    assert chargers[2].name is None
