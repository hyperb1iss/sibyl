import pytest

from sibyl.config import Settings


def test_settings_auth_fallbacks(monkeypatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "secret")
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "csecret")

    # Ensure the prefixed vars are not set so fallback path is exercised
    monkeypatch.delenv("SIBYL_JWT_SECRET", raising=False)
    monkeypatch.delenv("SIBYL_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("SIBYL_GITHUB_CLIENT_SECRET", raising=False)

    s = Settings(_env_file=None)
    assert s.jwt_secret.get_secret_value() == "secret"
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
    assert s.resolved_coordination_backend == "redis"


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
