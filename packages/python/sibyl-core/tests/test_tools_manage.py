"""Tests for sibyl-core manage tool.

Covers the manage() function and its action handlers:
- Input validation (action, entity_id, organization_id)
- Task workflow actions (start, block, complete, update, add_note)
- Epic workflow actions (start, complete, archive, update)
- Response formatting (success/error cases)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl_core.errors import EntityNotFoundError, InvalidTransitionError
from sibyl_core.models.entities import EntityType
from sibyl_core.models.tasks import TaskStatus
from sibyl_core.tools.manage import (
    ALL_ACTIONS,
    ANALYSIS_ACTIONS,
    EPIC_ACTIONS,
    SOURCE_ACTIONS,
    TASK_ACTIONS,
    ManageResponse,
    manage,
)

from .conftest import (
    make_entity,
)

# =============================================================================
# Action Constants Tests
# =============================================================================


class TestActionConstants:
    """Test action type constants."""

    def test_task_actions_contains_expected(self) -> None:
        """TASK_ACTIONS contains all expected task workflow actions."""
        expected = {
            "start_task",
            "block_task",
            "unblock_task",
            "submit_review",
            "complete_task",
            "archive_task",
            "update_task",
            "add_note",
        }
        assert expected == TASK_ACTIONS

    def test_epic_actions_contains_expected(self) -> None:
        """EPIC_ACTIONS contains all expected epic workflow actions."""
        expected = {
            "start_epic",
            "complete_epic",
            "archive_epic",
            "update_epic",
        }
        assert expected == EPIC_ACTIONS

    def test_source_actions_contains_expected(self) -> None:
        """SOURCE_ACTIONS contains all expected source operations."""
        expected = {
            "crawl",
            "sync",
            "refresh",
            "link_graph",
            "link_graph_status",
        }
        assert expected == SOURCE_ACTIONS

    def test_analysis_actions_contains_expected(self) -> None:
        """ANALYSIS_ACTIONS contains all expected analysis actions."""
        expected = {
            "estimate",
            "prioritize",
            "detect_cycles",
            "suggest",
        }
        assert expected == ANALYSIS_ACTIONS

    def test_all_actions_is_union(self) -> None:
        """ALL_ACTIONS is the union of all action sets."""
        assert ALL_ACTIONS == TASK_ACTIONS | EPIC_ACTIONS | SOURCE_ACTIONS | ANALYSIS_ACTIONS


# =============================================================================
# ManageResponse Tests
# =============================================================================


class TestManageResponse:
    """Test ManageResponse dataclass."""

    def test_manage_response_success(self) -> None:
        """ManageResponse for successful operation."""
        response = ManageResponse(
            success=True,
            action="start_task",
            entity_id="task_123",
            message="Task started",
            data={"status": "doing"},
        )
        assert response.success is True
        assert response.action == "start_task"
        assert response.entity_id == "task_123"
        assert response.message == "Task started"
        assert response.data["status"] == "doing"
        assert response.timestamp.tzinfo == UTC

    def test_manage_response_failure(self) -> None:
        """ManageResponse for failed operation."""
        response = ManageResponse(
            success=False,
            action="complete_task",
            entity_id="task_456",
            message="Invalid transition: todo -> done",
            data={"from_status": "todo", "to_status": "done"},
        )
        assert response.success is False
        assert "Invalid transition" in response.message

    def test_manage_response_defaults(self) -> None:
        """ManageResponse has correct defaults."""
        response = ManageResponse(success=True, action="test")
        assert response.entity_id is None
        assert response.message == ""
        assert response.data == {}
        assert isinstance(response.timestamp, datetime)


# =============================================================================
# Input Validation Tests
# =============================================================================


class TestInputValidation:
    """Test manage() input validation."""

    @pytest.mark.asyncio
    async def test_unknown_action_returns_error(self) -> None:
        """Unknown action returns error response."""
        response = await manage(
            action="nonexistent_action",
            organization_id="org_123",
        )
        assert response.success is False
        assert "Unknown action" in response.message
        assert "nonexistent_action" in response.message

    @pytest.mark.asyncio
    async def test_action_is_case_insensitive(self) -> None:
        """Action is normalized to lowercase."""
        # Should not error on case (but will fail on missing entity_id)
        response = await manage(
            action="START_TASK",
            organization_id="org_123",
        )
        # Should get entity_id required error, not unknown action
        assert "entity_id required" in response.message or "Unknown action" not in response.message

    @pytest.mark.asyncio
    async def test_action_is_trimmed(self) -> None:
        """Action whitespace is trimmed."""
        response = await manage(
            action="  start_task  ",
            organization_id="org_123",
        )
        # Should get entity_id required, not unknown action
        assert "Unknown action" not in response.message

    @pytest.mark.asyncio
    async def test_missing_organization_id(self) -> None:
        """Missing organization_id returns error."""
        response = await manage(
            action="start_task",
            entity_id="task_123",
            organization_id=None,
        )
        assert response.success is False
        assert "organization_id required" in response.message

    @pytest.mark.asyncio
    async def test_task_action_requires_entity_id(self) -> None:
        """Task actions require entity_id."""
        mock_client = AsyncMock()
        with patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client):
            response = await manage(
                action="start_task",
                entity_id=None,
                organization_id="org_123",
            )
            assert response.success is False
            assert "entity_id required" in response.message

    @pytest.mark.asyncio
    async def test_update_task_requires_entity_id(self) -> None:
        """update_task also requires entity_id."""
        mock_client = AsyncMock()
        with patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client):
            response = await manage(
                action="update_task",
                entity_id=None,
                data={"title": "New Title"},
                organization_id="org_123",
            )
            assert response.success is False
            assert "entity_id required" in response.message

    @pytest.mark.asyncio
    async def test_epic_action_requires_entity_id(self) -> None:
        """Epic actions require entity_id."""
        mock_client = AsyncMock()
        with patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client):
            response = await manage(
                action="start_epic",
                entity_id=None,
                organization_id="org_123",
            )
            assert response.success is False
            assert "entity_id required" in response.message


# =============================================================================
# Task Action Tests
# =============================================================================


class TestTaskActions:
    """Test task workflow actions."""

    @pytest.mark.asyncio
    async def test_start_task_success(self) -> None:
        """start_task moves task to doing status."""
        mock_client = AsyncMock()
        mock_task = MagicMock()
        mock_task.status = TaskStatus.DOING
        mock_task.branch_name = "feature/task-123"

        mock_workflow = AsyncMock()
        mock_workflow.start_task = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            response = await manage(
                action="start_task",
                entity_id="task_123",
                data={"assignee": "bliss"},
                organization_id="org_123",
            )
            assert response.success is True
            assert response.message == "Task started"
            assert response.data["status"] == "doing"
            mock_workflow.start_task.assert_called_once_with("task_123", "bliss")

    @pytest.mark.asyncio
    async def test_start_task_default_assignee(self) -> None:
        """start_task uses 'system' as default assignee."""
        mock_client = AsyncMock()
        mock_task = MagicMock()
        mock_task.status = TaskStatus.DOING
        mock_task.branch_name = None

        mock_workflow = AsyncMock()
        mock_workflow.start_task = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            await manage(
                action="start_task",
                entity_id="task_123",
                organization_id="org_123",
            )
            mock_workflow.start_task.assert_called_once_with("task_123", "system")

    @pytest.mark.asyncio
    async def test_block_task_success(self) -> None:
        """block_task marks task as blocked with reason."""
        mock_client = AsyncMock()
        mock_task = MagicMock()
        mock_task.status = TaskStatus.BLOCKED

        mock_workflow = AsyncMock()
        mock_workflow.block_task = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            response = await manage(
                action="block_task",
                entity_id="task_123",
                data={"reason": "Waiting for API access"},
                organization_id="org_123",
            )
            assert response.success is True
            assert "blocked" in response.message.lower()
            assert response.data["reason"] == "Waiting for API access"
            mock_workflow.block_task.assert_called_once_with("task_123", "Waiting for API access")

    @pytest.mark.asyncio
    async def test_block_task_default_reason(self) -> None:
        """block_task uses default reason when not provided."""
        mock_client = AsyncMock()
        mock_task = MagicMock()
        mock_task.status = TaskStatus.BLOCKED

        mock_workflow = AsyncMock()
        mock_workflow.block_task = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            await manage(
                action="block_task",
                entity_id="task_123",
                organization_id="org_123",
            )
            mock_workflow.block_task.assert_called_once_with("task_123", "No reason provided")

    @pytest.mark.asyncio
    async def test_unblock_task_success(self) -> None:
        """unblock_task removes blocked status."""
        mock_client = AsyncMock()
        mock_task = MagicMock()
        mock_task.status = TaskStatus.DOING

        mock_workflow = AsyncMock()
        mock_workflow.unblock_task = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            response = await manage(
                action="unblock_task",
                entity_id="task_123",
                organization_id="org_123",
            )
            assert response.success is True
            assert "unblocked" in response.message.lower()

    @pytest.mark.asyncio
    async def test_submit_review_success(self) -> None:
        """submit_review moves task to review status."""
        mock_client = AsyncMock()
        mock_task = MagicMock()
        mock_task.status = TaskStatus.REVIEW
        mock_task.pr_url = "https://github.com/org/repo/pull/42"

        mock_workflow = AsyncMock()
        mock_workflow.submit_for_review = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            response = await manage(
                action="submit_review",
                entity_id="task_123",
                data={
                    "commit_shas": ["abc123", "def456"],
                    "pr_url": "https://github.com/org/repo/pull/42",
                },
                organization_id="org_123",
            )
            assert response.success is True
            assert "review" in response.message.lower()
            assert response.data["pr_url"] == "https://github.com/org/repo/pull/42"

    @pytest.mark.asyncio
    async def test_complete_task_success(self) -> None:
        """complete_task marks task as done with learnings."""
        mock_client = AsyncMock()
        mock_task = MagicMock()
        mock_task.status = TaskStatus.DONE

        mock_workflow = AsyncMock()
        mock_workflow.complete_task = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            response = await manage(
                action="complete_task",
                entity_id="task_123",
                data={
                    "learnings": "Discovered a better approach using async iterators",
                    "actual_hours": 4.5,
                },
                organization_id="org_123",
            )
            assert response.success is True
            assert "completed" in response.message.lower()
            assert "learnings captured" in response.message
            assert (
                response.data["learnings"] == "Discovered a better approach using async iterators"
            )

    @pytest.mark.asyncio
    async def test_complete_task_without_learnings(self) -> None:
        """complete_task works without learnings."""
        mock_client = AsyncMock()
        mock_task = MagicMock()
        mock_task.status = TaskStatus.DONE

        mock_workflow = AsyncMock()
        mock_workflow.complete_task = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            response = await manage(
                action="complete_task",
                entity_id="task_123",
                organization_id="org_123",
            )
            assert response.success is True
            assert "learnings captured" not in response.message

    @pytest.mark.asyncio
    async def test_archive_task_success(self) -> None:
        """archive_task archives task."""
        mock_client = AsyncMock()
        mock_task = MagicMock()
        mock_task.status = TaskStatus.ARCHIVED

        mock_workflow = AsyncMock()
        mock_workflow.archive_task = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            response = await manage(
                action="archive_task",
                entity_id="task_123",
                data={"reason": "Duplicate of task_456"},
                organization_id="org_123",
            )
            assert response.success is True
            assert "archived" in response.message.lower()

    @pytest.mark.asyncio
    async def test_invalid_transition_error(self) -> None:
        """InvalidTransitionError is handled gracefully."""
        mock_client = AsyncMock()
        mock_workflow = AsyncMock()
        mock_workflow.start_task = AsyncMock(
            side_effect=InvalidTransitionError(
                from_status="done",
                to_status="doing",
                allowed=["archived"],
            )
        )

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            response = await manage(
                action="start_task",
                entity_id="task_123",
                organization_id="org_123",
            )
            assert response.success is False
            assert "Invalid transition" in response.message
            assert response.data["from_status"] == "done"
            assert response.data["to_status"] == "doing"


# =============================================================================
# Update Task Tests
# =============================================================================


class TestUpdateTask:
    """Test update_task action."""

    @pytest.mark.asyncio
    async def test_update_task_success(self) -> None:
        """update_task updates allowed fields."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.update = AsyncMock(return_value=make_entity())

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch("sibyl_core.tasks.workflow.TaskWorkflowEngine"),
        ):
            response = await manage(
                action="update_task",
                entity_id="task_123",
                data={
                    "title": "Updated Title",
                    "priority": "high",
                    "description": "New description",
                },
                organization_id="org_123",
            )
            assert response.success is True
            assert "updated" in response.message.lower()
            assert "title" in response.data["updated_fields"]
            assert "priority" in response.data["updated_fields"]

    @pytest.mark.asyncio
    async def test_update_task_filters_invalid_fields(self) -> None:
        """update_task ignores fields not in allowed set."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.update = AsyncMock(return_value=make_entity())

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch("sibyl_core.tasks.workflow.TaskWorkflowEngine"),
        ):
            response = await manage(
                action="update_task",
                entity_id="task_123",
                data={
                    "title": "Valid Update",
                    "invalid_field": "Should be ignored",
                    "organization_id": "Should also be ignored",
                },
                organization_id="org_123",
            )
            assert response.success is True
            assert "title" in response.data["updated_fields"]
            assert "invalid_field" not in response.data["updated_fields"]

    @pytest.mark.asyncio
    async def test_update_task_no_valid_fields(self) -> None:
        """update_task returns error when no valid fields provided."""
        mock_client = AsyncMock()

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch("sibyl_core.tasks.workflow.TaskWorkflowEngine"),
        ):
            response = await manage(
                action="update_task",
                entity_id="task_123",
                data={"invalid_field": "No valid fields here"},
                organization_id="org_123",
            )
            assert response.success is False
            assert "No valid fields" in response.message

    @pytest.mark.asyncio
    async def test_update_task_failed(self) -> None:
        """update_task returns error when update fails."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.update = AsyncMock(return_value=None)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch("sibyl_core.tasks.workflow.TaskWorkflowEngine"),
        ):
            response = await manage(
                action="update_task",
                entity_id="task_123",
                data={"title": "New Title"},
                organization_id="org_123",
            )
            assert response.success is False
            assert "Failed to update" in response.message


