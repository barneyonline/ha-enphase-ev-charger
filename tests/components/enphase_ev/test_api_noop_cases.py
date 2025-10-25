import json
from unittest.mock import MagicMock

import pytest
from aiohttp.client_exceptions import ClientResponseError

from custom_components.enphase_ev.api import EnphaseEVClient
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


def _cre(
    status: int, url: str = "https://example.com/", message: str | None = None
) -> ClientResponseError:
    # Minimal ClientResponseError with mocked RequestInfo
    req_info = MagicMock()
    req_info.real_url = url
    return ClientResponseError(
        request_info=req_info, history=(), status=status, message=message or str(status)
    )


class ErrorStubClient(EnphaseEVClient):
    def __init__(self, site_id=RANDOM_SITE_ID):
        self.calls = []
        super().__init__(MagicMock(), site_id, "EAUTH", "COOKIE")

    async def _json(self, method, url, **kwargs):
        # Record and raise based on action
        self.calls.append((method, url, kwargs.get("json")))
        if url.endswith("start_charging"):
            raise _cre(409, url)
        if url.endswith("stop_charging"):
            raise _cre(404, url)
        return {"status": "ok"}


class AlreadyChargingStubClient(EnphaseEVClient):
    def __init__(self, site_id=RANDOM_SITE_ID):
        self.calls = []
        super().__init__(MagicMock(), site_id, "EAUTH", "COOKIE")

    async def _json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        if url.endswith("start_charging"):
            body = {
                "meta": {"serverTimeStamp": 1760835362174},
                "data": None,
                "error": {
                    "displayMessage": "Charger is already in charging state",
                    "code": "400",
                    "errorMessageCode": "iqevc_ms-10012",
                },
            }
            raise _cre(400, url, message=json.dumps(body))
        return {"status": "ok"}


class UnpluggedStubClient(EnphaseEVClient):
    def __init__(self, site_id=RANDOM_SITE_ID):
        self.calls = []
        super().__init__(MagicMock(), site_id, "EAUTH", "COOKIE")

    async def _json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        if url.endswith("start_charging"):
            body = {
                "meta": {"serverTimeStamp": 1760835362174},
                "data": None,
                "error": {
                    "displayMessage": "Charger is not plugged to EV",
                    "code": "400",
                    "errorMessageCode": "iqevc_ms-10008",
                },
            }
            raise _cre(400, url, message=json.dumps(body))
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_start_charging_noop_when_not_ready():
    c = ErrorStubClient(site_id=RANDOM_SITE_ID)
    # Expect no exception; returns a benign payload
    out = await c.start_charging(RANDOM_SERIAL, 32, connector_id=1)
    assert isinstance(out, dict)
    assert out.get("status") == "not_ready"


@pytest.mark.asyncio
async def test_start_charging_already_active_is_not_error():
    c = AlreadyChargingStubClient(site_id=RANDOM_SITE_ID)
    out = await c.start_charging(RANDOM_SERIAL, 32, connector_id=1)
    assert isinstance(out, dict)
    assert out.get("status") == "already_charging"
    assert c.calls and "start_charging" in c.calls[-1][1]


@pytest.mark.asyncio
async def test_start_charging_unplugged_maps_to_not_ready():
    c = UnpluggedStubClient(site_id=RANDOM_SITE_ID)
    out = await c.start_charging(RANDOM_SERIAL, 32, connector_id=1)
    assert isinstance(out, dict)
    assert out.get("status") == "not_ready"
    assert c.calls and "start_charging" in c.calls[-1][1]


@pytest.mark.asyncio
async def test_stop_charging_noop_when_inactive():
    c = ErrorStubClient(site_id=RANDOM_SITE_ID)
    # Expect no exception; returns a benign payload
    out = await c.stop_charging(RANDOM_SERIAL)
    assert isinstance(out, dict)
    assert out.get("status") == "not_active"
