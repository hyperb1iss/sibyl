"""Tests for entity background jobs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.jobs.entities import (
    backfill_entity_embeddings,
    create_entity,
    create_learning_episode,
    create_learning_procedure,
    serialize_memory_policy_context,
)
from sibyl_core.auth import MemoryPolicyContext, OrganizationRole
from sibyl_core.models.entities import (
    Entity,
    EntityType,
    Episode,
    Pattern,
    Procedure,
    Relationship,
    RelationshipType,
)
from sibyl_core.models.tasks import Task, TaskStatus


def _policy_payload(project_id: str = "proj-1", org_id: str = "org-1") -> dict[str, object]:
    payload = serialize_memory_policy_context(
        MemoryPolicyContext(
            actor_user_id="user-1",
            organization_id=org_id,
            organization_role=OrganizationRole.MEMBER,
            accessible_projects={project_id},
            project_id=project_id,
            memory_space="project",
            scope_key=project_id,
            source_surface="task_complete",
        )
    )
    assert payload is not None
    return payload


class TestCreateEntityJob:
    @pytest.mark.asyncio
    async def test_can_defer_entity_relationship_and_projection_embeddings(self) -> None:
        entity = Pattern(
            id="pattern-123",
            name="Lexical first",
            content="Persist text first and enrich vectors later.",
        )
        entity_manager = MagicMock()
        entity_manager.create_direct = AsyncMock(return_value="pattern-123")
        relationship_manager = MagicMock()
        relationship_manager.create_direct_bulk = AsyncMock(return_value=["rel-1"])
        relationship_manager.create = AsyncMock()
        runtime = SimpleNamespace(
            entity_manager=entity_manager,
            relationship_manager=relationship_manager,
        )
        projection = SimpleNamespace(
            errors=(),
            extracted=0,
            projected_entities=0,
            relationships=0,
            projection_state="complete",
            created_projected_entities=(
                Entity(
                    id="topic-samsung-tv",
                    entity_type=EntityType.TOPIC,
                    name="Samsung TV",
                    content="Projected topic",
                ),
            ),
            created_projection_relationships=(
                Relationship(
                    id="rel-pattern-123-mentions-topic",
                    source_id="pattern-123",
                    target_id="topic-samsung-tv",
                    relationship_type=RelationshipType.MENTIONS,
                ),
            ),
        )
        extraction_enqueue = SimpleNamespace(
            status="skipped",
            reason="disabled",
            job_ids=[],
            queued_sources=0,
            skipped_sources=1,
        )

        with (
            patch("sibyl.jobs.entities.get_surreal_graph_runtime", AsyncMock(return_value=runtime)),
            patch(
                "sibyl.jobs.entities.project_memory_entity",
                AsyncMock(return_value=projection),
            ) as project_memory,
            patch("sibyl.jobs.entities._safe_broadcast", AsyncMock()),
            patch("sibyl.jobs.pending.clear_pending", AsyncMock()),
            patch("sibyl.jobs.pending.process_pending_operations", AsyncMock(return_value=[])),
            patch(
                "sibyl_core.tools.conflicts.find_similar_entities",
                AsyncMock(return_value=[]),
            ),
            patch(
                "sibyl.jobs.memory_extraction.enqueue_memory_extraction_batches",
                AsyncMock(return_value=extraction_enqueue),
            ),
            patch(
                "sibyl.jobs.queue.enqueue_entity_embedding_backfill",
                AsyncMock(return_value="embed-pattern-123"),
            ) as enqueue_backfill,
        ):
            result = await create_entity(
                {},
                entity.model_dump(mode="json"),
                "pattern",
                "org-1",
                relationships=[
                    {
                        "id": "rel-1",
                        "source_id": "pattern-123",
                        "target_id": "project-1",
                        "type": "BELONGS_TO",
                    }
                ],
                generate_embeddings=False,
            )

        assert result["entity_id"] == "pattern-123"
        assert result["relationships_created"] == 1
        assert entity_manager.create_direct.await_args.kwargs["generate_embedding"] is False
        assert (
            relationship_manager.create_direct_bulk.await_args.kwargs["generate_embeddings"]
            is False
        )
        relationship_manager.create.assert_not_awaited()
        assert project_memory.await_args.kwargs["generate_embeddings"] is False
        assert result["embedding_backfill_job_id"] == "embed-pattern-123"
        enqueue_backfill.assert_awaited_once()
        entities_payload, group_id = enqueue_backfill.await_args.args
        assert group_id == "org-1"
        assert entities_payload[0]["id"] == "pattern-123"
        assert entities_payload[1]["id"] == "topic-samsung-tv"
        assert {
            relationship["id"]
            for relationship in enqueue_backfill.await_args.kwargs["relationships"]
        } == {
            "rel-1",
            "rel-pattern-123-mentions-topic",
        }


class TestBackfillEntityEmbeddingsJob:
    @pytest.mark.asyncio
    async def test_backfills_entity_and_relationship_embeddings(self) -> None:
        entity = Entity(
            id="session-123",
            entity_type="session",
            name="Lexical session",
            content="Persisted before embeddings were available.",
        )
        relationship = Relationship(
            id="rel-session-project",
            source_id="session-123",
            target_id="project-1",
            relationship_type=RelationshipType.RELATED_TO,
        )
        entity_manager = MagicMock()
        entity_manager.create_direct_bulk = AsyncMock(return_value=["session-123"])
        relationship_manager = MagicMock()
        relationship_manager.create_direct_bulk = AsyncMock(return_value=["rel-session-project"])
        runtime = SimpleNamespace(
            entity_manager=entity_manager,
            relationship_manager=relationship_manager,
        )

        with patch(
            "sibyl.jobs.entities.get_surreal_graph_runtime",
            AsyncMock(return_value=runtime),
        ):
            result = await backfill_entity_embeddings(
                {},
                [entity.model_dump(mode="json")],
                "org-1",
                relationships=[relationship.model_dump(mode="json")],
            )

        assert result["entities"] == 1
        assert result["relationships"] == 1
        assert entity_manager.create_direct_bulk.await_args.kwargs["generate_embeddings"] is True
        assert (
            relationship_manager.create_direct_bulk.await_args.kwargs["generate_embeddings"] is True
        )


class TestCreateLearningEpisodeJob:
    @pytest.mark.asyncio
    async def test_creates_learning_episode_with_native_relationships(self) -> None:
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
        entity_manager.create_direct = AsyncMock(return_value="episode_task-123")
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
        runtime = SimpleNamespace(
            entity_manager=entity_manager,
            relationship_manager=relationship_manager,
        )

        with (
            patch("sibyl.jobs.entities.get_surreal_graph_runtime", AsyncMock(return_value=runtime)),
            patch("sibyl.jobs.entities._safe_broadcast", AsyncMock()),
            patch("sibyl.jobs.entities.log_memory_audit_event", AsyncMock()) as audit,
        ):
            result = await create_learning_episode(
                {},
                task_data,
                "org-1",
                policy_context=_policy_payload(),
            )

        assert result["episode_id"] == "episode_task-123"
        assert result["task_id"] == "task-123"
        assert result["inherited_relationships"] == 1
        entity_manager.create_direct.assert_awaited_once()
        created_episode = entity_manager.create_direct.await_args.args[0]
        assert isinstance(created_episode, Episode)
        assert created_episode.metadata["task_id"] == "task-123"
        assert created_episode.metadata["policy_reason"] == "same_scope_write_allowed"
        assert created_episode.metadata["source_surface"] == "job"
        assert {rel.target_id for rel in created_relationships} == {"task-123", "pattern-1"}
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["policy_reason"] == "same_scope_write_allowed"
        assert audit.await_args.kwargs["source_surface"] == "job"
        assert set(audit.await_args.kwargs["details"]) <= {
            "job",
            "source_policy_surface",
            "task_id",
        }

    @pytest.mark.asyncio
    async def test_denies_project_learning_episode_without_policy_context(self) -> None:
        task = Task(
            id="task-123",
            title="Ship the thing",
            description="Complete the feature",
            project_id="proj-1",
            status=TaskStatus.DONE,
            learnings="The useful bit",
        )

        with (
            patch("sibyl.jobs.entities.get_surreal_graph_runtime", AsyncMock()) as runtime,
            patch("sibyl.jobs.entities.log_memory_audit_event", AsyncMock()) as audit,
            pytest.raises(ValueError, match="principal_mismatch"),
        ):
            await create_learning_episode({}, task.model_dump(mode="json"), "org-1")

        runtime.assert_not_awaited()
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["policy_allowed"] is False
        assert audit.await_args.kwargs["policy_reason"] == "principal_mismatch"
        assert audit.await_args.kwargs["source_surface"] == "job"
        assert set(audit.await_args.kwargs["details"]) <= {
            "job",
            "source_policy_surface",
            "task_id",
        }

    @pytest.mark.asyncio
    async def test_denies_project_learning_episode_with_wrong_policy_org(self) -> None:
        task = Task(
            id="task-123",
            title="Ship the thing",
            description="Complete the feature",
            project_id="proj-1",
            status=TaskStatus.DONE,
            learnings="The useful bit",
        )

        with (
            patch("sibyl.jobs.entities.get_surreal_graph_runtime", AsyncMock()) as runtime,
            patch("sibyl.jobs.entities.log_memory_audit_event", AsyncMock()) as audit,
            pytest.raises(ValueError, match="organization_mismatch"),
        ):
            await create_learning_episode(
                {},
                task.model_dump(mode="json"),
                "org-1",
                policy_context=_policy_payload(org_id="org-2"),
            )

        runtime.assert_not_awaited()
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["policy_allowed"] is False
        assert audit.await_args.kwargs["policy_reason"] == "organization_mismatch"
        assert set(audit.await_args.kwargs["details"]) <= {
            "job",
            "source_policy_surface",
            "task_id",
        }

    @pytest.mark.asyncio
    async def test_denies_learning_episode_when_policy_project_mismatches_task(self) -> None:
        task = Task(
            id="task-123",
            title="Ship the thing",
            description="Complete the feature",
            project_id=None,
            status=TaskStatus.DONE,
            learnings="The useful bit",
        )

        with (
            patch("sibyl.jobs.entities.get_surreal_graph_runtime", AsyncMock()) as runtime,
            patch("sibyl.jobs.entities.log_memory_audit_event", AsyncMock()) as audit,
            pytest.raises(ValueError, match="project_mismatch"),
        ):
            await create_learning_episode(
                {},
                task.model_dump(mode="json"),
                "org-1",
                policy_context=_policy_payload(project_id="proj-1"),
            )

        runtime.assert_not_awaited()
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["project_id"] == "proj-1"
        assert audit.await_args.kwargs["policy_allowed"] is False
        assert audit.await_args.kwargs["policy_reason"] == "project_mismatch"
        assert set(audit.await_args.kwargs["details"]) <= {
            "job",
            "source_policy_surface",
            "task_id",
        }

    @pytest.mark.asyncio
    async def test_learning_episode_default_path(self) -> None:
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
        entity_manager.create_direct = AsyncMock(return_value="episode_task-123")
        relationship_manager = MagicMock()
        relationship_manager.create = AsyncMock(return_value="rel_episode_task-123")
        relationship_manager.get_for_entity = AsyncMock(return_value=[])
        runtime = SimpleNamespace(
            entity_manager=entity_manager,
            relationship_manager=relationship_manager,
        )

        with (
            patch("sibyl.jobs.entities.get_surreal_graph_runtime", AsyncMock(return_value=runtime)),
            patch("sibyl.jobs.entities._safe_broadcast", AsyncMock()),
            patch("sibyl.jobs.entities.log_memory_audit_event", AsyncMock()),
        ):
            result = await create_learning_episode(
                {},
                task_data,
                "org-1",
                policy_context=_policy_payload(),
            )

        assert result["episode_id"] == "episode_task-123"
        assert result["task_id"] == "task-123"
        relationship_manager.create.assert_awaited_once()


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
        runtime = SimpleNamespace(
            entity_manager=entity_manager,
            relationship_manager=relationship_manager,
        )

        with (
            patch("sibyl.jobs.entities.get_surreal_graph_runtime", AsyncMock(return_value=runtime)),
            patch("sibyl.jobs.entities._safe_broadcast", AsyncMock()),
            patch("sibyl.jobs.entities.log_memory_audit_event", AsyncMock()) as audit,
        ):
            result = await create_learning_procedure(
                {},
                task_data,
                "org-1",
                policy_context=_policy_payload(),
            )

        assert result["created"] is True
        assert result["procedure_id"] == "procedure-task-123"
        assert result["notes_used"] == 1
        entity_manager.create_direct.assert_awaited_once()
        created_procedure = entity_manager.create_direct.await_args.args[0]
        assert isinstance(created_procedure, Procedure)
        assert created_procedure.steps
        assert created_procedure.steps[0].title == "Inspect the failing logs"
        assert created_procedure.metadata["task_id"] == "task-123"
        assert created_procedure.metadata["policy_reason"] == "same_scope_write_allowed"
        audit.assert_awaited_once()
        assert {rel.relationship_type.value for rel in created_relationships} == {
            "USES_PROCEDURE",
            "DERIVED_FROM",
            "REFERENCES",
        }

    @pytest.mark.asyncio
    async def test_denies_project_learning_procedure_without_policy_context(self) -> None:
        task = Task(
            id="task-123",
            title="Ship the thing",
            description="Complete the feature",
            project_id="proj-1",
            status=TaskStatus.DONE,
            learnings="1. Capture the reusable step",
        )

        with (
            patch("sibyl.jobs.entities.get_surreal_graph_runtime", AsyncMock()) as runtime,
            patch("sibyl.jobs.entities.log_memory_audit_event", AsyncMock()) as audit,
            pytest.raises(ValueError, match="principal_mismatch"),
        ):
            await create_learning_procedure({}, task.model_dump(mode="json"), "org-1")

        runtime.assert_not_awaited()
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["policy_allowed"] is False
        assert audit.await_args.kwargs["policy_reason"] == "principal_mismatch"
        assert set(audit.await_args.kwargs["details"]) <= {
            "job",
            "source_policy_surface",
            "task_id",
        }
