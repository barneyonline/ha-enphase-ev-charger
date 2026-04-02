from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.enphase_ev import parsing_helpers as parsing_helpers_mod
from custom_components.enphase_ev import api
from custom_components.enphase_ev import heatpump_runtime as heatpump_runtime_mod
from custom_components.enphase_ev.heatpump_runtime import HeatpumpRuntime
from custom_components.enphase_ev.parsing_helpers import (
    coerce_optional_bool,
    coerce_optional_float,
    coerce_optional_text,
    heatpump_device_state,
    heatpump_lifecycle_status_text,
    heatpump_member_device_type,
    heatpump_operational_status_text,
    heatpump_pairing_status,
    heatpump_status_bucket,
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
async def test_heatpump_runtime_public_async_wrappers(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().heatpump_runtime
    runtime._async_refresh_hems_support_preflight = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_runtime_state = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_daily_consumption = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_power = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001

    await runtime.async_refresh_hems_support_preflight(force=True)
    await runtime.async_refresh_heatpump_runtime_state(force=True)
    await runtime.async_refresh_heatpump_daily_consumption(force=True)
    await runtime.async_refresh_heatpump_power(force=True)

    runtime._async_refresh_hems_support_preflight.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    runtime._async_refresh_heatpump_runtime_state.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    runtime._async_refresh_heatpump_daily_consumption.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    runtime._async_refresh_heatpump_power.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )


@pytest.mark.asyncio
async def test_heatpump_runtime_diagnostics_refreshes_power_snapshot(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory(serials=[]).heatpump_runtime
    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {"type_key": "heatpump", "count": 1, "devices": [{}]}
    }
    runtime._async_refresh_heatpump_runtime_state = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_daily_consumption = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_power = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001

    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)

    runtime._async_refresh_heatpump_runtime_state.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    runtime._async_refresh_heatpump_daily_consumption.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    runtime._async_refresh_heatpump_power.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )


@pytest.mark.asyncio
async def test_heatpump_runtime_diagnostics_logs_power_refresh_failures(
    coordinator_factory, caplog
) -> None:
    raw_uid = "DEVICE-UID-123456789"
    runtime = coordinator_factory(serials=[]).heatpump_runtime
    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "type_key": "heatpump",
            "count": 1,
            "devices": [{"device_uid": raw_uid}],
        }
    }
    runtime._async_refresh_heatpump_runtime_state = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_daily_consumption = AsyncMock()  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_power = AsyncMock(side_effect=RuntimeError(f"power {raw_uid}"))  # type: ignore[assignment]  # noqa: SLF001

    with caplog.at_level(logging.DEBUG):
        await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)

    assert "Heat pump power diagnostics refresh failed for site" in caplog.text
    assert raw_uid not in caplog.text
    assert "DEVI...6789" in caplog.text


@pytest.mark.asyncio
async def test_heatpump_runtime_power_failure_logs_truncated_device_uid(
    coordinator_factory, caplog
) -> None:
    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord.heatpump_runtime._async_refresh_hems_support_preflight = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.client.hems_energy_consumption = AsyncMock(
        side_effect=RuntimeError("fetch failed for HP-1")
    )

    with caplog.at_level(logging.DEBUG):
        await coord.heatpump_runtime._async_refresh_heatpump_power(
            force=True
        )  # noqa: SLF001

    assert "Heat pump power daily-consumption path unavailable for site" in caplog.text
    assert "HP-1" not in caplog.text
    assert "H...1" in caplog.text


@pytest.mark.asyncio
async def test_heatpump_runtime_power_logs_fetch_plan_and_selection_summary(
    coordinator_factory, caplog
) -> None:
    primary_uid = "DEVICE-UID-123456789"
    meter_uid = "METER-UID-987654321"
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": primary_uid,
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": meter_uid,
                        "statusText": "Recommended",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-03-13T00:05:00Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": primary_uid,
                        "device_name": "Primary",
                        "consumption": [{"details": [550.0]}],
                    },
                    {
                        "device_uid": meter_uid,
                        "device_name": "Meter",
                        "consumption": [{"details": [25.0]}],
                    },
                ]
            },
        }
    )

    with caplog.at_level(logging.DEBUG):
        await coord.heatpump_runtime._async_refresh_heatpump_power(  # noqa: SLF001
            force=True
        )

    assert "Heat pump power fetch plan for site" in caplog.text
    assert "Heat pump power selected daily-consumption payload for site" in caplog.text
    assert primary_uid not in caplog.text
    assert meter_uid not in caplog.text
    assert "DEVI...6789" in caplog.text
    assert "'detail_value_w': 550.0" in caplog.text
    assert coord.heatpump_power_device_uid == primary_uid
    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["compare_all"] is True
    assert power_snapshot["previous_device_ref"] is None
    assert power_snapshot["outcome"] == "selected_sample"
    assert power_snapshot["selected_source"] == "hems_energy_consumption:DEVI...6789"
    assert power_snapshot["selected_payload"]["resolved_device_ref"] == "DEVI...6789"
    assert power_snapshot["selected_payload"]["detail_value_w"] == pytest.approx(550.0)
    assert power_snapshot["selected_sample_at_utc"] is not None
    assert len(power_snapshot["attempts"]) == 1


@pytest.mark.asyncio
async def test_heatpump_runtime_power_logs_when_no_candidate_payload_is_usable(
    coordinator_factory, caplog
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(return_value=None)

    with caplog.at_level(logging.DEBUG):
        await coord.heatpump_runtime._async_refresh_heatpump_power(  # noqa: SLF001
            force=True
        )

    assert coord.heatpump_power_w is None
    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["outcome"] == "no_usable_payload"
    assert power_snapshot["attempts"] == []


@pytest.mark.asyncio
async def test_heatpump_runtime_power_snapshot_handles_selected_payload_without_uid(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "statusText": "Normal"}],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-03-13T00:05:00Z",
            "data": {"heat-pump": [{"consumption": [{"details": [125.0]}]}]},
        }
    )

    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(125.0)
    assert coord.heatpump_power_device_uid is None
    assert coord.heatpump_power_source == "hems_energy_consumption"
    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["selected_source"] == "hems_energy_consumption"


@pytest.mark.asyncio
async def test_heatpump_runtime_power_snapshot_redacts_identifier_bearing_errors(
    coordinator_factory,
) -> None:
    raw_uid = "DEVICE-UID-123456789"
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": raw_uid,
                        "serial_number": "SERIAL-123456",
                    }
                ],
            }
        },
        ["heatpump"],
    )

    async def _raise_fetch_error(
        *, start_at=None, end_at=None, timezone=None, step=None
    ):
        raise RuntimeError(f"fetch failed for {raw_uid} serial SERIAL-123456")

    coord.client.hems_energy_consumption = _raise_fetch_error  # type: ignore[method-assign]
    coord.heatpump_runtime._async_refresh_hems_support_preflight = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001

    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001

    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["outcome"] == "fetch_error"
    attempt_error = power_snapshot["attempts"][0]["error"]
    assert raw_uid not in attempt_error
    assert "SERIAL-123456" not in attempt_error
    assert "DEVI...6789" in attempt_error
    assert "SERI...3456" in attempt_error
    assert raw_uid not in power_snapshot["last_error"]
    assert "DEVI...6789" in power_snapshot["last_error"]


