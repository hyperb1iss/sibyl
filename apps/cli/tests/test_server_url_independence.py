"""The client CLI starts whatever server URL the environment carries.

`sibyl` imports sibyl-core, whose config loads at import. A SurrealDB URL
that only the server would open (a `surreal start` argument, an in-memory
store in production, a file store without the single-writer opt-in) must not
stop `sibyl --help` or `sibyl version`, or every agent hook on that machine
fails with it. The server's own Settings refuse those URLs at startup.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SECRET = "sk-live-cli-0123456789abcdefsecret"


def _run_sibyl(
    home: Path, env_overrides: dict[str, str], *args: str
) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SIBYL_OPENAI_API_KEY": _SECRET,
        **env_overrides,
    }
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    return subprocess.run(
        [sys.executable, "-c", "from sibyl_cli.entrypoint import main; main()", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    "env_overrides",
    [
        {"SIBYL_SURREAL_URL": "rocksdb:///data/sibyl.db"},
        {"SIBYL_SURREAL_URL": "tikv://pd:2379"},
        {"SIBYL_ENVIRONMENT": "production"},
        {"SIBYL_ENVIRONMENT": "production", "SIBYL_SURREAL_URL": "mem://"},
        {"SIBYL_ENVIRONMENT": "production", "SIBYL_SURREAL_URL": "file:///data/sibyl"},
        {"SIBYL_ENVIRONMENT": "production", "SIBYL_SURREAL_DATA_DIR": "/data/sibyl"},
        {"SIBYL_SURREAL_URL": "ws://surreal:8000/rpc", "SIBYL_SURREAL_DATA_DIR": "/data/sibyl"},
    ],
    ids=["rocksdb", "tikv", "prod-default", "prod-mem", "prod-file", "prod-data-dir", "both"],
)
@pytest.mark.parametrize("args", [("--help",), ("version",)], ids=["help", "version"])
def test_cli_starts_with_a_server_url_it_does_not_open(
    tmp_path: Path, env_overrides: dict[str, str], args: tuple[str, ...]
) -> None:
    result = _run_sibyl(tmp_path, env_overrides, *args)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ValidationError" not in result.stderr
    assert _SECRET not in result.stdout + result.stderr
