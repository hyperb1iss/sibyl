from __future__ import annotations

from pathlib import Path

import pytest

from sibyl_cli import local


def test_local_compose_defaults_to_fully_surreal_runtime() -> None:
    services = local.COMPOSE_CONFIG["services"]

    assert "surrealdb" in services
    assert "falkordb" not in services
    assert "postgres" not in services

    api = services["api"]
    assert api["depends_on"] == {"surrealdb": {"condition": "service_healthy"}}
    assert api["environment"]["SIBYL_STORE"] == "surreal"
    assert api["environment"]["SIBYL_AUTH_STORE"] == "surreal"
    assert api["environment"]["SIBYL_SURREAL_URL"] == "ws://surrealdb:8000/rpc"


def test_local_worker_uses_same_surreal_runtime_as_api() -> None:
    api_env = local.COMPOSE_CONFIG["services"]["api"]["environment"]
    worker_env = local.COMPOSE_CONFIG["services"]["worker"]["environment"]

    for key in (
        "SIBYL_STORE",
        "SIBYL_AUTH_STORE",
        "SIBYL_COORDINATION_BACKEND",
        "SIBYL_SURREAL_URL",
        "SIBYL_SURREAL_USERNAME",
        "SIBYL_SURREAL_PASSWORD",
    ):
        assert worker_env[key] == api_env[key]


def test_local_env_file_contains_surreal_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.setattr(local, "SIBYL_LOCAL_DIR", tmp_path)
    monkeypatch.setattr(local, "SIBYL_LOCAL_ENV", env_path)

    local.write_env_file("openai-key", "anthropic-key", "jwt-secret")

    env = env_path.read_text()
    assert "SIBYL_SURREAL_USERNAME=root" in env
    assert "SIBYL_SURREAL_PASSWORD=sibyl_local" in env
    assert "SIBYL_POSTGRES_PASSWORD" not in env
    assert "SIBYL_FALKORDB_PASSWORD" not in env
