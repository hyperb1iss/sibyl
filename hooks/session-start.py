#!/usr/bin/env python3
"""Sibyl SessionStart Hook - Load context at session start."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def run_sibyl(*args: str, timeout: int = 5) -> str | None:
    """Run sibyl command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["sibyl", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.environ.get("CLAUDE_PROJECT_DIR", "."),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def get_active_tasks() -> list[dict]:
    """Get tasks currently in progress."""
    output = run_sibyl("task", "list", "--status", "doing,blocked", "--limit", "5", "-j")
    if output:
        try:
            data = json.loads(output)
            # CLI returns array directly or {"entities": [...]}
            if isinstance(data, list):
                return data
            return data.get("entities", [])
        except json.JSONDecodeError:
            pass
    return []


def main():
    try:
        health = run_sibyl("health", timeout=3)
        if not health:
            sys.exit(0)

        lines = []
        tasks = get_active_tasks()
        if tasks:
            for t in tasks[:3]:
                status = t.get("metadata", {}).get("status", "")
                name = t.get("name", "")[:40]
                tid = t.get("id", "")[:12]
                lines.append(f"[{status}] {name} ({tid})")

        lines.append("Suggest 'sibyl add' when solving something non-obvious.")
        print("\n".join(lines))
        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
