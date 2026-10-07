"""Per-organization graph generations and write-path invalidation.

Reader caches hold validated views of an organization's graph. Every path
that can change a graph row or its protected evidence bumps the organization's
generation: the graph client after any statement that writes, a memory
correction, and an entity event arriving from another process. A cache keeps
an entry only while the generation it was built under is current.

This module is a leaf so the client, the services and the API can all import
it without a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

_generations: dict[str, int] = {}
_listeners: list[Callable[[str], None]] = []

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


def invalidate_graph_caches(organization_id: str) -> None:
    """Record that this organization's graph may have changed."""
    _generations[organization_id] = _generations.get(organization_id, 0) + 1
    for listener in list(_listeners):
        listener(organization_id)


def register_invalidation_listener(listener: Callable[[str], None]) -> None:
    """Have a cache drop its entries for an organization whenever it is invalidated."""
    if listener not in _listeners:
        _listeners.append(listener)


def query_mutates_graph(query: str, *, label: str | None = None) -> bool:
    """Whether a statement can change what readers see; false positives cost a rebuild."""
    if label is not None and label in BOOKKEEPING_QUERY_LABELS:
        return False
    return _MUTATION_TOKENS.search(query) is not None
