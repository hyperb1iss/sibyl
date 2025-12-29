#!/usr/bin/env python3
"""Configure Claude Code settings for Sibyl hooks.

This script:
1. Backs up existing settings to settings.json.bak
2. Preserves all non-Sibyl hooks
3. Adds/updates only Sibyl-specific hooks
"""

import json
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
    "UserPromptSubmit": [
        {
            "matcher": ".*",
            "hooks": [
                {
                    "type": "command",
                    "command": f"python3 {HOOKS_DIR}/user-prompt-submit.py",
                    "timeout": 5,
                }
            ],
        }
    ],
    "PostToolUse": [
        {
            "matcher": "Write|Edit|MultiEdit",
            "hooks": [
                {
                    "type": "command",
                    "command": f"python3 {HOOKS_DIR}/post-tool-use.py",
                    "timeout": 5,
                }
            ],
        }
    ],
    "Stop": [
        {
            "matcher": ".*",
            "hooks": [{"type": "command", "command": f"python3 {HOOKS_DIR}/stop.py", "timeout": 5}],
        }
    ],
}


def is_sibyl_hook(hook_entry: dict) -> bool:
    """Check if a hook entry is a Sibyl hook."""
    for hook in hook_entry.get("hooks", []):
        cmd = str(hook.get("command", ""))
        if "sibyl" in cmd or "hooks/sibyl" in cmd:
            return True
    return False


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

    # Get existing hooks, remove old sibyl ones but preserve others
    hooks = settings.get("hooks", {})
    preserved_count = 0

    for event in list(hooks.keys()):
        hooks[event] = [h for h in hooks[event] if not is_sibyl_hook(h)]
        preserved_count += len(hooks[event])

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
