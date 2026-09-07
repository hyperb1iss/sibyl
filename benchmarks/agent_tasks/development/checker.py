"""Independent behavioral checks for exposed, generated development tasks.

Executed outside the candidate workspace with the existing CheckerResult JSON
protocol. This trusted checker executes candidate Python; it is not a sealed
sandbox or an oracle resistant to hostile same-user code.
"""

# ruff: noqa: PLR2004
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def require(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def rejects(callback, error: type[Exception]) -> None:
    try:
        callback()
    except error:
        return
    raise AssertionError(f"expected {error.__name__}")


def config_presence(app) -> None:
    for value in (False, 0, "", [], {}, None, "explicit"):
        defaults = {"chosen": "default", "retained": 5}
        overrides = {"chosen": value, "unsupported": "ignore"}
        before = deepcopy((defaults, overrides))
        expected = "default" if value is None else value
        require(
            app.resolve_options(defaults, overrides) == {"chosen": expected, "retained": 5},
            "explicit override semantics",
        )
        require((defaults, overrides) == before, "input mutation")
    require(app.resolve_options({}, {"extra": 1}) == {}, "unsupported options")


def patch_clear(app) -> None:
    for value in (False, 0, "", [], {}, None, "new"):
        current = {"chosen": "old", "retained": 5}
        patch = {"chosen": value, "absent": None, "new": False}
        before = deepcopy((current, patch))
        expected = {"retained": 5, "new": False}
        if value is not None:
            expected["chosen"] = value
        require(app.apply_patch(current, patch) == expected, "patch presence/deletion semantics")
        require((current, patch) == before, "input mutation")
    current = {"x": 1}
    require(app.apply_patch(current, {}) is not current, "result must be a fresh mapping")


def cursor_zero(app) -> None:
    for tokens in ((0, ""), ("", 3), ("opaque", "last")):
        cursors = (None, *tokens)
        pages = {
            cursors[0]: {"items": ["same"], "next_cursor": cursors[1]},
            cursors[1]: {"items": [], "next_cursor": cursors[2]},
            cursors[2]: {"items": ["same", "last"], "next_cursor": None},
        }
        before = deepcopy(pages)
        calls = []

        def fetch(cursor, *, calls=calls, pages=pages):
            calls.append(cursor)
            return pages[cursor]

        require(app.collect_pages(fetch) == ["same", "same", "last"], "dropped export rows")
        require(calls == list(cursors), "wrong cursor sequence")
        require(pages == before, "page mutation")
    require(
        app.collect_pages(lambda cursor: {"items": [], "next_cursor": None}) == [], "empty export"
    )


def offset_filtered(app) -> None:
    for total in (0, 1, 4, 7, 12):
        for size in (1, 3, 5):
            calls = []
            expected = [value % 2 for value in range(total) if value % 3 == 2]

            def fetch(offset, limit, *, calls=calls, total=total):
                calls.append((offset, limit))
                require(len(calls) <= total + 1, "pagination made no progress")
                return {
                    "items": [
                        value % 2
                        for value in range(offset, min(offset + limit, total))
                        if value % 3 == 2
                    ],
                    "total": total,
                }

            require(app.collect_pages(fetch, size) == expected, "filtered rows lost")
            require(
                calls == [(offset, size) for offset in range(0, max(total, 1), size)],
                "wrong offset advancement",
            )
    for size in (0, -1):
        rejects(lambda size=size: app.collect_pages(lambda *_: None, size), ValueError)


def window_overlap(app) -> None:
    origin = datetime(2026, 2, 1, tzinfo=UTC)
    hour = timedelta(hours=1)
    for a, b, c, d, expected in (
        (0, 1, 1, 2, False),
        (1, 2, 0, 1, False),
        (0, 3, 1, 2, True),
        (0, 2, 1, 3, True),
        (0, 1, 2, 3, False),
        (0, 2, 0, 2, True),
    ):
        left = (origin + a * hour, origin + b * hour)
        right = tuple(
            (origin + value * hour).astimezone(timezone(timedelta(hours=5, minutes=30)))
            for value in (c, d)
        )
        require(app.overlaps(left, right) is expected, "half-open absolute instant overlap")
    good = (origin, origin + hour)
    for bad in (
        (origin, origin),
        (origin + hour, origin),
        (origin.replace(tzinfo=None), origin + hour),
    ):
        rejects(lambda bad=bad: app.overlaps(bad, good), ValueError)
        rejects(lambda bad=bad: app.overlaps(good, bad), ValueError)


def date_entitlement(app) -> None:
    start = date(2026, 2, 27)
    end = date(2026, 3, 2)
    for delta in range(-2, 7):
        require(
            app.active_on(start, end, start + timedelta(days=delta)) is (0 <= delta <= 3),
            "inclusive calendar boundaries",
        )
    require(app.active_on(start, start, start), "one-day entitlement")
    rejects(lambda: app.active_on(end, start, start), ValueError)
    for bad in (None, "2026-02-27", datetime(2026, 2, 27, tzinfo=UTC)):
        for position in range(3):
            args = [start, end, start]
            args[position] = bad
            rejects(lambda args=args: app.active_on(*args), TypeError)


def versioned_events(app) -> None:
    cases = [
        (
            [
                {"key": "a", "version": 4, "deleted": True, "value": None},
                {"key": "a", "version": 2, "deleted": False, "value": "old"},
            ],
            {},
        ),
        (
            [
                {"key": "a", "version": 3, "deleted": False, "value": False},
                {"key": "a", "version": 1, "deleted": True, "value": None},
            ],
            {"a": False},
        ),
        (
            [
                {"key": "a", "version": 2, "deleted": True, "value": None},
                {"key": "a", "version": 2, "deleted": False, "value": None},
            ],
            {"a": None},
        ),
        (
            [
                {"key": "a", "version": 2, "deleted": False, "value": 1},
                {"key": "a", "version": 2, "deleted": True, "value": 99},
            ],
            {},
        ),
        ([], {}),
    ]
    for events, expected in cases:
        before = deepcopy(events)
        require(app.materialize(events) == expected, "version/tombstone selection")
        require(events == before, "event mutation")
    events = [
        {"key": key, "version": version, "deleted": False, "value": version}
        for version in (4, 1, 3, 2)
        for key in ("a", "b")
    ]
    require(app.materialize(events) == {"a": 4, "b": 4}, "independent key versions")


def ordered_deltas(app) -> None:
    cases = [
        (
            {"a": 2},
            [
                {"op": "increment", "key": "a", "value": 3},
                {"op": "increment", "key": "a", "value": 4},
            ],
            {"a": 9},
        ),
        (
            {"a": 99},
            [{"op": "remove", "key": "a"}, {"op": "increment", "key": "a", "value": 2}],
            {"a": 2},
        ),
        (
            {},
            [{"op": "set", "key": "a", "value": 4}, {"op": "increment", "key": "a", "value": -1}],
            {"a": 3},
        ),
        ({"a": 3}, [{"op": "increment", "key": "a", "value": 4}, {"op": "remove", "key": "a"}], {}),
        (
            {"z": 7},
            [{"op": "set", "key": "a", "value": None}, {"op": "increment", "key": "b", "value": 0}],
            {"z": 7, "a": None, "b": 0},
        ),
        ({"a": 1}, [], {"a": 1}),
    ]
    for initial, events, expected in cases:
        before = deepcopy((initial, events))
        result = app.replay(initial, events)
        require(result == expected, "ordered delta replay")
        require(result is not initial and (initial, events) == before, "input mutation")


CHECKS = {
    "config-presence": config_presence,
    "patch-clear": patch_clear,
    "cursor-zero": cursor_zero,
    "offset-filtered": offset_filtered,
    "window-overlap": window_overlap,
    "date-entitlement": date_entitlement,
    "versioned-events": versioned_events,
    "ordered-deltas": ordered_deltas,
}


def load_candidate():
    spec = importlib.util.spec_from_file_location("candidate", Path.cwd() / "app.py")
    if spec is None or spec.loader is None:
        raise ImportError("candidate module unavailable")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    return app


def main() -> None:
    request = json.load(sys.stdin)
    if not request.get("attempt_id") or not request.get("snapshot_sha256"):
        raise ValueError("checker request must identify the checked attempt and snapshot")
    check = CHECKS[sys.argv[1]]
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            check(load_candidate())
    except Exception as exc:
        result: dict[str, Any] = {"passed": False, "detail": f"{type(exc).__name__}: {exc}"}
    else:
        result = {"passed": True, "detail": f"{sys.argv[1]} behavioral contract passed"}
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
