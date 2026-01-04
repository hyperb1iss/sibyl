"""Tests for structured authorization errors."""

from sibyl.auth.errors import (
    AuthErrorCode,
    AuthorizationError,
    NoOrgContextError,
    OrgAccessDeniedError,
    OwnershipRequiredError,
    ProjectAccessDeniedError,
    ResourceAccessDeniedError,
)


class TestAuthErrorCode:
    """Tests for AuthErrorCode enum."""

    def test_error_codes_are_strings(self) -> None:
        """Error codes should be string values."""
        assert AuthErrorCode.NO_ORG_CONTEXT == "no_org_context"
        assert AuthErrorCode.PROJECT_ACCESS_DENIED == "project_access_denied"
        assert AuthErrorCode.ORG_ACCESS_DENIED == "org_access_denied"


class TestAuthorizationError:
    """Tests for base AuthorizationError."""

    def test_basic_error(self) -> None:
        """Basic error with code and message."""
        error = AuthorizationError(
            code=AuthErrorCode.FORBIDDEN,
            message="Access denied",
        )
        assert error.status_code == 403
        assert error.detail["error"] == "forbidden"
        assert error.detail["message"] == "Access denied"
        assert "details" not in error.detail

    def test_error_with_details(self) -> None:
        """Error with additional details."""
        error = AuthorizationError(
            code=AuthErrorCode.INSUFFICIENT_PERMISSIONS,
            message="Missing permissions",
            details={"resource": "task_123", "action": "delete"},
        )
        assert error.detail["details"]["resource"] == "task_123"
        assert error.detail["details"]["action"] == "delete"


class TestNoOrgContextError:
    """Tests for NoOrgContextError."""

    def test_default_message(self) -> None:
        """Default action message."""
        error = NoOrgContextError()
        assert error.status_code == 403
        assert error.detail["error"] == "no_org_context"
        assert "perform this action" in error.detail["message"]

    def test_custom_action(self) -> None:
        """Custom action in message."""
        error = NoOrgContextError("access the graph")
        assert "access the graph" in error.detail["message"]

    def test_includes_hint(self) -> None:
        """Should include hint for resolution."""
        error = NoOrgContextError()
        assert "hint" in error.detail["details"]


class TestOrgAccessDeniedError:
    """Tests for OrgAccessDeniedError."""

    def test_required_role_only(self) -> None:
        """Error with only required role."""
        error = OrgAccessDeniedError(required_role="admin")
        assert error.detail["error"] == "org_access_denied"
        assert error.detail["details"]["required_role"] == "admin"
        assert "actual_role" not in error.detail["details"]

    def test_with_actual_role(self) -> None:
        """Error with actual role included."""
        error = OrgAccessDeniedError(required_role="admin", actual_role="member")
        assert error.detail["details"]["actual_role"] == "member"

    def test_with_org_id(self) -> None:
        """Error with org ID included."""
        error = OrgAccessDeniedError(required_role="owner", org_id="org_123")
        assert error.detail["details"]["org_id"] == "org_123"


class TestProjectAccessDeniedError:
    """Tests for ProjectAccessDeniedError."""

    def test_basic_error(self) -> None:
        """Basic project access error."""
        error = ProjectAccessDeniedError(
            project_id="proj_abc",
            required_role="contributor",
        )
        assert error.status_code == 403
        assert error.detail["error"] == "project_access_denied"
        assert error.detail["details"]["project_id"] == "proj_abc"
        assert error.detail["details"]["required_role"] == "contributor"

    def test_with_actual_role(self) -> None:
        """Error with actual role."""
        error = ProjectAccessDeniedError(
            project_id="proj_abc",
            required_role="maintainer",
            actual_role="viewer",
        )
        assert error.detail["details"]["actual_role"] == "viewer"


class TestResourceAccessDeniedError:
    """Tests for ResourceAccessDeniedError."""

    def test_basic_error(self) -> None:
        """Basic resource access error."""
        error = ResourceAccessDeniedError(
            resource_type="task",
            resource_id="task_123",
        )
        assert error.detail["error"] == "resource_access_denied"
        assert error.detail["details"]["resource_type"] == "task"
        assert error.detail["details"]["resource_id"] == "task_123"

    def test_with_reason(self) -> None:
        """Error with reason."""
        error = ResourceAccessDeniedError(
            resource_type="document",
            resource_id="doc_456",
            reason="Private document",
        )
        assert error.detail["details"]["reason"] == "Private document"


class TestOwnershipRequiredError:
    """Tests for OwnershipRequiredError."""

    def test_default_action(self) -> None:
        """Default modify action."""
        error = OwnershipRequiredError(
            resource_type="project",
            resource_id="proj_abc",
        )
        assert error.detail["error"] == "ownership_required"
        assert "modify" in error.detail["message"]

    def test_custom_action(self) -> None:
        """Custom action in message."""
        error = OwnershipRequiredError(
            resource_type="agent",
            resource_id="agent_123",
            action="terminate",
        )
        assert "terminate" in error.detail["message"]
        assert "agent" in error.detail["message"]
