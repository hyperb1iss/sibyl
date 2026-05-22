from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from sibyl_cli import config_store
from sibyl_cli import docker as docker_module
from sibyl_cli import local as local_module
from sibyl_cli.main import app


def _use_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)


def test_docker_compose_defaults_to_single_host_runtime() -> None:
    config = docker_module.compose_config(
        image_tag="1.0.0-rc.1",
        api_port=3334,
        web_port=3337,
        surreal_port=8000,
        with_worker=False,
        with_crawler=False,
    )

    services = config["services"]
    assert "worker" not in services
    assert "valkey" not in services
    assert services["api"]["environment"]["SIBYL_COORDINATION_BACKEND"] == "local"
    assert services["api"]["image"] == "ghcr.io/hyperb1iss/sibyl-api:1.0.0-rc.1"
    assert services["web"]["image"] == "ghcr.io/hyperb1iss/sibyl-web:1.0.0-rc.1"


def test_docker_compose_can_opt_into_worker_runtime() -> None:
    config = docker_module.compose_config(
        image_tag="1.0.0-rc.1",
        api_port=3334,
        web_port=3337,
        surreal_port=8000,
        with_worker=True,
        with_crawler=True,
    )

    services = config["services"]
    assert "worker" in services
    assert "valkey" in services
    assert services["api"]["environment"]["SIBYL_COORDINATION_BACKEND"] == "redis"
    assert services["api"]["image"] == "ghcr.io/hyperb1iss/sibyl-api-crawler:1.0.0-rc.1"
    assert services["worker"]["environment"]["SIBYL_REDIS_URL"] == "redis://valkey:6379/0"


def test_docker_init_writes_runtime_files_and_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)
    docker_dir = tmp_path / "docker"
    monkeypatch.setattr(docker_module, "SIBYL_DOCKER_DIR", docker_dir)
    monkeypatch.setattr(docker_module, "SIBYL_DOCKER_ENV", docker_dir / ".env")
    monkeypatch.setattr(docker_module, "SIBYL_DOCKER_COMPOSE", docker_dir / "docker-compose.yml")

    result = CliRunner().invoke(app, ["docker", "init", "--tag", "1.2.3"])

    assert result.exit_code == 0
    env = (docker_dir / ".env").read_text()
    compose = yaml.safe_load((docker_dir / "docker-compose.yml").read_text())
    assert "SIBYL_IMAGE_TAG=1.2.3" in env
    assert compose["services"]["api"]["image"] == "ghcr.io/hyperb1iss/sibyl-api:1.2.3"
    ctx = config_store.get_active_context()
    assert ctx is not None
    assert ctx.name == "docker"
    assert ctx.server_url == "http://localhost:3334"


def test_up_starts_local_runtime_without_agent_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_dir = tmp_path / "local"
    monkeypatch.setattr(local_module, "SIBYL_LOCAL_DIR", local_dir)
    monkeypatch.setattr(local_module, "SIBYL_LOCAL_ENV", local_dir / ".env")
    monkeypatch.setattr(local_module, "SIBYL_LOCAL_COMPOSE", local_dir / "docker-compose.yml")
    monkeypatch.setattr(local_module, "check_docker", lambda: True)
    monkeypatch.setattr(local_module, "check_docker_compose", lambda: True)
    monkeypatch.setattr(local_module, "is_running", lambda: False)
    monkeypatch.setattr(local_module, "wait_for_healthy", lambda: True)

    opened_urls: list[str] = []
    compose_calls: list[list[str]] = []
    monkeypatch.setattr(local_module.webbrowser, "open", opened_urls.append)

    def fake_run_compose(
        args: list[str],
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        compose_calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(local_module, "run_compose", fake_run_compose)

    result = CliRunner().invoke(app, ["up", "--pull", "--no-browser"])

    assert result.exit_code == 0
    assert opened_urls == []
    assert compose_calls == [["pull", "--quiet"], ["up", "-d"]]
    assert (local_dir / ".env").exists()
    assert (local_dir / "docker-compose.yml").exists()
    assert "sibyl local setup" not in result.output
    assert "Connect page" in result.output
