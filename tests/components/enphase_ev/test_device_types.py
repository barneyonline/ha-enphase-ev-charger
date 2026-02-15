from __future__ import annotations

from custom_components.enphase_ev.device_types import (
    member_is_retired,
    normalize_type_key,
    parse_type_identifier,
    sanitize_member,
    type_display_label,
    type_identifier,
)
import custom_components.enphase_ev.device_types as device_types_mod


def test_normalize_type_key_handles_aliases_and_unknown_tokens() -> None:
    assert normalize_type_key(" IQEVSE ") == "iqevse"
    assert normalize_type_key("EV Chargers") == "iqevse"
    assert normalize_type_key("microinverters") == "microinverter"
    assert normalize_type_key("meter") == "envoy"
    assert normalize_type_key("enpower") == "envoy"
    assert normalize_type_key("systemcontroller") == "envoy"
    assert normalize_type_key("wind-turbine") == "wind_turbine"
    assert normalize_type_key("___") is None
    assert normalize_type_key("") is None
    assert normalize_type_key(None) is None


def test_normalize_type_key_handles_bad_string_conversion() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert normalize_type_key(BadStr()) is None


def test_type_display_label_uses_known_and_title_case_defaults() -> None:
    assert type_display_label("envoy") == "Gateway"
    assert type_display_label("wind_turbine") == "Wind Turbine"
    assert type_display_label(None) is None


def test_type_identifier_round_trip_parsing() -> None:
    identifier = type_identifier("SITE123", "evse")
    assert identifier == ("enphase_ev", "type:SITE123:iqevse")
    assert parse_type_identifier(identifier[1]) == ("SITE123", "iqevse")
    assert parse_type_identifier("type:SITE123:meter") == ("SITE123", "envoy")
    assert parse_type_identifier("type:SITE123:enpower") == ("SITE123", "envoy")
    assert parse_type_identifier("type::iqevse") is None
    assert parse_type_identifier("site:SITE123") is None


def test_type_identifier_handles_bad_site_value() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert type_identifier(BadStr(), "iqevse") is None
    assert type_identifier("   ", "iqevse") is None
    assert type_identifier("site", None) is None


def test_type_display_label_empty_word_path(monkeypatch) -> None:
    monkeypatch.setattr(device_types_mod, "normalize_type_key", lambda _value: "_")
    assert device_types_mod.type_display_label("anything") is None


def test_parse_type_identifier_handles_bad_input_shapes() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert parse_type_identifier(None) is None
    assert parse_type_identifier(BadStr()) is None
    assert parse_type_identifier("type:only-two-parts") is None


def test_member_is_retired_detects_status_variants() -> None:
    assert member_is_retired({"status": "retired"}) is True
    assert member_is_retired({"statusText": "Retired"}) is True
    assert member_is_retired({"status_text": "RETIRED"}) is True
    assert member_is_retired({"isRetired": True}) is True
    assert member_is_retired({"status": "normal"}) is False


def test_member_is_retired_handles_non_dict_and_bad_status_value() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert member_is_retired("not-a-dict") is False
    assert member_is_retired({"status": BadStr()}) is False


def test_sanitize_member_keeps_scalars_and_stable_order() -> None:
    member = {
        "name": " Battery 1 ",
        "serial_number": "BAT-1",
        "status": "Normal",
        "nested": {"skip": True},
        "ports": [1, 2],
        "custom_flag": True,
        "watts": 12.5,
    }

    out = sanitize_member(member)

    assert out["name"] == "Battery 1"
    assert out["serial_number"] == "BAT-1"
    assert out["status"] == "Normal"
    assert out["custom_flag"] is True
    assert out["watts"] == 12.5
    assert "nested" not in out
    assert "ports" not in out


def test_sanitize_member_handles_non_dict_and_non_scalar_values() -> None:
    out = sanitize_member(
        {
            "name": None,
            "status": {"nested": "ignored"},
            "serial_number": "BAT-2",
            "extra": {"nested": "ignored"},
        }
    )
    assert out["name"] is None
    assert out["serial_number"] == "BAT-2"
    assert "status" not in out
    assert "extra" not in out
    assert sanitize_member("not-a-dict") == {}
