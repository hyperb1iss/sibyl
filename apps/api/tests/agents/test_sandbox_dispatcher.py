"""Tests for sandbox dispatcher -- task queue semantics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from sibyl.agents.sandbox_dispatcher import SandboxDispatcher, SandboxDispatcherError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ids():
    """Fresh set of UUIDs for each test."""
    return SimpleNamespace(
        sandbox=uuid4(),
        org=uuid4(),
        runner=uuid4(),
        task=uuid4(),
    )


@pytest.fixture
def mock_session():
    """Mock async session."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.fixture
def dispatcher(mock_session):
    """Dispatcher with mocked session factory."""
    factory = MagicMock(return_value=mock_session)
    return SandboxDispatcher(
        session_factory=factory,
        enabled=True,
        max_attempts=3,
    )


@pytest.fixture
def disabled_dispatcher(mock_session):
    """Dispatcher that is disabled."""
    factory = MagicMock(return_value=mock_session)
    return SandboxDispatcher(
        session_factory=factory,
        enabled=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSandboxDispatcher:
    """Task queue semantics: enqueue, dispatch, ack, complete."""

    def test_require_enabled_raises_when_disabled(self, disabled_dispatcher):
        """Dispatcher methods raise when disabled."""
        with pytest.raises(SandboxDispatcherError, match="disabled"):
            disabled_dispatcher._require_enabled()

    def test_require_enabled_passes_when_enabled(self, dispatcher):
        """No exception when dispatcher is enabled."""
        dispatcher._require_enabled()

    def test_pending_statuses(self, dispatcher):
        """Verify PENDING_STATUSES set."""
        assert "queued" in dispatcher.PENDING_STATUSES
        assert "retry" in dispatcher.PENDING_STATUSES
        assert "completed" not in dispatcher.PENDING_STATUSES

    def test_max_attempts_from_init(self, dispatcher):
        """Max attempts set from constructor."""
        assert dispatcher.max_attempts == 3

    def test_max_attempts_default(self, mock_session):
        """Default max attempts is 3."""
        factory = AsyncMock(return_value=mock_session)
        d = SandboxDispatcher(session_factory=factory, enabled=True)
        assert d.max_attempts == 3

    def test_parse_uuid_valid(self, dispatcher):
        """Valid UUID string parses."""
        u = uuid4()
        assert dispatcher._parse_uuid(str(u)) == u

    def test_parse_uuid_invalid(self, dispatcher):
        """Invalid UUID string returns None."""
        assert dispatcher._parse_uuid("not-a-uuid") is None

    def test_parse_uuid_object(self, dispatcher):
        """UUID object passes through."""
        u = uuid4()
        assert dispatcher._parse_uuid(u) == u

    def test_parse_uuid_empty_string(self, dispatcher):
        """Empty string returns None."""
        assert dispatcher._parse_uuid("") is None

    def test_enabled_flag(self, dispatcher, disabled_dispatcher):
        """Enabled flag is respected."""
        assert dispatcher.enabled is True
        assert disabled_dispatcher.enabled is False


class TestDispatcherAsyncOperations:
    """Async operations with mocked DB sessions."""

    async def test_fail_all_pending_returns_zero_when_none(self, dispatcher, mock_session, ids):
        """No pending tasks -> 0 failed."""
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        count = await dispatcher.fail_all_pending(ids.org)
        assert count == 0

    async def test_fail_all_pending_raises_when_disabled(self, disabled_dispatcher, ids):
        """fail_all_pending raises when dispatcher disabled."""
        with pytest.raises(SandboxDispatcherError, match="disabled"):
            await disabled_dispatcher.fail_all_pending(ids.org)

    async def test_enqueue_raises_when_disabled(self, disabled_dispatcher, ids):
        """enqueue_task raises when dispatcher disabled."""
        with pytest.raises(SandboxDispatcherError):
            await disabled_dispatcher.enqueue_task(
                sandbox_id=ids.sandbox,
                organization_id=ids.org,
            )

    async def test_ack_raises_when_disabled(self, disabled_dispatcher, ids):
        """ack_task raises when dispatcher disabled."""
        with pytest.raises(SandboxDispatcherError):
            await disabled_dispatcher.ack_task(task_id=ids.task)

    async def test_reap_raises_when_disabled(self, disabled_dispatcher):
        """reap_stale_tasks raises when dispatcher disabled."""
        with pytest.raises(SandboxDispatcherError):
            await disabled_dispatcher.reap_stale_tasks()

    async def test_complete_task_raises_when_disabled(self, disabled_dispatcher, ids):
        """complete_task raises when dispatcher disabled."""
        with pytest.raises(SandboxDispatcherError):
            await disabled_dispatcher.complete_task(
                task_id=ids.task,
                success=True,
            )

    async def test_dispatch_pending_raises_when_disabled(self, disabled_dispatcher, ids):
        """dispatch_pending_for_sandbox raises when disabled."""
        with pytest.raises(SandboxDispatcherError):
            await disabled_dispatcher.dispatch_pending_for_sandbox(
                sandbox_id=ids.sandbox,
                send_fn=AsyncMock(return_value=True),
            )
