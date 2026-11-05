"""`upgrade --surreal-major` moves a held 3.x store to 4.0 by converting it in place.

Every Docker command is stubbed: these tests pin the order of the move, the
service each step runs against, and what a failure leaves behind.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from sibyl_cli import docker as docker_module
from sibyl_cli import docker_storage, local
from sibyl_cli.docker_storage import (
    SURREAL_BACKUP_VOLUME,
    SURREAL_IMAGE,
    SURREAL_IMAGE_REFERENCE,
    SURREAL_STORAGE_ENV,
    SURREAL_STORE_PATH,
)
from sibyl_cli.main import app

SURREAL_3X_IMAGE = "${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.2.4}"
CONVERT_ENV = {**SURREAL_STORAGE_ENV, "SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X": "true"}
GIB = 1024 * 1024  # in KiB, the unit `du -sk` and `df -Pk` report


def _surreal(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["services"]["surrealdb"]


@dataclass
class FakeDocker:
    """Answers the move's Docker commands and records the surrealdb service each one saw."""

    compose_file: Path
    fail: str | None = None
    used: int = GIB
    free: int = 4 * GIB
    ready_after: int = 2
    calls: list[str] = field(default_factory=list)
    seen: dict[str, dict] = field(default_factory=dict)
    commands: dict[str, list[str]] = field(default_factory=dict)

    @staticmethod
    def name(cmd: list[str]) -> str:
        if cmd[:2] == ["docker", "pull"]:
            return "pull"
        if cmd[:2] == ["docker", "compose"]:
            rest = " ".join(cmd[6:])  # after `-f <file> --env-file <file>`
            return "ready" if "is-ready" in rest else rest
        if "du -sk" in cmd[-1]:
            return "measure"
        if "cp -a /data/. /backup/" in cmd[-1]:
            return "copy"
        return " ".join(cmd)

    def __call__(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        name = self.name(cmd)
        self.calls.append(name)
        self.seen.setdefault(name, _surreal(self.compose_file))
        self.commands.setdefault(name, cmd)
        if name == self.fail:
            return subprocess.CompletedProcess(cmd, 1, stdout="")
        if name == "measure":
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{self.used}\n{self.free}\n")
        if name == "ready":
            self.ready_after -= 1
            return subprocess.CompletedProcess(cmd, 0 if self.ready_after <= 0 else 1, stdout="")
        return subprocess.CompletedProcess(cmd, 0, stdout="")


@dataclass
class Stack:
    runtime: str
    compose_file: Path
    other_compose_file: Path
    held: dict
    api_waits: list[int]
    api_ready: list[bool]

    @property
    def held_compose_file(self) -> Path:
        return self.compose_file.with_name(f"{self.compose_file.name}.surreal3x.bak")

    def files(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.compose_file.parent.iterdir()}

    def upgrade(self, *args: str, input: str | None = None):
        return CliRunner().invoke(
            app, [self.runtime, "upgrade", "--surreal-major", *args], input=input
        )


@pytest.fixture(params=["local", "docker"])
def stack(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stack:
    """A runtime whose compose file holds the surrealdb service a 3.x CLI wrote."""
    runtime: str = request.param
    home = tmp_path / ".sibyl"
    for name, module in (("local", local), ("docker", docker_module)):
        prefix = "SIBYL_LOCAL" if name == "local" else "SIBYL_DOCKER"
        monkeypatch.setattr(module, f"{prefix}_DIR", home / name)
        monkeypatch.setattr(module, f"{prefix}_ENV", home / name / ".env")
        monkeypatch.setattr(module, f"{prefix}_COMPOSE", home / name / "docker-compose.yml")
    monkeypatch.setattr(local, "check_docker", lambda: True)
    monkeypatch.setattr(local, "check_docker_compose", lambda: True)
    monkeypatch.setattr(docker_module, "require_docker", lambda: None)
    monkeypatch.delenv("SIBYL_SURREAL_IMAGE", raising=False)
    # This CLI's release; the stack's API pins the same one unless a test says otherwise.
    monkeypatch.setattr(local, "DEFAULT_IMAGE_TAG", "1.5.0")
    monkeypatch.setattr(docker_module, "DEFAULT_IMAGE_TAG", "1.5.0")

    if runtime == "local":
        config = local.compose_config_for("1.5.0")
        local.SIBYL_LOCAL_DIR.mkdir(parents=True)
        local.SIBYL_LOCAL_ENV.touch()
    else:
        docker_module.write_env_file(image_tag="1.5.0", surreal_password="p", jwt_secret="j")
        config = docker_module.compose_config(
            image_tag="1.5.0",
            api_port=3334,
            web_port=3337,
            surreal_port=8000,
            with_worker=True,
            with_crawler=False,
        )
    held = config["services"]["surrealdb"]
    held["image"] = SURREAL_3X_IMAGE
    held["command"][-1] = "rocksdb:///data/sibyl.db"
    del held["environment"]
    del held["healthcheck"]["start_period"]
    compose_file = home / runtime / "docker-compose.yml"
    compose_file.write_text(yaml.dump(config, sort_keys=False))
    other = home / ("docker" if runtime == "local" else "local") / "docker-compose.yml"

    # Time only moves when the move sleeps, so its limits are exact.
    clock = [0.0]
    monkeypatch.setattr(docker_storage.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        docker_storage.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    api_waits: list[int] = []
    api_ready = [True]

    def wait_for_api(timeout: int = 120, **_kwargs: object) -> bool:
        api_waits.append(timeout)
        return api_ready[0]

    monkeypatch.setattr(local, "wait_for_healthy", wait_for_api)
    monkeypatch.setattr(docker_module, "wait_for_api", wait_for_api)
    return Stack(runtime, compose_file, other, held, api_waits, api_ready)


def _docker(stack: Stack, monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> FakeDocker:
    docker = FakeDocker(stack.compose_file, **kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr(docker_storage.subprocess, "run", docker)
    return docker


def _pin_api(stack: Stack, tag: str) -> None:
    config = yaml.safe_load(stack.compose_file.read_text())
    api = config["services"]["api"]
    api["image"] = f"{api['image'].rpartition(':')[0]}:{tag}"
    stack.compose_file.write_text(yaml.dump(config, sort_keys=False))


def _writers(stack: Stack) -> str:
    return "api web" if stack.runtime == "local" else "api worker web"


def test_the_move_converts_in_place_then_starts_sibyl_on_it(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A runtime already on 4.0 shares the volume safely.
    stack.other_compose_file.parent.mkdir(parents=True, exist_ok=True)
    stack.other_compose_file.write_text(
        yaml.dump({"services": {"surrealdb": {"image": SURREAL_IMAGE_REFERENCE}}})
    )
    held_bytes = stack.compose_file.read_bytes()
    docker = _docker(stack, monkeypatch, used=20 * GIB, free=50 * GIB)

    result = stack.upgrade("--yes")

    assert result.exit_code == 0, result.output
    assert docker.calls == [
        "pull",
        f"stop {_writers(stack)} surrealdb",
        "copy",
        "measure",
        "up -d --no-deps surrealdb",
        "ready",
        "ready",
        "up -d",
    ]
    assert docker.commands["pull"] == ["docker", "pull", SURREAL_IMAGE]
    copy = docker.commands["copy"]
    assert "sibyl_surreal:/data:ro" in copy and f"{SURREAL_BACKUP_VOLUME}:/backup" in copy
    assert 'if [ -n "$(ls -A /backup)" ]' in copy[-1], "refuses a backup that holds data"
    measure = docker.commands["measure"]
    assert "sibyl_surreal:/data:ro" in measure and "df -Pk /data" in measure[-1]

    # Nothing touches the compose file until the store is copied and measured.
    for step in ("pull", f"stop {_writers(stack)} surrealdb", "copy", "measure"):
        assert docker.seen[step] == stack.held
    assert stack.held_compose_file.read_bytes() == held_bytes

    converting = docker.seen["up -d --no-deps surrealdb"]
    assert converting["image"] == SURREAL_IMAGE_REFERENCE
    assert converting["command"][-1] == SURREAL_STORE_PATH
    assert converting["environment"] == CONVERT_ENV
    assert converting["healthcheck"]["start_period"] == "10m"
    serving = docker.seen["up -d"]
    assert serving["environment"] == SURREAL_STORAGE_ENV, "the opt-in is only for converting"
    assert serving["command"][-1] == SURREAL_STORE_PATH
    assert _surreal(stack.compose_file) == serving

    # 20 GiB: 60 s per GiB plus 5 min to rebuild, above the 10 min floor.
    assert stack.api_waits == [1500]
    output = " ".join(result.output.split())
    assert "SurrealDB now runs v4.0.0" in output
    assert "first requests for each organization can be slow" in output
    assert "rm -rf /data/sibyl.db/_rocksdb_backup" in output
    assert f"docker volume rm {SURREAL_BACKUP_VOLUME}" in output
    assert f"cp {stack.held_compose_file} {stack.compose_file}" in output
    assert "Never start SurrealDB v3.2.4 on the converted volume" in output


def test_a_declined_move_changes_nothing(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    before = stack.files()
    docker = _docker(stack, monkeypatch)

    result = stack.upgrade(input="\n")

    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "converts its data in place, one way" in output
    assert "twice the store's size must be free" in output
    assert docker.calls == []
    assert {k: v for k, v in stack.files().items() if k != ".upgrade.lock"} == before


@pytest.mark.parametrize(
    ("case", "says"),
    [
        ("other-runtime", "would open the converted data as an empty database"),
        ("override", "would override the move"),
        ("tag", "moves only SurrealDB"),
        ("hand-edited", "does not open rocksdb:///data/sibyl.db"),
        ("older-sibyl", "Sibyl runs 1.4.0 here, older than this CLI's 1.5.0"),
    ],
)
def test_the_plan_refuses_before_touching_anything(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, case: str, says: str
) -> None:
    args: tuple[str, ...] = ("--yes",)
    if case == "other-runtime":
        stack.other_compose_file.parent.mkdir(parents=True, exist_ok=True)
        stack.other_compose_file.write_text(
            yaml.dump({"services": {"surrealdb": {"image": SURREAL_3X_IMAGE}}})
        )
    elif case == "override":
        env = stack.compose_file.parent / ".env"
        env.write_text(env.read_text() + "SIBYL_SURREAL_IMAGE=surrealdb/surrealdb:v3.2.4\n")
    elif case == "tag":
        args = ("--yes", "--tag", "1.5.0")
    elif case == "older-sibyl":
        _pin_api(stack, "1.4.0")
    else:
        config = yaml.safe_load(stack.compose_file.read_text())
        config["services"]["surrealdb"]["command"][-1] = "rocksdb:///data/elsewhere.db"
        stack.compose_file.write_text(yaml.dump(config, sort_keys=False))
    before = stack.files()
    docker = _docker(stack, monkeypatch)

    result = stack.upgrade(*args)

    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert says in output and "Nothing changed" in output
    if case == "older-sibyl":
        assert f"sibyl {stack.runtime} upgrade, then run this again" in output
    assert docker.calls == []
    assert stack.files() == before


@pytest.mark.parametrize(
    ("api_tag", "cli_tag"),
    [
        ("main", "1.5.0"),  # a custom build's tag is not a release
        ("1.4.0", "1.5.1.dev3+gabc123"),  # a dev CLI has no release to compare
        ("1.6.0", "1.5.0"),  # newer than this CLI is not older
    ],
)
def test_only_an_older_release_holds_the_move_back(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, api_tag: str, cli_tag: str
) -> None:
    _pin_api(stack, api_tag)
    monkeypatch.setattr(local, "DEFAULT_IMAGE_TAG", cli_tag)
    monkeypatch.setattr(docker_module, "DEFAULT_IMAGE_TAG", cli_tag)
    docker = _docker(stack, monkeypatch)

    result = stack.upgrade("--yes")

    assert result.exit_code == 0, result.output
    assert docker.calls[-1] == "up -d"


def test_there_is_nothing_to_do_on_this_major(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = yaml.safe_load(stack.compose_file.read_text())
    config["services"]["surrealdb"]["image"] = SURREAL_IMAGE_REFERENCE
    stack.compose_file.write_text(yaml.dump(config, sort_keys=False))
    before = stack.files()
    docker = _docker(stack, monkeypatch)

    result = stack.upgrade("--yes")

    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output
    assert docker.calls == []
    assert stack.files() == before


def test_a_failed_disk_check_brings_the_held_stack_back(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_bytes = stack.compose_file.read_bytes()
    docker = _docker(stack, monkeypatch, used=10 * GIB, free=15 * GIB)

    result = stack.upgrade("--yes")

    assert result.exit_code == 1
    assert docker.calls == ["pull", f"stop {_writers(stack)} surrealdb", "copy", "measure", "up -d"]
    assert stack.compose_file.read_bytes() == held_bytes
    output = " ".join(result.output.split())
    assert "needs about 20.0 GiB free beside it, and only 15.0 GiB is" in output
    assert "The store was not converted" in output and "Nothing changed" in output
    # The copy is kept and named, never deleted for the user.
    assert f"docker volume rm {SURREAL_BACKUP_VOLUME}" in output
    assert "Never start" not in output


@pytest.mark.parametrize("fail", ["pull", "copy"])
def test_a_failed_step_before_the_conversion_brings_the_held_stack_back(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, fail: str
) -> None:
    held_bytes = stack.compose_file.read_bytes()
    docker = _docker(stack, monkeypatch, fail=fail)

    result = stack.upgrade("--yes")

    assert result.exit_code == 1
    assert docker.calls[-1] == "up -d"
    assert "measure" not in docker.calls
    assert stack.compose_file.read_bytes() == held_bytes
    output = " ".join(result.output.split())
    assert "Nothing changed" in output
    assert (f"docker volume rm {SURREAL_BACKUP_VOLUME}" in output) is (fail == "copy")


def test_a_conversion_timeout_prints_the_rollback_and_deletes_nothing(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _docker(stack, monkeypatch, ready_after=10**9)

    result = stack.upgrade("--yes")

    assert result.exit_code == 1
    assert docker.calls[:5] == [
        "pull",
        f"stop {_writers(stack)} surrealdb",
        "copy",
        "measure",
        "up -d --no-deps surrealdb",
    ]
    # Polled every 5 s for the 10 min floor; never restarted, stopped, or removed.
    assert set(docker.calls[5:]) == {"ready"} and len(docker.calls[5:]) == 121
    assert stack.api_waits == []
    assert _surreal(stack.compose_file)["environment"] == CONVERT_ENV
    assert stack.held_compose_file.exists()
    output = " ".join(result.output.split())
    assert "Still converting after" in output
    assert "did not finish converting within 10 min" in output
    assert f"sibyl {stack.runtime} logs surrealdb" in output
    assert f"cp {stack.held_compose_file} {stack.compose_file}" in output
    assert "Never start SurrealDB v3.2.4 on the converted volume" in output


def test_an_api_that_never_reports_ready_prints_the_rollback(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack.api_ready[0] = False
    docker = _docker(stack, monkeypatch)

    result = stack.upgrade("--yes")

    assert result.exit_code == 1
    assert docker.calls[-1] == "up -d"
    assert stack.api_waits == [600]
    assert _surreal(stack.compose_file)["environment"] == SURREAL_STORAGE_ENV
    output = " ".join(result.output.split())
    assert "may still be rebuilding the auth and content indexes" in output
    assert f"sibyl {stack.runtime} logs api" in output
    assert "Never start SurrealDB v3.2.4 on the converted volume" in output


def test_docker_waits_on_the_api_port_its_bundle_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(yaml.dump({"services": {"api": {"ports": ["127.0.0.1:4444:3334"]}}}))
    monkeypatch.setattr(docker_module, "SIBYL_DOCKER_COMPOSE", compose)
    urls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        docker_module, "wait_for_healthy", lambda timeout, url: urls.append((timeout, url)) or True
    )

    assert docker_module.wait_for_api(900) is True
    assert urls == [(900, "http://localhost:4444/api/health/ready")]
