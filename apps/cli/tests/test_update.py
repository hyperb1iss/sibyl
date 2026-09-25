"""Tests for the self-updater module."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli import docker as docker_module
from sibyl_cli import local as local_module
from sibyl_cli import update as update_module
from sibyl_cli.update import (
    ContainerPlan,
    ContainerRuntime,
    ContainerTarget,
    cli_update_available,
    container_target,
    get_current_cli_version,
    get_latest_cli_version,
    installed_container_runtimes,
    is_dev_mode,
    plan_container_upgrades,
    upgrade_container_runtime,
)


class TestDevModeDetection:
    """Tests for is_dev_mode() detection."""

    def test_dev_mode_when_skills_are_symlinks(self, tmp_path: Path) -> None:
        """Detect dev mode when skills directory is a symlink."""
        # Create a fake symlinked skill
        skill_dir = tmp_path / ".claude" / "skills" / "sibyl"
        skill_dir.parent.mkdir(parents=True)
        target = tmp_path / "repo" / "skills" / "sibyl"
        target.mkdir(parents=True)
        skill_dir.symlink_to(target)

        with patch("sibyl_cli.update.Path.home", return_value=tmp_path):
            assert is_dev_mode() is True

    def test_not_dev_mode_when_skills_are_copies(self, tmp_path: Path) -> None:
        """Not dev mode when skills are regular directories."""
        skill_dir = tmp_path / ".claude" / "skills" / "sibyl"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("test")

        with (
            patch("sibyl_cli.update.Path.home", return_value=tmp_path),
            patch("sibyl_cli.update.Path.cwd", return_value=tmp_path / "some" / "other"),
        ):
            # Also need to create the cwd path
            (tmp_path / "some" / "other").mkdir(parents=True)
            assert is_dev_mode() is False

    def test_dev_mode_when_in_repo_directory(self, tmp_path: Path) -> None:
        """Detect dev mode when cwd is in the sibyl repo."""
        repo = tmp_path / "dev" / "sibyl"
        (repo / "apps" / "cli").mkdir(parents=True)
        (repo / "moon.yml").write_text("test")

        # Skills not symlinked
        skill_dir = tmp_path / ".claude" / "skills" / "sibyl"
        skill_dir.mkdir(parents=True)

        with (
            patch("sibyl_cli.update.Path.home", return_value=tmp_path),
            patch("sibyl_cli.update.Path.cwd", return_value=repo),
        ):
            assert is_dev_mode() is True

    def test_dev_mode_when_in_repo_subdirectory(self, tmp_path: Path) -> None:
        """Detect dev mode when cwd is a subdirectory of the repo."""
        repo = tmp_path / "dev" / "sibyl"
        subdir = repo / "apps" / "cli" / "src"
        subdir.mkdir(parents=True)
        (repo / "moon.yml").write_text("test")

        # Skills not symlinked
        skill_dir = tmp_path / ".claude" / "skills" / "sibyl"
        skill_dir.mkdir(parents=True)

        with (
            patch("sibyl_cli.update.Path.home", return_value=tmp_path),
            patch("sibyl_cli.update.Path.cwd", return_value=subdir),
        ):
            assert is_dev_mode() is True


class TestVersionChecking:
    """Tests for version checking functions."""

    def test_get_current_cli_version_returns_version(self) -> None:
        """get_current_cli_version returns the installed version."""
        with patch("sibyl_cli.update.pkg_version", return_value="0.1.0"):
            assert get_current_cli_version() == "0.1.0"

    def test_get_current_cli_version_handles_not_installed(self) -> None:
        """get_current_cli_version returns None if not installed."""
        with patch("sibyl_cli.update.pkg_version", side_effect=Exception("not found")):
            assert get_current_cli_version() is None

    def test_get_latest_cli_version_from_pypi(self) -> None:
        """get_latest_cli_version fetches from PyPI."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"info": {"version": "0.2.0"}}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("sibyl_cli.update.urllib.request.urlopen", return_value=mock_response):
            assert get_latest_cli_version() == "0.2.0"

    def test_get_latest_cli_version_handles_network_error(self) -> None:
        """get_latest_cli_version returns None on network error."""
        with patch(
            "sibyl_cli.update.urllib.request.urlopen",
            side_effect=Exception("network error"),
        ):
            assert get_latest_cli_version() is None

    def test_cli_update_available_when_newer_version(self) -> None:
        """cli_update_available returns True when newer version exists."""
        with (
            patch("sibyl_cli.update.get_current_cli_version", return_value="0.1.0"),
            patch("sibyl_cli.update.get_latest_cli_version", return_value="0.2.0"),
        ):
            current, latest, available = cli_update_available()
            assert current == "0.1.0"
            assert latest == "0.2.0"
            assert available is True

    def test_cli_update_available_when_same_version(self) -> None:
        """cli_update_available returns False when versions match."""
        with (
            patch("sibyl_cli.update.get_current_cli_version", return_value="0.2.0"),
            patch("sibyl_cli.update.get_latest_cli_version", return_value="0.2.0"),
        ):
            current, latest, available = cli_update_available()
            assert current == "0.2.0"
            assert latest == "0.2.0"
            assert available is False

    def test_cli_update_available_handles_none_versions(self) -> None:
        """cli_update_available handles None versions gracefully."""
        with (
            patch("sibyl_cli.update.get_current_cli_version", return_value=None),
            patch("sibyl_cli.update.get_latest_cli_version", return_value="0.2.0"),
        ):
            current, latest, available = cli_update_available()
            assert current is None
            assert latest == "0.2.0"
            assert available is False

    def test_cli_update_available_handles_prerelease(self) -> None:
        """cli_update_available handles pre-release versions correctly."""
        with (
            patch("sibyl_cli.update.get_current_cli_version", return_value="0.1.0"),
            patch("sibyl_cli.update.get_latest_cli_version", return_value="0.2.0a1"),
        ):
            _current, _latest, available = cli_update_available()
            # 0.2.0a1 > 0.1.0 according to PEP 440
            assert available is True


