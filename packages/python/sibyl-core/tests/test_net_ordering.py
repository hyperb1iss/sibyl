"""The feed's order holds at nanosecond resolution, whatever digits a timestamp ends in.

The engine prints a datetime with its trailing zeros dropped, so two instants
inside one microsecond can print as ``...10.277579Z`` and ``...10.277579032Z``.
Read as text the first sorts after the second (``Z`` sorts after ``0``) though
it is 32 nanoseconds earlier. The feed orders, pages and resumes on these
instants, so every ordering here must be the instants' own order, for every
filter a reader can apply. Messages land on such timestamps whenever two posts
commit within one microsecond, which concurrent writers do; two can also share
one exact timestamp. Runs on the embedded engine and, with
SIBYL_LIVE_SURREAL_TESTS=1, on a live server.
"""

from __future__ import annotations

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.services.net import read_net_feed
from tests.test_net import (
    ORG,
    post,
)
from tests.test_net import (
    live_namespace as live_namespace,
)
from tests.test_net import (
    store as store,
)

# One microsecond, written every way the engine prints it, plus its neighbours
# and two instants that two messages share exactly.
INSTANTS = (
    "2026-11-02T10:00:00.277578999Z",
    "2026-11-02T10:00:00.277579Z",
    "2026-11-02T10:00:00.277579001Z",
    "2026-11-02T10:00:00.27757903Z",
    "2026-11-02T10:00:00.277579032Z",
    "2026-11-02T10:00:00.2775791Z",
    "2026-11-02T10:00:00.27758Z",
    "2026-11-02T10:00:00.3Z",
    "2026-11-02T10:00:00.3Z",
    "2026-11-02T10:00:00.655201457Z",
    "2026-11-02T10:00:00.655201457Z",
    "2026-11-02T10:00:01Z",
)
CHANNELS = ("#a", "#b")
THREAD = "t1"
# What a reader can filter on: nothing, one channel, several, a thread, both.
FILTERS = {
    "everything": {},
    "one-channel": {"channels": ["#a"]},
    "two-channels": {"channels": ["#a", "#b"]},
    "thread": {"thread": THREAD},
    "channel-and-thread": {"channels": ["#b"], "thread": THREAD},
}


async def place(store: SurrealContentClient) -> list[tuple[str, str, str]]:
    """One message at each instant, alternating channels, posted in reverse.

    Returns ``(instant, channel, id)`` in the instants' true order; messages
    that share an instant are ordered by id, as the feed orders them.
    """
    placed: list[tuple[str, str, str]] = []
    for index, instant in reversed(list(enumerate(INSTANTS))):
        channel = CHANNELS[index % 2]
        message = await post(channel, body=instant, thread=THREAD)
        await store.execute_query(
            "UPDATE net_messages SET created_at = <datetime>$at WHERE uuid = $uuid;",
            at=instant,
            uuid=message.id,
        )
        placed.append((instant, channel, message.id))
    position = {instant: index for index, instant in enumerate(INSTANTS)}
    return sorted(placed, key=lambda row: (position[row[0]], row[2]))


def visible(placed: list[tuple[str, str, str]], name: str) -> list[str]:
    channels = FILTERS[name].get("channels")
    return [row[2] for row in placed if channels is None or row[1] in channels]


def labels(placed: list[tuple[str, str, str]], ids: list[str]) -> list[str]:
    """Each message as its instant's seconds and fraction plus a short id, for diffs."""
    names = {row[2]: f"{row[0][17:]}/{row[2][:4]}" for row in placed}
    return [names.get(message_id, message_id) for message_id in ids]


@pytest.mark.parametrize("name", FILTERS)
async def test_a_tail_read_returns_the_instants_in_time_order(
    store: SurrealContentClient, name: str
) -> None:
    placed = await place(store)

    page = await read_net_feed(organization_id=ORG, since=None, limit=50, **FILTERS[name])

    assert labels(placed, [m.id for m in page.messages]) == labels(placed, visible(placed, name))


@pytest.mark.parametrize("page_size", [1, 2, 3])
@pytest.mark.parametrize("name", FILTERS)
async def test_paging_from_the_first_message_neither_skips_nor_repeats(
    store: SurrealContentClient, name: str, page_size: int
) -> None:
    placed = await place(store)
    expected = visible(placed, name)
    first = await read_net_feed(organization_id=ORG, since=None, limit=50, **FILTERS[name])
    start = next(m for m in first.messages if m.id == expected[0])

    seen = [start.id]
    cursor = start.cursor
    for _ in range(len(INSTANTS) * 2):
        page = await read_net_feed(
            organization_id=ORG, since=cursor, limit=page_size, **FILTERS[name]
        )
        if not page.messages:
            break
        seen.extend(m.id for m in page.messages)
        cursor = page.next_since

    assert labels(placed, seen) == labels(placed, expected)


@pytest.mark.parametrize("name", FILTERS)
async def test_reading_after_any_one_message_returns_exactly_the_later_ones(
    store: SurrealContentClient, name: str
) -> None:
    placed = await place(store)
    expected = visible(placed, name)
    everything = {
        m.id: m for m in (await read_net_feed(organization_id=ORG, since=None, limit=50)).messages
    }

    wrong = {}
    for index, message_id in enumerate(expected):
        after = await read_net_feed(
            organization_id=ORG, since=everything[message_id].cursor, limit=50, **FILTERS[name]
        )
        got = [m.id for m in after.messages]
        if got != expected[index + 1 :]:
            wrong[labels(placed, [message_id])[0]] = labels(placed, got)

    assert wrong == {}
