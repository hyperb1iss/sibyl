"""Split one pytest collection into deterministic, disjoint shards.

Load with ``-p sibyl_core.pytest_shard`` and select a slice with
``--shard K/N``. Each test lands in the slice picked by a stable hash of its
node id, so every shard (and every xdist worker inside a shard) computes the
same partition without a shared durations file, and a new test changes only
its own placement.
"""

from __future__ import annotations

import re
import zlib

import pytest

_SHARD_SPEC = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def parse_shard(spec: str) -> tuple[int, int]:
    """Return ``(index, total)`` for a one-based ``K/N`` spec."""
    match = _SHARD_SPEC.match(spec)
    if match is None:
        raise pytest.UsageError(f"--shard expects K/N, got {spec!r}")
    index, total = int(match.group(1)), int(match.group(2))
    if total < 1 or not 1 <= index <= total:
        raise pytest.UsageError(f"--shard {spec!r} needs 1 <= K <= N")
    return index, total


def shard_for(nodeid: str, total: int) -> int:
    """Return the one-based shard that owns ``nodeid``."""
    return zlib.crc32(nodeid.encode("utf-8")) % total + 1


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="K/N",
        help="Run only the K-th of N deterministic slices of the collected tests.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spec = config.getoption("--shard")
    if not spec:
        return
    index, total = parse_shard(spec)
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        (selected if shard_for(item.nodeid, total) == index else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected
