from __future__ import annotations

import ast
from pathlib import Path

INTEGRATION_ROOT = Path("custom_components/enphase_ev")
TASK_FACTORY_NAMES = {"async_create_task", "create_task"}
REDACTION_HELPERS = {"redact_identifier", "redact_site_id"}
SENSITIVE_NAMES = {"sn", "sn_str", "serial", "site_id"}
SENSITIVE_ATTRIBUTES = {"_serial", "serial", "site_id"}


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _contains_unredacted_identifier(node: ast.AST) -> bool:
    call_name = _call_name(node.func) if isinstance(node, ast.Call) else None
    if call_name in REDACTION_HELPERS:
        return False
    if isinstance(node, ast.Name):
        name = node.id.lower()
        return name in SENSITIVE_NAMES or "serial" in name
    if isinstance(node, ast.Attribute):
        attr = node.attr.lower()
        return attr in SENSITIVE_ATTRIBUTES or "serial" in attr
    return any(
        _contains_unredacted_identifier(child) for child in ast.iter_child_nodes(node)
    )


def _unsafe_task_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    unsafe_names: list[str] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or _call_name(node.func) not in TASK_FACTORY_NAMES
        ):
            continue
        name_keyword = next(
            (keyword for keyword in node.keywords if keyword.arg == "name"),
            None,
        )
        if name_keyword is None:
            continue
        name_value = name_keyword.value
        if isinstance(name_value, ast.Constant):
            continue
        if _contains_unredacted_identifier(name_value):
            unsafe_names.append(f"{path}:{node.lineno}: {ast.unparse(name_value)}")
    return unsafe_names


def test_task_names_do_not_include_unredacted_identifiers() -> None:
    unsafe_names = [
        unsafe_name
        for path in sorted(INTEGRATION_ROOT.rglob("*.py"))
        for unsafe_name in _unsafe_task_names(path)
    ]

    assert unsafe_names == []
