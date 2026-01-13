"""Tests for MetaOrchestrator API endpoints."""


import pytest
from fastapi import HTTPException

from sibyl.api.routes.meta_orchestrators import (
    GetOrCreateRequest,
    MetaOrchestratorResponse,
    QueueTasksRequest,
    SetBudgetRequest,
    SetStrategyRequest,
    _parse_strategy,
    _record_to_response,
)
from sibyl_core.models import (
    MetaOrchestratorRecord,
    MetaOrchestratorStatus,
    SprintStrategy,
)


@pytest.fixture
def meta_orchestrator():
    """Create a MetaOrchestratorRecord for testing."""
    return MetaOrchestratorRecord(
        id="meta-123",
        organization_id="org-123",
        project_id="proj-456",
        status=MetaOrchestratorStatus.IDLE,
        strategy=SprintStrategy.SEQUENTIAL,
        task_queue=["task-1", "task-2"],
        active_orchestrators=["taskorch-1"],
        budget_usd=100.0,
        spent_usd=25.0,
        max_concurrent=3,
        tasks_completed=5,
        tasks_failed=1,
        total_rework_cycles=2,
    )


class TestRecordToResponse:
    """Tests for _record_to_response helper."""

    def test_converts_record_correctly(self, meta_orchestrator):
        """Converts MetaOrchestratorRecord to response model."""
        response = _record_to_response(meta_orchestrator)

        assert response.id == "meta-123"
        assert response.project_id == "proj-456"
        assert response.status == "idle"
        assert response.strategy == "sequential"
        assert response.queue_size == 2
        assert response.active_count == 1
        assert response.budget_usd == 100.0
        assert response.spent_usd == 25.0
        assert response.budget_remaining == 75.0
        assert response.max_concurrent == 3
        assert response.tasks_completed == 5
        assert response.tasks_failed == 1
        assert response.total_rework_cycles == 2


class TestParseStrategy:
    """Tests for _parse_strategy helper."""

    def test_parses_sequential(self):
        """Parses sequential strategy."""
        assert _parse_strategy("sequential") == SprintStrategy.SEQUENTIAL
        assert _parse_strategy("SEQUENTIAL") == SprintStrategy.SEQUENTIAL

    def test_parses_parallel(self):
        """Parses parallel strategy."""
        assert _parse_strategy("parallel") == SprintStrategy.PARALLEL

    def test_parses_priority(self):
        """Parses priority strategy."""
        assert _parse_strategy("priority") == SprintStrategy.PRIORITY

    def test_raises_on_invalid(self):
        """Raises HTTPException for invalid strategy."""
        with pytest.raises(HTTPException) as exc:
            _parse_strategy("invalid")

        assert exc.value.status_code == 400
        assert "Invalid strategy" in exc.value.detail


class TestRequestModels:
    """Tests for request models."""

    def test_get_or_create_request(self):
        """GetOrCreateRequest validates correctly."""
        request = GetOrCreateRequest(project_id="proj-123")
        assert request.project_id == "proj-123"

    def test_queue_tasks_request(self):
        """QueueTasksRequest validates correctly."""
        request = QueueTasksRequest(task_ids=["task-1", "task-2"])
        assert request.task_ids == ["task-1", "task-2"]

    def test_set_strategy_request(self):
        """SetStrategyRequest validates correctly."""
        request = SetStrategyRequest(strategy="parallel", max_concurrent=5)
        assert request.strategy == "parallel"
        assert request.max_concurrent == 5

    def test_set_budget_request(self):
        """SetBudgetRequest validates correctly."""
        request = SetBudgetRequest(budget_usd=500.0, alert_threshold=0.9)
        assert request.budget_usd == 500.0
        assert request.alert_threshold == 0.9


class TestResponseModels:
    """Tests for response models."""

    def test_meta_orchestrator_response(self):
        """MetaOrchestratorResponse validates correctly."""
        response = MetaOrchestratorResponse(
            id="meta-123",
            project_id="proj-456",
            status="idle",
            strategy="sequential",
            queue_size=2,
            active_count=1,
            tasks_completed=5,
            tasks_failed=1,
            total_rework_cycles=2,
            budget_usd=100.0,
            spent_usd=25.0,
            budget_remaining=75.0,
            max_concurrent=3,
            created_at=None,
        )
        assert response.id == "meta-123"
        assert response.budget_remaining == 75.0
