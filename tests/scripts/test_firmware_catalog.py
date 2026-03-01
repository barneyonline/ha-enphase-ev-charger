from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

import pytest


@pytest.fixture(scope="module")
def firmware_catalog_module():
    script_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "firmware_catalog.py"
    )
    spec = importlib.util.spec_from_file_location("firmware_catalog", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["firmware_catalog"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def test_discover_apps_entrypoint(firmware_catalog_module, fixture_dir: Path) -> None:
    html = (fixture_dir / "enphase_root.html").read_text(encoding="utf-8")
    apps_path, product_type = firmware_catalog_module.discover_apps_entrypoint(html)
    assert apps_path == "/installers/resources/documentation/apps"
    assert product_type == "216"


def test_parse_facets_and_languages(firmware_catalog_module, fixture_dir: Path) -> None:
    apps_html = (fixture_dir / "enphase_apps_facets.html").read_text(encoding="utf-8")

    assert firmware_catalog_module.parse_product_type_from_apps_page(apps_html) == "216"

    product_facets = firmware_catalog_module.parse_facet_values(
        apps_html, "product_media_name"
    )
    assert product_facets["IQ Gateway software"] == 5002
    assert product_facets["IQ Microinverter software"] == 7738

    topic_facets = firmware_catalog_module.parse_facet_values(apps_html, "document")
    assert topic_facets["Release notes"] == 217

    search_locales = firmware_catalog_module.parse_language_options(
        apps_html, "search_api_language"
    )
    assert search_locales["en"] == "United States (EN)"
    assert search_locales["en-au"] == "Australia (EN)"

    alt_locales = firmware_catalog_module.parse_language_options(
        apps_html, "field_alternative_language"
    )
    assert alt_locales["ja-jp"] == "Japan (JP)"


def test_parse_release_cards_and_pagination(
    firmware_catalog_module, fixture_dir: Path, monkeypatch
) -> None:
    page0 = (fixture_dir / "enphase_release_page_0.html").read_text(encoding="utf-8")
    page1 = (fixture_dir / "enphase_release_page_1.html").read_text(encoding="utf-8")

    cards = firmware_catalog_module.parse_release_cards(page0)
    assert len(cards) == 2
    assert cards[0].version == "8.2.4401"
    assert cards[0].release_date == "2025-11-20"
    assert cards[0].media_id == "22469"
    assert cards[0].countries_text == "USA"

    next_url = firmware_catalog_module.find_next_page_url(
        "https://enphase.com/installers/resources/documentation/apps?product_type=216", page0
    )
    assert next_url is not None and "page=1" in next_url

    fixtures_by_page = {"0": page0, "1": page1}

    def _fake_fetch(url: str, *, timeout: int = 30) -> str:  # noqa: ARG001
        if "page=1" in url:
            return fixtures_by_page["1"]
        return fixtures_by_page["0"]

    monkeypatch.setattr(firmware_catalog_module, "fetch_text", _fake_fetch)

    crawled_cards, visited_pages = firmware_catalog_module.crawl_release_cards(
        apps_url="https://enphase.com/installers/resources/documentation/apps",
        product_type="216",
        topic_id=217,
        product_media_name_id=5002,
        timeout=5,
        max_pages=5,
    )
    assert len(crawled_cards) == 3
    assert len(visited_pages) == 2
    assert any("page=1" in url for url in visited_pages)


def test_country_applicability_parsing(firmware_catalog_module) -> None:
    alias_map = {
        "usa": "US",
        "unitedstates": "US",
        "puertorico": "PR",
        "canada": "CA",
        "ireland": "IE",
        "italy": "IT",
        "unitedkingdom": "GB",
    }

    explicit = firmware_catalog_module.parse_country_applicability(
        "United States, Puerto Rico, Canada", alias_map=alias_map
    )
    assert explicit.include == {"US", "PR", "CA"}
    assert explicit.ambiguous is False

    all_except = firmware_catalog_module.parse_country_applicability(
        "All countries except Ireland, Italy, and the United Kingdom",
        alias_map=alias_map,
    )
    assert all_except.all_countries is True
    assert all_except.exclude == {"IE", "IT", "GB"}
    assert all_except.ambiguous is False

    ambiguous = firmware_catalog_module.parse_country_applicability(
        "All European countries except Ireland", alias_map=alias_map
    )
    assert ambiguous.ambiguous is True


def test_pick_latest_release_and_urls(firmware_catalog_module) -> None:
    card_old = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.2.4300)",
        version="8.2.4300",
        release_date="2025-09-01",
        media_id="22000",
        langcode="en",
        summary="Old",
        countries_text="USA",
    )
    card_new = firmware_catalog_module.ReleaseCard(
        title="IQ Gateway software release notes (8.2.4401)",
        version="8.2.4401",
        release_date="2025-11-20",
        media_id="22469",
        langcode="und",
        summary="New",
        countries_text="USA",
    )

    latest = firmware_catalog_module.pick_latest_release([card_old, card_new])
    assert latest == card_new

    urls = firmware_catalog_module.build_release_urls_by_locale(
        locales=["en", "fr-fr", "en-au"],
        media_id="22469",
        langcode="und",
        docs_path="/installers/resources/documentation/apps",
    )
    assert urls["en"].startswith("https://enphase.com/installers/resources/documentation/apps?")
    assert urls["fr-fr"].startswith("https://enphase.com/fr-fr/installers/resources/documentation/apps?")
    assert "media_id=22469" in urls["en-au"]


