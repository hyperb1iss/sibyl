#!/usr/bin/env python3
"""Sibyl SessionStart Hook - Load context at session start.

Injects active tasks and relevant patterns into Claude's context
so every session starts with awareness of ongoing work.
"""

import json
import os
import subprocess
import sys

CYAN = "\033[38;2;128;255;234m"
PURPLE = "\033[38;2;225;53;255m"
CORAL = "\033[38;2;255;106;193m"
DIM = "\033[2m"
RESET = "\033[0m"


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
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    return None


def get_active_tasks() -> list[dict]:
    """Get tasks currently in progress."""
    output = run_sibyl("task", "list", "--status", "doing,blocked", "--limit", "5")
    if output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass
    return []


def get_recent_patterns(project_name: str) -> list[dict]:
    """Get patterns relevant to current project."""
    output = run_sibyl("search", project_name, "--type", "pattern", "--limit", "3")
    if output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass
    return []


def format_task(task: dict) -> str:
    """Format a task for display."""
    status_icons = {
        "doing": f"{CYAN}▶{RESET}",
        "blocked": f"{CORAL}⏸{RESET}",
        "todo": "○",
    }
    status = task.get("status", "todo")
    icon = status_icons.get(status, "○")
    name = task.get("name", "Untitled")
    task_id = task.get("id", "")[:12]
    return f"  {icon} {name} {DIM}({task_id}){RESET}"


def main():
    try:
        # Check if sibyl is available
        health = run_sibyl("health", timeout=3)
        if not health:
            sys.exit(0)  # Sibyl not available, exit silently

        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
        project_name = os.path.basename(project_dir)

        # Gather context
        tasks = get_active_tasks()
        patterns = get_recent_patterns(project_name)

        # Build output
        lines = [
            f"{PURPLE}▸ Sibyl{RESET} context loaded",
            "",
        ]

        if tasks:
            lines.append(f"{CYAN}Active Tasks:{RESET}")
            for task in tasks[:5]:
                lines.append(format_task(task))
            lines.append("")

        if patterns:
            lines.append(f"{CYAN}Relevant Patterns:{RESET}")
            for p in patterns[:3]:
                name = p.get("name", p.get("title", "Pattern"))
                lines.append(f"  • {name}")
            lines.append("")

        if not tasks and not patterns:
            lines.append(f"{DIM}No active tasks or patterns found.{RESET}")
            lines.append(f"{DIM}Use /sibyl-knowledge to search and add knowledge.{RESET}")

        print("\n".join(lines))
        sys.exit(0)

    except Exception:
        # Never block on errors
        sys.exit(0)


if __name__ == "__main__":
    main()
