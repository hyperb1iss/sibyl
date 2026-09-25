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
from sibyl_cli.local import ComposeContainer
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
        running_tag: str = "1.4.0",
        api_image: str | None = None,
        fail: tuple[str, ...] = (),
        ps_fails: bool = False,
        ps_hangs: bool = False,
        down_after_up: bool = False,
    ) -> None:
        self.running = running
        self.running_tag = running_tag
        self.api_image = api_image
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
        failed = bool(self.fail) and self.fail[0] == name
        if failed:
            self.fail.pop(0)
            if name == "up -d" and self.down_after_up:
                self.running = False
        elif name == "up -d":
            self.running = True
            self.running_tag = local.pinned_image_tag(path.read_text()) or self.running_tag
        return subprocess.CompletedProcess(args, 1 if failed else 0, stdout="")

    def containers(self) -> list[ComposeContainer] | None:
        """What `docker ps` reports for this runtime's compose file."""
        if self.ps_fails or self.ps_hangs:
            return None
        if not self.running:
            return []
        files = (local.SIBYL_LOCAL_COMPOSE.resolve(),)
        api_image = self.api_image or f"ghcr.io/hyperb1iss/sibyl-api:{self.running_tag}"
        return [
            ComposeContainer(
                files, "surrealdb", "surrealdb/surrealdb:v3.2.4", "s1", "sibyl-surrealdb"
            ),
            ComposeContainer(files, "api", api_image, "a1", "sibyl-api"),
        ]


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    compose: FakeCompose,
    healthy: list[bool],
    args: tuple[str, ...] = ("--tag", "1.5.0"),
):
    monkeypatch.setattr(local, "run_compose", compose)
    monkeypatch.setattr(local, "running_compose_containers", compose.containers)
    monkeypatch.setattr(local, "wait_for_healthy", lambda timeout=120: healthy.pop(0))
    return CliRunner().invoke(app, ["local", "upgrade", *args])


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
    assert compose.calls == [("pull --quiet", "staged")]


