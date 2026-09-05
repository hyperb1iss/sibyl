"""Exercise the curl-pipe installer without installing tools or starting services."""

from __future__ import annotations

import os
import pty
import subprocess
from pathlib import Path

import pytest
from tools.tests.conftest import REPO_ROOT

STUB_SHELL = r"""
uv() {
    printf 'uv %s\n' "$*" >> "$INSTALLER_CALL_LOG"
    if [ "$1" = --version ]; then printf 'uv fixture\n'; fi
}
sibyl() {
    printf 'sibyl %s\n' "$*" >> "$INSTALLER_CALL_LOG"
    if [ "$1" = start ]; then
        if [ "${INSTALLER_FAIL_START:-0}" = 1 ]; then return 7; fi
        INSTALLER_STARTED=1
    fi
}
sibyld() { printf 'sibyld %s\n' "$*" >> "$INSTALLER_CALL_LOG"; }
docker() {
    printf 'docker %s\n' "$*" >> "$INSTALLER_CALL_LOG"
    if [ "$1" = inspect ]; then printf '%s\n' "${INSTALLER_EXISTING_SERVER:-false}"; fi
    return "${INSTALLER_DOCKER_STATUS:-0}"
}
curl() {
    printf 'curl %s\n' "$*" >> "$INSTALLER_CALL_LOG"
    case "$*" in
        '-sS --noproxy * --max-time 5 http://localhost:3334/api/health/ready')
            return "${INSTALLER_OCCUPIED_PORT_STATUS:-7}" ;;
        '-fsS --noproxy * --max-time 5 http://localhost:3334/api/health/ready')
            INSTALLER_PROBED=1
            INSTALLER_PROBE_COUNT=$(( ${INSTALLER_PROBE_COUNT:-0} + 1 ))
            if [ "$INSTALLER_PROBE_COUNT" -le "${INSTALLER_HEALTH_AFTER:-0}" ]; then return 7; fi
            return "${INSTALLER_HEALTH_STATUS:-0}" ;;
        *) printf 'Unexpected download\n' >&2; exit 99 ;;
    esac
}
cat() {
    if [ "$*" = "$HOME/.sibyl/run/sibyld.pid" ]; then
        if [ "${INSTALLER_EXISTING_DAEMON:-0}" = 1 ] || [ "${INSTALLER_STARTED:-0}" = 1 ]; then
            printf '%s\n' "${INSTALLER_PID:-12345}"
        else
            return 1
        fi
    else
        command cat "$@"
    fi
}
kill() {
    [ "$1" = -0 ] || exit 99
    if [ "${INSTALLER_DAEMON_DIES:-0}" = 1 ]; then return 1; fi
    if [ "${INSTALLER_DIES_AFTER_PROBE:-0}:${INSTALLER_PROBED:-0}" = 1:1 ]; then return 1; fi
}
ps() { printf '%s\n' "${INSTALLER_PROCESS_COMMAND:-/fixture/bin/sibyld serve --embedded --host 127.0.0.1 --port 3334 --transport http}"; }
sleep() { INSTALLER_ELAPSED=$(( ${INSTALLER_ELAPSED:-0} + $1 )); }
date() { printf '%s\n' "${INSTALLER_ELAPSED:-0}"; }
uname() { printf 'Linux\n'; }
if [ "${INSTALLER_MISSING_CURL:-0}" = 1 ]; then
    unset -f curl
    PATH=/nonexistent
fi
installer_path=$1
shift
. "$installer_path"
"""


