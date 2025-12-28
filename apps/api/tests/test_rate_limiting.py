"""Tests for rate limiting configuration."""

from sibyl.api.rate_limit import RATE_LIMITS, get_rate_limit, limiter


class TestRateLimitConfiguration:
    """Tests for rate limit configuration."""

    def test_rate_limits_defined(self) -> None:
        """All rate limit types should be defined."""
        assert "auth" in RATE_LIMITS
        assert "device_poll" in RATE_LIMITS
        assert "api" in RATE_LIMITS
        assert "search" in RATE_LIMITS
        assert "admin" in RATE_LIMITS
        assert "crawl" in RATE_LIMITS

    def test_auth_limits_are_strict(self) -> None:
        """Auth endpoints should have strict limits."""
        auth_limit = RATE_LIMITS["auth"]
        # Auth should be limited to prevent brute force
        assert auth_limit in {"5/minute", "10/minute"}

    def test_api_limits_are_reasonable(self) -> None:
        """API endpoints should have reasonable limits."""
        api_limit = RATE_LIMITS["api"]
        # Should allow ~100 requests per minute for normal use
        assert api_limit == "100/minute"

    def test_get_rate_limit_returns_correct_limit(self) -> None:
        """get_rate_limit should return correct limit for known types."""
        assert get_rate_limit("auth") == RATE_LIMITS["auth"]
        assert get_rate_limit("api") == RATE_LIMITS["api"]

    def test_get_rate_limit_defaults_to_api(self) -> None:
        """Unknown types should default to API limit."""
        assert get_rate_limit("unknown") == RATE_LIMITS["api"]

    def test_limiter_configured(self) -> None:
        """Limiter should be properly configured."""
        assert limiter is not None
        # Key function should be set
        assert limiter._key_func is not None


class TestRateLimitKeyExtraction:
    """Tests for rate limit key extraction."""

    def test_key_uses_user_id_when_authenticated(self) -> None:
        """Authenticated requests should use user ID as key."""
        from unittest.mock import MagicMock

        from sibyl.api.rate_limit import _get_key

        request = MagicMock()
        request.state.jwt_claims = {"sub": "user-123"}

        key = _get_key(request)
        assert key == "user:user-123"

    def test_key_uses_ip_when_anonymous(self) -> None:
        """Anonymous requests should use IP as key."""
        from unittest.mock import MagicMock

        from sibyl.api.rate_limit import _get_key

        request = MagicMock()
        request.state.jwt_claims = None
        request.client.host = "192.168.1.1"
        # Ensure headers are accessible for slowapi
        request.headers = {}
        request.scope = {"type": "http"}

        key = _get_key(request)
        # Should fall back to IP
        assert key == "192.168.1.1"


class TestRateLimitValues:
    """Tests for rate limit value formats."""

    def test_all_limits_have_valid_format(self) -> None:
        """All rate limits should be in valid format (N/unit)."""
        import re

        pattern = re.compile(r"^\d+/(second|minute|hour|day)$")
        for limit_type, limit_value in RATE_LIMITS.items():
            assert pattern.match(limit_value), f"Invalid format for {limit_type}: {limit_value}"

    def test_auth_stricter_than_api(self) -> None:
        """Auth limits should be stricter than API limits."""
        # Parse the numbers from the limits
        auth_num = int(RATE_LIMITS["auth"].split("/")[0])
        api_num = int(RATE_LIMITS["api"].split("/")[0])

        # Auth should allow fewer requests
        assert auth_num < api_num
