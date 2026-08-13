from sibyl.config import Settings


def test_cookie_secure_auto_from_https(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_SERVER_URL", "https://example.com")
    monkeypatch.delenv("SIBYL_COOKIE_SECURE", raising=False)

    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    from sibyl.api.routes import auth as auth_routes

    assert auth_routes._cookie_secure() is True


def test_cookie_secure_override(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_SERVER_URL", "https://example.com")
    monkeypatch.setenv("SIBYL_COOKIE_SECURE", "false")

    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    from sibyl.api.routes import auth as auth_routes

    assert auth_routes._cookie_secure() is False


def test_cookie_secure_defaults_true_in_production_http(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_ENVIRONMENT", "production")
    monkeypatch.setenv("SIBYL_SERVER_URL", "http://example.com")
    monkeypatch.setenv("SIBYL_SURREAL_URL", "ws://surrealdb:8000/rpc")
    monkeypatch.setenv("SIBYL_SURREAL_USERNAME", "sibyl_admin")
    monkeypatch.setenv("SIBYL_SURREAL_PASSWORD", "really_secure_password")
    monkeypatch.setenv("SIBYL_JWT_SECRET", "test-jwt-secret-0123456789abcdef0123456789abcdef")
    monkeypatch.delenv("SIBYL_COOKIE_SECURE", raising=False)

    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    from sibyl.api.routes import auth as auth_routes

    assert auth_routes._cookie_secure() is True
