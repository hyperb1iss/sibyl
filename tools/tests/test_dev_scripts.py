from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from shutil import which

import pytest
from tools.tests.conftest import REPO_ROOT

EXPECTED_NATIVE_SAMPLE_LINES = 3
EXPECTED_TOOLCHAIN_VERSIONS = {
    "proto": "0.60.2",
    "moon": "2.5.2",
    "node": "24.19.0",
    "pnpm": "11.22.0",
    "python": "3.13.15",
    "uv": "0.12.5",
}
PROTO_INSTALLER_SHA256 = {
    "powershell": "91de41e2ba3ac62d26d9c6197001e44acedf451a6281908696b9ed2c76b9bcc8",
    "shell": "eda7887a3192337b87f62c08328781f08f39f55796efd885ec3472976b4e9adf",
}


def _write_stale_toolchain_stubs(proto_home: Path, state_dir: Path) -> None:
    bin_dir = proto_home / "bin"
    bin_dir.mkdir(parents=True)
    state_dir.mkdir()
    stub = """#!/usr/bin/env bash
set -euo pipefail
tool="$(basename "$0")"
if [[ "$tool" == "proto" && "${1:-}" == "install" ]]; then
  [[ "${4:-}" == "--pin" && "${5:-}" == "global" ]]
  printf 'install %s %s --pin global\\n' "$2" "$3" >> "$TOOL_INSTALL_LOG"
  printf '%s\\n' "$3" > "$TOOL_STATE_DIR/$2"
  exit 0
fi
if [[ "$tool" == "proto" && "${1:-}" == "upgrade" ]]; then
  printf 'upgrade %s\\n' "$2" >> "$TOOL_INSTALL_LOG"
  printf '%s\\n' "$2" > "$TOOL_STATE_DIR/proto"
  exit 0
fi
version="$(<"$TOOL_STATE_DIR/$tool")"
case "$tool" in
  node) printf 'v%s\\n' "$version" ;;
  python) printf 'Python %s\\n' "$version" ;;
  *) printf '%s %s\\n' "$tool" "$version" ;;
esac
"""
    for tool in EXPECTED_TOOLCHAIN_VERSIONS:
        binary = bin_dir / tool
        binary.write_text(stub, encoding="utf-8")
        binary.chmod(0o755)
        (state_dir / tool).write_text("0.1.0\n", encoding="utf-8")


