"""Per-organization read memos compute once per window and drop on writes."""

from __future__ import annotations

import asyncio

import pytest

from sibyl.persistence import read_memo as read_memo_module
from sibyl.persistence.read_memo import OrgReadMemo, invalidate_org_read_memos


@pytest.fixture
def memo() -> OrgReadMemo[int]:
    memo: OrgReadMemo[int] = OrgReadMemo("test", ttl_seconds=60.0)
    yield memo
    read_memo_module._MEMOS.remove(memo)


class _Counter:
    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        self.calls = 0
        self.gate = gate

    async def __call__(self) -> int:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return self.calls


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_computation(memo: OrgReadMemo[int]) -> None:
    gate = asyncio.Event()
    compute = _Counter(gate=gate)

    waiters = [asyncio.create_task(memo.get("org-a", compute)) for _ in range(10)]
    await asyncio.sleep(0)
    gate.set()
    values = await asyncio.gather(*waiters)

    assert compute.calls == 1
    assert values == [1] * 10
    assert memo.peek("org-a") == 1


@pytest.mark.asyncio
async def test_value_is_served_until_the_window_ends(memo: OrgReadMemo[int]) -> None:
    compute = _Counter()
    memo.ttl_seconds = 0.05

    assert await memo.get("org-a", compute) == 1
    assert await memo.get("org-a", compute) == 1
    assert compute.calls == 1

    await asyncio.sleep(0.06)
    assert memo.peek("org-a") is None
    assert await memo.get("org-a", compute) == 2


@pytest.mark.asyncio
async def test_organizations_do_not_share_values(memo: OrgReadMemo[int]) -> None:
    first = _Counter()
    second = _Counter()

    assert await memo.get("org-a", first) == 1
    assert await memo.get("org-b", second) == 1
    assert first.calls == 1
    assert second.calls == 1


@pytest.mark.asyncio
async def test_invalidation_during_compute_discards_the_result(memo: OrgReadMemo[int]) -> None:
    gate = asyncio.Event()
    compute = _Counter(gate=gate)

    waiter = asyncio.create_task(memo.get("org-a", compute))
    await asyncio.sleep(0)
    # A write lands while the pre-write rows are still being aggregated.
    memo.invalidate("org-a")
    gate.set()

    assert await waiter == 1
    assert memo.peek("org-a") is None
    assert await memo.get("org-a", compute) == 2


@pytest.mark.asyncio
async def test_registry_invalidation_targets_one_organization(memo: OrgReadMemo[int]) -> None:
    await memo.get("org-a", _Counter())
    await memo.get("org-b", _Counter())

    invalidate_org_read_memos("org-a")

    assert memo.peek("org-a") is None
    assert memo.peek("org-b") == 1


@pytest.mark.asyncio
async def test_failures_reach_every_waiter_and_leave_nothing_cached(
    memo: OrgReadMemo[int],
) -> None:
    gate = asyncio.Event()

    async def failing() -> int:
        await gate.wait()
        raise RuntimeError("boom")

    waiters = [asyncio.create_task(memo.get("org-a", failing)) for _ in range(3)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert all(isinstance(result, RuntimeError) for result in results)
    assert memo.peek("org-a") is None
    assert await memo.get("org-a", _Counter()) == 1


@pytest.mark.asyncio
async def test_a_cancelled_waiter_does_not_cancel_the_shared_computation(
    memo: OrgReadMemo[int],
) -> None:
    gate = asyncio.Event()
    compute = _Counter(gate=gate)

    first = asyncio.create_task(memo.get("org-a", compute))
    second = asyncio.create_task(memo.get("org-a", compute))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    gate.set()

    assert await second == 1
    assert compute.calls == 1
