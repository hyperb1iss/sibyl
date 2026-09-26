#!/usr/bin/env python3
"""Configure Claude Code settings for Sibyl hooks.

This script:
1. Backs up existing settings to settings.json.bak
2. Preserves all non-Sibyl hooks
3. Adds/updates only Sibyl-specific hooks
"""

import json
import re
import shlex
import shutil
from datetime import datetime
from pathlib import Path

SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
HOOKS_DIR = Path.home() / ".claude" / "hooks" / "sibyl"

SIBYL_HOOKS = {
    "SessionStart": [
        {
            "matcher": "startup",
            "hooks": [
                {
                    "type": "command",
                    "command": f"python3 {HOOKS_DIR}/session-start.py",
                    "timeout": 10,
                }
            ],
        },
        {
            "matcher": "resume",
            "hooks": [
                {
                    "type": "command",
                    "command": f"python3 {HOOKS_DIR}/session-start.py",
                    "timeout": 10,
                }
            ],
        },
    ],
}


# Scripts Sibyl has installed into ~/.claude/hooks/sibyl/, including retired
# ones so a re-run can prune them.
MANAGED_HOOK_SCRIPTS = frozenset({"session-start.py", "user-prompt-submit.py", "post-tool-use.py", "stop.py"})
_INTERPRETER = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|sh|bash|zsh)$")


def _executed_script(words: list[str]) -> str | None:
    """The program a hook command runs, looking through `env`, `uv run` and interpreters."""
    rest = list(words)
    if rest and Path(rest[0]).name == "env":
        rest = rest[1:]
    if len(rest) >= 2 and Path(rest[0]).name == "uv" and rest[1] == "run":
        rest = [word for word in rest[2:] if not word.startswith("-")]
    if rest and _INTERPRETER.match(Path(rest[0]).name):
        rest = [word for word in rest[1:] if not word.startswith("-")]
    return rest[0] if rest else None


def is_managed_hook(hook: object) -> bool:
    """True only for a hook object Sibyl installed.

    That is a command hook that executes one of Sibyl's scripts from a
    `.claude/hooks/sibyl/` directory, directly or through an interpreter, which
    is the shape the installer writes (`python3 <dir>/session-start.py`). A hook
    that only passes such a path as an argument, like `sha256sum <path>`, or
    that merely mentions sibyl, is never Sibyl's to remove.
    """
    if not isinstance(hook, dict) or hook.get("type", "command") != "command":
        return False
    try:
        script = _executed_script(shlex.split(str(hook.get("command", ""))))
    except ValueError:
        return False
    if script is None:
        return False
    path = Path(script)
    return path.name in MANAGED_HOOK_SCRIPTS and path.parent.parts[-3:] == (
        ".claude",
        "hooks",
        "sibyl",
    )


def remove_managed_hooks(hooks: dict) -> dict:
    """Drop only Sibyl's hook objects; a group keeps its other hooks and position."""
    cleaned = {}
    for event, entries in hooks.items():
        kept = []
        for entry in entries:
            inner = entry.get("hooks", [])
            remaining = [hook for hook in inner if not is_managed_hook(hook)]
            if len(remaining) == len(inner):
                kept.append(entry)
            elif remaining:
                kept.append({**entry, "hooks": remaining})
        cleaned[event] = kept
    return cleaned


def main():
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings
    existing_hooks_count = 0
    try:
        if SETTINGS_FILE.exists():
            settings = json.loads(SETTINGS_FILE.read_text())
            existing_hooks = settings.get("hooks", {})
            existing_hooks_count = sum(len(v) for v in existing_hooks.values())
        else:
            settings = {}
    except json.JSONDecodeError:
        settings = {}

    # Backup if there are existing hooks
    if existing_hooks_count > 0:
        backup = SETTINGS_FILE.with_suffix(f".json.{datetime.now():%Y%m%d-%H%M%S}.bak")
        shutil.copy2(SETTINGS_FILE, backup)
        print(f"  Backed up existing settings to {backup.name}")

    # Remove Sibyl's own hooks, leaving every other hook where it was.
    hooks = remove_managed_hooks(settings.get("hooks", {}))
    preserved_count = sum(len(entries) for entries in hooks.values())

    if preserved_count > 0:
        print(f"  Preserved {preserved_count} existing non-Sibyl hooks")

    # Add sibyl hooks
    for event, event_hooks in SIBYL_HOOKS.items():
        if event not in hooks:
            hooks[event] = []
        hooks[event].extend(event_hooks)

    settings["hooks"] = hooks
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    print(f"  Added Sibyl hooks for: {', '.join(SIBYL_HOOKS.keys())}")


if __name__ == "__main__":
    main()