# =============================================================================
# Add Note Tests
# =============================================================================


class TestAddNote:
    """Test add_note action."""

    @pytest.mark.asyncio
    async def test_add_note_success(self) -> None:
        """add_note creates a note linked to task."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.get = AsyncMock(return_value=make_entity(entity_type=EntityType.TASK))
        mock_entity_manager.create_direct = AsyncMock(return_value="note_123")

        mock_rel_manager = AsyncMock()
        mock_rel_manager.create = AsyncMock(return_value="rel_123")

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
            patch(
                "sibyl_core.tools.manage.RelationshipManager",
                return_value=mock_rel_manager,
            ),
            patch("sibyl_core.tasks.workflow.TaskWorkflowEngine"),
        ):
            response = await manage(
                action="add_note",
                entity_id="task_123",
                data={
                    "content": "This is a note about the implementation",
                    "author_type": "agent",
                    "author_name": "claude",
                },
                organization_id="org_123",
            )
            assert response.success is True
            assert "Note added" in response.message
            assert response.data["task_id"] == "task_123"
            assert response.data["author_type"] == "agent"

    @pytest.mark.asyncio
    async def test_add_note_requires_content(self) -> None:
        """add_note requires content."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.get = AsyncMock(return_value=make_entity(entity_type=EntityType.TASK))

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch("sibyl_core.tasks.workflow.TaskWorkflowEngine"),
        ):
            response = await manage(
                action="add_note",
                entity_id="task_123",
                data={},  # No content
                organization_id="org_123",
            )
            assert response.success is False
            assert "content required" in response.message

    @pytest.mark.asyncio
    async def test_add_note_task_not_found(self) -> None:
        """add_note returns error when task not found."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.get = AsyncMock(side_effect=EntityNotFoundError("Task", "task_123"))

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch("sibyl_core.tasks.workflow.TaskWorkflowEngine"),
        ):
            response = await manage(
                action="add_note",
                entity_id="task_123",
                data={"content": "Note content"},
                organization_id="org_123",
            )
            assert response.success is False
            assert "not found" in response.message

    @pytest.mark.asyncio
    async def test_add_note_default_author_type(self) -> None:
        """add_note defaults author_type to user."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.get = AsyncMock(return_value=make_entity(entity_type=EntityType.TASK))
        mock_entity_manager.create_direct = AsyncMock(return_value="note_123")

        mock_rel_manager = AsyncMock()
        mock_rel_manager.create = AsyncMock(return_value="rel_123")

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
            patch(
                "sibyl_core.tools.manage.RelationshipManager",
                return_value=mock_rel_manager,
            ),
            patch("sibyl_core.tasks.workflow.TaskWorkflowEngine"),
        ):
            response = await manage(
                action="add_note",
                entity_id="task_123",
                data={"content": "Note without author_type"},
                organization_id="org_123",
            )
            assert response.success is True
            assert response.data["author_type"] == "user"

    @pytest.mark.asyncio
    async def test_add_note_invalid_author_type_fallback(self) -> None:
        """add_note falls back to user for invalid author_type."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.get = AsyncMock(return_value=make_entity(entity_type=EntityType.TASK))
        mock_entity_manager.create_direct = AsyncMock(return_value="note_123")

        mock_rel_manager = AsyncMock()
        mock_rel_manager.create = AsyncMock(return_value="rel_123")

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
            patch(
                "sibyl_core.tools.manage.RelationshipManager",
                return_value=mock_rel_manager,
            ),
            patch("sibyl_core.tasks.workflow.TaskWorkflowEngine"),
        ):
            response = await manage(
                action="add_note",
                entity_id="task_123",
                data={"content": "Note", "author_type": "invalid_type"},
                organization_id="org_123",
            )
            assert response.success is True
            assert response.data["author_type"] == "user"


# =============================================================================
# Epic Action Tests
# =============================================================================


class TestEpicActions:
    """Test epic workflow actions."""

    @pytest.mark.asyncio
    async def test_start_epic_success(self) -> None:
        """start_epic moves epic to in_progress status."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_epic = make_entity(entity_type=EntityType.EPIC)
        mock_entity_manager.get = AsyncMock(return_value=mock_epic)
        mock_entity_manager.update = AsyncMock(return_value=mock_epic)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            response = await manage(
                action="start_epic",
                entity_id="epic_123",
                organization_id="org_123",
            )
            assert response.success is True
            assert "started" in response.message.lower()
            assert response.data["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_complete_epic_success(self) -> None:
        """complete_epic marks epic as completed."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_epic = make_entity(entity_type=EntityType.EPIC)
        mock_entity_manager.get = AsyncMock(return_value=mock_epic)
        mock_entity_manager.update = AsyncMock(return_value=mock_epic)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            response = await manage(
                action="complete_epic",
                entity_id="epic_123",
                data={"learnings": "Epic-level insights about the project"},
                organization_id="org_123",
            )
            assert response.success is True
            assert "completed" in response.message.lower()
            assert "learnings captured" in response.message

    @pytest.mark.asyncio
    async def test_archive_epic_success(self) -> None:
        """archive_epic archives the epic."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_epic = make_entity(entity_type=EntityType.EPIC)
        mock_entity_manager.get = AsyncMock(return_value=mock_epic)
        mock_entity_manager.update = AsyncMock(return_value=mock_epic)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            response = await manage(
                action="archive_epic",
                entity_id="epic_123",
                data={"reason": "Superseded by new epic"},
                organization_id="org_123",
            )
            assert response.success is True
            assert "archived" in response.message.lower()
            assert "Superseded" in response.message

    @pytest.mark.asyncio
    async def test_epic_not_found(self) -> None:
        """Epic action returns error when epic not found."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_entity_manager.get = AsyncMock(return_value=None)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            response = await manage(
                action="start_epic",
                entity_id="epic_123",
                organization_id="org_123",
            )
            assert response.success is False
            assert "not found" in response.message.lower()

    @pytest.mark.asyncio
    async def test_entity_not_epic_error(self) -> None:
        """Epic action returns error when entity is not an epic."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        # Return a task instead of an epic
        mock_task = make_entity(entity_type=EntityType.TASK)
        mock_entity_manager.get = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            response = await manage(
                action="start_epic",
                entity_id="task_123",  # This is actually a task
                organization_id="org_123",
            )
            assert response.success is False
            assert "not an epic" in response.message


# =============================================================================
# Update Epic Tests
# =============================================================================


class TestUpdateEpic:
    """Test update_epic action."""

    @pytest.mark.asyncio
    async def test_update_epic_success(self) -> None:
        """update_epic updates allowed fields."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_epic = make_entity(entity_type=EntityType.EPIC)
        mock_entity_manager.get = AsyncMock(return_value=mock_epic)
        mock_entity_manager.update = AsyncMock(return_value=mock_epic)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            response = await manage(
                action="update_epic",
                entity_id="epic_123",
                data={
                    "title": "Updated Epic Title",
                    "priority": "high",
                    "tags": ["refactor", "q1"],
                },
                organization_id="org_123",
            )
            assert response.success is True
            assert "updated" in response.message.lower()
            assert "title" in response.data["updated_fields"]

    @pytest.mark.asyncio
    async def test_update_epic_no_valid_fields(self) -> None:
        """update_epic returns error when no valid fields provided."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_epic = make_entity(entity_type=EntityType.EPIC)
        mock_entity_manager.get = AsyncMock(return_value=mock_epic)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
        ):
            response = await manage(
                action="update_epic",
                entity_id="epic_123",
                data={"invalid_field": "Should be filtered out"},
                organization_id="org_123",
            )
            assert response.success is False
            assert "No valid fields" in response.message


