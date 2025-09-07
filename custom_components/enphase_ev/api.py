
from __future__ import annotations

import aiohttp
import async_timeout
from .const import BASE_URL, API_TIMEOUT

class Unauthorized(Exception):
    pass

class EnphaseEVClient:
    def __init__(self, session: aiohttp.ClientSession, site_id: str, eauth: str, cookie: str):
        self._s = session
        self._site = site_id
        self._h = {
            "e-auth-token": eauth,
            "Cookie": cookie,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/pv/systems/{site_id}/summary",
        }
        # If XSRF-TOKEN cookie is present, add matching CSRF header some endpoints expect
        try:
            xsrf = None
            parts = [p.strip() for p in cookie.split(";")]
            for p in parts:
                if p.startswith("XSRF-TOKEN="):
                    xsrf = p.split("=", 1)[1]
                    break
            if xsrf:
                self._h["X-CSRF-Token"] = xsrf
        except Exception:
            pass

    async def _json(self, method: str, url: str, **kwargs):
        async with async_timeout.timeout(API_TIMEOUT):
            async with self._s.request(method, url, headers=self._h, **kwargs) as r:
                if r.status == 401:
                    raise Unauthorized()
                r.raise_for_status()
                return await r.json()

    async def status(self) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/status"
        data = await self._json("GET", url)
        # Normalize alternative shapes
        if not (data.get("evChargerData") or []):
            alt = f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/status"
            try:
                data2 = await self._json("GET", alt)
                if data2:
                    data = data2
            except Exception:
                pass

        # If response is { data: { chargers: [...] } }, map to evChargerData
        try:
            inner = data.get("data") if isinstance(data, dict) else None
            chargers = inner.get("chargers") if isinstance(inner, dict) else None
            if isinstance(chargers, list) and chargers:
                out = []
                for c in chargers:
                    conn = (c.get("connectors") or [{}])[0]
                    sess = c.get("session_d") or {}
                    # Derive start_time in seconds (strt_chrg appears in ms)
                    start_ms = sess.get("strt_chrg")
                    start_sec = int(int(start_ms) / 1000) if isinstance(start_ms, int) else None
                    out.append(
                        {
                            "sn": c.get("sn"),
                            "name": c.get("name"),
                            "connected": bool(c.get("connected")),
                            "pluggedIn": bool(c.get("pluggedIn") or conn.get("pluggedIn")),
                            "charging": bool(c.get("charging")),
                            "faulted": bool(c.get("faulted")),
                            "connectorStatusType": conn.get("connectorStatusType"),
                            "session_d": {
                                "e_c": sess.get("e_c"),
                                "start_time": start_sec,
                            },
                        }
                    )
                return {"evChargerData": out, "ts": data.get("meta", {}).get("serverTimeStamp")}
        except Exception:
            # If mapping fails, fall back to raw
            pass

        return data

    async def start_charging(self, sn: str, amps: int, connector_id: int = 1) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging"
        payload = {"chargingLevel": int(amps), "connectorId": connector_id}
        return await self._json("POST", url, json=payload)

    async def stop_charging(self, sn: str) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/stop_charging"
        return await self._json("PUT", url)

    async def trigger_message(self, sn: str, requested_message: str) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/trigger_message"
        payload = {"requestedMessage": requested_message}
        return await self._json("POST", url, json=payload)

    async def start_live_stream(self) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/start_live_stream"
        return await self._json("GET", url)

    async def stop_live_stream(self) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/stop_live_stream"
        return await self._json("GET", url)
