"""Execute one command from an inherited, already validated directory fd."""

from __future__ import annotations

import os
import subprocess
import sys

MINIMUM_ARGUMENT_COUNT = 4


def main() -> int:
    if len(sys.argv) < MINIMUM_ARGUMENT_COUNT or sys.argv[2] != "--":
        raise SystemExit("usage: release_fd_exec.py <directory-fd> -- <command> [args...]")
    try:
        directory_fd = int(sys.argv[1])
    except ValueError as exc:
        raise SystemExit("directory fd must be an integer") from exc
    command = sys.argv[3:]
    os.fchdir(directory_fd)
    return subprocess.run(command, check=False).returncode  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
