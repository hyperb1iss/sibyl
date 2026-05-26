from __future__ import annotations

import re
from pathlib import Path

import pytest

from sibyl_cli import local


def test_local_compose_defaults_to_fully_surreal_runtime() -> None:
    services = local.COMPOSE_CONFIG["services"]

    assert "surrealdb" in services
    assert services["surrealdb"]["image"] == "${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.0.5}"
    assert "falkordb" not in services
    assert "postgres" not in services

    api = services["api"]
    assert api["depends_on"] == {"surrealdb": {"condition": "service_healthy"}}
    assert api["environment"]["SIBYL_STORE"] == "surreal"
    assert api["environment"]["SIBYL_AUTH_STORE"] == "surreal"
    assert api["environment"]["SIBYL_SURREAL_URL"] == "ws://surrealdb:8000/rpc"


def test_local_compose_uses_versioned_sibyl_images() -> None:
    services = local.COMPOSE_CONFIG["services"]

    assert local._version_to_image_tag("1.0.0rc1") == "1.0.0-rc.1"
    assert services["api"]["image"] == f"ghcr.io/hyperb1iss/sibyl-api:{local.DEFAULT_IMAGE_TAG}"
    assert services["worker"]["image"] == f"ghcr.io/hyperb1iss/sibyl-api:{local.DEFAULT_IMAGE_TAG}"
    assert services["web"]["image"] == f"ghcr.io/hyperb1iss/sibyl-web:{local.DEFAULT_IMAGE_TAG}"
    assert ":latest" not in services["api"]["image"]
    assert ":latest" not in services["worker"]["image"]
    assert ":latest" not in services["web"]["image"]


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
    password_match = re.search(r"^SIBYL_SURREAL_PASSWORD=(\S+)$", env, re.MULTILINE)
    assert password_match is not None
    password = password_match.group(1)
    assert password != "sibyl_local", "must not regress to the static default"
    assert len(password) >= 24, "token_urlsafe(24) yields ~32 chars of entropy"
    assert "SIBYL_POSTGRES_PASSWORD" not in env
    assert "SIBYL_FALKORDB_PASSWORD" not in env