@pytest.mark.asyncio
async def test_heatpump_runtime_public_api_access(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = MagicMock()
    runtime._heatpump_primary_member.return_value = {"device_uid": "HP-1"}
    runtime._heatpump_primary_device_uid.return_value = "HP-1"
    runtime._heatpump_runtime_device_uid.return_value = "HP-RUNTIME"
    runtime._heatpump_daily_window.return_value = (
        "2026-03-27T00:00:00.000Z",
        "2026-03-27T23:59:59.999Z",
        "UTC",
        ("2026-03-27", "UTC"),
    )
    runtime._build_heatpump_daily_consumption_snapshot.return_value = {
        "daily_energy_wh": 123.0
    }
    runtime._heatpump_power_candidate_device_uids.return_value = ["HP-1", None]
    runtime._heatpump_member_for_uid.return_value = {"device_uid": "HP-1"}
    runtime._heatpump_member_primary_id.return_value = "PRIMARY-1"
    runtime._heatpump_member_parent_id.return_value = "PARENT-1"
    runtime._heatpump_member_alias_map.return_value = {"HP-1": "HP-1"}
    runtime._heatpump_power_inventory_marker.return_value = ()
    runtime._heatpump_power_fetch_plan.return_value = (["HP-1"], False, ())
    runtime._heatpump_power_candidate_is_recommended.return_value = True
    runtime._heatpump_power_candidate_type_rank.return_value = 3
    runtime._heatpump_power_selection_key.return_value = (1, 1, 1, 3, 500.0, 1, 0)
    runtime.async_refresh_hems_support_preflight = AsyncMock()
    runtime.async_ensure_heatpump_runtime_diagnostics = AsyncMock()
    runtime.async_refresh_heatpump_runtime_state = AsyncMock()
    runtime.async_refresh_heatpump_daily_consumption = AsyncMock()
    runtime.async_refresh_heatpump_power = AsyncMock()
    runtime.heatpump_runtime_diagnostics.return_value = {
        "runtime_state": {},
        "event_summary": {
            "known_event_counts": {},
            "unknown_event_keys": [],
        },
    }
    runtime.heatpump_runtime_state = {"device_uid": "HP-1"}
    runtime.heatpump_runtime_state_last_error = "runtime boom"
    runtime.heatpump_daily_consumption = {"daily_energy_wh": 123.0}
    runtime.heatpump_daily_consumption_last_error = "daily boom"
    runtime.heatpump_power_w = 640.0
    runtime.heatpump_power_sample_utc = datetime(2026, 3, 27, tzinfo=timezone.utc)
    runtime.heatpump_power_start_utc = datetime(2026, 3, 27, tzinfo=timezone.utc)
    runtime.heatpump_power_device_uid = "HP-1"
    runtime.heatpump_power_source = "hems_energy_consumption:HP-1"
    runtime.heatpump_power_last_error = "power boom"
    coord.heatpump_runtime = runtime

    assert runtime._heatpump_primary_member() == {"device_uid": "HP-1"}  # noqa: SLF001
    assert runtime._heatpump_primary_device_uid() == "HP-1"  # noqa: SLF001
    assert runtime._heatpump_runtime_device_uid() == "HP-RUNTIME"  # noqa: SLF001
    assert runtime._heatpump_daily_window() == (  # noqa: SLF001
        "2026-03-27T00:00:00.000Z",
        "2026-03-27T23:59:59.999Z",
        "UTC",
        ("2026-03-27", "UTC"),
    )
    assert runtime._build_heatpump_daily_consumption_snapshot(
        {"data": {}}
    ) == {  # noqa: SLF001
        "daily_energy_wh": 123.0
    }
    assert runtime._heatpump_power_candidate_device_uids() == [
        "HP-1",
        None,
    ]  # noqa: SLF001
    assert runtime._heatpump_member_for_uid("HP-1") == {
        "device_uid": "HP-1"
    }  # noqa: SLF001
    assert (
        runtime._heatpump_member_primary_id({"device_uid": "PRIMARY-1"})  # noqa: SLF001
        == "PRIMARY-1"
    )
    assert (
        runtime._heatpump_member_parent_id({"parent": "PARENT-1"}) == "PARENT-1"
    )  # noqa: SLF001
    assert runtime._heatpump_member_alias_map() == {"HP-1": "HP-1"}  # noqa: SLF001
    assert runtime._heatpump_power_inventory_marker() == ()  # noqa: SLF001
    assert runtime._heatpump_power_fetch_plan() == (["HP-1"], False, ())  # noqa: SLF001
    assert (
        runtime._heatpump_power_candidate_is_recommended("HP-1") is True
    )  # noqa: SLF001
    assert (
        runtime._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "HP-1",
            is_recommended=True,
        )
        == 3
    )
    assert runtime._heatpump_power_selection_key(  # noqa: SLF001
        {},
        requested_uid="HP-1",
        sample=(0, 500.0),
    ) == (1, 1, 1, 3, 500.0, 1, 0)

    await runtime.async_refresh_hems_support_preflight(force=True)
    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)
    await runtime.async_refresh_heatpump_runtime_state(force=True)
    await runtime.async_refresh_heatpump_daily_consumption(force=True)
    await runtime.async_refresh_heatpump_power(force=True)

    assert runtime.heatpump_runtime_diagnostics()["runtime_state"] == {}
    assert runtime.heatpump_runtime_diagnostics()["event_summary"] == {
        "known_event_counts": {},
        "unknown_event_keys": [],
    }
    assert runtime.heatpump_runtime_state == {"device_uid": "HP-1"}
    assert runtime.heatpump_runtime_state_last_error == "runtime boom"
    assert runtime.heatpump_daily_consumption == {"daily_energy_wh": 123.0}
    assert runtime.heatpump_daily_consumption_last_error == "daily boom"
    assert runtime.heatpump_power_w == 640.0
    assert runtime.heatpump_power_sample_utc == datetime(
        2026, 3, 27, tzinfo=timezone.utc
    )
    assert runtime.heatpump_power_start_utc == datetime(
        2026, 3, 27, tzinfo=timezone.utc
    )
    assert runtime.heatpump_power_device_uid == "HP-1"
    assert runtime.heatpump_power_source == "hems_energy_consumption:HP-1"
    assert runtime.heatpump_power_last_error == "power boom"

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
    runtime.async_refresh_hems_support_preflight.assert_awaited_once_with(force=True)
    runtime.async_ensure_heatpump_runtime_diagnostics.assert_awaited_once_with(
        force=True
    )
    runtime.async_refresh_heatpump_runtime_state.assert_awaited_once_with(force=True)
    runtime.async_refresh_heatpump_daily_consumption.assert_awaited_once_with(
        force=True
    )
    runtime.async_refresh_heatpump_power.assert_awaited_once_with(force=True)


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


