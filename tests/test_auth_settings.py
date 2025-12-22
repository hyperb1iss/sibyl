import os

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
    assert s.server_url.startswith("http")

