"""Tests for MetaOrchestratorService."""

from unittest.mock import AsyncMock, patch

import pytest

from sibyl.agents.meta_orchestrator import (
    MetaOrchestratorError,
    MetaOrchestratorService,
    create_meta_orchestrator_service,
)
from sibyl_core.models import (
    MetaOrchestratorRecord,
    MetaOrchestratorStatus,
    SprintStrategy,
    TaskOrchestratorRecord,
)


@pytest.fixture
def mock_entity_manager():
    """Create mock entity manager."""
    manager = AsyncMock()
    manager.create_direct = AsyncMock(return_value=None)
    manager.update = AsyncMock(return_value=None)
    manager.get = AsyncMock()
    manager.list_by_type = AsyncMock(return_value=[])
    return manager


@pytest.fixture
def meta_service(mock_entity_manager):
    """Create MetaOrchestratorService with mocked dependencies."""
    return MetaOrchestratorService(
        entity_manager=mock_entity_manager,
        org_id="org-123",
        project_id="proj-456",
    )


@pytest.fixture
def meta_orchestrator():
    """Create a MetaOrchestratorRecord."""
    return MetaOrchestratorRecord(
        id="meta-111",
        name="Test MetaOrch",
        organization_id="org-123",
        project_id="proj-456",
        status=MetaOrchestratorStatus.IDLE,
        strategy=SprintStrategy.SEQUENTIAL,
        task_queue=["task-1", "task-2"],
        active_orchestrators=[],
    )


@pytest.fixture
def task_orchestrator():
    """Create a TaskOrchestratorRecord."""
    return TaskOrchestratorRecord(
        id="taskorch-111",
        name="Test TaskOrch",
        organization_id="org-123",
        project_id="proj-456",
        task_id="task-1",
    )


class TestGetOrCreate:
    """Tests for get_or_create method."""

    @pytest.mark.asyncio
    async def test_creates_new_when_none_exists(self, meta_service, mock_entity_manager):
        """Creates new MetaOrchestrator when none exists for project."""
        mock_entity_manager.list_by_type.return_value = []

        record = await meta_service.get_or_create()

        assert record.project_id == "proj-456"
        assert record.status == MetaOrchestratorStatus.IDLE
        mock_entity_manager.create_direct.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_existing(self, meta_service, mock_entity_manager, meta_orchestrator):
        """Returns existing MetaOrchestrator when one exists."""
        mock_entity_manager.list_by_type.return_value = [meta_orchestrator]

        record = await meta_service.get_or_create()

        assert record.id == "meta-111"
        mock_entity_manager.create_direct.assert_not_called()


class TestQueueTask:
    """Tests for queue_task method."""

    @pytest.mark.asyncio
    async def test_adds_task_to_queue(self, meta_service, mock_entity_manager, meta_orchestrator):
        """Adds task to the queue."""
        meta_orchestrator.task_queue = []
        mock_entity_manager.get.return_value = meta_orchestrator

        await meta_service.queue_task("meta-111", "task-new")

        mock_entity_manager.update.assert_called_once()
        update_args = mock_entity_manager.update.call_args[0]
        assert "task-new" in update_args[1]["task_queue"]

    @pytest.mark.asyncio
    async def test_avoids_duplicates(self, meta_service, mock_entity_manager, meta_orchestrator):
        """Doesn't add duplicate tasks."""
        meta_orchestrator.task_queue = ["task-1"]
        mock_entity_manager.get.return_value = meta_orchestrator

        await meta_service.queue_task("meta-111", "task-1")

        # Should not call update since task already in queue
        mock_entity_manager.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_on_not_found(self, meta_service, mock_entity_manager):
        """Raises error when orchestrator not found."""
        mock_entity_manager.get.return_value = None

        with pytest.raises(MetaOrchestratorError) as exc:
            await meta_service.queue_task("meta-999", "task-1")

        assert "not found" in str(exc.value).lower()


