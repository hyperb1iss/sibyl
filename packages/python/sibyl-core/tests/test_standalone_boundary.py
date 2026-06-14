from __future__ import annotations

import ast
from pathlib import Path


def test_sibyl_core_does_not_import_api_package() -> None:
    source_root = Path(__file__).parents[1] / "src" / "sibyl_core"
    offenders: list[str] = []

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "sibyl" or node.module.startswith("sibyl."):
                    offenders.append(f"{path.relative_to(source_root)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sibyl" or alias.name.startswith("sibyl."):
                        offenders.append(f"{path.relative_to(source_root)}:{node.lineno}")

    assert offenders == []
