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
if [[ "${1:-}" == "compose" ]]; then
  shift
  if [[ "${1:-}" == "--env-file" ]]; then
    shift 2
  fi
  if [[ "${1:-}" == "ps" ]]; then
    printf '{"Service":"postgres"}\\n'
    exit 0
  fi
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

    env: dict[str, str] = {
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


def test_stop_dev_disables_default_compose_env_file(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_args = tmp_path / "docker-args.txt"
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$DOCKER_ARGS_LOG"
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "DOCKER_ARGS_LOG": str(docker_args),
    }
    bash = which("bash")
    assert bash is not None

    result = subprocess.run(  # noqa: S603
        [bash, "tools/dev/stop-dev.sh"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert docker_args.read_text(encoding="utf-8").splitlines() == [
        "compose",
        "--env-file",
        "/dev/null",
        "down",
    ]


def test_launch_command_uses_separate_process_group() -> None:
    bash = which("bash")
    assert bash is not None

    script = """
source tools/dev/run-surreal-dev.sh
launch_command "sleep 30"
pid="${child_pids[0]}"
pgid="$(process_pgid "$pid")"
printf 'pid=%s pgid=%s\\n' "$pid" "$pgid"
if [[ "$pgid" != "$pid" ]]; then
  signal_process_tree KILL "$pid"
  exit 1
fi
signal_process_tree TERM "$pid"
sleep 0.2
wait "$pid" 2>/dev/null || true
if process_tree_alive "$pid"; then
  exit 1
fi
"""

    result = subprocess.run(  # noqa: S603
        [bash, "-c", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_signal_process_tree_signals_parent_before_descendants() -> None:
    bash = which("bash")
    assert bash is not None

    script = """
source tools/dev/process-tree.sh
collect_descendants() { printf '20\\n30\\n'; }
process_is_group_leader() { return 1; }
kill() { printf '%s\\n' "$*"; }
signal_process_tree TERM 10
"""

    result = subprocess.run(  # noqa: S603
        [bash, "-c", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["-TERM 10", "-TERM 20", "-TERM 30"]


def test_surreal_container_snapshot_has_valid_bash_syntax() -> None:
    bash = which("bash")
    assert bash is not None

    result = subprocess.run(  # noqa: S603
        [bash, "-n", "tools/dev/surreal-container-snapshot.sh"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_surreal_container_snapshot_uses_pid_namespace_toolbox(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  inspect)
    printf 'name=/sibyl-surrealdb pid=123 running=true oom=false restarting=false started=now image=surrealdb/surrealdb:v3.2.3\\n'
    ;;
  stats)
    printf 'name=sibyl-surrealdb cpu=101.00%% mem=2GiB / 8GiB net=0B / 0B block=0B / 0B pids=85\\n'
    ;;
  logs)
    printf 'surreal log line\\n'
    ;;
  run)
    printf '%s\\n' "$*"
    ;;
  *)
    exit 64
    ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    bash = which("bash")
    assert bash is not None

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    result = subprocess.run(  # noqa: S603
        [
            bash,
            "tools/dev/surreal-container-snapshot.sh",
            "--seconds",
            "1",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--pid=container:sibyl-surrealdb" in result.stdout
    assert "-e SAMPLE_SECONDS=1" in result.stdout


def _run_surreal_runtime_gate(
    tmp_path: Path,
    *,
    inspect_state: str,
    restart_counts: tuple[int, int],
    oom_counts: tuple[int, int] = (0, 0),
    oom_kill_counts: tuple[int, int] = (0, 0),
    event: str = "",
    malformed_sample: bool = False,
    failure_marker: str | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{1:-}}" == "inspect" ]]; then
  printf '%s\\n' '{inspect_state}'
  exit 0
fi
exit 64
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    output_dir = tmp_path / "telemetry"
    output_dir.mkdir()
    header = [
        "timestamp",
        "container_id",
        "status",
        "restart_count",
        "oom_killed",
        "exit_code",
        "pid",
        "started_at",
        "finished_at",
        "rss_kib",
        "hwm_kib",
        "anon_kib",
        "file_kib",
        "swap_kib",
        "threads",
        "cgroup_current_bytes",
        "cgroup_peak_bytes",
        "cgroup_swap_bytes",
        "cgroup_oom",
        "cgroup_oom_kill",
        "pressure_some_total",
        "pressure_full_total",
        "host_available_kib",
    ]
    rows = [
        [
            "2026-07-23T00:00:00Z",
            "container-id",
            "running",
            str(restart_counts[0]),
            "false",
            "0",
            "101",
            "start",
            "finish",
            "1024",
            "2048",
            "900",
            "124",
            "0",
            "20",
            "1048576",
            "2097152",
            "0",
            str(oom_counts[0]),
            str(oom_kill_counts[0]),
            "1",
            "0",
            "8000000",
        ],
        [
            "2026-07-23T00:00:05Z",
            "container-id",
            "running",
            str(restart_counts[1]),
            "false",
            "0",
            "101",
            "start",
            "finish",
            "1536",
            "2560",
            "1300",
            "236",
            "0",
            "22",
            "1572864",
            "2621440",
            "0",
            str(oom_counts[1]),
            str(oom_kill_counts[1]),
            "2",
            "0",
            "7900000",
        ],
    ]
    rows_to_write = (
        [header, rows[0], ["timestamp", "container-id", "missing"]]
        if malformed_sample
        else [header, *rows]
    )
    (output_dir / "samples.tsv").write_text(
        "\n".join("\t".join(row) for row in rows_to_write) + "\n",
        encoding="utf-8",
    )
    (output_dir / "docker-events.jsonl").write_text(event, encoding="utf-8")
    (output_dir / "docker-events.pid").write_text("999999\n", encoding="utf-8")
    (output_dir / "monitor-ready").touch()
    if failure_marker is not None:
        (output_dir / failure_marker).touch()

    bash = which("bash")
    assert bash is not None
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(  # noqa: S603
        [
            bash,
            "tools/dev/surreal-runtime-monitor.sh",
            "gate",
            "--container",
            "container-id",
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_surreal_runtime_gate_accepts_complete_clean_telemetry(tmp_path: Path) -> None:
    result = _run_surreal_runtime_gate(
        tmp_path,
        inspect_state="running|0|false|0",
        restart_counts=(0, 0),
    )

    assert result.returncode == 0, result.stderr
    assert "samples=2" in result.stdout
    assert "rss_peak_kib=1536" in result.stdout
    assert "cgroup_reported_peak_bytes=2621440" in result.stdout
    assert "result=pass" in result.stdout


def test_surreal_runtime_gate_rejects_restarts(tmp_path: Path) -> None:
    result = _run_surreal_runtime_gate(
        tmp_path,
        inspect_state="running|1|false|0",
        restart_counts=(0, 1),
        event='{"Action":"restart"}\n',
    )

    assert result.returncode == 1
    assert "SurrealDB restarted: count=1 delta=1" in result.stderr
    assert "runtime integrity loss" in result.stderr


def test_surreal_runtime_gate_rejects_oom_activity(tmp_path: Path) -> None:
    result = _run_surreal_runtime_gate(
        tmp_path,
        inspect_state="running|0|true|137",
        restart_counts=(0, 0),
        oom_counts=(0, 1),
        oom_kill_counts=(0, 1),
        event='{"Action":"oom"}\n',
    )

    assert result.returncode == 1
    assert "cgroup recorded OOM activity" in result.stderr
    assert "runtime integrity loss" in result.stderr


def test_surreal_runtime_gate_rejects_preexisting_oom_activity(tmp_path: Path) -> None:
    result = _run_surreal_runtime_gate(
        tmp_path,
        inspect_state="running|0|false|0",
        restart_counts=(0, 0),
        oom_counts=(1, 1),
        oom_kill_counts=(1, 1),
    )

    assert result.returncode == 1
    assert "cgroup recorded OOM activity" in result.stderr


def test_surreal_runtime_gate_rejects_malformed_samples(tmp_path: Path) -> None:
    result = _run_surreal_runtime_gate(
        tmp_path,
        inspect_state="running|0|false|0",
        restart_counts=(0, 0),
        malformed_sample=True,
    )

    assert result.returncode == 1
    assert "telemetry is incomplete" in result.stderr


def test_surreal_runtime_gate_rejects_collector_failure(tmp_path: Path) -> None:
    result = _run_surreal_runtime_gate(
        tmp_path,
        inspect_state="running|0|false|0",
        restart_counts=(0, 0),
        failure_marker="docker-events.failed",
    )

    assert result.returncode == 1
    assert "telemetry failure marker: docker-events.failed" in result.stderr


def test_surreal_runtime_monitor_rejects_collector_exit(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  inspect)
    if [[ "$*" == *"container={{.Id}}"* ]]; then
      printf '%s\\n' \
        'container=container-id image=surrealdb/surrealdb:v3.2.0 started_at=start'
    else
      printf '%s\\n' \
        'container-id|running|0|false|0|999999|start|finish'
    fi
    ;;
  events)
    exit 42
    ;;
  *)
    exit 64
    ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    output_dir = tmp_path / "telemetry"
    bash = which("bash")
    assert bash is not None

    result = subprocess.run(  # noqa: S603
        [
            bash,
            "tools/dev/surreal-runtime-monitor.sh",
            "monitor",
            "--container",
            "container-id",
            "--output-dir",
            str(output_dir),
            "--interval",
            "1",
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 1
    assert "docker events collector exited unexpectedly" in result.stderr
    assert (output_dir / "docker-events.failed").exists()
