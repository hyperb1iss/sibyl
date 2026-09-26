"""Sibyl integration setup for Claude Code and Codex.

Installs the Sibyl skill and optional Claude hooks for assistant tooling:
  - Skills: ~/.claude/skills/sibyl/ and ~/.codex/skills/sibyl/
  - Hooks: ~/.claude/hooks/sibyl/ (Claude Code only)
"""

from __future__ import annotations

import json
import re
import shutil
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from rich.panel import Panel

from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    error,
    info,
    success,
    warn,
)
from sibyl_core.integration import AGENT_PROMPT_SNIPPET

# ============================================================================
# Paths
# ============================================================================

CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
CLAUDE_HOOKS_DIR = Path.home() / ".claude" / "hooks" / "sibyl"
CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

CODEX_SKILLS_DIR = Path.home() / ".codex" / "skills"

# Skills to install
SKILL_NAMES = ["sibyl"]

# ============================================================================
# Hook Configuration
# ============================================================================


def get_sibyl_hooks_config() -> dict:
    """Generate Sibyl hooks configuration for settings.json."""
    return {
        "SessionStart": [
            {
                "matcher": "startup",
                "hooks": [
                    {
                        "type": "command",
                        "command": managed_hook_command(CLAUDE_HOOKS_DIR, "session-start.py"),
                        "timeout": 10,
                    }
                ],
            },
            {
                "matcher": "resume",
                "hooks": [
                    {
                        "type": "command",
                        "command": managed_hook_command(CLAUDE_HOOKS_DIR, "session-start.py"),
                        "timeout": 10,
                    }
                ],
            },
        ],
    }


def valid_hooks_shape(hooks: object) -> bool:
    """True when `hooks` has the shape Claude Code reads: event -> list of entries.

    Each entry is an object whose optional `hooks` is a list of objects. Anything
    else is a file someone edited by hand, and rewriting it would lose their data.
    """
    if not isinstance(hooks, dict):
        return False
    for entries in hooks.values():
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            inner = entry.get("hooks", [])
            if not isinstance(inner, list) or not all(isinstance(h, dict) for h in inner):
                return False
    return True


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
    directory = hooks_dir or CLAUDE_HOOKS_DIR
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


def entry_has_managed_hook(entry: object) -> bool:
    """True when a hook group contains a hook Sibyl installed."""
    if not isinstance(entry, dict):
        return False
    return any(is_managed_hook(hook) for hook in entry.get("hooks") or [])


def remove_managed_hooks(hooks: dict) -> dict:
    """Drop only Sibyl's hook objects, keeping every other hook and its group.

    A group left empty by the removal held nothing but Sibyl's hooks, so it goes
    too; a group with other hooks keeps them, its matcher and its position.
    """
    cleaned: dict = {}
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


# ============================================================================
# Source Detection
# ============================================================================


def find_sibyl_repo() -> Path | None:
    """Find Sibyl repo if we're in a development context."""
    # Check common locations
    candidates = [
        Path.cwd(),
        Path.cwd().parent,
        Path.home() / "dev" / "sibyl",
        Path.home() / "projects" / "sibyl",
        Path.home() / "src" / "sibyl",
    ]

    for path in candidates:
        if (path / "skills" / "sibyl" / "SKILL.md").exists():
            return path

    return None


def get_package_data_dir() -> Path | None:
    """Get the package data directory for embedded skills/hooks."""
    # When installed as a package, data is in sibyl_cli/data/
    try:
        import sibyl_cli

        pkg_dir = Path(sibyl_cli.__file__).parent
        data_dir = pkg_dir / "data"
        if data_dir.exists():
            return data_dir
    except Exception:
        pass
    return None


# ============================================================================
# Installation Functions
# ============================================================================


