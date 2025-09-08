
from __future__ import annotations

import aiohttp
import async_timeout

from .const import BASE_URL


class Unauthorized(Exception):
    pass

class EnphaseEVClient:
    def __init__(self, session: aiohttp.ClientSession, site_id: str, eauth: str, cookie: str, timeout: int = 15):
        self._timeout = int(timeout)
        self._s = session
        self._site = site_id
        # Cache working API variant indexes per action to avoid retries once discovered
        self._start_variant_idx: int | None = None
        self._stop_variant_idx: int | None = None
        self._cookie = cookie
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

    def _bearer(self) -> str | None:
        """Extract Authorization bearer token from cookies if present.

        Enlighten sets an `enlighten_manager_token_production` cookie with a JWT the
        frontend uses as an Authorization Bearer token for some scheduler endpoints.
        """
        try:
            parts = [p.strip() for p in (self._cookie or "").split(";")]
            for p in parts:
                if p.startswith("enlighten_manager_token_production="):
                    return p.split("=", 1)[1]
        except Exception:
            return None
        return None

    async def _json(self, method: str, url: str, **kwargs):
        async with async_timeout.timeout(self._timeout):
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
        """Start charging or set the charging level.

        The Enlighten API has variations across deployments (method, path, and payload keys).
        We try a sequence of known variants until one succeeds.
        """
        level = int(amps)
        candidates = [
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"chargingLevel": level, "connectorId": connector_id},
            ),
            (
                "PUT",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"chargingLevel": level, "connectorId": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/start_charging",
                {"chargingLevel": level, "connectorId": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"charging_level": level, "connector_id": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"connectorId": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                None,
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/start_charging",
                None,
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"chargingLevel": level},
            ),
        ]
        # If we have a known working variant, try it first
        order = list(range(len(candidates)))
        if self._start_variant_idx is not None and 0 <= self._start_variant_idx < len(candidates):
            order.remove(self._start_variant_idx)
            order.insert(0, self._start_variant_idx)

        last_exc: Exception | None = None
        for idx in order:
            method, url, payload = candidates[idx]
            try:
                if payload is None:
                    result = await self._json(method, url)
                else:
                    result = await self._json(method, url, json=payload)
                # Cache the working variant index for future calls
                self._start_variant_idx = idx
                return result
            except aiohttp.ClientResponseError as e:
                # 400/404/405 variations; try next candidate
                last_exc = e
                continue
        if last_exc:
            raise last_exc
        # Should not happen, but keep static analyzer happy
        raise aiohttp.ClientError("start_charging failed with all variants")

    async def stop_charging(self, sn: str) -> dict:
        """Stop charging; try multiple endpoint variants."""
        candidates = [
            ("PUT",  f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/stop_charging", None),
            ("POST", f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/stop_charging", None),
            ("POST", f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/stop_charging", None),
        ]
        order = list(range(len(candidates)))
        if self._stop_variant_idx is not None and 0 <= self._stop_variant_idx < len(candidates):
            order.remove(self._stop_variant_idx)
            order.insert(0, self._stop_variant_idx)

        last_exc: Exception | None = None
        for idx in order:
            method, url, payload = candidates[idx]
            try:
                if payload is None:
                    result = await self._json(method, url)
                else:
                    result = await self._json(method, url, json=payload)
                self._stop_variant_idx = idx
                return result
            except aiohttp.ClientResponseError as e:
                last_exc = e
                continue
        if last_exc:
            raise last_exc
        raise aiohttp.ClientError("stop_charging failed with all variants")

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

    async def charge_mode(self, sn: str) -> str | None:
        """Fetch the current charge mode via scheduler API.

        GET /service/evse_scheduler/api/v1/iqevc/charging-mode/<site>/<sn>/preference
        Requires Authorization: Bearer <jwt> in addition to existing cookies.
        Returns one of: GREEN_CHARGING, SCHEDULED_CHARGING, MANUAL_CHARGING when enabled.
        """
        url = f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/{self._site}/{sn}/preference"
        headers = dict(self._h)
        bearer = self._bearer()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        data = await self._json("GET", url, headers=headers)
        try:
            modes = (data.get("data") or {}).get("modes") or {}
            # Prefer the mode whose 'enabled' is true
            for key in ("greenCharging", "scheduledCharging", "manualCharging"):
                m = modes.get(key)
                if isinstance(m, dict) and m.get("enabled"):
                    return m.get("chargingMode")
        except Exception:
            return None
        return None

    async def set_charge_mode(self, sn: str, mode: str) -> dict:
        """Set the charging mode via scheduler API.

        PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/<site>/<sn>/preference
        Body: { "mode": "MANUAL_CHARGING" | "SCHEDULED_CHARGING" | "GREEN_CHARGING" }
        """
        url = f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/{self._site}/{sn}/preference"
        headers = dict(self._h)
        bearer = self._bearer()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        payload = {"mode": str(mode)}
        return await self._json("PUT", url, json=payload, headers=headers)

    async def summary_v2(self) -> list[dict] | None:
        """Fetch charger summary v2 list.

        GET /service/evse_controller/api/v2/<site_id>/ev_chargers/summary?filter_retired=true
        Returns a list of charger objects with serialNumber and other properties.
        """
        url = f"{BASE_URL}/service/evse_controller/api/v2/{self._site}/ev_chargers/summary?filter_retired=true"
        data = await self._json("GET", url)
        try:
            return data.get("data") or []
        except Exception:
            return None