def test_heatpump_runtime_helper_edge_branches(coordinator_factory) -> None:
    runtime = coordinator_factory().heatpump_runtime

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {"devices": [{"device_type": "ENERGY_METER"}]}
    }
    assert runtime._heatpump_primary_member() == {
        "device_type": "ENERGY_METER"
    }  # noqa: SLF001

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "devices": [
                {"device_type": "ENERGY_METER"},
                {"device_type": "SG_READY_GATEWAY", "device_uid": "GW-1"},
                {
                    "device_type": "HEAT_PUMP",
                    "device_uid": "HP-1",
                    "uid": "HP-ALIAS",
                    "serial_number": "SER-1",
                    "parent": "GW-1",
                    "statusText": "Recommended",
                },
                {
                    "device_type": "HEAT_PUMP",
                    "uid": "HP-2",
                    "statusText": "Recommended",
                },
            ]
        }
    }

    assert runtime._heatpump_primary_device_uid() == "HP-1"  # noqa: SLF001
    assert runtime._heatpump_runtime_device_uid() == "HP-1"  # noqa: SLF001
    assert runtime._heatpump_member_for_uid("missing") is None  # noqa: SLF001
    assert runtime._heatpump_member_aliases(None) == []  # noqa: SLF001
    assert runtime._heatpump_member_alias_map()["HP-ALIAS"] == "HP-1"  # noqa: SLF001
    marker = runtime._heatpump_power_inventory_marker()  # noqa: SLF001
    assert marker[0][0] == "GW-1"
    assert (
        runtime._heatpump_power_candidate_is_recommended(None) is False
    )  # noqa: SLF001
    assert (
        runtime._heatpump_power_candidate_is_recommended("HP-1") is True
    )  # noqa: SLF001
    assert (
        runtime._heatpump_power_candidate_is_recommended("GW-1") is True
    )  # noqa: SLF001

    runtime._type_device_buckets = {"heatpump": {"devices": []}}  # noqa: SLF001
    assert (
        runtime._heatpump_power_candidate_is_recommended("HP-1") is False
    )  # noqa: SLF001

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {"devices": [{"device_uid": "FALLBACK-1"}]}
    }
    assert runtime._heatpump_primary_device_uid() == "FALLBACK-1"  # noqa: SLF001

    runtime._type_device_buckets = {"heatpump": {"devices": [{}]}}  # noqa: SLF001
    assert runtime._heatpump_primary_device_uid() is None  # noqa: SLF001
    assert runtime._heatpump_power_inventory_marker()[0][0] == "idx:0"  # noqa: SLF001


def test_heatpump_runtime_power_helper_edge_branches(monkeypatch) -> None:
    class BadStart:
        def __add__(self, _other):
            raise OverflowError("boom")

    assert (
        HeatpumpRuntime._infer_heatpump_interval_minutes(
            None, 1, datetime.now(timezone.utc)
        )
        is None
    )
    assert (
        HeatpumpRuntime._infer_heatpump_interval_minutes(
            BadStart(), 1, datetime.now(timezone.utc)
        )
        is None
    )
    assert HeatpumpRuntime._heatpump_latest_power_sample("bad") is None
    assert (
        HeatpumpRuntime._heatpump_latest_power_sample({"heat_pump_consumption": "bad"})
        is None
    )

    naive_now = datetime(2026, 3, 27, 12, 0)
    monkeypatch.setattr(heatpump_runtime_mod.dt_util, "utcnow", lambda: naive_now)

    future_payload = {
        "start_date": "3026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [1.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(future_payload) is None

    payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [None, "bad", float("inf"), 0.5, 10.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(payload) == (4, 10.0)

    open_only_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [None, None, 2.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(open_only_payload) == (2, 2.0)

    invalid_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [None, "bad", float("nan")],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(invalid_payload) is None

    monkeypatch.setattr(
        heatpump_runtime_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 3, 27, 2, 30),
    )
    provisional_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [50.0, 2.0, 0.1],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(provisional_payload) == (
        1,
        2.0,
    )

    completed_zero_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [50.0, 0.0, 1.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(completed_zero_payload) == (
        2,
        1.0,
    )

    open_missing_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [50.0, 2.0, None],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(open_missing_payload) == (
        1,
        2.0,
    )

    completed_missing_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [None, None, 3.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(completed_missing_payload) == (
        2,
        3.0,
    )

    open_selected_payload = {
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 60,
        "heat_pump_consumption": [50.0, 2.0, 5.0],
    }
    assert HeatpumpRuntime._heatpump_latest_power_sample(open_selected_payload) == (
        2,
        5.0,
    )

    assert (
        HeatpumpRuntime._infer_heatpump_interval_minutes(
            datetime(2026, 3, 27, tzinfo=timezone.utc),
            1,
            datetime(2026, 3, 28, tzinfo=timezone.utc),
        )
        == 60
    )


def test_heatpump_runtime_power_helper_additional_coverage(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 4,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "pairing-status": "PAIRED",
                        "device-state": "ACTIVE",
                        "statusText": "Normal",
                    },
                    {"device_type": "ENERGY_METER", "device_uid": "MTR-1"},
                    {"device_type": "SG_READY_GATEWAY", "device_uid": "SG-1"},
                    {"device_type": "OTHER", "device_uid": "OT-1"},
                ],
            }
        },
        ["heatpump"],
    )

    monkeypatch.setattr(
        heatpump_runtime_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 3, 27, 2, 5, tzinfo=timezone.utc),
    )
    assert HeatpumpRuntime._heatpump_latest_power_sample(
        {
            "start_date": "2026-03-27T00:00:00Z",
            "heat_pump_consumption": [1.0, 2.0],
        }
    ) == (1, 2.0)

    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "MTR-1",
            is_recommended=False,
        )
        == 0
    )
    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "MTR-1",
            is_recommended=True,
        )
        == 3
    )
    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "HP-1",
            is_recommended=True,
        )
        == 2
    )
    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "SG-1",
            is_recommended=True,
        )
        == 1
    )
    assert (
        coord._heatpump_power_candidate_type_rank(  # noqa: SLF001
            {},
            "OT-1",
            is_recommended=True,
        )
        == 0
    )

    assert coord._heatpump_power_selection_key(  # noqa: SLF001
        {"device_uid": "HP-1"},
        requested_uid="REQ-1",
        sample=None,
    ) == (0, 0, 0, 0, float("-inf"), 1, -1)
    assert coord._heatpump_power_selection_key(  # noqa: SLF001
        {"uid": "MTR-1"},
        requested_uid=None,
        sample=(2, 0.0),
    ) == (1, 0, 0, 0, 0.0, 1, 2)

    summary = runtime._heatpump_power_debug_payload_summary(  # noqa: SLF001
        {
            "device_uid": "HP-1",
            "heat_pump_consumption": [None, "bad", 1.0, 2.0, float("inf")],
            "start_date": "2026-03-27T00:00:00Z",
            "interval_minutes": "15",
        },
        requested_uid="REQ-1",
        sample=(3, 2.0),
        selection_key=(1, 1, 1, 2, 2.0, 1, 3),
    )
    assert summary == {
        "requested_device_ref": "R...1",
        "payload_device_ref": "H...1",
        "resolved_device_ref": "H...1",
        "member_device_type": "HEAT_PUMP",
        "pairing_status": "PAIRED",
        "device_state": "ACTIVE",
        "status": "Normal",
        "recommended": False,
        "bucket_count": 5,
        "non_null_bucket_count": 3,
        "sample_tail": [
            {"index": 4, "value_w": float("inf")},
            {"index": 3, "value_w": 2.0},
            {"index": 2, "value_w": 1.0},
        ],
        "latest_sample_index": 3,
        "latest_sample_w": 2.0,
        "start_date": "2026-03-27T00:00:00Z",
        "interval_minutes": 15.0,
        "selection_key": [1, 1, 1, 2, 2.0, 1, 3],
    }


