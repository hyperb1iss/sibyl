from __future__ import annotations

from pathlib import Path

from tools.lint.no_direct_storage_access import (
    DirectStorageImport,
    collect_direct_storage_imports,
    display_path,
    main,
    render_report,
)

GRAPHITI_MODULE = "graphiti" + "_core"


def test_collect_direct_storage_imports_flags_unallowlisted_modules(tmp_path: Path) -> None:
    route_dir = tmp_path / "apps/api/src/sibyl/api/routes"
    route_dir.mkdir(parents=True)
    path = route_dir / "bad.py"
    path.write_text(
        "import graphiti\n"
        f"from {GRAPHITI_MODULE}.nodes import EntityNode\n"
        "from sqlalchemy.ext.asyncio import AsyncSession\n"
        "from sqlmodel import select\n"
        "from sibyl_core.services.graph import EntityManager\n",
        encoding="utf-8",
    )

    violations = collect_direct_storage_imports(targets=(route_dir,))

    assert [(violation.module, violation.allowlisted) for violation in violations] == [
        ("graphiti", False),
        (f"{GRAPHITI_MODULE}.nodes", False),
        ("sqlalchemy.ext.asyncio", False),
        ("sqlmodel", False),
    ]


def test_collect_direct_storage_imports_honors_exact_allowlist_entries(tmp_path: Path) -> None:
    route_dir = tmp_path / "apps/api/src/sibyl/api/routes"
    route_dir.mkdir(parents=True)
    path = route_dir / "allowed.py"
    path.write_text(
        "from sqlalchemy.orm import Session\nfrom sqlmodel import select\n",
        encoding="utf-8",
    )

    violations = collect_direct_storage_imports(
        targets=(route_dir,),
        allowlist={display_path(path): ("sqlalchemy",)},
    )

    assert [(violation.module, violation.allowlisted) for violation in violations] == [
        ("sqlalchemy.orm", True),
        ("sqlmodel", False),
    ]


def test_collect_direct_storage_imports_ignores_type_checking_imports(tmp_path: Path) -> None:
    route_dir = tmp_path / "apps/api/src/sibyl/auth"
    route_dir.mkdir(parents=True)
    path = route_dir / "dependencies.py"
    path.write_text(
        "from typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n"
        "    from sqlalchemy.ext.asyncio import AsyncSession\n"
        f"    from {GRAPHITI_MODULE}.nodes import EntityNode\n",
        encoding="utf-8",
    )

    assert collect_direct_storage_imports(targets=(path,)) == []


def test_render_report_separates_unallowlisted_and_allowlisted_entries() -> None:
    report = render_report(
        [
            DirectStorageImport(
                path="apps/api/src/sibyl/api/routes/graph.py",
                lineno=10,
                module=f"{GRAPHITI_MODULE}.nodes",
                reason="Graphiti runtime import",
                allowlisted=True,
            ),
            DirectStorageImport(
                path="apps/api/src/sibyl/api/routes/new_surface.py",
                lineno=4,
                module="sqlalchemy.ext.asyncio",
                reason="raw SQLAlchemy import",
                allowlisted=False,
            ),
        ]
    )

    assert "1 unallowlisted, 1 allowlisted" in report
    assert "unallowlisted imports:" in report
    assert "allowlisted debt:" in report


def test_main_returns_nonzero_for_unallowlisted_imports(tmp_path: Path) -> None:
    route_dir = tmp_path / "apps/api/src/sibyl/api/routes"
    route_dir.mkdir(parents=True)
    (route_dir / "bad.py").write_text(
        f"from {GRAPHITI_MODULE} import Graphiti\n",
        encoding="utf-8",
    )

    assert main(["--path", str(route_dir)]) == 1


def test_repository_has_no_live_direct_storage_import_debt() -> None:
    assert collect_direct_storage_imports() == []
