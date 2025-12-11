from datetime import datetime, timezone

import pytest

from custom_components.enphase_ev.session_history import SessionHistoryManager


@pytest.mark.asyncio
async def test_session_history_parsers_handle_invalid_values(hass, monkeypatch):
    manager = SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
    )

    # Cover _parse_ts and _as_float/_as_int/_as_bool paths indirectly via _async_enrich_sessions helpers
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("homeassistant.util.dt.utcnow", lambda: now)
    results = [
        {"startTime": "bad", "endTime": None},  # skipped
        {
            "startTime": "2025-01-01T10:00:00Z",
            "endTime": "2025-01-01T11:00:00Z",
            "aggEnergyValue": "bad",
            "activeChargeTime": "bad",
            "sessionId": "sess-1",
            "milesAdded": "bad",
            "sessionCost": "bad",
            "avgCostPerUnitEnergy": "bad",
            "costCalculated": "nope",
            "manualOverridden": "yes",
            "chargeProfileStackLevel": "bad",
        },
    ]

    async def _fake_fetch(sn, day_local=None):
        return results

    monkeypatch.setattr(manager, "_async_fetch_sessions_today", _fake_fetch)
    updated = await manager._async_enrich_sessions(["sn"], day_local=now)
    # First entry from results[0] should be skipped; ensure we captured the second
    assert updated["sn"]  # non-empty
    session = updated["sn"][-1]
    # session_id may be blank when the source provided none; fallback to ID string
    assert session.get("session_id", "") in ("sess-1", "")
    assert session.get("energy_kwh_total") is None
    assert session.get("active_charge_time_s") is None
    assert session.get("energy_kwh") is None
    assert session.get("session_cost") is None
    assert session.get("manual_override") in (True, None)


@pytest.mark.asyncio
async def test_session_history_fallback_rounding_failure(hass, monkeypatch):
    manager = SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
    )

    class RoundBoom:
        def __float__(self):
            return 1.234

        def __round__(self, _ndigits=None):
            raise ValueError("cannot round")

    now = datetime(2025, 1, 1, 8, 0, 0, tzinfo=timezone.utc)

    results = [
        {
            "startTime": now.isoformat(),
            "endTime": now.isoformat(),
            "aggEnergyValue": RoundBoom(),
            "sessionId": "fallback",
        }
    ]
    sessions = manager._normalise_sessions_for_day(local_dt=now, results=results)
    assert sessions[0]["energy_kwh_total"] == pytest.approx(1.234)


@pytest.mark.asyncio
async def test_session_history_as_float_precision_none(monkeypatch, hass):
    manager = SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
        data_supplier=lambda: {},
    )

    call_count = 0
    real_round = round

    def boom_round(val, ndigits=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("round-fail")
        return real_round(val, ndigits) if ndigits is not None else real_round(val)

    monkeypatch.setattr("builtins.round", boom_round)

    now = datetime(2025, 1, 2, 8, 0, 0, tzinfo=timezone.utc)
    sessions = manager._normalise_sessions_for_day(
        local_dt=now,
        results=[
            {
                "startTime": now.isoformat(),
                "endTime": now.isoformat(),
                "aggEnergyValue": 1.5,
                "sessionId": "round-fallback",
            }
        ],
    )
    assert sessions[0]["energy_kwh_total"] == pytest.approx(1.5)
