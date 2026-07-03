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


def get_session_bundle() -> dict | None:
    """Get the packaged session bundle."""
    output = run_sibyl("session", "bundle", "--json", timeout=8)
    if not output:
        return None
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def main():
    try:
        health = run_sibyl("health", timeout=3)
        if not health:
            print("Sibyl server unreachable — skip sibyl commands this session.")
            sys.exit(0)

        lines = []
        bundle = get_session_bundle()
        if bundle:
            context = bundle.get("context", {})
            project = context.get("project_name") or context.get("project_id")
            if project:
                lines.append(f"Project: {project}")

            tasks = bundle.get("tasks", [])
            for task in tasks[:3]:
                status = task.get("status", "")
                name = task.get("name", "")[:60]
                tid = task.get("id", "")
                lines.append(f"[{status}] {name} ({tid})")

            memories = bundle.get("relevant_entities", [])
            for entity in memories[:2]:
                name = entity.get("name", "")[:60]
                eid = entity.get("id", "")
                lines.append(f"→ {name} ({eid})")

            remember_next = bundle.get("remember_next")
            if remember_next:
                lines.append(f"Remember: {remember_next}")
        else:
            tasks = get_active_tasks()
            if tasks:
                for t in tasks[:3]:
                    status = t.get("metadata", {}).get("status", "")
                    name = t.get("name", "")[:40]
                    tid = t.get("id", "")
                    lines.append(f"[{status}] {name} ({tid})")

        if not lines:
            lines.append("Session bundle is empty right now.")

        if not bundle or not bundle.get("remember_next"):
            lines.append("Suggest 'sibyl remember' when solving something non-obvious.")
        lines.append("Cite material memory with 'sibyl cite <ids>' or --cited on reflect/complete.")
        print("\n".join(lines))
        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
