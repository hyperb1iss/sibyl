"""Every `sibyl ...` suggestion the CLI prints must name a command that exists.

Hints go stale silently: a verb gets renamed or repurposed and the old spelling
keeps printing in error paths nobody exercises. This walks every string literal
in the CLI source, resolves each suggestion against the real command tree, and
fails on paths that do not exist or that the recall verb would swallow as its
goal: `sibyl context list` builds a memory pack for the goal "list", and
`sibyl context use local` fails on an unexpected extra argument.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command

from sibyl_cli import recall
from sibyl_cli.main import app

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "sibyl_cli"

# Stands in for an f-string replacement field, so it reads as a placeholder.
VALUE = "<value>"

# Rich markup ends a command the same way a closing quote does. It becomes a
# NUL rather than a newline so line offsets inside the literal stay true.
MARKUP = re.compile(r"\[/?[\w #.<>-]*\]")

# A suggestion runs from `sibyl` to the first character that cannot be part
# of one shell command: quotes, backticks, chaining, commas, parentheses, and
# a period that ends a sentence rather than sitting inside a token.
SUGGESTION = re.compile(r"(?<![\w/.~-])sibyl[ \t]+((?:[^\n\0`'\"&|;,().]|\.(?!\s|$))+)")

# Strings that mention `sibyl` followed by prose rather than a command. Keyed
# by file and suggestion so a new occurrence elsewhere is still checked.
NOT_COMMANDS = {
    ("data/hooks/session-start.py", "command and return stdout"),
    ("data/hooks/session-start.py", "commands this session"),
}


@dataclass(frozen=True)
class Suggestion:
    path: str
    line: int
    text: str

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}"


def _render(node: ast.JoinedStr) -> str:
    return "".join(part.value if isinstance(part, ast.Constant) else VALUE for part in node.values)


def _fstring_parts(tree: ast.AST) -> set[int]:
    """Ids of constants that belong to an f-string, which is scanned whole."""
    return {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
        if isinstance(part, ast.Constant)
    }


def _argv(node: ast.List | ast.Tuple) -> str | None:
    """`["sibyl", "docker", "upgrade", tag]` as the command line it runs."""
    first = node.elts[0] if node.elts else None
    if not (isinstance(first, ast.Constant) and first.value == "sibyl"):
        return None
    return " ".join(
        elt.value if isinstance(elt, ast.Constant) and isinstance(elt.value, str) else VALUE
        for elt in node.elts[1:]
    )


def _suggestions_in(source: Path, relative: str) -> list[Suggestion]:
    tree = ast.parse(source.read_text(), filename=str(source))
    fstring_parts = _fstring_parts(tree)
    found: list[Suggestion] = []
    for node in ast.walk(tree):
        # Subprocess argv lists run the command directly, so they resolve too.
        if isinstance(node, ast.List | ast.Tuple) and (argv := _argv(node)):
            found.append(Suggestion(relative, node.lineno, argv))
            continue
        if isinstance(node, ast.JoinedStr):
            text = _render(node)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in fstring_parts
        ):
            text = node.value
        else:
            continue
        text = MARKUP.sub("\0", text)
        # Only a literal that spans lines maps its own newlines onto source
        # lines; an escaped "\n" inside a one-line literal does not.
        multiline = node.end_lineno != node.lineno
        for match in SUGGESTION.finditer(text):
            offset = text.count("\n", 0, match.start()) if multiline else 0
            found.append(Suggestion(relative, node.lineno + offset, match.group(1).strip()))
    return sorted(found, key=lambda suggestion: suggestion.line)


def iter_suggestions(root: Path = SOURCE_ROOT) -> Iterator[Suggestion]:
    for source in sorted(root.rglob("*.py")):
        yield from _suggestions_in(source, source.relative_to(root).as_posix())


def _is_placeholder(token: str) -> bool:
    return token.startswith(("<", "[", "{")) or token == "..."


def _is_group(command: Any) -> bool:
    return hasattr(command, "list_commands") and hasattr(command, "get_command")


def _option_takes_value(command: Any, token: str) -> bool | None:
    """Whether `token` is a known option of `command` that consumes a value."""
    for param in getattr(command, "params", []):
        opts = [*getattr(param, "opts", []), *getattr(param, "secondary_opts", [])]
        if token in opts and param.param_type_name == "option":
            return not (param.is_flag or param.count)
    return None


def _is_recall_verb(command: Any) -> bool:
    callback = getattr(command, "callback", None)
    return callback is not None and inspect.unwrap(callback) is recall.recall_context


@dataclass(frozen=True)
class Resolution:
    path: tuple[str, ...]
    command: Any
    rest: tuple[str, ...]


def resolve(root: Any, ctx: Any, words: list[str]) -> Resolution:
    """Walk the command tree, consuming tokens until one is not a subcommand.

    Group options are skipped along with their values so `sibyl -C <name>
    auth login` still resolves to `auth login`.
    """
    command = root
    path: list[str] = []
    index = 0
    while index < len(words) and _is_group(command):
        token = words[index]
        if token.startswith("-"):
            takes_value = _option_takes_value(command, token)
            if takes_value is None:
                break
            index += 2 if takes_value else 1
            continue
        sub = command.get_command(ctx, token)
        if sub is None:
            break
        path.append(token)
        command = sub
        index += 1
    return Resolution(tuple(path), command, tuple(words[index:]))


def _walk(command: Any, ctx: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    yield path
    if _is_group(command):
        for name in command.list_commands(ctx):
            yield from _walk(command.get_command(ctx, name), ctx, (*path, name))


def _did_you_mean(root: Any, ctx: Any, name: str, word: str) -> list[str]:
    """Paths where `<name> <word>` is a real subcommand, e.g. `config context use`."""
    return [
        "sibyl " + " ".join(path)
        for path in _walk(root, ctx)
        if len(path) >= 2 and path[-2:] == (name, word)
    ]


def check(root: Any, ctx: Any, text: str) -> str | None:
    """Return why a suggestion is broken, or None when it resolves."""
    # A hint that ends a sentence ("run sibyl up.") keeps its period.
    words = [word.rstrip(".:!?") or word for word in text.split()]
    if not words:
        return None
    resolution = resolve(root, ctx, words)
    rest = resolution.rest
    shown = "sibyl " + " ".join(resolution.path) if resolution.path else "sibyl"

    if not resolution.path:
        if not rest or rest[0].startswith("-") or _is_placeholder(rest[0]):
            return None
        return f"`sibyl {rest[0]}` is not a command"

    if not rest or rest[0].startswith("-") or _is_placeholder(rest[0]):
        return None

    word = rest[0]
    if _is_group(resolution.command):
        return f"`{shown}` has no subcommand `{word}`"

    if _is_recall_verb(resolution.command):
        reason = f"`{shown}` takes `{word}` as its recall goal"
        if hints := _did_you_mean(root, ctx, resolution.path[-1], word):
            reason += f" (did you mean {', '.join(f'`{hint}`' for hint in hints)}?)"
        return reason
    return None


@pytest.fixture(scope="module")
def command_tree() -> tuple[Any, Any]:
    root = get_command(app)
    return root, root.make_context("sibyl", [], resilient_parsing=True)


@pytest.mark.parametrize(
    ("text", "broken"),
    [
        ("context use local", True),
        ("context list", True),
        ("context create local --use", True),
        ("recall pack", True),
        ("nope", True),
        ("task frobnicate", True),
        ("config context use local", False),
        ("config context create local --use", False),
        ("contexts list", False),
        ("context", False),
        ("context --quick", False),
        ("up.", False),
        ("local ...", False),
        ("context <goal> --intent build", False),
        ("-C <name> auth login", False),
        ("task show <value>", False),
        ("<value>", False),
        ("--help", False),
    ],
)
def test_resolver_classifies_suggestions(
    command_tree: tuple[Any, Any], text: str, *, broken: bool
) -> None:
    root, ctx = command_tree
    assert (check(root, ctx, text) is not None) is broken


def test_misrouted_recall_hint_names_the_real_command(command_tree: tuple[Any, Any]) -> None:
    root, ctx = command_tree
    reason = check(root, ctx, "context use <name>")
    assert reason is not None
    assert "`sibyl config context use`" in reason


def test_scanner_reads_fstrings_and_markup(tmp_path: Path) -> None:
    (tmp_path / "hints.py").write_text(
        "name = 'x'\n"
        'a = f"Run [bold {name}]sibyl context use {name}[/bold {name}] now"\n'
        "b = 'Next: sibyl auth login && sibyl doctor'\n"
        "c = (\n    'Run \\'sibyl init\\' '\n    'or \\'sibyl up --pull\\'.'\n)\n"
        "d = ['sibyl', 'docker', 'upgrade', '--tag', tag]\n"
        "e = 'Run sibyl context. It builds a pack from ~/.sibyl/local.'\n"
    )
    found = [(s.line, s.text) for s in iter_suggestions(tmp_path)]
    assert found == [
        (2, "context use <value>"),
        (3, "auth login"),
        (3, "doctor"),
        (5, "init"),
        (5, "up --pull"),
        (8, "docker upgrade --tag <value>"),
        (9, "context"),
    ]


def test_cli_hints_name_real_commands(command_tree: tuple[Any, Any]) -> None:
    root, ctx = command_tree
    suggestions = list(iter_suggestions())
    # A scanner that silently matches nothing would pass vacuously.
    assert len(suggestions) >= 50

    broken = [
        f"{s.location}: `sibyl {s.text}`: {reason}"
        for s in suggestions
        if (s.path, s.text) not in NOT_COMMANDS and (reason := check(root, ctx, s.text)) is not None
    ]
    assert broken == []


def test_not_commands_allowlist_has_no_stale_entries() -> None:
    seen = {(s.path, s.text) for s in iter_suggestions()}
    assert NOT_COMMANDS - seen == set()
