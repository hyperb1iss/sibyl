#!/usr/bin/env python3
"""Sibyl PostToolUse Hook - Capture file changes.

Logs file modifications to Sibyl so changes are tracked
in the knowledge graph automatically.
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
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})

        # Only track file modifications
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            sys.exit(0)

        file_path = tool_input.get("file_path", "")
        if not file_path:
            sys.exit(0)

        # Get relative path for cleaner logging
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
        try:
            rel_path = os.path.relpath(file_path, project_dir)
        except ValueError:
            rel_path = file_path

        # Skip common noise
        skip_patterns = [
            "node_modules/",
            ".git/",
            "__pycache__/",
            ".pyc",
            "package-lock.json",
            "pnpm-lock.yaml",
            ".next/",
            "dist/",
            "build/",
        ]
        if any(p in file_path for p in skip_patterns):
            sys.exit(0)

        # Determine action type
        action = "Created" if tool_name == "Write" else "Modified"

        # Build content with metadata
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        content = f"""Tool: {tool_name}
Path: {rel_path}
Time: {timestamp}
Session: Claude Code"""

        # Log to Sibyl (async, non-blocking)
        run_sibyl_async(
            "add",
            f"File {action}: {rel_path}",
            content,
            "-c",
            "file-change",
        )

        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
