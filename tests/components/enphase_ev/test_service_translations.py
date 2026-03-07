from __future__ import annotations

import json
import pathlib


def test_clear_reauth_issue_device_field_translated() -> None:
    """Ensure device selector metadata is translated for all locales."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    for lang in ("en", "fr"):
        data = json.loads((translations_dir / f"{lang}.json").read_text())
        fields = data["services"]["clear_reauth_issue"]["fields"]
        assert "device_id" in fields, f"{lang} missing device_id translation"
        entry = fields["device_id"]
        assert entry.get("name"), f"{lang} device_id name empty"
        assert entry.get("description"), f"{lang} device_id description empty"


def _at_path(data: dict, path: str) -> str:
    cur = data
    for part in path.split("."):
        cur = cur[part]
    assert isinstance(cur, str)
    return cur


def test_battery_profile_strings_localized_for_non_english_locales() -> None:
    """Guard against English fallback regressions for battery profile features."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.select.system_profile.name",
        "entity.number.battery_reserve.name",
        "entity.switch.savings_use_battery_after_peak.name",
        "entity.sensor.system_profile_status.name",
        "entity.sensor.system_profile_status.state.pending",
        "entity.button.cancel_pending_profile_change.name",
        "entity.button.storm_alert_opt_out.name",
        "issues.battery_profile_pending.title",
        "issues.battery_profile_pending.description",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        if name == "en.json" or name.startswith("en-"):
            continue
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            assert value != _at_path(en_data, path), (
                f"{name} should localize {path} (still matches English)"
            )
        desc = _at_path(data, "issues.battery_profile_pending.description")
        assert "{site_id}" in desc, f"{name} missing {{site_id}} placeholder"
        assert "{pending_timeout_minutes}" in desc, (
            f"{name} missing {{pending_timeout_minutes}} placeholder"
        )


def test_battery_settings_entity_strings_exist_for_all_locales() -> None:
    """Ensure newly added battery settings entity labels exist in every locale."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    paths = [
        "entity.sensor.battery_mode.name",
        "entity.sensor.grid_control_status.name",
        "entity.sensor.grid_control_status.state.ready",
        "entity.sensor.grid_control_status.state.blocked",
        "entity.sensor.grid_control_status.state.pending",
        "entity.sensor.battery_storage_charge.name",
        "entity.sensor.battery_overall_charge.name",
        "entity.sensor.battery_overall_status.name",
        "entity.sensor.battery_overall_status.state.normal",
        "entity.sensor.battery_overall_status.state.warning",
        "entity.sensor.battery_overall_status.state.error",
        "entity.sensor.battery_overall_status.state.unknown",
        "entity.number.battery_shutdown_level.name",
        "entity.switch.charge_from_grid.name",
        "entity.switch.charge_from_grid_schedule.name",
        "entity.time.charge_from_grid_start_time.name",
        "entity.time.charge_from_grid_end_time.name",
        "entity.calendar.backup_history.name",
    ]
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{locale.name} missing value for {path}"


def test_battery_inventory_strings_localized_for_non_english_locales() -> None:
    """Guard battery inventory labels from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.sensor.battery_available_energy.name",
        "entity.sensor.battery_available_power.name",
        "entity.sensor.battery_inactive_microinverters.name",
        "entity.sensor.battery_storage_status.name",
        "entity.sensor.battery_storage_health.name",
        "entity.sensor.battery_storage_cycle_count.name",
        "entity.sensor.battery_storage_last_reported.name",
        "entity.sensor.battery_last_reported.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(en_data, path), (
                    f"{name} should localize {path} (still matches English)"
                )

        if name != "en.json":
            for path in (
                "entity.sensor.battery_storage_status.name",
                "entity.sensor.battery_storage_health.name",
                "entity.sensor.battery_storage_cycle_count.name",
                "entity.sensor.battery_storage_last_reported.name",
            ):
                assert "{serial}" in _at_path(data, path), (
                    f"{name} missing {{serial}} placeholder in {path}"
                )


def test_microinverter_inventory_strings_localized_for_non_english_locales() -> None:
    """Guard microinverter inventory labels from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    assert (
        _at_path(en_data, "entity.sensor.microinverter_reporting_count.name")
        == "Active Microinverters"
    )
    paths = [
        "entity.sensor.microinverter_connectivity_status.name",
        "entity.sensor.microinverter_reporting_count.name",
        "entity.sensor.microinverter_last_reported.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(en_data, path), (
                    f"{name} should localize {path} (still matches English)"
                )


def test_heatpump_inventory_strings_localized_for_non_english_locales() -> None:
    """Guard heat pump labels from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    assert _at_path(en_data, "entity.sensor.heat_pump_status.name") == "Heat Pump Status"
    paths = [
        "entity.sensor.heat_pump_status.name",
        "entity.sensor.heat_pump_sg_ready_gateway.name",
        "entity.sensor.heat_pump_energy_meter.name",
        "entity.sensor.heat_pump_last_reported.name",
        "entity.sensor.heat_pump_power.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(en_data, path), (
                    f"{name} should localize {path} (still matches English)"
                )


