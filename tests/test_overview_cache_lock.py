from __future__ import annotations

import ast
from pathlib import Path


def test_overview_holds_build_lock_while_building_snapshot():
    source = (
        Path(__file__).resolve().parents[1] / "app/routes/api_diagnostics.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    overview = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "overview"
    )
    build_lock_with = next(
        node
        for node in ast.walk(overview)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Name)
            and item.context_expr.id == "_OVERVIEW_BUILD_LOCK"
            for item in node.items
        )
    )
    assert any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_build_overview"
        for node in build_lock_with.body
    )
