"""Tests for configuration security validation."""

import pytest

from sibyl.config import Settings


class TestDisableAuthSecurity:
    """Tests for disable_auth security validation."""

    def test_disable_auth_allowed_in_development(self) -> None:
        """disable_auth should be allowed in development environment."""
        settings = Settings(
            environment="development",
            disable_auth=True,
        )
        assert settings.disable_auth is True
        assert settings.environment == "development"

    def test_disable_auth_forbidden_in_production(self) -> None:
        """disable_auth=True should raise error in production."""
        with pytest.raises(ValueError, match="disable_auth=True is forbidden in production"):
            Settings(
                environment="production",
                disable_auth=True,
            )

    def test_disable_auth_allowed_in_staging(self) -> None:
        """disable_auth should be allowed in staging for testing."""
        settings = Settings(
            environment="staging",
            disable_auth=True,
        )
        assert settings.disable_auth is True

    def test_auth_enabled_works_everywhere(self) -> None:
        """disable_auth=False should work in all environments."""
        for env in ["development", "staging", "production"]:
            settings = Settings(
                environment=env,  # type: ignore[arg-type]
                disable_auth=False,
            )
            assert settings.disable_auth is False

    def test_default_environment_is_development(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default environment should be development."""
        # Clear env vars to test actual defaults
        monkeypatch.delenv("SIBYL_ENVIRONMENT", raising=False)
        settings = Settings()
        assert settings.environment == "development"

    def test_default_disable_auth_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default disable_auth should be False."""
        # Clear env vars to test actual defaults
        monkeypatch.delenv("SIBYL_DISABLE_AUTH", raising=False)
        settings = Settings()
        assert settings.disable_auth is False


class TestEnvironmentValidation:
    """Tests for environment field validation."""

    def test_valid_environments(self) -> None:
        """Valid environments should be accepted."""
        for env in ["development", "staging", "production"]:
            settings = Settings(environment=env)  # type: ignore[arg-type]
            assert settings.environment == env

    def test_invalid_environment_rejected(self) -> None:
        """Invalid environment values should be rejected."""
        with pytest.raises(ValueError):
            Settings(environment="dev")  # type: ignore[arg-type]

        with pytest.raises(ValueError):
            Settings(environment="prod")  # type: ignore[arg-type]

        with pytest.raises(ValueError):
            Settings(environment="test")  # type: ignore[arg-type]
