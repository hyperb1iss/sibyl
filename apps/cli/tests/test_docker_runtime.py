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
from sibyl_cli.docker_storage import SURREAL_IMAGE_REFERENCE
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
        "${SIBYL_API_UID:-10001}:${SIBYL_API_GID:-10001}",
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
        return [str(port["published"]) for port in config["services"][service].get("ports", [])]

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


def _docker_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    surreal_image: str = SURREAL_IMAGE_REFERENCE,
    with_crawler: bool = False,
    pull_status: int = 0,
) -> tuple[Path, list[dict[str, object]]]:
    """An initialized 1.3.2 runtime whose SurrealDB slot holds `surreal_image`.

    The returned list records each compose call: its arguments, the compose
    file it read, that file's SurrealDB image, and the SurrealDB image the live
    compose file held at that moment.
    """
    docker_dir = tmp_path / "docker"
    compose_path = docker_dir / "docker-compose.yml"
    monkeypatch.setattr(docker_module, "SIBYL_DOCKER_DIR", docker_dir)
    monkeypatch.setattr(docker_module, "SIBYL_DOCKER_ENV", docker_dir / ".env")
    monkeypatch.setattr(docker_module, "SIBYL_DOCKER_COMPOSE", compose_path)
    monkeypatch.setattr(docker_module, "require_docker", lambda: None)
    monkeypatch.delenv("SIBYL_SURREAL_IMAGE", raising=False)
    docker_module.write_env_file(image_tag="1.3.2", surreal_password="surreal", jwt_secret="jwt")
    config = docker_module.compose_config(
        image_tag="1.3.2",
        api_port=3334,
        web_port=3337,
        surreal_port=8000,
        with_worker=True,
        with_crawler=with_crawler,
    )
    config["services"]["surrealdb"]["image"] = surreal_image
    docker_module.write_compose_file(config)

    calls: list[dict[str, object]] = []

    def surreal_image_in(path: Path) -> str:
        return yaml.safe_load(path.read_text())["services"]["surrealdb"]["image"]

    def fake_run_compose(
        args: list[str], compose_file: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        read = compose_file or compose_path
        calls.append(
            {
                "args": args,
                "file": read.name,
                "surreal": surreal_image_in(read),
                "live_surreal": surreal_image_in(compose_path),
            }
        )
        return subprocess.CompletedProcess(args, pull_status if args == ["pull"] else 0)

    monkeypatch.setattr(docker_module, "run_compose", fake_run_compose)
    return compose_path, calls


def test_docker_upgrade_tag_updates_pinned_compose_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_path, _ = _docker_runtime(tmp_path, monkeypatch, with_crawler=True)

    result = CliRunner().invoke(app, ["docker", "upgrade", "--tag", "1.0.0-rc.8"])

    assert result.exit_code == 0, result.output
    env = (compose_path.parent / ".env").read_text()
    services = yaml.safe_load(compose_path.read_text())["services"]
    assert "SIBYL_IMAGE_TAG=1.0.0-rc.8" in env
    assert services["api"]["image"] == "ghcr.io/hyperb1iss/sibyl-api-crawler:1.0.0-rc.8"
    assert services["worker"]["image"] == "ghcr.io/hyperb1iss/sibyl-api-crawler:1.0.0-rc.8"
    assert services["web"]["image"] == "ghcr.io/hyperb1iss/sibyl-web:1.0.0-rc.8"
    assert services["surrealdb"]["image"] == "${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.2.4}"


@pytest.mark.parametrize("tag_args", [[], ["--tag", "1.4.0"]])
def test_docker_upgrade_moves_older_surreal_default_to_shipped_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tag_args: list[str],
) -> None:
    old_pin = "${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.2.3}"
    compose_path, calls = _docker_runtime(tmp_path, monkeypatch, surreal_image=old_pin)

    result = CliRunner().invoke(app, ["docker", "upgrade", *tag_args])

    assert result.exit_code == 0, result.output
    services = yaml.safe_load(compose_path.read_text())["services"]
    assert services["surrealdb"]["image"] == SURREAL_IMAGE_REFERENCE
    assert "Upgrading SurrealDB from v3.2.3 to v3.2.4" in result.output
    # The pull reads a staged copy with the new pin while the live file still
    # holds the old one; only then does the live file move and `up -d` run.
    assert calls == [
        {
            "args": ["pull"],
            "file": "docker-compose.upgrade.yml",
            "surreal": SURREAL_IMAGE_REFERENCE,
            "live_surreal": old_pin,
        },
        {
            "args": ["up", "-d"],
            "file": "docker-compose.yml",
            "surreal": SURREAL_IMAGE_REFERENCE,
            "live_surreal": SURREAL_IMAGE_REFERENCE,
        },
    ]
    # Compose holds the API until the recreated SurrealDB reports healthy.
    assert services["api"]["depends_on"]["surrealdb"] == {"condition": "service_healthy"}
    assert not (compose_path.parent / "docker-compose.upgrade.yml").exists()


