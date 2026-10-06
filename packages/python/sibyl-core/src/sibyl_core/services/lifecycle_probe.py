"""Pending-work probes for the scheduled lifecycle repair.

The repair tick visits every organization every minute. Visiting one with
nothing to do used to cost about 25 queries and a fresh graph pool: two
schema probes, a dimension lookup, three plane state reads, two keyset scans
and the raw repair's lease dance. These probes ask "is anything owed?" first.
The graph side is one multi-statement round trip on the organization's own
namespace; the content side is one round trip per tick for every
organization at once. Each statement runs through an index, so an idle
organization costs the tick one query. Only an organization that owes
something gets the full pass.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_embedding_states import (
    GRAPH_EMBEDDING_STATE_PLANE,
    embedding_state_key,
)
from sibyl_core.embeddings.provenance import vector_identity_differs_predicate
from sibyl_core.projection.repair import PENDING_REPAIR_QUERY
from sibyl_core.services.embedding_sweep import STATE_PROJECTION, plane_current

type EmbeddingStamp = Mapping[str, Any]

GRAPH_STATE_STATEMENT = f"SELECT {STATE_PROJECTION} FROM type::record($state_key);"
# One pending lifecycle row sends the organization through the full pass.
LIFECYCLE_PENDING_STATEMENT = PENDING_REPAIR_QUERY + ";"
# The rows ``repair_promoted_embeddings`` would hand to the queue: reflection
# candidates whose vector is missing or from another model, decided on the
# server rather than after reading every candidate page by page.
EMBEDDING_PENDING_STATEMENT = (
    "SELECT uuid FROM entity WITH INDEX idx_entity_reflection_candidate_uuid "
    "WHERE group_id = $group_id AND uuid > '' AND derivation_required = true "
    "AND attributes.reflection_identity.purpose = 'candidate' "
    "AND attributes.operational_source_id IS NONE "
    "AND (name_embedding = NONE OR "
    + vector_identity_differs_predicate("attributes.embedding_metadata", "stamp")
    + ") LIMIT 1;"
)
# Spelled CONTAINS rather than IN: the embedded test engine drops IN matches
# that an index answers (see ``REOPEN_EMBEDDING_STATES``).
CONTENT_STATES_STATEMENT = (
    "SELECT organization_id, plane, complete_metadata, IF complete_at = NONE THEN NONE "
    "ELSE duration::secs(time::now() - complete_at) END AS complete_age_seconds "
    "FROM embedding_states WHERE $planes CONTAINS plane;"
)
# Served by the index whose leading column is the pending flag, so the
# statement reads only the rows that are pending, however many captures the
# table holds.
VALIDATION_PENDING_STATEMENT = (
    "SELECT organization_id FROM raw_captures WITH INDEX idx_raw_captures_validation_pending "
    "WHERE metadata.source_validation_pending = true GROUP BY organization_id;"
)


@dataclass(frozen=True, slots=True)
class GraphWork:
    """What one organization's graph namespace still owes the lifecycle pass."""

    sweep_current: bool
    lifecycle_pending: bool
    embeddings_pending: bool

    @property
    def idle(self) -> bool:
        return self.sweep_current and not self.lifecycle_pending and not self.embeddings_pending


async def probe_graph_work(
    client: Any, *, graph_stamp: EmbeddingStamp | None, verify_interval: float
) -> GraphWork:
    """One round trip: the sweep state, one pending lifecycle row, one candidate owed a vector.

    ``graph_stamp`` is what the configured graph provider writes today; with
    none configured there is no sweep and no vector to owe, so only the
    lifecycle row is asked for.
    """
    group_id = str(client.group_id)
    params: dict[str, Any] = {"group_id": group_id, "cursor": "", "limit": 1}
    if graph_stamp is None:
        statements = [LIFECYCLE_PENDING_STATEMENT]
    else:
        statements = [
            GRAPH_STATE_STATEMENT,
            LIFECYCLE_PENDING_STATEMENT,
            EMBEDDING_PENDING_STATEMENT,
        ]
        params["state_key"] = embedding_state_key(group_id, GRAPH_EMBEDDING_STATE_PLANE)
        params["stamp"] = dict(graph_stamp)
    results = _statement_results(
        await client.execute_query_batch(" ".join(statements), **params), len(statements)
    )
    if graph_stamp is None:
        return GraphWork(
            sweep_current=True,
            lifecycle_pending=bool(normalize_records(results[0])),
            embeddings_pending=False,
        )
    states = normalize_records(results[0])
    return GraphWork(
        sweep_current=bool(states) and plane_current(states[0], dict(graph_stamp), verify_interval),
        lifecycle_pending=bool(normalize_records(results[1])),
        embeddings_pending=bool(normalize_records(results[2])),
    )


@dataclass(frozen=True, slots=True)
class ContentWork:
    """What the shared content namespace says each organization still owes."""

    current_planes: frozenset[tuple[str, str]]
    validation_pending: frozenset[str]

    def idle(self, organization_id: str, *, planes: Iterable[str]) -> bool:
        """Whether the organization owes nothing on the content side.

        ``planes`` names the content planes with a configured embedder; each
        must be current. Source validation must have nothing pending.
        """
        if organization_id in self.validation_pending:
            return False
        return all((organization_id, plane) in self.current_planes for plane in planes)


async def probe_content_work(
    client: Any, *, stamps: Mapping[str, EmbeddingStamp], verify_interval: float
) -> ContentWork:
    """One round trip for every organization: current content planes and pending source checks.

    ``stamps`` maps each content plane to the stamp its writes carry today;
    a plane without a configured embedder is left out and never counts as
    owed.
    """
    planes = list(stamps)
    statements = [VALIDATION_PENDING_STATEMENT]
    params: dict[str, Any] = {}
    if planes:
        statements.insert(0, CONTENT_STATES_STATEMENT)
        params["planes"] = planes
    results = _statement_results(
        await client.execute_query_batch(" ".join(statements), **params), len(statements)
    )
    current: set[tuple[str, str]] = set()
    if planes:
        for row in normalize_records(results[0]):
            plane = str(row.get("plane") or "")
            stamp = stamps.get(plane)
            if stamp is not None and plane_current(row, dict(stamp), verify_interval):
                current.add((str(row.get("organization_id") or ""), plane))
    pending = {
        str(row["organization_id"])
        for row in normalize_records(results[-1])
        if row.get("organization_id")
    }
    return ContentWork(current_planes=frozenset(current), validation_pending=frozenset(pending))


def _statement_results(response: object, expected: int) -> list[object]:
    if not isinstance(response, list) or len(response) != expected:
        raise RuntimeError(
            f"lifecycle probe expected {expected} statement results, got {type(response).__name__}"
        )
    return list(response)


__all__ = [
    "CONTENT_STATES_STATEMENT",
    "EMBEDDING_PENDING_STATEMENT",
    "GRAPH_STATE_STATEMENT",
    "LIFECYCLE_PENDING_STATEMENT",
    "VALIDATION_PENDING_STATEMENT",
    "ContentWork",
    "GraphWork",
    "probe_content_work",
    "probe_graph_work",
]
