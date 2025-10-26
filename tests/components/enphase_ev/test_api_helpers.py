"""Tests for helper utilities in the API module."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

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


def test_serialize_cookie_jar_ignores_failures() -> None:
    """Defensive branches ignore casting and filter errors."""

    class BadURL:
        def __str__(self) -> str:
            raise RuntimeError("cannot stringify")

    morsel = SimpleNamespace(value="good")
    jar = MagicMock()
    jar.filter_cookies.side_effect = [
        RuntimeError("jar blew up"),
        {"session": morsel},
    ]
    header, mapping = api._serialize_cookie_jar(
        jar,
        [
            URL("https://first.example"),
            BadURL(),
            "https://second.example",
        ],
    )
    assert header == "session=good"
    assert mapping == {"session": "good"}


def test_serialize_cookie_jar_skips_invalid_url() -> None:
    """Invalid URL entries are ignored without raising."""

    class BrokenURL:
        def __str__(self) -> str:
            raise RuntimeError("bad url")

    jar = MagicMock()
    header, mapping = api._serialize_cookie_jar(jar, [BrokenURL()])
    assert header == ""
    assert mapping == {}
    jar.filter_cookies.assert_not_called()


def test_serialize_cookie_jar_filter_failure() -> None:
    """Exceptions from filter_cookies should be swallowed."""
    jar = MagicMock()
    jar.filter_cookies.side_effect = RuntimeError("fail")
    header, mapping = api._serialize_cookie_jar(jar, ["https://example.test"])
    assert header == ""
    assert mapping == {}


def _make_token(payload: dict) -> str:
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    payload_b64 = payload_b64.rstrip("=")
    return f"header.{payload_b64}.sig"


def test_decode_jwt_exp_success() -> None:
    """Successfully decode the exp claim from a JWT payload."""
    token = _make_token({"exp": 1_700_000_000})
    assert api._decode_jwt_exp(token) == 1_700_000_000


def test_decode_jwt_exp_invalid_payload_returns_none() -> None:
    """Invalid payloads are handled defensively."""
    bad_payload = base64.urlsafe_b64encode(b"not-json").decode().rstrip("=")
    token = f"header.{bad_payload}.sig"
    assert api._decode_jwt_exp(token) is None


def test_decode_jwt_exp_non_numeric_exp() -> None:
    """Exp claims that are not numeric should be ignored."""
    token = _make_token({"exp": "tomorrow"})
    assert api._decode_jwt_exp(token) is None


def test_decode_jwt_exp_failure() -> None:
    """Non-JWT strings should produce None."""
    assert api._decode_jwt_exp("not-a-token") is None


def test_extract_xsrf_token() -> None:
    """XSRF token is located case-insensitively."""
    cookies = {"session": "abc", "XSRF-TOKEN": "xsrf-value"}
    assert api._extract_xsrf_token(cookies) == "xsrf-value"


def test_extract_xsrf_token_missing() -> None:
    """Missing tokens return None without raising."""
    assert api._extract_xsrf_token(None) is None
    assert api._extract_xsrf_token({"session": "abc"}) is None


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


def test_normalize_sites_skips_invalid_entries() -> None:
    """Invalid items or missing ids should be ignored."""
    payload = ["not-a-dict", {"id": None}, {"siteId": "42", "name": "Valid"}]
    result = api._normalize_sites(payload)
    assert [site.site_id for site in result] == ["42"]


def test_normalize_sites_with_non_iterable_payload() -> None:
    """Non-iterable payloads produce an empty list."""
    assert api._normalize_sites("unexpected") == []


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


def test_normalize_chargers_skips_invalid_entries() -> None:
    """Non-dict items or missing serials are filtered out."""
    payload = {
        "chargers": [
            "not-a-dict",
            {"name": "Missing serial"},
            {"id": 42, "display_name": "Valid"},
        ]
    }
    result = api._normalize_chargers(payload)
    assert [charger.serial for charger in result] == ["42"]
    assert result[0].name == "Valid"


def test_normalize_chargers_with_non_iterable_payload() -> None:
    """Non-iterable payloads result in an empty list."""
    assert api._normalize_chargers({"data": None}) == []
