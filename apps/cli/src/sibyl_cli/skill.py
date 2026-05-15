"""Skill contract inspection and installation commands."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer

from sibyl_cli.common import NEON_CYAN, console, error, info, success, warn

app = typer.Typer(
    help="Print or install the canonical Sibyl skill",
    invoke_without_command=True,
)

SKILL_NAME = "sibyl"
SKILL_RELATIVE_PATH = "data/skills/sibyl"


def canonical_skill_dir() -> Path:
    skill_dir = files("sibyl_cli").joinpath(SKILL_RELATIVE_PATH)
    return Path(str(skill_dir))


def canonical_skill_markdown() -> str:
    return canonical_skill_dir().joinpath("SKILL.md").read_text(encoding="utf-8")


def default_skill_roots() -> list[Path]:
    home = Path.home()
    return [
        home / ".claude" / "skills",
        home / ".codex" / "skills",
        home / ".agents" / "skills",
    ]


def install_canonical_skill(
    *,
    roots: Iterable[Path] | None = None,
    force: bool = False,
) -> dict[str, list[str]]:
    source = canonical_skill_dir()
    if not source.exists():
        raise FileNotFoundError(f"Canonical skill not found: {source}")

    installed: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    for root in roots or default_skill_roots():
        target = root / SKILL_NAME
        if target.exists() or target.is_symlink():
            if target.is_symlink() and not force:
                skipped.append(str(target))
                continue
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif force:
                target.unlink()
            else:
                skipped.append(str(target))
                continue
            updated.append(str(target))
        else:
            installed.append(str(target))

        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)

    return {"installed": installed, "updated": updated, "skipped": skipped}


@app.callback(invoke_without_command=True)
def skill(
    install: Annotated[
        bool,
        typer.Option("--install", help="Install the bundled skill into assistant skill roots"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace existing symlink or non-directory skill targets"),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress install status output"),
    ] = False,
) -> None:
    """Print the canonical skill markdown, or install it with --install."""
    if not install:
        sys.stdout.write(canonical_skill_markdown())
        return

    try:
        result = install_canonical_skill(force=force)
    except OSError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    if quiet:
        return

    installed = result["installed"]
    updated = result["updated"]
    skipped = result["skipped"]

    if installed:
        success(f"Installed {len(installed)} skill root(s)")
    if updated:
        success(f"Updated {len(updated)} skill root(s)")
    if skipped:
        warn(f"Skipped {len(skipped)} existing symlink or protected target(s)")
        info("Use: sibyl skill --install --force")

    for path in [*installed, *updated]:
        console.print(f"  [{NEON_CYAN}]{path}[/{NEON_CYAN}]")
