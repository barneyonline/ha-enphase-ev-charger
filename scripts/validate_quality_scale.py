#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import yaml  # type: ignore
except Exception:
    print(
        "ERROR: PyYAML is required. Install with `pip install pyyaml`.", file=sys.stderr
    )
    raise

QUALITY_LEVEL_ORDER = ("silver", "gold", "platinum")
NA_ALLOWED_RULES = {"discovery_update_info"}


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def _status_for_rule(entry: object) -> str:
    if isinstance(entry, dict):
        return str(entry.get("status") or "").strip().lower()
    if isinstance(entry, str):
        return entry.strip().lower()
    return ""


def _references_for_rule(entry: object) -> dict[str, list[str]]:
    if not isinstance(entry, dict):
        return {}
    references = entry.get("references")
    if not isinstance(references, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for key in ("code", "tests", "docs"):
        values = references.get(key)
        if isinstance(values, str):
            normalized[key] = [values]
        elif isinstance(values, list):
            normalized[key] = [str(value) for value in values if str(value).strip()]
    return normalized


def _claimed_level(root: Path) -> str:
    manifest_path = root / "custom_components" / "enphase_ev" / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"{manifest_path} not found")
    manifest = json.loads(manifest_path.read_text())
    level = str(manifest.get("quality_scale") or "silver").strip().lower()
    if level not in QUALITY_LEVEL_ORDER:
        raise ValueError(f"Unsupported manifest quality_scale value: {level!r}")
    return level


def _required_levels_for_claim(level: str) -> tuple[str, ...]:
    claimed_index = QUALITY_LEVEL_ORDER.index(level)
    return QUALITY_LEVEL_ORDER[: claimed_index + 1]


def _reference_path_exists(root: Path, reference: str) -> bool:
    path_text = reference.split("#", 1)[0].strip()
    if not path_text:
        return False
    return (root / path_text).exists()


def validate_quality_scale(root: Path) -> tuple[int, list[str]]:
    qs_path = root / "quality_scale.yaml"
    if not qs_path.exists():
        return 2, [f"ERROR: {qs_path} not found"]
    try:
        claimed_level = _claimed_level(root)
    except (json.JSONDecodeError, ValueError) as err:
        return 2, [f"ERROR: {err}"]

    data = _load_yaml(qs_path)
    levels = data.get("levels") or {}
    rules = data.get("rules") or {}

    messages: list[str] = []
    missing: list[str] = []
    not_accepted: list[tuple[str, str]] = []
    not_allowed_na: list[str] = []
    missing_references: list[str] = []
    missing_comments: list[str] = []
    bad_references: list[tuple[str, str]] = []

    for level in _required_levels_for_claim(claimed_level):
        required = (levels.get(level) or {}).get("required") or []
        if not required:
            messages.append(
                f"ERROR: No {level}.required rules defined in quality_scale.yaml"
            )
            return 2, messages
        for rule in required:
            entry = rules.get(rule)
            if entry is None:
                missing.append(rule)
                continue
            status = _status_for_rule(entry)
            if status == "done":
                pass
            elif status == "n/a":
                if rule not in NA_ALLOWED_RULES:
                    not_allowed_na.append(rule)
                elif not (
                    isinstance(entry, dict) and str(entry.get("comment") or "").strip()
                ):
                    missing_comments.append(rule)
            else:
                not_accepted.append((rule, status or "<unset>"))
            references = _references_for_rule(entry)
            if not any(references.values()):
                missing_references.append(rule)
                continue
            for refs in references.values():
                for reference in refs:
                    if not _reference_path_exists(root, reference):
                        bad_references.append((rule, reference))

    if (
        missing
        or not_accepted
        or not_allowed_na
        or missing_references
        or missing_comments
        or bad_references
    ):
        messages.append(
            f"Integration Quality Scale check failed for manifest level "
            f"{claimed_level!r}:\n"
        )
        if missing:
            messages.append("- Missing rule entries:")
            messages.extend(f"  - {rule}" for rule in missing)
        if not_accepted:
            messages.append("- Rules not marked done:")
            messages.extend(f"  - {rule}: {status}" for rule, status in not_accepted)
        if not_allowed_na:
            messages.append("- Rules marked n/a without an allowlist exception:")
            messages.extend(f"  - {rule}" for rule in not_allowed_na)
        if missing_comments:
            messages.append("- n/a rules missing explanatory comments:")
            messages.extend(f"  - {rule}" for rule in missing_comments)
        if missing_references:
            messages.append("- Rules missing code/test/doc references:")
            messages.extend(f"  - {rule}" for rule in missing_references)
        if bad_references:
            messages.append("- Rule references that do not exist:")
            messages.extend(
                f"  - {rule}: {reference}" for rule, reference in bad_references
            )
        return 1, messages

    messages.append(
        f"Integration Quality Scale ({claimed_level}) OK: all claimed-level rules "
        "are documented."
    )
    return 0, messages


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    exit_code, messages = validate_quality_scale(root)
    output = sys.stderr if exit_code else sys.stdout
    print("\n".join(messages), file=output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
