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
    ]
    for locale in translations_dir.glob("*.json"):
        data = json.loads(locale.read_text(encoding="utf-8"))
        for path in paths:
            value = _at_path(data, path)
            assert value.strip(), f"{locale.name} missing value for {path}"
