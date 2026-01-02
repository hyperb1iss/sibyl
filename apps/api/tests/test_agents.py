"""Tests for agent system - runner, instance lifecycle, and helpers.

These tests cover the core agent functionality without requiring a real Claude SDK
connection. Uses mocks to simulate the SDK behavior.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl_core.models import (
    AgentRecord,
    AgentSpawnSource,
    AgentStatus,
    AgentType,
    Task,
    TaskStatus,
)

# =============================================================================
# Mock Classes
# =============================================================================


@dataclass
class MockMessage:
    """Mock message from Claude SDK."""

    type: str = "assistant"
    content: str = "Test response"
    session_id: str | None = None


@dataclass
class MockResultMessage(MockMessage):
    """Mock result message with session ID."""

    type: str = "result"
    session_id: str = "session_123"


@dataclass
class MockEntityManager:
    """Mock EntityManager for testing agent persistence."""

    _entities: dict[str, Any] = field(default_factory=dict)
    _updates: list[tuple[str, dict]] = field(default_factory=list)

    async def create_direct(self, entity: Any) -> None:
        """Store entity without LLM extraction."""
        self._entities[entity.id] = entity

    async def update(self, entity_id: str, updates: dict) -> None:
        """Track updates to entities (don't actually apply to avoid Pydantic errors)."""
        self._updates.append((entity_id, updates))
        # Don't actually update the entity - just track the updates
        # Real EntityManager handles metadata differently

    async def get(self, entity_id: str) -> Any | None:
        """Get entity by ID."""
        return self._entities.get(entity_id)


@dataclass
class MockWorktreeManager:
    """Mock WorktreeManager for testing."""

    _worktrees: dict[str, Any] = field(default_factory=dict)

    async def create(
        self, task_id: str, branch_name: str, agent_id: str
    ) -> "MockWorktree":
        """Create a mock worktree."""
        worktree = MockWorktree(
            path=f"/tmp/worktrees/{agent_id}",
            branch=branch_name,
            task_id=task_id,
            agent_id=agent_id,
        )
        self._worktrees[agent_id] = worktree
        return worktree

    async def cleanup_orphaned(self) -> int:
        """Mock cleanup."""
        return 0


@dataclass
class MockWorktree:
    """Mock worktree data."""

    path: str
    branch: str
    task_id: str
    agent_id: str


@dataclass
class MockLockManager:
    """Mock EntityLockManager for testing."""

    _locks: dict[str, str] = field(default_factory=dict)
    _should_fail: bool = False

    async def acquire(
        self, org_id: str, entity_id: str, blocking: bool = True
    ) -> str | None:
        """Acquire a mock lock."""
        if self._should_fail:
            return None
        key = f"{org_id}:{entity_id}"
        if key in self._locks and not blocking:
            return None
        token = f"token_{entity_id}"
        self._locks[key] = token
        return token

    async def release(self, org_id: str, entity_id: str, token: str) -> bool:
        """Release a mock lock."""
        key = f"{org_id}:{entity_id}"
        if self._locks.get(key) == token:
            del self._locks[key]
            return True
        return False


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_entity_manager() -> MockEntityManager:
    """Create a mock entity manager."""
    return MockEntityManager()


@pytest.fixture
def mock_worktree_manager() -> MockWorktreeManager:
    """Create a mock worktree manager."""
    return MockWorktreeManager()


@pytest.fixture
def mock_lock_manager() -> MockLockManager:
    """Create a mock lock manager."""
    return MockLockManager()


@pytest.fixture
def sample_task() -> Task:
    """Create a sample task for testing."""
    return Task(
        id="task_test123",
        title="Test Task",
        description="A test task for agent testing",
        status=TaskStatus.TODO,
        project_id="project_123",
    )


@pytest.fixture
def sample_agent_record() -> AgentRecord:
    """Create a sample agent record."""
    return AgentRecord(
        id="agent_test123",
        name="Test Agent",
        organization_id="org_123",
        project_id="project_123",
        agent_type=AgentType.GENERAL,
        spawn_source=AgentSpawnSource.USER,
        status=AgentStatus.INITIALIZING,
        initial_prompt="Test prompt",
    )


# =============================================================================
# Fire-and-Forget Helper Tests
# =============================================================================


class TestFireAndForget:
    """Tests for the _fire_and_forget helper function."""

    @pytest.mark.asyncio
    async def test_successful_task_completes(self) -> None:
        """Fire-and-forget task completes successfully."""
        from sibyl.jobs.agents import _fire_and_forget

        result = []

        async def successful_coro() -> None:
            result.append("done")

        task = _fire_and_forget(successful_coro(), name="test_success")
        await task
        assert result == ["done"]

    @pytest.mark.asyncio
    async def test_failed_task_logs_error(self) -> None:
        """Fire-and-forget task logs exceptions instead of swallowing them."""
        from sibyl.jobs.agents import _fire_and_forget

        async def failing_coro() -> None:
            raise ValueError("Test error")

        with patch("sibyl.jobs.agents.log") as mock_log:
            task = _fire_and_forget(failing_coro(), name="test_failure")

            # Wait for task to complete (it will fail)
            await asyncio.sleep(0.01)

            # Verify error was logged
            mock_log.error.assert_called_once()
            call_args = mock_log.error.call_args
            assert "test_failure" in call_args[0][0]
            assert "Test error" in str(call_args[1].get("error", ""))

    @pytest.mark.asyncio
    async def test_cancelled_task_no_error(self) -> None:
        """Cancelled tasks don't trigger error logging."""
        from sibyl.jobs.agents import _fire_and_forget

        async def slow_coro() -> None:
            await asyncio.sleep(10)

        with patch("sibyl.jobs.agents.log") as mock_log:
            task = _fire_and_forget(slow_coro(), name="test_cancel")
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

            # Verify no error was logged
            mock_log.error.assert_not_called()


# =============================================================================
# AgentRunner Tests
# =============================================================================


class TestAgentRunner:
    """Tests for AgentRunner spawn and lifecycle management."""

    @pytest.mark.asyncio
    async def test_spawn_creates_agent_record(
        self,
        mock_entity_manager: MockEntityManager,
        mock_worktree_manager: MockWorktreeManager,
    ) -> None:
        """spawn() creates and persists an agent record."""
        from sibyl.agents.runner import AgentRunner

        with patch("sibyl.agents.runner.EntityLockManager") as MockLock:
            MockLock.return_value = MockLockManager()

            runner = AgentRunner(
                entity_manager=mock_entity_manager,  # type: ignore
                worktree_manager=mock_worktree_manager,  # type: ignore
                org_id="org_123",
                project_id="project_123",
            )

            # Mock the Claude SDK client
            with patch("sibyl.agents.runner.ClaudeSDKClient"):
                instance = await runner.spawn(
                    prompt="Test prompt",
                    agent_type=AgentType.GENERAL,
                    create_worktree=False,
                    enable_approvals=False,
                )

            # Verify agent was created
            assert instance.id.startswith("agent_")
            assert instance.record.agent_type == AgentType.GENERAL
            # Status transitions to WORKING after spawn completes
            assert instance.record.status in (AgentStatus.INITIALIZING, AgentStatus.WORKING)

            # Verify entity was persisted
            assert len(mock_entity_manager._entities) == 1

    @pytest.mark.asyncio
    async def test_spawn_for_task_prevents_duplicates(
        self,
        mock_entity_manager: MockEntityManager,
        mock_worktree_manager: MockWorktreeManager,
        mock_lock_manager: MockLockManager,
        sample_task: Task,
    ) -> None:
        """spawn_for_task() prevents duplicate agents for same task."""
        from sibyl.agents.runner import AgentRunner

        with patch("sibyl.agents.runner.EntityLockManager") as MockLock:
            MockLock.return_value = mock_lock_manager

            runner = AgentRunner(
                entity_manager=mock_entity_manager,  # type: ignore
                worktree_manager=mock_worktree_manager,  # type: ignore
                org_id="org_123",
                project_id="project_123",
            )
            runner._lock_manager = mock_lock_manager

            # Mock the Claude SDK client
            with patch("sibyl.agents.runner.ClaudeSDKClient"):
                # First spawn should succeed
                instance1 = await runner.spawn_for_task(sample_task)
                assert instance1.task == sample_task

                # Second spawn should fail (agent already running)
                with pytest.raises(ValueError, match="already running"):
                    await runner.spawn_for_task(sample_task)

    @pytest.mark.asyncio
    async def test_spawn_for_task_lock_failure(
        self,
        mock_entity_manager: MockEntityManager,
        mock_worktree_manager: MockWorktreeManager,
        mock_lock_manager: MockLockManager,
        sample_task: Task,
    ) -> None:
        """spawn_for_task() fails gracefully when lock unavailable."""
        from sibyl.agents.runner import AgentRunner
        from sibyl.locks import LockAcquisitionError

        mock_lock_manager._should_fail = True

        with patch("sibyl.agents.runner.EntityLockManager") as MockLock:
            MockLock.return_value = mock_lock_manager

            runner = AgentRunner(
                entity_manager=mock_entity_manager,  # type: ignore
                worktree_manager=mock_worktree_manager,  # type: ignore
                org_id="org_123",
                project_id="project_123",
            )
            runner._lock_manager = mock_lock_manager

            with pytest.raises(LockAcquisitionError):
                await runner.spawn_for_task(sample_task)

    @pytest.mark.asyncio
    async def test_stop_agent_removes_from_active(
        self,
        mock_entity_manager: MockEntityManager,
        mock_worktree_manager: MockWorktreeManager,
    ) -> None:
        """stop_agent() removes agent from active list."""
        from sibyl.agents.runner import AgentRunner

        with patch("sibyl.agents.runner.EntityLockManager") as MockLock:
            MockLock.return_value = MockLockManager()

            runner = AgentRunner(
                entity_manager=mock_entity_manager,  # type: ignore
                worktree_manager=mock_worktree_manager,  # type: ignore
                org_id="org_123",
                project_id="project_123",
            )

            # Mock the Claude SDK client
            with patch("sibyl.agents.runner.ClaudeSDKClient"):
                instance = await runner.spawn(
                    prompt="Test",
                    create_worktree=False,
                    enable_approvals=False,
                )

            assert len(runner._active_agents) == 1

            # Stop the agent
            stopped = await runner.stop_agent(instance.id)
            assert stopped is True
            assert len(runner._active_agents) == 0

    @pytest.mark.asyncio
    async def test_stop_nonexistent_agent_returns_false(
        self,
        mock_entity_manager: MockEntityManager,
        mock_worktree_manager: MockWorktreeManager,
    ) -> None:
        """stop_agent() returns False for nonexistent agent."""
        from sibyl.agents.runner import AgentRunner

        with patch("sibyl.agents.runner.EntityLockManager") as MockLock:
            MockLock.return_value = MockLockManager()

            runner = AgentRunner(
                entity_manager=mock_entity_manager,  # type: ignore
                worktree_manager=mock_worktree_manager,  # type: ignore
                org_id="org_123",
                project_id="project_123",
            )

            stopped = await runner.stop_agent("nonexistent_agent")
            assert stopped is False

    @pytest.mark.asyncio
    async def test_list_active_returns_all_agents(
        self,
        mock_entity_manager: MockEntityManager,
        mock_worktree_manager: MockWorktreeManager,
    ) -> None:
        """list_active() returns all active agent instances."""
        from sibyl.agents.runner import AgentRunner

        with patch("sibyl.agents.runner.EntityLockManager") as MockLock:
            MockLock.return_value = MockLockManager()

            runner = AgentRunner(
                entity_manager=mock_entity_manager,  # type: ignore
                worktree_manager=mock_worktree_manager,  # type: ignore
                org_id="org_123",
                project_id="project_123",
            )

            with patch("sibyl.agents.runner.ClaudeSDKClient"):
                await runner.spawn(prompt="Test 1", create_worktree=False, enable_approvals=False)
                await runner.spawn(prompt="Test 2", create_worktree=False, enable_approvals=False)

            active = await runner.list_active()
            assert len(active) == 2


# =============================================================================
# AgentInstance Tests
# =============================================================================


class TestAgentInstance:
    """Tests for AgentInstance lifecycle methods."""

    @pytest.mark.asyncio
    async def test_cancel_heartbeat_awaits_task(self) -> None:
        """_cancel_heartbeat() properly awaits the cancelled task."""
        from sibyl.agents.runner import AgentInstance

        # Create a minimal instance
        mock_record = AgentRecord(
            id="agent_test",
            name="Test",
            organization_id="org_123",
            project_id="project_123",
            agent_type=AgentType.GENERAL,
            spawn_source=AgentSpawnSource.USER,
            status=AgentStatus.WORKING,
            initial_prompt="Test",
        )

        instance = AgentInstance(
            record=mock_record,
            sdk_options=MagicMock(),
            entity_manager=MockEntityManager(),  # type: ignore
            initial_prompt="Test",
        )

        # Simulate a running heartbeat task
        async def slow_heartbeat() -> None:
            await asyncio.sleep(10)

        instance._heartbeat_task = asyncio.create_task(slow_heartbeat())

        # Cancel should complete without hanging
        await asyncio.wait_for(instance._cancel_heartbeat(), timeout=1.0)

        assert instance._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_stop_updates_status(self) -> None:
        """stop() updates agent status to TERMINATED."""
        from sibyl.agents.runner import AgentInstance

        mock_entity_manager = MockEntityManager()

        mock_record = AgentRecord(
            id="agent_test",
            name="Test",
            organization_id="org_123",
            project_id="project_123",
            agent_type=AgentType.GENERAL,
            spawn_source=AgentSpawnSource.USER,
            status=AgentStatus.WORKING,
            initial_prompt="Test",
        )
        mock_entity_manager._entities[mock_record.id] = mock_record

        instance = AgentInstance(
            record=mock_record,
            sdk_options=MagicMock(),
            entity_manager=mock_entity_manager,  # type: ignore
            initial_prompt="Test",
        )

        await instance.stop(reason="test_stop")

        # Verify status was updated - check that TERMINATED status was set
        status_updates = [u for u in mock_entity_manager._updates if "status" in u[1]]
        assert len(status_updates) >= 1
        # The last status update should be TERMINATED
        last_status = status_updates[-1][1]["status"]
        assert last_status == AgentStatus.TERMINATED.value

    @pytest.mark.asyncio
    async def test_pause_updates_status(self) -> None:
        """pause() updates agent status to PAUSED."""
        from sibyl.agents.runner import AgentInstance

        mock_entity_manager = MockEntityManager()

        mock_record = AgentRecord(
            id="agent_test",
            name="Test",
            organization_id="org_123",
            project_id="project_123",
            agent_type=AgentType.GENERAL,
            spawn_source=AgentSpawnSource.USER,
            status=AgentStatus.WORKING,
            initial_prompt="Test",
        )
        mock_entity_manager._entities[mock_record.id] = mock_record

        instance = AgentInstance(
            record=mock_record,
            sdk_options=MagicMock(),
            entity_manager=mock_entity_manager,  # type: ignore
            initial_prompt="Test",
        )

        await instance.pause(reason="test_pause")

        # Verify status was updated - check that PAUSED status was set
        status_updates = [u for u in mock_entity_manager._updates if "status" in u[1]]
        assert len(status_updates) >= 1
        last_status = status_updates[-1][1]["status"]
        assert last_status == AgentStatus.PAUSED.value


# =============================================================================
# Orchestrator Tests
# =============================================================================


class TestOrchestrator:
    """Tests for AgentOrchestrator coordination."""

    @pytest.mark.asyncio
    async def test_stop_cancels_health_check(self) -> None:
        """stop() properly cancels and awaits health check task."""
        from sibyl.agents.orchestrator import AgentOrchestrator

        mock_entity_manager = MockEntityManager()

        with (
            patch("sibyl.agents.orchestrator.AgentRunner") as MockRunner,
            patch("sibyl.agents.orchestrator.WorktreeManager") as MockWorktree,
        ):
            mock_runner = MagicMock()
            mock_runner.list_active = AsyncMock(return_value=[])
            MockRunner.return_value = mock_runner

            mock_worktree = MagicMock()
            mock_worktree.cleanup_orphaned = AsyncMock(return_value=0)
            MockWorktree.return_value = mock_worktree

            orchestrator = AgentOrchestrator(
                entity_manager=mock_entity_manager,  # type: ignore
                org_id="org_123",
                project_id="project_123",
                repo_path="/tmp/test_repo",
            )

            # Simulate a running health check
            async def slow_health_check() -> None:
                while True:
                    await asyncio.sleep(1)

            orchestrator._running = True
            orchestrator._health_check_task = asyncio.create_task(slow_health_check())

            # Stop should complete without hanging
            await asyncio.wait_for(orchestrator.stop(), timeout=2.0)

            assert orchestrator._health_check_task is None
            assert orchestrator._running is False
