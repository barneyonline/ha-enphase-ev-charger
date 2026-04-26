from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.enphase_ev.api import OptionalEndpointUnavailable
from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.sensor import (
    EnphaseTariffBillingSensor,
    EnphaseTariffExportRateValueSensor,
    EnphaseTariffRateSensor,
    EnphaseTariffRateValueSensor,
    async_setup_entry,
)
from custom_components.enphase_ev.tariff import (
    TARIFF_ENDPOINT_FAMILY,
    TariffRateSnapshot,
    TariffRuntime,
    _clean_text,
    _format_rate,
    export_rate_sensor_specs,
    next_billing_date,
    parse_tariff_billing,
    parse_tariff_rate,
    tariff_rate_sensor_specs,
)


def test_parse_tariff_billing_monthly_and_day_based() -> None:
    monthly = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2025-08-18",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    assert monthly is not None
    assert monthly.state == "Monthly"
    assert monthly.attributes == {
        "start_date": "2025-08-18",
        "billing_frequency": "MONTH",
        "billing_interval_value": 1,
        "billing_cycle": "Monthly",
    }
    assert next_billing_date(monthly, today=date(2026, 4, 26)) == date(2026, 5, 18)

    day_based = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2025-08-17",
            "billingFrequency": "DAY",
            "billingIntervalValue": 30,
        }
    )
    assert day_based is not None
    assert day_based.state == "Every 30 days"
    assert next_billing_date(day_based, today=date(2025, 9, 16)) == date(2025, 10, 16)

    two_month = parse_tariff_billing(
        {"billingFrequency": "MONTH", "billingIntervalValue": 2}
    )
    assert two_month is not None
    assert two_month.state == "Every 2 months"

    daily = parse_tariff_billing({"billingFrequency": "DAY", "billingIntervalValue": 1})
    assert daily is not None
    assert daily.state == "Daily"

    custom = parse_tariff_billing({"billingFrequency": "WEEK"})
    assert custom is not None
    assert custom.state == "WEEK"
    assert next_billing_date(custom, today=date(2026, 4, 26)) is None
    custom_with_start = parse_tariff_billing(
        {"anyBillPeriodStartDate": "2026-04-01", "billingFrequency": "WEEK"}
    )
    assert custom_with_start is not None
    assert next_billing_date(custom_with_start, today=date(2026, 4, 26)) is None


def test_next_billing_date_month_end_and_invalid_values() -> None:
    month_end = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2025-01-31",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    assert month_end is not None
    assert next_billing_date(month_end, today=date(2025, 2, 27)) == date(2025, 2, 28)
    assert next_billing_date(month_end, today=date(2025, 2, 28)) == date(2025, 3, 31)

    invalid_date = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "not-a-date",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    assert invalid_date is not None
    assert next_billing_date(invalid_date, today=date(2026, 4, 26)) is None

    invalid_interval = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "DAY",
            "billingIntervalValue": 0,
        }
    )
    assert invalid_interval is not None
    assert next_billing_date(invalid_interval, today=date(2026, 4, 26)) is None