def test_catalogs_equal_ignoring_generated_at(firmware_catalog_module) -> None:
    current = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {"envoy": {"latest_global": {"version": "8.2.4401"}}},
    }
    previous_same = {
        "schema_version": 1,
        "generated_at": "2026-02-28T00:00:00Z",
        "devices": {"envoy": {"latest_global": {"version": "8.2.4401"}}},
    }
    previous_changed = {
        "schema_version": 1,
        "generated_at": "2026-02-28T00:00:00Z",
        "devices": {"envoy": {"latest_global": {"version": "8.2.4300"}}},
    }

    assert (
        firmware_catalog_module.catalogs_equal_ignoring_generated_at(current, previous_same)
        is True
    )
    assert (
        firmware_catalog_module.catalogs_equal_ignoring_generated_at(current, previous_changed)
        is False
    )
    assert firmware_catalog_module.catalogs_equal_ignoring_generated_at(current, None) is False


def test_choose_generated_at_reuses_previous_when_catalog_unchanged(
    firmware_catalog_module,
) -> None:
    current = {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {"envoy": {"latest_global": {"version": "8.2.4401"}}},
    }
    previous = {
        "schema_version": 1,
        "generated_at": "2026-02-28T12:00:00Z",
        "devices": {"envoy": {"latest_global": {"version": "8.2.4401"}}},
    }

    reused = firmware_catalog_module.choose_generated_at(
        current_catalog=current,
        previous_catalog=previous,
        fallback_generated_at="2026-03-01T00:00:00Z",
    )
    assert reused == "2026-02-28T12:00:00Z"

    changed = firmware_catalog_module.choose_generated_at(
        current_catalog=current,
        previous_catalog={
            "schema_version": 1,
            "generated_at": "2026-02-28T12:00:00Z",
            "devices": {"envoy": {"latest_global": {"version": "8.2.4300"}}},
        },
        fallback_generated_at="2026-03-01T00:00:00Z",
    )
    assert changed == "2026-03-01T00:00:00Z"


