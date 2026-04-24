from __future__ import annotations

from custom_components.enphase_ev.battery_runtime_dry_contact import (
    dry_contact_member_is_dry_contact,
    match_dry_contact_settings,
    parse_dry_contact_settings_payload,
)


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return None


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def test_parse_dry_contact_settings_payload_normalizes_aliases_and_unmatched() -> None:
    members = [
        {
            "serial_number": "envoy-1",
            "device_uid": "uid-1",
            "contact_id": "1",
            "channel_type": "nc1",
        }
    ]

    result = parse_dry_contact_settings_payload(
        {
            "data": {
                "settings": [
                    {
                        "serialNumber": "envoy-1",
                        "deviceUid": "uid-1",
                        "contactId": "1",
                        "channelType": "NC1",
                        "configuredName": "Load shed",
                        "overrideSupported": "true",
                        "overrideActive": "false",
                        "pollingIntervalSeconds": "30",
                        "socThreshold": "25",
                        "socThresholdMin": "10",
                        "socThresholdMax": "90",
                        "scheduleWindows": [
                            {"startTime": "01:00", "endTime": "02:00"},
                            {"startTime": "01:00", "endTime": "02:00"},
                        ],
                    },
                    {
                        "serial": "envoy-2",
                        "contactId": "2",
                        "scheduleStart": "03:00",
                        "scheduleEnd": "04:00",
                    },
                ]
            }
        },
        members=members,
        coerce_bool=_coerce_bool,
        coerce_int=_coerce_int,
        coerce_text=_coerce_text,
    )

    assert result.supported is True
    assert result.entries == [
        {
            "serial_number": "envoy-1",
            "device_uid": "uid-1",
            "contact_id": "1",
            "channel_type": "NC1",
            "configured_name": "Load shed",
            "override_supported": True,
            "override_active": False,
            "polling_interval_seconds": 30,
            "soc_threshold": 25,
            "soc_threshold_min": 10,
            "soc_threshold_max": 90,
            "schedule_windows": [{"start": "01:00", "end": "02:00"}],
        },
        {
            "serial_number": "envoy-2",
            "contact_id": "2",
            "schedule_windows": [{"start": "03:00", "end": "04:00"}],
        },
    ]
    assert result.unmatched == [
        {
            "serial_number": "envoy-2",
            "contact_id": "2",
            "schedule_windows": [{"start": "03:00", "end": "04:00"}],
        }
    ]


def test_parse_dry_contact_settings_payload_invalid_is_unsupported() -> None:
    result = parse_dry_contact_settings_payload(
        ["bad"],
        members=[],
        coerce_bool=_coerce_bool,
        coerce_int=_coerce_int,
        coerce_text=_coerce_text,
    )

    assert result.supported is False
    assert result.entries == []
    assert result.unmatched == []


def test_dry_contact_matching_copies_results_and_detects_relays() -> None:
    members = [
        {"serial_number": "envoy-1", "channel_type": "NO1"},
        {"serial_number": "envoy-2", "name": "Aux relay NC2"},
    ]
    settings = [{"serial_number": "envoy-1", "channel_type": "NO1"}]

    matches, unmatched = match_dry_contact_settings(
        members, settings_entries=settings, coerce_text=_coerce_text
    )
    settings[0]["channel_type"] = "changed"

    assert matches == [{"serial_number": "envoy-1", "channel_type": "NO1"}, None]
    assert unmatched == []
    assert dry_contact_member_is_dry_contact(members[0], _coerce_text) is True
    assert dry_contact_member_is_dry_contact(members[1], _coerce_text) is True
