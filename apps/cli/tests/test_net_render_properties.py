"""What `sibyl net read` and `sibyl net watch` put on a terminal, for any message text.

Another agent writes the body, the agent label, the thread and the channel,
and every member writes their own display name, so none of it is trusted.
These seeded, table-driven fuzzers (the repository has no property-testing
library) run the real commands over a fake feed and check what reaches stdout:
no C0 or C1 control character other than a newline or a tab, no crash on
markup-looking text, and, for `watch`, one line per message.

``SIBYL_NET_FUZZ_EXAMPLES`` and ``SIBYL_NET_FUZZ_SEED`` size and seed the run.
"""

from __future__ import annotations

import os
import random
import re
import string
import unicodedata
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from rich.console import Console

from tests.test_net import (
    FakeNetClient,
    _invoke,
    _message,
)
from tests.test_net import (
    # Autouse: a throwaway HOME for the saved watch cursor, no harness labels.
    isolated_net_environment as isolated_net_environment,
)

EXAMPLES = int(os.environ.get("SIBYL_NET_FUZZ_EXAMPLES", "300"))
SEED = int(os.environ.get("SIBYL_NET_FUZZ_SEED", "20261102"))
BATCH = 12
FIELDS = ("body", "name", "agent", "channel", "thread")

C0 = [chr(code) for code in range(0x00, 0x20)]
C1 = [chr(code) for code in range(0x7F, 0xA0)]
HOSTILE_PIECES = [
    *C0,
    *C1,
    "\u2028",
    "\u2029",
    "\r\n",
    "\x1b[2K\r",
    "\x1b]0;pwned\x07",
    "\x1b]8;;https://evil.test\x1b\\link\x1b]8;;\x1b\\",
    "\x1b[31m",
    "\x9b2J",
    "\u202e",
    "\u200b",
    "[",
    "]",
    "[/]",
    "[bold red]",
    "[link=https://evil.test]",
    ":smile:",
    "\\",
    "\\[",
    "https://example.test",
    "123",
    "(",
    ")",
    "    ",
]
# Rich's own styling, when a terminal is forced: well-formed SGR sequences only.
_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _random_code_point(rng: random.Random) -> str:
    while True:
        code = rng.randrange(0, 0x110000)
        if not 0xD800 <= code <= 0xDFFF:
            return chr(code)


def fuzz_text(rng: random.Random, *, max_pieces: int = 16) -> str:
    pieces: list[str] = []
    for _ in range(rng.randint(1, max_pieces)):
        roll = rng.random()
        if roll < 0.4:
            pieces.append(rng.choice(string.printable))
        elif roll < 0.85:
            pieces.append(rng.choice(HOSTILE_PIECES))
        else:
            pieces.append(_random_code_point(rng))
    return "".join(pieces)


def message_with(index: int, **fields: str | None) -> dict[str, Any]:
    message = _message(
        f"m{index}",
        fields.get("body") or "x",
        channel=fields.get("channel") or "#reviews",
        thread=fields.get("thread"),
        agent=fields.get("agent"),
        kind="decision" if index % 3 == 0 else "message",
    )
    message["author"]["name"] = fields.get("name")
    return message


def fuzzed_message(rng: random.Random, index: int, fields: tuple[str, ...]) -> dict[str, Any]:
    return message_with(index, **{field: fuzz_text(rng) for field in fields})


def watch(messages: list[dict[str, Any]]) -> Any:
    # An explicit cursor, so no invocation resumes from one an earlier one saved.
    client = FakeNetClient(feed=[{"messages": messages, "next_since": "c1", "more": False}])
    return _invoke(client, ["watch", "-c", "#reviews", "--since", "c0", "--once"])


def read(messages: list[dict[str, Any]]) -> Any:
    client = FakeNetClient(feed=[{"messages": messages, "next_since": "c1", "more": False}])
    return _invoke(client, ["read", "#reviews"])


def control_characters(output: str) -> list[str]:
    visible = _SGR.sub("", output)
    return sorted(
        {
            f"U+{ord(char):04X}"
            for char in visible
            if char not in "\n\t" and unicodedata.category(char) == "Cc"
        }
    )


def batches(name: str, fields: tuple[str, ...]) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, EXAMPLES, BATCH):
        rng = random.Random(f"{SEED}:{name}:{start}")
        yield [fuzzed_message(rng, start + offset, fields) for offset in range(BATCH)]
    # Then every hostile piece on its own, in every field.
    for field in fields:
        yield [
            message_with(index, **{field: f"a{piece}b"})
            for index, piece in enumerate(HOSTILE_PIECES)
        ]


