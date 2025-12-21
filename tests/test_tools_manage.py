"""Tests for the manage() tool."""

from datetime import UTC, datetime

import pytest

from sibyl.tools.manage import (
    ADMIN_ACTIONS,
    ALL_ACTIONS,
    ANALYSIS_ACTIONS,
    SOURCE_ACTIONS,
    TASK_ACTIONS,
    ManageResponse,
    manage,
)


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
        assert TASK_ACTIONS == expected

    def test_source_actions_defined(self) -> None:
        """Verify all source actions are defined."""
        expected = {"crawl", "sync", "refresh"}
        assert SOURCE_ACTIONS == expected

    def test_analysis_actions_defined(self) -> None:
        """Verify all analysis actions are defined."""
        expected = {"estimate", "prioritize", "detect_cycles", "suggest"}
        assert ANALYSIS_ACTIONS == expected

    def test_admin_actions_defined(self) -> None:
        """Verify all admin actions are defined."""
        expected = {"health", "stats", "rebuild_index"}
        assert ADMIN_ACTIONS == expected

    def test_all_actions_combined(self) -> None:
        """Verify ALL_ACTIONS includes all categories."""
        assert ALL_ACTIONS == (TASK_ACTIONS | SOURCE_ACTIONS | ANALYSIS_ACTIONS | ADMIN_ACTIONS)

    def test_no_duplicate_actions(self) -> None:
        """Verify no action appears in multiple categories."""
        all_lists = [TASK_ACTIONS, SOURCE_ACTIONS, ANALYSIS_ACTIONS, ADMIN_ACTIONS]
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
        result = await manage(action="start_task")
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_block_task_requires_entity_id(self) -> None:
        """Test that block_task requires entity_id."""
        result = await manage(action="block_task")
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_complete_task_requires_entity_id(self) -> None:
        """Test that complete_task requires entity_id."""
        result = await manage(action="complete_task")
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_update_task_requires_entity_id(self) -> None:
        """Test that update_task requires entity_id."""
        result = await manage(action="update_task", data={"title": "New Title"})
        assert result.success is False
        assert "entity_id required" in result.message


class TestManageSourceActionsValidation:
    """Tests for source action input validation."""

    @pytest.mark.asyncio
    async def test_crawl_requires_url(self) -> None:
        """Test that crawl requires data.url."""
        result = await manage(action="crawl")
        assert result.success is False
        assert "url required" in result.message.lower()

    @pytest.mark.asyncio
    async def test_sync_requires_entity_id(self) -> None:
        """Test that sync requires entity_id."""
        result = await manage(action="sync")
        assert result.success is False
        assert "entity_id" in result.message.lower()


class TestManageAnalysisActionsValidation:
    """Tests for analysis action input validation."""

    @pytest.mark.asyncio
    async def test_estimate_requires_entity_id(self) -> None:
        """Test that estimate requires entity_id."""
        result = await manage(action="estimate")
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_prioritize_requires_entity_id(self) -> None:
        """Test that prioritize requires entity_id."""
        result = await manage(action="prioritize")
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_detect_cycles_requires_entity_id(self) -> None:
        """Test that detect_cycles requires entity_id."""
        result = await manage(action="detect_cycles")
        assert result.success is False
        assert "entity_id required" in result.message

    @pytest.mark.asyncio
    async def test_suggest_requires_entity_id(self) -> None:
        """Test that suggest requires entity_id."""
        result = await manage(action="suggest")
        assert result.success is False
        assert "entity_id required" in result.message


class TestManageActionNormalization:
    """Tests for action string normalization."""

    @pytest.mark.asyncio
    async def test_action_case_insensitive(self) -> None:
        """Test that actions are case-insensitive."""
        # These should all be recognized (even if they fail for other reasons)
        result1 = await manage(action="START_TASK")
        result2 = await manage(action="Start_Task")
        result3 = await manage(action="start_task")

        # All should fail for missing entity_id, not unknown action
        for result in [result1, result2, result3]:
            assert "Unknown action" not in result.message

    @pytest.mark.asyncio
    async def test_action_whitespace_stripped(self) -> None:
        """Test that action whitespace is stripped."""
        result = await manage(action="  start_task  ")
        # Should recognize the action (fail for entity_id, not unknown action)
        assert "Unknown action" not in result.message
