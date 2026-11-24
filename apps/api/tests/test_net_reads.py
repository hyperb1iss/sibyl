"""Shared feed reads: one query per question, and every reader gets its own exact page."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from uuid import UUID

import pytest

from sibyl.api.net_reads import FeedQuestion, SharedFeedReads, page_after
from sibyl_core.services.net import NetCursor, NetFeedPage, NetMessageRecord

QUESTION: FeedQuestion = ("org-a", frozenset({"#reviews"}), None)


def message(created_ns: int, number: int, author: str = "ada") -> NetMessageRecord:
    uuid = str(UUID(int=number))
    return NetMessageRecord(
        id=uuid,
        organization_id="org-a",
        channel="#reviews",
        thread="",
        kind="message",
        body=f"message {number}",
        project_id=None,
        author_user_id=author,
        api_key_id=None,
        author_agent=None,
        author_session=None,
        decision_memory_id=None,
        created_at=datetime.fromtimestamp(created_ns / 1e9, UTC),
        cursor=NetCursor(created_ns=created_ns, uuid=uuid),
    )


def individual_read(rows: list[NetMessageRecord], since: NetCursor, limit: int) -> NetFeedPage:
    """What the feed returns for one reader: the settled rows after its cursor."""
    after = [
        row
        for row in rows
        if (row.cursor.created_ns, row.cursor.uuid) > (since.created_ns, since.uuid)
    ]
    messages = after[:limit]
    return NetFeedPage(
        messages=messages,
        next_since=messages[-1].cursor if messages else since,
        more=len(after) > limit,
    )


class FakeFeed:
    """An ordered feed the shared reader reads through, counting its queries."""

    def __init__(self, rows: Iterable[NetMessageRecord]) -> None:
        self.rows = sorted(rows, key=lambda row: (row.cursor.created_ns, row.cursor.uuid))
        self.reads: list[tuple[NetCursor, int]] = []
        self.author_lookups: list[list[str]] = []
        self.gate: asyncio.Event | None = None
        self.fail: Exception | None = None

    async def read_page(self, question: FeedQuestion, since: NetCursor, limit: int) -> NetFeedPage:
        assert question == QUESTION
        self.reads.append((since, limit))
        # The read sees the feed as it stood when the query started.
        rows = list(self.rows)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return individual_read(rows, since, limit)

    async def authors(self, user_ids: Iterable[str]) -> Mapping[str, str]:
        ids = list(user_ids)
        self.author_lookups.append(ids)
        return {user_id: user_id.title() for user_id in ids}


def cursor_of(rows: list[NetMessageRecord], index: int) -> NetCursor:
    return rows[index].cursor if index >= 0 else NetCursor(created_ns=0, uuid="")


def test_a_page_cut_from_a_shared_read_is_the_page_the_reader_would_read() -> None:
    rng = random.Random(20261103)  # noqa: S311 - a reproducible test case, not a secret
    for _ in range(3000):
        # Few distinct instants, so many rows share one and ties on the id decide.
        rows = sorted(
            (message(rng.randrange(1, 8), number) for number in range(rng.randrange(0, 25))),
            key=lambda row: (row.cursor.created_ns, row.cursor.uuid),
        )
        oldest = rng.randrange(-1, len(rows))
        shared_limit = rng.randrange(1, 12)
        shared = individual_read(rows, cursor_of(rows, oldest), shared_limit)
        reader = rng.randrange(oldest, len(rows))
        since = cursor_of(rows, reader)
        if rng.random() < 0.2:
            # A cursor minted at an instant rather than at a message.
            since = NetCursor(created_ns=rng.randrange(0, 9), uuid="")
            if (since.created_ns, since.uuid) < (
                cursor_of(rows, oldest).created_ns,
                cursor_of(rows, oldest).uuid,
            ):
                continue
        limit = rng.randrange(1, shared_limit + 1)

        cut = page_after(shared, since, limit)

        if cut is not None:
            assert cut == individual_read(rows, since, limit)
        else:
            # Refused only when the shared read came back full.
            assert shared.more
            assert len(shared.messages) == shared_limit


async def test_concurrent_readers_of_one_question_share_one_read_and_one_lookup() -> None:
    rows = [message(10, number, author="ada" if number % 2 else "grace") for number in range(6)]
    feed = FakeFeed(rows)
    reads = SharedFeedReads(feed.read_page, feed.authors)
    start = NetCursor(created_ns=0, uuid="")

    answers = await asyncio.gather(*(reads.read(QUESTION, start, 100) for _ in range(25)))

    assert feed.reads == [(start, 100)]
    assert feed.author_lookups == [["grace", "ada"]]
    for page, authors in answers:
        assert page == individual_read(feed.rows, start, 100)
        assert authors == {"grace": "Grace", "ada": "Ada"}
    assert reads.in_flight() == 0


async def test_each_reader_keeps_only_the_rows_after_its_own_cursor() -> None:
    rows = [message(10 + number, number) for number in range(10)]
    feed = FakeFeed(rows)
    reads = SharedFeedReads(feed.read_page, feed.authors)
    asks = [(cursor_of(feed.rows, index), limit) for index, limit in ((-1, 3), (4, 2), (8, 10))]

    answers = await asyncio.gather(*(reads.read(QUESTION, since, limit) for since, limit in asks))

    # One read, from the oldest cursor, as large as the largest page.
    assert feed.reads == [(cursor_of(feed.rows, -1), 10)]
    for (since, limit), (page, _) in zip(asks, answers, strict=True):
        assert page == individual_read(feed.rows, since, limit)


async def test_a_reader_the_shared_read_cannot_answer_reads_on_its_own() -> None:
    rows = [message(10 + number, number) for number in range(20)]
    feed = FakeFeed(rows)
    reads = SharedFeedReads(feed.read_page, feed.authors)
    lagging, current = cursor_of(feed.rows, -1), cursor_of(feed.rows, 15)

    (behind, _), (ahead, ahead_authors) = await asyncio.gather(
        reads.read(QUESTION, lagging, 5), reads.read(QUESTION, current, 5)
    )

    assert behind == individual_read(feed.rows, lagging, 5)
    assert ahead == individual_read(feed.rows, current, 5)
    assert feed.reads == [(lagging, 5), (current, 5)]
    assert ahead_authors == {"ada": "Ada"}


async def test_a_reader_arriving_mid_read_waits_for_a_fresh_one() -> None:
    feed = FakeFeed([message(10, 1)])
    reads = SharedFeedReads(feed.read_page, feed.authors)
    start = NetCursor(created_ns=0, uuid="")
    feed.gate = asyncio.Event()

    first = asyncio.create_task(reads.read(QUESTION, start, 10))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(feed.reads) == 1
    # Written after the first read started, so that read must not answer for it.
    feed.rows.append(message(11, 2))
    late = [asyncio.create_task(reads.read(QUESTION, start, 10)) for _ in range(5)]
    await asyncio.sleep(0)
    assert len(feed.reads) == 1
    feed.gate.set()

    first_page, _ = await first
    late_pages = [page for page, _ in await asyncio.gather(*late)]

    assert [row.body for row in first_page.messages] == ["message 1"]
    for page in late_pages:
        assert [row.body for row in page.messages] == ["message 1", "message 2"]
    assert len(feed.reads) == 2


async def test_a_failed_read_fails_its_readers_and_the_next_read_starts_clean() -> None:
    feed = FakeFeed([message(10, 1)])
    reads = SharedFeedReads(feed.read_page, feed.authors)
    start = NetCursor(created_ns=0, uuid="")
    feed.fail = TimeoutError("content pool exhausted")

    results = await asyncio.gather(
        *(reads.read(QUESTION, start, 10) for _ in range(3)), return_exceptions=True
    )

    assert all(isinstance(result, TimeoutError) for result in results)
    assert reads.in_flight() == 0
    feed.fail = None
    page, _ = await reads.read(QUESTION, start, 10)
    assert [row.body for row in page.messages] == ["message 1"]


async def test_a_reader_that_goes_away_does_not_disturb_the_others() -> None:
    feed = FakeFeed([message(10, 1)])
    reads = SharedFeedReads(feed.read_page, feed.authors)
    start = NetCursor(created_ns=0, uuid="")
    feed.gate = asyncio.Event()

    gone = asyncio.create_task(reads.read(QUESTION, start, 10))
    staying = asyncio.create_task(reads.read(QUESTION, start, 10))
    await asyncio.sleep(0)
    gone.cancel()
    await asyncio.sleep(0)
    feed.gate.set()

    page, _ = await staying
    assert [row.body for row in page.messages] == ["message 1"]
    with pytest.raises(asyncio.CancelledError):
        await gone
    assert reads.in_flight() == 0


async def test_different_questions_do_not_share() -> None:
    seen: list[FeedQuestion] = []

    async def read_page(question: FeedQuestion, since: NetCursor, limit: int) -> NetFeedPage:
        seen.append(question)
        return NetFeedPage(messages=[], next_since=since, more=False)

    async def authors(user_ids: Iterable[str]) -> Mapping[str, str]:
        return {}

    reads = SharedFeedReads(read_page, authors)
    start = NetCursor(created_ns=0, uuid="")
    questions: list[FeedQuestion] = [
        ("org-a", frozenset({"#reviews"}), None),
        ("org-a", frozenset({"#reviews", "#builds"}), None),
        ("org-a", frozenset({"#reviews"}), "gh:acme/app#42"),
        ("org-b", frozenset({"#reviews"}), None),
    ]

    await asyncio.gather(*(reads.read(question, start, 10) for question in questions))

    assert sorted(seen, key=repr) == sorted(questions, key=repr)