def test_helper_edge_branches(firmware_catalog_module, tmp_path: Path, monkeypatch) -> None:
    assert firmware_catalog_module._now_utc_iso().endswith("Z")
    assert firmware_catalog_module._parse_date_to_iso("") is None
    assert firmware_catalog_module._parse_date_to_iso("not-a-date") is None
    assert firmware_catalog_module._version_sort_key(None) == ()
    assert firmware_catalog_module._version_sort_key("1..A") == (1, "a")
    assert firmware_catalog_module._version_sort_key(".1-") == (1,)
    assert firmware_catalog_module._normalize_locale(None) == "en"
    assert firmware_catalog_module._normalize_locale("   ") == "en"
    assert firmware_catalog_module._with_query(
        "https://example.com/path?x=1", {"y": "2"}
    ).startswith("https://example.com/path?")

    parser = firmware_catalog_module.ReleaseCardParser()
    assert parser._extract_country_text("") is None
    assert parser._extract_country_text("Countries:") is None
    assert (
        parser._extract_country_text("Countries : United States, Canada")
        == "United States, Canada"
    )
    assert (
        parser._extract_country_text(
            "Countries: United States. Platforms supported: IQ8"
        )
        == "United States"
    )
    assert parser._extract_country_text("No labels here") is None
    parser._flush_card()
    assert parser.cards == []

    parser._title_parts = ["IQ Gateway software release notes (1.0.0)"]
    parser._note_parts = ["x" * 600]
    parser._date_parts = ["November 20, 2025"]
    parser._flush_card()
    assert parser.cards
    assert parser.cards[0].summary.endswith("...")

    assert firmware_catalog_module.discover_apps_entrypoint("<html/>") == (
        "/installers/resources/documentation/apps",
        "216",
    )
    apps_path, product_type = firmware_catalog_module.discover_apps_entrypoint(
        """
        <a href="/installers/resources/documentation/apps?product_type=111" aria-label="Data sheets"></a>
        <a href="/installers/resources/documentation/apps?product_type=216" aria-label="Apps and software"></a>
        """
    )
    assert apps_path == "/installers/resources/documentation/apps"
    assert product_type == "216"
    assert (
        firmware_catalog_module.parse_product_type_from_apps_page(
            "<a href='?product_type=777'>x</a>"
        )
        == "777"
    )
    assert firmware_catalog_module.parse_product_type_from_apps_page("<html/>") is None
    assert firmware_catalog_module.parse_facet_values("<html/>", "document") == {}
    facet_with_blank = """
    <ul data-drupal-facet-alias="document">
      <li><a data-drupal-facet-item-value="217"><span class="facet-item__value"> </span></a></li>
      <li><a data-drupal-facet-item-value="218"><span class="facet-item__value">Release notes</span></a></li>
    </ul>
    """
    assert firmware_catalog_module.parse_facet_values(facet_with_blank, "document") == {
        "Release notes": 218
    }
    assert firmware_catalog_module.parse_language_options("<html/>", "search_api_language") == {}
    assert firmware_catalog_module.find_next_page_url("https://example.com", "<html/>") is None

    assert firmware_catalog_module._country_label_to_names("") == []
    assert firmware_catalog_module._country_label_to_names("Latin America (EN)") == []
    assert firmware_catalog_module._country_label_to_names("Germany and Austria (DE)") == [
        "Germany",
        "Austria",
    ]
    assert firmware_catalog_module._token_to_iso("   ") is None
    assert firmware_catalog_module._token_to_iso("au") == "AU"

    mapping = firmware_catalog_module.build_region_country_mapping(
        {
            "en": "United States (EN)",
            "de-de": "Germany and Austria (DE)",
            "en-lac": "Latin America (EN)",
            "fr-ft": "French Territories",
            "en-au": "Unknown Region",
        }
    )
    assert mapping["United States (EN)"]["type"] == "country_variant"
    assert mapping["Germany and Austria (DE)"]["type"] == "multi_country_region"
    assert mapping["Latin America (EN)"]["type"] == "aggregate_region"
    assert mapping["French Territories"]["type"] == "aggregate_region"
    assert mapping["Unknown Region"]["iso_codes"] == ["AU"]

    alias_map = firmware_catalog_module.build_country_alias_map(mapping)
    assert alias_map["germany"] == "DE"
    assert alias_map["austria"] == "AT"
    assert (
        firmware_catalog_module.build_country_alias_map(
            {"A and B": {"iso_codes": ["AA"], "locale": "aa-aa"}}
        )["a"]
        == "AA"
    )

    applicability_alias_map = {"unitedstates": "US", "canada": "CA"}
    assert firmware_catalog_module.parse_country_applicability(
        None, alias_map=applicability_alias_map
    ).ambiguous
    assert firmware_catalog_module.parse_country_applicability(
        "   ", alias_map=applicability_alias_map
    ).ambiguous
    assert firmware_catalog_module.parse_country_applicability(
        "United States...", alias_map=applicability_alias_map
    ).ambiguous
    assert firmware_catalog_module.parse_country_applicability(
        "Worldwide", alias_map=applicability_alias_map
    ).all_countries
    assert firmware_catalog_module.parse_country_applicability(
        "All countries except Atlantis", alias_map=applicability_alias_map
    ).ambiguous
    assert firmware_catalog_module.parse_country_applicability(
        "United States, Atlantis", alias_map=applicability_alias_map
    ).ambiguous
    assert firmware_catalog_module.parse_country_applicability(
        "Atlantis", alias_map=applicability_alias_map
    ).ambiguous

    no_date = firmware_catalog_module.ReleaseCard(
        title="A",
        version=None,
        release_date=None,
        media_id=None,
        langcode="en",
        summary="a",
        countries_text=None,
    )
    assert firmware_catalog_module.pick_latest_release([]) is None
    assert firmware_catalog_module.pick_latest_release([no_date]) == no_date

    entry = firmware_catalog_module.card_to_runtime_entry(
        card=no_date,
        topic_id=217,
        locales=["en"],
        docs_path="/installers/resources/documentation/apps",
    )
    assert entry["urls_by_locale"] == {}

    output_json = tmp_path / "x" / "y.json"
    firmware_catalog_module.write_json(output_json, {"ok": True})
    assert json.loads(output_json.read_text(encoding="utf-8")) == {"ok": True}

    monkeypatch.setattr(firmware_catalog_module, "fetch_text", lambda _url, timeout=30: '{"a":1}')
    assert firmware_catalog_module.fetch_json("https://example.com") == {"a": 1}

    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_json",
        lambda _url, timeout=30: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert firmware_catalog_module.fetch_previous_runtime_catalog() is None
    monkeypatch.setattr(firmware_catalog_module, "fetch_json", lambda _url, timeout=30: [1, 2, 3])
    assert firmware_catalog_module.fetch_previous_runtime_catalog() is None
    monkeypatch.setattr(
        firmware_catalog_module, "fetch_json", lambda _url, timeout=30: {"schema_version": 1}
    )
    assert firmware_catalog_module.fetch_previous_runtime_catalog() == {"schema_version": 1}

    assert (
        firmware_catalog_module.choose_generated_at(
            current_catalog={"schema_version": 1, "generated_at": "new", "devices": {}},
            previous_catalog={"schema_version": 1, "generated_at": "   ", "devices": {}},
            fallback_generated_at="fallback",
        )
        == "fallback"
    )


