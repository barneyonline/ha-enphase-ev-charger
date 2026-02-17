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