def install_skills_symlink(source_dir: Path) -> tuple[int, int]:
    """Install skills as symlinks from source directory."""
    installed = 0
    updated = 0

    for skill_name in SKILL_NAMES:
        source = source_dir / "skills" / skill_name
        if not source.exists():
            continue

        # Install for Claude
        claude_target = CLAUDE_SKILLS_DIR / skill_name
        CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        if claude_target.is_symlink():
            if claude_target.resolve() == source.resolve():
                continue  # Already correct symlink
            claude_target.unlink()
            updated += 1
        elif claude_target.exists():
            shutil.rmtree(claude_target)
            updated += 1

        claude_target.symlink_to(source)
        installed += 1

        # Install for Codex
        codex_target = CODEX_SKILLS_DIR / skill_name
        CODEX_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        if codex_target.is_symlink():
            if codex_target.resolve() == source.resolve():
                continue
            codex_target.unlink()
        elif codex_target.exists():
            shutil.rmtree(codex_target)

        codex_target.symlink_to(source)

    return installed, updated


def install_skills_copy(data_dir: Path) -> tuple[int, int]:
    """Install skills by copying from package data."""
    installed = 0
    updated = 0

    skills_source = data_dir / "skills"
    if not skills_source.exists():
        return 0, 0

    for skill_name in SKILL_NAMES:
        source = skills_source / skill_name
        if not source.exists():
            continue

        # Install for Claude
        claude_target = CLAUDE_SKILLS_DIR / skill_name
        CLAUDE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        if claude_target.exists():
            shutil.rmtree(claude_target)
            updated += 1

        shutil.copytree(source, claude_target)
        installed += 1

        # Install for Codex
        codex_target = CODEX_SKILLS_DIR / skill_name
        CODEX_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        if codex_target.exists():
            shutil.rmtree(codex_target)

        shutil.copytree(source, codex_target)

    return installed, updated


def install_hooks_symlink(source_dir: Path) -> bool:
    """Install hooks as symlinks from source directory."""
    hooks_source = source_dir / "hooks"
    if not hooks_source.exists():
        return False

    CLAUDE_HOOKS_DIR.mkdir(parents=True, exist_ok=True)

    hook_files = ["session-start.py"]
    _prune_legacy_hook_files()
    for hook_file in hook_files:
        source = hooks_source / hook_file
        target = CLAUDE_HOOKS_DIR / hook_file

        if not source.exists():
            continue

        if target.is_symlink():
            if target.resolve() == source.resolve():
                continue
            target.unlink()
        elif target.exists():
            target.unlink()

        target.symlink_to(source)

    return True


def install_hooks_copy(data_dir: Path) -> bool:
    """Install hooks by copying from package data."""
    hooks_source = data_dir / "hooks"
    if not hooks_source.exists():
        return False

    CLAUDE_HOOKS_DIR.mkdir(parents=True, exist_ok=True)

    hook_files = ["session-start.py"]
    _prune_legacy_hook_files()
    for hook_file in hook_files:
        source = hooks_source / hook_file
        target = CLAUDE_HOOKS_DIR / hook_file

        if not source.exists():
            continue

        if target.exists():
            target.unlink()

        shutil.copy2(source, target)
        target.chmod(0o755)

    return True


def _prune_legacy_hook_files() -> None:
    """Remove hook scripts that previous Sibyl versions installed but no longer ship."""
    for legacy in ("user-prompt-submit.py",):
        target = CLAUDE_HOOKS_DIR / legacy
        if target.is_symlink() or target.exists():
            with suppress(OSError):
                target.unlink()


def configure_claude_hooks() -> bool:
    """Update Claude Code settings.json with Sibyl hooks."""
    CLAUDE_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings. An unreadable file is the user's, not ours to
    # replace: rewriting it from an empty dict would drop every other setting.
    try:
        if CLAUDE_SETTINGS_FILE.exists():
            settings = json.loads(CLAUDE_SETTINGS_FILE.read_text())
        else:
            settings = {}
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(settings, dict):
        return False
    if "hooks" in settings and not valid_hooks_shape(settings["hooks"]):
        return False

    # Backup if there are existing hooks
    existing_hooks = settings.get("hooks", {})
    if existing_hooks:
        backup = CLAUDE_SETTINGS_FILE.with_suffix(f".json.{datetime.now():%Y%m%d-%H%M%S}.bak")
        shutil.copy2(CLAUDE_SETTINGS_FILE, backup)

    # Remove Sibyl's own hooks, leaving every other hook where it was.
    hooks = remove_managed_hooks(settings.get("hooks") or {})

    # Add new Sibyl hooks
    sibyl_hooks = get_sibyl_hooks_config()
    for event, event_hooks in sibyl_hooks.items():
        if event not in hooks:
            hooks[event] = []
        hooks[event].extend(event_hooks)

    settings["hooks"] = hooks
    CLAUDE_SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    return True


