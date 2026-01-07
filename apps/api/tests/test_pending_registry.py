"""Tests for pending entity registry.

Tests the Redis-backed pending entity tracking and operation queueing
that enables operations on not-yet-materialized async entities.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestMarkPending:
    """Tests for mark_pending()."""

    @pytest.mark.asyncio
    async def test_mark_pending_sets_redis_key(self) -> None:
        """mark_pending should store entity info in Redis with TTL."""
        mock_pool = AsyncMock()

        with patch("sibyl.jobs.pending.get_pool", return_value=mock_pool):
            from sibyl.jobs.pending import mark_pending

            await mark_pending(
                entity_id="task_123",
                job_id="create_entity:task_123",
                entity_type="task",
                group_id="org_456",
            )

        # Verify setex was called with correct key and TTL
        mock_pool.setex.assert_called_once()
        call_args = mock_pool.setex.call_args
        assert call_args[0][0] == "sibyl:pending:task_123"
        assert call_args[0][1] == 300  # 5 minutes TTL

        # Verify stored data
        stored_data = json.loads(call_args[0][2])
        assert stored_data["job_id"] == "create_entity:task_123"
        assert stored_data["entity_type"] == "task"
        assert stored_data["group_id"] == "org_456"
        assert "created_at" in stored_data


class TestIsPending:
    """Tests for is_pending()."""

    @pytest.mark.asyncio
    async def test_is_pending_returns_data_when_pending(self) -> None:
        """is_pending should return pending info if entity is pending."""
        mock_pool = AsyncMock()
        mock_pool.get.return_value = json.dumps(
            {
                "job_id": "create_entity:task_123",
                "entity_type": "task",
                "group_id": "org_456",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

        with patch("sibyl.jobs.pending.get_pool", return_value=mock_pool):
            from sibyl.jobs.pending import is_pending

            result = await is_pending("task_123")

        assert result is not None
        assert result["job_id"] == "create_entity:task_123"
        assert result["entity_type"] == "task"

    @pytest.mark.asyncio
    async def test_is_pending_returns_none_when_not_pending(self) -> None:
        """is_pending should return None if entity is not pending."""
        mock_pool = AsyncMock()
        mock_pool.get.return_value = None

        with patch("sibyl.jobs.pending.get_pool", return_value=mock_pool):
            from sibyl.jobs.pending import is_pending

            result = await is_pending("task_123")

        assert result is None


class TestClearPending:
    """Tests for clear_pending()."""

    @pytest.mark.asyncio
    async def test_clear_pending_deletes_key(self) -> None:
        """clear_pending should delete the pending key from Redis."""
        mock_pool = AsyncMock()
        mock_pool.delete.return_value = 1

        with patch("sibyl.jobs.pending.get_pool", return_value=mock_pool):
            from sibyl.jobs.pending import clear_pending

            result = await clear_pending("task_123")

        mock_pool.delete.assert_called_once_with("sibyl:pending:task_123")
        assert result is True

    @pytest.mark.asyncio
    async def test_clear_pending_returns_false_when_not_pending(self) -> None:
        """clear_pending should return False if entity wasn't pending."""
        mock_pool = AsyncMock()
        mock_pool.delete.return_value = 0

        with patch("sibyl.jobs.pending.get_pool", return_value=mock_pool):
            from sibyl.jobs.pending import clear_pending

            result = await clear_pending("task_123")

        assert result is False


class TestQueuePendingOperation:
    """Tests for queue_pending_operation()."""

    @pytest.mark.asyncio
    async def test_queue_pending_operation_adds_to_list(self) -> None:
        """queue_pending_operation should add operation to Redis list."""
        mock_pool = AsyncMock()

        with patch("sibyl.jobs.pending.get_pool", return_value=mock_pool):
            from sibyl.jobs.pending import queue_pending_operation

            op_id = await queue_pending_operation(
                entity_id="task_123",
                operation="add_note",
                payload={"content": "Test note", "author_type": "user"},
                user_id="user_789",
            )

        # Verify rpush was called
        mock_pool.rpush.assert_called_once()
        call_args = mock_pool.rpush.call_args
        assert call_args[0][0] == "sibyl:pending_ops:task_123"

        # Verify operation data
        op_data = json.loads(call_args[0][1])
        assert op_data["operation"] == "add_note"
        assert op_data["payload"]["content"] == "Test note"
        assert op_data["user_id"] == "user_789"
        assert op_data["op_id"].startswith("pending_op_")

        # Verify TTL was set
        mock_pool.expire.assert_called_once()

        # Verify op_id returned
        assert op_id.startswith("pending_op_")