@pytest.mark.asyncio
async def test_heatpump_runtime_diagnostics_and_refresh_edge_branches(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {"device_type": "HEAT_PUMP", "device_uid": "HP-1"},
                    {"device_type": "HEAT_PUMP", "device_uid": "HP-2"},
                    {"device_type": "HEAT_PUMP"},
                ],
            }
        },
        ["heatpump"],
    )

    runtime._async_refresh_heatpump_runtime_state = AsyncMock(side_effect=RuntimeError("runtime"))  # type: ignore[assignment]  # noqa: SLF001
    runtime._async_refresh_heatpump_daily_consumption = AsyncMock(side_effect=RuntimeError("daily"))  # type: ignore[assignment]  # noqa: SLF001
    coord.client.show_livestream = AsyncMock(return_value={"live": True})
    coord.client.heat_pump_events_json = AsyncMock(
        side_effect=["EVENT_SCALAR", ["list-payload"]]
    )
    coord.client.iq_er_events_json = AsyncMock(return_value="EVENT_NONE")

    def _redact(payload):
        if payload == "EVENT_SCALAR":
            return "scalar"
        if payload == "EVENT_NONE":
            return None
        return payload

    monkeypatch.setattr(heatpump_runtime_mod, "redact_battery_payload", _redact)

    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)

    diagnostics = coord.heatpump_runtime_diagnostics()
    assert diagnostics["show_livestream_payload"] == {"live": True}
    assert diagnostics["events_payloads"][0]["payload"] == {"value": "scalar"}
    assert diagnostics["events_payloads"][1]["payload"] == ["list-payload"]
    assert diagnostics["event_summary"] == {
        "known_event_counts": {},
        "unknown_event_keys": [],
    }

    runtime._heatpump_runtime_diagnostics_cache_until = None  # noqa: SLF001
    coord.client.show_livestream = AsyncMock(return_value=None)
    coord.client.heat_pump_events_json = None
    coord.client.iq_er_events_json = None
    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)
    assert coord.heatpump_runtime_diagnostics()["show_livestream_payload"] is None

    runtime._heatpump_runtime_diagnostics_cache_until = None  # noqa: SLF001
    coord.client.show_livestream = AsyncMock(side_effect=RuntimeError("live boom"))
    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)
    assert coord.heatpump_runtime_diagnostics()["show_livestream_payload"] is None
    assert coord.heatpump_runtime_diagnostics()["last_error"] == "live boom"

    runtime._async_refresh_heatpump_daily_consumption = HeatpumpRuntime._async_refresh_heatpump_daily_consumption.__get__(  # type: ignore[method-assign]  # noqa: SLF001
        runtime, HeatpumpRuntime
    )

    runtime._heatpump_power_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.hems_energy_consumption = AsyncMock(
        side_effect=AssertionError("no fetch")
    )
    await runtime.async_refresh_heatpump_power()
    coord.client.hems_energy_consumption.assert_not_awaited()

    runtime._heatpump_power_cache_until = None  # noqa: SLF001
    runtime._heatpump_power_backoff_until = time.monotonic() + 60  # noqa: SLF001
    await runtime.async_refresh_heatpump_power()
    coord.client.hems_energy_consumption.assert_not_awaited()

    runtime._heatpump_power_backoff_until = None  # noqa: SLF001
    coord.client._hems_site_supported = False  # noqa: SLF001
    await runtime.async_refresh_heatpump_power(force=True)
    assert coord.heatpump_power_source is None

    coord.client._hems_site_supported = True  # noqa: SLF001
    coord.client.hems_energy_consumption = None
    await runtime.async_refresh_heatpump_power(force=True)
    assert coord.heatpump_power_w is None

    coord.client.hems_energy_consumption = AsyncMock(return_value=None)
    await runtime.async_refresh_heatpump_power(force=True)
    assert coord.heatpump_power_w is None

    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "bad",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-1",
                        "consumption": [{"details": [None]}],
                    }
                ]
            },
        }
    )
    await runtime.async_refresh_heatpump_power(force=True)
    assert coord.heatpump_power_w == pytest.approx(0.0)
    assert coord.heatpump_power_sample_utc is None

    class BadFloat:
        def __float__(self) -> float:
            raise RuntimeError("boom")

    class BadString:
        def __str__(self) -> str:
            raise RuntimeError("boom")

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
    assert HeatpumpRuntime._first_optional_numeric_value("bad") is None
    assert (
        HeatpumpRuntime._first_optional_numeric_value(
            [None, "bad", float("inf"), float("nan")]
        )
        is None
    )
    assert HeatpumpRuntime._first_optional_numeric_value(
        [None, "2.5"]
    ) == pytest.approx(2.5)