def test_fetch_text_handles_charset_and_defaults(firmware_catalog_module, monkeypatch) -> None:
    class _Resp:
        def __init__(self, content_type: str, body: bytes):
            self.headers = {"content-type": content_type}
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self._body

    responses = iter(
        [
            _Resp("text/html; charset=iso-8859-1", b"caf\xe9"),
            _Resp("text/html", b"hello"),
        ]
    )
    monkeypatch.setattr(firmware_catalog_module, "urlopen", lambda _request, timeout=30: next(responses))

    assert firmware_catalog_module.fetch_text("https://example.com") == "caf\xe9"
    assert firmware_catalog_module.fetch_text("https://example.com") == "hello"


def test_build_catalog_success_and_error_paths(
    firmware_catalog_module, fixture_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    root_html = (fixture_dir / "enphase_root.html").read_text(encoding="utf-8")
    apps_html = (fixture_dir / "enphase_apps_facets.html").read_text(encoding="utf-8")

    def _fake_fetch_text(url: str, *, timeout: int = 30) -> str:  # noqa: ARG001
        if url.endswith("/installers/resources/documentation"):
            return root_html
        return apps_html

    gateway_cards = [
        firmware_catalog_module.ReleaseCard(
            title="IQ Gateway software release notes (8.2.4401)",
            version="8.2.4401",
            release_date="2025-11-20",
            media_id="22469",
            langcode="und",
            summary="Countries: United States, Puerto Rico",
            countries_text="United States, Puerto Rico",
        ),
        firmware_catalog_module.ReleaseCard(
            title="IQ Gateway software release notes (8.2.4500)",
            version="8.2.4500",
            release_date="2025-12-01",
            media_id="22999",
            langcode="en",
            summary="Countries: United States...",
            countries_text="United States...",
        ),
        firmware_catalog_module.ReleaseCard(
            title="IQ Gateway software release notes (8.2.4300)",
            version="8.2.4300",
            release_date="2025-10-20",
            media_id="22000",
            langcode="en",
            summary="Countries: All countries except Ireland",
            countries_text="All countries except Ireland",
        ),
    ]

    def _fake_crawl_release_cards(**kwargs):
        if kwargs["product_media_name_id"] == 5002:
            return gateway_cards, ["https://example.com/p0", "https://example.com/p1"]
        return [], ["https://example.com/p0"]

    monkeypatch.setattr(firmware_catalog_module, "_now_utc_iso", lambda: "2026-03-01T00:00:00Z")
    monkeypatch.setattr(firmware_catalog_module, "fetch_text", _fake_fetch_text)
    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_language_options",
        lambda _apps_html, select_name: {
            "en": "United States (EN)",
            "en-ie": "Ireland (EN)",
            "en-au": "Australia (EN)",
        }
        if select_name == "search_api_language"
        else {"ja-jp": "Japan (JP)"},
    )
    monkeypatch.setattr(firmware_catalog_module, "crawl_release_cards", _fake_crawl_release_cards)
    monkeypatch.setattr(
        firmware_catalog_module,
        "fetch_previous_runtime_catalog",
        lambda timeout=30: {
            "schema_version": 1,
            "generated_at": "2026-02-28T00:00:00Z",
            "source": {
                "type": "enphase_documentation_center",
                "entrypoint": "https://enphase.com/installers/resources/documentation",
                "apps_url": "https://enphase.com/installers/resources/documentation/apps",
                "product_type": 216,
                "crawl": {
                    "envoy": {
                        "pages": ["https://example.com/p0", "https://example.com/p1"],
                        "count": 2,
                    },
                    "microinverter": {"pages": ["https://example.com/p0"], "count": 0},
                },
            },
            "devices": {
                "envoy": {
                    "product_media_name_id": 5002,
                    "document_topic_id": 217,
                    "latest_by_country": {},
                    "latest_global": None,
                },
                "microinverter": {
                    "product_media_name_id": 7738,
                    "document_topic_id": 217,
                    "latest_by_country": {},
                    "latest_global": None,
                },
            },
        },
    )

    firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=2)
    runtime_path = tmp_path / "catalog" / "v1" / "runtime_catalog.json"
    assert runtime_path.exists()
    runtime_payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert runtime_payload["schema_version"] == 1
    assert runtime_payload["devices"]["envoy"]["product_media_name_id"] == 5002
    assert runtime_payload["devices"]["envoy"]["latest_by_country"]["PR"]["version"] == "8.2.4401"
    assert runtime_payload["devices"]["microinverter"]["latest_global"] is None
    assert (tmp_path / "sources" / "enphase_doc_center" / "entrypoints.json").exists()
    assert (tmp_path / "data" / "PR" / "catalog.json").exists()

    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda _apps_html, alias: {}
        if alias == "document"
        else {"IQ Gateway software": 5002, "IQ Microinverter software": 7738},
    )
    with pytest.raises(RuntimeError, match="release-notes topic id"):
        firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)

    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_facet_values",
        lambda _apps_html, alias: {"Release notes": 217}
        if alias == "document"
        else {"IQ Gateway software": 5002},
    )
    with pytest.raises(RuntimeError, match="IQ Microinverter software"):
        firmware_catalog_module.build_catalog(tmp_path, timeout=5, max_pages=1)


