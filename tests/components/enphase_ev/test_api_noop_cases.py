from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp.client_exceptions import ClientResponseError

from custom_components.enphase_ev.api import EnphaseEVClient, Unauthorized
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


class OptionalFwDetailsStubClient(EnphaseEVClient):
    def __init__(self, error):
        super().__init__(MagicMock(), RANDOM_SITE_ID, "EAUTH", "COOKIE")
        self._error = error

    async def _json(self, method, url, **kwargs):  # noqa: ARG002
        raise self._error


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ClientResponseError(
            request_info=MagicMock(real_url="https://example.com/"),
            history=(),
            status=403,
            message="403",
        ),
        ClientResponseError(
            request_info=MagicMock(real_url="https://example.com/"),
            history=(),
            status=404,
            message="404",
        ),
    ],
)
async def test_evse_fw_details_optional_client_response_errors(error) -> None:
    c = OptionalFwDetailsStubClient(error)
    assert await c.evse_fw_details() is None


@pytest.mark.asyncio
async def test_evse_fw_details_optional_unauthorized() -> None:
    c = OptionalFwDetailsStubClient(Unauthorized())
    assert await c.evse_fw_details() is None


@pytest.mark.asyncio
async def test_evse_fw_details_re_raises_unexpected_client_response_errors() -> None:
    error = ClientResponseError(
        request_info=MagicMock(real_url="https://example.com/"),
        history=(),
        status=500,
        message="500",
    )
    c = OptionalFwDetailsStubClient(error)

    with pytest.raises(ClientResponseError):
        await c.evse_fw_details()
