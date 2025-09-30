#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml  # type: ignore
except Exception:
    print("ERROR: PyYAML is required. Install with `pip install pyyaml`.", file=sys.stderr)
    raise


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    qs_path = root / "quality_scale.yaml"
    if not qs_path.exists():
        print(f"ERROR: {qs_path} not found", file=sys.stderr)
        return 2
    data = yaml.safe_load(qs_path.read_text()) or {}

    levels = (data or {}).get("levels") or {}
    rules = (data or {}).get("rules") or {}

    silver = (levels.get("silver") or {}).get("required") or []
    if not silver:
        print("ERROR: No silver.required rules defined in quality_scale.yaml", file=sys.stderr)
        return 2

    missing: list[str] = []
    not_done: list[tuple[str, str]] = []

    for rule in silver:
        entry = rules.get(rule)
        if entry is None:
            missing.append(rule)
            continue
        status = None
        if isinstance(entry, dict):
            status = str(entry.get("status") or "").strip().lower()
        elif isinstance(entry, str):
            status = entry.strip().lower()
        else:
            status = ""
        if status != "done":
            not_done.append((rule, status or "<unset>"))

    if missing or not_done:
        print("Integration Quality Scale check failed:\n")
        if missing:
            print("- Missing rule entries:")
            for r in missing:
                print(f"  • {r}")
        if not_done:
            print("- Rules not marked done:")
            for r, st in not_done:
                print(f"  • {r}: {st}")
        return 1

    print("Integration Quality Scale (silver) OK: all required rules are done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
