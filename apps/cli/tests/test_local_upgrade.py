"""`sibyl local upgrade` must never leave a healthy server down or roll data backwards."""

from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from sibyl_cli import local
from sibyl_cli.docker_storage import SURREAL_IMAGE_REFERENCE, upgraded_surreal_image
from sibyl_cli.main import app


@pytest.fixture
def local_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    local_dir = tmp_path / "local"
    monkeypatch.setattr(local, "SIBYL_LOCAL_DIR", local_dir)
    monkeypatch.setattr(local, "SIBYL_LOCAL_ENV", local_dir / ".env")
    monkeypatch.setattr(local, "SIBYL_LOCAL_COMPOSE", local_dir / "docker-compose.yml")
    monkeypatch.setattr(local, "check_docker", lambda: True)
    monkeypatch.setattr(local, "check_docker_compose", lambda: True)
    monkeypatch.setattr(local, "container_owner", lambda name="sibyl-api": "local")
    local.write_compose_file(local.compose_config_for("1.4.0"))
    return local_dir / "docker-compose.yml"


class FakeCompose:
    """Records compose calls; `fail` names the calls that return non-zero, in order.

    `down_after_up` makes a failed `up -d` leave nothing running, as when
    Compose stops the old containers and cannot start the new ones.
    """

    def __init__(
        self,
        *,
        running: bool = True,
        fail: tuple[str, ...] = (),
        ps_fails: bool = False,
        ps_hangs: bool = False,
        down_after_up: bool = False,
    ) -> None:
        self.running = running
        self.down_after_up = down_after_up
        self.fail = list(fail)
        self.ps_fails = ps_fails
        self.ps_hangs = ps_hangs
        self.calls: list[tuple[str, str]] = []
        self.staged_files: list[Path] = []

    def __call__(
        self,
        args: list[str],
        capture: bool = False,
        compose_file: Path | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        name = " ".join(args)
        path = compose_file or local.SIBYL_LOCAL_COMPOSE
        self.calls.append((name, "staged" if compose_file else path.name))
        if compose_file:
            self.staged_files.append(compose_file)
        if name == "ps -q":
            if self.ps_hangs:
                raise subprocess.TimeoutExpired(args, timeout or 0)
            if self.ps_fails:
                return subprocess.CompletedProcess(args, 1, stdout="")
            return subprocess.CompletedProcess(args, 0, stdout="abc\n" if self.running else "")
        failed = bool(self.fail) and self.fail[0] == name
        if failed:
            self.fail.pop(0)
            if name == "up -d" and self.down_after_up:
                self.running = False
        return subprocess.CompletedProcess(args, 1 if failed else 0, stdout="")


def _invoke(monkeypatch: pytest.MonkeyPatch, compose: FakeCompose, healthy: list[bool]):
    monkeypatch.setattr(local, "run_compose", compose)
    monkeypatch.setattr(local, "wait_for_healthy", lambda timeout=120: healthy.pop(0))
    return CliRunner().invoke(app, ["local", "upgrade", "--tag", "1.5.0"])


def _pin(compose: Path) -> str | None:
    return local.pinned_image_tag(compose.read_text())


def test_a_failed_pull_changes_nothing(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = local_runtime.read_bytes()
    compose = FakeCompose(fail=("pull --quiet",))

    result = _invoke(monkeypatch, compose, healthy=[])

    assert result.exit_code == 1
    assert "Nothing changed; the pin stays on 1.4.0" in result.output
    assert local_runtime.read_bytes() == before
    assert not list(local_runtime.parent.glob("docker-compose.*.next.yml"))
    # The running containers were never stopped or recreated.
    assert compose.calls == [("ps -q", "docker-compose.yml"), ("pull --quiet", "staged")]


def test_images_are_pulled_before_anything_restarts(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = FakeCompose()

    result = _invoke(monkeypatch, compose, healthy=[True])

    assert result.exit_code == 0, result.output
    assert _pin(local_runtime) == "1.5.0"
    assert compose.calls == [
        ("ps -q", "docker-compose.yml"),
        ("pull --quiet", "staged"),
        ("up -d", "docker-compose.yml"),
    ]


@pytest.mark.parametrize(
    ("fail", "healthy", "says"),
    [
        # The new images started but never reported healthy, e.g. a long migration.
        ((), [False], "running 1.5.0 but did not report healthy"),
        # Compose recreated some services, then failed on another.
        (("up -d",), [], "could not start every service on 1.5.0"),
    ],
)
def test_a_failed_start_keeps_the_new_pins_and_never_rolls_back(
    local_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: tuple[str, ...],
    healthy: list[bool],
    says: str,
) -> None:
    """Rolling back would boot an older API on a schema the new SurrealDB and API migrated."""
    compose = FakeCompose(fail=fail)

    result = _invoke(monkeypatch, compose, healthy=healthy)

    assert result.exit_code == 1
    assert says in result.output
    assert "The pins stay on 1.5.0" in result.output
    assert "sibyl local logs" in result.output
    assert "sibyl local upgrade --tag 1.5.0" in result.output
    # No restore advice, which `sibyl up` would ignore with a container present.
    assert "SIBYL_IMAGE_TAG=1.4.0" not in result.output
    assert _pin(local_runtime) == "1.5.0"
    assert [name for name, _ in compose.calls].count("up -d") == 1


def test_a_failed_start_that_left_nothing_running_says_to_start_it(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying `local upgrade` would find nothing running and only print advice."""
    compose = FakeCompose(fail=("up -d",), down_after_up=True)

    result = _invoke(monkeypatch, compose, healthy=[])

    assert result.exit_code == 1
    assert "Nothing is running now" in result.output
    assert "SIBYL_IMAGE_TAG=1.5.0 sibyl up" in result.output
    assert "Retry the start with" not in result.output


def test_the_start_advice_spells_out_the_tag_even_for_this_clis_own(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under `update` the tag is always this build's default, but the typed `sibyl` may be older."""
    monkeypatch.setattr(local, "DEFAULT_IMAGE_TAG", "1.5.0")
    compose = FakeCompose(fail=("up -d",), down_after_up=True)

    result = _invoke(monkeypatch, compose, healthy=[])

    assert result.exit_code == 1
    assert "SIBYL_IMAGE_TAG=1.5.0 sibyl up" in result.output


def test_sibyl_up_keeps_a_newer_surrealdb(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start the retry advice leads to must not undo what `local upgrade` kept."""
    newer = "${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.9.0}"
    config = yaml.safe_load(local_runtime.read_text())
    config["services"]["surrealdb"]["image"] = newer
    local.write_compose_file(config)

    local.write_compose_file()

    services = yaml.safe_load(local_runtime.read_text())["services"]
    assert services["surrealdb"]["image"] == newer
    assert services["api"]["image"].endswith(f":{local.DEFAULT_IMAGE_TAG}")


def test_surrealdb_is_never_moved_backwards(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    newer = "${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.9.0}"
    config = yaml.safe_load(local_runtime.read_text())
    config["services"]["surrealdb"]["image"] = newer
    local.write_compose_file(config)

    result = _invoke(monkeypatch, FakeCompose(), healthy=[True])

    assert result.exit_code == 0, result.output
    services = yaml.safe_load(local_runtime.read_text())["services"]
    assert services["surrealdb"]["image"] == newer
    assert services["api"]["image"].endswith(":1.5.0")


def test_an_older_cli_written_surrealdb_moves_up(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = yaml.safe_load(local_runtime.read_text())
    config["services"]["surrealdb"]["image"] = "${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.2.3}"
    local.write_compose_file(config)

    result = _invoke(monkeypatch, FakeCompose(), healthy=[True])

    assert result.exit_code == 0, result.output
    services = yaml.safe_load(local_runtime.read_text())["services"]
    assert services["surrealdb"]["image"] == SURREAL_IMAGE_REFERENCE


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        (SURREAL_IMAGE_REFERENCE, SURREAL_IMAGE_REFERENCE),
        ("${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.2.3}", SURREAL_IMAGE_REFERENCE),
        ("${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.9.0}", None),
        ("surrealdb/surrealdb:v3.2.1", None),
        ("${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:nightly}", None),
    ],
)
def test_the_shared_surrealdb_rule(image: str, expected: str | None) -> None:
    assert upgraded_surreal_image(image) == expected


def test_a_concurrent_upgrade_fails_cleanly(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = local_runtime.read_bytes()
    compose = FakeCompose()
    with open(local_runtime.parent / ".upgrade.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _invoke(monkeypatch, compose, healthy=[])

    assert result.exit_code == 1
    assert "Another sibyl local upgrade is running" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert compose.calls == []
    assert local_runtime.read_bytes() == before


def test_each_upgrade_stages_its_own_file(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = FakeCompose(fail=("pull --quiet",)), FakeCompose(fail=("pull --quiet",))

    _invoke(monkeypatch, first, healthy=[])
    _invoke(monkeypatch, second, healthy=[])

    assert first.staged_files and second.staged_files
    assert first.staged_files[0] != second.staged_files[0]
    assert first.staged_files[0].parent == local_runtime.parent


@pytest.mark.parametrize("probe", ["fails", "hangs"])
def test_an_unanswered_state_probe_changes_nothing(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    """A failing or hung `compose ps` is not "stopped"; `sibyl up` would be a no-op."""
    before = local_runtime.read_bytes()
    compose = FakeCompose(ps_fails=probe == "fails", ps_hangs=probe == "hangs")

    result = _invoke(monkeypatch, compose, healthy=[])

    assert result.exit_code == 1
    assert "could not report" in result.output
    assert "sibyl up --pull" not in result.output
    assert compose.calls == [("ps -q", "docker-compose.yml")]
    assert local_runtime.read_bytes() == before


def test_an_unreadable_pin_refuses_to_upgrade(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_runtime.write_text("services: {}\n")
    compose = FakeCompose()

    result = _invoke(monkeypatch, compose, healthy=[])

    assert result.exit_code == 1
    assert "Could not read the image tag" in result.output
    assert compose.calls == [("ps -q", "docker-compose.yml")]
    assert local_runtime.read_text() == "services: {}\n"


def test_a_stopped_instance_is_not_started(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = local_runtime.read_bytes()
    compose = FakeCompose(running=False)

    result = _invoke(monkeypatch, compose, healthy=[])

    assert result.exit_code == 0, result.output
    assert "nothing to upgrade in place" in result.output
    assert "SIBYL_IMAGE_TAG=1.5.0 sibyl up --pull" in result.output
    assert compose.calls == [("ps -q", "docker-compose.yml")]
    assert local_runtime.read_bytes() == before


def test_a_docker_owned_server_is_sent_to_docker_upgrade(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both runtimes name their API `sibyl-api`; the advice must name the owner's command."""
    monkeypatch.setattr(local, "container_owner", lambda name="sibyl-api": "docker")
    compose = FakeCompose(running=False)

    result = _invoke(monkeypatch, compose, healthy=[])

    assert result.exit_code == 0, result.output
    assert "sibyl docker upgrade" in result.output
    assert "sibyl up --pull" not in result.output

    local_runtime.unlink()
    result = _invoke(monkeypatch, FakeCompose(), healthy=[])
    assert result.exit_code == 1
    assert "sibyl docker upgrade" in result.output
    assert "Run 'sibyl up' first" not in result.output


def test_start_names_the_owning_runtimes_upgrade(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local, "is_running", lambda: True)
    monkeypatch.setattr(local, "container_owner", lambda name="sibyl-api": "docker")

    result = CliRunner().invoke(app, ["up", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert "sibyl docker upgrade" in result.output
    assert "sibyl local upgrade" not in result.output


@pytest.mark.parametrize(
    ("config_files", "owner"),
    [
        ("{local}/docker-compose.yml", "local"),
        ("{docker}/docker-compose.yml", "docker"),
        ("/elsewhere/compose.yml", None),
        ("", None),
    ],
)
def test_container_owner_reads_the_compose_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_files: str, owner: str | None
) -> None:
    local_dir = tmp_path / ".sibyl" / "local"
    monkeypatch.setattr(local, "SIBYL_LOCAL_DIR", local_dir)
    label = config_files.format(local=local_dir, docker=tmp_path / ".sibyl" / "docker")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert kwargs["timeout"] == local.DOCKER_PROBE_TIMEOUT_SECONDS
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{label}\n")

    monkeypatch.setattr(local.subprocess, "run", fake_run)

    assert local.container_owner() == owner
    assert calls[0][:2] == ["docker", "inspect"]


def test_check_docker_gives_up_on_a_wedged_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local.shutil, "which", lambda _name: "/usr/bin/docker")

    def wedged(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == local.DOCKER_PROBE_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(cmd, local.DOCKER_PROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(local.subprocess, "run", wedged)

    assert local.check_docker() is False


def test_compose_config_for_only_moves_the_sibyl_images() -> None:
    config = local.compose_config_for("1.5.0")
    services = config["services"]

    assert services["api"]["image"] == "ghcr.io/hyperb1iss/sibyl-api:1.5.0"
    assert services["web"]["image"] == "ghcr.io/hyperb1iss/sibyl-web:1.5.0"
    assert services["surrealdb"]["image"] == SURREAL_IMAGE_REFERENCE
    # The shared default is never mutated.
    assert local.COMPOSE_CONFIG["services"]["api"]["image"].endswith(f":{local.DEFAULT_IMAGE_TAG}")
