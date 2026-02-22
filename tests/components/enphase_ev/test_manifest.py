import json
import pathlib


def test_manifest_keys_present():
    manifest_path = pathlib.Path(__file__).resolve().parents[3] / "custom_components" / "enphase_ev" / "manifest.json"
    raw = manifest_path.read_text()
    data = json.loads(raw)

    assert data.get("version"), "manifest must include version"
    assert data.get("config_flow") is True, "config_flow must be true"
    assert data.get("integration_type") == "device", "integration_type should be 'device'"
    assert data.get("iot_class") == "cloud_polling", "iot_class should be 'cloud_polling'"


def test_branding_name_is_aligned_across_manifest_hacs_and_strings():
    root = pathlib.Path(__file__).resolve().parents[3]
    expected_name = "Enphase Energy"

    manifest = json.loads(
        (root / "custom_components" / "enphase_ev" / "manifest.json").read_text()
    )
    hacs = json.loads((root / "hacs.json").read_text())
    strings = json.loads(
        (root / "custom_components" / "enphase_ev" / "strings.json").read_text()
    )

    assert manifest.get("name") == expected_name
    assert hacs.get("name") == expected_name
    assert strings["config"]["step"]["user"]["title"] == expected_name
