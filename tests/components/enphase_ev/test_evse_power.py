from __future__ import annotations

from datetime import datetime
from datetime import timezone

import pytest

from custom_components.enphase_ev.evse_power import (
    _power_parse_timestamp,
    _power_topology,
    _three_phase_multiplier,
    build_evse_power_snapshot,
)


def test_build_evse_power_snapshot_seeds_lifetime_and_three_phase_max() -> None:
    snapshot = build_evse_power_snapshot(
        {
            "phase_mode": "three phase",
            "wiring_configuration": {"L1": "L1", "Neutral": "N"},
            "operating_v": 230,
            "session_charge_level": 32,
            "sampled_at_ts": 1_700_000_000,
            "lifetime_kwh": 10,
            "charging": True,
        },
        None,
        None,
        240,
    )

    assert snapshot["derived_power_max_throughput_w"] == 19200
    assert snapshot["derived_power_max_throughput_unbounded_w"] == 22080
    assert snapshot["derived_power_max_throughput_source"] == "session_charge_level"
    assert snapshot["derived_power_max_throughput_amps"] == pytest.approx(32.0)
    assert snapshot["derived_power_max_throughput_voltage"] == pytest.approx(230.0)
    assert snapshot["derived_power_max_throughput_topology"] == "three_phase"
    assert snapshot["derived_power_max_throughput_phase_multiplier"] == pytest.approx(
        3.0
    )
    assert snapshot["derived_last_lifetime_kwh"] == pytest.approx(10.0)
    assert snapshot["derived_last_energy_ts"] == pytest.approx(1_700_000_000.0)
    assert snapshot["derived_power_w"] == 0
    assert snapshot["derived_power_method"] == "seeded"
    assert snapshot["derived_power_window_seconds"] is None


def test_build_evse_power_snapshot_calculates_lifetime_window_power() -> None:
    snapshot = build_evse_power_snapshot(
        {
            "operating_v": 240,
            "session_charge_level": 16,
            "sampled_at_ts": 1_300,
            "lifetime_kwh": 10.2,
            "charging": True,
        },
        {"actual_charging": True},
        {
            "derived_last_lifetime_kwh": 10.0,
            "derived_last_energy_ts": 1_000,
            "derived_power_w": 111,
            "derived_power_method": "previous",
            "derived_power_window_seconds": 60,
        },
        240,
    )

    assert snapshot["derived_power_w"] == 2400
    assert snapshot["derived_power_method"] == "lifetime_energy_window"
    assert snapshot["derived_power_window_seconds"] == pytest.approx(300.0)
    assert snapshot["derived_last_lifetime_kwh"] == pytest.approx(10.2)
    assert snapshot["derived_last_energy_ts"] == pytest.approx(1_300.0)


def test_build_evse_power_snapshot_caps_lifetime_window_power_at_max() -> None:
    snapshot = build_evse_power_snapshot(
        {
            "operating_v": 240,
            "session_charge_level": 16,
            "sampled_at_ts": 1_300,
            "lifetime_kwh": 11.0,
            "charging": True,
        },
        {"actual_charging": True},
        {
            "derived_last_lifetime_kwh": 10.0,
            "derived_last_energy_ts": 1_000,
        },
        240,
    )

    assert snapshot["derived_power_max_throughput_w"] == 3840
    assert snapshot["derived_power_w"] == 3840


def test_build_evse_power_snapshot_same_sample_preserves_previous_snapshot() -> None:
    snapshot = build_evse_power_snapshot(
        {
            "sampled_at_ts": 1_000,
            "lifetime_kwh": 5,
            "charging": False,
            "charging_level": 10,
            "nominal_v": 230,
        },
        {"charging": True},
        {
            "derived_last_sample_ts": 1_000,
            "derived_last_lifetime_kwh": 5,
            "derived_power_w": 1234,
            "derived_power_method": "lifetime_energy_window",
            "derived_power_window_seconds": 300,
            "custom_marker": "kept",
        },
        240,
    )

    assert snapshot["custom_marker"] == "kept"
    assert snapshot["derived_power_w"] == 0
    assert snapshot["derived_power_method"] == "idle"
    assert snapshot["derived_power_window_seconds"] is None
    assert snapshot["derived_power_max_throughput_w"] == 2300


def test_build_evse_power_snapshot_seeds_on_known_idle_to_charging_transition() -> None:
    snapshot = build_evse_power_snapshot(
        {
            "sampled_at_ts": 1_300,
            "lifetime_kwh": 10.1,
            "charging": True,
        },
        {"charging": False},
        {
            "derived_last_lifetime_kwh": 10.0,
            "derived_last_energy_ts": 1_000,
            "derived_power_w": 1200,
            "derived_power_method": "lifetime_energy_window",
        },
        240,
    )

    assert snapshot["derived_power_w"] == 0
    assert snapshot["derived_power_method"] == "seeded"
    assert snapshot["derived_power_window_seconds"] is None
    assert snapshot["derived_last_lifetime_kwh"] == pytest.approx(10.1)
    assert snapshot["derived_last_energy_ts"] == pytest.approx(1_300.0)


def test_build_evse_power_snapshot_handles_missing_lifetime_as_idle() -> None:
    snapshot = build_evse_power_snapshot(
        {"last_reported_at": "2024-01-01T00:00:00Z[UTC]", "charging": False},
        None,
        {"derived_power_w": 900, "derived_power_method": "previous"},
        240,
    )

    assert snapshot["derived_sampled_at_utc"] == "2024-01-01T00:00:00+00:00"
    assert snapshot["derived_power_w"] == 0
    assert snapshot["derived_power_method"] == "idle"
    assert snapshot["derived_power_window_seconds"] is None


