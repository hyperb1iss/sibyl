from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from sibyl_cli import config_store
from sibyl_cli import dev as dev_module
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
    assert services["api"]["ports"] == ["127.0.0.1:3334:3334"]
    assert services["web"]["ports"] == ["127.0.0.1:3337:3337"]
    assert services["surrealdb"]["ports"] == ["127.0.0.1:8000:8000"]


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


def test_quickstart_compose_persists_generated_runtime_secrets() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    compose = yaml.safe_load((repo_root / "docker-compose.quickstart.yml").read_text())

    services = compose["services"]
    secret_mount = "sibyl_secrets:/home/sibyl/.sibyl"
    assert "SIBYL_JWT_SECRET" not in services["api"]["environment"]
    assert secret_mount in services["api"]["volumes"]
    assert secret_mount in services["worker"]["volumes"]
    assert services["secrets-init"]["command"] == [
        "chown",
        "-R",
        "1000:1000",
        "/home/sibyl/.sibyl",
    ]
    assert secret_mount in services["secrets-init"]["volumes"]
    assert services["api"]["depends_on"]["secrets-init"] == {
        "condition": "service_completed_successfully",
    }


def test_quickstart_test_compose_replaces_base_ports() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker compose is not available")

    repo_root = Path(__file__).resolve().parents[3]
    version = subprocess.run(
        [docker, "compose", "version"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if version.returncode != 0:
        pytest.skip("docker compose is not available")

    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "docker-compose.quickstart.yml",
            "-f",
            "docker-compose.quickstart.test.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)

    def published_ports(service: str) -> list[str]:
        return [
            str(port["published"])
            for port in config["services"][service].get("ports", [])
        ]

    assert published_ports("api") == ["3344"]
    assert published_ports("web") == ["3347"]
    assert published_ports("surrealdb") == ["8010"]
    assert config["networks"]["default"]["name"] == "sibyl-test"
    assert config["volumes"]["sibyl_secrets"]["name"] == "sibyl_test_secrets"
    assert config["volumes"]["sibyl_surreal"]["name"] == "sibyl_test_surreal"
    assert config["services"]["api"]["container_name"] == "sibyl-test-api"
    assert config["services"]["secrets-init"]["container_name"] == "sibyl-test-secrets-init"
    assert config["services"]["surrealdb"]["container_name"] == "sibyl-test-surrealdb"


def test_dev_compose_disables_default_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose = tmp_path / ".devcontainer" / "docker-compose.yml"
    compose.parent.mkdir()
    compose.write_text("services: {}\n")
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="[]")

    monkeypatch.setattr(dev_module.subprocess, "run", fake_run)

    result = dev_module._run_compose(["ps", "--format", "json"], compose, capture=True)

    assert result is not None
    assert result.returncode == 0
    assert calls == [
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            str(compose),
            "ps",
            "--format",
            "json",
        ]
    ]


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