def _runtime(tmp_path: Path, name: str = "docker", tag: str | None = "1.4.0") -> ContainerRuntime:
    compose = tmp_path / name / "docker-compose.yml"
    compose.parent.mkdir(parents=True, exist_ok=True)
    _pin(compose, tag)
    return ContainerRuntime(name, compose, compose.parent / ".env")


def _pin(compose: Path, tag: str | None) -> None:
    image = "ghcr.io/hyperb1iss/sibyl-api" + (f":{tag}" if tag else "")
    compose.write_text(f"services:\n  api:\n    image: {image}\n")


def _plan(
    tmp_path: Path,
    current: str | None,
    target: str | None,
    *,
    running: bool | None = True,
    name: str = "docker",
    source: str = "CLI",
) -> ContainerPlan:
    return ContainerPlan(
        _runtime(tmp_path, name, current), current, ContainerTarget(target, source), running
    )


def _ps_line(compose: Path, service: str, image: str) -> str:
    return f"{compose}\t{service}\t{image}"


def _docker_probe(
    cmd: list[str], running_tag: str | None, *, project_up: bool | None = None
) -> subprocess.CompletedProcess[str]:
    """Answer `docker ps` for the registered compose files.

    Each one runs SurrealDB while its project is up, plus an API on
    `running_tag` when that is set.
    """
    if cmd[:2] != ["docker", "ps"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="")
    up = bool(running_tag) if project_up is None else project_up
    lines = []
    for compose in _PINS:
        if up:
            lines.append(_ps_line(compose, "surrealdb", "surrealdb/surrealdb:v3.2.4"))
        if running_tag:
            lines.append(_ps_line(compose, "api", f"ghcr.io/hyperb1iss/sibyl-api:{running_tag}"))
    return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(lines) + "\n")


