"""Tests for worker promotion flow."""

from unittest.mock import AsyncMock, patch

import pytest

from sibyl.agents.worker_promotion import (
    WorkerPromotionError,
    WorkerPromotionService,
    create_promotion_service,
)
from sibyl_core.models import (
    AgentRecord,
    AgentStatus,
    QualityGateType,
    Task,
    TaskOrchestratorPhase,
    TaskOrchestratorRecord,
    TaskOrchestratorStatus,
)


@pytest.fixture
def mock_entity_manager():
    """Create mock entity manager."""
    manager = AsyncMock()
    manager.get = AsyncMock()
    manager.update = AsyncMock()
    manager.create_direct = AsyncMock()
    return manager


@pytest.fixture
def mock_relationship_manager():
    """Create mock relationship manager."""
    manager = AsyncMock()
    manager.create = AsyncMock()
    return manager


@pytest.fixture
def mock_agent_runner():
    """Create mock agent runner."""
    return AsyncMock()


@pytest.fixture
def mock_worktree_manager():
    """Create mock worktree manager."""
    return AsyncMock()


@pytest.fixture
def promotion_service(
    mock_entity_manager,
    mock_relationship_manager,
    mock_agent_runner,
    mock_worktree_manager,
):
    """Create WorkerPromotionService with mocked dependencies."""
    return WorkerPromotionService(
        entity_manager=mock_entity_manager,
        org_id="org-123",
        project_id="proj-456",
        relationship_manager=mock_relationship_manager,
        agent_runner=mock_agent_runner,
        worktree_manager=mock_worktree_manager,
    )


@pytest.fixture
def standalone_agent():
    """Create a standalone agent ready for promotion."""
    return AgentRecord(
        id="agent-123",
        name="Test Agent",
        organization_id="org-123",
        project_id="proj-456",
        task_id="task-789",
        standalone=True,
        status=AgentStatus.COMPLETED,
    )


@pytest.fixture
def managed_agent():
    """Create a managed agent (not promotable)."""
    return AgentRecord(
        id="agent-456",
        name="Managed Agent",
        organization_id="org-123",
        project_id="proj-456",
        task_id="task-789",
        standalone=False,
        task_orchestrator_id="orch-111",
        status=AgentStatus.WORKING,
    )


@pytest.fixture
def task():
    """Create a Task for promotion."""
    return Task(
        id="task-789",
        name="Test Task",
        organization_id="org-123",
        title="Implement feature X",
        project_id="proj-456",
    )


@pytest.fixture
def orchestrator():
    """Create a TaskOrchestrator."""
    return TaskOrchestratorRecord(
        id="orch-111",
        name="Test Orchestrator",
        organization_id="org-123",
        project_id="proj-456",
        task_id="task-789",
        current_phase=TaskOrchestratorPhase.IMPLEMENT,
        gate_config=[QualityGateType.LINT, QualityGateType.TEST],
    )


class TestCanPromote:
    """Tests for can_promote method."""

    @pytest.mark.asyncio
    async def test_can_promote_standalone_completed(
        self, promotion_service, mock_entity_manager, standalone_agent
    ):
        """Standalone completed agent can be promoted."""
        mock_entity_manager.get.return_value = standalone_agent

        can_promote, reason = await promotion_service.can_promote("agent-123")

        assert can_promote is True
        assert "eligible" in reason.lower()

    @pytest.mark.asyncio
    async def test_can_promote_standalone_paused(
        self, promotion_service, mock_entity_manager, standalone_agent
    ):
        """Standalone paused agent can be promoted."""
        standalone_agent.status = AgentStatus.PAUSED
        mock_entity_manager.get.return_value = standalone_agent

        can_promote, _reason = await promotion_service.can_promote("agent-123")

        assert can_promote is True

    @pytest.mark.asyncio
    async def test_can_promote_standalone_working(
        self, promotion_service, mock_entity_manager, standalone_agent
    ):
        """Standalone working agent can be promoted."""
        standalone_agent.status = AgentStatus.WORKING
        mock_entity_manager.get.return_value = standalone_agent

        can_promote, _reason = await promotion_service.can_promote("agent-123")

        assert can_promote is True

    @pytest.mark.asyncio
    async def test_cannot_promote_managed_agent(
        self, promotion_service, mock_entity_manager, managed_agent
    ):
        """Managed agent cannot be promoted."""
        mock_entity_manager.get.return_value = managed_agent

        can_promote, reason = await promotion_service.can_promote("agent-456")

        assert can_promote is False
        assert "already managed" in reason.lower()

    @pytest.mark.asyncio
    async def test_cannot_promote_agent_without_task(
        self, promotion_service, mock_entity_manager, standalone_agent
    ):
        """Agent without task cannot be promoted."""
        standalone_agent.task_id = None
        mock_entity_manager.get.return_value = standalone_agent

        can_promote, reason = await promotion_service.can_promote("agent-123")

        assert can_promote is False
        assert "no task" in reason.lower()

    @pytest.mark.asyncio
    async def test_cannot_promote_failed_agent(
        self, promotion_service, mock_entity_manager, standalone_agent
    ):
        """Failed agent cannot be promoted."""
        standalone_agent.status = AgentStatus.FAILED
        mock_entity_manager.get.return_value = standalone_agent

        can_promote, reason = await promotion_service.can_promote("agent-123")

        assert can_promote is False
        assert "invalid state" in reason.lower()

    @pytest.mark.asyncio
    async def test_cannot_promote_nonexistent_agent(self, promotion_service, mock_entity_manager):
        """Non-existent agent cannot be promoted."""
        mock_entity_manager.get.return_value = None

        can_promote, reason = await promotion_service.can_promote("agent-999")

        assert can_promote is False
        assert "not found" in reason.lower()


