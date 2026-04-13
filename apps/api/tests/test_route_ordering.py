"""Tests for route ordering and backup ID generation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sibyl.api.routes.backups import router as backups_router
from sibyl.api.routes.crawler import router as crawler_router
from sibyl.backup_ids import generate_backup_id


class TestBackupIds:
    """Tests for backup ID generation."""

    def test_backup_ids_include_org_fragment_and_nonce(self) -> None:
        org_id = "12345678-1234-5678-9abc-def012345678"

        backup_id = generate_backup_id(org_id)
        prefix, org_fragment, date_part, time_part, nonce = backup_id.split("_")

        assert backup_id.startswith("backup_12345678_")
        assert prefix == "backup"
        assert org_fragment == "12345678"
        assert len(date_part) == 8
        assert len(time_part) == 6
        assert len(nonce) == 10

    def test_backup_ids_are_unique_per_call(self) -> None:
        org_id = "12345678-1234-5678-9abc-def012345678"

        first = generate_backup_id(org_id)
        second = generate_backup_id(org_id)

        assert first != second

    def test_jobs_package_imports_without_backup_cycle(self) -> None:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", "from sibyl.jobs import WorkerSettings"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr


class TestRouteOrdering:
    """Tests for static route precedence."""

    def test_backup_cleanup_precedes_dynamic_backup_id_route(self) -> None:
        paths = [route.path for route in backups_router.routes]

        assert paths.index("/backups/cleanup") < paths.index("/backups/{backup_id}")

    def test_crawler_link_graph_routes_precede_dynamic_source_route(self) -> None:
        paths = [route.path for route in crawler_router.routes]

        assert paths.index("/sources/link-graph/status") < paths.index("/sources/{source_id}")
        assert paths.index("/sources/link-graph") < paths.index("/sources/{source_id}")