class TestGetPendingOperations:
    """Tests for get_pending_operations()."""

    @pytest.mark.asyncio
    async def test_get_pending_operations_returns_list(self) -> None:
        """get_pending_operations should return all queued operations."""
        mock_pool = AsyncMock()
        mock_pool.lrange.return_value = [
            json.dumps({"op_id": "op_1", "operation": "add_note", "payload": {}}),
            json.dumps({"op_id": "op_2", "operation": "update", "payload": {}}),
        ]

        with patch("sibyl.jobs.pending.get_pool", return_value=mock_pool):
            from sibyl.jobs.pending import get_pending_operations

            result = await get_pending_operations("task_123")

        assert len(result) == 2
        assert result[0]["op_id"] == "op_1"
        assert result[1]["op_id"] == "op_2"

    @pytest.mark.asyncio
    async def test_get_pending_operations_returns_empty_list(self) -> None:
        """get_pending_operations should return empty list if no ops queued."""
        mock_pool = AsyncMock()
        mock_pool.lrange.return_value = []

        with patch("sibyl.jobs.pending.get_pool", return_value=mock_pool):
            from sibyl.jobs.pending import get_pending_operations

            result = await get_pending_operations("task_123")

        assert result == []


class TestClearPendingOperations:
    """Tests for clear_pending_operations()."""

    @pytest.mark.asyncio
    async def test_clear_pending_operations_deletes_list(self) -> None:
        """clear_pending_operations should delete the ops list."""
        mock_pool = AsyncMock()
        mock_pool.llen.return_value = 3

        with patch("sibyl.jobs.pending.get_pool", return_value=mock_pool):
            from sibyl.jobs.pending import clear_pending_operations

            result = await clear_pending_operations("task_123")

        mock_pool.delete.assert_called_once_with("sibyl:pending_ops:task_123")
        assert result == 3


