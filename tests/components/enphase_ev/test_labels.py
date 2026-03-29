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
        labels.charge_mode_label("experimental-mode", hass=hass)
        == "Unknown option (experimental mode)"
    )


def test_render_label_and_friendly_status_edge_cases() -> None:
    assert labels._render_label("{missing}", value="x") == "{missing}"  # noqa: SLF001
    assert labels.friendly_status_text("_") is None
    assert labels.friendly_status_text("RUNNING") == "Running"


def test_unknown_label_paths_return_none_when_display_text_collapses() -> None:
    class FlakyText:
        def __init__(self, values: list[str]) -> None:
            self._values = values

        def __str__(self) -> str:
            return self._values.pop(0)

    assert labels.battery_profile_label(FlakyText(["mystery_mode", "_"])) is None
    assert labels.battery_grid_mode_label(FlakyText(["mystery-mode", "_"])) is None
    assert labels.charge_mode_label(FlakyText(["experimental", "_"])) is None