def _upgrader(
    calls: list[list[str]],
    envs: list[dict[str, str]] | None = None,
    *,
    returncode: int = 0,
    move_pin: bool = True,
    running: str | None = None,
    starts: bool = True,
):
    """A stand-in for the PATH `sibyl` and Docker.

    `sibyl <runtime> upgrade` calls are recorded and move the pin like the real
    ones; a successful one that `starts` also moves the running API container.
    Docker probes answer from that running tag, None meaning no API runs.
    """
    state = {"running": running}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] != "sibyl":
            return _docker_probe(cmd, state["running"])
        calls.append(cmd)
        if envs is not None:
            envs.append(kwargs.get("env") or {})  # type: ignore[arg-type]
        if move_pin and cmd[2:4] == ["upgrade", "--tag"]:
            for compose in _PINS:
                if compose.parent.name == cmd[1]:
                    _pin(compose, cmd[4])
            if returncode == 0 and starts and state["running"]:
                state["running"] = cmd[4]
        return subprocess.CompletedProcess(cmd, returncode)

    return fake_run


# Compose files the fake upgrader may move, registered by `_runtime` users.
_PINS: list[Path] = []


@pytest.fixture(autouse=True)
def _reset_pins() -> None:
    _PINS.clear()


class TestContainerRuntimes:
    """`update` finds runtimes where `sibyl up` and `sibyl docker init` put them."""

    def test_finds_the_files_the_runtime_commands_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        docker_dir = tmp_path / ".sibyl" / "docker"
        local_dir = tmp_path / ".sibyl" / "local"
        monkeypatch.setattr(docker_module, "SIBYL_DOCKER_DIR", docker_dir)
        monkeypatch.setattr(docker_module, "SIBYL_DOCKER_ENV", docker_dir / ".env")
        monkeypatch.setattr(
            docker_module, "SIBYL_DOCKER_COMPOSE", docker_dir / "docker-compose.yml"
        )
        monkeypatch.setattr(local_module, "SIBYL_LOCAL_DIR", local_dir)
        monkeypatch.setattr(local_module, "SIBYL_LOCAL_ENV", local_dir / ".env")
        monkeypatch.setattr(local_module, "SIBYL_LOCAL_COMPOSE", local_dir / "docker-compose.yml")

        assert installed_container_runtimes() == []

        docker_module.write_env_file(image_tag="1.3.0", surreal_password="p", jwt_secret="s")
        docker_module.write_compose_file(
            docker_module.compose_config(
                image_tag="1.3.0",
                api_port=3334,
                web_port=3337,
                surreal_port=8000,
                with_worker=False,
                with_crawler=True,
            )
        )
        local_module.write_compose_file()

        runtimes = installed_container_runtimes()
        assert [runtime.name for runtime in runtimes] == ["docker", "local"]
        assert [runtime.image_tag() for runtime in runtimes] == [
            "1.3.0",
            local_module.DEFAULT_IMAGE_TAG,
        ]

    def test_image_tag_is_none_when_the_compose_file_does_not_pin_one(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)
        for content in (
            "services:\n  api:\n    image: localhost:5000/sibyl-api\n",
            "services:\n  web: {}\n",
            "services: [\n",
        ):
            runtime.compose_file.write_text(content)
            assert runtime.image_tag() is None

    def test_is_running_matches_the_compose_file_not_the_project_name(self, tmp_path: Path) -> None:
        """Another project in a directory named `docker` must not read as this runtime."""
        runtime = _runtime(tmp_path, name="docker")
        foreign = tmp_path / "work" / "shop" / "docker" / "docker-compose.yml"
        listing = _ps_line(foreign, "api", "mycorp/api:1.0.3") + "\n"

        with patch(
            "sibyl_cli.update.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=listing),
        ) as run:
            assert runtime.is_running() is False
            assert runtime.running_api_tag() is None
        assert run.call_args.args[0][:3] == ["docker", "ps", "--no-trunc"]
        assert run.call_args.kwargs["timeout"] == update_module.DOCKER_PROBE_TIMEOUT_SECONDS

        listing += _ps_line(runtime.compose_file, "api", "ghcr.io/hyperb1iss/sibyl-api:1.4.0")
        with patch(
            "sibyl_cli.update.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=listing + "\n"),
        ):
            assert runtime.is_running() is True
            assert runtime.running_api_tag() == "1.4.0"

    def test_is_running_is_false_without_docker(self, tmp_path: Path) -> None:
        with patch("sibyl_cli.update.subprocess.run", side_effect=FileNotFoundError("docker")):
            assert _runtime(tmp_path).is_running() is False

    def test_running_api_tag_is_none_without_an_api_container(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path, tag="1.5.0")
        _PINS.append(runtime.compose_file)
        with patch(
            "sibyl_cli.update.subprocess.run",
            side_effect=lambda cmd, **_: _docker_probe(cmd, None, project_up=True),
        ):
            assert runtime.is_running() is True
            assert runtime.running_api_tag() is None
        with patch(
            "sibyl_cli.update.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["docker"], 10),
        ):
            assert runtime.running_api_tag() is None

    def test_is_running_is_unknown_when_docker_is_wedged(self, tmp_path: Path) -> None:
        wedged = subprocess.TimeoutExpired(["docker"], update_module.DOCKER_PROBE_TIMEOUT_SECONDS)
        with patch("sibyl_cli.update.subprocess.run", side_effect=wedged) as run:
            assert _runtime(tmp_path).is_running() is None
        assert run.call_args.kwargs["timeout"] == update_module.DOCKER_PROBE_TIMEOUT_SECONDS


