from __future__ import annotations

from custom_components.enphase_ev.log_redaction import (
    _key_kind,
    redact_identifier,
    redact_site_id,
    redact_text,
    truncate_identifier,
)


def test_identifier_helpers_redact_values() -> None:
    assert truncate_identifier("SERIAL-12345678") == "SERI...5678"
    assert truncate_identifier("   ") is None
    assert redact_identifier("ABCD") == "A...D"
    assert redact_identifier(None) == "[redacted]"
    assert redact_site_id("123456789") == "[site]"
    assert redact_site_id(None) == "[site]"


def test_redact_text_scrubs_common_sensitive_values() -> None:
    text = (
        "site_id=12345 serialNumber=SERIAL-12345678 uid=DEVICE-UID-9999 "
        "email=user@example.com ip=10.0.0.2 mac=AA:BB:CC:DD:EE:FF plain 12345"
    )

    redacted = redact_text(
        text,
        site_ids=("12345",),
        identifiers=("SERIAL-12345678", "DEVICE-UID-9999"),
    )

    assert "12345" not in redacted
    assert "SERIAL-12345678" not in redacted
    assert "DEVICE-UID-9999" not in redacted
    assert "user@example.com" not in redacted
    assert "10.0.0.2" not in redacted
    assert "AA:BB:CC:DD:EE:FF" not in redacted
    assert "site_id=[site]" in redacted
    assert "serialNumber=SERI...5678" in redacted
    assert "uid=DEVI...9999" in redacted
    assert redacted.count("[redacted]") >= 3


def test_identifier_helpers_cover_bad_string_inputs() -> None:
    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert truncate_identifier(_BadStr()) is None
    assert redact_site_id(_BadStr()) == "[site]"


def test_redact_text_covers_key_kinds_and_truncation() -> None:
    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    redacted = redact_text(
        (
            "entityId=sensor.foo site=123 sitename=Home token=secret host=router "
            "serial=SERIAL-12345678 chargerId=ABCDEFGH user=tester"
        ),
        site_ids=("123", _BadStr()),
        identifiers=("SERIAL-12345678", _BadStr()),
    )

    assert "entityId=sensor.foo" in redacted
    assert "site=[site]" in redacted
    assert "sitename=[site]" in redacted
    assert "token=[redacted]" in redacted
    assert "host=[redacted]" in redacted
    assert "serial=SERI...5678" in redacted
    assert "chargerId=A...H" in redacted
    assert "user=[redacted]" in redacted
    assert redact_text("x" * 40, max_length=10) == ("x" * 10) + "..."
    assert redact_text(_BadStr()) == ""


def test_key_kind_covers_non_stringable_and_empty_compact_keys() -> None:
    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert _key_kind(_BadStr()) == "text"
    assert _key_kind("---") == "text"