def test_images_are_pulled_before_anything_restarts(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = FakeCompose()

    result = _invoke(monkeypatch, compose, healthy=[True])

    assert result.exit_code == 0, result.output
    assert _pin(local_runtime) == "1.5.0"
    assert compose.calls == [("pull --quiet", "staged"), ("up -d", "docker-compose.yml")]


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
    assert compose.calls == []
    assert local_runtime.read_bytes() == before


def test_an_unreadable_pin_refuses_to_upgrade(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_runtime.write_text("services: {}\n")
    compose = FakeCompose()

    result = _invoke(monkeypatch, compose, healthy=[])

    assert result.exit_code == 1
    assert "Could not read the image tag" in result.output
    assert compose.calls == []
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
    assert compose.calls == []
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


@pytest.mark.parametrize(
    ("running", "running_tag", "pin", "says"),
    [
        # The server already runs a newer API than this CLI's.
        (True, "1.6.0", "1.6.0", "on 1.6.0, newer than this CLI's 1.5.0"),
        # An interrupted upgrade left the pin behind what runs.
        (True, "1.6.0", "1.4.0", "on 1.6.0, newer than this CLI's 1.5.0"),
        # Nothing runs, but the pins name a newer release.
        (False, "1.4.0", "1.6.0", "on 1.6.0, newer than this CLI's 1.5.0"),
        # rc tags compare as releases: 1.5.0-rc.2 is older than 1.5.0.
        (True, "1.5.1-rc.1", "1.5.1-rc.1", "on 1.5.1-rc.1, newer than this CLI's 1.5.0"),
    ],
)
def test_a_tagless_upgrade_never_moves_the_api_backwards(
    local_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    running: bool,
    running_tag: str,
    pin: str,
    says: str,
) -> None:
    """`sibyl up` and the installer both advise the tagless form, and the CLI can be older."""
    monkeypatch.setattr(local, "DEFAULT_IMAGE_TAG", "1.5.0")
    local.write_compose_file(local.compose_config_for(pin))
    before = local_runtime.read_bytes()
    compose = FakeCompose(running=running, running_tag=running_tag)

    result = _invoke(monkeypatch, compose, healthy=[], args=())

    assert result.exit_code == 1
    assert says in result.output
    assert "pass --tag" in result.output
    assert compose.calls == []
    assert local_runtime.read_bytes() == before


def test_an_explicit_tag_may_move_the_api_backwards(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local.write_compose_file(local.compose_config_for("1.6.0"))
    compose = FakeCompose(running_tag="1.6.0")

    result = _invoke(monkeypatch, compose, healthy=[True])

    assert result.exit_code == 0, result.output
    assert _pin(local_runtime) == "1.5.0"


def test_a_tagless_upgrade_moves_forward_to_this_clis_tag(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local, "DEFAULT_IMAGE_TAG", "1.5.0")
    compose = FakeCompose(running_tag="1.5.0-rc.2")
    local.write_compose_file(local.compose_config_for("1.5.0-rc.2"))

    result = _invoke(monkeypatch, compose, healthy=[True], args=())

    assert result.exit_code == 0, result.output
    assert _pin(local_runtime) == "1.5.0"


def test_a_killed_upgrade_leaves_no_staged_files_behind(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orphan = local_runtime.with_name("docker-compose.k1ll3d.next.yml")
    orphan.write_text("services: {}\n")

    result = _invoke(monkeypatch, FakeCompose(), healthy=[True])

    assert result.exit_code == 0, result.output
    assert not orphan.exists()
    assert not list(local_runtime.parent.glob("docker-compose.*.next.yml"))


def test_the_swap_keeps_the_compose_files_mode(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_runtime.chmod(0o644)

    result = _invoke(monkeypatch, FakeCompose(), healthy=[True])

    assert result.exit_code == 0, result.output
    assert _pin(local_runtime) == "1.5.0"
    assert local_runtime.stat().st_mode & 0o777 == 0o644


def test_an_unknown_owner_names_both_upgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local, "container_owner", lambda name="sibyl-api": None)

    advice = local.upgrade_advice_for_running_server()

    assert "sibyl local upgrade" in advice
    assert "sibyl docker upgrade" in advice
    assert "does not say" in advice


def test_containers_are_matched_on_the_compose_file_not_the_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ours = tmp_path / ".sibyl" / "local" / "docker-compose.yml"
    foreign = tmp_path / "work" / "shop" / "local" / "docker-compose.yml"
    listing = (
        f"{foreign}\tapi\tmycorp/api:1.0.3\n"
        f"{ours}\tapi\tghcr.io/hyperb1iss/sibyl-api:1.4.0\n"
        f"{ours}\tsurrealdb\tsurrealdb/surrealdb:v3.2.4\n"
        "\t\tbare-container:latest\n"
    )

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:3] == ["docker", "ps", "--no-trunc"]
        assert kwargs["timeout"] == local.DOCKER_PROBE_TIMEOUT_SECONDS
        return subprocess.CompletedProcess(cmd, 0, stdout=listing)

    monkeypatch.setattr(local.subprocess, "run", fake_run)

    ours_running = local.containers_for(ours)
    assert ours_running is not None
    assert [c.service for c in ours_running] == ["api", "surrealdb"]
    assert local.running_api_tag(ours) == "1.4.0"
    assert local.containers_for(foreign.with_name("other.yml")) == []

    def wedged(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(local.subprocess, "run", wedged)
    assert local.containers_for(ours) is None


@pytest.mark.parametrize("created_from", ["ghcr.io/hyperb1iss/sibyl-api:1.6.0", None])
def test_a_moved_tag_never_hides_a_newer_api(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch, created_from: str | None
) -> None:
    """`docker ps` shows `sha256:<hex>` after the tag moves; that must not skip the refusal."""
    monkeypatch.setattr(local, "DEFAULT_IMAGE_TAG", "1.5.0")
    local.write_compose_file(local.compose_config_for("1.6.0"))
    compose = FakeCompose(api_image="sha256:" + "9f" * 32)
    inspected: list[list[str]] = []

    def fake_inspect(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        inspected.append(cmd)
        if created_from is None:
            return subprocess.CompletedProcess(cmd, 1, stdout="")
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{created_from}\n")

    monkeypatch.setattr(local.subprocess, "run", fake_inspect)

    result = _invoke(monkeypatch, compose, healthy=[], args=())

    # Either the creating image or, failing that, the pin says 1.6.0.
    assert result.exit_code == 1
    assert "on 1.6.0, newer than this CLI's 1.5.0" in result.output
    assert inspected[0] == ["docker", "inspect", "--format", "{{.Config.Image}}", "a1"]
    assert compose.calls == []


def test_a_running_api_no_label_claims_is_not_called_stopped(
    local_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing or stale Compose label must not send the user round `sibyl up` again."""
    monkeypatch.setattr(local, "container_owner", lambda name="sibyl-api": None)
    compose = FakeCompose(running=False)
    stale = (Path("/somewhere/else/docker-compose.yml"),)
    monkeypatch.setattr(local, "run_compose", compose)
    monkeypatch.setattr(
        local,
        "running_compose_containers",
        lambda: [
            ComposeContainer(stale, "api", "ghcr.io/hyperb1iss/sibyl-api:1.4.0", "a1", "sibyl-api")
        ],
    )
    monkeypatch.setattr(local, "wait_for_healthy", lambda timeout=120: True)

    result = CliRunner().invoke(app, ["local", "upgrade", "--tag", "1.5.0"])

    assert result.exit_code == 1
    assert "owner is unknown" in result.output
    assert "sibyl local upgrade or sibyl docker upgrade" in result.output
    assert "not running" not in result.output
    assert "sibyl up --pull" not in result.output
    assert compose.calls == []


@pytest.mark.parametrize(
    ("image", "tag"),
    [
        ("ghcr.io/hyperb1iss/sibyl-api:1.4.0", "1.4.0"),
        ("ghcr.io/hyperb1iss/sibyl-api:1.5.0-rc.2", "1.5.0-rc.2"),
        ("ghcr.io/hyperb1iss/sibyl-api:main", "main"),
        ("sha256:" + "ab" * 32, None),
        ("ghcr.io/hyperb1iss/sibyl-api:" + "ab12" * 3, None),
        ("localhost:5000/sibyl-api", None),
        ("", None),
    ],
)
def test_tag_of_refuses_image_ids(image: str, tag: str | None) -> None:
    assert local.tag_of(image) == tag


def test_the_listing_keeps_ids_and_names_and_drops_relative_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = (
        "/abs/local/docker-compose.yml\tapi\tghcr.io/hyperb1iss/sibyl-api:1.4.0\tc1\tsibyl-api\n"
        "docker-compose.yml\tapi\tghcr.io/hyperb1iss/sibyl-api:1.4.0\tc2\told-api\n"
        "\t\tbusybox:1.37\tc3\tloose\n"
    )
    monkeypatch.setattr(
        local.subprocess,
        "run",
        lambda cmd, **_: subprocess.CompletedProcess(cmd, 0, stdout=listing),
    )

    containers = local.running_compose_containers()

    assert containers is not None
    assert [(c.container_id, c.name) for c in containers] == [
        ("c1", "sibyl-api"),
        ("c2", "old-api"),
        ("c3", "loose"),
    ]
    assert containers[0].config_files == (Path("/abs/local/docker-compose.yml").resolve(),)
    # A relative label cannot be placed, so it claims nothing.
    assert containers[1].config_files == ()
    assert local.unclaimed_api_running((Path("/abs/local/docker-compose.yml"),)) is False
    assert local.unclaimed_api_running((Path("/other/docker-compose.yml"),)) is True
