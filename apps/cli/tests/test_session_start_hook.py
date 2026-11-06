"""The packaged SessionStart hook: its mirror, and the one line about a waiting inbox."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

PACKAGED = Path(__file__).resolve().parents[1] / "src/sibyl_cli/data/hooks/session-start.py"
MIRROR = Path(__file__).resolve().parents[3] / "hooks/session-start.py"


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("session_start_hook", PACKAGED)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCheck:
    def __init__(self, output: str = "", returncode: int = 0, hangs: bool = False) -> None:
        self.output, self.returncode, self.hangs, self.killed = output, returncode, hangs, False

    def communicate(self, timeout: float) -> tuple[str, None]:
        assert 0 < timeout <= 9
        if self.hangs:
            raise subprocess.TimeoutExpired("sibyl", timeout)
        return self.output, None

    def kill(self) -> None:
        self.killed = True


def page(count: int, *, hours_ago: float, more: bool = False) -> str:
    oldest = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    messages = [{"id": f"m{i}", "channel": "@ada", "created_at": oldest} for i in range(count)]
    return json.dumps(
        {
            "messages": messages,
            "more": more,
            "web_url": "https://sibyl.acme.test/net?channel=%40ada",
        }
    )


def test_the_packaged_hook_and_its_mirror_are_identical() -> None:
    assert PACKAGED.read_bytes() == MIRROR.read_bytes()


def test_the_hook_reads_the_inbox_under_its_own_mark() -> None:
    hook = load_hook()
    assert hook.INBOX_COMMAND[1:5] == ("net", "read", "@me", "--new")
    assert hook.INBOX_COMMAND[5:7] == ("--mark", "session-start")


@pytest.mark.parametrize(
    ("hours_ago", "more", "expected"),
    [
        (0.2, False, "📬 3 new in @ada in the last 12m"),
        (5, True, "📬 3+ new in @ada since 5h ago"),
        (30, False, "📬 3 new in @ada since yesterday"),
        (80, False, "📬 3 new in @ada since 3 days ago"),
    ],
)
def test_new_inbox_messages_make_one_line(hours_ago: float, more: bool, expected: str) -> None:
    hook = load_hook()
    line = hook.inbox_line(
        FakeCheck(page(3, hours_ago=hours_ago, more=more)), started=time.monotonic()
    )
    assert line == (f"{expected} · sibyl net read @me · https://sibyl.acme.test/net?channel=%40ada")


@pytest.mark.parametrize(
    "check",
    [
        None,
        FakeCheck(page(0, hours_ago=1)),
        FakeCheck("not json"),
        FakeCheck(page(2, hours_ago=1), returncode=1),
    ],
)
def test_the_hook_says_nothing_when_the_inbox_is_empty_or_anything_fails(check: object) -> None:
    hook = load_hook()
    assert hook.inbox_line(check, started=time.monotonic()) is None


def test_a_slow_inbox_check_is_abandoned_not_waited_on() -> None:
    hook = load_hook()
    check = FakeCheck(hangs=True)
    assert hook.inbox_line(check, started=time.monotonic()) is None
    assert check.killed
