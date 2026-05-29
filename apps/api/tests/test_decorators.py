"""Tests for route handler decorators.

Covers the decorator functions that reduce boilerplate
in route handlers by handling common concerns.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from sibyl.api.decorators import handle_workflow_errors
from sibyl_core.errors import EntityNotFoundError, InvalidTransitionError


class TestHandleWorkflowErrors:
    """Tests for handle_workflow_errors decorator."""

    @pytest.mark.asyncio
    async def test_passes_through_on_success(self) -> None:
        """Returns function result when no exception."""

        @handle_workflow_errors("start_task")
        async def start_task(task_id: str) -> dict:
            return {"id": task_id, "status": "doing"}

        result = await start_task(task_id="task_123")
        assert result == {"id": "task_123", "status": "doing"}

    @pytest.mark.asyncio
    async def test_catches_entity_not_found(self) -> None:
        """Converts EntityNotFoundError to 404."""

        @handle_workflow_errors("start_task")
        async def start_task(task_id: str) -> dict:
            raise EntityNotFoundError("Task", task_id)

        with pytest.raises(HTTPException) as exc_info:
            await start_task(task_id="task_123")

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_catches_invalid_transition(self) -> None:
        """Converts InvalidTransitionError to 400."""

        @handle_workflow_errors("start_task")
        async def start_task(task_id: str) -> dict:
            raise InvalidTransitionError(from_status="done", to_status="doing")

        with pytest.raises(HTTPException) as exc_info:
            await start_task(task_id="task_123")

        assert exc_info.value.status_code == 400
        assert "done" in exc_info.value.detail
        assert "doing" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_catches_generic_exception(self) -> None:
        """Converts generic exceptions to 500 with logging."""

        @handle_workflow_errors("start_task")
        async def start_task(task_id: str) -> dict:
            raise RuntimeError("Database connection failed")

        with pytest.raises(HTTPException) as exc_info:
            await start_task(task_id="task_123")

        assert exc_info.value.status_code == 500
        assert "Failed to start task" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_preserves_http_exceptions(self) -> None:
        """Re-raises existing HTTPExceptions unchanged."""

        @handle_workflow_errors("start_task")
        async def start_task(task_id: str) -> dict:
            raise HTTPException(status_code=403, detail="Access denied")

        with pytest.raises(HTTPException) as exc_info:
            await start_task(task_id="task_123")

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Access denied"

    @pytest.mark.asyncio
    async def test_uses_operation_name_in_error_message(self) -> None:
        """Operation name appears in error message."""

        @handle_workflow_errors("complete_task")
        async def complete_task(task_id: str) -> dict:
            raise RuntimeError("Something went wrong")

        with pytest.raises(HTTPException) as exc_info:
            await complete_task(task_id="task_123")

        assert "complete task" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_preserves_function_metadata(self) -> None:
        """Preserves original function name and docstring."""

        @handle_workflow_errors("start_task")
        async def start_task(task_id: str) -> dict:
            """Start working on a task."""
            return {"id": task_id}

        assert start_task.__name__ == "start_task"
        assert start_task.__doc__ == "Start working on a task."
