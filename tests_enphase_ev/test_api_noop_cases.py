from unittest.mock import MagicMock

import pytest
from aiohttp.client_exceptions import ClientResponseError

from custom_components.enphase_ev.api import EnphaseEVClient
from tests_enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


def _cre(status: int, url: str = "https://example.com/") -> ClientResponseError:
    # Minimal ClientResponseError with mocked RequestInfo
    req_info = MagicMock()
    req_info.real_url = url
    return ClientResponseError(
        request_info=req_info, history=(), status=status, message=str(status)
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


@pytest.mark.asyncio
async def test_start_charging_noop_when_not_ready():
    c = ErrorStubClient(site_id=RANDOM_SITE_ID)
    # Expect no exception; returns a benign payload
    out = await c.start_charging(RANDOM_SERIAL, 32, connector_id=1)
    assert isinstance(out, dict)
    assert out.get("status") == "not_ready"


@pytest.mark.asyncio
async def test_stop_charging_noop_when_inactive():
    c = ErrorStubClient(site_id=RANDOM_SITE_ID)
    # Expect no exception; returns a benign payload
    out = await c.stop_charging(RANDOM_SERIAL)
    assert isinstance(out, dict)
    assert out.get("status") == "not_active"
