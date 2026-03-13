from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _seed_dry_contact_members(coord) -> None:
    coord._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contacts",
                "count": 2,
                "devices": [
                    {
                        "name": "Dry Contact 1",
                        "serial_number": "DC-1",
                        "channel_type": "dry_contact_1",
                        "device_uid": "UID-1",
                        "contact_id": "CID-1",
                    },
                    {
                        "name": "Dry Contact 2",
                        "serial_number": "DC-2",
                        "channel_type": "dry_contact_2",
                        "device_uid": "UID-2",
                        "contact_id": "CID-2",
                    },
                ],
            },
        },
        ["envoy", "dry_contact"],
    )


def test_parse_dry_contact_settings_payload_normalizes_and_tracks_unmatched(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    _seed_dry_contact_members(coord)

    coord._parse_dry_contact_settings_payload(  # noqa: SLF001
        {
            "data": {
                "contacts": [
                    {
                        "serial": "DC-1",
                        "displayName": "Solar Diverter",
                        "overrideSupported": True,
                        "overrideActive": False,
                        "controlMode": "soc_threshold",
                        "pollingInterval": 30,
                        "socThreshold": 45,
                        "socThresholdMin": 20,
                        "socThresholdMax": 80,
                        "scheduleWindows": [
                            {"startTime": "22:00", "endTime": "06:00"},
                        ],
                    },
                    {
                        "channelType": "dry_contact_9",
                        "name": "Spare Contact",
                        "overrideSupported": False,
                    },
                ]
            }
        }
    )

    assert coord.dry_contact_settings_supported is True
    entries = coord.dry_contact_settings_entries()
    assert len(entries) == 2
    assert entries[0]["serial_number"] == "DC-1"
    assert entries[0]["configured_name"] == "Solar Diverter"
    assert entries[0]["override_supported"] is True
    assert entries[0]["override_active"] is False
    assert entries[0]["control_mode"] == "soc_threshold"
    assert entries[0]["polling_interval_seconds"] == 30
    assert entries[0]["soc_threshold"] == 45
    assert entries[0]["soc_threshold_min"] == 20
    assert entries[0]["soc_threshold_max"] == 80
    assert entries[0]["schedule_windows"] == [{"start": "22:00", "end": "06:00"}]

    matches, unmatched = coord.dry_contact_settings_matches(
        [
            {
                "serial_number": "DC-1",
                "channel_type": "dry_contact_1",
                "name": "Dry Contact 1",
            },
            {
                "serial_number": "DC-2",
                "channel_type": "dry_contact_2",
                "name": "Dry Contact 2",
            },
        ]
    )
    assert matches[0] is not None
    assert matches[0]["configured_name"] == "Solar Diverter"
    assert matches[1] is None
    assert unmatched == coord.dry_contact_unmatched_settings()
    assert unmatched[0]["configured_name"] == "Spare Contact"


def test_parse_dry_contact_settings_payload_invalid_marks_unsupported(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])

    coord._parse_dry_contact_settings_payload(["bad"])  # noqa: SLF001

    assert coord.dry_contact_settings_supported is False
    assert coord.dry_contact_settings_entries() == []
    assert coord.dry_contact_unmatched_settings() == []


def test_parse_dry_contact_settings_payload_empty_dict_marks_supported_with_no_entries(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])

    coord._parse_dry_contact_settings_payload({})  # noqa: SLF001

    assert coord.dry_contact_settings_supported is True
    assert coord.dry_contact_settings_entries() == []
    assert coord.dry_contact_unmatched_settings() == []