def test_parse_args_and_main_paths(firmware_catalog_module, monkeypatch, tmp_path: Path, capsys) -> None:
    default_args = firmware_catalog_module.parse_args([])
    assert default_args.output_dir == "."
    assert default_args.timeout == firmware_catalog_module.DEFAULT_TIMEOUT
    assert default_args.max_pages == firmware_catalog_module.DEFAULT_MAX_PAGES

    custom_args = firmware_catalog_module.parse_args(
        ["--output-dir", str(tmp_path), "--timeout", "11", "--max-pages", "5"]
    )
    assert custom_args.output_dir == str(tmp_path)
    assert custom_args.timeout == 11
    assert custom_args.max_pages == 5

    monkeypatch.setattr(
        firmware_catalog_module,
        "parse_args",
        lambda _argv: argparse.Namespace(output_dir=str(tmp_path), timeout=7, max_pages=3),
    )
    monkeypatch.setattr(firmware_catalog_module, "build_catalog", lambda *_args, **_kwargs: None)
    assert firmware_catalog_module.main(["--dummy"]) == 0
    assert "Firmware catalog generated at" in capsys.readouterr().out

    monkeypatch.setattr(
        firmware_catalog_module,
        "build_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert firmware_catalog_module.main(None) == 1
    assert "Failed to build firmware catalog: boom" in capsys.readouterr().err
