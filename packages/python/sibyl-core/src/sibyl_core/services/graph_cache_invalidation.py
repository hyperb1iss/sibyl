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

# Statements that can change a row. Schema statements (DEFINE, REMOVE) run on
# every runtime acquisition and change no row, so they never bump a
# generation. A keyword inside a literal only costs one cache rebuild.
_MUTATION_TOKENS: Final = re.compile(
    r"\b(?:CREATE|UPDATE|UPSERT|DELETE|INSERT|RELATE)\b", re.IGNORECASE
)


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


def query_mutates_graph(query: str) -> bool:
    """Whether a statement can change rows; false positives only cost a rebuild."""
    return _MUTATION_TOKENS.search(query) is not None