def test_build_evse_power_snapshot_marks_lifetime_sample_idle() -> None:
    snapshot = build_evse_power_snapshot(
        {"sampled_at_ts": 1_300, "lifetime_kwh": 10.2, "charging": False},
        {"charging": True},
        {
            "derived_last_lifetime_kwh": 10.0,
            "derived_last_energy_ts": 1_000,
            "derived_power_w": 1200,
        },
        240,
    )

    assert snapshot["derived_last_lifetime_kwh"] == pytest.approx(10.2)
    assert snapshot["derived_last_energy_ts"] == pytest.approx(1_300.0)
    assert snapshot["derived_power_w"] == 0
    assert snapshot["derived_power_method"] == "idle"
    assert snapshot["derived_power_window_seconds"] is None


def test_build_evse_power_snapshot_updates_lifetime_without_sample_time() -> None:
    snapshot = build_evse_power_snapshot(
        {"lifetime_kwh": 10.2, "charging": True},
        {"charging": True},
        {
            "derived_last_lifetime_kwh": 10.0,
            "derived_last_energy_ts": 1_000,
            "derived_power_w": 1200,
            "derived_power_method": "previous",
            "derived_power_window_seconds": 300,
        },
        240,
    )

    assert snapshot["derived_last_lifetime_kwh"] == pytest.approx(10.2)
    assert snapshot["derived_last_energy_ts"] == pytest.approx(1_000.0)
    assert snapshot["derived_power_w"] == 1200
    assert snapshot["derived_power_method"] == "previous"


def test_build_evse_power_snapshot_ignores_previous_without_charging_state() -> None:
    snapshot = build_evse_power_snapshot(
        {"sampled_at_ts": 1_300, "lifetime_kwh": 10.2, "charging": True},
        {"status": "unknown"},
        {
            "derived_last_lifetime_kwh": 10.0,
            "derived_last_energy_ts": 1_000,
        },
        240,
    )

    assert snapshot["derived_power_method"] == "lifetime_energy_window"


def test_build_evse_power_snapshot_detects_lifetime_reset() -> None:
    snapshot = build_evse_power_snapshot(
        {"sampled_at_ts": 1_300, "lifetime_kwh": 9.0, "charging": True},
        {"charging": True},
        {
            "derived_last_lifetime_kwh": 10.0,
            "derived_last_energy_ts": 1_000,
            "derived_power_w": 1200,
        },
        240,
    )

    assert snapshot["derived_power_w"] == 0
    assert snapshot["derived_power_method"] == "lifetime_reset"
    assert snapshot["derived_power_window_seconds"] is None
    assert snapshot["derived_last_reset_at"] == pytest.approx(1_300.0)


def test_build_evse_power_snapshot_retains_previous_power_for_tiny_delta() -> None:
    snapshot = build_evse_power_snapshot(
        {"sampled_at_ts": 1_300, "lifetime_kwh": 10.0004, "charging": True},
        {"charging": True},
        {
            "derived_last_lifetime_kwh": 10.0,
            "derived_last_energy_ts": 1_000,
            "derived_power_w": 1200,
            "derived_power_method": "previous",
            "derived_power_window_seconds": 300,
        },
        240,
    )

    assert snapshot["derived_power_w"] == 1200
    assert snapshot["derived_power_method"] == "previous"
    assert snapshot["derived_power_window_seconds"] == pytest.approx(300.0)


def test_build_evse_power_snapshot_skips_tiny_unbounded_max_candidate() -> None:
    snapshot = build_evse_power_snapshot(
        {
            "operating_v": 0.001,
            "session_charge_level": 0.001,
            "charging": True,
        },
        None,
        None,
        240,
    )

    assert snapshot["derived_power_max_throughput_w"] == 19200
    assert snapshot["derived_power_max_throughput_source"] == "static_default"


def test_evse_power_normalizes_topology_and_multiplier_edges() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert _power_topology({"phase_mode": BadStr()}) == "unknown"
    assert _power_topology({"phase_mode": "single-phase"}) == "single_phase"
    assert _power_topology({"phase_mode": "split"}) == "split_phase"
    assert _power_topology({"phase_count": 1}) == "single_phase"
    assert _power_topology({"phase_count": 3}) == "three_phase"
    assert _power_topology({"phase_count": 2}) == "unknown"
    assert _three_phase_multiplier({"wiring_configuration": {BadStr(): "L1"}}) == (
        pytest.approx(1.7320508075688772)
    )
    assert _three_phase_multiplier({"wiring_configuration": {"L1": "L1"}}) == (
        pytest.approx(1.7320508075688772)
    )
    assert _three_phase_multiplier(
        {"wiring_configuration": {"L1": "L1", "N": "N"}}
    ) == (pytest.approx(3.0))


def test_evse_power_parse_timestamp_variants() -> None:
    class BadFloat(float):
        def __float__(self) -> float:
            raise ValueError("boom")

    assert _power_parse_timestamp(BadFloat(1.0)) is None
    assert _power_parse_timestamp(float("inf")) is None
    assert _power_parse_timestamp(1_700_000_000_000) == pytest.approx(1_700_000_000)
    assert _power_parse_timestamp(0) is None
    assert _power_parse_timestamp("") is None
    assert _power_parse_timestamp("bad") is None
    assert _power_parse_timestamp([]) is None
    expected = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
    assert _power_parse_timestamp("2024-01-01T00:00:00") == pytest.approx(expected)
