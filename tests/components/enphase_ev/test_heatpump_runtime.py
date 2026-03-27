from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.enphase_ev.heatpump_runtime import HeatpumpRuntime
from custom_components.enphase_ev.parsing_helpers import (
    coerce_optional_bool,
    coerce_optional_float,
    coerce_optional_text,
    heatpump_member_device_type,
    heatpump_status_text,
    parse_inverter_last_report,
    type_member_text,
)


@pytest.mark.asyncio
async def test_heatpump_runtime_preflight_without_refresh_kw(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.heatpump_runtime
    calls: list[str] = []
    coord.client._hems_site_supported = None  # noqa: SLF001

    async def system_dashboard_summary():
        calls.append("system_dashboard_summary")
        return {"is_hems": True}

    coord.client.system_dashboard_summary = system_dashboard_summary  # type: ignore[method-assign]

    await runtime._async_refresh_hems_support_preflight(force=True)  # noqa: SLF001

    assert calls == ["system_dashboard_summary"]
    assert coord.client.hems_site_supported is True


@pytest.mark.asyncio
async def test_heatpump_runtime_fetcher_falls_back_when_uninspectable(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().heatpump_runtime

    class BadSignatureFetcher:
        @property
        def __signature__(self):
            raise ValueError("boom")

        async def __call__(self):
            return {"ok": True}

    assert await runtime._async_call_refreshable_fetcher(  # noqa: SLF001
        BadSignatureFetcher(),
        force=True,
    ) == {"ok": True}


@pytest.mark.asyncio
async def test_coordinator_heatpump_runtime_wrapper_delegation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = MagicMock()
    runtime._heatpump_primary_member.return_value = {"device_uid": "HP-1"}
    runtime._heatpump_primary_device_uid.return_value = "HP-1"
    runtime._heatpump_runtime_device_uid.return_value = "HP-RUNTIME"
    runtime._heatpump_daily_window.return_value = (
        "2026-03-27T00:00:00+00:00",
        "2026-03-28T00:00:00+00:00",
        "UTC",
        ("2026-03-27", "UTC"),
    )
    runtime._build_heatpump_daily_consumption_snapshot.return_value = {
        "daily_energy_wh": 123.0
    }
    runtime._heatpump_power_candidate_device_uids.return_value = ["HP-1", None]
    runtime._heatpump_member_for_uid.return_value = {"device_uid": "HP-1"}
    runtime._heatpump_member_alias_map.return_value = {"HP-1": "HP-1"}
    runtime._heatpump_power_inventory_marker.return_value = ()
    runtime._heatpump_power_fetch_plan.return_value = (["HP-1"], False, ())
    runtime._heatpump_power_candidate_is_recommended.return_value = True
    runtime._heatpump_power_candidate_type_rank.return_value = 3
    runtime._heatpump_power_selection_key.return_value = (1, 1, 1, 3, 500.0, 1, 0)
    runtime._async_refresh_hems_support_preflight = AsyncMock()
    runtime.async_ensure_heatpump_runtime_diagnostics = AsyncMock()
    runtime._async_refresh_heatpump_runtime_state = AsyncMock()
    runtime._async_refresh_heatpump_daily_consumption = AsyncMock()
    runtime._async_refresh_heatpump_power = AsyncMock()
    runtime.heatpump_runtime_diagnostics.return_value = {"runtime_state": {}}
    runtime.heatpump_runtime_state = {"device_uid": "HP-1"}
    runtime.heatpump_runtime_state_last_error = "runtime boom"
    runtime.heatpump_daily_consumption = {"daily_energy_wh": 123.0}
    runtime.heatpump_daily_consumption_last_error = "daily boom"
    runtime.heatpump_power_w = 640.0
    runtime.heatpump_power_sample_utc = datetime(2026, 3, 27, tzinfo=timezone.utc)
    runtime.heatpump_power_start_utc = datetime(2026, 3, 27, tzinfo=timezone.utc)
    runtime.heatpump_power_device_uid = "HP-1"
    runtime.heatpump_power_source = "hems_power_timeseries:HP-1"
    runtime.heatpump_power_last_error = "power boom"
    coord.heatpump_runtime = runtime

    assert coord._heatpump_primary_member() == {"device_uid": "HP-1"}  # noqa: SLF001
    assert coord._heatpump_primary_device_uid() == "HP-1"  # noqa: SLF001
    assert coord._heatpump_runtime_device_uid() == "HP-RUNTIME"  # noqa: SLF001
    assert coord._heatpump_daily_window() == (  # noqa: SLF001
        "2026-03-27T00:00:00+00:00",
        "2026-03-28T00:00:00+00:00",
        "UTC",
        ("2026-03-27", "UTC"),
    )
    assert coord._build_heatpump_daily_consumption_snapshot(
        {"data": {}}
    ) == {  # noqa: SLF001
        "daily_energy_wh": 123.0
    }
    assert coord._heatpump_power_candidate_device_uids() == [
        "HP-1",
        None,
    ]  # noqa: SLF001
    assert coord._heatpump_member_for_uid("HP-1") == {
        "device_uid": "HP-1"
    }  # noqa: SLF001
    assert (
        coord._heatpump_member_primary_id({"device_uid": "PRIMARY-1"})  # noqa: SLF001
        == "PRIMARY-1"
    )
    assert (
        coord._heatpump_member_parent_id({"parent": "PARENT-1"}) == "PARENT-1"
    )  # noqa: SLF001
    assert coord._heatpump_member_alias_map() == {"HP-1": "HP-1"}  # noqa: SLF001
    assert coord._heatpump_power_inventory_marker() == ()  # noqa: SLF001
    assert coord._heatpump_power_fetch_plan() == (["HP-1"], False, ())  # noqa: SLF001
    assert (
        coord._heatpump_power_candidate_is_recommended("HP-1") is True
    )  # noqa: SLF001
    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "HP-1",
            is_recommended=True,
        )
        == 3
    )
    assert coord._heatpump_power_selection_key(  # noqa: SLF001
        {},
        requested_uid="HP-1",
        sample=(0, 500.0),
    ) == (1, 1, 1, 3, 500.0, 1, 0)

    await coord._async_refresh_hems_support_preflight(force=True)  # noqa: SLF001
    await coord.async_ensure_heatpump_runtime_diagnostics(force=True)
    await coord._async_refresh_heatpump_runtime_state(force=True)  # noqa: SLF001
    await coord._async_refresh_heatpump_daily_consumption(force=True)  # noqa: SLF001
    await coord._async_refresh_heatpump_power(force=True)  # noqa: SLF001

    assert coord.heatpump_runtime_diagnostics() == {"runtime_state": {}}
    assert coord.heatpump_runtime_state == {"device_uid": "HP-1"}
    assert coord.heatpump_runtime_state_last_error == "runtime boom"
    assert coord.heatpump_daily_consumption == {"daily_energy_wh": 123.0}
    assert coord.heatpump_daily_consumption_last_error == "daily boom"
    assert coord.heatpump_power_w == 640.0
    assert coord.heatpump_power_sample_utc == datetime(2026, 3, 27, tzinfo=timezone.utc)
    assert coord.heatpump_power_start_utc == datetime(2026, 3, 27, tzinfo=timezone.utc)
    assert coord.heatpump_power_device_uid == "HP-1"
    assert coord.heatpump_power_source == "hems_power_timeseries:HP-1"
    assert coord.heatpump_power_last_error == "power boom"

    runtime._heatpump_primary_member.assert_called_once_with()
    runtime._heatpump_primary_device_uid.assert_called_once_with()
    runtime._heatpump_runtime_device_uid.assert_called_once_with()
    runtime._heatpump_daily_window.assert_called_once_with()
    runtime._build_heatpump_daily_consumption_snapshot.assert_called_once_with(
        {"data": {}}
    )
    runtime._heatpump_power_candidate_device_uids.assert_called_once_with()
    runtime._heatpump_member_for_uid.assert_called_once_with("HP-1")
    runtime._heatpump_member_alias_map.assert_called_once_with()
    runtime._heatpump_power_inventory_marker.assert_called_once_with()
    runtime._heatpump_power_fetch_plan.assert_called_once_with()
    runtime._heatpump_power_candidate_is_recommended.assert_called_once_with("HP-1")
    runtime._heatpump_power_candidate_type_rank.assert_called_once_with(
        {},
        "HP-1",
        is_recommended=True,
    )
    runtime._heatpump_power_selection_key.assert_called_once_with(
        {},
        requested_uid="HP-1",
        sample=(0, 500.0),
    )
    runtime._async_refresh_hems_support_preflight.assert_awaited_once_with(force=True)
    runtime.async_ensure_heatpump_runtime_diagnostics.assert_awaited_once_with(
        force=True
    )
    runtime._async_refresh_heatpump_runtime_state.assert_awaited_once_with(force=True)
    runtime._async_refresh_heatpump_daily_consumption.assert_awaited_once_with(
        force=True
    )
    runtime._async_refresh_heatpump_power.assert_awaited_once_with(force=True)


