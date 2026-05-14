"""Tests for entity background jobs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.jobs.entities import (
    create_learning_episode,
    create_learning_procedure,
    serialize_memory_policy_context,
)
from sibyl_core.auth import MemoryPolicyContext, OrganizationRole
from sibyl_core.models.entities import Episode, Procedure
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
            patch("sibyl.jobs.entities.get_native_graph_runtime", AsyncMock(return_value=runtime)),
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
            patch("sibyl.jobs.entities.get_native_graph_runtime", AsyncMock()) as runtime,
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
            patch("sibyl.jobs.entities.get_native_graph_runtime", AsyncMock()) as runtime,
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
            patch("sibyl.jobs.entities.get_native_graph_runtime", AsyncMock()) as runtime,
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
    async def test_learning_episode_default_path_does_not_import_graphiti(self) -> None:
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
        original_import = __import__

        def guarded_import(name, globals_=None, locals_=None, fromlist=(), level=0):
            if name == "graphiti_core" or name.startswith("graphiti_core."):
                raise AssertionError(f"Graphiti import forbidden: {name}")
            return original_import(name, globals_, locals_, fromlist, level)

        with (
            patch("builtins.__import__", side_effect=guarded_import),
            patch("sibyl.jobs.entities.get_native_graph_runtime", AsyncMock(return_value=runtime)),
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
            patch("sibyl.jobs.entities.get_native_graph_runtime", AsyncMock(return_value=runtime)),
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
            patch("sibyl.jobs.entities.get_native_graph_runtime", AsyncMock()) as runtime,
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
