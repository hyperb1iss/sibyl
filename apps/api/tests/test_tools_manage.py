"""Tests for the manage() tool."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.models.tasks import TaskStatus
from sibyl.tools.manage import (
    ALL_ACTIONS,
    ANALYSIS_ACTIONS,
    EPIC_ACTIONS,
    SOURCE_ACTIONS,
    TASK_ACTIONS,
    ManageResponse,
    _update_task,
    manage,
)

# Test organization ID for non-admin actions
TEST_ORG_ID = "test-org-12345"


class TestManageResponse:
    """Tests for ManageResponse dataclass."""

    def test_basic_response(self) -> None:
        """Test creating a basic response."""
        response = ManageResponse(
            success=True,
            action="test_action",
            entity_id="ent-123",
            message="Test message",
        )
        assert response.success is True
        assert response.action == "test_action"
        assert response.entity_id == "ent-123"
        assert response.message == "Test message"
        assert response.data == {}
        assert isinstance(response.timestamp, datetime)

    def test_response_with_data(self) -> None:
        """Test response with custom data."""
        response = ManageResponse(
            success=True,
            action="test",
            data={"key": "value", "count": 42},
        )
        assert response.data == {"key": "value", "count": 42}

    def test_response_failure(self) -> None:
        """Test failure response."""
        response = ManageResponse(
            success=False,
            action="failed_action",
            message="Something went wrong",
        )
        assert response.success is False
        assert response.entity_id is None


class TestActionCategories:
    """Tests for action category constants."""

    def test_task_actions_defined(self) -> None:
        """Verify all task actions are defined."""
        expected = {
            "start_task",
            "block_task",
            "unblock_task",
            "submit_review",
            "complete_task",
            "archive_task",
            "update_task",
        }
        assert expected == TASK_ACTIONS

    def test_source_actions_defined(self) -> None:
        """Verify all source actions are defined."""
        expected = {"crawl", "sync", "refresh", "link_graph", "link_graph_status"}
        assert expected == SOURCE_ACTIONS

    def test_epic_actions_defined(self) -> None:
        """Verify all epic actions are defined."""
        expected = {"start_epic", "complete_epic", "archive_epic", "update_epic"}
        assert expected == EPIC_ACTIONS

    def test_analysis_actions_defined(self) -> None:
        """Verify all analysis actions are defined."""
        expected = {"estimate", "prioritize", "detect_cycles", "suggest"}
        assert expected == ANALYSIS_ACTIONS

    def test_all_actions_combined(self) -> None:
        """Verify ALL_ACTIONS includes all categories."""
        assert ALL_ACTIONS == (
            TASK_ACTIONS | EPIC_ACTIONS | SOURCE_ACTIONS | ANALYSIS_ACTIONS
        )

    def test_no_duplicate_actions(self) -> None:
        """Verify no action appears in multiple categories."""
        all_lists = [TASK_ACTIONS, EPIC_ACTIONS, SOURCE_ACTIONS, ANALYSIS_ACTIONS]
        seen = set()
        for action_set in all_lists:
            for action in action_set:
                assert action not in seen, f"Duplicate action: {action}"
                seen.add(action)


class TestManageUnknownAction:
    """Tests for unknown action handling."""

    @pytest.mark.asyncio
    async def test_unknown_action_returns_failure(self) -> None:
        """Test that unknown actions return failure response."""
        result = await manage(action="unknown_action")
        assert result.success is False
        assert "Unknown action" in result.message
        assert "unknown_action" in result.message

    @pytest.mark.asyncio
    async def test_unknown_action_includes_valid_actions(self) -> None:
        """Test that error message includes valid actions."""
        result = await manage(action="not_a_real_action")
        # Should mention at least some valid actions
        assert "start_task" in result.message or "Valid actions" in result.message


class TestManageTaskActionsValidation:
    """Tests for task action input validation."""

    @pytest.mark.asyncio
    async def test_start_task_requires_entity_id(self) -> None:
        """Test that start_task requires entity_id."""
        result = await manage(action="start_task", organization_id=TEST_ORG_ID)
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_block_task_requires_entity_id(self) -> None:
        """Test that block_task requires entity_id."""
        result = await manage(action="block_task", organization_id=TEST_ORG_ID)
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_complete_task_requires_entity_id(self) -> None:
        """Test that complete_task requires entity_id."""
        result = await manage(action="complete_task", organization_id=TEST_ORG_ID)
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_update_task_requires_entity_id(self) -> None:
        """Test that update_task requires entity_id."""
        result = await manage(
            action="update_task", data={"title": "New Title"}, organization_id=TEST_ORG_ID
        )
        assert result.success is False
        assert "entity_id required" in result.message


class TestManageSourceActionsValidation:
    """Tests for source action input validation."""

    @pytest.mark.asyncio
    async def test_crawl_requires_url(self) -> None:
        """Test that crawl requires data.url."""
        result = await manage(action="crawl", organization_id=TEST_ORG_ID)
        assert result.success is False
        assert "url required" in result.message.lower()

    @pytest.mark.asyncio
    async def test_sync_requires_entity_id(self) -> None:
        """Test that sync requires entity_id."""
        result = await manage(action="sync", organization_id=TEST_ORG_ID)
        assert result.success is False
        assert "entity_id" in result.message.lower()


class TestManageAnalysisActionsValidation:
    """Tests for analysis action input validation."""

    @pytest.mark.asyncio
    async def test_estimate_requires_entity_id(self) -> None:
        """Test that estimate requires entity_id."""
        result = await manage(action="estimate", organization_id=TEST_ORG_ID)
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_prioritize_requires_entity_id(self) -> None:
        """Test that prioritize requires entity_id."""
        result = await manage(action="prioritize", organization_id=TEST_ORG_ID)
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_detect_cycles_requires_entity_id(self) -> None:
        """Test that detect_cycles requires entity_id."""
        result = await manage(action="detect_cycles", organization_id=TEST_ORG_ID)
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_suggest_requires_entity_id(self) -> None:
        """Test that suggest requires entity_id."""
        result = await manage(action="suggest", organization_id=TEST_ORG_ID)
        assert result.success is False
        assert "entity_id required" in result.message


class TestManageActionNormalization:
    """Tests for action string normalization."""

    @pytest.mark.asyncio
    async def test_action_case_insensitive(self) -> None:
        """Test that actions are case-insensitive."""
        # These should all be recognized (even if they fail for other reasons)
        result1 = await manage(action="START_TASK", organization_id=TEST_ORG_ID)
        result2 = await manage(action="Start_Task", organization_id=TEST_ORG_ID)
        result3 = await manage(action="start_task", organization_id=TEST_ORG_ID)

        # All should fail for missing entity_id, not unknown action
        for result in [result1, result2, result3]:
            assert "Unknown action" not in result.message

    @pytest.mark.asyncio
    async def test_action_whitespace_stripped(self) -> None:
        """Test that action whitespace is stripped."""
        result = await manage(action="  start_task  ", organization_id=TEST_ORG_ID)
        # Should recognize the action (fail for entity_id, not unknown action)
        assert "Unknown action" not in result.message


class TestUpdateTask:
    """Tests for _update_task function."""

    @pytest.mark.asyncio
    async def test_update_task_requires_entity_id(self) -> None:
        """_update_task should fail without entity_id."""
        mock_manager = MagicMock()
        result = await _update_task(mock_manager, None, {"title": "New Title"})
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_update_task_filters_allowed_fields(self) -> None:
        """_update_task should filter to allowed fields only."""
        mock_manager = MagicMock()
        mock_manager.update = AsyncMock(return_value=MagicMock())

        result = await _update_task(
            mock_manager,
            "task_123",
            {"title": "New", "invalid_field": "ignored", "priority": "high"},
        )

        assert result.success is True
        # Verify only allowed fields were passed
        call_args = mock_manager.update.call_args
        updates = call_args[0][1]
        assert "title" in updates
        assert "priority" in updates
        assert "invalid_field" not in updates

    @pytest.mark.asyncio
    async def test_update_task_no_valid_fields(self) -> None:
        """_update_task should fail if no valid fields provided."""
        mock_manager = MagicMock()
        result = await _update_task(
            mock_manager, "task_123", {"invalid_field": "value", "another_invalid": 123}
        )
        assert result.success is False
        assert "No valid fields" in result.message

    @pytest.mark.asyncio
    async def test_update_task_sync_mode(self) -> None:
        """_update_task should update directly in sync mode (default)."""
        mock_manager = MagicMock()
        mock_manager.update = AsyncMock(
            return_value=MagicMock(id="task_123", title="Updated Title")
        )

        result = await _update_task(
            mock_manager, "task_123", {"title": "Updated Title", "sync": True}
        )

        assert result.success is True
        assert "Task updated" in result.message
        mock_manager.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_task_async_mode_requires_org_id(self) -> None:
        """_update_task in async mode requires organization_id."""
        mock_manager = MagicMock()
        result = await _update_task(mock_manager, "task_123", {"title": "New", "sync": False})
        assert result.success is False
        assert "organization_id required" in result.message

    @pytest.mark.asyncio
    async def test_update_task_update_fails(self) -> None:
        """_update_task should handle update failure."""
        mock_manager = MagicMock()
        mock_manager.update = AsyncMock(return_value=None)

        result = await _update_task(mock_manager, "task_123", {"title": "New"})

        assert result.success is False
        assert "Failed to update" in result.message


class TestTaskWorkflowHandlers:
    """Tests for task workflow action handlers with mocked workflow engine."""

    @pytest.mark.asyncio
    async def test_start_task_success(self) -> None:
        """start_task should call workflow.start_task."""
        mock_task = MagicMock()
        mock_task.status = TaskStatus.DOING
        mock_task.branch_name = "feature/test-task"

        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager"),
            patch("sibyl.tools.manage.RelationshipManager"),
            patch("sibyl.tasks.workflow.TaskWorkflowEngine") as mock_workflow,
        ):
            mock_client.return_value = MagicMock()
            mock_engine = MagicMock()
            mock_engine.start_task = AsyncMock(return_value=mock_task)
            mock_workflow.return_value = mock_engine

            result = await manage(
                action="start_task",
                entity_id="task_123",
                data={"assignee": "alice"},
                organization_id=TEST_ORG_ID,
            )

            assert result.success is True
            assert result.data["status"] == "doing"
            assert "branch_name" in result.data
            mock_engine.start_task.assert_called_once_with("task_123", "alice")

    @pytest.mark.asyncio
    async def test_block_task_success(self) -> None:
        """block_task should call workflow.block_task with reason."""
        mock_task = MagicMock()
        mock_task.status = TaskStatus.BLOCKED

        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager"),
            patch("sibyl.tools.manage.RelationshipManager"),
            patch("sibyl.tasks.workflow.TaskWorkflowEngine") as mock_workflow,
        ):
            mock_client.return_value = MagicMock()
            mock_engine = MagicMock()
            mock_engine.block_task = AsyncMock(return_value=mock_task)
            mock_workflow.return_value = mock_engine

            result = await manage(
                action="block_task",
                entity_id="task_123",
                data={"reason": "Waiting on API keys"},
                organization_id=TEST_ORG_ID,
            )

            assert result.success is True
            assert "blocked" in result.message.lower()
            mock_engine.block_task.assert_called_once_with("task_123", "Waiting on API keys")

    @pytest.mark.asyncio
    async def test_complete_task_with_learnings(self) -> None:
        """complete_task should capture learnings."""
        mock_task = MagicMock()
        mock_task.status = TaskStatus.DONE

        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager"),
            patch("sibyl.tools.manage.RelationshipManager"),
            patch("sibyl.tasks.workflow.TaskWorkflowEngine") as mock_workflow,
        ):
            mock_client.return_value = MagicMock()
            mock_engine = MagicMock()
            mock_engine.complete_task = AsyncMock(return_value=mock_task)
            mock_workflow.return_value = mock_engine

            result = await manage(
                action="complete_task",
                entity_id="task_123",
                data={
                    "learnings": "OAuth tokens expire after 1 hour",
                    "actual_hours": 4.5,
                },
                organization_id=TEST_ORG_ID,
            )

            assert result.success is True
            assert "learnings captured" in result.message
            mock_engine.complete_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_transition_error(self) -> None:
        """Task workflow should handle InvalidTransitionError gracefully."""
        from sibyl.errors import InvalidTransitionError

        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager"),
            patch("sibyl.tools.manage.RelationshipManager"),
            patch("sibyl.tasks.workflow.TaskWorkflowEngine") as mock_workflow,
        ):
            mock_client.return_value = MagicMock()
            mock_engine = MagicMock()
            mock_engine.start_task = AsyncMock(
                side_effect=InvalidTransitionError(
                    from_status="done",
                    to_status="doing",
                )
            )
            mock_workflow.return_value = mock_engine

            result = await manage(
                action="start_task",
                entity_id="task_123",
                organization_id=TEST_ORG_ID,
            )

            assert result.success is False
            assert "done" in result.message or "transition" in result.message.lower()


class TestSourceActions:
    """Tests for source action handlers."""

    @pytest.mark.asyncio
    async def test_crawl_creates_source(self) -> None:
        """crawl action should create a source entity."""
        with patch("sibyl.tools.manage.get_graph_client") as mock_client:
            mock_client.return_value = MagicMock()
            with patch("sibyl.tools.manage.EntityManager") as mock_manager_class:
                mock_manager = MagicMock()
                mock_manager.create = AsyncMock(return_value="source_abc123")
                mock_manager_class.return_value = mock_manager

                result = await manage(
                    action="crawl",
                    data={"url": "https://docs.example.com", "depth": 3},
                    organization_id=TEST_ORG_ID,
                )

                assert result.success is True
                assert "queued" in result.message.lower()
                assert result.data["url"] == "https://docs.example.com"
                mock_manager.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_source_not_found(self) -> None:
        """sync action should fail gracefully if source not found."""
        with patch("sibyl.tools.manage.get_graph_client") as mock_client:
            mock_client.return_value = MagicMock()
            with patch("sibyl.tools.manage.EntityManager") as mock_manager_class:
                mock_manager = MagicMock()
                mock_manager.get = AsyncMock(side_effect=Exception("Not found"))
                mock_manager_class.return_value = mock_manager

                result = await manage(
                    action="sync",
                    entity_id="source_nonexistent",
                    organization_id=TEST_ORG_ID,
                )

                assert result.success is False
                assert "not found" in result.message.lower()

    @pytest.mark.asyncio
    async def test_sync_source_success(self) -> None:
        """sync action should update source status to pending."""
        with patch("sibyl.tools.manage.get_graph_client") as mock_client:
            mock_client.return_value = MagicMock()
            with patch("sibyl.tools.manage.EntityManager") as mock_manager_class:
                mock_manager = MagicMock()
                mock_manager.get = AsyncMock(return_value=MagicMock(id="source_123"))
                mock_manager.update = AsyncMock(return_value=MagicMock())
                mock_manager_class.return_value = mock_manager

                result = await manage(
                    action="sync",
                    entity_id="source_123",
                    organization_id=TEST_ORG_ID,
                )

                assert result.success is True
                assert "queued" in result.message.lower()
                mock_manager.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_all_sources(self) -> None:
        """refresh action should queue updates for all sources."""
        mock_sources = [
            MagicMock(id="source_1"),
            MagicMock(id="source_2"),
            MagicMock(id="source_3"),
        ]

        with patch("sibyl.tools.manage.get_graph_client") as mock_client:
            mock_client.return_value = MagicMock()
            with patch("sibyl.tools.manage.EntityManager") as mock_manager_class:
                mock_manager = MagicMock()
                mock_manager.list_by_type = AsyncMock(return_value=mock_sources)
                mock_manager.update = AsyncMock(return_value=MagicMock())
                mock_manager_class.return_value = mock_manager

                result = await manage(action="refresh", organization_id=TEST_ORG_ID)

                assert result.success is True
                assert result.data["sources_queued"] == 3
                assert mock_manager.update.call_count == 3


class TestAnalysisActions:
    """Tests for analysis action handlers."""

    @pytest.mark.asyncio
    async def test_prioritize_empty_project(self) -> None:
        """prioritize action should handle empty project."""
        with patch("sibyl.tools.manage.get_graph_client") as mock_client:
            mock_client.return_value = MagicMock()
            with patch("sibyl.tools.manage.EntityManager") as mock_manager_class:
                mock_manager = MagicMock()
                mock_manager.list_by_type = AsyncMock(return_value=[])
                mock_manager_class.return_value = mock_manager

                with patch("sibyl.tools.manage.RelationshipManager"):
                    result = await manage(
                        action="prioritize",
                        entity_id="proj_123",
                        organization_id=TEST_ORG_ID,
                    )

                    assert result.success is True
                    assert "No tasks" in result.message or result.data["tasks"] == []

    @pytest.mark.asyncio
    async def test_prioritize_sorts_by_priority(self) -> None:
        """prioritize action should sort tasks by priority."""
        mock_tasks = [
            MagicMock(
                id="t1", name="Low Task", metadata={"priority": "low", "project_id": "proj_123"}
            ),
            MagicMock(
                id="t2",
                name="High Task",
                metadata={"priority": "high", "project_id": "proj_123"},
            ),
            MagicMock(
                id="t3",
                name="Critical Task",
                metadata={"priority": "critical", "project_id": "proj_123"},
            ),
        ]

        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager") as mock_manager_class,
            patch("sibyl.tools.manage.RelationshipManager"),
        ):
            mock_client.return_value = MagicMock()
            mock_manager = MagicMock()
            mock_manager.list_by_type = AsyncMock(return_value=mock_tasks)
            mock_manager_class.return_value = mock_manager

            result = await manage(
                action="prioritize",
                entity_id="proj_123",
                organization_id=TEST_ORG_ID,
            )

            assert result.success is True
            tasks = result.data["tasks"]
            assert len(tasks) == 3
            # Critical should be first
            assert tasks[0]["priority"] == "critical"
            assert tasks[1]["priority"] == "high"
            assert tasks[2]["priority"] == "low"

    @pytest.mark.asyncio
    async def test_detect_cycles_returns_no_cycles(self) -> None:
        """detect_cycles action should return empty cycles (placeholder impl)."""
        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager"),
            patch("sibyl.tools.manage.RelationshipManager"),
        ):
            mock_client.return_value = MagicMock()

            result = await manage(
                action="detect_cycles",
                entity_id="proj_123",
                organization_id=TEST_ORG_ID,
            )

            assert result.success is True
            assert result.data["has_cycles"] is False
            assert result.data["cycles"] == []


class TestManageOrganizationRequired:
    """Tests for organization_id requirement."""

    @pytest.mark.asyncio
    async def test_task_action_requires_org_id(self) -> None:
        """Task actions should require organization_id."""
        result = await manage(action="start_task", entity_id="task_123")
        assert result.success is False
        assert "organization_id required" in result.message

    @pytest.mark.asyncio
    async def test_source_action_requires_org_id(self) -> None:
        """Source actions should require organization_id."""
        result = await manage(action="crawl", data={"url": "https://example.com"})
        assert result.success is False
        assert "organization_id required" in result.message

    @pytest.mark.asyncio
    async def test_analysis_action_requires_org_id(self) -> None:
        """Analysis actions should require organization_id."""
        result = await manage(action="estimate", entity_id="task_123")
        assert result.success is False
        assert "organization_id required" in result.message


class TestEpicActions:
    """Tests for epic action handlers."""

    @pytest.mark.asyncio
    async def test_epic_action_requires_entity_id(self) -> None:
        """Epic actions should require entity_id (except update_epic)."""
        result = await manage(action="start_epic", organization_id=TEST_ORG_ID)
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_epic_action_requires_org_id(self) -> None:
        """Epic actions should require organization_id."""
        result = await manage(action="start_epic", entity_id="epic_123")
        assert result.success is False
        assert "organization_id required" in result.message

    @pytest.mark.asyncio
    async def test_start_epic_success(self) -> None:
        """start_epic should update status to in_progress."""
        from sibyl.models.entities import EntityType

        mock_epic = MagicMock()
        mock_epic.entity_type = EntityType.EPIC

        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager") as mock_manager_class,
        ):
            mock_client.return_value = MagicMock()
            mock_manager = MagicMock()
            mock_manager.get = AsyncMock(return_value=mock_epic)
            mock_manager.update = AsyncMock(return_value=MagicMock())
            mock_manager_class.return_value = mock_manager

            result = await manage(
                action="start_epic",
                entity_id="epic_123",
                organization_id=TEST_ORG_ID,
            )

            assert result.success is True
            assert result.data["status"] == "in_progress"
            assert "started" in result.message.lower()
            mock_manager.update.assert_called_once_with("epic_123", {"status": "in_progress"})

    @pytest.mark.asyncio
    async def test_complete_epic_with_learnings(self) -> None:
        """complete_epic should capture learnings."""
        from sibyl.models.entities import EntityType

        mock_epic = MagicMock()
        mock_epic.entity_type = EntityType.EPIC

        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager") as mock_manager_class,
        ):
            mock_client.return_value = MagicMock()
            mock_manager = MagicMock()
            mock_manager.get = AsyncMock(return_value=mock_epic)
            mock_manager.update = AsyncMock(return_value=MagicMock())
            mock_manager_class.return_value = mock_manager

            result = await manage(
                action="complete_epic",
                entity_id="epic_123",
                data={"learnings": "OAuth redirect URIs matter"},
                organization_id=TEST_ORG_ID,
            )

            assert result.success is True
            assert "learnings captured" in result.message
            assert result.data["status"] == "completed"
            # Verify update was called with learnings
            call_args = mock_manager.update.call_args[0][1]
            assert call_args["status"] == "completed"
            assert call_args["learnings"] == "OAuth redirect URIs matter"

    @pytest.mark.asyncio
    async def test_archive_epic_with_reason(self) -> None:
        """archive_epic should archive with optional reason."""
        from sibyl.models.entities import EntityType

        mock_epic = MagicMock()
        mock_epic.entity_type = EntityType.EPIC

        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager") as mock_manager_class,
        ):
            mock_client.return_value = MagicMock()
            mock_manager = MagicMock()
            mock_manager.get = AsyncMock(return_value=mock_epic)
            mock_manager.update = AsyncMock(return_value=MagicMock())
            mock_manager_class.return_value = mock_manager

            result = await manage(
                action="archive_epic",
                entity_id="epic_123",
                data={"reason": "Superseded by new architecture"},
                organization_id=TEST_ORG_ID,
            )

            assert result.success is True
            assert result.data["status"] == "archived"
            assert "Superseded" in result.message

    @pytest.mark.asyncio
    async def test_update_epic_filters_allowed_fields(self) -> None:
        """update_epic should only allow specific fields."""
        from sibyl.models.entities import EntityType

        mock_epic = MagicMock()
        mock_epic.entity_type = EntityType.EPIC

        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager") as mock_manager_class,
        ):
            mock_client.return_value = MagicMock()
            mock_manager = MagicMock()
            mock_manager.get = AsyncMock(return_value=mock_epic)
            mock_manager.update = AsyncMock(return_value=MagicMock())
            mock_manager_class.return_value = mock_manager

            result = await manage(
                action="update_epic",
                entity_id="epic_123",
                data={
                    "title": "New Title",
                    "invalid_field": "ignored",
                    "priority": "high",
                },
                organization_id=TEST_ORG_ID,
            )

            assert result.success is True
            # Verify only allowed fields were passed
            call_args = mock_manager.update.call_args[0][1]
            assert "title" in call_args
            assert "priority" in call_args
            assert "invalid_field" not in call_args

    @pytest.mark.asyncio
    async def test_epic_not_found(self) -> None:
        """Epic action should fail if epic not found."""
        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager") as mock_manager_class,
        ):
            mock_client.return_value = MagicMock()
            mock_manager = MagicMock()
            mock_manager.get = AsyncMock(return_value=None)
            mock_manager_class.return_value = mock_manager

            result = await manage(
                action="start_epic",
                entity_id="epic_nonexistent",
                organization_id=TEST_ORG_ID,
            )

            assert result.success is False
            assert "not found" in result.message.lower()

    @pytest.mark.asyncio
    async def test_entity_not_epic_type(self) -> None:
        """Epic action should fail if entity is not an epic."""
        from sibyl.models.entities import EntityType

        mock_task = MagicMock()
        mock_task.entity_type = EntityType.TASK

        with (
            patch("sibyl.tools.manage.get_graph_client") as mock_client,
            patch("sibyl.tools.manage.EntityManager") as mock_manager_class,
        ):
            mock_client.return_value = MagicMock()
            mock_manager = MagicMock()
            mock_manager.get = AsyncMock(return_value=mock_task)
            mock_manager_class.return_value = mock_manager

            result = await manage(
                action="start_epic",
                entity_id="task_123",
                organization_id=TEST_ORG_ID,
            )

            assert result.success is False
            assert "not an epic" in result.message.lower()
