from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest


def test_grid_control_supported_is_unknown_before_first_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    assert coord.grid_control_supported is None
    assert coord.grid_toggle_allowed is None
    assert coord.grid_toggle_blocked_reasons == []


def test_parse_grid_control_check_payload_maps_flags_and_allows(coordinator_factory) -> None:
    coord = coordinator_factory()

    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_pending is False
    assert coord.grid_toggle_blocked_reasons == []
    assert coord.grid_toggle_allowed is True


def test_parse_grid_control_check_payload_tracks_blocked_reasons(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": True,
            "activeDownload": True,
            "sunlightBackupSystemCheck": True,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_pending is False
    assert coord.grid_toggle_allowed is False
    assert coord.grid_toggle_blocked_reasons == [
        "disable_grid_control",
        "active_download",
        "sunlight_backup_system_check",
    ]


def test_parse_grid_control_check_payload_nested_data_and_grid_outage_reason(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "data": {
                "disableGridControl": False,
                "activeDownload": False,
                "sunlightBackupSystemCheck": False,
                "gridOutageCheck": True,
                "userInitiatedGridToggle": False,
            }
        }
    )

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_allowed is False
    assert coord.grid_toggle_blocked_reasons == ["grid_outage_check"]


def test_parse_grid_control_check_payload_pending_state(coordinator_factory) -> None:
    coord = coordinator_factory()

    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": True,
        }
    )

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_pending is True
    assert coord.grid_toggle_allowed is False


def test_parse_grid_control_check_payload_partial_is_unknown_allowed(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
        }
    )

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_pending is False
    assert coord.grid_toggle_blocked_reasons == []
    assert coord.grid_toggle_allowed is None


