"""Tests for entity background jobs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.jobs.entities import (
    _save_episode_mention,
    create_learning_episode,
    create_learning_procedure,
)
from sibyl_core.models.entities import Episode, Procedure
from sibyl_core.models.tasks import Task, TaskStatus


class TestCreateLearningEpisodeJob:
    @pytest.mark.asyncio
    async def test_surreal_like_driver_saves_episode_mentions(self) -> None:
        edge_ops = SimpleNamespace(save=AsyncMock())
        driver = SimpleNamespace(episodic_edge_ops=edge_ops)
        client = SimpleNamespace(get_org_driver=lambda group_id: driver)

        assert await _save_episode_mention(
            client,
            group_id="org-1",
            episode_id="episode-task-123",
            target_id="task-123",
            link_id="mention-1",
        )

        edge_ops.save.assert_awaited_once()
        saved_edge = edge_ops.save.await_args.args[1]
        assert saved_edge.uuid == "mention-1"
        assert saved_edge.source_node_uuid == "episode-task-123"
        assert saved_edge.target_node_uuid == "task-123"

    @pytest.mark.asyncio
    async def test_uses_episode_mentions_for_surreal_learning_links(self) -> None:
        task = Task(
            id="task-123",
            title="Ship the thing",
            description="Complete the feature",
            project_id="proj-1",
            status=TaskStatus.DONE,
            learnings="The useful bit",
        )
        task_data = task.model_dump(mode="json")

        entity_manager = MagicMock()
        entity_manager.create = AsyncMock(return_value="episode_task-123")
        relationship_manager = MagicMock()
        relationship_manager.create = AsyncMock()
        relationship_manager.get_for_entity = AsyncMock(
            return_value=[
                SimpleNamespace(target_id="pattern-1"),
            ]
        )
        save_episode_mention = AsyncMock(return_value=True)
        client = SimpleNamespace()

        with (
            patch("sibyl_core.graph.client.get_graph_client", AsyncMock(return_value=client)),
            patch("sibyl_core.graph.entities.EntityManager", return_value=entity_manager),
            patch(
                "sibyl_core.graph.relationships.RelationshipManager",
                return_value=relationship_manager,
            ),
            patch("sibyl.jobs.entities._save_episode_mention", save_episode_mention),
            patch("sibyl.jobs.entities._safe_broadcast", AsyncMock()),
        ):
            result = await create_learning_episode({}, task_data, "org-1")

        assert result["episode_id"] == "episode_task-123"
        assert result["task_id"] == "task-123"
        assert result["inherited_relationships"] == 1
        entity_manager.create.assert_awaited_once()
        created_episode = entity_manager.create.await_args.args[0]
        assert isinstance(created_episode, Episode)
        assert created_episode.metadata["task_id"] == "task-123"
        relationship_manager.create.assert_not_awaited()
        assert save_episode_mention.await_count == 2
        save_episode_mention.assert_any_await(
            client,
            group_id="org-1",
            episode_id="episode_task-123",
            target_id="task-123",
            link_id="rel_episode_task-123",
        )
        save_episode_mention.assert_any_await(
            client,
            group_id="org-1",
            episode_id="episode_task-123",
            target_id="pattern-1",
            link_id="rel_episode_task-123_pattern-1",
        )

    @pytest.mark.asyncio
    async def test_surreal_learning_episode_skips_missing_mention_endpoint(self) -> None:
        task = Task(
            id="task-123",
            title="Ship the thing",
            description="Complete the feature",
            project_id="proj-1",
            status=TaskStatus.DONE,
            learnings="The useful bit",
        )
        task_data = task.model_dump(mode="json")

        entity_manager = MagicMock()
        entity_manager.create = AsyncMock(return_value="episode_task-123")
        relationship_manager = MagicMock()
        relationship_manager.create = AsyncMock(side_effect=AssertionError("wrong edge table"))
        relationship_manager.get_for_entity = AsyncMock(return_value=[])
        save_episode_mention = AsyncMock(side_effect=ValueError("target entity not found"))
        client = SimpleNamespace()

        with (
            patch("sibyl_core.graph.client.get_graph_client", AsyncMock(return_value=client)),
            patch("sibyl_core.graph.entities.EntityManager", return_value=entity_manager),
            patch(
                "sibyl_core.graph.relationships.RelationshipManager",
                return_value=relationship_manager,
            ),
            patch("sibyl.jobs.entities._save_episode_mention", save_episode_mention),
            patch("sibyl.jobs.entities._get_surreal_driver", MagicMock(return_value=object())),
            patch("sibyl.jobs.entities._safe_broadcast", AsyncMock()),
        ):
            result = await create_learning_episode({}, task_data, "org-1")

        assert result["episode_id"] == "episode_task-123"
        assert result["task_id"] == "task-123"
        relationship_manager.create.assert_not_awaited()
        save_episode_mention.assert_awaited_once()


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
