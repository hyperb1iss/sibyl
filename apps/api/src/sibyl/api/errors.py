"""Secure error handling for API responses.

This module provides utilities for returning safe error messages to clients
while logging full details for debugging. Never expose internal exceptions
directly to API clients.

Two patterns are provided:
1. raise_* functions - Raise exceptions directly (use in helper functions)
2. Factory functions - Return HTTPException instances (use with `raise`)

Example usage:
    # Pattern 1: Direct raise
    raise_not_found("Task", resource_id=task_id)

    # Pattern 2: Factory (for inline conditionals)
    if not entity:
        raise not_found("Task", task_id)
"""

import re
import uuid
from typing import Any

import structlog
from fastapi import HTTPException, Request, status

log = structlog.get_logger()

# Generic messages for different error categories
INTERNAL_ERROR = "An internal error occurred. Please try again later."
VALIDATION_ERROR = "Invalid request data."
NOT_FOUND_ERROR = "The requested resource was not found."
CONFLICT_ERROR = "The operation conflicts with the current state."
AUTH_ERROR = "Authentication failed."
FORBIDDEN_ERROR = "You don't have permission to perform this action."
NO_ORG_CONTEXT = "Organization context required."
REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PREFIX = "req_"
SAFE_DETAIL_FIELDS = frozenset(
    {
        "actual",
        "entity_type",
        "expected",
        "field",
        "relationship_type",
        "remediation",
        "request_id",
    }
)
_SENSITIVE_DETAIL_PATTERNS = (
    re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE|MATCH|RELATE)\b", re.IGNORECASE),
    re.compile(r"(?:/[\w.-]+){2,}"),
    re.compile(r"[A-Za-z]:\\"),
    re.compile(r"\b(?:token|secret|password|credential|api[_-]?key)\b", re.IGNORECASE),
)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