class TestContainerTargets:
    """Only versions with a published image become targets."""

    @pytest.mark.parametrize(
        ("version", "tag"),
        [
            ("1.4.0", "1.4.0"),
            ("1.5.0rc2", "1.5.0-rc.2"),
            ("1.4.0+g1a2b3c", None),
            ("1.5.0.dev3+gabc123", None),
            ("1.4.0.dev0", None),
            ("1.4.0.post1", None),
            ("1.5.0a1", None),
            ("1.5.0b2", None),
            ("1.4", None),
            ("1!1.4.0", None),
            ("1.4.0rc01", "1.4.0-rc.1"),
            (None, None),
        ],
    )
    def test_only_releases_and_rcs_have_a_target(
        self, monkeypatch: pytest.MonkeyPatch, version: str | None, tag: str | None
    ) -> None:
        monkeypatch.delenv("SIBYL_IMAGE_TAG", raising=False)
        target = container_target(version)
        assert target.tag == tag
        if tag is None:
            assert target.reason

    def test_a_dev_build_says_why_it_leaves_containers_alone(self, tmp_path: Path) -> None:
        plan = ContainerPlan(
            _runtime(tmp_path), "1.4.0", container_target("1.5.0.dev3+gabc123"), True
        )
        assert plan.applies is False
        assert "1.5.0.dev3+gabc123 is not a release" in plan.status()

    def test_the_override_wins_and_names_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIBYL_IMAGE_TAG", "1.6.0")
        assert container_target("1.5.0.dev3") == ContainerTarget("1.6.0", source="SIBYL_IMAGE_TAG")


