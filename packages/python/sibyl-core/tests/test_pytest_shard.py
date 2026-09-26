from __future__ import annotations

from typing import cast

import pytest

from sibyl_core import pytest_shard

_NODEIDS = [f"tests/test_mod_{i % 23}.py::test_case[{i}]" for i in range(400)]


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid


class _Hook:
    def __init__(self) -> None:
        self.deselected: list[_Item] = []

    def pytest_deselected(self, items: list[_Item]) -> None:
        self.deselected.extend(items)


class _Config:
    def __init__(self, shard: str | None) -> None:
        self.shard = shard
        self.hook = _Hook()

    def getoption(self, name: str) -> str | None:
        assert name == "--shard"
        return self.shard


def _run_shard(spec: str | None) -> tuple[list[str], list[str]]:
    config = _Config(spec)
    items = [_Item(nodeid) for nodeid in _NODEIDS]
    pytest_shard.pytest_collection_modifyitems(
        cast(pytest.Config, config), cast(list[pytest.Item], items)
    )
    return [item.nodeid for item in items], [item.nodeid for item in config.hook.deselected]


@pytest.mark.parametrize("total", [1, 2, 3, 5])
def test_shards_partition_the_collection_exactly_once(total: int) -> None:
    owners: dict[str, int] = {}
    for index in range(1, total + 1):
        selected, deselected = _run_shard(f"{index}/{total}")
        assert sorted(selected + deselected) == sorted(_NODEIDS)
        for nodeid in selected:
            assert nodeid not in owners
            owners[nodeid] = index

    assert owners.keys() == set(_NODEIDS)


def test_shards_keep_collection_order_and_stay_balanced() -> None:
    first, _ = _run_shard("1/2")
    second, _ = _run_shard("2/2")

    assert first == [nodeid for nodeid in _NODEIDS if nodeid in set(first)]
    assert second == [nodeid for nodeid in _NODEIDS if nodeid in set(second)]
    assert abs(len(first) - len(second)) < len(_NODEIDS) // 5


def test_shard_placement_is_stable_across_processes() -> None:
    # crc32 is fixed by the algorithm, unlike str hashes, which
    # PYTHONHASHSEED salts per process. These pins catch a swap back.
    assert pytest_shard.shard_for("tests/test_config.py::test_defaults", 2) == 1
    assert pytest_shard.shard_for("tests/test_config.py::test_env", 2) == 2


def test_missing_shard_leaves_collection_untouched() -> None:
    selected, deselected = _run_shard(None)

    assert selected == _NODEIDS
    assert deselected == []


@pytest.mark.parametrize(("spec", "expected"), [("1/2", (1, 2)), (" 3 / 4 ", (3, 4))])
def test_parse_shard_reads_one_based_specs(spec: str, expected: tuple[int, int]) -> None:
    assert pytest_shard.parse_shard(spec) == expected


@pytest.mark.parametrize("spec", ["0/2", "3/2", "1/0", "2", "a/b", "1/2/3", "-1/2"])
def test_parse_shard_rejects_malformed_specs(spec: str) -> None:
    with pytest.raises(pytest.UsageError):
        pytest_shard.parse_shard(spec)
