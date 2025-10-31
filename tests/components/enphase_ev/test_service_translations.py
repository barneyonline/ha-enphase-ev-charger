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
