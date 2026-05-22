from datetime import UTC, datetime

import pytest

from sibyl.config import Settings


def _oidc_provider(**overrides) -> dict[str, object]:
    provider: dict[str, object] = {
        "name": "entra",
        "issuer": "https://login.microsoftonline.com/tenant/v2.0",
        "client_id": "sibyl-client",
        "client_secret_env": "SIBYL_OIDC_ENTRA_CLIENT_SECRET",
    }
    provider.update(overrides)
    return provider


def test_settings_auth_fallbacks(monkeypatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-key-for-api-tests")
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "csecret")

    # Ensure the prefixed vars are not set so fallback path is exercised
    monkeypatch.delenv("SIBYL_JWT_SECRET", raising=False)
    monkeypatch.delenv("SIBYL_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("SIBYL_GITHUB_CLIENT_SECRET", raising=False)

    s = Settings(_env_file=None)
    assert s.jwt_secret.get_secret_value() == "test-jwt-secret-key-for-api-tests"
    assert s.github_client_id.get_secret_value() == "cid"
    assert s.github_client_secret.get_secret_value() == "csecret"


def test_settings_server_url_default() -> None:
    s = Settings(_env_file=None)
    assert s.server_url == "http://localhost:3334"


def test_settings_store_defaults_to_surreal() -> None:
    s = Settings(_env_file=None)
    assert s.store == "surreal"
    assert s.auth_store == "surreal"
    assert s.fully_surreal is True
    assert s.uses_relational_auth is False
    assert s.requires_relational_support is False
    assert s.coordination_backend == "auto"
    assert s.resolved_coordination_backend == "local"


def test_settings_store_uses_store_env(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_STORE", "legacy")

    s = Settings(_env_file=None)

    assert s.store == "legacy"
    assert s.auth_store == "surreal"
    assert s.fully_surreal is False
    assert s.uses_relational_auth is False
    assert s.requires_relational_support is False
    assert s.resolved_coordination_backend == "local"


def test_settings_store_ignores_removed_graph_backend_alias(monkeypatch) -> None:
    monkeypatch.delenv("SIBYL_STORE", raising=False)
    monkeypatch.setenv("SIBYL_GRAPH_BACKEND", "falkordb")

    s = Settings(_env_file=None)

    assert s.store == "surreal"


def test_settings_auth_store_can_use_surreal(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_AUTH_STORE", "surreal")

    s = Settings(_env_file=None)

    assert s.auth_store == "surreal"


def test_settings_reads_surreal_token(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_SURREAL_TOKEN", "token-123")

    s = Settings(_env_file=None)

    assert s.surreal_token.get_secret_value() == "token-123"


def test_settings_rejects_removed_postgres_auth_store(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_STORE", "surreal")
    monkeypatch.setenv("SIBYL_AUTH_STORE", "postgres")

    with pytest.raises(ValueError, match="auth_store"):
        Settings(_env_file=None)


def test_settings_coordination_backend_can_override_auto() -> None:
    s = Settings(_env_file=None, store="surreal", coordination_backend="redis")
    assert s.resolved_coordination_backend == "redis"


def test_settings_rate_limit_storage_uses_redis_password() -> None:
    s = Settings(
        _env_file=None,
        redis_host="valkey",
        redis_port=6379,
        redis_password="p@ ss",
        rate_limit_storage="redis://valkey:6379/4",
    )

    assert s.rate_limit_storage == "redis://:p%40%20ss@valkey:6379/4"


def test_settings_rate_limit_storage_keeps_explicit_auth() -> None:
    s = Settings(
        _env_file=None,
        redis_password="secret",
        rate_limit_storage="redis://:already@valkey:6379/4",
    )

    assert s.rate_limit_storage == "redis://:already@valkey:6379/4"


def test_settings_resolves_surreal_data_dir_url() -> None:
    s = Settings(_env_file=None, store="surreal", surreal_data_dir="./var/sibyl-surreal")
    assert s.resolved_surreal_url == "surrealkv://./var/sibyl-surreal"


def test_settings_server_url_uses_public_url_when_explicit() -> None:
    s = Settings(_env_file=None, public_url="https://public.example.com")
    assert s.server_url == "https://public.example.com"


def test_settings_mcp_auth_mode_default() -> None:
    s = Settings(_env_file=None)
    assert s.mcp_auth_mode == "auto"


def test_settings_auth_defaults_keep_development_login_available() -> None:
    s = Settings(_env_file=None)

    assert s.local_auth_enabled is True
    assert s.break_glass_enabled is False
    assert s.oidc.providers == []


def test_settings_auth_defaults_keep_production_local_login_available() -> None:
    s = Settings(
        _env_file=None,
        environment="production",
        store="surreal",
        auth_store="surreal",
        surreal_url="ws://surrealdb:8000/rpc",
        surreal_username="sibyl_admin",
        surreal_password="really_secure_password",
    )

    assert s.local_auth_enabled is True
    assert s.public_signups_enabled is False
    assert s.break_glass_enabled is False
    assert s.oidc.providers == []


def test_settings_explicit_local_auth_override_is_respected() -> None:
    s = Settings(_env_file=None, local_auth_enabled=False)

    assert s.local_auth_enabled is False


def test_settings_explicit_production_local_auth_override_is_respected() -> None:
    s = Settings(
        _env_file=None,
        environment="production",
        store="surreal",
        auth_store="surreal",
        surreal_url="ws://surrealdb:8000/rpc",
        surreal_username="sibyl_admin",
        surreal_password="really_secure_password",
        local_auth_enabled=True,
    )

    assert s.local_auth_enabled is True


def test_settings_oidc_defaults_to_enterprise_contract() -> None:
    s = Settings(
        _env_file=None,
        environment="production",
        store="surreal",
        auth_store="surreal",
        surreal_url="ws://surrealdb:8000/rpc",
        surreal_username="sibyl_admin",
        surreal_password="really_secure_password",
    )

    assert s.break_glass_enabled is False
    assert s.break_glass_allowed_ips == []
    assert s.break_glass_expires_at is None
    assert s.oidc.providers == []
    assert s.oidc.role_claim == "roles"
    assert s.oidc.session_minutes == 60
    assert s.oidc.silent_refresh_enabled is False
    assert s.oidc.extra_providers_enabled is False


def test_settings_break_glass_parses_cidrs_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIBYL_BREAK_GLASS_ALLOWED_IPS", '["203.0.113.0/24","2001:db8::/32"]')
    monkeypatch.setenv("SIBYL_BREAK_GLASS_EXPIRES_AT", "2026-05-22T19:00:00Z")

    s = Settings(_env_file=None)

    assert s.break_glass_allowed_ips == ["203.0.113.0/24", "2001:db8::/32"]
    assert s.break_glass_expires_at == datetime(2026, 5, 22, 19, tzinfo=UTC)


def test_settings_break_glass_rejects_invalid_cidr() -> None:
    with pytest.raises(ValueError, match="not-a-cidr"):
        Settings(_env_file=None, break_glass_allowed_ips=["not-a-cidr"])


def test_settings_oidc_accepts_corporate_provider_config() -> None:
    s = Settings(
        _env_file=None,
        oidc={
            "providers": [
                _oidc_provider(scopes=["openid", "profile", "email", "groups"]),
            ],
            "role_claim": "resource_access.sibyl.roles",
            "redirect_uri_base": "https://sibyl.example.com/",
            "session_minutes": 45,
        },
    )

    provider = s.oidc.providers[0]
    assert provider.name == "entra"
    assert provider.scopes == ["openid", "profile", "email", "groups"]
    assert provider.role_claim_override is None
    assert s.oidc.role_claim == "resource_access.sibyl.roles"
    assert s.oidc.redirect_uri_base == "https://sibyl.example.com/"
    assert s.oidc.session_minutes == 45


def test_settings_oidc_parses_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SIBYL_OIDC",
        """
        {
          "providers": [
            {
              "name": "okta",
              "issuer": "https://example.okta.com/oauth2/default",
              "client_id": "sibyl-client",
              "client_secret_env": "SIBYL_OIDC_OKTA_CLIENT_SECRET",
              "role_claim_override": "groups"
            }
          ],
          "role_claim": "groups"
        }
        """,
    )

    s = Settings(_env_file=None)

    assert s.oidc.providers[0].name == "okta"
    assert s.oidc.providers[0].scopes == ["openid", "profile", "email"]
    assert s.oidc.providers[0].role_claim_override == "groups"
    assert s.oidc.role_claim == "groups"


def test_settings_oidc_rejects_extra_provider_by_default() -> None:
    with pytest.raises(ValueError, match="extra_providers_enabled=true"):
        Settings(
            _env_file=None,
            oidc={
                "providers": [
                    _oidc_provider(name="github", issuer="https://github.com/login/oauth"),
                ],
            },
        )


def test_settings_oidc_allows_extra_provider_when_explicit() -> None:
    s = Settings(
        _env_file=None,
        oidc={
            "providers": [
                _oidc_provider(name="github", issuer="https://github.com/login/oauth"),
            ],
            "extra_providers_enabled": True,
        },
    )

    assert s.oidc.providers[0].is_extra_provider is True


def test_settings_oidc_requires_openid_scope() -> None:
    with pytest.raises(ValueError, match="scopes must include openid"):
        Settings(
            _env_file=None,
            oidc={
                "providers": [
                    _oidc_provider(scopes=["profile", "email"]),
                ],
            },
        )