def minimal_failure(
    messages: list[dict[str, Any]],
    fields: tuple[str, ...],
    fails: Callable[[dict[str, Any]], bool],
) -> str:
    """The first failing message, each fuzzed field shrunk character by character."""
    culprit = next(message for message in messages if fails(message))
    values = {
        "body": culprit["body"],
        "name": culprit["author"]["name"],
        "agent": culprit["author"]["agent"],
        "channel": culprit["channel"],
        "thread": culprit["thread"],
    }
    for field in fields:
        text = values[field] or ""
        changed = True
        while changed:
            changed = False
            for cut in range(len(text)):
                candidate = text[:cut] + text[cut + 1 :]
                if fails(message_with(0, **{**values, field: candidate or None})):
                    text = candidate
                    values[field] = candidate or None
                    changed = True
                    break
    return ", ".join(f"{field}={values[field]!r}" for field in fields)


@pytest.mark.parametrize("command", [watch, read], ids=["watch", "read"])
def test_no_terminal_control_character_reaches_stdout_for_any_message(
    command: Callable[[list[dict[str, Any]]], Any],
) -> None:
    def fails(message: dict[str, Any]) -> bool:
        result = command([message])
        return result.exit_code != 0 or bool(control_characters(result.stdout))

    for messages in batches(f"controls-{command.__name__}", FIELDS):
        result = command(messages)
        if result.exit_code == 0 and not control_characters(result.stdout):
            continue
        detail = (
            f"exit {result.exit_code}: {result.exception!r}"
            if result.exit_code
            else f"controls {control_characters(result.stdout)}"
        )
        pytest.fail(
            f"{command.__name__} {detail}; minimal: {minimal_failure(messages, FIELDS, fails)}"
        )


@pytest.mark.parametrize("command", [watch, read], ids=["watch", "read"])
def test_no_terminal_control_character_reaches_a_forced_color_terminal(
    command: Callable[[list[dict[str, Any]]], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same property with Rich styling on, as on a real terminal."""
    # No file: Rich writes to whatever sys.stdout is at print time, which the
    # runner captures.
    terminal = Console(force_terminal=True, color_system="truecolor", width=160)
    monkeypatch.setattr("sibyl_cli.net.console", terminal)
    for messages in batches(f"controls-color-{command.__name__}", FIELDS):
        result = command(messages)
        assert result.exit_code == 0, result.exception
        assert control_characters(result.stdout) == [], result.stdout[:400]


def unindented_lines(output: str) -> list[str]:
    return [line for line in _SGR.sub("", output).split("\n") if line and not line.startswith("  ")]


def test_watch_prints_one_line_per_message_whatever_the_author_calls_themselves() -> None:
    """A display name is free text any member sets on their own profile.

    ``sibyl net watch`` promises one line per message, the way a dispatcher
    reading it line by line (or a human scanning it) tells messages apart. A
    name with a line break would print a second line shaped like another
    author's message.
    """

    def fails(message: dict[str, Any]) -> bool:
        result = watch([message])
        return len(unindented_lines(result.stdout)) != 1

    for messages in batches("watch-lines", ("name",)):
        result = watch(messages)
        assert result.exit_code == 0, result.exception
        if len(unindented_lines(result.stdout)) != len(messages):
            pytest.fail(
                f"{len(messages)} messages printed {len(unindented_lines(result.stdout))} lines; "
                f"minimal: {minimal_failure(messages, ('name',), fails)}"
            )


def test_read_prints_one_unindented_header_per_message_whatever_the_author_name() -> None:
    """In `read`, only a message's header line starts at the margin; its body is indented."""

    def fails(message: dict[str, Any]) -> bool:
        return len(unindented_lines(read([message]).stdout)) != 1

    for messages in batches("read-lines", ("name",)):
        result = read(messages)
        assert result.exit_code == 0, result.exception
        if len(unindented_lines(result.stdout)) != len(messages):
            pytest.fail(
                f"{len(messages)} messages printed {len(unindented_lines(result.stdout))} headers; "
                f"minimal: {minimal_failure(messages, ('name',), fails)}"
            )


def test_bodies_with_any_text_keep_one_header_per_message_in_read_and_watch() -> None:
    """Bodies may be multi-line; their continuation never reaches the margin."""
    for messages in batches("body-lines", ("body",)):
        watched = watch(messages)
        assert watched.exit_code == 0, watched.exception
        assert len(unindented_lines(watched.stdout)) == len(messages), watched.stdout[:400]
        shown = read(messages)
        assert shown.exit_code == 0, shown.exception
        assert len(unindented_lines(shown.stdout)) == len(messages), shown.stdout[:400]