class TestProcessPendingOperations:
    """Tests for process_pending_operations()."""

    @pytest.mark.asyncio
    async def test_process_pending_operations_empty_returns_early(self) -> None:
        """process_pending_operations should return empty if no ops."""
        mock_pool = AsyncMock()
        mock_pool.lrange.return_value = []

        with patch("sibyl.jobs.pending.get_pool", return_value=mock_pool):
            from sibyl.jobs.pending import process_pending_operations

            result = await process_pending_operations("task_123", "org_456")

        assert result == []

    @pytest.mark.asyncio
    async def test_process_pending_operations_handles_add_note(self) -> None:
        """process_pending_operations should process add_note operations."""
        mock_pool = AsyncMock()
        mock_pool.lrange.return_value = [
            json.dumps(
                {
                    "op_id": "op_1",
                    "operation": "add_note",
                    "payload": {
                        "note_id": "note_xyz",
                        "content": "Test note",
                        "author_type": "user",
                        "author_name": "Test User",
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                }
            ),
        ]
        mock_pool.llen.return_value = 1  # For clear_pending_operations

        mock_entity_manager = AsyncMock()
        mock_relationship_manager = AsyncMock()
        mock_client = MagicMock()

        with (
            patch("sibyl.jobs.pending.get_pool", return_value=mock_pool),
            patch("sibyl_core.graph.client.get_graph_client", return_value=mock_client),
            patch("sibyl_core.graph.entities.EntityManager", return_value=mock_entity_manager),
            patch(
                "sibyl_core.graph.relationships.RelationshipManager",
                return_value=mock_relationship_manager,
            ),
        ):
            from sibyl.jobs.pending import process_pending_operations

            result = await process_pending_operations("task_123", "org_456")

        assert len(result) == 1
        assert result[0]["op_id"] == "op_1"
        assert result[0]["operation"] == "add_note"
        assert result[0]["success"] is True
        assert result[0]["note_id"] == "note_xyz"

        # Verify note was created
        mock_entity_manager.create_direct.assert_called_once()
        mock_relationship_manager.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_pending_operations_handles_unknown_operation(self) -> None:
        """process_pending_operations should handle unknown operations gracefully."""
        mock_pool = AsyncMock()
        mock_pool.lrange.return_value = [
            json.dumps(
                {
                    "op_id": "op_1",
                    "operation": "unknown_op",
                    "payload": {},
                }
            ),
        ]
        mock_pool.llen.return_value = 1  # For clear_pending_operations

        mock_client = MagicMock()
        mock_entity_manager = AsyncMock()
        mock_relationship_manager = AsyncMock()

        with (
            patch("sibyl.jobs.pending.get_pool", return_value=mock_pool),
            patch("sibyl_core.graph.client.get_graph_client", return_value=mock_client),
            patch("sibyl_core.graph.entities.EntityManager", return_value=mock_entity_manager),
            patch(
                "sibyl_core.graph.relationships.RelationshipManager",
                return_value=mock_relationship_manager,
            ),
        ):
            from sibyl.jobs.pending import process_pending_operations

            result = await process_pending_operations("task_123", "org_456")

        assert len(result) == 1
        assert result[0]["success"] is True  # Unknown ops are logged but don't fail
        assert "error" in result[0]


class TestEnqueueCreateEntityMarksPending:
    """Tests that enqueue_create_entity marks entity as pending."""

    @pytest.mark.asyncio
    async def test_enqueue_create_entity_marks_pending(self) -> None:
        """enqueue_create_entity should call mark_pending after enqueueing."""
        mock_pool = AsyncMock()
        mock_job = MagicMock()
        mock_job.job_id = "create_entity:task_123"
        mock_pool.enqueue_job.return_value = mock_job

        with (
            patch("sibyl.jobs.queue.get_pool", return_value=mock_pool),
            patch("sibyl.jobs.pending.get_pool", return_value=mock_pool),
        ):
            from sibyl.jobs.queue import enqueue_create_entity

            job_id = await enqueue_create_entity(
                entity_id="task_123",
                entity_data={"id": "task_123", "name": "Test Task"},
                entity_type="task",
                group_id="org_456",
            )

        assert job_id == "create_entity:task_123"

        # Verify mark_pending was called (via setex)
        mock_pool.setex.assert_called_once()
        call_args = mock_pool.setex.call_args
        assert call_args[0][0] == "sibyl:pending:task_123"


class TestCreateNoteChecksPending:
    """Tests that create_note checks pending status."""

    @pytest.mark.asyncio
    async def test_create_note_queues_when_task_pending(self) -> None:
        """create_note should queue operation when task is pending."""
        from unittest.mock import patch

        # Mock dependencies
        mock_pool = AsyncMock()
        mock_pool.get.return_value = json.dumps(
            {
                "job_id": "create_entity:task_123",
                "entity_type": "task",
                "group_id": "org_456",
            }
        )

        mock_org = MagicMock()
        mock_org.id = "org_456"

        mock_user = MagicMock()
        mock_user.id = "user_789"

        mock_auth = MagicMock()
        mock_auth.ctx = MagicMock()
        mock_auth.session = AsyncMock()

        # Import and patch
        with (
            patch("sibyl.jobs.pending.get_pool", return_value=mock_pool),
            patch("sibyl.api.routes.tasks._verify_task_access", return_value=None),
            patch("sibyl.api.routes.tasks.broadcast_event", return_value=None),
        ):
            from sibyl.api.routes.tasks import CreateNoteRequest, create_note

            request = CreateNoteRequest(content="Test note")

            result = await create_note(
                task_id="task_123",
                request=request,
                org=mock_org,
                user=mock_user,
                auth=mock_auth,
            )

        # Verify response indicates pending
        assert result.status == "pending"
        assert result.task_id == "task_123"
        assert result.content == "Test note"

        # Verify operation was queued
        mock_pool.rpush.assert_called_once()
