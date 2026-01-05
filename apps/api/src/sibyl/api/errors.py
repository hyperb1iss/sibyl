"""Secure error handling for API responses.

This module provides utilities for returning safe error messages to clients
while logging full details for debugging. Never expose internal exceptions
directly to API clients.

Two patterns are provided:
1. raise_* functions - Raise exceptions directly (use in helper functions)
2. Factory functions - Return HTTPException instances (use with `raise`)

Example usage:
    # Pattern 1: Direct raise
    raise_not_found("Agent", resource_id=agent_id)

    # Pattern 2: Factory (for inline conditionals)
    if not entity:
        raise not_found("Agent", agent_id)
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
FORBIDDEN_ERROR = "You don't have permission to perform this action."
NO_ORG_CONTEXT = "Organization context required."


# =============================================================================
# Factory Functions (return HTTPException for inline `raise`)
# =============================================================================
def not_found(resource: str, resource_id: str | None = None) -> HTTPException:
    """Create a 404 exception for a missing resource.

    Args:
        resource: Type of resource (e.g., "Agent", "Task", "Entity")
        resource_id: Optional ID to include in message

    Returns:
        HTTPException ready to be raised

    Example:
        if not entity:
            raise not_found("Agent", agent_id)
    """
    detail = f"{resource} not found"
    if resource_id:
        detail = f"{resource} not found: {resource_id}"
    return HTTPException(status_code=404, detail=detail)


def bad_request(message: str) -> HTTPException:
    """Create a 400 exception for invalid requests.

    Args:
        message: User-facing error message

    Returns:
        HTTPException ready to be raised

    Example:
        if not request.name:
            raise bad_request("Name is required")
    """
    return HTTPException(status_code=400, detail=message)


def forbidden(message: str | None = None) -> HTTPException:
    """Create a 403 exception for permission errors.

    Args:
        message: Optional custom message (defaults to generic)

    Returns:
        HTTPException ready to be raised

    Example:
        if not is_admin:
            raise forbidden("Admin access required")
    """
    return HTTPException(status_code=403, detail=message or FORBIDDEN_ERROR)


def no_org_context(action: str | None = None) -> HTTPException:
    """Create a 403 exception for missing organization context.

    Args:
        action: Optional action description for clearer message

    Returns:
        HTTPException ready to be raised

    Example:
        if not ctx.organization:
            raise no_org_context("list agents")
    """
    if action:
        detail = f"Organization context required to {action}"
    else:
        detail = NO_ORG_CONTEXT
    return HTTPException(status_code=403, detail=detail)


def conflict(message: str | None = None) -> HTTPException:
    """Create a 409 exception for conflicts.

    Args:
        message: Optional custom message

    Returns:
        HTTPException ready to be raised

    Example:
        raise conflict("Resource is locked by another process")
    """
    return HTTPException(status_code=409, detail=message or CONFLICT_ERROR)


def unauthorized(message: str | None = None) -> HTTPException:
    """Create a 401 exception for auth failures.

    Args:
        message: Optional custom message

    Returns:
        HTTPException ready to be raised

    Example:
        raise unauthorized("Invalid or expired token")
    """
    return HTTPException(status_code=401, detail=message or AUTH_ERROR)


def internal_error(error_id: str | None = None) -> HTTPException:
    """Create a 500 exception with optional error reference.

    Args:
        error_id: Optional error ID for tracking

    Returns:
        HTTPException ready to be raised

    Example:
        error_id = log_error(exc)
        raise internal_error(error_id)
    """
    detail = INTERNAL_ERROR
    if error_id:
        detail = f"{INTERNAL_ERROR} (ref: {error_id})"
    return HTTPException(status_code=500, detail=detail)


# =============================================================================
# Entity-Specific Helpers
# =============================================================================
def agent_not_found(agent_id: str) -> HTTPException:
    """Create 404 for missing agent."""
    return not_found("Agent", agent_id)


def task_not_found(task_id: str) -> HTTPException:
    """Create 404 for missing task."""
    return not_found("Task", task_id)


def epic_not_found(epic_id: str) -> HTTPException:
    """Create 404 for missing epic."""
    return not_found("Epic", epic_id)


def project_not_found(project_id: str) -> HTTPException:
    """Create 404 for missing project."""
    return not_found("Project", project_id)


def source_not_found(source_id: str) -> HTTPException:
    """Create 404 for missing source."""
    return not_found("Source", source_id)


def document_not_found(document_id: str) -> HTTPException:
    """Create 404 for missing document."""
    return not_found("Document", document_id)


def entity_not_found(entity_id: str) -> HTTPException:
    """Create 404 for missing entity."""
    return not_found("Entity", entity_id)


def approval_not_found(approval_id: str) -> HTTPException:
    """Create 404 for missing approval."""
    return not_found("Approval", approval_id)


# =============================================================================
# Error ID Generation
# =============================================================================
def generate_error_id() -> str:
    """Generate a short error ID for tracking.

    Returns:
        8-character hex string
    """
    return str(uuid.uuid4())[:8]


def log_and_raise_internal(
    exc: Exception,
    *,
    context: str | None = None,
    **extra: object,
) -> NoReturn:
    """Log exception details and raise safe 500 error.

    Combines error logging with safe error response in one call.

    Args:
        exc: The original exception
        context: Human-readable context for logs
        **extra: Additional fields for structured logging

    Raises:
        HTTPException: 500 with generic message and error reference
    """
    error_id = generate_error_id()
    log.error(
        "internal_error",
        error_id=error_id,
        context=context,
        error_type=type(exc).__name__,
        error_message=str(exc),
        **extra,
    )
    raise internal_error(error_id) from exc


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
