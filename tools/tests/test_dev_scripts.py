from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from shutil import which

from tools.tests.conftest import REPO_ROOT


def _write_docker_stub(bin_dir: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "compose" && "${2:-}" == "ps" ]]; then
  printf '{"Service":"postgres"}\\n'
  exit 0
fi
if [[ "${1:-}" == "volume" && "${2:-}" == "ls" ]]; then
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)


def _write_podman_docker_stub(bin_dir: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--version" ]]; then
  printf 'Emulate Docker CLI using podman. Create /etc/containers/nodocker to quiet msg.\\n'
  printf 'podman version 5.8.2\\n'
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    for name in ("podman", "podman-compose", "docker-compose"):
        binary = bin_dir / name
        binary.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)


def _run_detector(
    tmp_path: Path,
    *,
    migrated: bool,
    explicit_data_dir: bool = True,
    rocksdb: bool = False,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_docker_stub(bin_dir)

    data_dir = (
        tmp_path / "surreal-dev" if explicit_data_dir else tmp_path / ".moon/cache/surreal-dev"
    )
    data_dir.mkdir(parents=True)
    if migrated:
        (data_dir / ".sibyl-migrated").write_text(
            "archive=/tmp/sibyl-migrate.tar.gz\nmigrated_at=2026-05-04T00:00:00Z\n",
            encoding="utf-8",
        )
    if rocksdb:
        rocksdb_dir = data_dir / "sibyl.db"
        rocksdb_dir.mkdir()
        (rocksdb_dir / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SIBYL_STORE": "surreal",
    }
    if explicit_data_dir:
        env["SURREAL_DATA_DIR"] = str(data_dir)
    else:
        env.pop("SURREAL_DATA_DIR", None)

    detector = "source tools/dev/run-surreal-dev.sh; "
    if not explicit_data_dir:
        detector += f"repo_root={shlex.quote(str(tmp_path))}; "
    detector += "warn_if_legacy_setup_detected"

    bash = which("bash")
    assert bash is not None
    return subprocess.run(  # noqa: S603
        [
            bash,
            "-c",
            detector,
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_legacy_guard_allows_migrated_surreal_runtime(tmp_path: Path) -> None:
    result = _run_detector(tmp_path, migrated=True)

    assert result.returncode == 0
    assert "Local legacy data detected" not in result.stdout


def test_legacy_guard_allows_migrated_default_surreal_runtime(tmp_path: Path) -> None:
    result = _run_detector(tmp_path, migrated=True, explicit_data_dir=False)

    assert result.returncode == 0
    assert "Local legacy data detected" not in result.stdout


def test_legacy_guard_allows_existing_default_surreal_runtime(tmp_path: Path) -> None:
    result = _run_detector(tmp_path, migrated=False, explicit_data_dir=False, rocksdb=True)

    assert result.returncode == 0
    assert "Local legacy data detected" not in result.stdout


def test_legacy_guard_warns_when_legacy_exists_without_surreal_marker(tmp_path: Path) -> None:
    result = _run_detector(tmp_path, migrated=False, explicit_data_dir=False)

    assert result.returncode == 1
    assert "Local legacy data detected" in result.stdout
    assert "sibyld migrate import <archive>" in result.stdout
    assert "--source-type legacy-archive" in result.stdout
    assert "--target-mode surreal" in result.stdout
    assert "moon run dev-legacy" not in result.stdout


def test_compose_command_prefers_quiet_docker_compose_provider(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_podman_docker_stub(bin_dir)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    bash = which("bash")
    assert bash is not None

    result = subprocess.run(  # noqa: S603
        [bash, "-c", "source tools/dev/run-surreal-dev.sh; compose_command"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "env",
        "PODMAN_COMPOSE_WARNING_LOGS=false",
        f"PODMAN_COMPOSE_PROVIDER={bin_dir / 'docker-compose'}",
        "podman",
        "compose",
    ]


def test_dev_main_allows_empty_extra_commands_with_nounset() -> None:
    env = {
        **os.environ,
        "SIBYL_STORE": "surreal",
        "SIBYL_AUTH_STORE": "surreal",
        "SIBYL_COORDINATION_BACKEND": "local",
        "SIBYL_SURREAL_URL": "ws://127.0.0.1:8000/rpc",
        "SIBYL_DEV_API_COMMAND": "true",
        "SIBYL_DEV_WEB_COMMAND": "true",
        "SIBYL_DEV_SKIP_LEGACY_CHECK": "1",
    }
    bash = which("bash")
    assert bash is not None

    script = """
source tools/dev/run-surreal-dev.sh
sleep() { :; }
launch_command() { child_pids+=("99999"); }
wait_for_api_ready() { return 0; }
wait_for_commands() { child_pids=(); return 0; }
cleanup() { exit "${1:-0}"; }
main
"""

    result = subprocess.run(  # noqa: S603
        [bash, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "extra_commands[@]: unbound variable" not in result.stderr
