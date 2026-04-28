#!/usr/bin/env python3
"""Profile Enphase integration import time with Python 3.14 importtime output."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence

DEFAULT_PACKAGE = "custom_components.enphase_ev"


def discover_modules(root: Path, package: str = DEFAULT_PACKAGE) -> tuple[str, ...]:
    """Return top-level modules in the integration package."""

    package_path = root / Path(*package.split("."))
    if not package_path.is_dir():
        raise ValueError(f"Package path not found: {package_path}")

    modules = [package]
    modules.extend(
        f"{package}.{path.stem}"
        for path in sorted(package_path.glob("*.py"))
        if path.name != "__init__.py"
    )
    return tuple(modules)


def build_import_runner(
    modules: Sequence[str], *, warning_module_pattern: str | None = None
) -> str:
    """Return Python code that imports every requested module."""

    modules_json = json.dumps(list(modules))
    warning_lines = ""
    if warning_module_pattern is not None:
        warning_lines = (
            "import warnings\n"
            f"_warning_module = {warning_module_pattern!r}\n"
            "for _category in (\n"
            "    DeprecationWarning,\n"
            "    PendingDeprecationWarning,\n"
            "    FutureWarning,\n"
            "    SyntaxWarning,\n"
            "):\n"
            "    warnings.filterwarnings(\n"
            '        "error", category=_category, module=_warning_module\n'
            "    )\n"
        )
    return (
        "import importlib\n"
        f"{warning_lines}"
        f"modules = {modules_json}\n"
        "for module in modules:\n"
        "    importlib.import_module(module)\n"
        "print(f'imported {len(modules)} modules')\n"
    )


def run_importtime(
    root: Path,
    modules: Sequence[str],
    *,
    python: str = sys.executable,
    strict_warnings: bool = False,
    warning_module_pattern: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the Python 3.14 import-time profiler for the requested modules."""

    if strict_warnings and warning_module_pattern is None:
        warning_module_pattern = rf"{re.escape(DEFAULT_PACKAGE)}(\.|$)"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(root), env.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [
            python,
            "-X",
            "importtime=2",
            "-c",
            build_import_runner(modules, warning_module_pattern=warning_module_pattern),
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def render_report(result: subprocess.CompletedProcess[str]) -> str:
    """Combine importtime stderr with the import runner summary."""

    sections: list[str] = []
    if result.stdout:
        sections.append(result.stdout.rstrip())
    if result.stderr:
        sections.append(result.stderr.rstrip())
    return "\n\n".join(sections) + "\n"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Python 3.14 '-X importtime=2' against Enphase integration modules."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root to import from.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable to run with '-X importtime=2'.",
    )
    parser.add_argument(
        "--package",
        default=DEFAULT_PACKAGE,
        help="Package to discover modules from when --module is not provided.",
    )
    parser.add_argument(
        "--module",
        action="append",
        dest="modules",
        help="Explicit module to import. May be supplied more than once.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the combined importtime report.",
    )
    parser.add_argument(
        "--strict-integration-warnings",
        action="store_true",
        help=(
            "Treat import-time DeprecationWarning, PendingDeprecationWarning, "
            "FutureWarning, and SyntaxWarning from the integration package as errors."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = args.repo_root.resolve()
    modules = tuple(args.modules or discover_modules(root, args.package))
    warning_module_pattern = (
        rf"{re.escape(args.package)}(\.|$)"
        if args.strict_integration_warnings
        else None
    )
    result = run_importtime(
        root,
        modules,
        python=args.python,
        strict_warnings=args.strict_integration_warnings,
        warning_module_pattern=warning_module_pattern,
    )
    report = render_report(result)

    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")

    if result.returncode != 0 and args.output:
        print(report, file=sys.stderr, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
