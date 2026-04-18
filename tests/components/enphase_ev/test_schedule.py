from __future__ import annotations

from datetime import time

from custom_components.enphase_ev import schedule as schedule_mod


def test_normalize_slot_payload_filters_fields_and_defaults() -> None:
    slot = {
        "id": 123,
        "scheduleType": None,
        "enabled": "false",
        "days": ["1", 9, "bad", 2],
        "unknown": "ignored",
    }
    normalized = schedule_mod.normalize_slot_payload(slot)

    assert normalized["id"] == "123"
    assert normalized["scheduleType"] == "CUSTOM"
    assert normalized["enabled"] is False
    assert normalized["days"] == [1, 2]
    assert "unknown" not in normalized


def test_normalize_slot_payload_off_peak_defaults_days() -> None:
    slot = {"id": "slot-2", "scheduleType": "OFF_PEAK"}
    normalized = schedule_mod.normalize_slot_payload(slot)

    assert normalized["days"] == [1, 2, 3, 4, 5, 6, 7]


def test_normalize_slot_payload_formats_times() -> None:
    slot = {
        "id": "slot-3",
        "scheduleType": "CUSTOM",
        "startTime": time(8, 0),
        "endTime": time(9, 0),
    }
    normalized = schedule_mod.normalize_slot_payload(slot)

    assert normalized["startTime"] == "08:00"
    assert normalized["endTime"] == "09:00"


def test_normalize_slot_payload_coerces_int_fields() -> None:
    slot = {
        "id": "slot-4",
        "remindTime": "15",
        "chargingLevel": 32.0,
        "chargingLevelAmp": "40",
    }
    normalized = schedule_mod.normalize_slot_payload(slot)

    assert normalized["remindTime"] == 15
    assert normalized["chargingLevel"] == 32
    assert normalized["chargingLevelAmp"] == 40


def test_coerce_bool_variants() -> None:
    assert schedule_mod._coerce_bool(True) is True
    assert schedule_mod._coerce_bool("true") is True
    assert schedule_mod._coerce_bool("false") is False
    assert schedule_mod._coerce_bool("maybe") == "maybe"
    assert schedule_mod._coerce_bool(0) is False

    class Dummy:
        pass

    dummy = Dummy()
    assert schedule_mod._coerce_bool(dummy) is dummy


def test_coerce_int_variants() -> None:
    assert schedule_mod._coerce_int(None) is None
    assert schedule_mod._coerce_int(True) is True
    assert schedule_mod._coerce_int(7) == 7
    assert schedule_mod._coerce_int(7.9) == 7
    assert schedule_mod._coerce_int("9") == 9
    assert schedule_mod._coerce_int("bad") == "bad"

    class Dummy:
        pass

    dummy = Dummy()
    assert schedule_mod._coerce_int(dummy) is dummy