class TestQueueTasks:
    """Tests for queue_tasks method."""

    @pytest.mark.asyncio
    async def test_adds_multiple_tasks(self, meta_service, mock_entity_manager, meta_orchestrator):
        """Adds multiple tasks at once."""
        meta_orchestrator.task_queue = ["task-1"]
        mock_entity_manager.get.return_value = meta_orchestrator

        await meta_service.queue_tasks("meta-111", ["task-2", "task-3", "task-1"])

        update_args = mock_entity_manager.update.call_args[0]
        task_queue = update_args[1]["task_queue"]
        # task-1 should not be duplicated
        assert task_queue.count("task-1") == 1
        assert "task-2" in task_queue
        assert "task-3" in task_queue


class TestStart:
    """Tests for start method."""

    @pytest.mark.asyncio
    async def test_starts_orchestration(self, meta_service, mock_entity_manager, meta_orchestrator):
        """Starts orchestration and spawns TaskOrchestrator."""
        mock_entity_manager.get.return_value = meta_orchestrator

        # Mock _spawn_orchestrators to avoid complex dependencies
        with patch.object(
            meta_service, "_spawn_orchestrators", new_callable=AsyncMock
        ) as mock_spawn:
            await meta_service.start("meta-111")

            # Should update status to running
            status_update = None
            for call in mock_entity_manager.update.call_args_list:
                if call[0][1].get("status") == MetaOrchestratorStatus.RUNNING.value:
                    status_update = call
                    break
            assert status_update is not None

            # Should have called spawn
            mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_if_already_running(
        self, meta_service, mock_entity_manager, meta_orchestrator
    ):
        """Raises error if already running."""
        meta_orchestrator.status = MetaOrchestratorStatus.RUNNING
        mock_entity_manager.get.return_value = meta_orchestrator

        with pytest.raises(MetaOrchestratorError) as exc:
            await meta_service.start("meta-111")

        assert "already running" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_raises_if_no_tasks(self, meta_service, mock_entity_manager, meta_orchestrator):
        """Raises error if no tasks queued."""
        meta_orchestrator.task_queue = []
        mock_entity_manager.get.return_value = meta_orchestrator

        with pytest.raises(MetaOrchestratorError) as exc:
            await meta_service.start("meta-111")

        assert "no tasks" in str(exc.value).lower()


class TestOnTaskComplete:
    """Tests for on_task_complete method."""

    @pytest.mark.asyncio
    async def test_updates_metrics_on_success(
        self, meta_service, mock_entity_manager, meta_orchestrator
    ):
        """Updates metrics when task completes successfully."""
        meta_orchestrator.active_orchestrators = ["taskorch-111"]
        meta_orchestrator.task_queue = []
        mock_entity_manager.get.return_value = meta_orchestrator

        await meta_service.on_task_complete(
            "meta-111",
            task_orchestrator_id="taskorch-111",
            success=True,
            cost_usd=5.0,
            rework_cycles=1,
        )

        # Find the metrics update call
        metrics_update = None
        for call in mock_entity_manager.update.call_args_list:
            if "tasks_completed" in call[0][1]:
                metrics_update = call[0][1]
                break

        assert metrics_update is not None
        assert metrics_update["tasks_completed"] == 1
        assert metrics_update["spent_usd"] == 5.0
        assert metrics_update["total_rework_cycles"] == 1

    @pytest.mark.asyncio
    async def test_removes_from_active_list(
        self, meta_service, mock_entity_manager, meta_orchestrator
    ):
        """Removes completed orchestrator from active list."""
        meta_orchestrator.active_orchestrators = ["taskorch-111", "taskorch-222"]
        meta_orchestrator.task_queue = []
        mock_entity_manager.get.return_value = meta_orchestrator

        await meta_service.on_task_complete(
            "meta-111",
            task_orchestrator_id="taskorch-111",
            success=True,
        )

        # Find the active_orchestrators update
        active_update = None
        for call in mock_entity_manager.update.call_args_list:
            if "active_orchestrators" in call[0][1]:
                active_update = call[0][1]["active_orchestrators"]
                break

        assert active_update is not None
        assert "taskorch-111" not in active_update
        assert "taskorch-222" in active_update