def test_heatpump_runtime_recommended_parent_matching(coordinator_factory) -> None:
    runtime = coordinator_factory().heatpump_runtime

    class BadString:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "devices": [
                {"device_uid": "PARENT-1", "statusText": "Recommended"},
                {"device_uid": "CHILD-1", "parent": "PARENT-1"},
                {
                    "device_uid": "REC-CHILD",
                    "parent": "PARENT-1",
                    "statusText": "Recommended",
                },
            ]
        }
    }

    assert (
        runtime._heatpump_power_candidate_is_recommended("CHILD-1") is True
    )  # noqa: SLF001

    runtime._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "devices": [
                {"device_uid": "CHILD-1", "parent": "PARENT-1"},
                {
                    "device_uid": "REC-CHILD",
                    "parent": "PARENT-1",
                    "statusText": "Recommended",
                },
            ]
        }
    }
    assert (
        runtime._heatpump_power_candidate_is_recommended("CHILD-1") is True
    )  # noqa: SLF001
    assert (
        type_member_text({"name": BadString(), "serial": "SERIAL-1"}, "name", "serial")
        == "SERIAL-1"
    )
    assert heatpump_member_device_type({"device-type": "iq_er"}) == "IQ_ER"
    assert heatpump_member_device_type({"device_type": BadString()}) is None
    assert heatpump_pairing_status(None) is None
    assert heatpump_device_state(None) is None
    assert heatpump_device_state({"device-state": "inactive"}) == "INACTIVE"
    assert parsing_helpers_mod._friendly_heatpump_status(None) is None  # noqa: SLF001
    assert heatpump_lifecycle_status_text(None) is None
    assert heatpump_operational_status_text(None) is None
    assert heatpump_status_bucket("") == "unknown"
    assert heatpump_status_bucket("unpaired") == "not_reporting"
    assert heatpump_status_bucket("pending") == "warning"
    assert heatpump_status_text({"statusText": "Running"}) == "Running"
    assert heatpump_status_text({"status": "not_reporting"}) == "Not Reporting"
    assert (
        heatpump_status_text({"statusText": "Fault", "pairing-status": "UNPAIRED"})
        == "Fault"
    )
    assert (
        heatpump_status_text({"statusText": "Normal", "device-state": "INACTIVE"})
        == "Inactive"
    )
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
    assert runtime._heatpump_runtime_member() is None  # noqa: SLF001
    assert runtime.has_type("heatpump") is False
    assert runtime._type_bucket_members(None) == []  # noqa: SLF001
    assert runtime._type_bucket_members("heatpump") == []  # noqa: SLF001
    assert runtime._type_bucket_members("envoy") == [
        {"serial": "ENV-1"}
    ]  # noqa: SLF001


def test_parse_inverter_last_report_handles_none_epoch_value(monkeypatch) -> None:
    monkeypatch.setattr(
        parsing_helpers_mod,
        "float",
        lambda _value: None,
        raising=False,
    )

    assert parse_inverter_last_report(1711843200) is None


@pytest.mark.asyncio
async def test_refresh_heatpump_power_tracks_latest_valid_sample(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord._battery_timezone = "UTC"  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "statusText": "Normal",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-02-27T00:10:00Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-1",
                        "device_name": "Waermepumpe",
                        "consumption": [{"details": [500.5, None]}],
                    }
                ]
            },
        }
    )

    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(500.5)
    assert coord.heatpump_power_device_uid == "HP-1"
    assert coord.heatpump_power_source == "hems_energy_consumption:HP-1"
    assert coord.heatpump_power_start_utc is None
    assert coord.heatpump_power_sample_utc is not None
    assert coord.heatpump_power_last_error is None
    assert coord._heatpump_power_cache_until is not None  # noqa: SLF001
    assert (
        coord._heatpump_power_cache_until
        == coord._heatpump_daily_consumption_cache_until
    )  # noqa: SLF001
    first_call = coord.client.hems_energy_consumption.await_args_list[0]
    assert first_call.kwargs["timezone"] == "UTC"
    assert first_call.kwargs["step"] == "P1D"

    coord._heatpump_power_cache_until = None  # noqa: SLF001
    coord.client.hems_energy_consumption = AsyncMock(side_effect=RuntimeError("boom"))
    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_power_last_error == "boom"
    assert coord._heatpump_power_backoff_until is not None  # noqa: SLF001

    coord.inventory_runtime._set_type_device_buckets({}, [])  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_power(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_power_w is None
    assert coord.heatpump_power_source is None


@pytest.mark.asyncio
async def test_refresh_heatpump_power_prefers_energy_consumption_details(
    coordinator_factory, caplog
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-04-02"}  # noqa: SLF001
    coord._battery_timezone = "Europe/Berlin"  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "statusText": "Normal",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-04-01T22:31:12.407610009Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-1",
                        "device_name": "Waermepumpe",
                        "consumption": [
                            {
                                "solar": 0,
                                "battery": 0,
                                "grid": 0,
                                "details": [3],
                            }
                        ],
                    }
                ]
            },
        }
    )
    coord.client.hems_power_timeseries = AsyncMock(
        side_effect=AssertionError("timeseries should not be used")
    )

    with caplog.at_level(logging.DEBUG):
        await coord.heatpump_runtime._async_refresh_heatpump_power(
            force=True
        )  # noqa: SLF001

    assert coord.heatpump_power_w == pytest.approx(3.0)
    assert coord.heatpump_power_device_uid == "HP-1"
    assert coord.heatpump_power_source == "hems_energy_consumption:HP-1"
    assert coord.heatpump_power_sample_utc == datetime(
        2026, 4, 1, 22, 31, 12, 407610, tzinfo=timezone.utc
    )
    coord.client.hems_power_timeseries.assert_not_awaited()
    power_snapshot = coord.heatpump_runtime.heatpump_runtime_diagnostics()[
        "power_snapshot"
    ]
    assert power_snapshot["outcome"] == "selected_sample"
    assert power_snapshot["selected_source"] == "hems_energy_consumption:H...1"
    assert power_snapshot["selected_payload"]["detail_value_w"] == pytest.approx(3.0)
    assert "Heat pump power selected daily-consumption payload for site" in caplog.text

    coord._heatpump_daily_consumption = {"details": ["bad", None]}  # noqa: SLF001
    summary = coord.heatpump_runtime._heatpump_power_summary_from_daily_snapshot(  # noqa: SLF001
        coord._heatpump_daily_consumption  # noqa: SLF001
    )
    assert summary is not None
    assert summary[0] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_refresh_heatpump_runtime_state_uses_dedicated_heatpump_uid(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 3,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-1",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-EM-1",
                    },
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "name": "Primary Heat Pump",
                        "pairing_status": "PAIRED",
                        "device_state": "ACTIVE",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_heatpump_state = AsyncMock(
        return_value={
            "device_uid": "HP-1",
            "heatpump_status": "RUNNING",
            "sg_ready_mode_raw": "MODE_3",
            "sg_ready_mode_label": "Recommended",
            "sg_ready_active": True,
            "sg_ready_contact_state": "closed",
            "last_report_at": "2026-03-20T08:18:59.604Z",
        }
    )

    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001

    coord.client.hems_heatpump_state.assert_awaited_once()
    assert coord.client.hems_heatpump_state.await_args.kwargs["device_uid"] == "HP-1"
    assert coord.heatpump_runtime_state["device_uid"] == "HP-1"
    assert coord.heatpump_runtime_state["member_name"] == "Primary Heat Pump"
    assert coord.heatpump_runtime_state["member_device_type"] == "HEAT_PUMP"
    assert coord.heatpump_runtime_state["pairing_status"] == "PAIRED"
    assert coord.heatpump_runtime_state["device_state"] == "ACTIVE"
    assert coord.heatpump_runtime_state["source"] == "hems_heatpump_state:HP-1"


