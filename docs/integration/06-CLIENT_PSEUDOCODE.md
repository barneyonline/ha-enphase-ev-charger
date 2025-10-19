# Client Pseudocode

```python
import aiohttp
import async_timeout

class EnphaseEVClient:
    def __init__(self, session: aiohttp.ClientSession, base: str, site_id: str, eauth: str, cookie: str):
        self._s = session
        self._base = base.rstrip("/")
        self._site = site_id
        self._h = {"e-auth-token": eauth, "Cookie": cookie}

    async def get_status(self) -> dict:
        url = f"{self._base}/service/evse_controller/{self._site}/ev_chargers/status"
        async with async_timeout.timeout(10):
            async def _do():
                async with self._s.get(url, headers=self._h) as r:
                    if r.status == 401:
                        raise Unauthorized()
                    r.raise_for_status()
                    return await r.json()
            return await _do()

    async def start_charging(self, sn: str, amps: int, connector_id: int = 1) -> dict:
        url = f"{self._base}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging"
        payload = {"chargingLevel": int(amps), "connectorId": connector_id}
        async with self._s.post(url, headers=self._h, json=payload, timeout=10) as r:
            if r.status == 401:
                raise Unauthorized()
            r.raise_for_status()
            return await r.json()

    async def stop_charging(self, sn: str) -> dict:
        url = f"{self._base}/service/evse_controller/{self._site}/ev_chargers/{sn}/stop_charging"
        async with self._s.put(url, headers=self._h, timeout=10) as r:
            if r.status == 401:
                raise Unauthorized()
            r.raise_for_status()
            return await r.json()

    async def trigger_message(self, sn: str, requested: str) -> dict:
        url = f"{self._base}/service/evse_controller/{self._site}/ev_charger/{sn}/trigger_message"
        payload = {"requestedMessage": requested}
        async with self._s.post(url, headers=self._h, json=payload, timeout=10) as r:
            if r.status == 401:
                raise Unauthorized()
            r.raise_for_status()
            return await r.json()
```
