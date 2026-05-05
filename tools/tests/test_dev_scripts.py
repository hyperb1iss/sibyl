from __future__ import annotations

import os
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
  printf '{"Service":"falkordb"}\\n'
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


def _run_detector(tmp_path: Path, *, migrated: bool) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_docker_stub(bin_dir)

    data_dir = tmp_path / "surreal-dev"
    data_dir.mkdir()
    if migrated:
        (data_dir / ".sibyl-migrated").write_text(
            "archive=/tmp/sibyl-migrate.tar.gz\nmigrated_at=2026-05-04T00:00:00Z\n",
            encoding="utf-8",
        )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SIBYL_STORE": "surreal",
        "SURREAL_DATA_DIR": str(data_dir),
    }
    bash = which("bash")
    assert bash is not None
    return subprocess.run(  # noqa: S603
        [
            bash,
            "-lc",
            "source tools/dev/run-surreal-dev.sh; warn_if_legacy_setup_detected",
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


def test_legacy_guard_warns_when_legacy_exists_without_surreal_marker(tmp_path: Path) -> None:
    result = _run_detector(tmp_path, migrated=False)

    assert result.returncode == 1
    assert "Local legacy data detected" in result.stdout
    assert "moon run dev -- --migrate-legacy" in result.stdout