def test_parse_grid_control_check_payload_missing_or_invalid_marks_unsupported(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord._parse_grid_control_check_payload({})  # noqa: SLF001
    assert coord.grid_control_supported is False
    assert coord.grid_toggle_allowed is None

    coord._parse_grid_control_check_payload(["bad"])  # noqa: SLF001
    assert coord.grid_control_supported is False
    assert coord.grid_toggle_allowed is None


@pytest.mark.asyncio
async def test_refresh_grid_control_check_caches_and_redacts(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.grid_control_check = AsyncMock(
        return_value={
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
            "token": "secret-token",
        }
    )

    await coord._async_refresh_grid_control_check(force=True)  # noqa: SLF001

    assert coord.grid_control_supported is True
    assert coord._grid_control_check_payload is not None  # noqa: SLF001
    assert coord._grid_control_check_payload["token"] == "[redacted]"  # noqa: SLF001

    coord._grid_control_check_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord.client.grid_control_check.reset_mock()
    await coord._async_refresh_grid_control_check()  # noqa: SLF001
    coord.client.grid_control_check.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_grid_control_check_wraps_non_dict_redaction(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.grid_control_check = AsyncMock(
        return_value={
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._redact_battery_payload = lambda _payload: "masked"  # type: ignore[method-assign]  # noqa: SLF001

    await coord._async_refresh_grid_control_check(force=True)  # noqa: SLF001

    assert coord._grid_control_check_payload == {"value": "masked"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_grid_control_check_failure_marks_unknown_when_stale(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._grid_control_check_last_success_mono = time.monotonic() - 999  # noqa: SLF001
    coord.client.grid_control_check = AsyncMock(side_effect=RuntimeError("boom"))

    await coord._async_refresh_grid_control_check(force=True)  # noqa: SLF001

    assert coord.grid_control_supported is None
    assert coord.grid_control_disable is None
    assert coord.grid_toggle_allowed is None
    assert coord._grid_control_check_failures == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_grid_control_check_failure_keeps_recent_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._grid_control_check_last_success_mono = time.monotonic()  # noqa: SLF001
    coord.client.grid_control_check = AsyncMock(side_effect=RuntimeError("boom"))

    await coord._async_refresh_grid_control_check(force=True)  # noqa: SLF001

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_allowed is True
    assert coord._grid_control_check_failures == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_update_data_ignores_grid_control_refresh_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord._async_refresh_grid_control_check = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("boom")
    )

    result = await coord._async_update_data()  # noqa: SLF001

    assert result == {}

    coord = coordinator_factory()
    coord.client.status = AsyncMock(return_value={"evChargerData": [], "ts": 0})
    coord._async_refresh_grid_control_check = AsyncMock(  # noqa: SLF001
        side_effect=RuntimeError("boom")
    )

    await coord._async_update_data()  # noqa: SLF001


def test_grid_control_staleness_and_support_properties(coordinator_factory) -> None:
    coord = coordinator_factory()

    assert coord._grid_control_is_stale() is True  # noqa: SLF001

    coord._grid_control_supported = True  # noqa: SLF001
    coord._grid_control_check_last_success_mono = time.monotonic() + 5  # noqa: SLF001
    assert coord._grid_control_is_stale() is False  # noqa: SLF001

    coord._grid_control_check_last_success_mono = time.monotonic() - 999  # noqa: SLF001
    assert coord.grid_control_supported is None


def test_collect_site_metrics_includes_grid_control_fields(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": True,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )

    metrics = coord.collect_site_metrics()

    assert metrics["grid_control_supported"] is True
    assert metrics["grid_toggle_allowed"] is False
    assert metrics["grid_toggle_pending"] is False
    assert metrics["grid_toggle_blocked_reasons"] == ["active_download"]
    assert metrics["grid_control_data_stale"] is False
    assert metrics["grid_control_fetch_failures"] == 0


def test_collect_site_metrics_includes_grid_control_last_success_age(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._grid_control_check_last_success_mono = time.monotonic() - 1.0  # noqa: SLF001

    metrics = coord.collect_site_metrics()

    assert "grid_control_last_success_age_s" in metrics
    assert isinstance(metrics["grid_control_last_success_age_s"], float)


def test_grid_mode_normalization_and_raw_states(coordinator_factory) -> None:
    coord = coordinator_factory()
    assert coord._normalize_grid_mode_value(None) is None  # noqa: SLF001

    coord.data = {
        "A": {"off_grid_state": "ON_GRID"},
        "B": {"off_grid_state": "ON_GRID"},
    }
    assert coord.grid_mode_raw_states == ["ON_GRID"]
    assert coord.grid_mode == "on_grid"

    coord.data = {
        "A": {"off_grid_state": "OFF_GRID"},
        "B": {"off_grid_state": "ON_GRID"},
    }
    assert coord.grid_mode == "unknown"

    coord.data = {
        "A": {"off_grid_state": "unexpected"},
    }
    assert coord.grid_mode == "unknown"

    coord.data = {}
    assert coord.grid_mode is None

    coord.data = {
        "A": "bad",
        "B": {"off_grid_state": None},
        "C": {"off_grid_state": ""},
    }
    assert coord.grid_mode_raw_states == []


@pytest.mark.asyncio
async def test_async_request_grid_toggle_otp_success(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord.client.request_grid_toggle_otp = AsyncMock(
        return_value={"success": "email sent successfully"}
    )
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001

    await coord.async_request_grid_toggle_otp()

    coord.client.request_grid_toggle_otp.assert_awaited_once()
    coord._async_refresh_grid_control_check.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )


@pytest.mark.asyncio
async def test_async_request_grid_toggle_otp_blocked_or_unsupported(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._grid_control_supported = None  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    with pytest.raises(ServiceValidationError):
        await coord.async_request_grid_toggle_otp()

    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": True,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    with pytest.raises(ServiceValidationError):
        await coord.async_request_grid_toggle_otp()


@pytest.mark.asyncio
async def test_async_request_grid_toggle_otp_client_paths(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001

    coord.client.request_grid_toggle_otp = None
    with pytest.raises(ServiceValidationError):
        await coord.async_request_grid_toggle_otp()

    coord.client.request_grid_toggle_otp = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(ServiceValidationError):
        await coord.async_request_grid_toggle_otp()


@pytest.mark.asyncio
async def test_async_set_grid_mode_success_logs_and_refreshes(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "122447007044"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord.client.set_grid_state = AsyncMock(return_value={"request_id": "x"})
    coord.client.log_grid_change = AsyncMock(return_value={"status": "ok"})
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()

    await coord.async_set_grid_mode("off_grid", "1234")

    coord.client.validate_grid_toggle_otp.assert_awaited_once_with("1234")
    coord.client.set_grid_state.assert_awaited_once_with("122447007044", 1)
    coord.client.log_grid_change.assert_awaited_once_with(
        "122447007044",
        "OPER_RELAY_CLOSED",
        "OPER_RELAY_OFFGRID_AC_GRID_PRESENT",
    )
    coord.async_request_refresh.assert_awaited_once()
    assert coord._async_refresh_grid_control_check.await_count == 2  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_set_grid_mode_best_effort_log_failure(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "122447007044"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord.client.set_grid_state = AsyncMock(return_value={"request_id": "x"})
    coord.client.log_grid_change = AsyncMock(side_effect=RuntimeError("nope"))
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()

    await coord.async_set_grid_mode("on_grid", "1234")

    coord.client.set_grid_state.assert_awaited_once_with("122447007044", 2)
    coord.client.log_grid_change.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_set_grid_mode_setter_failure_raises_validation(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "122447007044"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord.client.set_grid_state = AsyncMock(side_effect=RuntimeError("setter down"))
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()

    with pytest.raises(ServiceValidationError):
        await coord.async_set_grid_mode("on_grid", "1234")


@pytest.mark.asyncio
async def test_async_set_grid_mode_validation_paths(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {"count": 1, "devices": [{"serial_number": "122447007044"}]}
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord.client.set_grid_state = AsyncMock(return_value={"request_id": "x"})
    coord.client.log_grid_change = AsyncMock(return_value={"status": "ok"})
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()

    with pytest.raises(ServiceValidationError):
        await coord.async_set_grid_mode("bad", "1234")
    with pytest.raises(ServiceValidationError):
        await coord.async_set_grid_mode("off_grid", "")
    with pytest.raises(ServiceValidationError):
        await coord.async_set_grid_mode("off_grid", "12ab")

    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=False)
    with pytest.raises(ServiceValidationError):
        await coord.async_set_grid_mode("off_grid", "1234")

    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord._type_device_buckets = {"envoy": {"count": 1, "devices": [{}]}}  # noqa: SLF001
    with pytest.raises(ServiceValidationError):
        await coord.async_set_grid_mode("off_grid", "1234")


@pytest.mark.asyncio
async def test_async_set_grid_connection_maps_bool_and_requires_otp(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.async_set_grid_mode = AsyncMock()

    with pytest.raises(ServiceValidationError):
        await coord.async_set_grid_connection(True)

    await coord.async_set_grid_connection(True, otp="1234")
    coord.async_set_grid_mode.assert_awaited_once_with("on_grid", "1234")


@pytest.mark.asyncio
async def test_async_set_grid_mode_additional_error_paths(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {"count": 1, "devices": [{"serial_number": "122447007044"}]}
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001

    class _BadStr:
        def __str__(self):
            raise RuntimeError("bad string")

    with pytest.raises(ServiceValidationError):
        await coord.async_set_grid_mode(_BadStr(), "1234")

    coord.client.validate_grid_toggle_otp = None
    with pytest.raises(ServiceValidationError):
        await coord.async_set_grid_mode("off_grid", "1234")

    coord.client.validate_grid_toggle_otp = AsyncMock(side_effect=RuntimeError("nope"))
    with pytest.raises(ServiceValidationError):
        await coord.async_set_grid_mode("off_grid", "1234")

    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord.client.set_grid_state = None
    with pytest.raises(ServiceValidationError):
        await coord.async_set_grid_mode("off_grid", "1234")

    class _ServiceValidationCompat(Exception):
        def __init__(self, *args, **kwargs):
            if "message" in kwargs:
                raise TypeError("message kw not supported")
            super().__init__(*args)

    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.ServiceValidationError",
        _ServiceValidationCompat,
    )
    with pytest.raises(_ServiceValidationCompat):
        coord._raise_grid_validation("grid_mode_invalid", message="bad mode")  # noqa: SLF001


def test_grid_envoy_serial_edge_paths(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.type_bucket = lambda _key: None  # type: ignore[method-assign]
    assert coord._grid_envoy_serial() is None  # noqa: SLF001

    coord.type_bucket = lambda _key: {"devices": "bad"}  # type: ignore[method-assign]
    assert coord._grid_envoy_serial() is None  # noqa: SLF001

    coord.type_bucket = lambda _key: {"devices": ["bad"]}  # type: ignore[method-assign]
    assert coord._grid_envoy_serial() is None  # noqa: SLF001