def test_dry_contact_settings_helper_edge_cases(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])

    assert coord._dry_contact_settings_is_stale() is True  # noqa: SLF001

    coord._dry_contact_settings_supported = True  # noqa: SLF001
    coord._dry_contact_settings_last_success_mono = time.monotonic() + 5  # noqa: SLF001
    assert coord._dry_contact_settings_is_stale() is False  # noqa: SLF001
    coord._dry_contact_settings_last_success_mono = (
        time.monotonic() - 999
    )  # noqa: SLF001
    assert coord.dry_contact_settings_supported is None

    coord._dry_contact_settings_entries = "bad"  # type: ignore[assignment]  # noqa: SLF001
    coord._dry_contact_unmatched_settings = "bad"  # type: ignore[assignment]  # noqa: SLF001
    assert coord.dry_contact_settings_entries() == []
    assert coord.dry_contact_unmatched_settings() == []

    copied = coord._copy_dry_contact_settings_entry(  # noqa: SLF001
        {"nested": {"a": 1}, "windows": [{"start": "01:00", "end": "02:00"}]}
    )
    assert copied["nested"] == {"a": 1}
    assert copied["windows"] == [{"start": "01:00", "end": "02:00"}]

    assert coord._normalize_dry_contact_schedule_windows(  # noqa: SLF001
        {"startTime": "01:00", "endTime": "02:00"}
    ) == [{"start": "01:00", "end": "02:00"}]
    assert coord._normalize_dry_contact_schedule_windows(  # noqa: SLF001
        [{}, {"start": "01:00", "end": "02:00"}, {"start": "01:00", "end": "02:00"}]
    ) == [{"start": "01:00", "end": "02:00"}]
    assert coord._dry_contact_settings_looks_like_entry("bad") is False  # noqa: SLF001

    identities = coord._dry_contact_identity_candidates(  # noqa: SLF001
        {"serial": "DC-1", "configured_name": "Contact A", "name": "Contact A"}
    )
    assert identities[0] == ("serial_number", "dc-1")
    assert identities[-1] == ("name", "contact a")

    assert coord._dry_contact_member_is_dry_contact("bad") is False  # noqa: SLF001
    assert (
        coord._dry_contact_member_is_dry_contact({"channel_type": "NC1"}) is True
    )  # noqa: SLF001
    assert (
        coord._dry_contact_member_is_dry_contact({"name": "drycontactloads"}) is True
    )  # noqa: SLF001
    assert (
        coord._dry_contact_member_is_dry_contact({"name": "Load-control relay NO2"})
        is True
    )  # noqa: SLF001
    assert coord._dry_contact_member_dedupe_key({}, 3) == (
        ("idx", "3"),
    )  # noqa: SLF001
    assert (
        coord._dry_contact_match_conflicts(  # noqa: SLF001
            {"serial_number": "dc-1"},
            {"serial_number": "dc-2"},
        )
        is True
    )


def test_parse_dry_contact_settings_payload_edge_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    _seed_dry_contact_members(coord)
    loop: dict[str, object] = {}
    loop["self"] = loop

    coord._parse_dry_contact_settings_payload(  # noqa: SLF001
        {
            "data": {
                "deep": {"a": {"b": {"c": {"d": {"serial": "TOO-DEEP"}}}}},
                "loop": loop,
                "contacts": [
                    {
                        "device-uid": "DU-1",
                        "uid": "UID-ALT-1",
                        "contactId": "CID-ALT-1",
                        "displayName": "Contact Edge",
                        "scheduleStart": "07:00",
                        "scheduleEnd": "08:00",
                    },
                    {"windows": [{}]},
                    {
                        "device-uid": "DU-1",
                        "uid": "UID-ALT-1",
                        "contactId": "CID-ALT-1",
                        "displayName": "Contact Edge",
                        "scheduleStart": "07:00",
                        "scheduleEnd": "08:00",
                    },
                ],
            }
        }
    )

    entries = coord.dry_contact_settings_entries()
    assert entries == [
        {
            "device_uid": "DU-1",
            "uid": "UID-ALT-1",
            "contact_id": "CID-ALT-1",
            "configured_name": "Contact Edge",
            "schedule_windows": [{"start": "07:00", "end": "08:00"}],
        }
    ]


def test_dry_contact_members_for_settings_filters_invalid_and_duplicate_members(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "type_key": "envoy",
            "type_label": "Gateway",
            "count": 1,
            "devices": [
                {
                    "name": "Dry Contact Gateway",
                    "channel_type": "dry_contact_1",
                    "serial_number": "DC-ENV",
                },
                "bad",
                {"status": "retired"},
                {},
            ],
        },
        "dry_contact": {
            "type_key": "dry_contact",
            "type_label": "Dry Contacts",
            "count": 3,
            "devices": [
                "bad",
                {"serial_number": "DC-ENV", "channel_type": "dry_contact_1"},
                {"serial_number": "DC-ENV", "channel_type": "dry_contact_2"},
                {"serial_number": "DC-2", "channel_type": "dry_contact_2"},
                {"status": "retired"},
                {},
            ],
        },
    }

    members = coord._dry_contact_members_for_settings()  # noqa: SLF001

    assert members == [
        {
            "name": "Dry Contact Gateway",
            "channel_type": "dry_contact_1",
            "serial_number": "DC-ENV",
        },
        {
            "serial_number": "DC-ENV",
            "channel_type": "dry_contact_2",
        },
        {
            "serial_number": "DC-2",
            "channel_type": "dry_contact_2",
        },
    ]


def test_dry_contact_settings_matches_leave_ambiguous_same_serial_unmatched(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])

    matches, unmatched = coord._match_dry_contact_settings(  # noqa: SLF001
        [
            {"serial_number": "DC-1", "channel_type": "dry_contact_1"},
            {"serial_number": "DC-1", "channel_type": "dry_contact_2"},
        ],
        settings_entries=[
            {"serial_number": "DC-1", "configured_name": "Shared Serial Only"},
            {
                "serial_number": "DC-1",
                "channel_type": "dry_contact_2",
                "configured_name": "Contact Two",
            },
        ],
    )

    assert matches == [
        None,
        {
            "serial_number": "DC-1",
            "channel_type": "dry_contact_2",
            "configured_name": "Contact Two",
        },
    ]
    assert unmatched == [
        {"serial_number": "DC-1", "configured_name": "Shared Serial Only"}
    ]


