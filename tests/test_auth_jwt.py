from uuid import uuid4

import pytest

from sibyl.auth.jwt import JwtError, create_access_token, verify_access_token
from sibyl.config import Settings


def test_jwt_roundtrip(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_JWT_SECRET", "secret")
    monkeypatch.setenv("SIBYL_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SIBYL_JWT_EXPIRY_HOURS", "24")

    # Reset global settings instance by re-importing Settings and reloading module-level settings
    # (Settings is loaded at import time in sibyl.config; changing env vars affects new Settings).
    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    user_id = uuid4()
    token = create_access_token(user_id=user_id)
    claims = verify_access_token(token)
    assert claims["sub"] == str(user_id)
    assert claims["typ"] == "access"


def test_jwt_rejects_wrong_secret(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_JWT_SECRET", "secret1")
    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    token = create_access_token(user_id=uuid4())

    monkeypatch.setenv("SIBYL_JWT_SECRET", "secret2")
    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    with pytest.raises(JwtError):
        verify_access_token(token)
