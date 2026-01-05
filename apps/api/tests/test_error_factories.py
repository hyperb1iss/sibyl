"""Tests for error factory functions.

Covers the factory functions that return HTTPException instances
for use with `raise` in route handlers.
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from sibyl.api.errors import (
    AUTH_ERROR,
    CONFLICT_ERROR,
    FORBIDDEN_ERROR,
    INTERNAL_ERROR,
    NO_ORG_CONTEXT,
    agent_not_found,
    approval_not_found,
    bad_request,
    conflict,
    document_not_found,
    entity_not_found,
    epic_not_found,
    forbidden,
    generate_error_id,
    internal_error,
    log_and_raise_internal,
    no_org_context,
    not_found,
    project_not_found,
    source_not_found,
    task_not_found,
    unauthorized,
)


# =============================================================================
# not_found Tests
# =============================================================================
class TestNotFound:
    """Tests for not_found factory."""

    def test_returns_http_exception(self) -> None:
        """Returns HTTPException, not raises it."""
        result = not_found("Agent", "agent_123")
        assert isinstance(result, HTTPException)

    def test_status_code_404(self) -> None:
        """Returns 404 status code."""
        result = not_found("Task", "task_456")
        assert result.status_code == 404

    def test_detail_with_id(self) -> None:
        """Detail includes resource type and ID."""
        result = not_found("Agent", "agent_abc123")
        assert result.detail == "Agent not found: agent_abc123"

    def test_detail_without_id(self) -> None:
        """Detail works without ID."""
        result = not_found("Entity")
        assert result.detail == "Entity not found"

    def test_can_be_raised(self) -> None:
        """Can be used with raise."""
        with pytest.raises(HTTPException) as exc_info:
            raise not_found("Project", "proj_123")
        assert exc_info.value.status_code == 404
        assert "Project not found: proj_123" in exc_info.value.detail


# =============================================================================
# bad_request Tests
# =============================================================================
class TestBadRequest:
    """Tests for bad_request factory."""

    def test_returns_http_exception(self) -> None:
        """Returns HTTPException instance."""
        result = bad_request("Name is required")
        assert isinstance(result, HTTPException)

    def test_status_code_400(self) -> None:
        """Returns 400 status code."""
        result = bad_request("Invalid data")
        assert result.status_code == 400

    def test_uses_provided_message(self) -> None:
        """Uses the exact message provided."""
        result = bad_request("Custom validation error")
        assert result.detail == "Custom validation error"


# =============================================================================
# forbidden Tests
# =============================================================================
class TestForbidden:
    """Tests for forbidden factory."""

    def test_returns_http_exception(self) -> None:
        """Returns HTTPException instance."""
        result = forbidden()
        assert isinstance(result, HTTPException)

    def test_status_code_403(self) -> None:
        """Returns 403 status code."""
        result = forbidden()
        assert result.status_code == 403

    def test_default_message(self) -> None:
        """Uses default message when none provided."""
        result = forbidden()
        assert result.detail == FORBIDDEN_ERROR

    def test_custom_message(self) -> None:
        """Uses custom message when provided."""
        result = forbidden("Admin access required")
        assert result.detail == "Admin access required"


# =============================================================================
# no_org_context Tests
# =============================================================================
class TestNoOrgContext:
    """Tests for no_org_context factory."""

    def test_returns_http_exception(self) -> None:
        """Returns HTTPException instance."""
        result = no_org_context()
        assert isinstance(result, HTTPException)

    def test_status_code_403(self) -> None:
        """Returns 403 status code."""
        result = no_org_context()
        assert result.status_code == 403

    def test_default_message(self) -> None:
        """Uses default message when no action provided."""
        result = no_org_context()
        assert result.detail == NO_ORG_CONTEXT

    def test_action_in_message(self) -> None:
        """Includes action in message when provided."""
        result = no_org_context("list agents")
        assert result.detail == "Organization context required to list agents"


# =============================================================================
# conflict Tests
# =============================================================================
class TestConflict:
    """Tests for conflict factory."""

    def test_returns_http_exception(self) -> None:
        """Returns HTTPException instance."""
        result = conflict()
        assert isinstance(result, HTTPException)

    def test_status_code_409(self) -> None:
        """Returns 409 status code."""
        result = conflict()
        assert result.status_code == 409

    def test_default_message(self) -> None:
        """Uses default message when none provided."""
        result = conflict()
        assert result.detail == CONFLICT_ERROR

    def test_custom_message(self) -> None:
        """Uses custom message when provided."""
        result = conflict("Resource locked")
        assert result.detail == "Resource locked"


# =============================================================================
# unauthorized Tests
# =============================================================================
class TestUnauthorized:
    """Tests for unauthorized factory."""

    def test_returns_http_exception(self) -> None:
        """Returns HTTPException instance."""
        result = unauthorized()
        assert isinstance(result, HTTPException)

    def test_status_code_401(self) -> None:
        """Returns 401 status code."""
        result = unauthorized()
        assert result.status_code == 401

    def test_default_message(self) -> None:
        """Uses default message when none provided."""
        result = unauthorized()
        assert result.detail == AUTH_ERROR

    def test_custom_message(self) -> None:
        """Uses custom message when provided."""
        result = unauthorized("Token expired")
        assert result.detail == "Token expired"


# =============================================================================
# internal_error Tests
# =============================================================================
class TestInternalError:
    """Tests for internal_error factory."""

    def test_returns_http_exception(self) -> None:
        """Returns HTTPException instance."""
        result = internal_error()
        assert isinstance(result, HTTPException)

    def test_status_code_500(self) -> None:
        """Returns 500 status code."""
        result = internal_error()
        assert result.status_code == 500

    def test_default_message(self) -> None:
        """Uses generic message without error ID."""
        result = internal_error()
        assert result.detail == INTERNAL_ERROR

    def test_includes_error_id(self) -> None:
        """Includes error ID reference when provided."""
        result = internal_error("abc12345")
        assert "ref: abc12345" in result.detail
        assert INTERNAL_ERROR in result.detail


# =============================================================================
# Entity-Specific Helper Tests
# =============================================================================
class TestEntityHelpers:
    """Tests for entity-specific not_found helpers."""

    def test_agent_not_found(self) -> None:
        """agent_not_found creates correct exception."""
        result = agent_not_found("agent_123")
        assert result.status_code == 404
        assert result.detail == "Agent not found: agent_123"

    def test_task_not_found(self) -> None:
        """task_not_found creates correct exception."""
        result = task_not_found("task_456")
        assert result.status_code == 404
        assert result.detail == "Task not found: task_456"

    def test_epic_not_found(self) -> None:
        """epic_not_found creates correct exception."""
        result = epic_not_found("epic_789")
        assert result.status_code == 404
        assert result.detail == "Epic not found: epic_789"

    def test_project_not_found(self) -> None:
        """project_not_found creates correct exception."""
        result = project_not_found("proj_abc")
        assert result.status_code == 404
        assert result.detail == "Project not found: proj_abc"

    def test_source_not_found(self) -> None:
        """source_not_found creates correct exception."""
        result = source_not_found("src_def")
        assert result.status_code == 404
        assert result.detail == "Source not found: src_def"

    def test_document_not_found(self) -> None:
        """document_not_found creates correct exception."""
        result = document_not_found("doc_ghi")
        assert result.status_code == 404
        assert result.detail == "Document not found: doc_ghi"

    def test_entity_not_found(self) -> None:
        """entity_not_found creates correct exception."""
        result = entity_not_found("ent_jkl")
        assert result.status_code == 404
        assert result.detail == "Entity not found: ent_jkl"

    def test_approval_not_found(self) -> None:
        """approval_not_found creates correct exception."""
        result = approval_not_found("apr_mno")
        assert result.status_code == 404
        assert result.detail == "Approval not found: apr_mno"


# =============================================================================
# Error ID Generation Tests
# =============================================================================
class TestGenerateErrorId:
    """Tests for generate_error_id function."""

    def test_returns_string(self) -> None:
        """Returns a string."""
        result = generate_error_id()
        assert isinstance(result, str)

    def test_length_is_8(self) -> None:
        """Returns 8-character string."""
        result = generate_error_id()
        assert len(result) == 8

    def test_unique_ids(self) -> None:
        """Each call returns different ID."""
        ids = [generate_error_id() for _ in range(100)]
        assert len(set(ids)) == 100  # All unique


# =============================================================================
# log_and_raise_internal Tests
# =============================================================================
class TestLogAndRaiseInternal:
    """Tests for log_and_raise_internal function."""

    def test_raises_http_exception(self) -> None:
        """Raises HTTPException with 500 status."""
        with pytest.raises(HTTPException) as exc_info:
            log_and_raise_internal(ValueError("test error"))
        assert exc_info.value.status_code == 500

    def test_includes_error_id_in_detail(self) -> None:
        """Detail includes error reference ID."""
        with pytest.raises(HTTPException) as exc_info:
            log_and_raise_internal(ValueError("test"))
        assert "ref:" in exc_info.value.detail

    def test_logs_error_details(self) -> None:
        """Logs exception details for debugging."""
        with (
            patch("sibyl.api.errors.log") as mock_log,
            pytest.raises(HTTPException),
        ):
            log_and_raise_internal(
                ValueError("original error"),
                context="creating entity",
                entity_id="ent_123",
            )

        mock_log.error.assert_called_once()
        call_kwargs = mock_log.error.call_args
        assert call_kwargs[0][0] == "internal_error"
        assert call_kwargs[1]["context"] == "creating entity"
        assert call_kwargs[1]["error_type"] == "ValueError"
        assert call_kwargs[1]["error_message"] == "original error"
        assert call_kwargs[1]["entity_id"] == "ent_123"

    def test_preserves_original_exception(self) -> None:
        """Original exception is chained."""
        original = ValueError("original")
        with pytest.raises(HTTPException) as exc_info:
            log_and_raise_internal(original)
        assert exc_info.value.__cause__ is original


# =============================================================================
# Usage Pattern Tests
# =============================================================================
class TestUsagePatterns:
    """Tests demonstrating intended usage patterns."""

    def test_inline_conditional_pattern(self) -> None:
        """Factories work with inline conditional raises."""
        entity = None

        with pytest.raises(HTTPException) as exc_info:
            if not entity:
                raise not_found("Entity", "ent_123")

        assert exc_info.value.status_code == 404

    def test_validation_chain_pattern(self) -> None:
        """Factories work in validation chains."""
        org = None
        entity = {"id": "123"}

        with pytest.raises(HTTPException) as exc_info:
            if not org:
                raise no_org_context("access entity")
            if not entity:
                raise not_found("Entity", "123")

        assert exc_info.value.status_code == 403
        assert "Organization context required" in exc_info.value.detail

    def test_permission_check_pattern(self) -> None:
        """Factories work for permission checks."""
        is_admin = False
        is_owner = False

        with pytest.raises(HTTPException) as exc_info:
            if not is_admin and not is_owner:
                raise forbidden("You must be admin or owner")

        assert exc_info.value.status_code == 403
        assert "admin or owner" in exc_info.value.detail
