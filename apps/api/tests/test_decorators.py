"""Tests for route handler decorators.

Covers the decorator functions that reduce boilerplate
in route handlers by handling common concerns.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from sibyl.api.decorators import (
    handle_not_found,
    log_operation,
    require_state,
    with_error_context,
)
from sibyl_core.errors import EntityNotFoundError


# =============================================================================
# handle_not_found Tests
# =============================================================================
class TestHandleNotFound:
    """Tests for handle_not_found decorator."""

    @pytest.mark.asyncio
    async def test_passes_through_on_success(self) -> None:
        """Returns function result when no exception."""

        @handle_not_found("Agent", "agent_id")
        async def get_agent(agent_id: str) -> dict:
            return {"id": agent_id, "name": "Test"}

        result = await get_agent(agent_id="agent_123")
        assert result == {"id": "agent_123", "name": "Test"}

    @pytest.mark.asyncio
    async def test_catches_entity_not_found(self) -> None:
        """Converts EntityNotFoundError to 404."""

        @handle_not_found("Agent", "agent_id")
        async def get_agent(agent_id: str) -> dict:
            raise EntityNotFoundError("Agent", agent_id)

        with pytest.raises(HTTPException) as exc_info:
            await get_agent(agent_id="agent_123")

        assert exc_info.value.status_code == 404
        assert "Agent not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_includes_id_in_error_message(self) -> None:
        """Error message includes the entity ID."""

        @handle_not_found("Task", "task_id")
        async def get_task(task_id: str) -> dict:
            raise EntityNotFoundError("Task", task_id)

        with pytest.raises(HTTPException) as exc_info:
            await get_task(task_id="task_abc123")

        assert "task_abc123" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_uses_correct_param_name(self) -> None:
        """Uses specified param name for ID extraction."""

        @handle_not_found("Project", "project_uuid")
        async def get_project(project_uuid: str) -> dict:
            raise EntityNotFoundError("Project", project_uuid)

        with pytest.raises(HTTPException) as exc_info:
            await get_project(project_uuid="proj_xyz")

        assert "proj_xyz" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_default_id_param(self) -> None:
        """Uses 'id' as default param name."""

        @handle_not_found("Entity")
        async def get_entity(id: str) -> dict:  # noqa: A002
            raise EntityNotFoundError("Entity", id)

        with pytest.raises(HTTPException) as exc_info:
            await get_entity(id="ent_123")

        assert "ent_123" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_handles_missing_id_param(self) -> None:
        """Handles case where ID param is not in kwargs."""

        @handle_not_found("Agent", "agent_id")
        async def get_agent() -> dict:
            raise EntityNotFoundError("Agent", "unknown")

        with pytest.raises(HTTPException) as exc_info:
            await get_agent()

        assert exc_info.value.status_code == 404
        assert "Agent not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_preserves_other_exceptions(self) -> None:
        """Does not catch other exception types."""

        @handle_not_found("Agent", "agent_id")
        async def get_agent(agent_id: str) -> dict:
            raise ValueError("Something else went wrong")

        with pytest.raises(ValueError):
            await get_agent(agent_id="agent_123")

    @pytest.mark.asyncio
    async def test_preserves_function_metadata(self) -> None:
        """Preserves original function name and docstring."""

        @handle_not_found("Agent", "agent_id")
        async def get_agent(agent_id: str) -> dict:
            """Get an agent by ID."""
            return {"id": agent_id}

        assert get_agent.__name__ == "get_agent"
        assert get_agent.__doc__ == "Get an agent by ID."


# =============================================================================
# log_operation Tests
# =============================================================================
class TestLogOperation:
    """Tests for log_operation decorator."""

    @pytest.mark.asyncio
    async def test_passes_through_on_success(self) -> None:
        """Returns function result on success."""

        @log_operation("test_op")
        async def do_something() -> str:
            return "result"

        result = await do_something()
        assert result == "result"

    @pytest.mark.asyncio
    async def test_logs_success(self) -> None:
        """Logs success with timing on completion."""

        @log_operation("test_op")
        async def do_something() -> str:
            return "result"

        with patch("sibyl.api.decorators.log") as mock_log:
            await do_something()
            mock_log.info.assert_called_once()
            call_args = mock_log.info.call_args
            assert "test_op_success" in call_args[0]
            assert "elapsed_ms" in call_args[1]

    @pytest.mark.asyncio
    async def test_uses_function_name_as_default(self) -> None:
        """Uses function name when operation not specified."""

        @log_operation()
        async def my_operation() -> str:
            return "result"

        with patch("sibyl.api.decorators.log") as mock_log:
            await my_operation()
            call_args = mock_log.info.call_args
            assert "my_operation_success" in call_args[0]

    @pytest.mark.asyncio
    async def test_logs_result_type_when_enabled(self) -> None:
        """Includes result type in log when include_result=True."""

        @log_operation("test_op", include_result=True)
        async def do_something() -> dict:
            return {"key": "value"}

        with patch("sibyl.api.decorators.log") as mock_log:
            await do_something()
            call_args = mock_log.info.call_args
            assert call_args[1]["result_type"] == "dict"

    @pytest.mark.asyncio
    async def test_logs_http_error_as_debug(self) -> None:
        """Logs HTTP exceptions at debug level."""

        @log_operation("test_op")
        async def do_something() -> str:
            raise HTTPException(status_code=404, detail="Not found")

        with patch("sibyl.api.decorators.log") as mock_log:
            with pytest.raises(HTTPException):
                await do_something()
            mock_log.debug.assert_called_once()
            assert "test_op_http_error" in mock_log.debug.call_args[0]

    @pytest.mark.asyncio
    async def test_logs_other_errors_as_exception(self) -> None:
        """Logs other exceptions with full traceback."""

        @log_operation("test_op")
        async def do_something() -> str:
            raise ValueError("Something went wrong")

        with patch("sibyl.api.decorators.log") as mock_log:
            with pytest.raises(ValueError):
                await do_something()
            mock_log.exception.assert_called_once()
            call_args = mock_log.exception.call_args
            assert "test_op_error" in call_args[0]
            assert call_args[1]["error_type"] == "ValueError"

    @pytest.mark.asyncio
    async def test_reraises_http_exception(self) -> None:
        """Re-raises HTTP exceptions unchanged."""

        @log_operation("test_op")
        async def do_something() -> str:
            raise HTTPException(status_code=403, detail="Forbidden")

        with pytest.raises(HTTPException) as exc_info:
            await do_something()
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Forbidden"

    @pytest.mark.asyncio
    async def test_reraises_other_exceptions(self) -> None:
        """Re-raises non-HTTP exceptions."""

        @log_operation("test_op")
        async def do_something() -> str:
            raise RuntimeError("Unexpected error")

        with pytest.raises(RuntimeError):
            await do_something()

    @pytest.mark.asyncio
    async def test_preserves_function_metadata(self) -> None:
        """Preserves original function name and docstring."""

        @log_operation("test_op")
        async def my_function() -> str:
            """My docstring."""
            return "result"

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."


# =============================================================================
# require_state Tests
# =============================================================================
class TestRequireState:
    """Tests for require_state decorator."""

    @pytest.mark.asyncio
    async def test_allows_valid_state(self) -> None:
        """Proceeds when entity is in valid state."""
        entity = MagicMock()
        entity.metadata = {"status": "working"}

        @require_state("working", "paused", operation="pause agent")
        async def pause_agent(entity: MagicMock) -> str:
            return "paused"

        result = await pause_agent(entity=entity)
        assert result == "paused"

    @pytest.mark.asyncio
    async def test_rejects_invalid_state(self) -> None:
        """Raises 400 when entity is in invalid state."""
        entity = MagicMock()
        entity.metadata = {"status": "terminated"}

        @require_state("working", "paused", operation="pause agent")
        async def pause_agent(entity: MagicMock) -> str:
            return "paused"

        with pytest.raises(HTTPException) as exc_info:
            await pause_agent(entity=entity)

        assert exc_info.value.status_code == 400
        assert "Cannot pause agent" in exc_info.value.detail
        assert "terminated" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_includes_valid_states_in_error(self) -> None:
        """Error message includes list of valid states."""
        entity = MagicMock()
        entity.metadata = {"status": "terminated"}

        @require_state("working", "paused", operation="resume")
        async def resume_agent(entity: MagicMock) -> str:
            return "resumed"

        with pytest.raises(HTTPException) as exc_info:
            await resume_agent(entity=entity)

        assert "paused" in exc_info.value.detail
        assert "working" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_uses_custom_state_field(self) -> None:
        """Uses specified state_field for lookup."""
        entity = MagicMock()
        entity.metadata = {"phase": "active"}

        @require_state("active", "pending", state_field="phase")
        async def process(entity: MagicMock) -> str:
            return "processed"

        result = await process(entity=entity)
        assert result == "processed"

    @pytest.mark.asyncio
    async def test_uses_custom_state_getter(self) -> None:
        """Uses state_getter function for complex extraction."""
        entity = MagicMock()
        entity.metadata = {"nested": {"state": "ready"}}

        def get_nested_state(metadata: dict) -> str:
            return metadata.get("nested", {}).get("state", "unknown")

        @require_state("ready", state_getter=get_nested_state)
        async def process(entity: MagicMock) -> str:
            return "processed"

        result = await process(entity=entity)
        assert result == "processed"

    @pytest.mark.asyncio
    async def test_handles_missing_entity(self) -> None:
        """Proceeds when entity is not in kwargs."""

        @require_state("working", operation="do thing")
        async def do_thing(other_arg: str) -> str:
            return other_arg

        result = await do_thing(other_arg="value")
        assert result == "value"

    @pytest.mark.asyncio
    async def test_handles_missing_metadata(self) -> None:
        """Handles entity without metadata attribute."""
        entity = MagicMock(spec=[])  # No attributes

        @require_state("working", operation="process")
        async def process(entity: MagicMock) -> str:
            return "processed"

        with pytest.raises(HTTPException) as exc_info:
            await process(entity=entity)

        assert "unknown" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_handles_none_metadata(self) -> None:
        """Handles entity with None metadata."""
        entity = MagicMock()
        entity.metadata = None

        @require_state("working", operation="process")
        async def process(entity: MagicMock) -> str:
            return "processed"

        with pytest.raises(HTTPException) as exc_info:
            await process(entity=entity)

        assert "unknown" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_preserves_function_metadata(self) -> None:
        """Preserves original function name and docstring."""

        @require_state("working")
        async def my_function(entity: MagicMock) -> str:
            """My docstring."""
            return "result"

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."


# =============================================================================
# with_error_context Tests
# =============================================================================
class TestWithErrorContext:
    """Tests for with_error_context decorator."""

    @pytest.mark.asyncio
    async def test_passes_through_on_success(self) -> None:
        """Returns function result on success."""

        @with_error_context("doing something")
        async def do_something() -> str:
            return "result"

        result = await do_something()
        assert result == "result"

    @pytest.mark.asyncio
    async def test_reraises_http_exception_without_logging(self) -> None:
        """Re-raises HTTP exceptions without additional logging."""

        @with_error_context("doing something")
        async def do_something() -> str:
            raise HTTPException(status_code=404, detail="Not found")

        with patch("sibyl.api.decorators.log") as mock_log:
            with pytest.raises(HTTPException) as exc_info:
                await do_something()

            assert exc_info.value.status_code == 404
            mock_log.exception.assert_not_called()

    @pytest.mark.asyncio
    async def test_logs_and_reraises_other_exceptions(self) -> None:
        """Logs context and re-raises non-HTTP exceptions."""

        @with_error_context("creating entity")
        async def create_entity() -> str:
            raise ValueError("Something went wrong")

        with patch("sibyl.api.decorators.log") as mock_log:
            with pytest.raises(ValueError):
                await create_entity()

            mock_log.exception.assert_called_once()
            call_args = mock_log.exception.call_args
            assert call_args[0][0] == "unhandled_error"
            assert call_args[1]["context"] == "creating entity"
            assert call_args[1]["error_type"] == "ValueError"
            assert call_args[1]["error_message"] == "Something went wrong"

    @pytest.mark.asyncio
    async def test_preserves_original_exception(self) -> None:
        """Original exception is preserved when re-raised."""
        original = RuntimeError("original error")

        @with_error_context("processing")
        async def process() -> str:
            raise original

        with pytest.raises(RuntimeError) as exc_info:
            await process()

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_preserves_function_metadata(self) -> None:
        """Preserves original function name and docstring."""

        @with_error_context("doing thing")
        async def my_function() -> str:
            """My docstring."""
            return "result"

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."


# =============================================================================
# Decorator Composition Tests
# =============================================================================
class TestDecoratorComposition:
    """Tests for combining multiple decorators."""

    @pytest.mark.asyncio
    async def test_handle_not_found_with_log_operation(self) -> None:
        """Decorators work together correctly."""

        @log_operation("get_agent")
        @handle_not_found("Agent", "agent_id")
        async def get_agent(agent_id: str) -> dict:
            raise EntityNotFoundError("Agent", agent_id)

        with patch("sibyl.api.decorators.log") as mock_log:
            with pytest.raises(HTTPException) as exc_info:
                await get_agent(agent_id="agent_123")

            # Should be 404 from handle_not_found
            assert exc_info.value.status_code == 404

            # log_operation should log HTTP error at debug level
            mock_log.debug.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_decorators_together(self) -> None:
        """All decorators can be stacked."""
        entity = MagicMock()
        entity.metadata = {"status": "working"}

        @with_error_context("processing agent")
        @log_operation("process_agent")
        @require_state("working", operation="process")
        @handle_not_found("Agent", "agent_id")
        async def process_agent(agent_id: str, entity: MagicMock) -> dict:
            return {"id": agent_id, "processed": True}

        with patch("sibyl.api.decorators.log"):
            result = await process_agent(agent_id="agent_123", entity=entity)

        assert result == {"id": "agent_123", "processed": True}


# =============================================================================
# Usage Pattern Tests
# =============================================================================
class TestUsagePatterns:
    """Tests demonstrating intended usage patterns."""

    @pytest.mark.asyncio
    async def test_typical_route_handler_pattern(self) -> None:
        """Demonstrates typical route handler decoration."""
        mock_manager = AsyncMock()
        mock_manager.get.return_value = {"id": "agent_123", "name": "Test Agent"}

        @log_operation("get_agent")
        @handle_not_found("Agent", "agent_id")
        async def get_agent(agent_id: str) -> dict:
            return await mock_manager.get(agent_id)

        with patch("sibyl.api.decorators.log"):
            result = await get_agent(agent_id="agent_123")

        assert result["id"] == "agent_123"
        mock_manager.get.assert_called_once_with("agent_123")

    @pytest.mark.asyncio
    async def test_state_transition_pattern(self) -> None:
        """Demonstrates state validation for transitions."""
        entity = MagicMock()
        entity.metadata = {"status": "working"}

        @require_state("working", "waiting_approval", operation="pause agent")
        async def pause_agent(agent_id: str, entity: MagicMock) -> dict:
            return {"id": agent_id, "status": "paused"}

        result = await pause_agent(agent_id="agent_123", entity=entity)
        assert result["status"] == "paused"

    @pytest.mark.asyncio
    async def test_error_context_for_complex_operations(self) -> None:
        """Demonstrates error context for debugging."""

        @with_error_context("bulk entity creation")
        async def create_bulk(items: list) -> list:
            results = []
            for item in items:
                results.append({"id": item, "created": True})
            return results

        result = await create_bulk(["a", "b", "c"])
        assert len(result) == 3
