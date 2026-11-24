"""Share one feed read among concurrent sibylnet readers asking the same question.

A post wakes every long-poll waiter on its channel at the same instant, and
each used to read the feed and look up its authors on its own, so one message
cost two queries per waiter, all queued on the same pools at once. Readers in
this process asking the same question (organization, channel set, thread)
now join one read: it starts from the oldest of their cursors, and each
reader keeps the rows after its own cursor, which is the page it would have
read itself at that moment. One author lookup covers every page the read
answered.

A reader only joins a read that has not started, so what it gets was read
after it asked and nothing it was woken for can be missing. While a read is
running, readers that arrive queue for the next one, so a question has at
most one read in flight per process. A reader the shared read cannot answer
exactly, because rows before its cursor filled the read, reads on its own.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field

from sibyl_core.services.net import NetCursor, NetFeedPage, NetMessageRecord

type FeedQuestion = tuple[str, frozenset[str] | None, str | None]
"""Organization, channels (None for all) and thread (None for any)."""


def _position(cursor: NetCursor) -> tuple[int, str]:
    # The feed orders by created_ns, then by uuid as text; so does this.
    return (cursor.created_ns, cursor.uuid)


def page_after(shared: NetFeedPage, since: NetCursor, limit: int) -> NetFeedPage | None:
    """The page a read after ``since`` would return, cut from a read that started earlier.

    ``shared`` holds the settled rows after an earlier or equal cursor, in
    feed order, and whether more followed. The rows after ``since`` are a
    suffix of it. They answer exactly unless the shared read filled up
    before holding ``limit`` of them; then this returns None.
    """
    start = _position(since)
    after = [message for message in shared.messages if _position(message.cursor) > start]
    if shared.more and len(after) < limit:
        return None
    messages = after[:limit]
    return NetFeedPage(
        messages=messages,
        next_since=messages[-1].cursor if messages else since,
        # Rows past the shared read all follow ``since`` too.
        more=shared.more or len(after) > limit,
    )


@dataclass(eq=False, slots=True)
class _Ask[A]:
    since: NetCursor
    limit: int
    answer: asyncio.Future[tuple[NetFeedPage, Mapping[str, A]] | None]


@dataclass(eq=False, slots=True)
class _Flight[A]:
    asks: list[_Ask[A]] = field(default_factory=list)
    drain: asyncio.Task[None] | None = None


class SharedFeedReads[A]:
    """This process's feed reads, one in flight per question."""

    def __init__(
        self,
        read_page: Callable[[FeedQuestion, NetCursor, int], Awaitable[NetFeedPage]],
        authors: Callable[[Iterable[str]], Awaitable[Mapping[str, A]]],
    ) -> None:
        self._read_page = read_page
        self._authors = authors
        self._flights: dict[FeedQuestion, _Flight[A]] = {}

    async def read(
        self, question: FeedQuestion, since: NetCursor, limit: int
    ) -> tuple[NetFeedPage, Mapping[str, A]]:
        """The page after ``since``, with its authors, read alongside any same-question readers."""
        if limit <= 0:
            msg = "a shared read needs a positive limit"
            raise ValueError(msg)
        flight = self._flights.get(question)
        if flight is None:
            flight = self._flights[question] = _Flight()
        ask: _Ask[A] = _Ask(since, limit, asyncio.get_running_loop().create_future())
        flight.asks.append(ask)
        if flight.drain is None:
            # The read serves every reader in the batch, so it runs in no
            # one request's context. It starts on the next turn of the loop,
            # after every reader woken by the same timer has joined.
            flight.drain = asyncio.create_task(
                self._drain(question, flight), context=contextvars.Context()
            )
        answer = await ask.answer
        if answer is not None:
            return answer
        page = await self._read_page(question, since, limit)
        return page, await self._authors(_author_ids(page.messages))

    def in_flight(self) -> int:
        return len(self._flights)

    async def _drain(self, question: FeedQuestion, flight: _Flight[A]) -> None:
        try:
            while flight.asks:
                asks = [ask for ask in flight.asks if not ask.answer.done()]
                flight.asks = []
                if asks:
                    await self._answer(question, asks)
        finally:
            for ask in flight.asks:
                ask.answer.cancel()
            if self._flights.get(question) is flight:
                del self._flights[question]

    async def _answer(self, question: FeedQuestion, asks: list[_Ask[A]]) -> None:
        try:
            since = min((ask.since for ask in asks), key=_position)
            shared = await self._read_page(question, since, max(ask.limit for ask in asks))
            pages = [page_after(shared, ask.since, ask.limit) for ask in asks]
            authors = await self._authors(
                _author_ids(message for page in pages if page for message in page.messages)
            )
        except BaseException as exc:
            for ask in asks:
                if ask.answer.done():
                    continue
                if isinstance(exc, Exception):
                    ask.answer.set_exception(exc)
                else:
                    ask.answer.cancel()
            if not isinstance(exc, Exception):
                raise
            return
        for ask, page in zip(asks, pages, strict=True):
            if not ask.answer.done():
                ask.answer.set_result(None if page is None else (page, authors))


def _author_ids(messages: Iterable[NetMessageRecord]) -> list[str]:
    return list(dict.fromkeys(message.author_user_id for message in messages))


__all__ = ["FeedQuestion", "SharedFeedReads", "page_after"]