def test_heatpump_and_parsing_helper_guards() -> None:
    class BadString:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    class BadFloat:
        def __float__(self) -> float:
            raise RuntimeError("boom")

    class BadFloatSubclass(float):
        def __float__(self) -> float:
            raise RuntimeError("boom")

    ts = parse_inverter_last_report(1711843200)
    assert ts == datetime.fromtimestamp(1711843200, tz=timezone.utc)
    assert parse_inverter_last_report(BadFloatSubclass(1.0)) is None
    assert parse_inverter_last_report(BadString()) is None
    assert parse_inverter_last_report("") is None
    assert parse_inverter_last_report("not-a-date") is None
    assert parse_inverter_last_report("1711843200000") == datetime(
        2024, 3, 31, 0, 0, tzinfo=timezone.utc
    )
    assert parse_inverter_last_report("2026-03-27T12:00:00") == datetime(
        2026, 3, 27, 12, 0, tzinfo=timezone.utc
    )
    assert parse_inverter_last_report("2026-03-27T12:00:00[UTC]") == datetime(
        2026, 3, 27, 12, 0, tzinfo=timezone.utc
    )

    assert coerce_optional_float(BadFloat()) is None
    assert coerce_optional_float(float("inf")) == float("inf")
    assert coerce_optional_float(True) == 1.0
    assert coerce_optional_float("1,234.5") == pytest.approx(1234.5)
    assert coerce_optional_text(BadString()) is None
    assert coerce_optional_text("  hello  ") == "hello"
    assert coerce_optional_bool("enabled") is True
    assert coerce_optional_bool("disabled") is False
    assert coerce_optional_bool(None) is None
    assert type_member_text(None, "name") is None
    assert (
        type_member_text({"name": BadString(), "serial": "SERIAL-1"}, "name", "serial")
        == "SERIAL-1"
    )
    assert heatpump_member_device_type({"device-type": "iq_er"}) == "IQ_ER"
    assert heatpump_member_device_type({"device_type": BadString()}) is None
    assert heatpump_status_text({"statusText": "Running"}) == "Running"
    assert heatpump_status_text({"status": "not_reporting"}) == "Not Reporting"
    assert heatpump_status_text({"status": BadString()}) is None
    assert HeatpumpRuntime._sum_optional_values("bad") is None
    assert HeatpumpRuntime._sum_optional_values([1.0, float("inf"), 2.0]) == 3.0


def test_heatpump_runtime_type_helpers_cover_guard_paths(coordinator_factory) -> None:
    runtime = coordinator_factory().heatpump_runtime

    class BadCount:
        def __int__(self) -> int:
            raise RuntimeError("boom")

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {"count": BadCount(), "devices": "bad"},
        "envoy": {"count": 1, "devices": [{"serial": "ENV-1"}, "bad"]},
    }

    assert runtime.has_type(None) is False
    assert runtime.has_type("heatpump") is False
    assert runtime._type_bucket_members(None) == []  # noqa: SLF001
    assert runtime._type_bucket_members("heatpump") == []  # noqa: SLF001
    assert runtime._type_bucket_members("envoy") == [
        {"serial": "ENV-1"}
    ]  # noqa: SLF001
