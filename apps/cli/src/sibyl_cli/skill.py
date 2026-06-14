"""Skill contract inspection and installation commands."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer

from sibyl_cli.common import NEON_CYAN, console, error, info, success, warn

app = typer.Typer(
    help="Install the loader skill and print bundled markdown packs",
    invoke_without_command=True,
)

SKILL_NAME = "sibyl"
SKILL_RELATIVE_PATH = "data/skills/sibyl"
SKILL_PACKS_RELATIVE_PATH = "data/skill-packs"


@dataclass(frozen=True)
class SkillPack:
    name: str
    filename: str
    description: str


SKILL_PACKS = {
    "core": SkillPack(
        name="core",
        filename="core.md",
        description="Core Sibyl workflow, command contract, and agent rules",
    ),
    "quick": SkillPack(
        name="quick",
        filename="quick.md",
        description="Minimal agent rules and verb table for subagents (~500 tokens)",
    ),
    "workflows": SkillPack(
        name="workflows",
        filename="workflows.md",
        description="Longer task, project, memory, and debugging workflows",
    ),
    "examples": SkillPack(
        name="examples",
        filename="examples.md",
        description="Concrete CLI examples for search, tasks, memory, and projects",
    ),
    "migration": SkillPack(
        name="migration",
        filename="migration.md",
        description="Legacy Graphiti/FalkorDB migration guidance",
    ),
}


def canonical_skill_dir() -> Path:
    skill_dir = files("sibyl_cli").joinpath(SKILL_RELATIVE_PATH)
    return Path(str(skill_dir))


def canonical_skill_markdown() -> str:
    return canonical_skill_dir().joinpath("SKILL.md").read_text(encoding="utf-8")


def skill_pack_dir() -> Path:
    pack_dir = files("sibyl_cli").joinpath(SKILL_PACKS_RELATIVE_PATH)
    return Path(str(pack_dir))


def available_skill_packs() -> list[SkillPack]:
    return [SKILL_PACKS[name] for name in sorted(SKILL_PACKS)]


def skill_pack_markdown(name: str) -> str:
    normalized = name.strip().lower()
    pack = SKILL_PACKS.get(normalized)
    if pack is None:
        choices = ", ".join(sorted(SKILL_PACKS))
        raise KeyError(f"Unknown skill pack: {name}. Available packs: {choices}")

    path = skill_pack_dir() / pack.filename
    return path.read_text(encoding="utf-8")


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


def print_install_result(result: dict[str, list[str]], *, quiet: bool) -> None:
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
        info("Use: sibyl skill install --force")

    for path in [*installed, *updated]:
        console.print(f"  [{NEON_CYAN}]{path}[/{NEON_CYAN}]")


def run_install(*, force: bool, quiet: bool) -> None:
    try:
        result = install_canonical_skill(force=force)
    except OSError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    print_install_result(result, quiet=quiet)


@app.callback(invoke_without_command=True)
def skill(
    ctx: typer.Context,
    install: Annotated[
        bool,
        typer.Option("--install", help="Install the loader skill into assistant skill roots"),
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
    """Print the loader skill markdown, or install it with --install."""
    if ctx.invoked_subcommand is not None:
        return

    if install:
        run_install(force=force, quiet=quiet)
        return

    sys.stdout.write(canonical_skill_markdown())


@app.command("install")
def install_skill(
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace existing symlink or non-directory skill targets"),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress install status output"),
    ] = False,
) -> None:
    """Install the stable Sibyl skill stub into assistant skill roots."""
    run_install(force=force, quiet=quiet)


@app.command("list")
def list_skill_packs() -> None:
    """List skill packs available from this installed CLI version."""
    for pack in available_skill_packs():
        console.print(f"[{NEON_CYAN}]{pack.name}[/{NEON_CYAN}]  {pack.description}")


@app.command("get")
def get_skill_pack(
    name: Annotated[
        str,
        typer.Argument(help="Skill pack to print. Run `sibyl skill list` for choices."),
    ] = "core",
) -> None:
    """Print a version-matched markdown skill pack from the CLI bundle."""
    try:
        sys.stdout.write(skill_pack_markdown(name))
    except KeyError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
