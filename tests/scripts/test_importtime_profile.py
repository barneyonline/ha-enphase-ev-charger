from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys


def _load_module():
    root = Path(__file__).resolve().parents[2]
    module_path = root / "scripts" / "importtime_profile.py"
    spec = importlib.util.spec_from_file_location("importtime_profile", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


importtime_profile = _load_module()


def test_discover_modules_returns_integration_package_modules(tmp_path: Path) -> None:
    package_root = tmp_path / "custom_components" / "enphase_ev"
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "sensor.py").write_text("", encoding="utf-8")
    (package_root / "api.py").write_text("", encoding="utf-8")
    (package_root / "README.md").write_text("ignored", encoding="utf-8")

    modules = importtime_profile.discover_modules(tmp_path)

    assert modules == (
        "custom_components.enphase_ev",
        "custom_components.enphase_ev.api",
        "custom_components.enphase_ev.sensor",
    )


def test_build_import_runner_imports_each_module() -> None:
    runner = importtime_profile.build_import_runner(
        ("package", "package.module"),
        warning_module_pattern=r"package(\.|$)",
    )

    assert "importlib.import_module(module)" in runner
    assert '"package", "package.module"' in runner
    assert "warnings.filterwarnings" in runner
    assert "DeprecationWarning" in runner
    assert "imported {len(modules)} modules" in runner


def test_run_importtime_uses_python314_importtime(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(args[0], 0, "imported 1 modules\n", "")

    monkeypatch.setattr(importtime_profile.subprocess, "run", fake_run)

    result = importtime_profile.run_importtime(
        tmp_path,
        ("custom_components.enphase_ev",),
        python="python3.14",
        strict_warnings=True,
    )

    assert result.returncode == 0
    command = calls[0]["args"][0]
    assert command[:3] == ["python3.14", "-X", "importtime=2"]
    assert "custom_components\\\\.enphase_ev" in command[4]
    assert calls[0]["kwargs"]["cwd"] == tmp_path
    assert calls[0]["kwargs"]["text"] is True
    assert calls[0]["kwargs"]["capture_output"] is True
    assert str(tmp_path) in calls[0]["kwargs"]["env"]["PYTHONPATH"].split(os.pathsep)


def test_main_writes_output(monkeypatch, tmp_path: Path) -> None:
    def fake_run_importtime(
        _root,
        _modules,
        *,
        python,
        strict_warnings,
        warning_module_pattern,
    ):
        assert strict_warnings is True
        assert warning_module_pattern == r"custom_components\.enphase_ev(\.|$)"
        return subprocess.CompletedProcess(
            [python], 0, "imported 1 modules\n", "import time: test\n"
        )

    monkeypatch.setattr(
        importtime_profile,
        "run_importtime",
        fake_run_importtime,
    )
    output = tmp_path / "importtime.log"

    exit_code = importtime_profile.main(
        [
            "--repo-root",
            str(tmp_path),
            "--module",
            "custom_components.enphase_ev",
            "--strict-integration-warnings",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert output.read_text(encoding="utf-8") == (
        "imported 1 modules\n\nimport time: test\n"
    )