def test_heatpump_binary_sensor_strings_localized_for_non_english_locales() -> None:
    """Guard heat pump binary-sensor labels from silently falling back to English."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    path = "entity.binary_sensor.heat_pump_sg_ready_active.name"
    assert _at_path(en_data, path) == "Heat Pump SG-Ready Active"
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        value = _at_path(data, path)
        assert value.strip(), f"{name} missing value for {path}"
        if name != "en.json" and not name.startswith("en-"):
            assert value != _at_path(en_data, path), (
                f"{name} should localize {path} (still matches English)"
            )


def test_french_heatpump_inventory_strings_are_specific() -> None:
    """Ensure French heat pump labels are not mixed with battery/site-consumption labels."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    fr_data = json.loads((translations_dir / "fr.json").read_text(encoding="utf-8"))
    expected = {
        "entity.sensor.heat_pump_status.name": "État de la pompe à chaleur",
        "entity.sensor.heat_pump_sg_ready_gateway.name": (
            "Passerelle SG-Ready de la pompe à chaleur"
        ),
        "entity.sensor.heat_pump_energy_meter.name": (
            "Compteur d'énergie de la pompe à chaleur"
        ),
        "entity.sensor.heat_pump_last_reported.name": (
            "Dernier signalement de la pompe à chaleur"
        ),
        "entity.sensor.heat_pump_power.name": "Puissance de la pompe à chaleur",
        "entity.binary_sensor.heat_pump_sg_ready_active.name": (
            "SG-Ready actif de la pompe à chaleur"
        ),
    }
    for path, value in expected.items():
        assert _at_path(fr_data, path) == value


def test_heatpump_inventory_strings_are_not_site_consumption_concatenations() -> None:
    """Guard against reusing site-consumption labels for heat-pump inventory sensors."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        prefix = _at_path(data, "entity.sensor.site_heat_pump_consumption.name")
        assert _at_path(data, "entity.sensor.heat_pump_status.name") != (
            f"{prefix} {_at_path(data, 'entity.sensor.battery_overall_status.name')}"
        ), f"{locale.name} reintroduced concatenated heat pump status label"
        assert _at_path(data, "entity.sensor.heat_pump_sg_ready_gateway.name") != (
            f"{prefix} SG-Ready Gateway"
        ), f"{locale.name} reintroduced concatenated SG-Ready label"
        assert _at_path(data, "entity.sensor.heat_pump_energy_meter.name") != (
            f"{prefix} {_at_path(data, 'entity.sensor.gateway_consumption_meter.name')}"
        ), f"{locale.name} reintroduced concatenated energy meter label"
        assert _at_path(data, "entity.sensor.heat_pump_last_reported.name") != (
            f"{prefix} {_at_path(data, 'entity.sensor.microinverter_last_reported.name')}"
        ), f"{locale.name} reintroduced concatenated last reported label"
        assert _at_path(data, "entity.sensor.heat_pump_power.name") != (
            f"{prefix} {_at_path(data, 'entity.sensor.battery_available_power.name')}"
        ), f"{locale.name} reintroduced concatenated power label"


def test_site_device_lifetime_strings_localized_for_non_english_locales() -> None:
    """Ensure new site device-lifetime sensor labels are translated."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.sensor.site_evse_charging.name",
        "entity.sensor.site_heat_pump_consumption.name",
        "entity.sensor.site_water_heater_consumption.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(en_data, path), (
                    f"{name} should localize {path} (still matches English)"
                )


def test_gateway_status_string_localized_for_non_english_locales() -> None:
    """Ensure gateway status label remains localized for non-English locales."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    path = "entity.sensor.gateway_connectivity_status.name"
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        value = _at_path(data, path)
        assert value.strip(), f"{name} missing value for {path}"
        if name != "en.json" and not name.startswith("en-"):
            assert value != _at_path(en_data, path), (
                f"{name} should localize {path} (still matches English)"
            )


def test_update_entity_strings_localized_for_non_english_locales() -> None:
    """Ensure firmware update entity labels are translated for non-English locales."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.update.gateway_firmware.name",
        "entity.update.microinverter_firmware.name",
    ]
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if name != "en.json" and not name.startswith("en-"):
                assert value != _at_path(en_data, path), (
                    f"{name} should localize {path} (still matches English)"
                )


