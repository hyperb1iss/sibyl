"""Per-organization graph generations and write-path invalidation.

Reader caches hold validated views of an organization's graph. Every path
that can change a graph row or its protected evidence bumps the organization's
generation: the graph client after any statement that writes, a memory
correction, and an entity event arriving from another process. A cache keeps
an entry only while the generation it was built under is current.

A structural write also marks its organization as pending announcement. The
host flushes that mark through the installed graph-update announcer (the API
event bus) when a background lease is released and when a worker job ends, so
a write in one process retires the caches of every other process. Events
that arrive from the bus retire caches without marking anything, so an
announcement never announces itself.

Windows that announcement does not cover, where a cache lives until its TTL:
the schema lease control client and an archive restore's native socket write
through connections that are not an organization's graph client; sibyld
migrate and db subcommands run in processes with no bus; and the local
coordination backend only delivers inside its own process, so a separate
worker on that backend cannot reach the API's caches.

This module is a leaf so the client, the services and the API can all import
it without a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

_generations: dict[str, int] = {}
_listeners: list[Callable[[str], None]] = []
_pending_announcements: set[str] = set()

# Statements that can change a row: the row-changing subset of the connection
# layer's _WRITE_QUERY_TOKENS. Stored functions (FN) can write when invoked
# from a SELECT or RETURN, IMPORT and REMOVE replace or drop whole tables.
# Schema definitions (DEFINE, ALTER, REBUILD) and transaction brackets change
# no row by themselves and run on every runtime acquisition, so they never
# bump a generation. A keyword inside a literal only costs one rebuild.
_MUTATION_TOKENS: Final = re.compile(
    r"\b(?:CREATE|UPDATE|UPSERT|DELETE|INSERT|RELATE|FN|IMPORT|REMOVE)\b", re.IGNORECASE
)

# The bookkeeping contract. A statement sent with one of these as its
# ``_query_label`` touches usage and activity bookkeeping only: the recall and
# citation stamps (last_recalled_at, last_used_at, retrieval_count,
# citation_count, misled_count) and the project activity counters
# (last_activity_at, total_tasks, completed_tasks, in_progress_tasks, with
# the updated_at they carry). None of it changes what a graph reader renders
# or proves, and every search, context pack and recall writes it, so the
# write seam leaves the caches alone for these. Anything structural (create,
# delete, relate, status, visibility, attributes) ships without a label or
# with any other label and bumps the generation. Adding a label here is a
# claim that its statement can never change a rendered or proven fact.
BOOKKEEPING_QUERY_LABELS: Final = frozenset({"usage.graph_stamp", "entity.bookkeeping"})


def graph_generation(organization_id: str) -> int:
    """Current generation of an organization's graph; a fresh org starts at 0."""
    return _generations.get(organization_id, 0)


def invalidate_graph_caches(organization_id: str, *, announce: bool = True) -> None:
    """Record that this organization's graph may have changed.

    A local structural write announces itself to other processes at the next
    flush; a retirement caused by an announcement passes announce=False.
    """
    _generations[organization_id] = _generations.get(organization_id, 0) + 1
    for listener in list(_listeners):
        listener(organization_id)
    if announce:
        _pending_announcements.add(organization_id)


def pending_graph_updates() -> frozenset[str]:
    """Organizations with structural writes not yet announced to other processes."""
    return frozenset(_pending_announcements)


async def announce_graph_updates(organization_id: str | None = None) -> frozenset[str]:
    """Flush pending announcements through the installed announcer.

    Returns the organizations announced. Without an announcer (a process with
    no bus) the marks are left in place for a host that has one.
    """
    from sibyl_core.runtime_ports import get_graph_update_announcer

    announcer = get_graph_update_announcer()
    if announcer is None:
        return frozenset()
    due = (
        {organization_id} & _pending_announcements
        if organization_id is not None
        else set(_pending_announcements)
    )
    for pending in due:
        _pending_announcements.discard(pending)
        await announcer(pending)
    return frozenset(due)


def register_invalidation_listener(listener: Callable[[str], None]) -> None:
    """Have a cache drop its entries for an organization whenever it is invalidated."""
    if listener not in _listeners:
        _listeners.append(listener)


def query_mutates_graph(query: str, *, label: str | None = None) -> bool:
    """Whether a statement can change what readers see; false positives cost a rebuild."""
    if label is not None and label in BOOKKEEPING_QUERY_LABELS:
        return False
    return _MUTATION_TOKENS.search(query) is not None
