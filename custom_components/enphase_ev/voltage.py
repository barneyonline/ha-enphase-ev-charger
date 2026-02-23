from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from .const import DEFAULT_NOMINAL_VOLTAGE

COUNTRY_NOMINAL_VOLTAGE: dict[str, int] = {
    "AU": 230,
    "BG": 230,
    "BR": 220,
    "CA": 120,
    "CZ": 230,
    "DE": 230,
    "DK": 230,
    "EE": 230,
    "ES": 230,
    "FI": 230,
    "FR": 230,
    "GB": 230,
    "GR": 230,
    "HU": 230,
    "IE": 230,
    "IT": 230,
    "LT": 230,
    "LV": 230,
    "NL": 230,
    "NO": 230,
    "NZ": 230,
    "PL": 230,
    "RO": 230,
    "SE": 230,
    "US": 120,
}

LANGUAGE_DEFAULT_COUNTRY: dict[str, str] = {
    "bg": "BG",
    "cs": "CZ",
    "da": "DK",
    "de": "DE",
    "el": "GR",
    "en-au": "AU",
    "en-ca": "CA",
    "en-gb": "GB",
    "en-ie": "IE",
    "en-nz": "NZ",
    "en-us": "US",
    "es": "ES",
    "et": "EE",
    "fi": "FI",
    "fr": "FR",
    "hu": "HU",
    "it": "IT",
    "lt": "LT",
    "lv": "LV",
    "nb": "NO",
    "nl": "NL",
    "no": "NO",
    "pl": "PL",
    "pt-br": "BR",
    "ro": "RO",
    "sv": "SE",
}


def coerce_nominal_voltage(value: object) -> int | None:
    """Parse a nominal voltage value from config options."""
    if value is None:
        return None
    try:
        parsed = int(round(float(str(value).strip())))
    except Exception:  # noqa: BLE001
        return None
    if parsed <= 0:
        return None
    return parsed


def resolve_nominal_voltage_for_locale(
    *,
    country: object | None = None,
    language: object | None = None,
) -> int:
    """Resolve locale-specific nominal voltage using country then language."""
    country_code = _normalize_country(country)
    if country_code is not None:
        configured = COUNTRY_NOMINAL_VOLTAGE.get(country_code)
        if configured is not None:
            return configured

    language_tag = _normalize_language(language)
    if language_tag is not None:
        inferred_country = _country_from_language(language_tag)
        if inferred_country is not None:
            configured = COUNTRY_NOMINAL_VOLTAGE.get(inferred_country)
            if configured is not None:
                return configured

    return DEFAULT_NOMINAL_VOLTAGE


def resolve_nominal_voltage_for_hass(hass: Any) -> int:
    """Resolve locale-specific nominal voltage from Home Assistant config."""
    config = getattr(hass, "config", None)
    country = None
    language = None
    try:
        country = getattr(config, "country", None)
    except Exception:  # noqa: BLE001
        country = None
    try:
        language = getattr(config, "language", None)
    except Exception:  # noqa: BLE001
        language = None
    return resolve_nominal_voltage_for_locale(country=country, language=language)


def preferred_operating_voltage(values: Iterable[object]) -> int | None:
    """Pick the most common valid API operating voltage."""
    observed: list[int] = []
    for value in values:
        parsed = coerce_nominal_voltage(value)
        if parsed is not None:
            observed.append(parsed)
    if not observed:
        return None
    counts = Counter(observed)
    return counts.most_common(1)[0][0]


def _normalize_country(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        code = str(value).strip().upper()
    except Exception:  # noqa: BLE001
        return None
    if not code:
        return None
    return code


def _normalize_language(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        tag = str(value).strip().replace("_", "-").lower()
    except Exception:  # noqa: BLE001
        return None
    if not tag:
        return None
    return tag


def _country_from_language(language_tag: str) -> str | None:
    mapped = LANGUAGE_DEFAULT_COUNTRY.get(language_tag)
    if mapped is not None:
        return mapped

    parts = language_tag.split("-")
    if len(parts) > 1:
        region = parts[-1].upper()
        if region in COUNTRY_NOMINAL_VOLTAGE:
            return region
    return LANGUAGE_DEFAULT_COUNTRY.get(parts[0])
