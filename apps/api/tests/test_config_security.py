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
            kwargs: dict[str, object] = {
                "environment": env,
                "disable_auth": False,
                "store": "surreal",
                "auth_store": "surreal",
            }
            if env == "production":
                kwargs["surreal_url"] = "ws://surrealdb:8000/rpc"
                kwargs["jwt_secret"] = "test-jwt-secret-0123456789abcdef0123456789abcdef"
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
            kwargs: dict[str, object] = {
                "environment": env,
                "store": "surreal",
                "auth_store": "surreal",
            }
            if env == "production":
                kwargs["surreal_url"] = "ws://surrealdb:8000/rpc"
                kwargs["jwt_secret"] = "test-jwt-secret-0123456789abcdef0123456789abcdef"
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

    def test_in_memory_surreal_forbidden_when_auth_uses_surreal_in_production(self) -> None:
        with pytest.raises(ValueError, match="In-memory SurrealDB is forbidden in production"):
            Settings(
                _env_file=None,
                environment="production",
                store="surreal",
                auth_store="surreal",
                surreal_url="",
            )

    def test_fully_surreal_production_construction(self) -> None:
        settings = Settings(
            environment="production",
            store="surreal",
            auth_store="surreal",
            surreal_url="ws://surrealdb:8000/rpc",
            jwt_secret="test-jwt-secret-0123456789abcdef0123456789abcdef",
        )

        assert settings.fully_surreal is True

    def test_default_surreal_credentials_forbidden_in_production(self) -> None:
        with pytest.raises(ValueError, match="Default SurrealDB credentials are forbidden"):
            Settings(
                environment="production",
                store="surreal",
                auth_store="surreal",
                surreal_url="ws://surrealdb:8000/rpc",
                surreal_username="root",
                surreal_password="sibyl_dev",
            )

    def test_surreal_kv_forbidden_in_production_without_single_writer_opt_in(self) -> None:
        with pytest.raises(ValueError, match="Embedded SurrealDB requires explicit single-writer"):
            Settings(
                _env_file=None,
                environment="production",
                store="surreal",
                auth_store="surreal",
                surreal_url="",
                surreal_data_dir="/var/lib/sibyl/surreal",
                surreal_username="sibyl_admin",
                surreal_password="really_secure_password",
                allow_embedded_single_writer=False,
            )

    def test_surreal_kv_allowed_in_production_with_single_writer_opt_in(self) -> None:
        settings = Settings(
            _env_file=None,
            environment="production",
            store="surreal",
            auth_store="surreal",
            surreal_url="",
            surreal_data_dir="/var/lib/sibyl/surreal",
            surreal_username="sibyl_admin",
            surreal_password="really_secure_password",
            allow_embedded_single_writer=True,
            jwt_secret="test-jwt-secret-0123456789abcdef0123456789abcdef",
        )

        assert settings.resolved_surreal_url == "surrealkv:///var/lib/sibyl/surreal"

    def test_insecure_cookies_forbidden_in_production(self) -> None:
        with pytest.raises(ValueError, match="cookie_secure=False is forbidden"):
            Settings(
                environment="production",
                store="surreal",
                auth_store="surreal",
                surreal_url="ws://surrealdb:8000/rpc",
                surreal_username="sibyl_admin",
                surreal_password="really_secure_password",
                cookie_secure=False,
            )

    def test_placeholder_surreal_credentials_forbidden_in_production(self) -> None:
        with pytest.raises(ValueError, match="Default SurrealDB credentials are forbidden"):
            Settings(
                environment="production",
                store="surreal",
                auth_store="surreal",
                surreal_url="ws://surrealdb:8000/rpc",
                surreal_username="change-me",
                surreal_password="change-me-strong-password",
            )

    def test_non_default_surreal_credentials_allowed_in_production(self) -> None:
        settings = Settings(
            environment="production",
            store="surreal",
            auth_store="surreal",
            surreal_url="ws://surrealdb:8000/rpc",
            surreal_username="sibyl_admin",
            surreal_password="really_secure_password",
            jwt_secret="test-jwt-secret-0123456789abcdef0123456789abcdef",
        )

        assert settings.environment == "production"

    def test_surreal_production_settings_construct(self) -> None:
        """Fully-surreal production settings should construct with a remote URL."""
        settings = Settings(
            environment="production",
            store="surreal",
            auth_store="surreal",
            surreal_url="ws://surrealdb:8000/rpc",
            jwt_secret="test-jwt-secret-0123456789abcdef0123456789abcdef",
        )
        assert settings.environment == "production"


