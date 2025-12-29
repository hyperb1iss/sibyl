#!/usr/bin/env python3
"""Sibyl Stop Hook - Log session summary.

When Claude stops, logs a brief session marker to Sibyl
for session history tracking.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone


def run_sibyl_async(*args: str) -> None:
    """Fire and forget sibyl command."""
    try:
        subprocess.Popen(
            ["sibyl", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.environ.get("CLAUDE_PROJECT_DIR", "."),
        )
    except Exception:
        pass


def main():
    try:
        data = json.load(sys.stdin)

        # Don't re-trigger if already in a stop hook
        if data.get("stop_hook_active"):
            sys.exit(0)

        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
        project_name = os.path.basename(project_dir)
        session_id = data.get("session_id", "unknown")[:12]
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Log session end (async)
        content = f"""Project: {project_name}
Session: {session_id}
Ended: {timestamp}"""

        run_sibyl_async(
            "add",
            f"Session ended: {project_name}",
            content,
            "-c",
            "session",
        )

        # Exit 0 = allow stop (don't block)
        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
