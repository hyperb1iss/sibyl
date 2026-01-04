"""Structured authorization errors for consistent API responses.

All 403 errors should use these classes for frontend-parseable responses.
"""

from enum import StrEnum
from typing import Any

from fastapi import HTTPException, status


class AuthErrorCode(StrEnum):
    """Error codes for authorization failures."""

    # Organization errors
    NO_ORG_CONTEXT = "no_org_context"
    ORG_ACCESS_DENIED = "org_access_denied"
    ORG_ROLE_REQUIRED = "org_role_required"

    # Project errors
    PROJECT_ACCESS_DENIED = "project_access_denied"
    PROJECT_NOT_FOUND = "project_not_found"

    # Resource errors
    RESOURCE_ACCESS_DENIED = "resource_access_denied"
    OWNERSHIP_REQUIRED = "ownership_required"

    # Generic
    FORBIDDEN = "forbidden"
    INSUFFICIENT_PERMISSIONS = "insufficient_permissions"


class AuthorizationError(HTTPException):
    """Base class for structured 403 errors.

    All authorization errors include:
    - error: Machine-readable error code
    - message: Human-readable description
    - details: Additional context (optional)
    """

    def __init__(
        self,
        code: AuthErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ):
        detail = {
            "error": code.value,
            "message": message,
        }
        if details:
            detail["details"] = details
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


class NoOrgContextError(AuthorizationError):
    """Raised when org context is required but missing."""

    def __init__(self, action: str = "perform this action"):
        super().__init__(
            code=AuthErrorCode.NO_ORG_CONTEXT,
            message=f"Organization context required to {action}",
            details={"hint": "Ensure you have selected an organization"},
        )


class OrgAccessDeniedError(AuthorizationError):
    """Raised when user lacks org-level access."""

    def __init__(
        self,
        required_role: str,
        actual_role: str | None = None,
        org_id: str | None = None,
    ):
        details: dict[str, Any] = {"required_role": required_role}
        if actual_role:
            details["actual_role"] = actual_role
        if org_id:
            details["org_id"] = org_id

        super().__init__(
            code=AuthErrorCode.ORG_ACCESS_DENIED,
            message=f"Requires {required_role} role in organization",
            details=details,
        )


class ProjectAccessDeniedError(AuthorizationError):
    """Raised when user lacks project-level access."""

    def __init__(
        self,
        project_id: str,
        required_role: str,
        actual_role: str | None = None,
    ):
        details: dict[str, Any] = {
            "project_id": project_id,
            "required_role": required_role,
        }
        if actual_role:
            details["actual_role"] = actual_role

        super().__init__(
            code=AuthErrorCode.PROJECT_ACCESS_DENIED,
            message=f"Requires {required_role} access to project",
            details=details,
        )


class ResourceAccessDeniedError(AuthorizationError):
    """Raised when user lacks access to a specific resource."""

    def __init__(
        self,
        resource_type: str,
        resource_id: str,
        reason: str | None = None,
    ):
        details: dict[str, Any] = {
            "resource_type": resource_type,
            "resource_id": resource_id,
        }
        if reason:
            details["reason"] = reason

        super().__init__(
            code=AuthErrorCode.RESOURCE_ACCESS_DENIED,
            message=f"Access denied to {resource_type}",
            details=details,
        )


class OwnershipRequiredError(AuthorizationError):
    """Raised when only the resource owner can perform an action."""

    def __init__(
        self,
        resource_type: str,
        resource_id: str,
        action: str = "modify",
    ):
        super().__init__(
            code=AuthErrorCode.OWNERSHIP_REQUIRED,
            message=f"Only the owner can {action} this {resource_type}",
            details={
                "resource_type": resource_type,
                "resource_id": resource_id,
            },
        )


# Convenience functions for quick raises
def require_org_context(action: str = "access this resource") -> None:
    """Raise NoOrgContextError - use in route guards."""
    raise NoOrgContextError(action)


def deny_org_access(
    required_role: str,
    actual_role: str | None = None,
) -> None:
    """Raise OrgAccessDeniedError."""
    raise OrgAccessDeniedError(required_role, actual_role)


def deny_project_access(
    project_id: str,
    required_role: str,
    actual_role: str | None = None,
) -> None:
    """Raise ProjectAccessDeniedError."""
    raise ProjectAccessDeniedError(project_id, required_role, actual_role)