class TestPromote:
    """Tests for promote method."""

    @pytest.mark.asyncio
    async def test_promote_creates_orchestrator(
        self, promotion_service, mock_entity_manager, standalone_agent, orchestrator, task
    ):
        """Promote creates new orchestrator when not provided."""
        mock_entity_manager.get.side_effect = [
            standalone_agent,  # can_promote: get agent
            standalone_agent,  # promote: get agent
            task,  # _create_orchestrator: get task
            standalone_agent,  # promote: refresh agent
            orchestrator,  # promote: refresh orchestrator
        ]

        with patch("sibyl.agents.task_orchestrator.TaskOrchestratorService") as MockService:
            mock_service = AsyncMock()
            mock_service.create.return_value = orchestrator
            MockService.return_value = mock_service

            _agent, _orch = await promotion_service.promote(
                "agent-123",
                create_worktree=False,
            )

            mock_service.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_promote_uses_existing_orchestrator(
        self, promotion_service, mock_entity_manager, standalone_agent, orchestrator
    ):
        """Promote uses provided orchestrator."""
        mock_entity_manager.get.side_effect = [
            standalone_agent,  # Initial get for can_promote
            standalone_agent,  # Get agent after can_promote
            orchestrator,  # Get orchestrator
            standalone_agent,  # Refresh agent
            orchestrator,  # Refresh orchestrator
        ]

        _agent, orch = await promotion_service.promote(
            "agent-123",
            orchestrator_id="orch-111",
            create_worktree=False,
        )

        assert orch.id == "orch-111"

    @pytest.mark.asyncio
    async def test_promote_updates_agent(
        self, promotion_service, mock_entity_manager, standalone_agent, orchestrator
    ):
        """Promote updates agent to managed status."""
        mock_entity_manager.get.side_effect = [
            standalone_agent,  # can_promote check
            standalone_agent,  # Get agent
            orchestrator,  # Get orchestrator
            standalone_agent,  # Refresh agent
            orchestrator,  # Refresh orchestrator
        ]

        await promotion_service.promote(
            "agent-123",
            orchestrator_id="orch-111",
            create_worktree=False,
        )

        # Should have updated agent with standalone=False
        update_calls = mock_entity_manager.update.call_args_list
        agent_update = None
        for call in update_calls:
            if call[0][0] == "agent-123":
                agent_update = call[0][1]
                break

        assert agent_update is not None
        assert agent_update.get("standalone") is False
        assert agent_update.get("task_orchestrator_id") == "orch-111"

    @pytest.mark.asyncio
    async def test_promote_fails_for_ineligible(
        self, promotion_service, mock_entity_manager, managed_agent
    ):
        """Promote fails for ineligible agent."""
        mock_entity_manager.get.return_value = managed_agent

        with pytest.raises(WorkerPromotionError) as exc:
            await promotion_service.promote("agent-456")

        assert "already managed" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_promote_rejects_terminal_orchestrator(
        self, promotion_service, mock_entity_manager, standalone_agent, orchestrator
    ):
        """Promote rejects orchestrator in terminal status."""
        orchestrator.status = TaskOrchestratorStatus.COMPLETE
        mock_entity_manager.get.side_effect = [
            standalone_agent,  # can_promote
            standalone_agent,  # Get agent
            orchestrator,  # Get orchestrator (completed)
        ]

        with pytest.raises(WorkerPromotionError) as exc:
            await promotion_service.promote(
                "agent-123",
                orchestrator_id="orch-111",
            )

        assert "terminal status" in str(exc.value).lower()


class TestDemote:
    """Tests for demote method."""

    @pytest.mark.asyncio
    async def test_demote_updates_agent(
        self, promotion_service, mock_entity_manager, managed_agent
    ):
        """Demote updates agent to standalone status."""
        mock_entity_manager.get.side_effect = [
            managed_agent,  # Initial check
            AgentRecord(
                id="agent-456",
                name="Demoted Agent",
                organization_id="org-123",
                project_id="proj-456",
                standalone=True,
                status=AgentStatus.WORKING,
            ),  # After update
        ]

        await promotion_service.demote("agent-456")

        # Verify update was called with standalone=True
        update_call = mock_entity_manager.update.call_args
        assert update_call[0][0] == "agent-456"
        assert update_call[0][1]["standalone"] is True
        assert update_call[0][1]["task_orchestrator_id"] is None

    @pytest.mark.asyncio
    async def test_demote_fails_for_standalone(
        self, promotion_service, mock_entity_manager, standalone_agent
    ):
        """Demote fails for already standalone agent."""
        mock_entity_manager.get.return_value = standalone_agent

        with pytest.raises(WorkerPromotionError) as exc:
            await promotion_service.demote("agent-123")

        assert "already standalone" in str(exc.value).lower()


class TestCreatePromotionService:
    """Tests for factory function."""

    @pytest.mark.asyncio
    async def test_create_returns_configured_service(self, mock_entity_manager):
        """Factory creates properly configured service."""
        service = await create_promotion_service(
            entity_manager=mock_entity_manager,
            org_id="org-123",
            project_id="proj-456",
        )

        assert isinstance(service, WorkerPromotionService)
        assert service.org_id == "org-123"
        assert service.project_id == "proj-456"
