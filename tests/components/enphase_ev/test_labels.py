from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.enphase_ev import labels


@pytest.mark.asyncio
async def test_async_prime_label_translations_uses_hass_language(monkeypatch) -> None:
    calls: list[tuple[object, str, str, list[str]]] = []

    async def _fake_get_translations(hass, language, category, integrations):
        calls.append((hass, language, category, list(integrations)))
        return {}

    monkeypatch.setattr(labels, "async_get_translations", _fake_get_translations)
    hass = SimpleNamespace(config=SimpleNamespace(language="fr"))

    await labels.async_prime_label_translations(hass)

    assert calls == [(hass, "fr", "entity", ["enphase_ev"])]


def test_label_helpers_cover_translation_fallbacks_and_unknowns(monkeypatch) -> None:
    fr_cache = {
        "component.enphase_ev.entity.sensor.shared_labels.state.self_consumption": "Autoconsommation",
    }
    en_cache = {
        "component.enphase_ev.entity.sensor.shared_labels.state.importonly": "Import Only",
        "component.enphase_ev.entity.sensor.shared_labels.state.not_reporting": "Not Reporting",
    }

    def _fake_get_cached_translations(_hass, language, _category, _integration):
        if language == "fr":
            return fr_cache
        if language == "en":
            return en_cache
        return {}

    monkeypatch.setattr(
        labels, "async_get_cached_translations", _fake_get_cached_translations
    )
    hass = SimpleNamespace(config=SimpleNamespace(language="fr"))

    assert (
        labels.battery_profile_label("self-consumption", hass=hass)
        == "Autoconsommation"
    )
    assert labels.battery_grid_mode_label("ImportOnly", hass=hass) == "Import Only"
    assert labels.status_label("not_reporting", hass=hass) == "Not Reporting"

    assert labels.battery_profile_label("_", hass=hass) is None
    assert labels.battery_grid_mode_label("_", hass=hass) is None
    assert labels.charge_mode_label(None, hass=hass) is None
    assert (
        labels.charge_mode_label("experimental-mode", hass=hass) == "Experimental Mode"
    )


def test_render_label_and_friendly_status_edge_cases() -> None:
    assert labels._render_label("{missing}", value="x") == "{missing}"  # noqa: SLF001
    assert labels._display_raw_value(None) is None  # noqa: SLF001
    assert labels._friendly_label_text(None) is None  # noqa: SLF001
    assert (
        labels._friendly_label_text("REGIONAL_SPECIAL") == "Regional Special"
    )  # noqa: SLF001
    assert (
        labels._friendly_label_text("regional_special") == "Regional Special"
    )  # noqa: SLF001
    assert (
        labels._friendly_label_text("RegionalSpecial") == "RegionalSpecial"
    )  # noqa: SLF001
    assert labels.friendly_status_text("_") is None
    assert labels.friendly_status_text("RUNNING") == "Running"
    assert labels.friendly_status_text("RegionalSpecial") == "RegionalSpecial"


def test_unknown_label_paths_return_none_when_display_text_collapses() -> None:
    assert labels.battery_profile_label("_") is None
    assert labels.battery_grid_mode_label("_") is None
    assert labels.charge_mode_label("_") is None
    assert labels.status_label(None) is None
    assert labels.status_label("mystery_status") is None


def test_entity_translation_value_and_schedule_type_label_fallbacks(
    monkeypatch,
) -> None:
    def _fake_get_cached_translations(_hass, language, _category, _integration):
        if language == "en":
            return {
                "component.enphase_ev.entity.switch.charge_from_grid_schedule.name": "Charge Battery From Grid",
            }
        return {}

    monkeypatch.setattr(
        labels, "async_get_cached_translations", _fake_get_cached_translations
    )
    hass = SimpleNamespace(config=SimpleNamespace(language="fr"))

    assert (
        labels._entity_translation_value(None, "switch", "charge_from_grid") is None
    )  # noqa: SLF001
    assert (
        labels._entity_translation_value(hass, "switch", "charge_from_grid_schedule")
        == "Charge Battery From Grid"
    )
    assert labels._entity_translation_value(hass, "switch", "missing_key") is None
    assert labels.battery_schedule_type_label(None, hass=hass) is None
    assert (
        labels.battery_schedule_type_label("cfg", hass=hass)
        == "Charge Battery From Grid"
    )
    assert labels.battery_schedule_type_label("custom_mode", hass=hass) == "Custom Mode"


def test_battery_schedule_button_label_fallbacks(monkeypatch) -> None:
    monkeypatch.setattr(
        labels,
        "async_get_cached_translations",
        lambda *_args, **_kwargs: {},
    )
    hass = SimpleNamespace(config=SimpleNamespace(language="fr"))

    assert (
        labels.battery_schedule_button_label("save", hass=hass)
        == "Save Battery Schedule"
    )
    assert (
        labels.battery_schedule_button_label("custom_action", hass=hass)
        == "Custom Action"
    )