@pytest.mark.asyncio
async def test_refresh_heatpump_runtime_state_covers_cache_and_error_paths(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "name": "Primary Heat Pump",
                        "pairing_status": "PAIRED",
                        "device_state": "ACTIVE",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord.heatpump_runtime._async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value=None
    )
    mono_now = 1_000.0
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: mono_now)

    coord.client.hems_heatpump_state = AsyncMock(side_effect=AssertionError("cached"))
    coord._heatpump_runtime_state_cache_until = mono_now + 10  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state()  # noqa: SLF001
    coord.client.hems_heatpump_state.assert_not_awaited()

    coord._heatpump_runtime_state_cache_until = None  # noqa: SLF001
    coord._heatpump_runtime_state_backoff_until = mono_now + 10  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state()  # noqa: SLF001
    coord.client.hems_heatpump_state.assert_not_awaited()

    coord._heatpump_runtime_state_backoff_until = None  # noqa: SLF001
    coord.client._hems_site_supported = False  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_runtime_state == {}
    assert coord.heatpump_runtime_state_last_error is None

    coord.client._hems_site_supported = None  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP"}],
            }
        },
        ["heatpump"],
    )
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_runtime_state == {}

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_heatpump_state = None
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001

    coord.client.hems_heatpump_state = AsyncMock(side_effect=RuntimeError("boom"))
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_runtime_state_last_error == "boom"
    assert coord._heatpump_runtime_state_backoff_until is not None  # noqa: SLF001

    coord._heatpump_runtime_state_backoff_until = None  # noqa: SLF001
    coord.client.hems_heatpump_state = AsyncMock(return_value=None)
    await coord.heatpump_runtime._async_refresh_heatpump_runtime_state(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_runtime_state == {}


@pytest.mark.asyncio
async def test_refresh_heatpump_daily_consumption_tracks_site_day(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-20"}  # noqa: SLF001
    coord._battery_timezone = "Europe/Berlin"  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-03-20T07:53:00.739143826Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-1",
                        "device_name": "Waermepumpe",
                        "consumption": [
                            {
                                "solar": 10.0,
                                "battery": 20.0,
                                "grid": 200.0,
                                "details": [230.0],
                            }
                        ],
                    }
                ]
            },
        }
    )

    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(  # noqa: SLF001
        force=True
    )

    coord.client.hems_energy_consumption.assert_awaited_once()
    kwargs = coord.client.hems_energy_consumption.await_args.kwargs
    assert kwargs["timezone"] == "Europe/Berlin"
    assert kwargs["step"] == "P1D"
    assert kwargs["start_at"] == "2026-03-19T23:00:00.000Z"
    assert kwargs["end_at"] == "2026-03-20T22:59:59.999Z"
    assert coord.heatpump_daily_consumption["daily_energy_wh"] == pytest.approx(230.0)
    assert coord.heatpump_daily_consumption["daily_grid_wh"] == pytest.approx(200.0)
    assert coord.heatpump_daily_consumption["source"] == "hems_energy_consumption:HP-1"
    assert coord.heatpump_daily_consumption["sampled_at_utc"].startswith(
        "2026-03-20T07:53:00.739143"
    )


def test_heatpump_daily_window_handles_dst_transition_day(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._battery_timezone = "Europe/Berlin"  # noqa: SLF001
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-29"}  # noqa: SLF001

    assert coord.heatpump_runtime._heatpump_daily_window() == (  # noqa: SLF001
        "2026-03-28T23:00:00.000Z",
        "2026-03-29T21:59:59.999Z",
        "Europe/Berlin",
        ("2026-03-29", "Europe/Berlin"),
    )


def test_heatpump_daily_helper_and_property_edge_cases(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._battery_timezone = "Not/A-Timezone"  # noqa: SLF001
    assert coord._site_timezone_name() == "UTC"  # noqa: SLF001
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        heatpump_runtime_mod,
        "resolve_site_timezone_name",
        lambda _value: "Not/A-Timezone",
    )
    monkeypatch.setattr(
        heatpump_runtime_mod,
        "resolve_site_local_current_date",
        lambda _payload, _timezone: "bad-date",
    )
    assert coord.heatpump_runtime._heatpump_daily_window() is None  # noqa: SLF001
    monkeypatch.undo()

    assert coord._sum_optional_values("bad") is None  # noqa: SLF001
    assert (
        coord._sum_optional_values([None, "bad", float("inf")]) is None
    )  # noqa: SLF001
    assert coord._sum_optional_values([1.0, "2.5", float("nan")]) == pytest.approx(
        3.5
    )  # noqa: SLF001

    assert (
        coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(["bad"])
        is None
    )  # noqa: SLF001
    assert (
        coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot({"data": []})
        is None
    )  # noqa: SLF001
    assert (
        coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
            {"data": {"heat-pump": []}}
        )
        is None
    )

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    snapshot = coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        {
            "data": {
                "heat-pump": [
                    "skip-me",
                    {
                        "device_uid": "HP-2",
                        "device_name": "Backup",
                        "consumption": [
                            "skip-me",
                            {
                                "solar": "1.0",
                                "battery": "2.0",
                                "grid": "3.0",
                                "details": [4.0, "bad", None],
                            },
                        ],
                    },
                ]
            }
        }
    )
    assert snapshot is None

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "ENERGY_METER", "device_uid": "HP-EM-1"}],
            }
        },
        ["heatpump"],
    )
    snapshot = coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        {
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-2",
                        "device_name": "Backup",
                        "consumption": [
                            {
                                "solar": "1.0",
                                "battery": "2.0",
                                "grid": "3.0",
                                "details": [4.0, "bad", None],
                            },
                        ],
                    }
                ]
            }
        }
    )
    assert snapshot == {
        "device_uid": "HP-2",
        "device_name": "Backup",
        "member_name": None,
        "member_device_type": "ENERGY_METER",
        "pairing_status": None,
        "device_state": None,
        "daily_energy_wh": pytest.approx(4.0),
        "daily_solar_wh": pytest.approx(1.0),
        "daily_battery_wh": pytest.approx(2.0),
        "daily_grid_wh": pytest.approx(3.0),
        "details": [4.0, "bad", None],
        "source": "hems_energy_consumption:HP-2",
        "endpoint_type": None,
        "endpoint_timestamp": None,
        "sampled_at_utc": None,
    }
    snapshot = coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        {
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-2",
                        "device_name": "Backup",
                        "consumption": [
                            {
                                "solar": 0.0,
                                "battery": 0.0,
                                "grid": 0.0,
                                "details": [],
                            },
                        ],
                    }
                ]
            }
        }
    )
    assert snapshot == {
        "device_uid": "HP-2",
        "device_name": "Backup",
        "member_name": None,
        "member_device_type": "ENERGY_METER",
        "pairing_status": None,
        "device_state": None,
        "daily_energy_wh": pytest.approx(0.0),
        "daily_solar_wh": pytest.approx(0.0),
        "daily_battery_wh": pytest.approx(0.0),
        "daily_grid_wh": pytest.approx(0.0),
        "details": [],
        "source": "hems_energy_consumption:HP-2",
        "endpoint_type": None,
        "endpoint_timestamp": None,
        "sampled_at_utc": None,
    }
    assert (
        coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
            {"data": {"heat-pump": [{"device_uid": "HP-1", "consumption": ["bad"]}]}}
        )
        is None
    )