def test_gateway_iq_energy_router_string_localized_for_non_english_locales() -> None:
    """Ensure IQ Energy Router label remains localized for non-English locales."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    path = "entity.sensor.gateway_iq_energy_router.name"
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        value = _at_path(data, path)
        assert value.strip(), f"{name} missing value for {path}"
        assert "{index}" in value, f"{name} missing {{index}} placeholder for {path}"
        if name != "en.json" and not name.startswith("en-"):
            assert value != _at_path(en_data, path), (
                f"{name} should localize {path} (still matches English)"
            )


def test_ev_charger_status_and_storm_guard_labels_localized() -> None:
    """Ensure EV charger status and storm guard labels stay localized."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    paths = [
        "entity.sensor.status.name",
        "entity.sensor.storm_guard_state.name",
    ]
    non_english_must_differ = {"entity.sensor.status.name"}

    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
            if (
                path in non_english_must_differ
                and name != "en.json"
                and not name.startswith("en-")
            ):
                assert value != _at_path(en_data, path), (
                    f"{name} should localize {path} (still matches English)"
                )


def test_grid_control_strings_exist_for_all_locales() -> None:
    """Ensure OTP/grid-control strings exist across services, entities, and errors."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    paths = [
        "entity.button.request_grid_toggle_otp.name",
        "entity.sensor.grid_mode.name",
        "entity.sensor.grid_mode.state.on_grid",
        "entity.sensor.grid_mode.state.off_grid",
        "entity.sensor.grid_mode.state.unknown",
        "services.request_grid_toggle_otp.name",
        "services.request_grid_toggle_otp.description",
        "services.set_grid_mode.name",
        "services.set_grid_mode.description",
        "services.set_grid_mode.fields.mode.name",
        "services.set_grid_mode.fields.mode.description",
        "services.set_grid_mode.fields.otp.name",
        "services.set_grid_mode.fields.otp.description",
        "exceptions.grid_control_unavailable.message",
        "exceptions.grid_control_blocked.message",
        "exceptions.grid_mode_invalid.message",
        "exceptions.grid_otp_required.message",
        "exceptions.grid_otp_invalid_format.message",
        "exceptions.grid_otp_invalid.message",
        "exceptions.grid_envoy_serial_missing.message",
        "exceptions.grid_site_required.message",
        "exceptions.grid_site_ambiguous.message",
    ]
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{locale.name} missing value for {path}"

        blocked = _at_path(data, "exceptions.grid_control_blocked.message")
        ambiguous = _at_path(data, "exceptions.grid_site_ambiguous.message")
        assert "{reasons}" in blocked, (
            f"{locale.name} missing {{reasons}} in grid_control_blocked message"
        )
        assert "{count}" in ambiguous, (
            f"{locale.name} missing {{count}} in grid_site_ambiguous message"
        )


def test_options_device_category_strings_exist_for_all_locales() -> None:
    """Ensure options flow strings stay in sync for category-based controls."""

    translations_dir = (
        pathlib.Path(__file__).resolve().parents[3]
        / "custom_components"
        / "enphase_ev"
        / "translations"
    )
    paths = [
        "config.step.devices.data.type_heatpump",
        "config.step.devices.data_description.type_heatpump",
        "options.step.init.data.type_envoy",
        "options.step.init.data.type_encharge",
        "options.step.init.data.type_iqevse",
        "options.step.init.data.type_heatpump",
        "options.step.init.data.type_microinverter",
        "options.step.init.data.api_timeout",
        "options.step.init.data.nominal_voltage",
        "options.step.init.data_description.type_envoy",
        "options.step.init.data_description.type_encharge",
        "options.step.init.data_description.type_iqevse",
        "options.step.init.data_description.type_heatpump",
        "options.step.init.data_description.type_microinverter",
        "options.step.init.data_description.api_timeout",
        "options.step.init.data_description.nominal_voltage",
        "options.error.serials_required",
    ]
    non_english_must_differ = [
        "config.step.devices.data.type_heatpump",
        "config.step.devices.data_description.type_heatpump",
        "options.step.init.data.type_heatpump",
        "options.step.init.data_description.type_heatpump",
        "options.step.init.data.api_timeout",
        "options.step.init.data.nominal_voltage",
        "options.step.init.data_description.api_timeout",
        "options.step.init.data_description.nominal_voltage",
    ]
    en_data = json.loads((translations_dir / "en.json").read_text(encoding="utf-8"))
    for locale in translations_dir.glob("*.json"):
        name = locale.name
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{name} missing value for {path}"
        if name != "en.json" and not name.startswith("en-"):
            for path in non_english_must_differ:
                assert _at_path(data, path) != _at_path(en_data, path), (
                    f"{name} should localize {path} (still matches English)"
                )
