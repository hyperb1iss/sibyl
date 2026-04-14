"""Tests for export CLI pagination behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from typer.testing import CliRunner

from sibyl.cli import export as export_cli
from sibyl.cli.main import app as main_cli_app
from sibyl_core.models.entities import EntityType


@dataclass
class _FakeEntity:
    id: str
    name: str
    entity_type: str
    description: str = ""
    metadata: dict[str, object] | None = None
    created_at: str = "2026-04-14T00:00:00+00:00"

    def __post_init__(self) -> None:
        self.metadata = self.metadata or {}

    def model_dump(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "entity_type": self.entity_type,
            "description": self.description,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


@dataclass
class _FakeRelationship:
    id: str
    source_id: str
    target_id: str
    rel_type: str = "RELATED_TO"

    def model_dump(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "rel_type": self.rel_type,
        }


runner = CliRunner()


def test_export_commands_are_registered_on_main_cli() -> None:
    result = runner.invoke(main_cli_app, ["export", "--help"])

    assert result.exit_code == 0
    assert "graph" in result.stdout
    assert "tasks" in result.stdout
    assert "entities" in result.stdout


def test_export_tasks_pages_all_results(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "tasks.out"
    page_one = [
        _FakeEntity("task-1", "Task 1", "task"),
        _FakeEntity("task-2", "Task 2", "task"),
    ]
    page_two = [_FakeEntity("task-3", "Task 3", "task")]
    explore = AsyncMock(
        side_effect=[
            SimpleNamespace(entities=page_one, has_more=True),
            SimpleNamespace(entities=page_two, has_more=False),
        ]
    )

    monkeypatch.setattr(export_cli, "EXPLORE_PAGE_SIZE", 2)

    with patch("sibyl_core.tools.core.explore", explore):
        result = runner.invoke(
            export_cli.app,
            ["tasks", "--format", "json", "--output", str(output)],
        )

    assert result.exit_code == 0
    exported = json.loads(output.with_suffix(".json").read_text())
    assert [item["id"] for item in exported] == ["task-1", "task-2", "task-3"]
    assert explore.await_args_list == [
        call(mode="list", types=["task"], project=None, status=None, limit=2, offset=0),
        call(mode="list", types=["task"], project=None, status=None, limit=2, offset=2),
    ]


def test_export_entities_pages_all_results(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "entities.out"
    page_one = [
        _FakeEntity("pattern-1", "Pattern 1", "pattern"),
        _FakeEntity("pattern-2", "Pattern 2", "pattern"),
    ]
    page_two = [_FakeEntity("pattern-3", "Pattern 3", "pattern")]
    explore = AsyncMock(
        side_effect=[
            SimpleNamespace(entities=page_one, has_more=True),
            SimpleNamespace(entities=page_two, has_more=False),
        ]
    )

    monkeypatch.setattr(export_cli, "EXPLORE_PAGE_SIZE", 2)

    with patch("sibyl_core.tools.core.explore", explore):
        result = runner.invoke(
            export_cli.app,
            ["entities", "--type", "pattern", "--format", "json", "--output", str(output)],
        )

    assert result.exit_code == 0
    exported = json.loads(output.with_suffix(".json").read_text())
    assert [item["id"] for item in exported] == ["pattern-1", "pattern-2", "pattern-3"]
    assert explore.await_args_list == [
        call(mode="list", types=["pattern"], limit=2, offset=0),
        call(mode="list", types=["pattern"], limit=2, offset=2),
    ]


def test_export_graph_pages_entities_and_relationships(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "graph.json"
    entity_manager = MagicMock()
    relationship_manager = MagicMock()

    pattern_page_one = [
        _FakeEntity("pattern-1", "Pattern 1", "pattern"),
        _FakeEntity("pattern-2", "Pattern 2", "pattern"),
    ]
    pattern_page_two = [_FakeEntity("pattern-3", "Pattern 3", "pattern")]
    relationship_page_one = [
        _FakeRelationship("rel-1", "pattern-1", "pattern-2"),
        _FakeRelationship("rel-2", "pattern-2", "pattern-3"),
        _FakeRelationship("rel-3", "pattern-3", "pattern-1"),
    ]
    relationship_page_two = [_FakeRelationship("rel-4", "pattern-1", "pattern-3")]

    async def list_by_type(
        entity_type: EntityType, *, limit: int, offset: int
    ) -> list[_FakeEntity]:
        assert limit == 2
        if entity_type == EntityType.PATTERN:
            if offset == 0:
                return pattern_page_one
            if offset == 2:
                return pattern_page_two
        return []

    async def list_all(*, limit: int, offset: int) -> list[_FakeRelationship]:
        assert limit == 3
        if offset == 0:
            return relationship_page_one
        if offset == 3:
            return relationship_page_two
        return []

    entity_manager.list_by_type = AsyncMock(side_effect=list_by_type)
    relationship_manager.list_all = AsyncMock(side_effect=list_all)

    monkeypatch.setattr(export_cli, "GRAPH_ENTITY_PAGE_SIZE", 2)
    monkeypatch.setattr(export_cli, "GRAPH_RELATIONSHIP_PAGE_SIZE", 3)

    with (
        patch("sibyl_core.graph.client.get_graph_client", AsyncMock(return_value=AsyncMock())),
        patch("sibyl_core.graph.entities.EntityManager", return_value=entity_manager),
        patch(
            "sibyl_core.graph.relationships.RelationshipManager", return_value=relationship_manager
        ),
    ):
        result = runner.invoke(
            export_cli.app,
            ["graph", "--org-id", "org-123", "--output", str(output)],
        )

    assert result.exit_code == 0
    exported = json.loads(output.read_text())
    assert exported["metadata"]["entity_count"] == 3
    assert exported["metadata"]["relationship_count"] == 4
    assert entity_manager.list_by_type.await_args_list[:2] == [
        call(EntityType.PATTERN, limit=2, offset=0),
        call(EntityType.PATTERN, limit=2, offset=2),
    ]
    assert relationship_manager.list_all.await_args_list == [
        call(limit=3, offset=0),
        call(limit=3, offset=3),
    ]
