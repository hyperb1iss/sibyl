#!/usr/bin/env python3
"""Configure Claude Code settings for Sibyl hooks.

This script:
1. Backs up existing settings to settings.json.bak
2. Preserves all non-Sibyl hooks
3. Adds/updates only Sibyl-specific hooks
"""

import json
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


MANAGED_HOOK_SCRIPTS = {"session-start.py", "user-prompt-submit.py"}


def is_managed_hook(hook: dict) -> bool:
    """True only for a command hook that runs one of Sibyl's scripts from HOOKS_DIR's layout.

    Mirrors sibyl_cli.setup.is_managed_hook: a user's hook that merely mentions
    sibyl, such as /opt/sibyl-policy/check, is never ours to remove.
    """
    if not isinstance(hook, dict) or hook.get("type", "command") != "command":
        return False
    try:
        words = shlex.split(str(hook.get("command", "")))
    except ValueError:
        return False
    return any(
        Path(word).name in MANAGED_HOOK_SCRIPTS
        and Path(word).parent.parts[-3:] == (".claude", "hooks", "sibyl")
        for word in words
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