def test_dry_contact_settings_matches_skip_conflicting_unique_candidate(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])

    matches, unmatched = coord._match_dry_contact_settings(  # noqa: SLF001
        [
            {
                "serial_number": "DC-1",
                "channel_type": "dry_contact_1",
            }
        ],
        settings_entries=[
            {
                "serial_number": "DC-1",
                "channel_type": "dry_contact_9",
                "configured_name": "Wrong Channel",
            }
        ],
    )

    assert matches == [None]
    assert unmatched == [
        {
            "serial_number": "DC-1",
            "channel_type": "dry_contact_9",
            "configured_name": "Wrong Channel",
        }
    ]


@pytest.mark.asyncio
async def test_refresh_dry_contact_settings_caches_and_redacts(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    _seed_dry_contact_members(coord)
    coord.client.dry_contacts_settings = AsyncMock(
        return_value={
            "data": {
                "contacts": [
                    {
                        "serial": "DC-1",
                        "displayName": "Solar Diverter",
                    }
                ]
            },
            "token": "secret-token",
        }
    )

    await coord._async_refresh_dry_contact_settings(force=True)  # noqa: SLF001

    assert coord._dry_contact_settings_payload is not None  # noqa: SLF001
    assert coord._dry_contact_settings_payload["token"] == "[redacted]"  # noqa: SLF001
    assert coord.dry_contact_settings_supported is True
    assert (
        coord.dry_contact_settings_entries()[0]["configured_name"] == "Solar Diverter"
    )

    coord._dry_contact_settings_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord.client.dry_contacts_settings.reset_mock()
    await coord._async_refresh_dry_contact_settings()  # noqa: SLF001
    coord.client.dry_contacts_settings.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_dry_contact_settings_wraps_non_dict_redaction(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.client.dry_contacts_settings = AsyncMock(return_value={"contacts": []})
    coord._redact_battery_payload = lambda _payload: "masked"  # type: ignore[method-assign]  # noqa: SLF001

    await coord._async_refresh_dry_contact_settings(force=True)  # noqa: SLF001

    assert coord._dry_contact_settings_payload == {"value": "masked"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_dry_contact_settings_failure_stale_and_recent_behavior(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._dry_contact_settings_supported = True  # noqa: SLF001
    coord._dry_contact_settings_last_success_mono = (
        time.monotonic() - 999
    )  # noqa: SLF001
    coord.client.dry_contacts_settings = AsyncMock(side_effect=RuntimeError("boom"))

    await coord._async_refresh_dry_contact_settings(force=True)  # noqa: SLF001

    assert coord.dry_contact_settings_supported is None
    assert coord._dry_contact_settings_failures == 1  # noqa: SLF001

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._dry_contact_settings_supported = True  # noqa: SLF001
    coord._dry_contact_settings_last_success_mono = time.monotonic()  # noqa: SLF001
    coord._dry_contact_settings_entries = [{"serial_number": "DC-1"}]  # noqa: SLF001
    coord.client.dry_contacts_settings = AsyncMock(side_effect=RuntimeError("boom"))

    await coord._async_refresh_dry_contact_settings(force=True)  # noqa: SLF001

    assert coord.dry_contact_settings_supported is True
    assert coord._dry_contact_settings_failures == 1  # noqa: SLF001
    assert coord.dry_contact_settings_entries() == [{"serial_number": "DC-1"}]


def test_collect_site_metrics_includes_dry_contact_settings_fields(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    _seed_dry_contact_members(coord)
    coord._parse_dry_contact_settings_payload(  # noqa: SLF001
        {
            "contacts": [
                {"serial": "DC-1", "name": "Solar Diverter"},
                {"channelType": "dry_contact_9", "name": "Spare Contact"},
            ]
        }
    )
    coord._dry_contact_settings_last_success_mono = (
        time.monotonic() - 1.0
    )  # noqa: SLF001

    metrics = coord.collect_site_metrics()

    assert metrics["dry_contact_settings_supported"] is True
    assert metrics["dry_contact_settings_contact_count"] == 2
    assert metrics["dry_contact_settings_unmatched_count"] == 1
    assert metrics["dry_contact_settings_fetch_failures"] == 0
    assert metrics["dry_contact_settings_data_stale"] is False
    assert "dry_contact_settings_last_success_age_s" in metrics


@pytest.mark.asyncio
async def test_update_data_ignores_dry_contact_settings_refresh_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord._async_refresh_dry_contact_settings = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("boom")
    )

    result = await coord._async_update_data()  # noqa: SLF001

    assert result == {}

    coord = coordinator_factory()
    coord.client.status = AsyncMock(return_value={"evChargerData": [], "ts": 0})
    coord._async_refresh_dry_contact_settings = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("boom")
    )

    await coord._async_update_data()  # noqa: SLF001
