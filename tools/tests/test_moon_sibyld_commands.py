"""Every moon task that runs ``sibyld <subcommand>`` must name a real command.

A task that points at a removed subcommand keeps passing every static gate
and only fails when an operator runs it, so resolve each invocation against
the live Typer tree instead of trusting the YAML.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import typer
import yaml
from tools.tests.conftest import REPO_ROOT
from typer.core import TyperGroup

from sibyl.cli.main import app as sibyld_app

_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "\n"})
# uv flags whose value is a package name, not the program to run.
_PACKAGE_VALUE_FLAGS = frozenset({"--package", "--from", "--with"})


@dataclass(frozen=True, slots=True)
class SibyldInvocation:
    task: str
    words: tuple[str, ...]


def _moon_config_paths() -> list[Path]:
    workspace = yaml.safe_load((REPO_ROOT / ".moon/workspace.yml").read_text(encoding="utf-8"))
    projects = cast(dict[str, str], workspace["projects"])
    paths = [REPO_ROOT / source / "moon.yml" for source in projects.values()]
    paths.extend(sorted((REPO_ROOT / ".moon/tasks").glob("*.yml")))
    return [path for path in paths if path.is_file()]


def _shell_words_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return shlex.join(str(item) for item in value)
    return ""


def _task_shell_text(task: Mapping[str, object]) -> str:
    # moon appends ``args`` to ``command``, so they share one command line.
    command = " ".join(
        part for part in (_shell_words_text(task.get(key)) for key in ("command", "args")) if part
    )
    return "\n".join(part for part in (command, _shell_words_text(task.get("script"))) if part)


def _tokens(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
    lexer.whitespace = " \t\r"
    lexer.commenters = "#"
    lexer.wordchars += "$@%+,:{}[]^!"
    try:
        return list(lexer)
    except ValueError:
        # Embedded heredoc code can carry an unbalanced quote; plain splitting
        # still finds every bare ``sibyld`` word.
        return [token for line in text.splitlines() for token in (*line.split(), "\n")]


def _invocations(task_name: str, text: str) -> list[SibyldInvocation]:
    shell_words = _tokens(text)
    found: list[SibyldInvocation] = []
    for index, program in enumerate(shell_words):
        if Path(program).name != "sibyld":
            continue
        if index and shell_words[index - 1] in _PACKAGE_VALUE_FLAGS:
            continue
        words: list[str] = []
        for word in shell_words[index + 1 :]:
            if word in _SEPARATORS or word.startswith("-"):
                break
            words.append(word)
        found.append(SibyldInvocation(task=task_name, words=tuple(words)))
    return found


def _moon_sibyld_invocations() -> list[SibyldInvocation]:
    found: list[SibyldInvocation] = []
    for path in _moon_config_paths():
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tasks = cast(dict[str, dict[str, object]], config.get("tasks") or {})
        project = path.parent.relative_to(REPO_ROOT).as_posix()
        for name, task in tasks.items():
            found.extend(_invocations(f"{project}:{name}", _task_shell_text(task)))
    return found


def _unresolved_word(root: object, words: tuple[str, ...]) -> str | None:
    """Return the first word that names no subcommand, or None when all resolve.

    Words after a leaf command are its positional arguments, and a task may
    stop at a group and take the subcommand from ``moon run <task> -- ...``.
    """
    command = root
    for word in words:
        if not isinstance(command, TyperGroup):
            return None
        child = command.commands.get(word)
        if child is None:
            return word
        command = child
    return None


def test_sibyld_invocation_scan_sees_the_serve_and_worker_tasks() -> None:
    invoked = {invocation.words[:1] for invocation in _moon_sibyld_invocations()}

    assert ("serve",) in invoked
    assert ("worker",) in invoked


def test_scan_skips_package_names_and_stops_at_flags() -> None:
    text = (
        "uv build --package sibyld --out-dir dist/\n"
        "uv run --directory apps/api sibyld db migrate --dry-run && echo ok"
    )

    invocations = _invocations("root:example", text)

    assert invocations == [SibyldInvocation(task="root:example", words=("db", "migrate"))]


def test_scan_reads_moon_args_and_path_qualified_binaries() -> None:
    task = {"command": "uv run sibyld", "args": ["migrate", "rehearse"]}
    script = 'SIBYL_TOKEN="$token" .venv/bin/sibyld db backup "$out"'

    assert _invocations("root:args", _task_shell_text(task)) == [
        SibyldInvocation(task="root:args", words=("migrate", "rehearse"))
    ]
    assert _invocations("root:script", script) == [
        SibyldInvocation(task="root:script", words=("db", "backup", "$out"))
    ]


def test_unknown_subcommand_is_reported() -> None:
    root = typer.main.get_command(sibyld_app)

    assert _unresolved_word(root, ("migrate", "rehearse")) == "rehearse"
    assert _unresolved_word(root, ("migrate", "import", "archive.tar.gz")) is None
    assert _unresolved_word(root, ("migrate",)) is None


def test_every_moon_sibyld_invocation_names_a_real_subcommand() -> None:
    root = typer.main.get_command(sibyld_app)

    broken = [
        f"{invocation.task}: sibyld {' '.join(invocation.words)} (no subcommand {word!r})"
        for invocation in _moon_sibyld_invocations()
        if (word := _unresolved_word(root, invocation.words)) is not None
    ]

    assert not broken, "moon tasks invoke missing sibyld subcommands:\n" + "\n".join(broken)