# =============================================================================
# Factory Functions (return HTTPException for inline `raise`)
# =============================================================================
def not_found(resource: str, resource_id: str | None = None) -> HTTPException:
    """Create a 404 exception for a missing resource.

    Args:
        resource: Type of resource (e.g., "Task", "Project", "Entity")
        resource_id: Optional ID to include in message

    Returns:
        HTTPException ready to be raised

    Example:
        if not entity:
            raise not_found("Task", task_id)
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
            raise no_org_context("list projects")
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


def constraint_violation(
    message: str,
    *,
    remediation: str | None = None,
    details: dict[str, object] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=safe_error_payload(
            error="constraint_violation",
            message=message,
            remediation=remediation,
            details=details,
        ),
    )


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
def project_not_found(project_id: str) -> HTTPException:
    """Create 404 for missing project."""
    return not_found("Project", project_id)


def source_not_found(source_id: str) -> HTTPException:
    """Create 404 for missing source."""
    return not_found("Source", source_id)


# =============================================================================
# Error ID Generation
# =============================================================================
def generate_error_id() -> str:
    """Generate a short error ID for tracking.

    Returns:
        8-character hex string
    """
    return str(uuid.uuid4())[:8]


def generate_request_id() -> str:
    return f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex[:12]}"


def get_request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    if isinstance(value, str) and value:
        return value
    header_value = request.headers.get(REQUEST_ID_HEADER)
    if header_value and _REQUEST_ID_RE.fullmatch(header_value):
        return header_value
    return generate_request_id()


def safe_error_payload(
    *,
    error: str,
    message: str,
    request_id: str | None = None,
    remediation: str | None = None,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "error": _safe_token(error) or "api_error",
        "message": sanitize_error_text(message),
    }
    if request_id:
        payload["request_id"] = request_id
    if remediation:
        payload["remediation"] = sanitize_error_text(remediation)
    safe_details = _safe_details(details or {})
    if safe_details:
        payload["details"] = safe_details
    return payload


def http_exception_payload(exc: HTTPException, request_id: str) -> dict[str, object]:
    detail = exc.detail
    if isinstance(detail, dict):
        raw_error = str(
            detail.get("error") or detail.get("code") or _error_code_for_status(exc.status_code)
        )
        raw_message = str(detail.get("message") or _message_for_status(exc.status_code))
        raw_remediation = detail.get("remediation")
        raw_details = detail.get("details")
        details = raw_details if isinstance(raw_details, dict) else {}
        return safe_error_payload(
            error=raw_error,
            message=_safe_message_for_status(raw_message, exc.status_code),
            request_id=request_id,
            remediation=str(raw_remediation) if raw_remediation else _remediation_for(raw_error),
            details=details,
        )

    message = str(detail) if detail else _message_for_status(exc.status_code)
    error = _error_code_for_status(exc.status_code)
    return safe_error_payload(
        error=error,
        message=_safe_message_for_status(message, exc.status_code),
        request_id=request_id,
        remediation=_remediation_for(error),
    )


def internal_error_payload(request_id: str) -> dict[str, object]:
    return safe_error_payload(
        error="internal_error",
        message=INTERNAL_ERROR,
        request_id=request_id,
        remediation="Retry the command or inspect server logs with this request ID.",
    )


def validation_error_payload(
    errors: list[dict[str, Any]],
    *,
    request_id: str,
) -> dict[str, object]:
    first = errors[0] if errors else {}
    location = first.get("loc") if isinstance(first, dict) else None
    field = ".".join(str(part) for part in location) if isinstance(location, tuple | list) else None
    details: dict[str, object] = {}
    if field:
        details["field"] = field
    if isinstance(first, dict) and first.get("type"):
        details["expected"] = first["type"]
    return safe_error_payload(
        error="validation_error",
        message=VALIDATION_ERROR,
        request_id=request_id,
        remediation="Check the command arguments and try again.",
        details=details,
    )


def sanitize_error_text(message: str) -> str:
    if not message:
        return VALIDATION_ERROR
    if len(message) > 200:
        return VALIDATION_ERROR
    for pattern in _SENSITIVE_DETAIL_PATTERNS:
        if pattern.search(message):
            return VALIDATION_ERROR
    return message


def _safe_message_for_status(message: str, status_code: int) -> str:
    sanitized = sanitize_error_text(message)
    if sanitized == VALIDATION_ERROR and status_code not in {
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    }:
        return _message_for_status(status_code)
    return sanitized


def _safe_token(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_")
    return safe[:80]


def _safe_details(details: dict[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in details.items():
        mapped_key = _map_detail_key(str(key))
        if mapped_key not in SAFE_DETAIL_FIELDS or value is None:
            continue
        safe_value = sanitize_error_text(str(value))
        if safe_value != VALIDATION_ERROR:
            safe[mapped_key] = safe_value
    return safe


def _map_detail_key(key: str) -> str:
    if key == "required_role":
        return "expected"
    if key == "actual_role":
        return "actual"
    return key


def _error_code_for_status(status_code: int) -> str:
    if status_code == status.HTTP_400_BAD_REQUEST:
        return "invalid_request"
    if status_code == status.HTTP_401_UNAUTHORIZED:
        return "authentication_required"
    if status_code == status.HTTP_403_FORBIDDEN:
        return "forbidden"
    if status_code == status.HTTP_404_NOT_FOUND:
        return "not_found"
    if status_code == status.HTTP_409_CONFLICT:
        return "conflict"
    if status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        return "rate_limited"
    if status_code == status.HTTP_422_UNPROCESSABLE_CONTENT:
        return "validation_error"
    if status_code >= 500:
        return "internal_error"
    return "api_error"


def _message_for_status(status_code: int) -> str:
    if status_code == status.HTTP_400_BAD_REQUEST:
        return VALIDATION_ERROR
    if status_code == status.HTTP_401_UNAUTHORIZED:
        return AUTH_ERROR
    if status_code == status.HTTP_403_FORBIDDEN:
        return FORBIDDEN_ERROR
    if status_code == status.HTTP_404_NOT_FOUND:
        return NOT_FOUND_ERROR
    if status_code == status.HTTP_409_CONFLICT:
        return CONFLICT_ERROR
    if status_code >= 500:
        return INTERNAL_ERROR
    return "Request failed."


def _remediation_for(error: str) -> str | None:
    return {
        "authentication_required": "Run 'sibyl auth login' or set SIBYL_AUTH_TOKEN.",
        "conflict": "Refresh the resource state and retry the operation.",
        "constraint_violation": "Use a different title or update the existing entity.",
        "forbidden": "Check your organization and project permissions.",
        "invalid_request": "Check the command arguments and try again.",
        "not_found": "Check the ID or prefix and try again.",
        "project_access_denied": "Check your project permissions or switch context.",
        "rate_limited": "Wait briefly, then retry the command.",
        "validation_error": "Check the command arguments and try again.",
    }.get(error)
