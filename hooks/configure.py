#!/usr/bin/env python3
"""Configure Claude Code settings for Sibyl hooks.

This script:
1. Backs up existing settings to settings.json.bak
2. Preserves all non-Sibyl hooks
3. Adds/updates only Sibyl-specific hooks
"""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
HOOKS_DIR = Path.home() / ".claude" / "hooks" / "sibyl"

# ============================================================================
# Managed hook templates
# ============================================================================
# Every hook Sibyl has ever written into Claude settings, and nothing else. A
# hook is Sibyl's only when it equals one of these after light normalization;
# wrappers, flags, `bash -c` and commands that merely mention the path are the
# user's. Keep in step with hooks/configure.py (a test pins the two equal).

# Scripts installed into ~/.claude/hooks/sibyl/, including retired ones so a
# re-run can prune them.
MANAGED_HOOK_SCRIPTS = ("session-start.py", "user-prompt-submit.py", "post-tool-use.py", "stop.py")

# The prompt-type Stop hook hooks/configure.py wrote on 2025-12-30 (49cf9729a),
# retired the same day (7a10519af).
LEGACY_STOP_HOOK_PROMPT = (
    "You are evaluating whether Claude should stop or first capture learnings to Sibyl (a knowledge graph).\n"
    "\n"
    "Session context: $ARGUMENTS\n"
    "\n"
    "Analyze the conversation for UNCAPTURED valuable learnings:\n"
    "1. Non-obvious solutions or workarounds discovered\n"
    "2. Gotchas, edge cases, or debugging insights\n"
    "3. Architectural decisions with reasoning\n"
    "4. Integration patterns or configuration quirks\n"
    "\n"
    "IMPORTANT:\n"
    "- If stop_hook_active is true, ALWAYS return ok:true (prevents infinite loops)\n"
    "- Only block if there are SPECIFIC, CONCRETE learnings worth capturing\n"
    "- Skip trivial info, well-documented basics, or temporary hacks\n"
    "- Look for 'sibyl add' calls - if learnings were already captured, approve\n"
    "\n"
    "Respond with JSON:\n"
    '- To BLOCK (has uncaptured learnings): {"ok": false, "reason": "Before stopping, capture these learnings to Sibyl via \'sibyl add\':\\n\\n1. [Title]: [What, why, caveats]\\n2. ..."}\n'
    '- To APPROVE (no learnings or already captured): {"ok": true, "reason": "No uncaptured learnings"}'
)


def managed_hook_command(hooks_dir: Path, script: str) -> str:
    """The exact command the installer writes for `script`."""
    return f"python3 {hooks_dir}/{script}"


def _normalize_hook_text(text: str, home: str) -> str:
    """Trim and collapse spaces and tabs, expand ~ and $HOME, and tidy slashes.

    Only ASCII space and tab separate words here. Any other whitespace or
    control character, such as a no-break space or a carriage return, is part
    of a filename to the shell, so it is kept and the comparison fails.
    """
    text = " ".join(re.split(r"[ \t]+", text.strip(" \t")))
    text = text.replace("${HOME}", home).replace("$HOME", home)
    text = re.sub(r"(^| )~(?=/|$)", lambda match: match.group(1) + home, text)
    text = re.sub(r"/{2,}", "/", text)
    while "/./" in text:
        text = text.replace("/./", "/")
    return text


def is_managed_hook(hook: object, hooks_dir: Path | None = None) -> bool:
    """True only for a hook object equal to one Sibyl's installer wrote."""
    if not isinstance(hook, dict):
        return False
    directory = hooks_dir or HOOKS_DIR
    home = str(directory.parents[2])
    kind = hook.get("type", "command")
    if kind == "command":
        command = _normalize_hook_text(str(hook.get("command", "")), home)
        return command in {
            _normalize_hook_text(managed_hook_command(directory, script), home)
            for script in MANAGED_HOOK_SCRIPTS
        }
    if kind == "prompt":
        prompt = " ".join(str(hook.get("prompt", "")).split())
        return prompt == " ".join(LEGACY_STOP_HOOK_PROMPT.split())
    return False


SIBYL_HOOKS = {
    "SessionStart": [
        {
            "matcher": "startup",
            "hooks": [
                {
                    "type": "command",
                    "command": managed_hook_command(HOOKS_DIR, "session-start.py"),
                    "timeout": 10,
                }
            ],
        },
        {
            "matcher": "resume",
            "hooks": [
                {
                    "type": "command",
                    "command": managed_hook_command(HOOKS_DIR, "session-start.py"),
                    "timeout": 10,
                }
            ],
        },
    ],
}


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
