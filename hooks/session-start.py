#!/usr/bin/env python3
"""Sibyl SessionStart Hook - Load context at session start."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

# The hook is registered with a 10 second timeout, and a hook that overruns it
# loses everything it would have printed. Every call takes only what is left
# of this budget, counted from when the process started, so a slow server
# shortens the context instead of erasing it.
HOOK_BUDGET_SECONDS = 7.0
STARTED = time.monotonic()
# Below this, a call is not worth starting.
MIN_CALL_SECONDS = 0.5
INBOX_COMMAND = (
    "sibyl",
    *("net", "read", "@me", "--new", "--mark", "session-start", "--json", "--limit", "100"),
)


def remaining() -> float:
    """Seconds left of the hook's budget."""
    return HOOK_BUDGET_SECONDS - (time.monotonic() - STARTED)


def run_sibyl(*args: str, timeout: float = 5) -> str | None:
    """Run sibyl command and return stdout, or None on failure or when the budget is spent."""
    allowed = min(timeout, remaining())
    if allowed < MIN_CALL_SECONDS:
        return None
    try:
        result = subprocess.run(
            ["sibyl", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=allowed,
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


def start_inbox_check() -> subprocess.Popen[str] | None:
    """Start reading what reached the caller's inbox since this hook last looked.

    The hook keeps its own place (``--mark session-start``), apart from watch
    cursors and from an agent's own ``net read --new``.
    """
    try:
        return subprocess.Popen(
            INBOX_COMMAND,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=os.environ.get("CLAUDE_PROJECT_DIR", "."),
        )
    except Exception:
        return None


def _since(created_at: object) -> str:
    try:
        then = datetime.fromisoformat(str(created_at))
    except ValueError:
        return ""
    # Hooks run under the system python3, which can predate datetime.UTC.
    utc = timezone.utc  # noqa: UP017
    if then.tzinfo is None:
        then = then.replace(tzinfo=utc)
    seconds = max((datetime.now(utc) - then).total_seconds(), 0)
    if seconds < 3600:
        return f" in the last {max(int(seconds // 60), 1)}m"
    if seconds < 86400:
        return f" since {int(seconds // 3600)}h ago"
    if seconds < 2 * 86400:
        return " since yesterday"
    return f" since {int(seconds // 86400)} days ago"


def inbox_line(check: subprocess.Popen[str] | None) -> str | None:
    """One line about new inbox messages, or None when there are none or anything failed."""
    if check is None:
        return None
    try:
        output, _ = check.communicate(timeout=max(remaining(), 0.1))
        if check.returncode != 0:
            return None
        data = json.loads(output)
    except Exception:
        check.kill()
        return None
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list) or not messages:
        return None
    inbox = messages[-1].get("channel") or "@me"
    count = f"{len(messages)}+" if data.get("more") else str(len(messages))
    parts = [f"📬 {count} new in {inbox}{_since(messages[0].get('created_at'))}"]
    parts.append("sibyl net read @me")
    if web_url := data.get("web_url"):
        parts.append(str(web_url))
    return " · ".join(parts)


def main():
    try:
        health = run_sibyl("health", timeout=3)
        if not health:
            print("Sibyl server unreachable — skip sibyl commands this session.")
            sys.exit(0)

        lines = []
        inbox_check = start_inbox_check()
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
        if inbox := inbox_line(inbox_check):
            lines.insert(0, inbox)
        print("\n".join(lines))
        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
