from __future__ import annotations

import asyncio

import pytest

from sibyl.services.recall_limits import (
    RECALL_MAX_CONCURRENT_ENV,
    RECALL_QUEUE_TIMEOUT_ENV,
    RecallConcurrencyLimiter,
    RecallConcurrencyLimitExceededError,
)
from sibyl_core.auth import OrganizationRole


@pytest.mark.asyncio
async def test_recall_limiter_refuses_a_member_slot_that_outwaits_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(RECALL_MAX_CONCURRENT_ENV, raising=False)
    limiter = RecallConcurrencyLimiter(default_max_concurrent=1, queue_timeout_seconds=0.02)

    async with limiter.slot(
        organization_id="org-1",
        user_id="user-1",
        organization_role=OrganizationRole.MEMBER,
    ):
        with pytest.raises(RecallConcurrencyLimitExceededError) as exc:
            async with limiter.slot(
                organization_id="org-1",
                user_id="user-1",
                organization_role=OrganizationRole.MEMBER,
            ):
                pass

    assert exc.value.max_concurrent == 1
    assert exc.value.user_id == "user-1"
    assert exc.value.waited_seconds >= 0.02
    assert limiter.waiting(organization_id="org-1", user_id="user-1") == 0


@pytest.mark.asyncio
async def test_recall_limiter_queues_a_burst_within_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three recalls from one agent against a bound of one all complete, one at a time."""

    monkeypatch.delenv(RECALL_MAX_CONCURRENT_ENV, raising=False)
    limiter = RecallConcurrencyLimiter(default_max_concurrent=1, queue_timeout_seconds=5.0)
    peak = 0
    finished: list[int] = []

    async def recall(index: int) -> None:
        nonlocal peak
        async with limiter.slot(
            organization_id="org-1", user_id="user-1", organization_role=OrganizationRole.MEMBER
        ):
            peak = max(peak, limiter.active(organization_id="org-1", user_id="user-1"))
            await asyncio.sleep(0.01)
            finished.append(index)

    await asyncio.gather(*(recall(index) for index in range(3)))

    assert sorted(finished) == [0, 1, 2]
    assert peak == 1
    assert limiter.active(organization_id="org-1", user_id="user-1") == 0
    assert limiter.waiting(organization_id="org-1", user_id="user-1") == 0


@pytest.mark.asyncio
async def test_recall_limiter_queue_timeout_follows_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(RECALL_MAX_CONCURRENT_ENV, raising=False)
    monkeypatch.setenv(RECALL_QUEUE_TIMEOUT_ENV, "0.02")
    limiter = RecallConcurrencyLimiter(default_max_concurrent=1)

    async with limiter.slot(
        organization_id="org-1", user_id="user-1", organization_role=OrganizationRole.MEMBER
    ):
        with pytest.raises(RecallConcurrencyLimitExceededError):
            async with limiter.slot(
                organization_id="org-1",
                user_id="user-1",
                organization_role=OrganizationRole.MEMBER,
            ):
                pass


@pytest.mark.asyncio
async def test_recall_limiter_releases_member_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RECALL_MAX_CONCURRENT_ENV, raising=False)
    limiter = RecallConcurrencyLimiter(default_max_concurrent=1)

    async with limiter.slot(
        organization_id="org-1",
        user_id="user-1",
        organization_role=OrganizationRole.MEMBER,
    ):
        pass

    async with limiter.slot(
        organization_id="org-1",
        user_id="user-1",
        organization_role=OrganizationRole.MEMBER,
    ):
        pass


@pytest.mark.asyncio
async def test_recall_limiter_bypasses_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RECALL_MAX_CONCURRENT_ENV, raising=False)
    limiter = RecallConcurrencyLimiter(default_max_concurrent=1)

    async with (
        limiter.slot(
            organization_id="org-1",
            user_id="user-1",
            organization_role=OrganizationRole.OWNER,
        ),
        limiter.slot(
            organization_id="org-1",
            user_id="user-1",
            organization_role=OrganizationRole.OWNER,
        ),
    ):
        pass


@pytest.mark.asyncio
async def test_recall_limiter_uses_environment_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RECALL_MAX_CONCURRENT_ENV, "2")
    limiter = RecallConcurrencyLimiter(default_max_concurrent=1, queue_timeout_seconds=0.02)

    async with (
        limiter.slot(
            organization_id="org-1",
            user_id="user-1",
            organization_role=OrganizationRole.MEMBER,
        ),
        limiter.slot(
            organization_id="org-1",
            user_id="user-1",
            organization_role=OrganizationRole.MEMBER,
        ),
    ):
        with pytest.raises(RecallConcurrencyLimitExceededError):
            async with limiter.slot(
                organization_id="org-1",
                user_id="user-1",
                organization_role=OrganizationRole.MEMBER,
            ):
                pass
