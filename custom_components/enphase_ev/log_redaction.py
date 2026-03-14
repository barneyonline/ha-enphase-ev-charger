from __future__ import annotations

import re
from collections.abc import Iterable

_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC_RE = re.compile(r"(?i)\b(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}\b")
_DEBUG_KV_RE = re.compile(
    r"(?P<key>[A-Za-z][A-Za-z0-9_\-]*)(?P<sep>\s*[=:]\s*)(?P<value>[^,\s)]+)"
)


def truncate_identifier(value: object) -> str | None:
    """Return a short, non-reversible identifier for logs."""

    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None
    if len(text) <= 2:
        return "[redacted]"
    if len(text) <= 8:
        return f"{text[:1]}...{text[-1:]}"
    return f"{text[:4]}...{text[-4:]}"


def redact_identifier(value: object) -> str:
    """Return a redacted log-safe identifier."""

    return truncate_identifier(value) or "[redacted]"


def redact_site_id(value: object) -> str:
    """Return a stable site marker for logs."""

    if value is None:
        return "[site]"
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return "[site]"
    return "[site]" if text else "[site]"


def _key_kind(key: object) -> str:
    try:
        key_text = str(key).strip().lower()
    except Exception:  # noqa: BLE001
        return "text"
    compact = "".join(ch for ch in key_text if ch.isalnum())
    if not compact:
        return "text"
    if compact in {"site", "siteid", "sitename"}:
        return "site"
    if compact in {"entityid"}:
        return "text"
    if any(
        token in compact
        for token in ("token", "auth", "cookie", "email", "user", "pass", "secret")
    ):
        return "redact"
    if any(
        token in compact
        for token in (
            "ip",
            "mac",
            "host",
            "hostname",
            "apn",
            "imei",
            "imsi",
            "iccid",
            "devicelink",
        )
    ):
        return "redact"
    if any(
        token in compact
        for token in ("serial", "deviceuid", "uid", "uuid", "hemsdeviceid")
    ):
        return "truncate"
    if compact.endswith(("id", "ids")):
        return "truncate"
    return "text"


def _redact_kv_match(match: re.Match[str]) -> str:
    key = match.group("key")
    sep = match.group("sep")
    value = match.group("value")
    kind = _key_kind(key)
    if kind == "site":
        safe_value = "[site]"
    elif kind == "redact":
        safe_value = "[redacted]"
    elif kind == "truncate":
        safe_value = redact_identifier(value)
    else:
        safe_value = value
    return f"{key}{sep}{safe_value}"


def _normalize_iterable(values: Iterable[object] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or ():
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            continue
        if text:
            normalized.append(text)
    return normalized


def redact_text(
    value: object,
    *,
    site_ids: Iterable[object] | None = None,
    identifiers: Iterable[object] | None = None,
    max_length: int = 512,
) -> str:
    """Return compact text with common Enphase identifiers removed."""

    try:
        text = " ".join(str(value or "").split()).strip()
    except Exception:  # noqa: BLE001
        return ""
    if not text:
        return ""

    for identifier in _normalize_iterable(identifiers):
        text = text.replace(identifier, redact_identifier(identifier))

    for site_id in _normalize_iterable(site_ids):
        text = text.replace(site_id, "[site]")

    text = _EMAIL_RE.sub("[redacted]", text)
    text = _IPV4_RE.sub("[redacted]", text)
    text = _MAC_RE.sub("[redacted]", text)
    text = _DEBUG_KV_RE.sub(_redact_kv_match, text)
    if len(text) > max_length:
        text = f"{text[:max_length]}..."
    return text