# =============================================================================
# Source Action Tests
# =============================================================================


class TestSourceActions:
    """Test source operation actions."""

    @pytest.mark.asyncio
    async def test_crawl_requires_url(self) -> None:
        """crawl action requires data.url."""
        response = await manage(
            action="crawl",
            data={},  # No URL
            organization_id="org_123",
        )
        assert response.success is False
        assert "url required" in response.message

    @pytest.mark.asyncio
    async def test_sync_requires_entity_id(self) -> None:
        """sync action requires entity_id."""
        response = await manage(
            action="sync",
            entity_id=None,
            organization_id="org_123",
        )
        assert response.success is False
        assert "entity_id" in response.message or "source ID" in response.message


# =============================================================================
# Analysis Action Tests
# =============================================================================


class TestAnalysisActions:
    """Test analysis actions."""

    @pytest.mark.asyncio
    async def test_estimate_requires_entity_id(self) -> None:
        """estimate action requires entity_id."""
        response = await manage(
            action="estimate",
            entity_id=None,
            organization_id="org_123",
        )
        assert response.success is False
        assert "entity_id required" in response.message

    @pytest.mark.asyncio
    async def test_prioritize_requires_entity_id(self) -> None:
        """prioritize action requires entity_id."""
        response = await manage(
            action="prioritize",
            entity_id=None,
            organization_id="org_123",
        )
        assert response.success is False
        assert "entity_id required" in response.message

    @pytest.mark.asyncio
    async def test_detect_cycles_returns_result(self) -> None:
        """detect_cycles returns cycle detection result."""
        mock_client = AsyncMock()
        mock_entity_manager = AsyncMock()
        mock_rel_manager = AsyncMock()

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch(
                "sibyl_core.tools.manage.EntityManager",
                return_value=mock_entity_manager,
            ),
            patch(
                "sibyl_core.tools.manage.RelationshipManager",
                return_value=mock_rel_manager,
            ),
        ):
            response = await manage(
                action="detect_cycles",
                entity_id="project_123",
                organization_id="org_123",
            )
            assert response.success is True
            assert "has_cycles" in response.data
            assert response.data["has_cycles"] is False


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestErrorHandling:
    """Test error handling in manage()."""

    @pytest.mark.asyncio
    async def test_exception_returns_error_response(self) -> None:
        """Unhandled exceptions return error response."""
        mock_client = AsyncMock()
        mock_workflow = AsyncMock()
        mock_workflow.start_task = AsyncMock(side_effect=RuntimeError("Unexpected error"))

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            response = await manage(
                action="start_task",
                entity_id="task_123",
                organization_id="org_123",
            )
            assert response.success is False
            assert "Action failed" in response.message
            assert "Unexpected error" in response.message

    @pytest.mark.asyncio
    async def test_null_data_handled(self) -> None:
        """None data is handled gracefully (converted to empty dict)."""
        mock_client = AsyncMock()
        mock_task = MagicMock()
        mock_task.status = TaskStatus.BLOCKED

        mock_workflow = AsyncMock()
        mock_workflow.block_task = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            response = await manage(
                action="block_task",
                entity_id="task_123",
                data=None,  # None data
                organization_id="org_123",
            )
            # Should use default reason
            assert response.success is True
            mock_workflow.block_task.assert_called_once_with("task_123", "No reason provided")


