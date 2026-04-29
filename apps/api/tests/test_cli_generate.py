"""Tests for synthetic data generation CLI commands."""

from __future__ import annotations

from typer.testing import CliRunner

from sibyl.cli import generate as generate_cli

runner = CliRunner()


def test_clean_requires_org_id() -> None:
    result = runner.invoke(generate_cli.app, ["clean", "--yes"])

    assert result.exit_code == 1
    assert "--org-id is required for graph operations" in result.output


def test_count_result_rows_prefers_count_fields() -> None:
    rows = [{"count": 2}, {"count": 3}, {"uuid": "entity-1"}]

    assert generate_cli._count_result_rows(rows) == 5


def test_count_result_rows_falls_back_to_row_count() -> None:
    rows = [{"uuid": "entity-1"}, {"uuid": "entity-2"}]

    assert generate_cli._count_result_rows(rows) == 2