# ============================================================================
# Main Setup Function
# ============================================================================


def setup_agent_integration(verbose: bool = True) -> bool:
    """Set up Claude/Codex integration for external assistants.

    Returns True if setup was successful.
    """
    if verbose:
        console.print()
        console.print(
            f"[{ELECTRIC_PURPLE}][bold]Claude/Codex Integration Setup[/bold][/{ELECTRIC_PURPLE}]"
        )
        console.print()

    hooks_installed = True

    # Determine source - prefer dev symlinks, fall back to package data
    repo_dir = find_sibyl_repo()
    data_dir = get_package_data_dir()

    use_symlinks = repo_dir is not None

    if use_symlinks and repo_dir is not None:
        if verbose:
            info(f"Development mode - using symlinks from {repo_dir}")

        # Install skills
        installed, updated = install_skills_symlink(repo_dir)
        if installed > 0:
            if verbose:
                success(f"Installed {installed} skill(s) as symlinks")
        elif updated > 0 and verbose:
            success(f"Updated {updated} skill symlink(s)")

        # Install hooks
        if install_hooks_symlink(repo_dir):
            if configure_claude_hooks():
                if verbose:
                    success("Installed Claude Code hooks")
            else:
                hooks_installed = False
                if verbose:
                    warn("Could not configure Claude Code hooks")
        else:
            hooks_installed = False
            if verbose:
                warn("Could not find hooks in repo")

    elif data_dir is not None:
        if verbose:
            info("Package mode - copying embedded skills/hooks")

        # Install skills
        installed, updated = install_skills_copy(data_dir)
        if installed > 0 and verbose:
            success(f"Installed {installed} skill(s)")

        # Install hooks
        if install_hooks_copy(data_dir):
            if configure_claude_hooks():
                if verbose:
                    success("Installed Claude Code hooks")
            else:
                hooks_installed = False
                if verbose:
                    warn("Could not configure Claude Code hooks")
        else:
            hooks_installed = False
            if verbose:
                warn("No embedded hooks found in package")

    else:
        if verbose:
            error("Could not find skill/hook source files")
            console.print()
            console.print("Run this command from the Sibyl repository directory,")
            console.print("or ensure the package includes embedded data.")
        return False

    if verbose:
        console.print()
        if hooks_installed:
            success("Sibyl integration setup complete!")
        else:
            # Claiming completion under a warning that hooks are missing is the
            # same untruth as exiting 0 on a refusal, one layer up.
            warn("Sibyl skills installed; hooks were not.")
        console.print()
        claude_line = "Skills and hooks installed" if hooks_installed else "Skills installed only"
        console.print(f"  [{NEON_CYAN}]Claude Code:[/{NEON_CYAN}]  {claude_line}")
        console.print(f"  [{NEON_CYAN}]Codex CLI:[/{NEON_CYAN}]    Skills installed (no hooks)")
        console.print()
        if hooks_installed:
            console.print(f"[{CORAL}]Restart Claude Code to activate hooks.[/{CORAL}]")

    # Hooks are half of what this command installs, so a run that skipped them
    # is not a success its caller can report as one.
    return hooks_installed


