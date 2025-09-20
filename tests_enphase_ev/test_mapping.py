
import pytest


def map_status(payload: dict, want_serials: set[str]) -> dict:
    out = {}
    arr = payload.get("evChargerData") or []
    for obj in arr:
        sn = str(obj.get("sn") or "")
        if sn and (not want_serials or sn in want_serials):
            out[sn] = {
                "sn": sn,
                "name": obj.get("name"),
                "connected": bool(obj.get("connected")),
                "plugged": bool(obj.get("pluggedIn")),
                "charging": bool(obj.get("charging")),
                "faulted": bool(obj.get("faulted")),
                "connector_status": obj.get("connectorStatusType"),
                "session_kwh": (obj.get("session_d") or {}).get("e_c"),
                "session_start": (obj.get("session_d") or {}).get("start_time"),
            }
    return out

@pytest.mark.parametrize("fixture_name, charging, kwh", [
    ("status_idle.json", False, 0.0),
    ("status_charging.json", True, 3.52),
])
def test_mapping(load_fixture, fixture_name, charging, kwh):
    payload = load_fixture(fixture_name)
    mapped = map_status(payload, {"482522020944"})
    assert "482522020944" in mapped
    st = mapped["482522020944"]
    assert st["charging"] is charging
    assert pytest.approx(st["session_kwh"], rel=1e-6) == kwh
