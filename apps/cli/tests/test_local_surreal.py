from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from sibyl_cli import local


def test_local_compose_defaults_to_fully_surreal_runtime() -> None:
    services = local.COMPOSE_CONFIG["services"]

    assert "surrealdb" in services
    assert services["surrealdb"]["image"] == "${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.2.3}"
    assert "falkordb" not in services
    assert "postgres" not in services
    assert "worker" not in services

    api = services["api"]
    assert api["depends_on"] == {"surrealdb": {"condition": "service_healthy"}}
    assert api["environment"]["SIBYL_STORE"] == "surreal"
    assert api["environment"]["SIBYL_AUTH_STORE"] == "surreal"
    assert api["environment"]["SIBYL_SURREAL_URL"] == "ws://surrealdb:8000/rpc"
    assert api["environment"]["SIBYL_COORDINATION_BACKEND"] == "local"


def test_local_compose_uses_versioned_sibyl_images() -> None:
    services = local.COMPOSE_CONFIG["services"]

    assert local._version_to_image_tag("1.0.0rc1") == "1.0.0-rc.1"
    assert services["api"]["image"] == f"ghcr.io/hyperb1iss/sibyl-api:{local.DEFAULT_IMAGE_TAG}"
    assert services["web"]["image"] == f"ghcr.io/hyperb1iss/sibyl-web:{local.DEFAULT_IMAGE_TAG}"
    assert ":latest" not in services["api"]["image"]
    assert ":latest" not in services["web"]["image"]


def test_local_compose_web_healthcheck_uses_ipv4_loopback() -> None:
    web = local.COMPOSE_CONFIG["services"]["web"]

    assert web["environment"]["HOSTNAME"] == "::"
    healthcheck_test = web["healthcheck"]["test"]
    url = healthcheck_test[-1]
    assert "127.0.0.1" in url
    assert "localhost" not in url


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


def _stub_local_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    running: bool = False,
    healthy: bool = True,
    pull_status: int = 0,
) -> tuple[list[list[str]], list[str]]:
    env_path = tmp_path / ".env"
    env_path.touch()
    monkeypatch.setattr(local, "SIBYL_LOCAL_ENV", env_path)
    monkeypatch.setattr(local, "check_docker", lambda: True)
    monkeypatch.setattr(local, "check_docker_compose", lambda: True)
    monkeypatch.setattr(local, "is_running", lambda: running)
    monkeypatch.setattr(local, "wait_for_healthy", lambda: healthy)
    commands: list[list[str]] = []
    browsers: list[str] = []
    monkeypatch.setattr(local, "write_compose_file", lambda: commands.append(["write-config"]))
    monkeypatch.setattr(local.webbrowser, "open", browsers.append)

    def compose(args: list[str]) -> SimpleNamespace:
        commands.append(args)
        return SimpleNamespace(returncode=pull_status if args[0] == "pull" else 0)

    monkeypatch.setattr(local, "run_compose", compose)
    return commands, browsers


def test_local_start_preserves_running_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands, browsers = _stub_local_start(tmp_path, monkeypatch, running=True)
    local.start(no_browser=False, pull=True)
    assert commands == []
    assert browsers == []
    output = capsys.readouterr().out
    assert "leaving server images unchanged" in output
    assert "sibyl down && sibyl up --pull" in output
    assert "Sibyl is ready" not in output


def test_local_start_stops_after_pull_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands, browsers = _stub_local_start(tmp_path, monkeypatch, pull_status=1)
    with pytest.raises(typer.Exit) as exc:
        local.start(no_browser=False, pull=True)
    assert exc.value.exit_code == 1
    assert commands == [["write-config"], ["pull", "--quiet"]]
    assert browsers == []
    assert "Sibyl is ready" not in capsys.readouterr().out


def test_local_start_does_not_announce_or_open_unhealthy_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, browsers = _stub_local_start(tmp_path, monkeypatch, healthy=False)
    with pytest.raises(typer.Exit) as exc:
        local.start(no_browser=False, pull=True)
    assert exc.value.exit_code == 1
    assert browsers == []
    assert "Sibyl is ready" not in capsys.readouterr().out


def test_local_start_opens_browser_after_successful_health_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands, browsers = _stub_local_start(tmp_path, monkeypatch)
    local.start(no_browser=False, pull=True)
    assert commands == [["write-config"], ["pull", "--quiet"], ["up", "-d"]]
    assert browsers == ["http://localhost:3337"]
    assert "Sibyl is ready" in capsys.readouterr().out


def test_local_api_healthcheck_requires_success_status() -> None:
    command = local.COMPOSE_CONFIG["services"]["api"]["healthcheck"]["test"]
    assert command[-1].endswith(".raise_for_status()")


@pytest.mark.parametrize("status", [200, 503])
def test_local_wait_uses_dependency_readiness_without_proxy(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    import httpx

    clock = iter([0, 0, 2])
    monkeypatch.setattr(local.time, "time", lambda: next(clock))
    monkeypatch.setattr(local.time, "sleep", lambda _seconds: None)
    calls = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        calls.append((url, kwargs))
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    assert local.wait_for_healthy(timeout=1) is (status == 200)
    assert calls == [("http://localhost:3334/api/health/ready", {"timeout": 2, "trust_env": False})]


@pytest.mark.parametrize("status", [200, 503])
def test_container_probe_executes_dependency_readiness_check(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    import httpx

    def get(url: str, **kwargs: object) -> httpx.Response:
        assert url == "http://localhost:3334/api/health/ready"
        assert kwargs == {"trust_env": False}
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    probe = local.COMPOSE_CONFIG["services"]["api"]["healthcheck"]["test"][-1]
    if status == 503:
        with pytest.raises(httpx.HTTPStatusError):
            exec(probe)  # noqa: S102
    else:
        exec(probe)  # noqa: S102