def test_heatpump_event_summary_classifies_known_event_keys(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.heatpump_runtime._heatpump_events_payloads = [  # noqa: SLF001
        {
            "device_uid": "HP-1",
            "payload": [
                {"event_key": "hems_sgready_mode_changed_to_3"},
                {"eventKey": "hems_energy_meter_offline"},
                {"event_key": "custom_unknown_event"},
            ],
        }
    ]

    assert coord.heatpump_runtime._heatpump_event_summary() == {  # noqa: SLF001
        "known_event_counts": {
            "sg_ready_recommended": 1,
            "energy_meter_offline": 1,
        },
        "unknown_event_keys": ["custom_unknown_event"],
    }
    assert coord.heatpump_runtime._hems_event_entries(  # noqa: SLF001
        {"items": [{"event_key": "hems_sgready_mode_changed_to_2"}, "skip-me"]}
    ) == [{"event_key": "hems_sgready_mode_changed_to_2"}]
    coord.heatpump_runtime._heatpump_events_payloads = [  # noqa: SLF001
        {"payload": {"events": [{"event_key": None}, {"eventKey": ""}]}}
    ]
    assert coord.heatpump_runtime._heatpump_event_summary() == {  # noqa: SLF001
        "known_event_counts": {},
        "unknown_event_keys": [],
    }
    assert (
        coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
            {"data": {"heat-pump": ["skip-me"]}}
        )
        is None
    )

    class BadString:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert coord.heatpump_runtime_state == {}
    coord._heatpump_runtime_state_last_error = BadString()  # noqa: SLF001
    assert coord.heatpump_runtime_state_last_error is None
    assert coord.heatpump_daily_consumption == {}
    coord._heatpump_daily_consumption_last_error = BadString()  # noqa: SLF001
    assert coord.heatpump_daily_consumption_last_error is None


@pytest.mark.asyncio
async def test_refresh_heatpump_daily_consumption_covers_cache_and_error_paths(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord.heatpump_runtime._async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        return_value=None
    )
    mono_now = 2_000.0
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: mono_now)
    marker = ("2026-03-20", "UTC")
    coord.heatpump_runtime._heatpump_daily_window = lambda: (  # type: ignore[assignment]  # noqa: SLF001
        "2026-03-20T00:00:00+00:00",
        "2026-03-21T00:00:00+00:00",
        "UTC",
        marker,
    )

    coord.client.hems_energy_consumption = AsyncMock(
        side_effect=AssertionError("cached")
    )
    coord._heatpump_daily_consumption_cache_key = marker  # noqa: SLF001
    coord._heatpump_daily_consumption_cache_until = mono_now + 10  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption()  # noqa: SLF001
    coord.client.hems_energy_consumption.assert_not_awaited()

    coord._heatpump_daily_consumption_cache_until = None  # noqa: SLF001
    coord._heatpump_daily_consumption_backoff_until = mono_now + 10  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption()  # noqa: SLF001
    coord.client.hems_energy_consumption.assert_not_awaited()

    coord.heatpump_runtime._heatpump_daily_window = lambda: None  # type: ignore[assignment]  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001

    coord.heatpump_runtime._heatpump_daily_window = lambda: (  # type: ignore[assignment]  # noqa: SLF001
        "2026-03-20T00:00:00+00:00",
        "2026-03-21T00:00:00+00:00",
        "UTC",
        marker,
    )
    coord.client._hems_site_supported = False  # noqa: SLF001
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_daily_consumption == {}


def test_heatpump_runtime_inventory_merge_and_helper_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    inventory = coord.inventory_runtime

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert inventory._devices_inventory_buckets([{"ok": 1}, "bad"]) == [
        {"ok": 1}
    ]  # noqa: SLF001
    assert (
        inventory._hems_devices_groups({"data": {"hems-devices": []}}) == []
    )  # noqa: SLF001
    normalized = inventory._normalize_hems_member(
        {"device-uid": "HP-1", "serial": "SER-1"}
    )  # noqa: SLF001
    assert normalized["uid"] == "HP-1"
    assert normalized["serial_number"] == "SER-1"
    assert inventory._hems_bucket_type(BadStr()) is None  # noqa: SLF001
    assert (
        inventory._heatpump_worst_status_text({"warning": 1}) == "Warning"
    )  # noqa: SLF001
    assert (
        inventory._heatpump_worst_status_text({"normal": 1}) == "Normal"
    )  # noqa: SLF001

    coord.inventory_runtime._set_type_device_buckets(
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
            }
        },
        ["envoy"],
    )
    inventory._hems_devices_payload = {
        "data": {
            "hems-devices": {
                "heat-pump": [
                    {
                        "device-type": "SG_READY_GATEWAY",
                        "device-uid": "HP-SG-1",
                        "name": "SG Ready Gateway",
                        "last-report": "2026-02-27T09:14:44Z",
                        "status": "normal",
                        "statusText": "Normal",
                        "model": "Expert Net Control 2302",
                    },
                    {
                        "device-type": "ENERGY_METER",
                        "device-uid": "HP-EM-1",
                        "name": "Energy Meter",
                        "last-report": "2026-02-27T09:15:44Z",
                        "statusText": "Warning",
                        "firmware-version": "3.3",
                        "model": "Energy Manager 420",
                    },
                    {
                        "device-type": "HEAT_PUMP",
                        "device-uid": "HP-1",
                        "name": "Waermepumpe",
                        "statusText": "Normal",
                        "model": "Europa Mini WP",
                        "hardware-sku": "HP-SKU-1",
                    },
                ]
            }
        }
    }  # noqa: SLF001
    inventory._merge_heatpump_type_bucket()  # noqa: SLF001
    bucket = coord.inventory_view.type_bucket("heatpump")
    assert bucket is not None
    assert bucket["count"] == 3
    assert bucket["status_counts"]["warning"] == 1
    assert bucket["latest_reported_device"]["device_uid"] == "HP-EM-1"
    assert coord.inventory_view.type_device_model("heatpump") == "Europa Mini WP"
    assert coord.inventory_view.type_device_model_id("heatpump") == "HP-SKU-1"

    inventory._hems_devices_payload = None  # noqa: SLF001
    inventory._devices_inventory_payload = {
        "result": [
            {
                "type": "hemsDevices",
                "devices": [
                    {
                        "gateway": [
                            {
                                "device-type": "IQ_ENERGY_ROUTER",
                                "device-uid": "ROUTER-1",
                            }
                        ]
                    }
                ],
            }
        ]
    }  # noqa: SLF001
    assert (
        inventory._hems_group_members("gateway")[0]["device_uid"] == "ROUTER-1"
    )  # noqa: SLF001