class TestContainerPlans:
    """Runtimes follow the CLI version and are only upgraded while running."""

    def test_target_is_the_image_tag_of_the_cli_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SIBYL_IMAGE_TAG", raising=False)
        runtime = _runtime(tmp_path, tag="1.4.0")
        monkeypatch.setattr(update_module, "installed_container_runtimes", lambda: [runtime])
        monkeypatch.setattr(ContainerRuntime, "is_running", lambda _self: True)
        monkeypatch.setattr(ContainerRuntime, "running_api_tag", lambda _self: "1.4.0")

        [plan] = plan_container_upgrades("1.5.0rc2")

        assert (plan.current_tag, plan.target_tag) == ("1.4.0", "1.5.0-rc.2")
        assert plan.applies is True
        assert plan_container_upgrades(None)[0].target_tag is None

        # The override `sibyl up` honours wins here too, and a custom tag is
        # never compared against a release.
        monkeypatch.setenv("SIBYL_IMAGE_TAG", "edge")
        [plan] = plan_container_upgrades("1.5.0")
        assert (plan.target_tag, plan.applies) == ("edge", False)

    @pytest.mark.parametrize(
        ("current", "target", "running", "behind", "applies"),
        [
            ("1.4.0", "1.5.0", True, True, True),
            ("1.0.0-rc.8", "1.0.0", True, True, True),
            ("1.4.0", "1.5.0", False, True, False),
            ("1.4.0", "1.5.0", None, True, False),
            ("1.5.0", "1.5.0", True, False, False),
            ("1.6.0", "1.5.0", True, False, False),
            ("main", "1.5.0", True, False, False),
            (None, "1.5.0", True, False, False),
            ("1.4.0", None, True, False, False),
        ],
    )
    def test_only_a_running_runtime_behind_the_cli_is_upgraded(
        self,
        tmp_path: Path,
        current: str | None,
        target: str | None,
        *,
        running: bool | None,
        behind: bool,
        applies: bool,
    ) -> None:
        plan = _plan(tmp_path, current, target, running=running)
        assert (plan.behind, plan.applies) == (behind, applies)

    def test_an_interrupted_upgrade_still_counts_as_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pin says 1.5.0 but the old API still runs, so the next update finishes the job."""
        monkeypatch.delenv("SIBYL_IMAGE_TAG", raising=False)
        runtime = _runtime(tmp_path, tag="1.5.0")
        monkeypatch.setattr(update_module, "installed_container_runtimes", lambda: [runtime])
        monkeypatch.setattr(ContainerRuntime, "is_running", lambda _self: True)
        monkeypatch.setattr(ContainerRuntime, "running_api_tag", lambda _self: "1.4.0")

        [plan] = plan_container_upgrades("1.5.0")

        assert (plan.current_tag, plan.pinned_tag, plan.applies) == ("1.4.0", "1.5.0", True)
        assert "pin says 1.5.0" in plan.status()

    @pytest.mark.parametrize(
        ("pinned", "target", "applies"),
        [
            # Interrupted after Compose stopped the old API: bring it back on the target.
            ("1.5.0", "1.5.0", True),
            ("1.4.0", "1.5.0", True),
            # A pin newer than this CLI must not be moved backwards.
            ("1.6.0", "1.5.0", False),
        ],
    )
    def test_a_runtime_up_without_its_api_is_not_current(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pinned: str,
        target: str,
        *,
        applies: bool,
    ) -> None:
        monkeypatch.delenv("SIBYL_IMAGE_TAG", raising=False)
        runtime = _runtime(tmp_path, tag=pinned)
        monkeypatch.setattr(update_module, "installed_container_runtimes", lambda: [runtime])
        monkeypatch.setattr(ContainerRuntime, "is_running", lambda _self: True)
        monkeypatch.setattr(ContainerRuntime, "running_api_tag", lambda _self: None)

        [plan] = plan_container_upgrades(target)

        assert plan.current_tag is None
        assert plan.api_missing is True
        assert plan.applies is applies
        assert f"no API container running (pin says {pinned})" in plan.status()
        assert "matches" not in plan.status()

    def test_the_pin_speaks_only_when_no_api_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime = _runtime(tmp_path, tag="1.4.0")
        monkeypatch.setattr(update_module, "installed_container_runtimes", lambda: [runtime])
        monkeypatch.setattr(ContainerRuntime, "is_running", lambda _self: False)
        probed: list[str] = []
        monkeypatch.setattr(
            ContainerRuntime, "running_api_tag", lambda _self: probed.append("x") or "9.9.9"
        )

        [plan] = plan_container_upgrades("1.5.0")

        assert plan.current_tag == "1.4.0"
        assert probed == []

    def test_stopped_and_unknown_runtimes_say_what_happens_next(self, tmp_path: Path) -> None:
        local = _plan(tmp_path, "1.4.0", "1.5.0", running=False, name="local")
        docker = _plan(tmp_path, "1.4.0", "1.5.0", running=False, name="docker")
        wedged = _plan(tmp_path, "1.4.0", "1.5.0", running=None, name="docker")

        assert "the next `sibyl up` runs 1.5.0" in local.status()
        assert "`sibyl docker upgrade --tag 1.5.0`" in docker.status()
        assert "state unknown" in wedged.status()
        assert "stopped" not in wedged.status()

    @pytest.mark.parametrize(
        ("current", "target", "source", "label"),
        [
            ("1.5.0", "1.5.0", "CLI", "matches CLI"),
            ("1.6.0", "1.5.0", "CLI", "newer than CLI 1.5.0"),
            ("1.6.0", "1.5.0", "SIBYL_IMAGE_TAG", "newer than SIBYL_IMAGE_TAG 1.5.0"),
            ("main", "1.5.0", "CLI", "custom tag"),
            ("1.4.0", "edge", "SIBYL_IMAGE_TAG", "SIBYL_IMAGE_TAG edge is not a release"),
        ],
    )
    def test_runtimes_left_alone_say_why(
        self, tmp_path: Path, current: str, target: str, source: str, label: str
    ) -> None:
        assert label in _plan(tmp_path, current, target, source=source).status()


class TestUpdateFunctions:
    """Tests for update execution functions."""

    def test_update_cli_success(self) -> None:
        """update_cli returns True on successful upgrade."""
        from sibyl_cli.update import update_cli

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("sibyl_cli.update.subprocess.run", return_value=mock_result),
            patch("sibyl_cli.update.get_current_cli_version", return_value="0.2.0"),
            patch(
                "sibyl_cli.update.sync_skills_after_cli_update", return_value=True
            ) as mock_sync_skills,
        ):
            assert update_cli() is True
            mock_sync_skills.assert_called_once_with()

    def test_update_cli_failure(self) -> None:
        """update_cli returns False on failed upgrade."""
        from sibyl_cli.update import update_cli

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error message"

        with patch("sibyl_cli.update.subprocess.run", return_value=mock_result):
            assert update_cli() is False

    @pytest.mark.parametrize("name", ["docker", "local"])
    def test_each_runtime_upgrades_through_its_own_command(self, tmp_path: Path, name: str) -> None:
        calls: list[list[str]] = []
        envs: list[dict[str, str]] = []
        plan = _plan(tmp_path, "1.4.0", "1.5.0", name=name)
        _PINS.append(plan.runtime.compose_file)

        with patch(
            "sibyl_cli.update.subprocess.run",
            side_effect=_upgrader(calls, envs, running="1.4.0"),
        ):
            assert upgrade_container_runtime(plan) is True

        assert calls == [["sibyl", name, "upgrade", "--tag", "1.5.0"]]
        # The PATH `sibyl` may be another build, so the tag is passed, not derived.
        assert envs[0]["SIBYL_IMAGE_TAG"] == "1.5.0"

    def test_success_is_only_claimed_once_the_pin_moved(self, tmp_path: Path) -> None:
        """A PATH `sibyl` that exits 0 on its own version must not read as success."""
        calls: list[list[str]] = []
        plan = _plan(tmp_path, "1.4.0", "1.5.0", name="local")
        _PINS.append(plan.runtime.compose_file)

        with patch("sibyl_cli.update.subprocess.run", side_effect=_upgrader(calls, move_pin=False)):
            assert upgrade_container_runtime(plan) is False

        assert plan.runtime.image_tag() == "1.4.0"

    @pytest.mark.parametrize(
        ("move_pin", "expected"),
        [
            # `docker upgrade` pulls first, so an unchanged pin means the pull failed.
            (False, ["Nothing changed; the docker runtime still pins 1.4.0"]),
            # A moved pin means the images arrived and the start failed.
            (True, ["pulled and pinned", "sibyl docker up", "sibyl docker logs"]),
        ],
    )
    def test_a_docker_failure_reads_the_pin_it_left(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        *,
        move_pin: bool,
        expected: list[str],
    ) -> None:
        calls: list[list[str]] = []
        plan = _plan(tmp_path, "1.4.0", "1.5.0", name="docker")
        _PINS.append(plan.runtime.compose_file)

        with patch(
            "sibyl_cli.update.subprocess.run",
            side_effect=_upgrader(calls, returncode=1, move_pin=move_pin, running="1.4.0"),
        ):
            assert upgrade_container_runtime(plan) is False

        output = capsys.readouterr().out
        for fragment in expected:
            assert fragment in output
        # No downgrade advice: after a start failure the data may be on the new version.
        assert "upgrade --tag 1.4.0" not in output

    def test_a_local_failure_leaves_the_explanation_to_local_upgrade(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls: list[list[str]] = []
        plan = _plan(tmp_path, "1.4.0", "1.5.0", name="local")

        with patch(
            "sibyl_cli.update.subprocess.run",
            side_effect=_upgrader(calls, returncode=1, move_pin=False),
        ):
            assert upgrade_container_runtime(plan) is False

        output = capsys.readouterr().out
        assert "sibyl local upgrade --tag 1.5.0 failed" in output
        assert "Nothing changed" not in output

    def test_success_needs_the_running_api_on_the_target(self, tmp_path: Path) -> None:
        """A moved pin with the old API still running is not an upgrade."""
        calls: list[list[str]] = []
        plan = _plan(tmp_path, "1.4.0", "1.5.0", name="docker")
        _PINS.append(plan.runtime.compose_file)

        with patch(
            "sibyl_cli.update.subprocess.run",
            side_effect=_upgrader(calls, running="1.4.0", starts=False),
        ):
            assert upgrade_container_runtime(plan) is False

        with patch(
            "sibyl_cli.update.subprocess.run",
            side_effect=_upgrader(calls, running="1.4.0"),
        ):
            assert upgrade_container_runtime(plan) is True

    def test_an_api_that_exits_after_the_start_is_not_success(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`docker upgrade` does not wait for health, so a crashed API must still fail."""
        calls: list[list[str]] = []
        plan = _plan(tmp_path, "1.4.0", "1.5.0", name="docker")
        _PINS.append(plan.runtime.compose_file)

        with patch("sibyl_cli.update.subprocess.run", side_effect=_upgrader(calls, running=None)):
            assert upgrade_container_runtime(plan) is False

        output = capsys.readouterr().out
        assert "no docker API container is running 1.5.0" in output
        assert "sibyl docker logs" in output

    def test_update_skills_delegates_to_setup(self) -> None:
        """update_skills calls setup_agent_integration."""
        from sibyl_cli.update import update_skills

        with patch("sibyl_cli.setup.setup_agent_integration", return_value=True) as mock:
            assert update_skills() is True
            mock.assert_called_once_with(verbose=False)


