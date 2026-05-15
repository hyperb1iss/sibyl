"""Tests for pending entity registry shims and processing."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_registry() -> MagicMock:
    registry = MagicMock()
    registry.mark_pending = AsyncMock()
    registry.is_pending = AsyncMock()
    registry.clear_pending = AsyncMock()
    registry.queue_pending_operation = AsyncMock()
    registry.get_pending_operations = AsyncMock()
    registry.clear_pending_operations = AsyncMock()
    return registry


class TestMarkPending:
    """Tests for mark_pending()."""

    @pytest.mark.asyncio
    async def test_mark_pending_delegates_to_registry(self, mock_registry: MagicMock) -> None:
        with patch("sibyl.jobs.pending.get_pending", return_value=mock_registry):
            from sibyl.jobs.pending import mark_pending

            await mark_pending(
                entity_id="task_123",
                job_id="create_entity:task_123",
                entity_type="task",
                group_id="org_456",
            )

        mock_registry.mark_pending.assert_awaited_once_with(
            "task_123",
            "create_entity:task_123",
            "task",
            "org_456",
        )


class TestIsPending:
    """Tests for is_pending()."""

    @pytest.mark.asyncio
    async def test_is_pending_returns_data_when_pending(self, mock_registry: MagicMock) -> None:
        mock_registry.is_pending.return_value = {
            "job_id": "create_entity:task_123",
            "entity_type": "task",
            "group_id": "org_456",
            "created_at": datetime.now(UTC).isoformat(),
        }

        with patch("sibyl.jobs.pending.get_pending", return_value=mock_registry):
            from sibyl.jobs.pending import is_pending

            result = await is_pending("task_123")

        assert result is not None
        assert result["job_id"] == "create_entity:task_123"
        assert result["entity_type"] == "task"

    @pytest.mark.asyncio
    async def test_is_pending_returns_none_when_not_pending(self, mock_registry: MagicMock) -> None:
        mock_registry.is_pending.return_value = None

        with patch("sibyl.jobs.pending.get_pending", return_value=mock_registry):
            from sibyl.jobs.pending import is_pending

            result = await is_pending("task_123")

        assert result is None


class TestClearPending:
    """Tests for clear_pending()."""

    @pytest.mark.asyncio
    async def test_clear_pending_returns_true(self, mock_registry: MagicMock) -> None:
        mock_registry.clear_pending.return_value = True

        with patch("sibyl.jobs.pending.get_pending", return_value=mock_registry):
            from sibyl.jobs.pending import clear_pending

            result = await clear_pending("task_123")

        mock_registry.clear_pending.assert_awaited_once_with("task_123")
        assert result is True

    @pytest.mark.asyncio
    async def test_clear_pending_returns_false_when_not_pending(
        self, mock_registry: MagicMock
    ) -> None:
        mock_registry.clear_pending.return_value = False

        with patch("sibyl.jobs.pending.get_pending", return_value=mock_registry):
            from sibyl.jobs.pending import clear_pending

            result = await clear_pending("task_123")

        assert result is False


class TestQueuePendingOperation:
    """Tests for queue_pending_operation()."""

    @pytest.mark.asyncio
    async def test_queue_pending_operation_delegates_to_registry(
        self, mock_registry: MagicMock
    ) -> None:
        mock_registry.queue_pending_operation.return_value = "pending_op_123"

        with patch("sibyl.jobs.pending.get_pending", return_value=mock_registry):
            from sibyl.jobs.pending import queue_pending_operation

            op_id = await queue_pending_operation(
                entity_id="task_123",
                operation="add_note",
                payload={"content": "Test note", "author_type": "user"},
                user_id="user_789",
            )

        mock_registry.queue_pending_operation.assert_awaited_once_with(
            "task_123",
            "add_note",
            {"content": "Test note", "author_type": "user"},
            "user_789",
        )
        assert op_id == "pending_op_123"


class TestGetPendingOperations:
    """Tests for get_pending_operations()."""

    @pytest.mark.asyncio
    async def test_get_pending_operations_returns_list(self, mock_registry: MagicMock) -> None:
        mock_registry.get_pending_operations.return_value = [
            {"op_id": "op_1", "operation": "add_note", "payload": {}},
            {"op_id": "op_2", "operation": "update", "payload": {}},
        ]

        with patch("sibyl.jobs.pending.get_pending", return_value=mock_registry):
            from sibyl.jobs.pending import get_pending_operations

            result = await get_pending_operations("task_123")

        assert len(result) == 2
        assert result[0]["op_id"] == "op_1"
        assert result[1]["op_id"] == "op_2"

    @pytest.mark.asyncio
    async def test_get_pending_operations_returns_empty_list(
        self, mock_registry: MagicMock
    ) -> None:
        mock_registry.get_pending_operations.return_value = []

        with patch("sibyl.jobs.pending.get_pending", return_value=mock_registry):
            from sibyl.jobs.pending import get_pending_operations

            result = await get_pending_operations("task_123")

        assert result == []


class TestClearPendingOperations:
    """Tests for clear_pending_operations()."""

    @pytest.mark.asyncio
    async def test_clear_pending_operations_returns_count(self, mock_registry: MagicMock) -> None:
        mock_registry.clear_pending_operations.return_value = 3

        with patch("sibyl.jobs.pending.get_pending", return_value=mock_registry):
            from sibyl.jobs.pending import clear_pending_operations

            result = await clear_pending_operations("task_123")

        mock_registry.clear_pending_operations.assert_awaited_once_with("task_123")
        assert result == 3


class TestProcessPendingOperations:
    """Tests for process_pending_operations()."""

    @pytest.mark.asyncio
    async def test_process_pending_operations_empty_returns_early(
        self, mock_registry: MagicMock
    ) -> None:
        mock_registry.get_pending_operations.return_value = []

        with patch("sibyl.jobs.pending.get_pending", return_value=mock_registry):
            from sibyl.jobs.pending import process_pending_operations

            result = await process_pending_operations("task_123", "org_456")

        assert result == []
        mock_registry.clear_pending_operations.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_process_pending_operations_handles_add_note(
        self, mock_registry: MagicMock
    ) -> None:
        mock_registry.get_pending_operations.return_value = [
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
            },
        ]
        mock_registry.clear_pending_operations.return_value = 1

        mock_entity_manager = AsyncMock()
        mock_relationship_manager = AsyncMock()
        mock_runtime = MagicMock(
            entity_manager=mock_entity_manager,
            relationship_manager=mock_relationship_manager,
        )

        with (
            patch("sibyl.jobs.pending.get_pending", return_value=mock_registry),
            patch(
                "sibyl_core.services.native_graph.get_native_graph_runtime",
                AsyncMock(return_value=mock_runtime),
            ),
        ):
            from sibyl.jobs.pending import process_pending_operations

            result = await process_pending_operations("task_123", "org_456")

        assert len(result) == 1
        assert result[0]["op_id"] == "op_1"
        assert result[0]["operation"] == "add_note"
        assert result[0]["success"] is True
        assert result[0]["note_id"] == "note_xyz"
        mock_entity_manager.create_direct.assert_awaited_once()
        mock_relationship_manager.create.assert_awaited_once()
        mock_registry.clear_pending_operations.assert_awaited_once_with("task_123")

    @pytest.mark.asyncio
    async def test_process_pending_operations_handles_update(
        self, mock_registry: MagicMock
    ) -> None:
        mock_registry.get_pending_operations.return_value = [
            {
                "op_id": "op_update",
                "operation": "update",
                "payload": {"updates": {"status": "doing"}},
            },
        ]
        mock_registry.clear_pending_operations.return_value = 1

        mock_entity_manager = AsyncMock()
        mock_relationship_manager = AsyncMock()
        mock_runtime = MagicMock(
            entity_manager=mock_entity_manager,
            relationship_manager=mock_relationship_manager,
        )

        with (
            patch("sibyl.jobs.pending.get_pending", return_value=mock_registry),
            patch(
                "sibyl_core.services.native_graph.get_native_graph_runtime",
                AsyncMock(return_value=mock_runtime),
            ),
        ):
            from sibyl.jobs.pending import process_pending_operations

            result = await process_pending_operations("task_123", "org_456")

        assert result == [
            {
                "op_id": "op_update",
                "operation": "update",
                "success": True,
                "updated_fields": ["status"],
            }
        ]
        mock_entity_manager.update.assert_awaited_once_with("task_123", {"status": "doing"})
        mock_registry.clear_pending_operations.assert_awaited_once_with("task_123")

    @pytest.mark.asyncio
    async def test_process_pending_operations_handles_unknown_operation(
        self, mock_registry: MagicMock
    ) -> None:
        mock_registry.get_pending_operations.return_value = [
            {
                "op_id": "op_1",
                "operation": "unknown_op",
                "payload": {},
            }
        ]
        mock_registry.clear_pending_operations.return_value = 1

        mock_entity_manager = AsyncMock()
        mock_relationship_manager = AsyncMock()
        mock_runtime = MagicMock(
            entity_manager=mock_entity_manager,
            relationship_manager=mock_relationship_manager,
        )

        with (
            patch("sibyl.jobs.pending.get_pending", return_value=mock_registry),
            patch(
                "sibyl_core.services.native_graph.get_native_graph_runtime",
                AsyncMock(return_value=mock_runtime),
            ),
        ):
            from sibyl.jobs.pending import process_pending_operations

            result = await process_pending_operations("task_123", "org_456")

        assert len(result) == 1
        assert result[0]["success"] is True
        assert "error" in result[0]
        mock_registry.clear_pending_operations.assert_awaited_once_with("task_123")


class TestEnqueueCreateEntityMarksPending:
    """Tests that enqueue_create_entity marks entity as pending."""

    @pytest.mark.asyncio
    async def test_enqueue_create_entity_marks_pending(self, mock_registry: MagicMock) -> None:
        from sibyl.coordination._redis.broker import RedisQueueBroker

        mock_pool = AsyncMock()
        mock_job = MagicMock()
        mock_job.job_id = "create_entity:task_123"
        mock_pool.enqueue_job.return_value = mock_job
        mock_pool.zadd = AsyncMock()
        mock_pool.zremrangebyrank = AsyncMock()
        broker = RedisQueueBroker()
        broker.get_pool = AsyncMock(return_value=mock_pool)  # type: ignore[method-assign]

        with (
            patch("sibyl.jobs.queue.get_queue", return_value=broker),
            patch("sibyl.jobs.pending.get_pending", return_value=mock_registry),
        ):
            from sibyl.jobs.queue import enqueue_create_entity

            job_id = await enqueue_create_entity(
                entity_id="task_123",
                entity_data={"id": "task_123", "name": "Test Task"},
                entity_type="task",
                group_id="org_456",
            )

        assert job_id == "create_entity:task_123"
        mock_registry.mark_pending.assert_awaited_once_with(
            "task_123",
            "create_entity:task_123",
            "task",
            "org_456",
        )


class TestCreateNoteChecksPending:
    """Tests that create_note checks pending status."""

    @pytest.mark.asyncio
    async def test_create_note_queues_when_task_pending(self, mock_registry: MagicMock) -> None:
        mock_registry.is_pending.return_value = {
            "job_id": "create_entity:task_123",
            "entity_type": "task",
            "group_id": "org_456",
        }
        mock_registry.queue_pending_operation.return_value = "pending_op_123"

        mock_org = MagicMock()
        mock_org.id = "org_456"

        mock_user = MagicMock()
        mock_user.id = "user_789"

        mock_auth = MagicMock()
        http_request = MagicMock()
        http_request.headers = {}

        with (
            patch("sibyl.jobs.pending.get_pending", return_value=mock_registry),
            patch("sibyl.api.routes.tasks._verify_task_access", return_value=None),
            patch("sibyl.api.routes.tasks.broadcast_event", return_value=None),
        ):
            from sibyl.api.routes.tasks import CreateNoteRequest, create_note

            request = CreateNoteRequest(content="Test note")

            result = await create_note(
                task_id="task_123",
                http_request=http_request,
                request=request,
                org=mock_org,
                user=mock_user,
                auth=mock_auth,
            )

        assert result.status == "pending"
        assert result.task_id == "task_123"
        assert result.content == "Test note"
        mock_registry.queue_pending_operation.assert_awaited_once()
