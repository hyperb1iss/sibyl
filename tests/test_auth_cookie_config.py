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