def print_prompt_snippet() -> None:
    """Print the prompt snippet for users to add to their assistant config."""
    console.print()
    console.print(
        f"[{ELECTRIC_PURPLE}][bold]Add to Your Assistant Config[/bold][/{ELECTRIC_PURPLE}]"
    )
    console.print()
    console.print(
        "Copy this into your agent's instructions "
        "([bold]~/.claude/CLAUDE.md[/bold], [bold]AGENTS.md[/bold], or its system prompt):"
    )
    console.print()

    panel = Panel(
        AGENT_PROMPT_SNIPPET,
        title="[bold]Sibyl Integration[/bold]",
        border_style=NEON_CYAN,
        padding=(1, 2),
    )
    console.print(panel)


def get_installation_status() -> dict:
    """Get current installation status."""
    claude_skills: list[dict[str, str | bool | None]] = []
    codex_skills: list[dict[str, str | bool | None]] = []
    claude_hooks = False
    claude_hooks_configured = False

    # Check Claude skills
    for skill_name in SKILL_NAMES:
        skill_path = CLAUDE_SKILLS_DIR / skill_name
        if skill_path.exists():
            is_symlink = skill_path.is_symlink()
            claude_skills.append(
                {
                    "name": skill_name,
                    "path": str(skill_path),
                    "symlink": is_symlink,
                    "target": str(skill_path.resolve()) if is_symlink else None,
                }
            )

    # Check Codex skills
    for skill_name in SKILL_NAMES:
        skill_path = CODEX_SKILLS_DIR / skill_name
        if skill_path.exists():
            is_symlink = skill_path.is_symlink()
            codex_skills.append(
                {
                    "name": skill_name,
                    "path": str(skill_path),
                    "symlink": is_symlink,
                    "target": str(skill_path.resolve()) if is_symlink else None,
                }
            )

    # Check hooks
    hook_files = ["session-start.py"]
    claude_hooks = all((CLAUDE_HOOKS_DIR / f).exists() for f in hook_files)

    # Check settings.json for hook configuration
    if CLAUDE_SETTINGS_FILE.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS_FILE.read_text())
            hooks = settings.get("hooks", {})
            # Check if Sibyl hooks are configured
            for event in ["SessionStart"]:
                if event in hooks:
                    for h in hooks[event]:
                        if entry_has_managed_hook(h):
                            claude_hooks_configured = True
                            break
        except Exception:
            pass

    return {
        "claude_skills": claude_skills,
        "codex_skills": codex_skills,
        "claude_hooks": claude_hooks,
        "claude_hooks_configured": claude_hooks_configured,
    }


def print_status() -> None:
    """Print current installation status."""
    status = get_installation_status()

    console.print()
    console.print(f"[{ELECTRIC_PURPLE}][bold]Agent Integration Status[/bold][/{ELECTRIC_PURPLE}]")
    console.print()

    # Claude skills
    console.print(f"[{NEON_CYAN}]Claude Code Skills:[/{NEON_CYAN}]")
    if status["claude_skills"]:
        for skill in status["claude_skills"]:
            link_info = f" → {skill['target']}" if skill["symlink"] else " (copy)"
            console.print(f"  [{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] {skill['name']}{link_info}")
    else:
        console.print(f"  [{CORAL}]✗[/{CORAL}] Not installed")

    console.print()

    # Codex skills
    console.print(f"[{NEON_CYAN}]Codex CLI Skills:[/{NEON_CYAN}]")
    if status["codex_skills"]:
        for skill in status["codex_skills"]:
            link_info = f" → {skill['target']}" if skill["symlink"] else " (copy)"
            console.print(f"  [{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] {skill['name']}{link_info}")
    else:
        console.print(f"  [{CORAL}]✗[/{CORAL}] Not installed")

    console.print()

    # Claude hooks
    console.print(f"[{NEON_CYAN}]Claude Code Hooks:[/{NEON_CYAN}]")
    if status["claude_hooks"]:
        console.print(f"  [{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] Hook scripts installed")
    else:
        console.print(f"  [{CORAL}]✗[/{CORAL}] Hook scripts not installed")

    if status["claude_hooks_configured"]:
        console.print(f"  [{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] Hooks configured in settings.json")
    else:
        console.print(f"  [{CORAL}]✗[/{CORAL}] Hooks not configured in settings.json")

    console.print()
