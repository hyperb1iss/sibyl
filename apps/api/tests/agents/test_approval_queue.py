"""Tests for ApprovalQueue service."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.agents.approval_queue import ApprovalQueue, create_approval_queue
from sibyl_core.models import ApprovalRecord, ApprovalStatus, ApprovalType


@pytest.fixture
def mock_entity_manager():
    """Create mock entity manager."""
    manager = AsyncMock()
    manager.create_direct = AsyncMock(return_value=None)
    manager.update = AsyncMock(return_value=None)
    manager.get = AsyncMock(return_value=None)
    return manager


@pytest.fixture
def mock_redis():
    """Create mock Redis client."""
    redis = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.delete = AsyncMock(return_value=1)
    redis.publish = AsyncMock(return_value=1)
    redis.close = AsyncMock()

    # Mock scan_iter as async generator
    async def scan_iter(pattern):
        return
        yield  # Make it an async generator that yields nothing

    redis.scan_iter = scan_iter
    return redis


@pytest.fixture
def approval_queue(mock_entity_manager, mock_redis):
    """Create ApprovalQueue with mocked dependencies."""
    queue = ApprovalQueue(
        entity_manager=mock_entity_manager,
        org_id="org-123",
        project_id="proj-456",
        agent_id="agent-789",
        task_id="task-000",
    )
    queue._redis = mock_redis
    return queue


class TestApprovalQueueInit:
    """Tests for ApprovalQueue initialization."""

    def test_init_sets_attributes(self, mock_entity_manager):
        """Queue initializes with correct attributes."""
        queue = ApprovalQueue(
            entity_manager=mock_entity_manager,
            org_id="org-123",
            project_id="proj-456",
            agent_id="agent-789",
            task_id="task-000",
        )

        assert queue.org_id == "org-123"
        assert queue.project_id == "proj-456"
        assert queue.agent_id == "agent-789"
        assert queue.task_id == "task-000"
        assert queue._redis is None

    def test_init_without_task_id(self, mock_entity_manager):
        """Queue works without task_id."""
        queue = ApprovalQueue(
            entity_manager=mock_entity_manager,
            org_id="org-123",
            project_id="proj-456",
            agent_id="agent-789",
        )

        assert queue.task_id is None


class TestApprovalQueueEnqueue:
    """Tests for enqueue method."""

    @pytest.mark.asyncio
    async def test_enqueue_creates_record(self, approval_queue, mock_entity_manager):
        """Enqueue creates approval record in graph."""
        with (
            patch("sibyl.agents.approval_queue.ApprovalQueue._store_pending_state"),
            patch("sibyl.agents.approval_queue.ApprovalQueue._broadcast_approval_request"),
        ):
            record = await approval_queue.enqueue(
                approval_type=ApprovalType.DESTRUCTIVE_COMMAND,
                title="Test approval",
                summary="Test summary",
                metadata={"tool_name": "Bash", "command": "rm -rf /"},
            )

            assert record.approval_type == ApprovalType.DESTRUCTIVE_COMMAND
            assert record.title == "Test approval"
            assert record.status == ApprovalStatus.PENDING
            mock_entity_manager.create_direct.assert_called_once()

    @pytest.mark.asyncio
    async def test_enqueue_stores_pending_state(self, approval_queue, mock_redis):
        """Enqueue stores state in Redis for recovery."""
        with patch("sibyl.agents.approval_queue.ApprovalQueue._broadcast_approval_request"):
            await approval_queue.enqueue(
                approval_type=ApprovalType.FILE_WRITE,
                title="Write file",
                summary="Writing to test.txt",
                metadata={"file_path": "test.txt"},
            )

            # Should call setex to store pending state
            mock_redis.setex.assert_called()

    @pytest.mark.asyncio
    async def test_enqueue_respects_timeout(self, approval_queue):
        """Enqueue uses provided timeout for expiration."""
        with (
            patch("sibyl.agents.approval_queue.ApprovalQueue._store_pending_state"),
            patch("sibyl.agents.approval_queue.ApprovalQueue._broadcast_approval_request"),
        ):
            record = await approval_queue.enqueue(
                approval_type=ApprovalType.EXTERNAL_API,
                title="API call",
                summary="Calling external API",
                metadata={"url": "https://api.example.com"},
                expiry=timedelta(minutes=30),
            )

            # expires_at should be ~30 minutes from now
            assert record.expires_at is not None
            delta = record.expires_at - datetime.now(UTC)
            assert timedelta(minutes=29) < delta < timedelta(minutes=31)


class TestApprovalQueueWaitForResponse:
    """Tests for wait_for_response method."""

    @pytest.mark.asyncio
    async def test_wait_returns_existing_response(self, approval_queue, mock_redis):
        """Wait returns immediately if response already exists."""
        existing_response = {"approved": True, "message": "Already done", "by": "human"}

        # Need to mock pubsub since it's called before existing check
        mock_pubsub = AsyncMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        with patch.object(
            approval_queue, "_check_existing_response", return_value=existing_response
        ):
            response = await approval_queue.wait_for_response("approval-123", wait_seconds=10.0)

            assert response["approved"] is True
            assert response["by"] == "human"

    @pytest.mark.asyncio
    async def test_wait_timeout_returns_denied(self, approval_queue, mock_redis):
        """Wait returns denied response on timeout."""
        mock_pubsub = AsyncMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()

        # Make listen block forever (will timeout)
        async def slow_listen():
            await asyncio.sleep(100)
            yield {"type": "message", "data": '{"approved": true}'}

        mock_pubsub.listen = slow_listen
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        with (
            patch.object(approval_queue, "_check_existing_response", return_value=None),
            patch.object(approval_queue, "_handle_timeout"),
        ):
            response = await approval_queue.wait_for_response("approval-123", wait_seconds=0.1)

            assert response["approved"] is False
            assert "timed out" in response["message"].lower()


class TestApprovalQueueRespond:
    """Tests for respond method."""

    @pytest.mark.asyncio
    async def test_respond_updates_graph(self, approval_queue, mock_entity_manager):
        """Respond updates the approval record in graph."""
        with (
            patch.object(approval_queue, "_store_response"),
            patch.object(approval_queue, "_clear_pending_state"),
            patch("sibyl.agents.approval_queue.publish_approval_response"),
        ):
            result = await approval_queue.respond(
                approval_id="approval-123",
                approved=True,
                message="Looks good",
                responded_by="alice",
            )

            assert result is True
            mock_entity_manager.update.assert_called()
            call_args = mock_entity_manager.update.call_args
            assert call_args[0][0] == "approval-123"
            assert call_args[0][1]["status"] == ApprovalStatus.APPROVED.value

    @pytest.mark.asyncio
    async def test_respond_publishes_to_redis(self, approval_queue):
        """Respond publishes response for waiting worker."""
        with (
            patch.object(approval_queue, "_store_response"),
            patch.object(approval_queue, "_clear_pending_state"),
            patch("sibyl.agents.approval_queue.publish_approval_response") as mock_publish,
        ):
            await approval_queue.respond(
                approval_id="approval-123",
                approved=False,
                message="Denied",
                responded_by="bob",
            )

            mock_publish.assert_called_once_with(
                "approval-123",
                {"approved": False, "message": "Denied", "by": "bob"},
            )


class TestApprovalQueueReattach:
    """Tests for reattach_waiter method."""

    @pytest.mark.asyncio
    async def test_reattach_returns_none_if_no_pending_state(self, approval_queue):
        """Reattach returns None if approval wasn't being waited on."""
        with patch.object(approval_queue, "_get_pending_state", return_value=None):
            result = await approval_queue.reattach_waiter("approval-123")
            assert result is None

    @pytest.mark.asyncio
    async def test_reattach_returns_existing_response(self, approval_queue):
        """Reattach returns response if it was stored while we were down."""
        pending = {"id": "approval-123", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
        existing = {"approved": True, "message": "Done", "by": "alice"}

        with (
            patch.object(approval_queue, "_get_pending_state", return_value=pending),
            patch.object(approval_queue, "_check_existing_response", return_value=existing),
            patch.object(approval_queue, "_clear_pending_state"),
        ):
            result = await approval_queue.reattach_waiter("approval-123")

            assert result == existing

    @pytest.mark.asyncio
    async def test_reattach_handles_expired(self, approval_queue):
        """Reattach handles approvals that expired during downtime."""
        pending = {
            "id": "approval-123",
            "expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),  # Expired
        }

        with (
            patch.object(approval_queue, "_get_pending_state", return_value=pending),
            patch.object(approval_queue, "_check_existing_response", return_value=None),
            patch.object(approval_queue, "_handle_timeout"),
        ):
            result = await approval_queue.reattach_waiter("approval-123")

            assert result["approved"] is False
            assert "expired" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_reattach_continues_waiting(self, approval_queue):
        """Reattach continues waiting if approval still pending."""
        pending = {
            "id": "approval-123",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
        wait_response = {"approved": True, "message": "OK", "by": "human"}

        with (
            patch.object(approval_queue, "_get_pending_state", return_value=pending),
            patch.object(approval_queue, "_check_existing_response", return_value=None),
            patch.object(approval_queue, "wait_for_response", return_value=wait_response),
        ):
            result = await approval_queue.reattach_waiter("approval-123", wait_seconds=60.0)

            assert result == wait_response


class TestApprovalQueueListPending:
    """Tests for list_pending method."""

    @pytest.mark.asyncio
    async def test_list_pending_returns_empty_when_none(self, approval_queue, mock_redis):
        """List pending returns empty list when no pending approvals."""
        result = await approval_queue.list_pending()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_pending_returns_approvals(self, approval_queue, mock_redis):
        """List pending returns stored approvals."""
        approval_data = {"id": "approval-123", "title": "Test"}

        async def scan_with_results(pattern):
            yield "sibyl:pending_approvals:agent-789:approval-123"

        mock_redis.scan_iter = scan_with_results
        mock_redis.get = AsyncMock(return_value='{"id": "approval-123", "title": "Test"}')

        result = await approval_queue.list_pending()

        assert len(result) == 1
        assert result[0]["id"] == "approval-123"


class TestApprovalQueueCancelAll:
    """Tests for cancel_all method."""

    @pytest.mark.asyncio
    async def test_cancel_all_responds_denied(self, approval_queue):
        """Cancel all responds denied to all pending approvals."""
        pending = [{"id": "approval-1"}, {"id": "approval-2"}]

        with (
            patch.object(approval_queue, "list_pending", return_value=pending),
            patch.object(approval_queue, "respond") as mock_respond,
        ):
            count = await approval_queue.cancel_all(reason="test_cancel")

            assert count == 2
            assert mock_respond.call_count == 2


class TestApprovalQueueExpireStale:
    """Tests for expire_stale method."""

    @pytest.mark.asyncio
    async def test_expire_stale_handles_expired(self, approval_queue, mock_redis):
        """Expire stale handles approvals past their deadline."""
        expired_data = {
            "id": "approval-old",
            "expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        }

        async def scan_with_expired(pattern):
            yield "sibyl:pending_approvals:agent-789:approval-old"

        mock_redis.scan_iter = scan_with_expired
        mock_redis.get = AsyncMock(return_value='{"id": "approval-old", "expires_at": "' + expired_data["expires_at"] + '"}')

        with patch.object(approval_queue, "_handle_timeout") as mock_timeout:
            count = await approval_queue.expire_stale()

            assert count == 1
            mock_timeout.assert_called_once_with("approval-old")


class TestCreateApprovalQueue:
    """Tests for factory function."""

    @pytest.mark.asyncio
    async def test_create_returns_configured_instance(self, mock_entity_manager):
        """Factory creates properly configured instance."""
        queue = await create_approval_queue(
            entity_manager=mock_entity_manager,
            org_id="org-123",
            project_id="proj-456",
            agent_id="agent-789",
            task_id="task-000",
        )

        assert isinstance(queue, ApprovalQueue)
        assert queue.org_id == "org-123"
        assert queue.agent_id == "agent-789"
