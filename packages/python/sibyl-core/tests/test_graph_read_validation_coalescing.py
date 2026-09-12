"""Concurrent requests share only identical graph inputs within one phase."""

import asyncio

import pytest

from sibyl_core.services.graph_read_validation import GraphReadValidation


async def test_graph_read_overlapping_batches_share_snapshot(monkeypatch):
    from sibyl_core.services import validation_promotion

    calls = []
    release = asyncio.Event()
    both_started = asyncio.Event()

    async def current(org, ids):
        calls.append(ids)
        if len(calls) == 2:
            both_started.set()
        await release.wait()
        return dict.fromkeys(ids, len(calls) == 2)

    monkeypatch.setattr(validation_promotion, "validated_graph_currents", current)
    read = GraphReadValidation("org")
    first = asyncio.create_task(read.prepare_graph(["a", "b"]))
    await asyncio.sleep(0)
    second = asyncio.create_task(read.prepare_graph(["b", "c"]))
    await asyncio.wait_for(both_started.wait(), 2)
    release.set()
    await asyncio.gather(first, second)
    assert calls == [["a", "b"], ["c"]]
    assert await read.graph_validated("org", "b") is True
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [False, True])
async def test_graph_read_shared_failure_and_new_phase(monkeypatch, failure):
    from sibyl_core.services import validation_promotion

    calls = []

    async def current(org, ids):
        calls.append(ids)
        await asyncio.sleep(0)
        if failure:
            raise ValueError("unavailable")
        return dict.fromkeys(ids, len(calls) == 1)

    monkeypatch.setattr(validation_promotion, "validated_graph_currents", current)
    read = GraphReadValidation("org")
    values = await asyncio.gather(
        read.graph_validated("org", "a"),
        read.graph_validated("org", "a"),
        return_exceptions=True,
    )
    assert calls == [["a"]]
    if failure:
        assert all(isinstance(value, ValueError) for value in values)
    else:
        assert values == [True, True]
        assert await GraphReadValidation("org").graph_validated("org", "a") is False


async def test_graph_read_cancelled_waiter_preserves_shared_load(monkeypatch):
    from sibyl_core.services import validation_promotion

    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def current(org, ids):
        calls.append(ids)
        started.set()
        await release.wait()
        return dict.fromkeys(ids, True)

    monkeypatch.setattr(validation_promotion, "validated_graph_currents", current)
    read = GraphReadValidation("org")
    first = asyncio.create_task(read.graph_validated("org", "a"))
    await started.wait()
    second = asyncio.create_task(read.graph_validated("org", "a"))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second is True
    assert calls == [["a"]]
