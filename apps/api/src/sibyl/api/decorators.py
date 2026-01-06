"""Route handler decorators for common patterns.

These decorators reduce boilerplate in route handlers by handling
common concerns like exception translation and logging.

Usage:
    @router.get("/agents/{agent_id}")
    @handle_not_found("Agent", "agent_id")
    async def get_agent(agent_id: str) -> AgentResponse:
        entity = await manager.get(agent_id)
        return AgentResponse.from_entity(entity)
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import structlog
from fastapi import HTTPException

from sibyl.api.errors import not_found
from sibyl_core.errors import EntityNotFoundError, InvalidTransitionError

log = structlog.get_logger()

P = ParamSpec("P")
R = TypeVar("R")


def handle_not_found(
    entity_type: str,
    id_param: str = "id",
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that catches EntityNotFoundError and raises 404.

    Eliminates the common try/except pattern in route handlers:

        try:
            entity = await manager.get(entity_id)
        except EntityNotFoundError:
            raise HTTPException(status_code=404, detail=f"X not found: {id}")

    Args:
        entity_type: Type name for error message (e.g., "Agent", "Task")
        id_param: Name of the route parameter containing the entity ID

    Returns:
        Decorated function that translates EntityNotFoundError to 404

    Example:
        @router.get("/agents/{agent_id}")
        @handle_not_found("Agent", "agent_id")
        async def get_agent(agent_id: str) -> AgentResponse:
            entity = await manager.get(agent_id)  # If not found, 404 is raised
            return AgentResponse.from_entity(entity)
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await func(*args, **kwargs)
            except EntityNotFoundError:
                entity_id = kwargs.get(id_param)
                raise not_found(entity_type, str(entity_id) if entity_id else None) from None

        return wrapper

    return decorator


def log_operation(
    operation: str | None = None,
    *,
    include_result: bool = False,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that logs operation start, success, and failure.

    Provides structured logging with timing for route handlers.

    Args:
        operation: Operation name for logs (defaults to function name)
        include_result: Whether to log result type on success

    Returns:
        Decorated function with logging

    Example:
        @router.post("/agents")
        @log_operation("spawn_agent")
        async def spawn_agent(request: SpawnRequest) -> AgentResponse:
            ...
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        op_name = operation or func.__name__

        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start = time.monotonic()

            try:
                result = await func(*args, **kwargs)
                elapsed_ms = int((time.monotonic() - start) * 1000)

                log_kwargs: dict[str, object] = {"elapsed_ms": elapsed_ms}
                if include_result:
                    log_kwargs["result_type"] = type(result).__name__

                log.info(f"{op_name}_success", **log_kwargs)
                return result

            except HTTPException:
                # Re-raise HTTP exceptions without logging as errors
                elapsed_ms = int((time.monotonic() - start) * 1000)
                log.debug(f"{op_name}_http_error", elapsed_ms=elapsed_ms)
                raise

            except Exception as e:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                log.exception(
                    f"{op_name}_error",
                    elapsed_ms=elapsed_ms,
                    error_type=type(e).__name__,
                )
                raise

        return wrapper

    return decorator


def require_state(
    *valid_states: str,
    state_getter: Callable[[dict], str] | None = None,
    state_field: str = "status",
    operation: str = "perform this operation",
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that validates entity state before executing handler.

    Checks that the entity is in one of the valid states before proceeding.
    The entity must be passed as a keyword argument to the handler.

    Args:
        *valid_states: Allowed state values
        state_getter: Optional function to extract state from entity metadata
        state_field: Metadata field containing state (default: "status")
        operation: Description for error message

    Returns:
        Decorated function that validates state

    Example:
        @router.post("/agents/{agent_id}/pause")
        @require_state("working", "waiting_approval", operation="pause agent")
        async def pause_agent(agent_id: str, entity: Entity) -> Response:
            ...
    """
    valid_set = set(valid_states)

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            entity = kwargs.get("entity")
            if entity is None:
                # No entity to validate, proceed
                return await func(*args, **kwargs)

            metadata = getattr(entity, "metadata", None) or {}

            if state_getter:
                current_state = state_getter(metadata)
            else:
                current_state = metadata.get(state_field, "unknown")

            if current_state not in valid_set:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cannot {operation} when {state_field} is '{current_state}'. "
                        f"Valid states: {', '.join(sorted(valid_set))}"
                    ),
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def with_error_context(
    context: str,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that adds context to unhandled exceptions.

    Wraps unexpected exceptions with additional context for debugging
    while preserving the original traceback.

    Args:
        context: Description of what the handler was doing

    Returns:
        Decorated function with enhanced error context

    Example:
        @router.delete("/entities/{entity_id}")
        @with_error_context("deleting entity")
        async def delete_entity(entity_id: str) -> None:
            ...
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await func(*args, **kwargs)
            except HTTPException:
                raise
            except Exception as e:
                log.exception(
                    "unhandled_error",
                    context=context,
                    error_type=type(e).__name__,
                    error_message=str(e),
                )
                raise

        return wrapper

    return decorator


def handle_workflow_errors(
    operation: str,
    id_param: str = "task_id",
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that handles common workflow operation errors.

    Translates workflow exceptions to appropriate HTTP responses:
    - EntityNotFoundError → 404 Not Found
    - InvalidTransitionError → 400 Bad Request
    - Other exceptions → 500 with logging

    Args:
        operation: Operation name for error messages (e.g., "start_task")
        id_param: Name of the route parameter containing the entity ID

    Returns:
        Decorated function with workflow error handling

    Example:
        @router.post("/{task_id}/start")
        @handle_workflow_errors("start_task")
        async def start_task(task_id: str) -> TaskActionResponse:
            task = await workflow.start_task(task_id, assignee)
            return TaskActionResponse(...)
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            entity_id = kwargs.get(id_param, "unknown")

            try:
                return await func(*args, **kwargs)

            except EntityNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e

            except InvalidTransitionError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

            except HTTPException:
                raise

            except Exception as e:
                log.exception(
                    f"{operation}_failed",
                    **{id_param: entity_id},
                    error=str(e),
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to {operation.replace('_', ' ')}. Please try again.",
                ) from e

        return wrapper

    return decorator