# =============================================================================
# Response Formatting Tests
# =============================================================================


class TestResponseFormatting:
    """Test response formatting."""

    @pytest.mark.asyncio
    async def test_success_response_structure(self) -> None:
        """Success responses have correct structure."""
        mock_client = AsyncMock()
        mock_task = MagicMock()
        mock_task.status = TaskStatus.DOING
        mock_task.branch_name = "feature/test"

        mock_workflow = AsyncMock()
        mock_workflow.start_task = AsyncMock(return_value=mock_task)

        with (
            patch("sibyl_core.tools.manage.get_graph_client", return_value=mock_client),
            patch("sibyl_core.tools.manage.EntityManager"),
            patch("sibyl_core.tools.manage.RelationshipManager"),
            patch(
                "sibyl_core.tasks.workflow.TaskWorkflowEngine",
                return_value=mock_workflow,
            ),
        ):
            response = await manage(
                action="start_task",
                entity_id="task_123",
                organization_id="org_123",
            )
            assert isinstance(response, ManageResponse)
            assert response.success is True
            assert response.action == "start_task"
            assert response.entity_id == "task_123"
            assert isinstance(response.message, str)
            assert isinstance(response.data, dict)
            assert isinstance(response.timestamp, datetime)

    @pytest.mark.asyncio
    async def test_error_response_structure(self) -> None:
        """Error responses have correct structure."""
        response = await manage(
            action="invalid_action",
            organization_id="org_123",
        )
        assert isinstance(response, ManageResponse)
        assert response.success is False
        assert response.action == "invalid_action"
        assert len(response.message) > 0
        assert isinstance(response.timestamp, datetime)

    @pytest.mark.asyncio
    async def test_timestamp_is_utc(self) -> None:
        """Response timestamps are in UTC."""
        response = await manage(
            action="unknown_action",
            organization_id="org_123",
        )
        assert response.timestamp.tzinfo == UTC
