#!/usr/bin/env python3
"""Build and publish Enphase firmware catalog artifacts.

This script discovers release-note metadata from Enphase's documentation pages,
then emits source metadata and a runtime catalog used by the integration.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

BASE_URL = "https://enphase.com"
ROOT_PATH = "/installers/resources/documentation"
TARGET_CATEGORY_LABEL = "Apps and software"
TARGET_CATEGORY_PATH = "/installers/resources/documentation/apps"

TARGET_PRODUCTS: dict[str, str] = {
    "envoy": "IQ Gateway software",
    "microinverter": "IQ Microinverter software",
}

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_PAGES = 40
SCHEMA_VERSION = 1
PREVIOUS_RUNTIME_CATALOG_URL = (
    "https://raw.githubusercontent.com/barneyonline/ha-enphase-ev-charger/"
    "firmware-catalog/catalog/v1/runtime_catalog.json"
)

# Small deterministic alias map for parsing country applicability text.
COUNTRY_TOKEN_ALIASES: dict[str, str] = {
    "us": "US",
    "usa": "US",
    "unitedstates": "US",
    "unitedstatesofamerica": "US",
    "puertorico": "PR",
    "canada": "CA",
    "mexico": "MX",
    "australia": "AU",
    "newzealand": "NZ",
    "unitedkingdom": "GB",
    "uk": "GB",
    "greatbritain": "GB",
    "ireland": "IE",
    "germany": "DE",
    "austria": "AT",
    "france": "FR",
    "italy": "IT",
    "spain": "ES",
    "portugal": "PT",
    "netherlands": "NL",
    "belgium": "BE",
    "switzerland": "CH",
    "sweden": "SE",
    "norway": "NO",
    "finland": "FI",
    "denmark": "DK",
    "poland": "PL",
    "czechrepublic": "CZ",
    "czechia": "CZ",
    "slovakia": "SK",
    "romania": "RO",
    "hungary": "HU",
    "bulgaria": "BG",
    "greece": "GR",
    "cyprus": "CY",
    "turkiye": "TR",
    "turkey": "TR",
    "japan": "JP",
    "thailand": "TH",
    "philippines": "PH",
    "malaysia": "MY",
    "vietnam": "VN",
    "india": "IN",
    "croatia": "HR",
    "slovenia": "SI",
    "estonia": "EE",
    "latvia": "LV",
    "lithuania": "LT",
    "luxembourg": "LU",
    "malta": "MT",
    "dominicanrepublic": "DO",
    "colombia": "CO",
    "costarica": "CR",
    "panama": "PA",
    "serbia": "RS",
    "southafrica": "ZA",
    "bermuda": "BM",
    "aruba": "AW",
    "frenchpolynesia": "PF",
    "monaco": "MC",
    "turksandcaicos": "TC",
}


def _now_utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _strip_tags(value: str) -> str:
    return _collapse_ws(re.sub(r"<[^>]+>", " ", html.unescape(value)))


def _parse_date_to_iso(value: str) -> str | None:
    text = _collapse_ws(value).replace("\u00a0", " ")
    if not text:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _version_sort_key(value: str | None) -> tuple[Any, ...]:
    if not value:
        return tuple()
    key: list[Any] = []
    for token in re.split(r"[^A-Za-z0-9]+", value):
        if not token:
            continue
        if token.isdigit():
            key.append(int(token))
        else:
            key.append(token.lower())
    return tuple(key)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _normalize_locale(locale: str | None) -> str:
    if not locale:
        return "en"
    text = locale.strip().lower().replace("_", "-")
    if not text:
        return "en"
    return text


def _with_query(url: str, params: dict[str, str]) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


@dataclass(slots=True)
class ReleaseCard:
    title: str
    version: str | None
    release_date: str | None
    media_id: str | None
    langcode: str
    summary: str
    countries_text: str | None


@dataclass(slots=True)
class CountryApplicability:
    include: set[str] = field(default_factory=set)
    exclude: set[str] = field(default_factory=set)
    all_countries: bool = False
    ambiguous: bool = False


class ReleaseCardParser(HTMLParser):
    """Parse release cards from the documentation listing HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.cards: list[ReleaseCard] = []
        self._card_depth = 0
        self._stack: list[dict[str, bool]] = []
        self._in_name = 0
        self._in_date = 0
        self._in_note = 0
        self._title_parts: list[str] = []
        self._date_parts: list[str] = []
        self._note_parts: list[str] = []
        self._media_id: str | None = None
        self._langcode: str | None = None

    @staticmethod
    def _class_tokens(attrs: dict[str, str]) -> set[str]:
        return {token for token in attrs.get("class", "").split() if token}

    @staticmethod
    def _parse_x_data(value: str) -> tuple[str | None, str | None]:
        media_match = re.search(r"media_id\s*:\s*['\"]([^'\"]+)['\"]", value)
        lang_match = re.search(r"langcode\s*:\s*['\"]([^'\"]+)['\"]", value)
        media_id = media_match.group(1).strip() if media_match else None
        langcode = lang_match.group(1).strip() if lang_match else None
        return media_id, langcode

    @staticmethod
    def _extract_country_text(note_text: str) -> str | None:
        text = _collapse_ws(note_text)
        if not text:
            return None

        for label in ("Countries", "Geographies"):
            marker = f"{label}:"
            idx = text.lower().find(marker.lower())
            if idx < 0:
                continue
            tail = text[idx + len(marker) :].strip()
            if not tail:
                continue
            # Stop at likely next field labels in structured release notes.
            stops = [
                "Platforms supported:",
                "Microinverters supported:",
                "Supported system configurations:",
                "Release notes:",
                "Release note:",
                "Applicable devices:",
                "Features:",
            ]
            end = len(tail)
            for stop in stops:
                stop_idx = tail.lower().find(stop.lower())
                if stop_idx >= 0:
                    end = min(end, stop_idx)
            value = tail[:end].strip(" .;,")
            if value:
                return value

        # Fallback for simpler sentence-style notes.
        match = re.search(
            r"\b(?:Countries|Geographies)\s*:\s*([^\n]+)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            value = match.group(1).strip(" .;,")
            return value or None

        return None

    def _reset_card(self) -> None:
        self._title_parts = []
        self._date_parts = []
        self._note_parts = []
        self._media_id = None
        self._langcode = None

    def _flush_card(self) -> None:
        title = _collapse_ws(" ".join(self._title_parts))
        canonical_title_match = re.search(
            r"(.+?release notes\s*\([^()]+\))",
            title,
            flags=re.IGNORECASE,
        )
        if canonical_title_match:
            title = canonical_title_match.group(1).strip()
        if not title:
            return
        version_match = re.search(r"\(([^()]+)\)\s*$", title)
        version = version_match.group(1).strip() if version_match else None
        release_date = _parse_date_to_iso(" ".join(self._date_parts))
        summary = _collapse_ws(" ".join(self._note_parts))
        if len(summary) > 500:
            summary = summary[:497].rstrip() + "..."
        countries_text = self._extract_country_text(summary)
        card = ReleaseCard(
            title=title,
            version=version,
            release_date=release_date,
            media_id=self._media_id,
            langcode=self._langcode or "und",
            summary=summary,
            countries_text=countries_text,
        )
        self.cards.append(card)

    def handle_starttag(self, tag: str, attrs_tuples: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_tuples}
        classes = self._class_tokens(attrs)

        if self._card_depth == 0 and tag == "div" and "release-item" in classes:
            self._card_depth = 1
            self._stack.clear()
            self._in_name = 0
            self._in_date = 0
            self._in_note = 0
            self._reset_card()
            self._stack.append({"name": False, "date": False, "note": False})
            return

        if self._card_depth == 0:
            return

        entered_name = tag == "div" and "release-item__name" in classes
        entered_date = tag == "div" and "release-item__date" in classes
        entered_note = tag == "div" and "release-item__note" in classes
        if entered_name:
            self._in_name += 1
        if entered_date:
            self._in_date += 1
        if entered_note:
            self._in_note += 1

        if tag == "button" and "document-copy-link" in classes:
            media_id, langcode = self._parse_x_data(attrs.get("x-data", ""))
            if media_id:
                self._media_id = media_id
            if langcode:
                self._langcode = langcode

        self._stack.append({"name": entered_name, "date": entered_date, "note": entered_note})
        self._card_depth += 1

    def handle_endtag(self, _tag: str) -> None:
        if self._card_depth == 0:
            return

        self._card_depth -= 1
        context = self._stack.pop() if self._stack else {"name": False, "date": False, "note": False}
        if context.get("name"):
            self._in_name = max(0, self._in_name - 1)
        if context.get("date"):
            self._in_date = max(0, self._in_date - 1)
        if context.get("note"):
            self._in_note = max(0, self._in_note - 1)

        if self._card_depth == 0:
            self._flush_card()

    def handle_data(self, data: str) -> None:
        if self._card_depth == 0:
            return
        if self._in_name > 0:
            self._title_parts.append(data)
        if self._in_date > 0:
            self._date_parts.append(data)
        if self._in_note > 0:
            self._note_parts.append(data)


def fetch_text(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    request = Request(url, headers={"User-Agent": "ha-enphase-ev-firmware-catalog/1.0"})
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("content-type", "")
        charset_match = re.search(r"charset=([\w\-]+)", content_type, flags=re.I)
        charset = charset_match.group(1) if charset_match else "utf-8"
        return response.read().decode(charset, errors="replace")


def discover_apps_entrypoint(root_html: str) -> tuple[str, str]:
    """Return (apps_path, product_type) discovered from root documentation page."""
    matches = re.finditer(
        r"<a[^>]+href=\"([^\"]*product_type=(\d+)[^\"]*)\"[^>]*aria-label=\"([^\"]+)\"",
        root_html,
        flags=re.IGNORECASE,
    )
    for match in matches:
        href, product_type, aria_label = match.groups()
        if TARGET_CATEGORY_LABEL.lower() not in html.unescape(aria_label).lower():
            continue
        parsed = urlparse(html.unescape(href))
        path = parsed.path or TARGET_CATEGORY_PATH
        if not path.endswith("/apps"):
            path = TARGET_CATEGORY_PATH
        return path, product_type

    # Fallback to known category path if card structure changes.
    return TARGET_CATEGORY_PATH, "216"


def parse_product_type_from_apps_page(apps_html: str) -> str | None:
    match = re.search(r"productType\s*:\s*'?(\d+)'?", apps_html)
    if match:
        return match.group(1)
    match = re.search(r"[?&]product_type=(\d+)", apps_html)
    return match.group(1) if match else None


def parse_facet_values(apps_html: str, alias: str) -> dict[str, int]:
    section_match = re.search(
        rf"<ul[^>]+data-drupal-facet-alias=\"{re.escape(alias)}\"[^>]*>(.*?)</ul>",
        apps_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not section_match:
        return {}
    section = section_match.group(1)
    values: dict[str, int] = {}
    for match in re.finditer(
        r"data-drupal-facet-item-value=\"(\d+)\"[^>]*>\s*<span class=\"facet-item__value\">(.*?)</span>",
        section,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raw_id, raw_label = match.groups()
        label = _strip_tags(raw_label)
        if not label:
            continue
        values[label] = int(raw_id)
    return values


def parse_language_options(apps_html: str, select_name: str) -> dict[str, str]:
    match = re.search(
        rf"<select[^>]*name=\"{re.escape(select_name)}\"[^>]*>(.*?)</select>",
        apps_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return {}

    options_html = match.group(1)
    options: dict[str, str] = {}
    for option_match in re.finditer(
        r"<option[^>]*value=\"([^\"]*)\"[^>]*>(.*?)</option>",
        options_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raw_value, raw_label = option_match.groups()
        value = _normalize_locale(html.unescape(raw_value))
        label = _strip_tags(raw_label)
        if not value or value == "all" or not label:
            continue
        options[value] = label
    return options


def find_next_page_url(current_url: str, page_html: str) -> str | None:
    match = re.search(r"<a[^>]+href=\"([^\"]+)\"[^>]*rel=\"next\"", page_html)
    if not match:
        return None
    href = html.unescape(match.group(1))
    return urljoin(current_url, href)


def parse_release_cards(page_html: str) -> list[ReleaseCard]:
    parser = ReleaseCardParser()
    parser.feed(page_html)
    return parser.cards


def _country_tokens(value: str) -> list[str]:
    cleaned = (
        value.replace(" and ", ",")
        .replace("&", ",")
        .replace("/", ",")
        .replace(";", ",")
    )
    return [token.strip() for token in cleaned.split(",") if token.strip()]


def _country_label_to_names(label: str) -> list[str]:
    """Extract one or more country names from a region selector label."""
    name = _collapse_ws(label.split("(", 1)[0])
    if not name:
        return []
    if name.lower() in {"latin america", "southern african region", "europe"}:
        return []
    return _country_tokens(name)


def _token_to_iso(token: str) -> str | None:
    token = re.sub(r"^the\s+", "", token.strip(), flags=re.IGNORECASE)
    normalized = _slug(token)
    if not normalized:
        return None
    if len(normalized) == 2:
        return normalized.upper()
    return COUNTRY_TOKEN_ALIASES.get(normalized)


def build_region_country_mapping(options: dict[str, str]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for locale, label in options.items():
        names = _country_label_to_names(label)
        derived_iso = locale.split("-")[-1].upper() if "-" in locale else "US"
        iso_codes: list[str] = []
        for name in names:
            iso = _token_to_iso(name)
            if iso and iso not in iso_codes:
                iso_codes.append(iso)

        # Fallback for single-country locale variants.
        if not iso_codes and len(derived_iso) == 2 and derived_iso.isalpha():
            if label.split("(", 1)[0].strip().lower() not in {
                "latin america",
                "southern african region",
                "europe",
                "french territories",
            }:
                iso_codes.append(derived_iso)

        if len(iso_codes) == 1:
            mapping[label] = {
                "type": "country_variant",
                "iso_codes": iso_codes,
                "locale": locale,
            }
        elif len(iso_codes) > 1:
            mapping[label] = {
                "type": "multi_country_region",
                "iso_codes": iso_codes,
                "locale": locale,
            }
        else:
            mapping[label] = {
                "type": "aggregate_region",
                "region_code": _slug(label.upper())[:16] or "REGION",
                "locale": locale,
            }
    return mapping


def build_country_alias_map(region_mapping: dict[str, dict[str, Any]]) -> dict[str, str]:
    aliases = dict(COUNTRY_TOKEN_ALIASES)
    for label, info in region_mapping.items():
        iso_codes = [str(code).upper() for code in info.get("iso_codes", []) if code]
        if not iso_codes:
            continue
        names = _country_label_to_names(label)
        for idx, name in enumerate(names):
            if idx >= len(iso_codes):
                break
            aliases[_slug(name)] = iso_codes[idx]
    return aliases


def parse_country_applicability(
    countries_text: str | None,
    *,
    alias_map: dict[str, str],
) -> CountryApplicability:
    if not countries_text:
        return CountryApplicability(ambiguous=True)

    text = _collapse_ws(countries_text)
    lower = text.lower()
    if not text:
        return CountryApplicability(ambiguous=True)
    if "..." in text:
        return CountryApplicability(ambiguous=True)

    if "global" in lower or "worldwide" in lower:
        return CountryApplicability(all_countries=True)

    all_except_match = re.search(
        r"\ball(?P<prefix>[^.]*)countries?\s+except\s+(?P<exclusions>.+)$",
        text,
        flags=re.I,
    )
    if all_except_match:
        prefix = _collapse_ws(all_except_match.group("prefix"))
        excluded_raw = all_except_match.group("exclusions")
        excluded: set[str] = set()
        for token in _country_tokens(excluded_raw):
            iso = alias_map.get(_slug(token)) or _token_to_iso(token)
            if iso:
                excluded.add(iso)
        prefix_is_plain_all = not prefix or prefix.lower() in {"the"}
        if not prefix_is_plain_all:
            return CountryApplicability(exclude=excluded, ambiguous=True)
        if excluded:
            return CountryApplicability(all_countries=True, exclude=excluded)
        return CountryApplicability(ambiguous=True)

    includes: set[str] = set()
    unknown_tokens = 0
    for token in _country_tokens(text):
        iso = alias_map.get(_slug(token)) or _token_to_iso(token)
        if iso:
            includes.add(iso)
        else:
            unknown_tokens += 1

    if includes and unknown_tokens == 0:
        return CountryApplicability(include=includes)
    if includes and unknown_tokens > 0:
        return CountryApplicability(include=includes, ambiguous=True)
    return CountryApplicability(ambiguous=True)


def pick_latest_release(releases: list[ReleaseCard]) -> ReleaseCard | None:
    if not releases:
        return None

    def _sort_key(card: ReleaseCard) -> tuple[datetime, tuple[Any, ...], str]:
        if card.release_date:
            date_obj = datetime.strptime(card.release_date, "%Y-%m-%d")
        else:
            date_obj = datetime(1970, 1, 1)
        return (date_obj, _version_sort_key(card.version), card.media_id or "")

    return max(releases, key=_sort_key)


def build_release_urls_by_locale(
    *,
    locales: list[str],
    media_id: str,
    langcode: str,
    docs_path: str,
) -> dict[str, str]:
    urls: dict[str, str] = {}
    for locale in locales:
        normalized = _normalize_locale(locale)
        prefix = "" if normalized == "en" else f"/{normalized}"
        path = f"{prefix}{docs_path}"
        urls[normalized] = _with_query(
            f"{BASE_URL}{path}",
            {"media_id": media_id, "langcode": langcode or "und"},
        )
    return urls


def card_to_runtime_entry(
    *,
    card: ReleaseCard,
    topic_id: int,
    locales: list[str],
    docs_path: str,
) -> dict[str, Any]:
    media_id = card.media_id or ""
    langcode = card.langcode or "und"
    urls_by_locale = (
        build_release_urls_by_locale(
            locales=locales,
            media_id=media_id,
            langcode=langcode,
            docs_path=docs_path,
        )
        if media_id
        else {}
    )
    return {
        "version": card.version,
        "release_date": card.release_date,
        "media_id": media_id or None,
        "document_topic_id": topic_id,
        "countries_text": card.countries_text,
        "urls_by_locale": urls_by_locale,
        "summary": card.summary,
        "title": card.title,
    }


def crawl_release_cards(
    *,
    apps_url: str,
    product_type: str,
    topic_id: int,
    product_media_name_id: int,
    timeout: int,
    max_pages: int,
) -> tuple[list[ReleaseCard], list[str]]:
    page_url = _with_query(
        apps_url,
        {
            "product_type": product_type,
            "f[0]": f"document:{topic_id}",
            "f[1]": f"product_media_name:{product_media_name_id}",
            "search_api_language": "en",
            "field_alternative_language": "en",
            "page": "0",
        },
    )

    cards: list[ReleaseCard] = []
    seen_pages: set[str] = set()
    visited_pages: list[str] = []

    while page_url and page_url not in seen_pages and len(seen_pages) < max_pages:
        seen_pages.add(page_url)
        visited_pages.append(page_url)
        page_html = fetch_text(page_url, timeout=timeout)
        cards.extend(parse_release_cards(page_html))
        page_url = find_next_page_url(page_url, page_html)

    deduped: dict[tuple[str | None, str | None, str | None, str], ReleaseCard] = {}
    for card in cards:
        key = (card.media_id, card.version, card.release_date, card.title)
        if key not in deduped:
            deduped[key] = card
    return list(deduped.values()), visited_pages


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch_json(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> Any:
    return json.loads(fetch_text(url, timeout=timeout))


def fetch_previous_runtime_catalog(*, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    try:
        payload = fetch_json(PREVIOUS_RUNTIME_CATALOG_URL, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def catalogs_equal_ignoring_generated_at(
    current_catalog: dict[str, Any],
    previous_catalog: dict[str, Any] | None,
) -> bool:
    if not isinstance(previous_catalog, dict):
        return False
    current_normalized = dict(current_catalog)
    previous_normalized = dict(previous_catalog)
    current_normalized.pop("generated_at", None)
    previous_normalized.pop("generated_at", None)
    return current_normalized == previous_normalized


def choose_generated_at(
    *,
    current_catalog: dict[str, Any],
    previous_catalog: dict[str, Any] | None,
    fallback_generated_at: str,
) -> str:
    if not catalogs_equal_ignoring_generated_at(current_catalog, previous_catalog):
        return fallback_generated_at
    if isinstance(previous_catalog, dict):
        previous_generated = previous_catalog.get("generated_at")
        if isinstance(previous_generated, str) and previous_generated.strip():
            return previous_generated.strip()
    return fallback_generated_at


def build_catalog(output_dir: Path, *, timeout: int, max_pages: int) -> None:
    generated_at = _now_utc_iso()
    root_url = f"{BASE_URL}{ROOT_PATH}"
    root_html = fetch_text(root_url, timeout=timeout)
    docs_path, discovered_product_type = discover_apps_entrypoint(root_html)

    apps_url = urljoin(BASE_URL, docs_path)
    apps_bootstrap_url = _with_query(apps_url, {"product_type": discovered_product_type})
    apps_html = fetch_text(apps_bootstrap_url, timeout=timeout)

    product_type = parse_product_type_from_apps_page(apps_html) or discovered_product_type
    product_facets = parse_facet_values(apps_html, "product_media_name")
    topic_facets = parse_facet_values(apps_html, "document")
    topic_id = topic_facets.get("Release notes")
    if topic_id is None:
        raise RuntimeError("Could not discover release-notes topic id from apps page")

    product_ids: dict[str, int] = {}
    for device_key, product_label in TARGET_PRODUCTS.items():
        product_id = product_facets.get(product_label)
        if product_id is None:
            raise RuntimeError(f"Could not discover product id for '{product_label}'")
        product_ids[device_key] = product_id

    language_options = parse_language_options(apps_html, "search_api_language")
    alt_language_options = parse_language_options(apps_html, "field_alternative_language")
    locale_options = dict(language_options)
    locale_options.update(alt_language_options)
    locale_options.setdefault("en", "United States (EN)")

    region_mapping = build_region_country_mapping(locale_options)
    alias_map = build_country_alias_map(region_mapping)

    locales = sorted({*locale_options.keys(), "en"})
    locale_countries = {
        str(code).upper()
        for info in region_mapping.values()
        for code in info.get("iso_codes", [])
        if code
    }

    devices_catalog: dict[str, Any] = {}
    crawl_meta: dict[str, Any] = {}
    all_country_codes: set[str] = set(locale_countries)
    for device_key, product_id in product_ids.items():
        cards, visited_pages = crawl_release_cards(
            apps_url=apps_url,
            product_type=product_type,
            topic_id=topic_id,
            product_media_name_id=product_id,
            timeout=timeout,
            max_pages=max_pages,
        )
        crawl_meta[device_key] = {
            "pages": visited_pages,
            "count": len(cards),
        }

        latest_global_card = pick_latest_release(cards)
        latest_global = (
            card_to_runtime_entry(
                card=latest_global_card,
                topic_id=topic_id,
                locales=locales,
                docs_path=docs_path,
            )
            if latest_global_card
            else None
        )

        latest_by_country: dict[str, Any] = {}
        applicability_by_card: dict[int, CountryApplicability] = {}
        for idx, card in enumerate(cards):
            applicability_by_card[idx] = parse_country_applicability(
                card.countries_text,
                alias_map=alias_map,
            )

        device_country_codes = set(locale_countries)
        for applicability in applicability_by_card.values():
            device_country_codes.update(applicability.include)
            device_country_codes.update(applicability.exclude)
        all_country_codes.update(device_country_codes)

        for country_code in sorted(device_country_codes):
            country_candidates: list[ReleaseCard] = []
            for idx, card in enumerate(cards):
                applicability = applicability_by_card[idx]
                if applicability.ambiguous:
                    continue
                if applicability.all_countries and country_code not in applicability.exclude:
                    country_candidates.append(card)
                elif country_code in applicability.include:
                    country_candidates.append(card)

            best = pick_latest_release(country_candidates)
            if best is None:
                if latest_global is not None:
                    latest_by_country[country_code] = dict(latest_global)
                continue

            latest_by_country[country_code] = card_to_runtime_entry(
                card=best,
                topic_id=topic_id,
                locales=locales,
                docs_path=docs_path,
            )

        devices_catalog[device_key] = {
            "product_media_name_id": product_id,
            "document_topic_id": topic_id,
            "latest_by_country": latest_by_country,
            "latest_global": latest_global,
        }

    runtime_catalog = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "source": {
            "type": "enphase_documentation_center",
            "entrypoint": root_url,
            "apps_url": apps_url,
            "product_type": int(product_type),
            "crawl": crawl_meta,
        },
        "devices": devices_catalog,
    }
    previous_runtime_catalog = fetch_previous_runtime_catalog(timeout=timeout)
    generated_at = choose_generated_at(
        current_catalog=runtime_catalog,
        previous_catalog=previous_runtime_catalog,
        fallback_generated_at=generated_at,
    )
    runtime_catalog["generated_at"] = generated_at

    # Source metadata artifacts.
    sources_dir = output_dir / "sources" / "enphase_doc_center"
    write_json(
        sources_dir / "entrypoints.json",
        {
            "generated_at": generated_at,
            "root": root_url,
            "apps": apps_url,
            "product_type": int(product_type),
        },
    )
    write_json(
        sources_dir / "facet_ids.json",
        {
            "generated_at": generated_at,
            "document": topic_facets,
            "release_notes_topic_id": topic_id,
        },
    )
    write_json(
        sources_dir / "product_media_name_ids.json",
        {
            "generated_at": generated_at,
            "products": product_facets,
            "targets": {
                key: {
                    "label": TARGET_PRODUCTS[key],
                    "product_media_name_id": product_ids[key],
                }
                for key in TARGET_PRODUCTS
            },
        },
    )
    write_json(
        sources_dir / "regions_raw.json",
        {
            "generated_at": generated_at,
            "search_api_language": language_options,
            "field_alternative_language": alt_language_options,
        },
    )
    write_json(
        sources_dir / "region_to_country_codes.json",
        {
            "generated_at": generated_at,
            "mapping": region_mapping,
        },
    )

    write_json(output_dir / "catalog" / "v1" / "runtime_catalog.json", runtime_catalog)

    # Optional per-country debug catalogs.
    for country_code in sorted(all_country_codes):
        per_country = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "country": country_code,
            "source": runtime_catalog["source"],
            "devices": {
                key: {
                    "product_media_name_id": value["product_media_name_id"],
                    "document_topic_id": value["document_topic_id"],
                    "latest": value["latest_by_country"].get(country_code)
                    or value["latest_global"],
                }
                for key, value in devices_catalog.items()
            },
        }
        write_json(output_dir / "data" / country_code / "catalog.json", per_country)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Enphase firmware catalog")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Output directory for generated sources/catalog data",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="HTTP timeout seconds",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help="Maximum paginated result pages per product",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output_dir = Path(args.output_dir).resolve()
    try:
        build_catalog(output_dir, timeout=args.timeout, max_pages=args.max_pages)
    except Exception as err:  # noqa: BLE001
        print(f"Failed to build firmware catalog: {err}", file=sys.stderr)
        return 1

    print(f"Firmware catalog generated at {output_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
