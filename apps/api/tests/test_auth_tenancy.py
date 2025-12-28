"""Tests for auth tenancy module."""

import pytest

from sibyl.auth.tenancy import MissingOrganizationError, resolve_group_id


class TestMissingOrganizationError:
    """Tests for MissingOrganizationError exception."""

    def test_error_is_exception(self) -> None:
        """MissingOrganizationError should be an Exception subclass."""
        assert issubclass(MissingOrganizationError, Exception)

    def test_error_message(self) -> None:
        """Test error message is preserved."""
        error = MissingOrganizationError("custom message")
        assert str(error) == "custom message"


class TestResolveGroupId:
    """Tests for resolve_group_id function."""

    def test_returns_org_from_claims(self) -> None:
        """resolve_group_id should return org claim value."""
        claims = {"org": "org_12345", "sub": "user_abc"}
        result = resolve_group_id(claims)
        assert result == "org_12345"

    def test_converts_org_to_string(self) -> None:
        """resolve_group_id should convert org to string."""
        claims = {"org": 12345}  # Numeric org
        result = resolve_group_id(claims)
        assert result == "12345"
        assert isinstance(result, str)

    def test_raises_on_none_claims(self) -> None:
        """resolve_group_id should raise when claims is None."""
        with pytest.raises(MissingOrganizationError) as exc_info:
            resolve_group_id(None)
        assert "Organization context required" in str(exc_info.value)

    def test_raises_on_empty_claims(self) -> None:
        """resolve_group_id should raise when claims is empty dict."""
        with pytest.raises(MissingOrganizationError):
            resolve_group_id({})

    def test_raises_on_missing_org_key(self) -> None:
        """resolve_group_id should raise when org key is missing."""
        claims = {"sub": "user_abc", "exp": 12345}
        with pytest.raises(MissingOrganizationError):
            resolve_group_id(claims)

    def test_raises_on_none_org_value(self) -> None:
        """resolve_group_id should raise when org value is None."""
        claims = {"org": None, "sub": "user_abc"}
        with pytest.raises(MissingOrganizationError):
            resolve_group_id(claims)

    def test_raises_on_empty_org_value(self) -> None:
        """resolve_group_id should raise when org value is empty string."""
        claims = {"org": "", "sub": "user_abc"}
        with pytest.raises(MissingOrganizationError):
            resolve_group_id(claims)