def test_heatpump_runtime_power_helper_paths(coordinator_factory, monkeypatch) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    fixed_now = datetime(2026, 2, 27, 0, 7, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "custom_components.enphase_ev.heatpump_runtime.dt_util.utcnow",
        lambda: fixed_now,
    )

    assert runtime._heatpump_primary_member() is None  # noqa: SLF001
    assert runtime._heatpump_primary_device_uid() is None  # noqa: SLF001

    coord.inventory_runtime._set_type_device_buckets(
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "statusText": "Recommended",
                        "parent_uid": "HP-CTRL",
                    },
                ],
            }
        },
        ["heatpump"],
    )

    assert runtime._heatpump_primary_device_uid() == "HP-CTRL"  # noqa: SLF001
    assert runtime._heatpump_power_candidate_device_uids() == [  # noqa: SLF001
        "HP-CTRL",
        "HP-METER",
        None,
    ]
    assert (
        runtime._heatpump_power_candidate_is_recommended("HP-METER") is True
    )  # noqa: SLF001
    assert runtime._heatpump_power_fetch_plan()[0] == [  # noqa: SLF001
        "HP-CTRL",
        "HP-METER",
        None,
    ]
    assert runtime._heatpump_latest_power_sample(  # noqa: SLF001
        {
            "heat_pump_consumption": [560.0, 0.0, 0.0],
            "start_date": "2026-02-27T00:00:00Z",
            "interval_minutes": 5,
        }
    ) == (0, 560.0)
    assert (
        runtime._infer_heatpump_interval_minutes(  # noqa: SLF001
            datetime(2026, 2, 27, 0, 0, tzinfo=timezone.utc),
            2,
            datetime(2026, 2, 27, 0, 10, tzinfo=timezone.utc),
        )
        == 5
    )


@pytest.mark.asyncio
async def test_heatpump_runtime_power_and_diagnostics_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.heatpump_runtime
    coord._devices_inventory_payload = {"curr_date_site": "2026-03-13"}  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets(
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 3,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-METER",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG",
                        "statusText": "Recommended",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-03-13T00:05:00Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-CTRL",
                        "device_name": "Primary",
                        "consumption": [{"details": [610.0]}],
                    }
                ]
            },
        }
    )
    await runtime._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    assert coord.heatpump_power_w == pytest.approx(610.0)
    assert coord.heatpump_power_device_uid == "HP-CTRL"

    runtime._heatpump_power_cache_until = None  # noqa: SLF001
    runtime._heatpump_power_selection_marker = (
        runtime._heatpump_power_inventory_marker()
    )  # noqa: SLF001
    runtime._heatpump_power_device_uid = "HP-CTRL"  # noqa: SLF001
    coord.client.hems_energy_consumption = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-03-13T00:10:00Z",
            "data": {
                "heat-pump": [
                    {
                        "device_uid": "HP-CTRL",
                        "device_name": "Primary",
                        "consumption": [{"details": [0.0]}],
                    }
                ]
            },
        }
    )
    await runtime._async_refresh_heatpump_power(force=True)  # noqa: SLF001
    assert coord.heatpump_power_w == pytest.approx(0.0)
    assert coord.heatpump_power_device_uid == "HP-CTRL"

    runtime._heatpump_runtime_diagnostics_cache_until = (
        time.monotonic() + 60
    )  # noqa: SLF001
    coord.client.show_livestream = AsyncMock(side_effect=AssertionError("no fetch"))
    await runtime.async_ensure_heatpump_runtime_diagnostics()
    coord.client.show_livestream.assert_not_awaited()

    runtime._heatpump_runtime_diagnostics_cache_until = None  # noqa: SLF001

    def _redact(payload):
        if payload == "SHOW_SCALAR":
            return "live-redacted"
        if payload == "EVENT_SCALAR":
            return "event-redacted"
        return None if payload == "EVENT_NONE" else payload

    monkeypatch.setattr(heatpump_runtime_mod, "redact_battery_payload", _redact)
    coord.client.show_livestream = AsyncMock(return_value="SHOW_SCALAR")
    coord.client.heat_pump_events_json = AsyncMock(
        side_effect=lambda uid: "EVENT_NONE" if uid == "HP-CTRL" else "EVENT_SCALAR"
    )
    coord.client.iq_er_events_json = AsyncMock(
        side_effect=api.OptionalEndpointUnavailable("optional boom")
    )
    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)

    diagnostics = coord.heatpump_runtime_diagnostics()
    assert diagnostics["show_livestream_payload"] == {"value": "live-redacted"}
    assert diagnostics["events_payloads"][0]["payload"] is None
    assert [entry.get("payload") for entry in diagnostics["events_payloads"]] == [
        None,
        None,
        None,
    ]
    assert [entry.get("error") for entry in diagnostics["events_payloads"]] == [
        None,
        "optional boom",
        "optional boom",
    ]
    assert diagnostics["event_summary"] == {
        "known_event_counts": {},
        "unknown_event_keys": [],
    }

    runtime._type_device_buckets = {}  # noqa: SLF001
    runtime._type_device_order = []  # noqa: SLF001
    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)
    assert coord.heatpump_runtime_diagnostics()["events_payloads"] == []
    assert coord.heatpump_runtime_diagnostics()["event_summary"] == {
        "known_event_counts": {},
        "unknown_event_keys": [],
    }
    assert coord.heatpump_daily_consumption_last_error is None

    coord.inventory_runtime._set_type_device_buckets(
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-CTRL",
                        "statusText": "Normal",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord.client._hems_site_supported = None  # noqa: SLF001
    coord.client.hems_energy_consumption = None
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001

    coord.client.hems_energy_consumption = AsyncMock(side_effect=RuntimeError("boom"))
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_daily_consumption_last_error == "boom"
    assert coord._heatpump_daily_consumption_backoff_until is not None  # noqa: SLF001

    coord._heatpump_daily_consumption_backoff_until = None  # noqa: SLF001
    coord.client.hems_energy_consumption = AsyncMock(return_value=None)
    await coord.heatpump_runtime._async_refresh_heatpump_daily_consumption(
        force=True
    )  # noqa: SLF001
    assert coord.heatpump_daily_consumption == {}
