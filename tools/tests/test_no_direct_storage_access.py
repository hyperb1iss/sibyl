from __future__ import annotations

from pathlib import Path

from tools.lint.no_direct_storage_access import (
    DirectStorageImport,
    collect_direct_storage_imports,
    display_path,
    main,
    render_report,
)


def test_collect_direct_storage_imports_flags_unallowlisted_modules(tmp_path: Path) -> None:
    route_dir = tmp_path / "apps/api/src/sibyl/api/routes"
    route_dir.mkdir(parents=True)
    path = route_dir / "bad.py"
    path.write_text(
        "from sibyl_core.graph.client import get_graph_client\n"
        "from sibyl.auth.rls import get_auth_session\n"
        "from sibyl.auth.sessions import SessionManager\n"
        "from sibyl.persistence.legacy.auth_runtime import get_user_by_id\n"
        "from sqlmodel import select\n",
        encoding="utf-8",
    )

    violations = collect_direct_storage_imports(targets=(route_dir,))

    assert [(violation.module, violation.allowlisted) for violation in violations] == [
        ("sibyl_core.graph.client", False),
        ("sibyl.auth.rls", False),
        ("sibyl.auth.sessions", False),
        ("sibyl.persistence.legacy.auth_runtime", False),
        ("sqlmodel", False),
    ]


def test_collect_direct_storage_imports_honors_exact_allowlist_entries(tmp_path: Path) -> None:
    route_dir = tmp_path / "apps/api/src/sibyl/api/routes"
    route_dir.mkdir(parents=True)
    path = route_dir / "allowed.py"
    path.write_text(
        "from sibyl_core.graph.client import get_graph_client\nfrom sqlmodel import select\n",
        encoding="utf-8",
    )

    violations = collect_direct_storage_imports(
        targets=(route_dir,),
        allowlist={display_path(path): ("sibyl_core.graph",)},
    )

    assert [(violation.module, violation.allowlisted) for violation in violations] == [
        ("sibyl_core.graph.client", True),
        ("sqlmodel", False),
    ]


def test_render_report_separates_unallowlisted_and_allowlisted_entries() -> None:
    report = render_report(
        [
            DirectStorageImport(
                path="apps/api/src/sibyl/api/routes/graph.py",
                lineno=10,
                module="sibyl_core.graph.client",
                reason="legacy graph runtime import",
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
        "from sibyl_core.graph.entities import EntityManager\n",
        encoding="utf-8",
    )

    assert main(["--path", str(route_dir)]) == 1


def test_repository_has_no_live_direct_storage_import_debt() -> None:
    assert collect_direct_storage_imports() == []