class TestUpdateCommand:
    """`sibyl update` wires the plans to the runtime commands."""

    @pytest.fixture
    def runtime_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[list[list[str]], ContainerRuntime]:
        monkeypatch.delenv("SIBYL_IMAGE_TAG", raising=False)
        runtime = _runtime(tmp_path, "docker", "1.4.0")
        _PINS.append(runtime.compose_file)
        calls: list[list[str]] = []

        monkeypatch.setattr(update_module, "is_dev_mode", lambda: False)
        monkeypatch.setattr(update_module, "installed_container_runtimes", lambda: [runtime])
        monkeypatch.setattr(update_module.subprocess, "run", _upgrader(calls, running="1.4.0"))
        return calls, runtime

    def test_containers_follow_the_installed_cli(
        self,
        runtime_calls: tuple[list[list[str]], ContainerRuntime],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls, runtime = runtime_calls
        monkeypatch.setattr(ContainerRuntime, "is_running", lambda _self: True)
        monkeypatch.setattr(update_module, "get_current_cli_version", lambda: "1.5.0")

        result = CliRunner().invoke(update_module.app, ["--containers", "--yes"])

        assert result.exit_code == 0, result.output
        assert calls == [["sibyl", "docker", "upgrade", "--tag", "1.5.0"]]
        assert runtime.image_tag() == "1.5.0"

    def test_a_stopped_runtime_is_left_alone(
        self,
        runtime_calls: tuple[list[list[str]], ContainerRuntime],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls, _ = runtime_calls
        monkeypatch.setattr(ContainerRuntime, "is_running", lambda _self: False)
        monkeypatch.setattr(update_module, "get_current_cli_version", lambda: "1.5.0")

        result = CliRunner().invoke(update_module.app, ["--containers", "--yes"])

        assert result.exit_code == 0, result.output
        assert calls == []
        assert "Everything is up to date" not in result.output

    def test_an_unreadable_tag_is_not_up_to_date(
        self,
        runtime_calls: tuple[list[list[str]], ContainerRuntime],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls, runtime = runtime_calls
        runtime.compose_file.write_text("services: {}\n")
        monkeypatch.setattr(ContainerRuntime, "is_running", lambda _self: True)
        monkeypatch.setattr(ContainerRuntime, "running_api_tag", lambda _self: None)
        monkeypatch.setattr(update_module, "get_current_cli_version", lambda: "1.5.0")

        result = CliRunner().invoke(update_module.app, ["--containers", "--check"])

        assert result.exit_code == 0, result.output
        assert "Unknown image tag" in result.output
        assert "Everything is up to date" not in result.output
        assert calls == []

    @pytest.mark.parametrize(
        ("landed", "expected", "note"),
        [
            ("1.5.0", [["sibyl", "docker", "upgrade", "--tag", "1.5.0"]], None),
            # A pinned tool install exits 0 without moving; the server must
            # not be moved ahead of it.
            ("1.4.0", [], "containers follow 1.4.0"),
            (None, [], "Could not read the CLI version"),
        ],
    )
    def test_containers_follow_the_cli_version_that_landed(
        self,
        runtime_calls: tuple[list[list[str]], ContainerRuntime],
        monkeypatch: pytest.MonkeyPatch,
        landed: str | None,
        expected: list[list[str]],
        note: str | None,
    ) -> None:
        calls, _ = runtime_calls
        monkeypatch.setattr(ContainerRuntime, "is_running", lambda _self: True)
        monkeypatch.setattr(update_module, "cli_update_available", lambda: ("1.3.0", "1.5.0", True))
        monkeypatch.setattr(update_module, "get_server_version", lambda: None)
        monkeypatch.setattr(update_module, "update_cli", lambda: True)
        monkeypatch.setattr(update_module, "get_current_cli_version", lambda: landed)
        monkeypatch.setattr(update_module, "update_skills", lambda: True)

        result = CliRunner().invoke(update_module.app, ["--yes"])

        assert result.exit_code == 0, result.output
        assert calls == expected
        if note:
            assert note in result.output
        assert "follow None" not in result.output

    def test_containers_wait_for_a_cli_upgrade_that_failed(
        self,
        runtime_calls: tuple[list[list[str]], ContainerRuntime],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls, _ = runtime_calls
        monkeypatch.setattr(ContainerRuntime, "is_running", lambda _self: True)
        monkeypatch.setattr(update_module, "cli_update_available", lambda: ("1.4.0", "1.5.0", True))
        monkeypatch.setattr(update_module, "get_server_version", lambda: None)
        monkeypatch.setattr(update_module, "update_cli", lambda: False)
        monkeypatch.setattr(update_module, "update_skills", lambda: True)

        result = CliRunner().invoke(update_module.app, ["--yes"])

        assert result.exit_code == 1
        assert calls == []
        assert "Skipped the container upgrade" in result.output
