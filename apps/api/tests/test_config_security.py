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
            # Production requires non-default passwords
            kwargs: dict[str, object] = {"environment": env, "disable_auth": False}
            if env == "production":
                kwargs["falkordb_password"] = "secure_falkordb_pw"
                kwargs["postgres_password"] = "secure_postgres_pw"
            settings = Settings(**kwargs)  # type: ignore[arg-type]
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
            # Production requires non-default passwords
            kwargs: dict[str, object] = {"environment": env}
            if env == "production":
                kwargs["falkordb_password"] = "secure_falkordb_pw"
                kwargs["postgres_password"] = "secure_postgres_pw"
            settings = Settings(**kwargs)  # type: ignore[arg-type]
            assert settings.environment == env

    def test_invalid_environment_rejected(self) -> None:
        """Invalid environment values should be rejected."""
        with pytest.raises(ValueError):
            Settings(environment="dev")  # type: ignore[arg-type]

        with pytest.raises(ValueError):
            Settings(environment="prod")  # type: ignore[arg-type]

        with pytest.raises(ValueError):
            Settings(environment="test")  # type: ignore[arg-type]


class TestProductionPasswordSecurity:
    """Tests for production password validation."""

    def test_default_falkordb_password_forbidden_in_production(self) -> None:
        """Default FalkorDB password should be rejected in production."""
        with pytest.raises(ValueError, match="Default FalkorDB password"):
            Settings(
                environment="production",
                falkordb_password="conventions",
                postgres_password="secure_pw",
            )

    def test_default_postgres_password_forbidden_in_production(self) -> None:
        """Default PostgreSQL password should be rejected in production."""
        with pytest.raises(ValueError, match="Default PostgreSQL password"):
            Settings(
                environment="production",
                falkordb_password="secure_pw",
                postgres_password="sibyl_dev",
            )

    def test_default_passwords_allowed_in_development(self) -> None:
        """Default passwords should be allowed in development."""
        settings = Settings(
            environment="development",
            falkordb_password="conventions",
            postgres_password="sibyl_dev",
        )
        assert settings.falkordb_password == "conventions"

    def test_secure_passwords_work_in_production(self) -> None:
        """Non-default passwords should work in production."""
        settings = Settings(
            environment="production",
            falkordb_password="my_secure_falkordb",
            postgres_password="my_secure_postgres",
        )
        assert settings.environment == "production"