class TestPauseResume:
    """Tests for pause and resume methods."""

    @pytest.mark.asyncio
    async def test_pause_updates_status(self, meta_service, mock_entity_manager, meta_orchestrator):
        """Pause updates status to paused."""
        mock_entity_manager.get.return_value = meta_orchestrator

        await meta_service.pause("meta-111", "test_reason")

        mock_entity_manager.update.assert_called_with(
            "meta-111",
            {"status": MetaOrchestratorStatus.PAUSED.value},
        )

    @pytest.mark.asyncio
    async def test_resume_updates_status(
        self, meta_service, mock_entity_manager, meta_orchestrator
    ):
        """Resume updates status to running."""
        meta_orchestrator.status = MetaOrchestratorStatus.PAUSED
        mock_entity_manager.get.return_value = meta_orchestrator

        # Mock _spawn_orchestrators to avoid complex dependencies
        with patch.object(meta_service, "_spawn_orchestrators", new_callable=AsyncMock):
            await meta_service.resume("meta-111")

            # First call should set status to running
            first_update = mock_entity_manager.update.call_args_list[0]
            assert first_update[0][1]["status"] == MetaOrchestratorStatus.RUNNING.value

    @pytest.mark.asyncio
    async def test_resume_raises_if_not_paused(
        self, meta_service, mock_entity_manager, meta_orchestrator
    ):
        """Resume raises if not paused."""
        meta_orchestrator.status = MetaOrchestratorStatus.RUNNING
        mock_entity_manager.get.return_value = meta_orchestrator

        with pytest.raises(MetaOrchestratorError) as exc:
            await meta_service.resume("meta-111")

        assert "only resume paused" in str(exc.value).lower()


class TestSetStrategy:
    """Tests for set_strategy method."""

    @pytest.mark.asyncio
    async def test_updates_strategy(self, meta_service, mock_entity_manager, meta_orchestrator):
        """Updates strategy setting."""
        mock_entity_manager.get.return_value = meta_orchestrator

        await meta_service.set_strategy("meta-111", SprintStrategy.PARALLEL, max_concurrent=5)

        mock_entity_manager.update.assert_called_with(
            "meta-111",
            {"strategy": "parallel", "max_concurrent": 5},
        )


class TestSetBudget:
    """Tests for set_budget method."""

    @pytest.mark.asyncio
    async def test_updates_budget(self, meta_service, mock_entity_manager, meta_orchestrator):
        """Updates budget settings."""
        mock_entity_manager.get.return_value = meta_orchestrator

        await meta_service.set_budget("meta-111", budget_usd=200.0, alert_threshold=0.9)

        mock_entity_manager.update.assert_called_with(
            "meta-111",
            {"budget_usd": 200.0, "cost_alert_threshold": 0.9},
        )


class TestGetStatus:
    """Tests for get_status method."""

    @pytest.mark.asyncio
    async def test_returns_status_dict(self, meta_service, mock_entity_manager, meta_orchestrator):
        """Returns comprehensive status dict."""
        meta_orchestrator.tasks_completed = 3
        meta_orchestrator.spent_usd = 25.0
        mock_entity_manager.get.return_value = meta_orchestrator

        status = await meta_service.get_status("meta-111")

        assert status["id"] == "meta-111"
        assert status["status"] == "idle"
        assert status["queue_size"] == 2
        assert status["tasks_completed"] == 3
        assert status["spent_usd"] == 25.0


class TestCreateMetaOrchestratorService:
    """Tests for factory function."""

    @pytest.mark.asyncio
    async def test_creates_service(self, mock_entity_manager):
        """Factory creates properly configured service."""
        service = await create_meta_orchestrator_service(
            entity_manager=mock_entity_manager,
            org_id="org-123",
            project_id="proj-456",
        )

        assert isinstance(service, MetaOrchestratorService)
        assert service.org_id == "org-123"
        assert service.project_id == "proj-456"
