from __future__ import annotations

import ast
from pathlib import Path

CORE_SRC = Path(__file__).resolve().parents[1] / "src" / "sibyl_core"
FORBIDDEN_IMPORT_PREFIXES = ("sibyl", "apps.api")


def test_sibyl_core_source_does_not_import_api_runtime() -> None:
    offenders: list[str] = []
    for path in sorted(CORE_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for module in _imported_modules(node):
                if _is_forbidden_import(module):
                    relpath = path.relative_to(CORE_SRC)
                    offenders.append(f"{relpath}:{node.lineno}:{module}")

    assert offenders == []


def _imported_modules(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.ImportFrom):
        return (node.module,) if node.module else ()
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    return ()


def _is_forbidden_import(module: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in FORBIDDEN_IMPORT_PREFIXES
    )
