import pytest


@pytest.mark.asyncio
async def test_summary_v2_enrichment(monkeypatch):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )

    cfg = {
        CONF_SITE_ID: "3381244",
        CONF_SERIALS: ["482522020944"],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "async_get_clientsession", lambda *args, **kwargs: object())
    coord = EnphaseCoordinator(object(), cfg)

    status_payload = {
        "evChargerData": [
            {
                "sn": "482522020944",
                "name": "IQ EV Charger",
                "connected": True,
                "pluggedIn": False,
                "charging": False,
                "faulted": False,
                "lst_rpt_at": None,
                "connectors": [
                    {
                        "connectorId": 1,
                        "connectorStatusType": "AVAILABLE",
                        "connectorStatusReason": "INSUFFICIENT_SOLAR",
                        "dlbActive": False,
                    }
                ],
            }
        ],
        "ts": 1757299870275,
    }

    summary_list = [
        {
            "serialNumber": "482522020944",
            "lastReportedAt": "2025-09-08T02:55:30.347Z[UTC]",
            "chargeLevelDetails": {"min": "6", "max": "32", "granularity": "1", "defaultChargeLevel": "disabled"},
            "maxCurrent": 32,
            "phaseMode": 1,
            "status": "NORMAL",
            "lifeTimeConsumption": 39153.87,
            "commissioningStatus": 1,
        }
    ]

    class StubClient:
        async def status(self):
            return status_payload

        async def summary_v2(self):
            return summary_list

        async def charge_mode(self, sn: str):
            return "MANUAL_CHARGING"

    coord.client = StubClient()

    data = await coord._async_update_data()
    st = data["482522020944"]

    assert st["min_amp"] == 6
    assert st["max_amp"] == 32
    assert st["max_current"] == 32
    assert st["phase_mode"] == 1
    assert st["status"] == "NORMAL"
    assert st["commissioned"] is True
    assert st["lifetime_kwh"] == pytest.approx(39153.87)
    # last_reported_at should come from summary
    assert "last_reported_at" in st and st["last_reported_at"].startswith("2025-09-08")
    # charge mode cached/derived value
    assert st["charge_mode"] == "MANUAL_CHARGING"