def test_parse_tariff_rate_flat_tou_and_tiered_shapes() -> None:
    payload = {
        "currency": "$",
        "purchase": {
            "typeKind": "seasonal-and-weekends",
            "typeId": "tou",
            "source": "manual",
            "seasons": [
                {
                    "id": "summer",
                    "startMonth": "12",
                    "endMonth": "5",
                    "days": [
                        {
                            "id": "weekdays",
                            "days": [1, 2, 3, 4, 5],
                            "periods": [
                                {
                                    "id": "peak-1",
                                    "rate": "0.31",
                                    "type": "peak",
                                    "startTime": "840",
                                    "endTime": "1260",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "buyback": {
            "typeKind": "single",
            "typeId": "tiered",
            "source": "netFit",
            "exportPlan": "netFit",
            "seasons": [
                {
                    "id": "default",
                    "startMonth": "1",
                    "endMonth": "12",
                    "offPeak": "0.04",
                    "tiers": [
                        {
                            "id": "tier-1",
                            "rate": "0.06",
                            "startValue": "10",
                            "endValue": "20",
                        },
                        {
                            "id": "tier-2",
                            "rate": "0.10",
                            "startValue": "20",
                            "endValue": -1,
                        },
                    ],
                }
            ],
        },
    }

    purchase = parse_tariff_rate(payload, "purchase")
    assert purchase is not None
    assert purchase.state == "Time of use"
    assert purchase.variation_type == "Seasonal weekdays and weekends"
    assert purchase.seasons[0]["days"][0]["periods"][0]["start_time"] == "14:00"
    assert purchase.seasons[0]["days"][0]["periods"][0]["end_time"] == "21:00"

    buyback = parse_tariff_rate(payload, "buyback")
    assert buyback is not None
    assert buyback.state == "Tiered"
    assert buyback.export_plan == "netFit"
    assert buyback.attributes["export_plan"] == "netFit"
    assert buyback.seasons[0]["off_peak"] == "0.04"
    assert buyback.seasons[0]["tiers"][1]["unbounded"] is True


def test_tariff_rate_sensor_specs_for_tou_and_tiered_rates() -> None:
    import_tou = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "off-peak",
                                        "type": "off-peak",
                                        "rate": "0.18",
                                    },
                                    {"id": "peak-1", "type": "peak", "rate": "0.31"},
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )

    import_specs = tariff_rate_sensor_specs(import_tou)

    assert [spec["name"] for spec in import_specs] == ["Off-Peak", "Peak"]
    assert [spec["state"] for spec in import_specs] == [0.18, 0.31]
    assert [spec["unit"] for spec in import_specs] == ["$/kWh", "$/kWh"]
    assert import_specs[0]["attributes"]["formatted_rate"] == "$0.18"
    assert import_specs[0]["attributes"]["source"] == "manual"

    export_tou = parse_tariff_rate(
        {
            "currency": "$",
            "buyback": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "netFit",
                "exportPlan": "netFit",
                "seasons": [
                    {
                        "id": "default",
                        "startMonth": "1",
                        "endMonth": "12",
                        "days": [
                            {
                                "id": "week",
                                "days": [1, 2, 3, 4, 5, 6, 7],
                                "periods": [
                                    {
                                        "id": "off-peak",
                                        "type": "off-peak",
                                        "rate": "0.02",
                                    },
                                    {
                                        "id": "peak-1",
                                        "type": "peak",
                                        "rate": "0.06",
                                        "startTime": "960",
                                        "endTime": "1320",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "buyback",
    )

    specs = export_rate_sensor_specs(export_tou)

    assert [spec["name"] for spec in specs] == ["Off-Peak", "Peak"]
    assert [spec["state"] for spec in specs] == [0.02, 0.06]
    assert [spec["unit"] for spec in specs] == ["$/kWh", "$/kWh"]
    assert specs[0]["key"] == "default_week_off_peak"
    assert specs[1]["attributes"]["rate_structure"] == "Time of use"
    assert specs[1]["attributes"]["period_type"] == "peak"
    assert specs[1]["attributes"]["start_time"] == "16:00"
    assert specs[1]["attributes"]["end_time"] == "22:00"
    assert specs[1]["attributes"]["export_plan"] == "netFit"

    tiered = parse_tariff_rate(
        {
            "currency": "AUD",
            "buyback": {
                "typeKind": "single",
                "typeId": "tiered",
                "seasons": [
                    {
                        "id": "default",
                        "tiers": [
                            {"id": "tier-1", "rate": "0.04", "startValue": "0"},
                            {"id": "tier-2", "rate": "0.10", "endValue": -1},
                        ],
                    }
                ],
            },
        },
        "buyback",
    )

    tier_specs = export_rate_sensor_specs(tiered)

    assert [spec["state"] for spec in tier_specs] == [0.04, 0.10]
    assert [spec["unit"] for spec in tier_specs] == ["AUD/kWh", "AUD/kWh"]
    assert tier_specs[1]["attributes"]["unbounded"] is True


def test_export_rate_sensor_specs_handles_sparse_and_duplicate_rows() -> None:
    assert export_rate_sensor_specs(None) == ()
    sparse = parse_tariff_rate(
        {
            "buyback": {
                "typeId": "tou",
                "seasons": [
                    {
                        "days": [
                            "bad",
                            {
                                "periods": [
                                    "bad",
                                    {"id": "", "rate": None},
                                    {"id": "text", "rate": "not-numeric"},
                                    {"id": "", "rate": "0.01"},
                                    {"id": "", "rate": "0.02"},
                                ]
                            },
                        ],
                        "tiers": [
                            "bad",
                            {"id": "", "rate": None},
                            {"id": "", "rate": "0.03"},
                        ],
                    }
                ],
            }
        },
        "buyback",
    )

    specs = export_rate_sensor_specs(sparse)

    assert [spec["key"] for spec in specs] == [
        "season_1_days_1_period_3",
        "season_1_days_1_period_4",
        "season_1_tier_2",
    ]
    assert [spec["name"] for spec in specs] == ["Period 3", "Period 4", "Tier 2"]
    assert [spec["state"] for spec in specs] == [0.01, 0.02, 0.03]
    assert [spec["unit"] for spec in specs] == [None, None, None]

    duplicate_keys = parse_tariff_rate(
        {
            "buyback": {
                "typeId": "tou",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {"type": "peak", "rate": "0.01"},
                                    {"type": "peak", "rate": "0.02"},
                                    {"type": "free"},
                                ],
                            }
                        ],
                    }
                ],
            }
        },
        "buyback",
    )

    duplicate_specs = export_rate_sensor_specs(duplicate_keys)

    assert [spec["key"] for spec in duplicate_specs] == [
        "default_week_peak",
        "default_week_peak_2",
    ]

    assert (
        export_rate_sensor_specs(
            TariffRateSnapshot(
                state="Time of use",
                rate_structure="Time of use",
                variation_type=None,
                source=None,
                currency=None,
                export_plan=None,
                seasons=(
                    {
                        "days": ["bad", {"periods": ["bad", {"rate": "0.04"}]}],
                        "tiers": ["bad", {"id": "free"}],
                    },
                ),
            )
        )[0]["state"]
        == 0.04
    )


def test_parse_tariff_rate_rejects_empty_or_bad_branches() -> None:
    class BadString:
        def __str__(self) -> str:
            raise RuntimeError("bad")

    assert _clean_text(BadString()) is None
    assert _format_rate(None, "$") is None
    assert parse_tariff_rate({}, "purchase") is None
    assert parse_tariff_rate("bad", "purchase") is None
    assert parse_tariff_rate({"purchase": {"seasons": []}}, "purchase") is None
    assert parse_tariff_billing("bad") is None
    assert parse_tariff_billing({}) is None
    bad_interval = parse_tariff_billing(
        {"billingFrequency": "WEEK", "billingIntervalValue": "bad"}
    )
    assert bad_interval is not None
    assert bad_interval.billing_interval_value is None
    assert parse_tariff_rate(
        {
            "purchase": {
                "typeId": "custom",
                "typeKind": "custom-kind",
                "seasons": [
                    "bad",
                    {
                        "days": ["bad", {"periods": ["bad"]}],
                        "tiers": ["bad", {"endValue": "bad"}],
                    },
                ],
            }
        },
        "purchase",
    ).attributes["seasons"] == [{"days": [{}], "tiers": [{"end_value": "bad"}]}]


@pytest.mark.asyncio
async def test_tariff_runtime_refreshes_snapshots(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {
                "anyBillPeriodStartDate": "2025-08-18",
                "billingFrequency": "MONTH",
                "billingIntervalValue": 1,
            },
            {
                "currency": "$",
                "purchase": {
                    "typeKind": "single",
                    "typeId": "flat",
                    "source": "manual",
                    "seasons": [
                        {
                            "id": "default",
                            "startMonth": "1",
                            "endMonth": "12",
                            "days": [
                                {
                                    "id": "week",
                                    "days": [1, 2, 3, 4, 5, 6, 7],
                                    "periods": [{"rate": "0.03"}],
                                }
                            ],
                        }
                    ],
                },
                "buyback": {
                    "typeKind": "single",
                    "typeId": "flat",
                    "source": "grossFit",
                    "exportPlan": "grossFit",
                    "seasons": [
                        {
                            "id": "default",
                            "startMonth": "1",
                            "endMonth": "12",
                            "days": [
                                {
                                    "id": "week",
                                    "days": [1, 2, 3, 4, 5, 6, 7],
                                    "periods": [{"rate": "0.01"}],
                                }
                            ],
                        }
                    ],
                },
            },
        )
    )

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing.state == "Monthly"
    assert coord.tariff_import_rate.state == "Flat"
    assert coord.tariff_export_rate.export_plan == "grossFit"
    assert coord.tariff_last_refresh_utc is not None
    assert coord.tariff_rates_last_refresh_utc is coord.tariff_last_refresh_utc
    assert coord._endpoint_family_state("tariff").support_state == "supported"


@pytest.mark.asyncio
async def test_tariff_runtime_keeps_stale_data_on_failure(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.tariff_billing = parse_tariff_billing(
        {"billingFrequency": "MONTH", "billingIntervalValue": 1}
    )
    coord._note_endpoint_family_success("tariff")
    coord._endpoint_family_state("tariff").next_retry_mono = None
    err = aiohttp.ClientError("boom")
    coord.client.site_tariff_bundle = AsyncMock(side_effect=err)

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing.state == "Monthly"
    health = coord._endpoint_family_state("tariff")
    assert health.consecutive_failures == 1
    assert health.cooldown_active is True


@pytest.mark.asyncio
async def test_tariff_runtime_keeps_stale_data_on_empty_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.tariff_billing = parse_tariff_billing(
        {"billingFrequency": "MONTH", "billingIntervalValue": 1}
    )
    coord._note_endpoint_family_success("tariff")
    coord._endpoint_family_state("tariff").next_retry_mono = None
    coord.client.site_tariff_bundle = AsyncMock(return_value=({}, {}))

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing.state == "Monthly"
    health = coord._endpoint_family_state("tariff")
    assert health.consecutive_failures == 1
    assert health.last_error == "Tariff payload did not include data"


def test_tariff_runtime_refresh_due_uses_endpoint_family_gate(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._endpoint_family_should_run = MagicMock(return_value=True)  # noqa: SLF001

    assert TariffRuntime(coord).refresh_due() is True
    coord._endpoint_family_should_run.assert_called_once_with("tariff")  # noqa: SLF001


@pytest.mark.asyncio
async def test_tariff_runtime_respects_endpoint_gate(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._endpoint_family_should_run = MagicMock(return_value=False)  # noqa: SLF001
    coord.client.site_tariff_bundle = AsyncMock()

    await TariffRuntime(coord).async_refresh()

    coord.client.site_tariff_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_tariff_runtime_raises_without_stale_data(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(side_effect=aiohttp.ClientError("boom"))

    with pytest.raises(OptionalEndpointUnavailable):
        await TariffRuntime(coord).async_refresh()


@pytest.mark.asyncio
async def test_tariff_runtime_raises_when_client_method_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client = object()

    with pytest.raises(OptionalEndpointUnavailable):
        await TariffRuntime(coord).async_refresh()


@pytest.mark.asyncio
async def test_tariff_runtime_keeps_stale_data_on_client_attribute_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.tariff_billing = parse_tariff_billing(
        {"billingFrequency": "MONTH", "billingIntervalValue": 1}
    )
    coord._note_endpoint_family_success("tariff")
    coord._endpoint_family_state("tariff").next_retry_mono = None
    coord.client.site_tariff_bundle = AsyncMock(side_effect=AttributeError("boom"))

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing.state == "Monthly"
    health = coord._endpoint_family_state("tariff")
    assert health.consecutive_failures == 1
    assert health.cooldown_active is True


@pytest.mark.asyncio
async def test_tariff_runtime_treats_empty_payload_without_stale_data_as_unconfigured(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(return_value=({}, {}))

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing is None
    assert coord.tariff_import_rate is None
    assert coord.tariff_export_rate is None
    assert coord.tariff_last_refresh_utc is not None
    assert getattr(coord, "tariff_rates_last_refresh_utc", None) is None
    health = coord._endpoint_family_state("tariff")
    assert health.support_state == "supported"
    assert health.consecutive_failures == 0
    assert health.last_error is None


@pytest.mark.asyncio
async def test_tariff_runtime_marks_non_empty_no_rate_payload_authoritative(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_tariff_bundle = AsyncMock(
        return_value=(
            {
                "anyBillPeriodStartDate": "2026-04-01",
                "billingFrequency": "MONTH",
                "billingIntervalValue": 1,
            },
            {"currency": "$"},
        )
    )

    await TariffRuntime(coord).async_refresh()

    assert coord.tariff_billing.state == "Monthly"
    assert coord.tariff_import_rate is None
    assert coord.tariff_export_rate is None
    assert coord.tariff_rates_last_refresh_utc is coord.tariff_last_refresh_utc


def test_tariff_runtime_diagnostics(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.tariff_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    coord.tariff_rates_last_refresh_utc = datetime(2026, 4, 26, 1, tzinfo=timezone.utc)
    coord.tariff_billing = parse_tariff_billing(
        {"billingFrequency": "MONTH", "billingIntervalValue": 1}
    )
    health = coord._endpoint_family_state(TARIFF_ENDPOINT_FAMILY)
    health.support_state = "supported"
    health.consecutive_failures = 1
    health.last_failure_utc = datetime(2026, 4, 25, tzinfo=timezone.utc)
    health.last_error = "Tariff payload did not include data"

    diag = TariffRuntime(coord).diagnostics()

    assert diag["billing_available"] is True
    assert diag["import_rate_available"] is False
    assert diag["export_rate_available"] is False
    assert diag["last_refresh_utc"] == "2026-04-26T00:00:00+00:00"
    assert diag["rates_last_refresh_utc"] == "2026-04-26T01:00:00+00:00"
    assert diag["endpoint_family"]["support_state"] == "supported"
    assert diag["endpoint_family"]["consecutive_failures"] == 1
    assert diag["endpoint_family"]["last_failure_utc"] == "2026-04-25T00:00:00+00:00"
    assert (
        diag["endpoint_family"]["last_error"] == "Tariff payload did not include data"
    )


def test_tariff_sensors_expose_state_attributes_and_gateway_device(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.tariff_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    coord.tariff_billing = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2025-08-18",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "flat",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "startMonth": "1",
                        "endMonth": "12",
                        "days": [{"id": "week", "periods": [{"rate": "0.03"}]}],
                    }
                ],
            },
        },
        "purchase",
    )

    billing_sensor = EnphaseTariffBillingSensor(coord)
    import_sensor = EnphaseTariffRateSensor(coord, True)
    export_sensor = EnphaseTariffRateSensor(coord, False)

    assert billing_sensor.native_value == date(2026, 5, 18)
    assert billing_sensor.icon == "mdi:calendar-month"
    assert billing_sensor.device_class == "date"
    assert billing_sensor.extra_state_attributes["start_date"] == "2025-08-18"
    assert billing_sensor.extra_state_attributes["billing_cycle"] == "Monthly"
    assert import_sensor.native_value == "Flat"
    assert import_sensor.icon == "mdi:cash-minus"
    assert export_sensor.icon == "mdi:cash-plus"
    assert import_sensor.extra_state_attributes["seasons"][0]["id"] == "default"
    assert billing_sensor.device_info["identifiers"]


def test_export_rate_value_sensor_exposes_rate_state_and_attributes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.tariff_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    coord.tariff_export_rate = parse_tariff_rate(
        {
            "currency": "$",
            "buyback": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "netFit",
                "exportPlan": "netFit",
                "seasons": [
                    {
                        "id": "default",
                        "startMonth": "1",
                        "endMonth": "12",
                        "days": [
                            {
                                "id": "week",
                                "days": [1, 2, 3, 4, 5, 6, 7],
                                "periods": [
                                    {
                                        "id": "peak-1",
                                        "type": "peak",
                                        "rate": "0.06",
                                        "startTime": "960",
                                        "endTime": "1320",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "buyback",
    )
    spec = export_rate_sensor_specs(coord.tariff_export_rate)[0]

    sensor = EnphaseTariffExportRateValueSensor(coord, spec)

    assert sensor.native_value == 0.06
    assert sensor.native_unit_of_measurement == "$/kWh"
    assert sensor.state_class == "measurement"
    assert sensor.icon == "mdi:cash-plus"
    assert sensor.translation_key == "tariff_export_rate_value"
    assert sensor.translation_placeholders == {"detail": "Peak"}
    assert sensor.available is True
    assert sensor.extra_state_attributes["rate_structure"] == "Time of use"
    assert sensor.extra_state_attributes["period_type"] == "peak"
    assert (
        sensor.extra_state_attributes["last_refresh_utc"] == "2026-04-26T00:00:00+00:00"
    )

    coord.tariff_export_rate = None

    assert sensor.available is False
    assert sensor.native_value is None


def test_import_rate_value_sensor_exposes_rate_state_and_attributes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.tariff_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "off-peak",
                                        "type": "off-peak",
                                        "rate": "0.18",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    spec = tariff_rate_sensor_specs(coord.tariff_import_rate)[0]

    sensor = EnphaseTariffRateValueSensor(coord, spec, is_import=True)

    assert sensor.native_value == 0.18
    assert sensor.native_unit_of_measurement == "$/kWh"
    assert sensor.state_class == "measurement"
    assert sensor.suggested_display_precision == 4
    assert sensor.icon == "mdi:cash-minus"
    assert sensor.translation_key == "tariff_import_rate_value"
    assert sensor.translation_placeholders == {"detail": "Off-Peak"}
    assert sensor.available is True
    assert sensor.extra_state_attributes["rate_structure"] == "Time of use"
    assert sensor.extra_state_attributes["period_type"] == "off-peak"


def test_rate_value_sensor_uses_home_assistant_currency_for_unit(
    hass,
    coordinator_factory,
    monkeypatch,
) -> None:
    coord = coordinator_factory()
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"type": "peak", "rate": "0.31"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    spec = tariff_rate_sensor_specs(coord.tariff_import_rate)[0]
    sensor = EnphaseTariffRateValueSensor(coord, spec, is_import=True)
    sensor.hass = hass
    monkeypatch.setattr(hass.config, "currency", "AUD")

    assert sensor.native_unit_of_measurement == "AUD/kWh"
    assert sensor.extra_state_attributes["currency"] == "$"
    assert sensor.extra_state_attributes["formatted_rate"] == "$0.31"


def test_tariff_sensor_falls_back_to_cloud_device(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.inventory_view.type_device_info = MagicMock(return_value=None)
    coord.tariff_billing = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "DAY",
            "billingIntervalValue": 30,
        }
    )

    sensor = EnphaseTariffBillingSensor(coord)

    assert sensor.native_value == date(2026, 5, 1)
    assert ("enphase_ev", f"type:{coord.site_id}:cloud") in sensor.device_info[
        "identifiers"
    ]


@pytest.mark.asyncio
async def test_tariff_sensors_not_created_without_enphase_data(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    tariff_unique_ids = [
        entity.unique_id for entity in added if "tariff" in str(entity.unique_id)
    ]
    assert tariff_unique_ids == []


@pytest.mark.asyncio
async def test_tariff_sensor_created_when_previously_registered_without_data(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_tariff_billing_cycle",
        config_entry=config_entry,
    )
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    tariff_unique_ids = [
        entity.unique_id for entity in added if "tariff" in str(entity.unique_id)
    ]
    assert tariff_unique_ids == [f"{DOMAIN}_site_{coord.site_id}_tariff_billing_cycle"]


@pytest.mark.asyncio
async def test_tariff_dynamic_rate_entities_removed_when_branch_removed(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    coord.tariff_billing = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    coord.tariff_import_rate = TariffRateSnapshot(
        state="Flat",
        rate_structure="Flat",
        variation_type="Single",
        source="manual",
        currency="$",
        export_plan=None,
        seasons=(),
    )
    coord.tariff_rates_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    old_import_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_default_week_off_peak"
    )
    old_export_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_export_rate_default_week_peak"
    )
    for unique_id in (old_import_unique_id, old_export_unique_id):
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            unique_id,
            config_entry=config_entry,
        )
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_import_unique_id) is None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_export_unique_id) is None
    assert [
        entity.unique_id for entity in added if "tariff" in str(entity.unique_id)
    ] == [
        f"{DOMAIN}_site_{coord.site_id}_tariff_billing_cycle",
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate",
    ]


@pytest.mark.asyncio
async def test_tariff_dynamic_rate_entities_removed_when_rates_unconfigured(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    coord.tariff_billing = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    coord.tariff_rates_last_refresh_utc = datetime(2026, 4, 26, tzinfo=timezone.utc)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    old_import_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_default_week_off_peak"
    )
    old_export_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_export_rate_default_week_peak"
    )
    for unique_id in (old_import_unique_id, old_export_unique_id):
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            unique_id,
            config_entry=config_entry,
        )
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_import_unique_id) is None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_export_unique_id) is None
    assert [
        entity.unique_id for entity in added if "tariff" in str(entity.unique_id)
    ] == [f"{DOMAIN}_site_{coord.site_id}_tariff_billing_cycle"]


@pytest.mark.asyncio
async def test_tariff_dynamic_rate_entities_preserved_without_current_context(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    coord.tariff_billing = parse_tariff_billing(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "MONTH",
            "billingIntervalValue": 1,
        }
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    old_import_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_default_week_off_peak"
    )
    old_export_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_export_rate_default_week_peak"
    )
    for unique_id in (old_import_unique_id, old_export_unique_id):
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            unique_id,
            config_entry=config_entry,
        )
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    assert (
        ent_reg.async_get_entity_id("sensor", DOMAIN, old_import_unique_id) is not None
    )
    assert (
        ent_reg.async_get_entity_id("sensor", DOMAIN, old_export_unique_id) is not None
    )
    assert [
        entity.unique_id for entity in added if "tariff" in str(entity.unique_id)
    ] == [f"{DOMAIN}_site_{coord.site_id}_tariff_billing_cycle"]


@pytest.mark.asyncio
async def test_tariff_rate_sensor_entities_resync_when_structure_changes(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    listeners = []
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    monkeypatch.setattr(
        coord,
        "async_add_listener",
        lambda callback: listeners.append(callback) or (lambda: None),
        raising=False,
    )
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"type": "off-peak", "rate": "0.18"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    old_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_default_week_off_peak"
    )
    assert old_unique_id in {entity.unique_id for entity in added}
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        old_unique_id,
        config_entry=config_entry,
    )

    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"type": "peak", "rate": "0.31"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    for listener in listeners:
        listener()

    new_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_default_week_peak"
    )
    assert new_unique_id in {entity.unique_id for entity in added}
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, old_unique_id) is None