def _run_installer(
    tmp_path: Path,
    *args: str,
    env: dict[str, str] | None = None,
    terminal: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    call_log = tmp_path / "calls.log"
    process_env = {
        "PATH": "/usr/bin:/bin",
        "TERM": "xterm-256color",
        "INSTALLER_CALL_LOG": str(call_log),
        **(env or {}),
    }
    # Tool functions shadow real executables even after the installer adds to PATH.
    # Reuse the actual home path without touching it; every mutation is stubbed.
    process_env["HOME"] = str(Path.home())
    command = ["/bin/sh", "-c", STUB_SHELL, "installer-test", str(REPO_ROOT / "install.sh"), *args]
    if terminal:
        master, slave = pty.openpty()
        try:
            with (
                (tmp_path / "stderr.txt").open("w+") as stderr,
                subprocess.Popen(  # noqa: S603
                    command, env=process_env, stdout=slave, stderr=stderr, text=True
                ) as process,
            ):
                os.close(slave)
                slave = -1
                output = bytearray()
                while True:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                process.wait()
                stderr.seek(0)
                result = subprocess.CompletedProcess(
                    command, process.returncode, output.decode(), stderr.read()
                )
        finally:
            os.close(master)
            if slave >= 0:
                os.close(slave)
    else:
        result = subprocess.run(  # noqa: S603
            command, env=process_env, capture_output=True, text=True, check=False
        )
    return result, call_log.read_text().splitlines() if call_log.exists() else []


def test_default_installer_checks_docker_before_installing_and_starts_server(
    tmp_path: Path,
) -> None:
    result, calls = _run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls == [
        "docker info",
        "uv --version",
        "uv tool install sibyl-dev@latest --force",
        "sibyl skill install --quiet",
        "docker inspect --format {{.State.Running}} sibyl-api",
        "sibyl up --pull",
        "curl -fsS --noproxy * --max-time 5 http://localhost:3334/api/health/ready",
    ]
    assert "\x1b" not in result.stdout
    assert "SIBYL" in result.stdout


@pytest.mark.parametrize(
    ("args", "env", "last_call"),
    [
        (["--remote"], {}, "sibyl skill install --quiet"),
        (["--no-start"], {}, "sibyl skill install --quiet"),
        (["--no-open"], {}, "sibyl up --pull --no-browser"),
        (["--no-open", "--no-pull"], {}, "sibyl up --no-browser"),
        (["--daemon"], {}, "sibyl start"),
        ([], {"SIBYL_INSTALL_MODE": "remote"}, "sibyl skill install --quiet"),
    ],
)
def test_installer_preserves_modes_and_start_flags(
    tmp_path: Path, args: list[str], env: dict[str, str], last_call: str
) -> None:
    result, calls = _run_installer(tmp_path, *args, env=env)
    assert result.returncode == 0, result.stderr
    assert last_call in calls
    if "--remote" in args or "--no-start" in args or env:
        assert "docker info" not in calls
    if "--daemon" in args:
        assert "docker info" not in calls
        assert "uv tool install sibyld@latest --force" in calls
        assert "sibyl init --local --force" in calls


def test_installer_preserves_pinned_prerelease_version(tmp_path: Path) -> None:
    result, calls = _run_installer(tmp_path, "--daemon", "--no-start", "--version", "1.4.0-rc.1")
    assert result.returncode == 0, result.stderr
    assert "uv tool install sibyl-dev==1.4.0rc1 --force" in calls
    assert "uv tool install sibyld==1.4.0rc1 --force" in calls


@pytest.mark.parametrize(
    "env",
    [{"SIBYL_INSTALL_MODE": "bogus"}, {"SIBYL_INSTALL_START": "yes"}],
)
def test_invalid_configuration_fails_before_installing(tmp_path: Path, env: dict[str, str]) -> None:
    result, calls = _run_installer(tmp_path, env=env)
    assert result.returncode != 0
    assert calls == []
    assert "Installation complete" not in result.stdout


def test_unavailable_docker_fails_before_installing(tmp_path: Path) -> None:
    result, calls = _run_installer(tmp_path, env={"INSTALLER_DOCKER_STATUS": "1"})
    assert result.returncode != 0
    assert calls == ["docker info"]
    assert "Docker daemon is not running" in result.stderr


def test_daemon_failure_never_reports_completion(tmp_path: Path) -> None:
    result, calls = _run_installer(tmp_path, "--daemon", env={"INSTALLER_FAIL_START": "1"})
    assert result.returncode != 0
    assert calls[-1] == "sibyl start"
    assert "Embedded daemon did not start" in result.stderr
    assert "Installation complete" not in result.stdout
    assert "Embedded daemon: http" not in result.stdout


def test_installer_checks_health_even_when_older_cli_reports_success(tmp_path: Path) -> None:
    result, calls = _run_installer(tmp_path, env={"INSTALLER_HEALTH_STATUS": "22"})
    assert result.returncode != 0
    assert "sibyl up --pull" in calls
    assert calls[-1] == "curl -fsS --noproxy * --max-time 5 http://localhost:3334/api/health/ready"
    assert "local API is not ready" in result.stderr
    assert "Installation complete" not in result.stdout


@pytest.mark.parametrize("env", [{"NO_COLOR": "1"}, {"TERM": "dumb"}])
def test_terminal_can_disable_ansi(tmp_path: Path, env: dict[str, str]) -> None:
    result, _ = _run_installer(tmp_path, "--remote", env=env, terminal=True)
    assert result.returncode == 0, result.stderr
    assert "\x1b" not in result.stdout


def test_terminal_shows_silkcircuit_gradient_wordmark(tmp_path: Path) -> None:
    result, _ = _run_installer(tmp_path, "--remote", terminal=True)
    assert result.returncode == 0, result.stderr
    assert "╔═╗" in result.stdout
    assert "\x1b[38;2;225;53;255m" in result.stdout
    assert "\x1b[38;2;128;255;234m" in result.stdout


def test_installer_help_has_no_side_effects(tmp_path: Path) -> None:
    result, calls = _run_installer(tmp_path, "--help")
    assert result.returncode == 0
    assert calls == []
    assert "--version" in result.stdout
    assert "\x1b" not in result.stdout


@pytest.mark.parametrize("args", [[], ["--daemon"]])
def test_missing_curl_fails_before_any_install(tmp_path: Path, args: list[str]) -> None:
    result, calls = _run_installer(tmp_path, *args, env={"INSTALLER_MISSING_CURL": "1"})
    assert result.returncode != 0
    assert calls == []
    assert "curl is required" in result.stderr


def test_server_rerun_explains_explicit_image_upgrade(tmp_path: Path) -> None:
    result, _ = _run_installer(tmp_path, env={"INSTALLER_EXISTING_SERVER": "true"})
    assert result.returncode == 0
    assert "running server images will remain unchanged" in result.stdout
    assert "sibyl down && sibyl up --pull" in result.stdout


def test_healthy_daemon_rerun_preserves_process_and_context(tmp_path: Path) -> None:
    result, calls = _run_installer(tmp_path, "--daemon", env={"INSTALLER_EXISTING_DAEMON": "1"})
    assert result.returncode == 0, result.stderr
    assert "sibyl start" not in calls
    assert "sibyl init --local --force" not in calls
    assert "Existing daemon preserved" in result.stdout
    assert "sibyl stop && sibyl start" in result.stdout


@pytest.mark.parametrize(
    "env",
    [
        {"INSTALLER_DAEMON_DIES": "1"},
        {"INSTALLER_DIES_AFTER_PROBE": "1"},
        {"INSTALLER_HEALTH_STATUS": "22"},
    ],
)
def test_daemon_zero_exit_requires_owned_process_and_readiness(
    tmp_path: Path, env: dict[str, str]
) -> None:
    result, calls = _run_installer(tmp_path, "--daemon", env=env)
    assert result.returncode != 0
    assert "sibyl start" in calls
    assert "did not become ready" in result.stderr
    assert "Installation complete" not in result.stdout


def test_daemon_does_not_borrow_readiness_from_other_api(tmp_path: Path) -> None:
    result, calls = _run_installer(
        tmp_path, "--daemon", env={"INSTALLER_OCCUPIED_PORT_STATUS": "0"}
    )
    assert result.returncode != 0
    assert "sibyl start" not in calls
    assert "Port 3334 already serves an API" in result.stderr


def test_daemon_does_not_trust_reused_pid(tmp_path: Path) -> None:
    result, _ = _run_installer(
        tmp_path, "--daemon", env={"INSTALLER_PROCESS_COMMAND": "unrelated-service"}
    )
    assert result.returncode != 0
    assert "Installation complete" not in result.stdout


@pytest.mark.parametrize("args", [[], ["--daemon"]])
def test_slow_startup_waits_for_dependency_readiness(tmp_path: Path, args: list[str]) -> None:
    unready_probes = 30
    result, calls = _run_installer(
        tmp_path, *args, env={"INSTALLER_HEALTH_AFTER": str(unready_probes)}
    )
    assert result.returncode == 0, result.stderr
    assert "Installation complete" in result.stdout
    probes = [call for call in calls if call.startswith("curl -fsS --noproxy")]
    assert len(probes) == unready_probes + 1