def test_docker_upgrade_failed_pull_leaves_pins_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_path, calls = _docker_runtime(
        tmp_path,
        monkeypatch,
        surreal_image="${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.2.3}",
        pull_status=1,
    )
    env_path = compose_path.parent / ".env"
    compose_before, env_before = compose_path.read_text(), env_path.read_text()

    result = CliRunner().invoke(app, ["docker", "upgrade", "--tag", "1.4.0"])

    assert result.exit_code == 1
    # The running 1.3.2 containers and the pins that describe them still agree.
    assert compose_path.read_text() == compose_before
    assert env_path.read_text() == env_before
    assert [call["args"] for call in calls] == [["pull"]]
    assert "Failed to pull the upgrade images" in result.output
    assert "Docker deployment upgraded" not in result.output
    assert not (compose_path.parent / "docker-compose.upgrade.yml").exists()


@pytest.mark.parametrize("source", ["env-file", "shell"])
def test_docker_upgrade_keeps_explicit_surreal_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    compose_path, calls = _docker_runtime(
        tmp_path, monkeypatch, surreal_image="${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.2.3}"
    )
    pinned = "registry.example/surrealdb:v3.2.3-patched"
    env_path = compose_path.parent / ".env"
    if source == "shell":
        monkeypatch.setenv("SIBYL_SURREAL_IMAGE", pinned)
    else:
        env_path.write_text(env_path.read_text() + f"SIBYL_SURREAL_IMAGE={pinned}\n")
    env_before = env_path.read_text()

    result = CliRunner().invoke(app, ["docker", "upgrade"])

    assert result.exit_code == 0, result.output
    # The override still feeds the interpolated slot, and nothing rewrote it.
    image = yaml.safe_load(compose_path.read_text())["services"]["surrealdb"]["image"]
    assert image.startswith("${SIBYL_SURREAL_IMAGE:-")
    assert env_path.read_text() == env_before
    assert f"keeps SurrealDB on {pinned}" in result.output
    assert [call["args"] for call in calls] == [["pull"], ["up", "-d"]]


@pytest.mark.parametrize(
    "image",
    [
        "surrealdb/surrealdb:v3.2.3",
        "registry.example/surrealdb:custom",
        "${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:nightly}",
        "${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.3.0}",
    ],
)
def test_docker_upgrade_leaves_hand_edited_surreal_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    image: str,
) -> None:
    compose_path, _ = _docker_runtime(tmp_path, monkeypatch, surreal_image=image)
    compose_before = compose_path.read_text()

    result = CliRunner().invoke(app, ["docker", "upgrade"])

    assert result.exit_code == 0, result.output
    assert compose_path.read_text() == compose_before
    assert "Leaving the SurrealDB image" in result.output


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


@pytest.mark.parametrize("runtime", ["local", "docker"])
def test_surreal_volume_initialization_preserves_nonroot_and_existing_files(runtime: str) -> None:
    config = (
        local_module.COMPOSE_CONFIG
        if runtime == "local"
        else docker_module.compose_config(
            image_tag="test",
            api_port=3334,
            web_port=3337,
            surreal_port=8000,
            with_worker=False,
            with_crawler=False,
        )
    )
    services = config["services"]
    initializer = services["surreal-init"]
    database = services["surrealdb"]
    assert initializer["command"] == ["chown", "65532:65532", "/data"]
    assert initializer["user"] == "0:0"
    assert initializer["network_mode"] == "none"
    assert initializer["cap_drop"] == ["ALL"]
    assert initializer["cap_add"] == ["CHOWN"]
    assert initializer["read_only"] is True
    assert "user" not in database
    assert database["depends_on"]["surreal-init"] == {"condition": "service_completed_successfully"}
    assert (
        database["volumes"]
        == initializer["volumes"]
        == [
            {
                "type": "volume",
                "source": "sibyl_surreal",
                "target": "/data",
                "volume": {"nocopy": True},
            }
        ]
    )