@pytest.mark.skipif(which("bash") is None, reason="bash is required for setup-dev.sh")
def test_shell_setup_reconciles_hostile_stale_tools_to_exact_repo_pins(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "setup-dev.sh").write_bytes((REPO_ROOT / "setup-dev.sh").read_bytes())
    (repo_dir / ".prototools").write_bytes((REPO_ROOT / ".prototools").read_bytes())

    proto_home = tmp_path / "proto-home"
    state_dir = tmp_path / "state"
    install_log = tmp_path / "installs.log"
    _write_stale_toolchain_stubs(proto_home, state_dir)

    bash = which("bash")
    assert bash is not None
    script = f"""
source {shlex.quote(str(repo_dir / "setup-dev.sh"))}
install_proto
install_moon
install_toolchain
"""
    result = subprocess.run(  # noqa: S603
        [bash, "-c", script],
        cwd=repo_dir,
        env={
            **os.environ,
            "PATH": f"{proto_home / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "PROTO_HOME": str(proto_home),
            "TOOL_INSTALL_LOG": str(install_log),
            "TOOL_STATE_DIR": str(state_dir),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert {
        tool: (state_dir / tool).read_text(encoding="utf-8").strip()
        for tool in EXPECTED_TOOLCHAIN_VERSIONS
    } == EXPECTED_TOOLCHAIN_VERSIONS
    assert install_log.read_text(encoding="utf-8").splitlines() == [
        "upgrade 0.60.2",
        "install moon 2.5.2 --pin global",
        "install node 24.19.0 --pin global",
        "install pnpm 11.22.0 --pin global",
        "install python 3.13.15 --pin global",
        "install uv 0.12.5 --pin global",
    ]


@pytest.mark.skipif(which("bash") is None, reason="bash is required for setup-dev.sh")
def test_shell_setup_reinstalls_tool_when_version_command_fails(tmp_path: Path) -> None:
    state = tmp_path / "installed"
    install_log = tmp_path / "installs.log"
    bash = which("bash")
    assert bash is not None
    script = f"""
source {shlex.quote(str(REPO_ROOT / "setup-dev.sh"))}
badtool() {{
  printf 'badtool 2.5.2\\n'
  [[ -f {shlex.quote(str(state))} ]]
}}
proto() {{
  printf '%s\\n' "$*" > {shlex.quote(str(install_log))}
  touch {shlex.quote(str(state))}
}}
install_exact_tool badtool 2.5.2
"""
    result = subprocess.run(  # noqa: S603
        [bash, "-c", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert install_log.read_text(encoding="utf-8").strip() == ("install badtool 2.5.2 --pin global")


@pytest.mark.skipif(which("bash") is None, reason="bash is required for setup-dev.sh")
@pytest.mark.parametrize(
    ("tool", "function_name"),
    [("proto", "install_proto"), ("moon", "install_moon"), ("uv", "install_toolchain")],
)
def test_shell_setup_rejects_malformed_pin_before_install(
    tmp_path: Path,
    tool: str,
    function_name: str,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "setup-dev.sh").write_bytes((REPO_ROOT / "setup-dev.sh").read_bytes())
    prototools = (REPO_ROOT / ".prototools").read_text(encoding="utf-8")
    prototools = prototools.replace(
        f'{tool} = "{EXPECTED_TOOLCHAIN_VERSIONS[tool]}"',
        f'{tool} = "latest"',
    )
    (repo_dir / ".prototools").write_text(prototools, encoding="utf-8")

    proto_home = tmp_path / "proto-home"
    state_dir = tmp_path / "state"
    install_log = tmp_path / "installs.log"
    _write_stale_toolchain_stubs(proto_home, state_dir)
    for name, version in EXPECTED_TOOLCHAIN_VERSIONS.items():
        (state_dir / name).write_text(f"{version}\n", encoding="utf-8")

    bash = which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603
        [
            bash,
            "-c",
            f"source {shlex.quote(str(repo_dir / 'setup-dev.sh'))}; {function_name}",
        ],
        cwd=repo_dir,
        env={
            **os.environ,
            "PATH": f"{proto_home / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "PROTO_HOME": str(proto_home),
            "TOOL_INSTALL_LOG": str(install_log),
            "TOOL_STATE_DIR": str(state_dir),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert f"Missing exact {tool} version" in result.stderr
    assert not install_log.exists()


@pytest.mark.skipif(
    which("bash") is None or not (which("sha256sum") or which("shasum")),
    reason="bash and a SHA-256 tool are required for setup-dev.sh",
)
def test_shell_proto_bootstrap_rejects_tampered_installer_and_cleans_up(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "setup-dev.sh").write_bytes((REPO_ROOT / "setup-dev.sh").read_bytes())
    (repo_dir / ".prototools").write_bytes((REPO_ROOT / ".prototools").read_bytes())

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
output=''
while (($#)); do
  if [[ "$1" == "-o" ]]; then
    output="$2"
    shift 2
  else
    shift
  fi
done
printf '#!/usr/bin/env bash\\ntouch "$MALICIOUS_MARKER"\\n' > "$output"
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    proto_home = tmp_path / "proto-home"
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    marker = tmp_path / "executed"
    bash = which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603
        [bash, "-c", f"source {shlex.quote(str(repo_dir / 'setup-dev.sh'))}; install_proto"],
        cwd=repo_dir,
        env={
            **os.environ,
            "MALICIOUS_MARKER": str(marker),
            "PATH": f"{fake_bin}{os.pathsep}{os.defpath}",
            "PROTO_HOME": str(proto_home),
            "TMPDIR": str(temp_dir),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "proto installer checksum mismatch" in result.stderr
    assert not marker.exists()
    assert list(temp_dir.iterdir()) == []


def _write_powershell_toolchain_stubs(proto_home: Path, state_dir: Path) -> None:
    bin_dir = proto_home / "bin"
    shims_dir = proto_home / "shims"
    bin_dir.mkdir(parents=True)
    shims_dir.mkdir()
    state_dir.mkdir()

    if os.name == "nt":
        stub = """@echo off
set tool=%~n0
if "%tool%"=="proto" if "%1"=="install" (
  echo install %2 %3 --pin global>>"%TOOL_INSTALL_LOG%"
  echo %3>"%TOOL_STATE_DIR%\\%2"
  if exist "%TOOL_STATE_DIR%\\%2.fail" del "%TOOL_STATE_DIR%\\%2.fail"
  exit /b 0
)
set /p version=<"%TOOL_STATE_DIR%\\%tool%"
if "%tool%"=="node" (echo v%version%) else if "%tool%"=="python" (echo Python %version%) else (echo %tool% %version%)
if exist "%TOOL_STATE_DIR%\\%tool%.fail" exit /b 42
"""
        suffix = ".cmd"
    else:
        stub = """#!/usr/bin/env bash
set -euo pipefail
tool="$(basename "$0")"
if [[ "$tool" == "proto" && "${1:-}" == "install" ]]; then
  printf 'install %s %s --pin global\\n' "$2" "$3" >> "$TOOL_INSTALL_LOG"
  printf '%s\\n' "$3" > "$TOOL_STATE_DIR/$2"
  rm -f "$TOOL_STATE_DIR/$2.fail"
  exit 0
fi
version="$(<"$TOOL_STATE_DIR/$tool")"
case "$tool" in
  node) printf 'v%s\\n' "$version" ;;
  python) printf 'Python %s\\n' "$version" ;;
  *) printf '%s %s\\n' "$tool" "$version" ;;
esac
if [[ -f "$TOOL_STATE_DIR/$tool.fail" ]]; then
  exit 42
fi
"""
        suffix = ""

    proto = bin_dir / f"proto{suffix}"
    proto.write_text(stub, encoding="utf-8")
    proto.chmod(0o755)
    (state_dir / "proto").write_text("0.60.2\n", encoding="utf-8")
    for tool in ("moon", "node", "pnpm", "python", "uv"):
        binary = shims_dir / f"{tool}{suffix}"
        binary.write_text(stub, encoding="utf-8")
        binary.chmod(0o755)
        (state_dir / tool).write_text("0.1.0\n", encoding="utf-8")
    (state_dir / "node").write_text(
        f"{EXPECTED_TOOLCHAIN_VERSIONS['node']}\n",
        encoding="utf-8",
    )
    (state_dir / "node.fail").touch()


@pytest.mark.skipif(which("pwsh") is None, reason="pwsh is required for setup-dev.ps1")
def test_powershell_setup_reconciles_exact_tools_and_executable_sources(
    tmp_path: Path,
) -> None:
    proto_home = tmp_path / "proto-home"
    state_dir = tmp_path / "state"
    install_log = tmp_path / "installs.log"
    _write_powershell_toolchain_stubs(proto_home, state_dir)

    pwsh = which("pwsh")
    assert pwsh is not None
    harness = tmp_path / "powershell-toolchain-contract.ps1"
    harness.write_text(
        """
param($SetupScript, $ProtoHome, $InstallLog, $StateDir)
. $SetupScript -SkipMain
$env:PROTO_HOME = $ProtoHome
$env:TOOL_INSTALL_LOG = $InstallLog
$env:TOOL_STATE_DIR = $StateDir
$separator = [IO.Path]::PathSeparator
$env:Path = @(
    (Join-Path $ProtoHome 'bin'),
    (Join-Path $ProtoHome 'shims'),
    (Join-Path $ProtoHome 'bin'),
    $env:Path
) -join $separator
function global:node { 'v99.99.99' }
Install-Moon
Install-Toolchain
$nodeCommand = Get-ApplicationCommand -Name 'node'
Write-Output "NODE_SOURCE=$($nodeCommand.Source)"
Write-Output "FINAL_PATH=$env:Path"
""".lstrip(),
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            "-SetupScript",
            str(REPO_ROOT / "setup-dev.ps1"),
            "-ProtoHome",
            str(proto_home),
            "-InstallLog",
            str(install_log),
            "-StateDir",
            str(state_dir),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert {
        tool: (state_dir / tool).read_text(encoding="utf-8").strip()
        for tool in EXPECTED_TOOLCHAIN_VERSIONS
    } == EXPECTED_TOOLCHAIN_VERSIONS
    assert install_log.read_text(encoding="utf-8").splitlines() == [
        "install moon 2.5.2 --pin global",
        "install node 24.19.0 --pin global",
        "install pnpm 11.22.0 --pin global",
        "install python 3.13.15 --pin global",
        "install uv 0.12.5 --pin global",
    ]

    source_line = next(line for line in result.stdout.splitlines() if "NODE_SOURCE=" in line)
    path_line = next(line for line in result.stdout.splitlines() if "FINAL_PATH=" in line)
    node_source = Path(source_line.split("=", 1)[1])
    path_parts = path_line.split("=", 1)[1].split(os.pathsep)
    assert node_source.parent.resolve() == (proto_home / "shims").resolve()
    assert [Path(part).resolve() for part in path_parts[:2]] == [
        (proto_home / "shims").resolve(),
        (proto_home / "bin").resolve(),
    ]
    assert sum(Path(part).resolve() == (proto_home / "shims").resolve() for part in path_parts) == 1
    assert sum(Path(part).resolve() == (proto_home / "bin").resolve() for part in path_parts) == 1


def test_proto_bootstrap_uses_verified_pinned_release_installers() -> None:
    shell = (REPO_ROOT / "setup-dev.sh").read_text(encoding="utf-8")
    powershell = (REPO_ROOT / "setup-dev.ps1").read_text(encoding="utf-8")
    expected_version = EXPECTED_TOOLCHAIN_VERSIONS["proto"]

    shell_version = re.search(r'PROTO_INSTALLER_VERSION="([^"]+)"', shell)
    powershell_version = re.search(r"PROTO_INSTALLER_VERSION = '([^']+)'", powershell)
    assert shell_version
    assert powershell_version
    assert shell_version.group(1) == expected_version
    assert powershell_version.group(1) == expected_version

    assert PROTO_INSTALLER_SHA256["shell"] in shell
    assert PROTO_INSTALLER_SHA256["powershell"] in powershell
    assert "releases/download/v${PROTO_INSTALLER_VERSION}/proto_cli-installer.sh" in shell
    assert "releases/download/v${PROTO_INSTALLER_VERSION}/proto_cli-installer.ps1" in powershell
    assert '"$expected" != "$PROTO_INSTALLER_VERSION"' in shell
    assert "$expected -ne $PROTO_INSTALLER_VERSION" in powershell
    assert "moonrepo.dev/install" not in shell
    assert "moonrepo.dev/install" not in powershell


def test_devcontainer_has_one_exact_node_and_pnpm_owner() -> None:
    config = json.loads(
        (REPO_ROOT / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
    )
    dockerfile = (REPO_ROOT / ".devcontainer" / "Dockerfile").read_text(encoding="utf-8")

    features = config.get("features", {})
    assert isinstance(features, dict)
    assert not any(key.startswith("ghcr.io/devcontainers/features/node") for key in features)
    assert 'ENV PATH="/opt/proto/shims:/opt/proto/bin:${PATH}"' in dockerfile
    assert "> /etc/profile.d/proto.sh" in dockerfile
    assert "proto install node 24.19.0 --pin global" in dockerfile
    assert "proto install pnpm 11.22.0 --pin global" in dockerfile


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


def test_api_readiness_has_no_default_deadline_while_process_is_alive() -> None:
    bash = which("bash")
    assert bash is not None

    script = """
source tools/dev/run-surreal-dev.sh
unset SIBYL_DEV_API_READY_TIMEOUT
SIBYL_SERVER_HOST=127.0.0.1
SIBYL_SERVER_PORT=3334
attempts=0
process_tree_alive() { return 0; }
curl() {
  attempts=$((attempts + 1))
  if ((attempts >= 35)); then
    return 0
  fi
  return 1
}
sleep() { SECONDS=$((SECONDS + 1)); }
SECONDS=0
wait_for_api_ready 123
printf 'attempts=%s\\n' "$attempts"
"""

    result = subprocess.run(  # noqa: S603
        [bash, "-c", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "attempts=35\n"


def test_api_readiness_honors_explicit_deadline() -> None:
    bash = which("bash")
    assert bash is not None

    script = """
source tools/dev/run-surreal-dev.sh
SIBYL_DEV_API_READY_TIMEOUT=2
SIBYL_SERVER_HOST=127.0.0.1
SIBYL_SERVER_PORT=3334
process_tree_alive() { return 0; }
curl() { return 1; }
sleep() { SECONDS=$((SECONDS + 1)); }
SECONDS=0
wait_for_api_ready 123
"""

    result = subprocess.run(  # noqa: S603
        [bash, "-c", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert (
        result.stderr == "Timed out waiting for API readiness at http://127.0.0.1:3334/api/health\n"
    )


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
    (output_dir / "runtime-kind.txt").write_text("container\n", encoding="utf-8")
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


def test_surreal_runtime_monitor_samples_and_gates_native_process(tmp_path: Path) -> None:
    bash = which("bash")
    sleep = which("sleep")
    assert bash is not None
    assert sleep is not None
    output_dir = tmp_path / "telemetry"
    target = subprocess.Popen([sleep, "30"])  # noqa: S603
    monitor = subprocess.Popen(  # noqa: S603
        [
            bash,
            "tools/dev/surreal-runtime-monitor.sh",
            "monitor",
            "--pid",
            str(target.pid),
            "--output-dir",
            str(output_dir),
            "--interval",
            "1",
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        samples = output_dir / "samples.tsv"
        while time.monotonic() < deadline:
            if (
                samples.exists()
                and len(samples.read_text(encoding="utf-8").splitlines())
                >= EXPECTED_NATIVE_SAMPLE_LINES
            ):
                break
            time.sleep(0.05)
        else:
            monitor.terminate()
            _stdout, stderr = monitor.communicate(timeout=5)
            raise AssertionError(f"native monitor did not produce two samples: {stderr}")

        monitor.terminate()
        _stdout, stderr = monitor.communicate(timeout=5)
        assert monitor.returncode == 0, stderr
        gate = subprocess.run(  # noqa: S603
            [
                bash,
                "tools/dev/surreal-runtime-monitor.sh",
                "gate",
                "--pid",
                str(target.pid),
                "--output-dir",
                str(output_dir),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        assert gate.returncode == 0, gate.stderr
        assert "runtime_kind=process" in gate.stdout
        assert "valid_samples=2" in gate.stdout
        assert "result=pass" in gate.stdout
    finally:
        if monitor.poll() is None:
            monitor.terminate()
            monitor.communicate(timeout=5)
        target.terminate()
        target.wait(timeout=5)
