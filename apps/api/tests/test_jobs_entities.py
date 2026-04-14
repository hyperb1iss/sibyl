"""Tests for entity background jobs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.jobs.entities import create_learning_procedure
from sibyl_core.models.entities import Procedure
from sibyl_core.models.tasks import Task, TaskStatus


class TestCreateLearningProcedureJob:
    @pytest.mark.asyncio
    async def test_creates_procedure_from_task_learnings_and_notes(self) -> None:
        task = Task(
            id="task-123",
            title="Ship the thing",
            description="Complete the feature",
            project_id="proj-1",
            status=TaskStatus.DONE,
            learnings="1. Inspect the failing logs\n2. Capture the pattern\n3. Write the fix",
        )
        task_data = task.model_dump(mode="json")

        entity_manager = MagicMock()
        entity_manager.create_direct = AsyncMock(return_value="procedure-task-123")
        entity_manager.get_notes_for_task = AsyncMock(
            return_value=[
                SimpleNamespace(content="4. Document the workaround"),
            ]
        )
        relationship_manager = MagicMock()
        created_relationships = []

        async def _record_relationship(relationship):
            created_relationships.append(relationship)
            return relationship.id

        relationship_manager.create = AsyncMock(side_effect=_record_relationship)
        relationship_manager.get_for_entity = AsyncMock(
            return_value=[
                SimpleNamespace(target_id="pattern-1"),
            ]
        )
        client = SimpleNamespace()

        with (
            patch("sibyl_core.graph.client.get_graph_client", AsyncMock(return_value=client)),
            patch("sibyl_core.graph.entities.EntityManager", return_value=entity_manager),
            patch(
                "sibyl_core.graph.relationships.RelationshipManager",
                return_value=relationship_manager,
            ),
            patch("sibyl.jobs.entities._safe_broadcast", AsyncMock()),
        ):
            result = await create_learning_procedure({}, task_data, "org-1")

        assert result["created"] is True
        assert result["procedure_id"] == "procedure-task-123"
        assert result["notes_used"] == 1
        entity_manager.create_direct.assert_awaited_once()
        created_procedure = entity_manager.create_direct.await_args.args[0]
        assert isinstance(created_procedure, Procedure)
        assert created_procedure.steps
        assert created_procedure.steps[0].title == "Inspect the failing logs"
        assert created_procedure.metadata["task_id"] == "task-123"
        assert {rel.relationship_type.value for rel in created_relationships} == {
            "USES_PROCEDURE",
            "DERIVED_FROM",
            "REFERENCES",
        }
