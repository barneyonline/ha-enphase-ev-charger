"""Refresh performance diagnostics helpers."""

from __future__ import annotations

REFRESH_TOTAL_BUDGET_S = 30.0
REFRESH_STAGE_BUDGET_S = 5.0


def refresh_performance_summary(
    phase_timings: dict[str, float],
    *,
    latency_ms: int | None = None,
    cloud_calls: int | None = None,
    steady_cloud_calls: int | None = None,
    fast_cloud_calls: int | None = None,
    total_budget_s: float = REFRESH_TOTAL_BUDGET_S,
    stage_budget_s: float = REFRESH_STAGE_BUDGET_S,
) -> dict[str, object]:
    """Return a compact refresh timing budget summary."""

    timings: dict[str, float] = {}
    for key, value in (phase_timings or {}).items():
        try:
            timing = float(value)
        except (TypeError, ValueError):
            continue
        if timing < 0:
            continue
        timings[str(key)] = round(timing, 3)

    stage_timings = {key: value for key, value in timings.items() if key != "total_s"}
    total_s = timings.get("total_s")
    if total_s is None and latency_ms is not None:
        try:
            total_s = round(float(latency_ms) / 1000.0, 3)
        except (TypeError, ValueError):
            total_s = None

    slowest_stage = None
    slowest_stage_s = None
    if stage_timings:
        slowest_stage, slowest_stage_s = max(
            stage_timings.items(), key=lambda item: item[1]
        )

    over_budget_stages = sorted(
        key for key, value in stage_timings.items() if value > stage_budget_s
    )

    return {
        "total_s": total_s,
        "total_budget_s": total_budget_s,
        "total_over_budget": (
            bool(total_s > total_budget_s) if total_s is not None else False
        ),
        "timed_stage_count": len(stage_timings),
        "stage_budget_s": stage_budget_s,
        "over_budget_stages": over_budget_stages,
        "slowest_stage": slowest_stage,
        "slowest_stage_s": slowest_stage_s,
        "cloud_calls": cloud_calls,
        "steady_cloud_calls": steady_cloud_calls,
        "fast_cloud_calls": fast_cloud_calls,
    }
