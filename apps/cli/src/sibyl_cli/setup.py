"""Agent integration setup for Claude Code and Codex.

Installs skills and hooks for AI agent integration:
  - Skills: ~/.claude/skills/sibyl/ and ~/.codex/skills/sibyl/
  - Hooks: ~/.claude/hooks/sibyl/ (Claude Code only)
"""

from __future__ import annotations

import json
import shutil
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

# ============================================================================
# Paths
# ============================================================================

CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
CLAUDE_HOOKS_DIR = Path.home() / ".claude" / "hooks" / "sibyl"
CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

CODEX_SKILLS_DIR = Path.home() / ".codex" / "skills"

# Skills to install
SKILL_NAMES = ["sibyl", "agent-orchestrate"]

# ============================================================================
# Prompt Snippet
# ============================================================================

PROMPT_SNIPPET = """## Sibyl - Your Persistent Memory

Sibyl is your knowledge graph—extended memory that persists across sessions.

### Session Start (MANDATORY)

**Immediately run `/sibyl` at the start of every session.** The skill provides full CLI guidance,
task context, and relevant patterns. No exceptions.

### Workflow

1. **Research first** — Search for patterns, past learnings, known issues before implementing
2. **Track tasks** — Never do significant work without a task. Update status as you go
3. **Capture learnings** — When you solve something non-obvious, add it to the graph

### What to Capture

**Always:** Non-obvious solutions, gotchas, configuration quirks, architectural decisions
**Consider:** Useful patterns, performance findings, integration approaches
**Skip:** Trivial info, temporary hacks, well-documented basics

### Quality Bar

**Bad:** "Fixed the auth bug"
**Good:** "JWT refresh tokens fail silently when Redis TTL expires. Root cause: token service
doesn't handle WRONGTYPE error. Fix: Add try/except with token regeneration fallback."

---

The graph should be smarter after every session. Search often. Add generously. Track everything.
"""


# ============================================================================
# Hook Configuration
# ============================================================================


def get_sibyl_hooks_config() -> dict:
    """Generate Sibyl hooks configuration for settings.json."""
    hooks_dir = str(CLAUDE_HOOKS_DIR)
    return {
        "SessionStart": [
            {
                "matcher": "startup",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"python3 {hooks_dir}/session-start.py",
                        "timeout": 10,
                    }
                ],
            },
            {
                "matcher": "resume",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"python3 {hooks_dir}/session-start.py",
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
                        "command": f"python3 {hooks_dir}/user-prompt-submit.py",
                        "timeout": 5,
                    }
                ],
            }
        ],
    }


def is_sibyl_hook(hook_entry: dict) -> bool:
    """Check if a hook entry is a Sibyl hook."""
    for hook in hook_entry.get("hooks", []):
        cmd = str(hook.get("command", ""))
        if "sibyl" in cmd or "hooks/sibyl" in cmd:
            return True
        prompt = str(hook.get("prompt", ""))
        if "Sibyl" in prompt and ("knowledge graph" in prompt or "sibyl add" in prompt.lower()):
            return True
    return False


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

    hook_files = ["session-start.py", "user-prompt-submit.py"]
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

    hook_files = ["session-start.py", "user-prompt-submit.py"]
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


def configure_claude_hooks() -> bool:
    """Update Claude Code settings.json with Sibyl hooks."""
    CLAUDE_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings
    try:
        if CLAUDE_SETTINGS_FILE.exists():
            settings = json.loads(CLAUDE_SETTINGS_FILE.read_text())
        else:
            settings = {}
    except json.JSONDecodeError:
        settings = {}

    # Backup if there are existing hooks
    existing_hooks = settings.get("hooks", {})
    if existing_hooks:
        backup = CLAUDE_SETTINGS_FILE.with_suffix(f".json.{datetime.now():%Y%m%d-%H%M%S}.bak")
        shutil.copy2(CLAUDE_SETTINGS_FILE, backup)

    # Remove old Sibyl hooks but preserve others
    hooks = settings.get("hooks", {})
    for event in list(hooks.keys()):
        hooks[event] = [h for h in hooks[event] if not is_sibyl_hook(h)]

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
    """Set up agent integration for Claude Code and Codex.

    Returns True if setup was successful.
    """
    if verbose:
        console.print()
        console.print(
            f"[{ELECTRIC_PURPLE}][bold]Agent Integration Setup[/bold][/{ELECTRIC_PURPLE}]"
        )
        console.print()

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
            if configure_claude_hooks() and verbose:
                success("Installed Claude Code hooks")
        else:
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
            if configure_claude_hooks() and verbose:
                success("Installed Claude Code hooks")
        else:
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
        success("Agent integration setup complete!")
        console.print()
        console.print(f"  [{NEON_CYAN}]Claude Code:[/{NEON_CYAN}]  Skills and hooks installed")
        console.print(f"  [{NEON_CYAN}]Codex CLI:[/{NEON_CYAN}]    Skills installed (no hooks)")
        console.print()
        console.print(f"[{CORAL}]Restart Claude Code to activate hooks.[/{CORAL}]")

    return True


def print_prompt_snippet() -> None:
    """Print the prompt snippet for users to add to their agent config."""
    console.print()
    console.print(f"[{ELECTRIC_PURPLE}][bold]Add to Your Agent Config[/bold][/{ELECTRIC_PURPLE}]")
    console.print()
    console.print(
        "Copy this to your [bold]~/.claude/CLAUDE.md[/bold] or [bold]~/.codex/AGENTS.md[/bold]:"
    )
    console.print()

    panel = Panel(
        PROMPT_SNIPPET,
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
    hook_files = ["session-start.py", "user-prompt-submit.py"]
    claude_hooks = all((CLAUDE_HOOKS_DIR / f).exists() for f in hook_files)

    # Check settings.json for hook configuration
    if CLAUDE_SETTINGS_FILE.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS_FILE.read_text())
            hooks = settings.get("hooks", {})
            # Check if Sibyl hooks are configured
            for event in ["SessionStart", "UserPromptSubmit"]:
                if event in hooks:
                    for h in hooks[event]:
                        if is_sibyl_hook(h):
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
