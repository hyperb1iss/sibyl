"""Secure error handling for API responses.

This module provides utilities for returning safe error messages to clients
while logging full details for debugging. Never expose internal exceptions
directly to API clients.
"""

import uuid
from typing import NoReturn

import structlog
from fastapi import HTTPException

log = structlog.get_logger()

# Generic messages for different error categories
INTERNAL_ERROR = "An internal error occurred. Please try again later."
VALIDATION_ERROR = "Invalid request data."
NOT_FOUND_ERROR = "The requested resource was not found."
CONFLICT_ERROR = "The operation conflicts with the current state."
AUTH_ERROR = "Authentication failed."


def raise_internal_error(
    exc: Exception,
    *,
    context: str | None = None,
    log_details: dict | None = None,
) -> NoReturn:
    """Raise a 500 error with a safe message while logging full details.

    Args:
        exc: The original exception (logged but not exposed)
        context: Human-readable context for logs (e.g., "creating entity")
        log_details: Additional details to include in logs

    Raises:
        HTTPException: 500 with generic message
    """
    error_id = str(uuid.uuid4())[:8]

    log.error(
        "internal_error",
        error_id=error_id,
        context=context,
        error_type=type(exc).__name__,
        error_message=str(exc),
        **(log_details or {}),
    )

    raise HTTPException(
        status_code=500,
        detail=f"{INTERNAL_ERROR} (ref: {error_id})",
    ) from exc


def raise_validation_error(
    message: str | None = None,
    *,
    exc: Exception | None = None,
    context: str | None = None,
) -> NoReturn:
    """Raise a 400 error with a safe validation message.

    Args:
        message: Safe user-facing message (or uses default)
        exc: Optional original exception (for logging only)
        context: Human-readable context for logs

    Raises:
        HTTPException: 400 with validation message
    """
    if exc:
        log.warning(
            "validation_error",
            context=context,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    raise HTTPException(
        status_code=400,
        detail=message or VALIDATION_ERROR,
    ) from exc


def raise_not_found(
    resource: str,
    *,
    resource_id: str | None = None,
) -> NoReturn:
    """Raise a 404 error for a missing resource.

    Args:
        resource: Type of resource (e.g., "entity", "task")
        resource_id: Optional ID (will be logged but may be shown carefully)

    Raises:
        HTTPException: 404 with safe message
    """
    log.info("resource_not_found", resource=resource, resource_id=resource_id)

    # For 404s, we can be slightly more specific
    detail = f"{resource.capitalize()} not found"
    if resource_id:
        detail = f"{resource.capitalize()} not found: {resource_id}"

    raise HTTPException(status_code=404, detail=detail)


def raise_conflict(
    message: str | None = None,
    *,
    exc: Exception | None = None,
    context: str | None = None,
) -> NoReturn:
    """Raise a 409 conflict error with a safe message.

    Args:
        message: Safe user-facing message (or uses default)
        exc: Optional original exception (for logging only)
        context: Human-readable context for logs

    Raises:
        HTTPException: 409 with conflict message
    """
    if exc:
        log.warning(
            "conflict_error",
            context=context,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    raise HTTPException(
        status_code=409,
        detail=message or CONFLICT_ERROR,
    ) from exc


def raise_auth_error(
    message: str | None = None,
    *,
    exc: Exception | None = None,
    context: str | None = None,
) -> NoReturn:
    """Raise a 401 authentication error with a safe message.

    Args:
        message: Safe user-facing message (or uses default)
        exc: Optional original exception (for logging only)
        context: Human-readable context for logs

    Raises:
        HTTPException: 401 with auth error message
    """
    if exc:
        log.warning(
            "auth_error",
            context=context,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    raise HTTPException(
        status_code=401,
        detail=message or AUTH_ERROR,
    ) from exc


def sanitize_error_message(exc: Exception) -> str:
    """Extract a safe error message from an exception.

    This tries to determine if the exception message is safe to show
    to clients. If uncertain, returns a generic message.

    Args:
        exc: The exception to sanitize

    Returns:
        A safe error message string
    """
    msg = str(exc)

    # Patterns that indicate internal/sensitive information
    unsafe_patterns = [
        "/",  # File paths
        "\\",  # Windows paths
        "Traceback",
        "File ",
        "line ",
        "Error:",
        "Exception:",
        "password",
        "secret",
        "token",
        "key",
        "credential",
        "sql",
        "query",
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
    ]

    msg_lower = msg.lower()
    for pattern in unsafe_patterns:
        if pattern.lower() in msg_lower:
            return VALIDATION_ERROR

    # If message is very long, it's probably a stack trace
    if len(msg) > 200:
        return VALIDATION_ERROR

    return msg