class TestProductionJwtSecret:
    """Tests for the production JWT signing key requirement."""

    def test_missing_jwt_secret_forbidden_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with pytest.raises(ValueError, match="A JWT secret is required in production"):
            Settings(
                _env_file=None,
                environment="production",
                store="surreal",
                auth_store="surreal",
                surreal_url="ws://surrealdb:8000/rpc",
                surreal_username="sibyl_admin",
                surreal_password="really_secure_password",
            )

    def test_non_prefixed_jwt_secret_satisfies_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JWT_SECRET", "fallback-jwt-secret-0123456789abcdef0123456789ab")
        settings = Settings(
            _env_file=None,
            environment="production",
            store="surreal",
            auth_store="surreal",
            surreal_url="ws://surrealdb:8000/rpc",
            surreal_username="sibyl_admin",
            surreal_password="really_secure_password",
        )

        assert (
            settings.jwt_secret.get_secret_value()
            == "fallback-jwt-secret-0123456789abcdef0123456789ab"
        )

    def test_missing_jwt_secret_allowed_outside_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JWT_SECRET", raising=False)
        settings = Settings(_env_file=None, environment="staging")

        assert settings.jwt_secret.get_secret_value()

    def test_short_jwt_secret_forbidden_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with pytest.raises(ValueError, match="at least 32 are required"):
            Settings(
                _env_file=None,
                environment="production",
                store="surreal",
                auth_store="surreal",
                surreal_url="ws://surrealdb:8000/rpc",
                surreal_username="sibyl_admin",
                surreal_password="really_secure_password",
                jwt_secret="x",
            )

    def test_padded_jwt_secret_is_rejected_not_trimmed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Trimming would change the signing key and split a rolling upgrade."""
        monkeypatch.delenv("JWT_SECRET", raising=False)
        for padded in ("a" * 40 + "\n", "  " + "a" * 40, " " + "a" * 40 + " "):
            with pytest.raises(ValueError, match="leading or trailing whitespace"):
                Settings(
                    _env_file=None,
                    environment="production",
                    store="surreal",
                    auth_store="surreal",
                    surreal_url="ws://surrealdb:8000/rpc",
                    surreal_username="sibyl_admin",
                    surreal_password="really_secure_password",
                    jwt_secret=padded,
                )

    def test_clean_jwt_secret_is_preserved_byte_for_byte(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JWT_SECRET", raising=False)
        key = "a" * 40
        settings = Settings(
            _env_file=None,
            environment="production",
            store="surreal",
            auth_store="surreal",
            surreal_url="ws://surrealdb:8000/rpc",
            surreal_username="sibyl_admin",
            surreal_password="really_secure_password",
            jwt_secret=key,
        )

        assert settings.jwt_secret.get_secret_value() == key

    def test_whitespace_only_jwt_secret_forbidden_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with pytest.raises(ValueError, match="A JWT secret is required in production"):
            Settings(
                _env_file=None,
                environment="production",
                store="surreal",
                auth_store="surreal",
                surreal_url="ws://surrealdb:8000/rpc",
                surreal_username="sibyl_admin",
                surreal_password="really_secure_password",
                jwt_secret="   ",
            )


class TestProductionMcpAuthMode:
    """MCP auth must not be switched off in production."""

    def _production(self, **overrides: object) -> Settings:
        kwargs: dict[str, object] = {
            "_env_file": None,
            "environment": "production",
            "store": "surreal",
            "auth_store": "surreal",
            "surreal_url": "ws://surrealdb:8000/rpc",
            "surreal_username": "sibyl_admin",
            "surreal_password": "really_secure_password",
            "jwt_secret": "a" * 40,
        }
        kwargs.update(overrides)
        return Settings(**kwargs)  # type: ignore[arg-type]

    def test_auth_mode_off_forbidden_in_production(self) -> None:
        with pytest.raises(ValueError, match="mcp_auth_mode=off is forbidden in production"):
            self._production(mcp_auth_mode="off")

    def test_auth_mode_auto_and_on_allowed_in_production(self) -> None:
        for mode in ("auto", "on"):
            assert self._production(mcp_auth_mode=mode).mcp_auth_mode == mode

    def test_auth_mode_off_allowed_outside_production(self) -> None:
        settings = Settings(_env_file=None, environment="development", mcp_auth_mode="off")

        assert settings.mcp_auth_mode == "off"
