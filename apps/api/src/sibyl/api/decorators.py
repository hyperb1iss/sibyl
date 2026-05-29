"""Route handler decorators for common patterns.

These decorators reduce boilerplate in route handlers by handling
common concerns like exception translation and logging.

Usage:
    @router.get("/tasks/{task_id}")
    @handle_not_found("Task", "task_id")
    async def get_task(task_id: str) -> TaskResponse:
        task = await manager.get(task_id)
        return TaskResponse.from_entity(task)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import structlog
from fastapi import HTTPException

from sibyl_core.errors import EntityNotFoundError, InvalidTransitionError

log = structlog.get_logger()

P = ParamSpec("P")
R = TypeVar("R")


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
