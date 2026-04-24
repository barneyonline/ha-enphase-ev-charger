from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


def _load_module():
    root = Path(__file__).resolve().parents[2]
    module_path = root / "scripts" / "validate_quality_scale.py"
    spec = importlib.util.spec_from_file_location("validate_quality_scale", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validate_quality_scale = _load_module()


def _write_manifest(root: Path, quality_scale: str = "platinum") -> None:
    manifest_dir = root / "custom_components" / "enphase_ev"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"quality_scale": quality_scale})
    )


def _write_reference(root: Path, path: str) -> None:
    reference = root / path
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text("ok")


def _write_quality_scale(root: Path, body: str) -> None:
    (root / "quality_scale.yaml").write_text(body)


def test_repository_quality_scale_matches_manifest_claim() -> None:
    root = Path(__file__).resolve().parents[2]

    exit_code, messages = validate_quality_scale.validate_quality_scale(root)

    assert exit_code == 0, "\n".join(messages)


def test_platinum_claim_requires_gold_and_platinum_rules(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "platinum")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  silver:
    required: [config_flow]
  gold:
    required: [devices]
  platinum:
    required: [dynamic_devices]
rules:
  config_flow:
    status: done
    references:
      docs: [README.md]
  dynamic_devices:
    status: done
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "devices" in "\n".join(messages)


def test_na_rules_need_explanatory_comments(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "silver")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  silver:
    required: [discovery_update_info]
rules:
  discovery_update_info:
    status: n/a
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "n/a rules missing explanatory comments" in "\n".join(messages)


def test_na_status_is_restricted_to_allowlisted_rules(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "silver")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  silver:
    required: [config_flow]
rules:
  config_flow:
    status: n/a
    comment: Not actually acceptable for this rule.
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "Rules marked n/a without an allowlist exception" in "\n".join(messages)


def test_rule_references_must_exist(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "silver")
    _write_quality_scale(
        tmp_path,
        """
levels:
  silver:
    required: [config_flow]
rules:
  config_flow:
    status: done
    references:
      code: [custom_components/enphase_ev/config_flow.py]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "Rule references that do not exist" in "\n".join(messages)


def test_manifest_quality_scale_must_be_supported(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "diamond")
    _write_quality_scale(
        tmp_path,
        """
levels:
  silver:
    required: [config_flow]
rules:
  config_flow:
    status: done
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 2
    assert "Unsupported manifest quality_scale value" in "\n".join(messages)
