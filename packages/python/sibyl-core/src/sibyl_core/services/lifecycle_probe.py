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

A plane is idle only when nothing about it can change without a write:
its last pass finished for the configured stamp within the verify
interval, its verdict is final rather than provisional (a provisional
adoption is weighed again every pass), and, for the raw capture plane, its
last receipt set no capture aside (a deferred or refused capture is
counted on every pass and retried once its wait ends).
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
from sibyl_core.services.content_raw_embedding_repair import (
    RAW_CAPTURE_EMBEDDING_PLANE,
    raw_plane_current,
)
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
    "SELECT organization_id, plane, complete_metadata, legacy_decision, legacy_warning, "
    "legacy_notice, legacy_provisional, last_run.pass_deferred AS pass_deferred, "
    "last_run.pass_refused AS pass_refused, IF complete_at = NONE THEN NONE "
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
class PlaneFacts:
    """What one plane's state row says: whether it is idle, and the verdict it reports."""

    current: bool
    legacy_decision: str | None = None
    warning: str | None = None
    notice: str | None = None


def plane_facts(
    state: Mapping[str, Any], *, plane: str, stamp: EmbeddingStamp, verify_interval: float
) -> PlaneFacts:
    """Judge a plane state row the way its own pass would, without running it."""
    if plane == RAW_CAPTURE_EMBEDDING_PLANE:
        current = raw_plane_current(state, stamp, verify_interval)
    else:
        # A provisional adoption is weighed again by every settle, so the pass
        # that does the weighing must run.
        current = plane_current(state, dict(stamp), verify_interval) and not state.get(
            "legacy_provisional"
        )
    return PlaneFacts(
        current=current,
        legacy_decision=_optional_str(state.get("legacy_decision")),
        warning=_optional_str(state.get("legacy_warning")),
        notice=_optional_str(state.get("legacy_notice")),
    )


@dataclass(frozen=True, slots=True)
class GraphWork:
    """What one organization's graph namespace still owes the lifecycle pass.

    ``sweep`` is None when no graph provider is configured: there is no
    sweep to owe.
    """

    sweep: PlaneFacts | None
    lifecycle_pending: bool
    embeddings_pending: bool

    @property
    def idle(self) -> bool:
        sweep_current = self.sweep is None or self.sweep.current
        return sweep_current and not self.lifecycle_pending and not self.embeddings_pending


async def probe_graph_work(
    client: Any,
    *,
    graph_stamp: EmbeddingStamp | None,
    embedding_stamp: EmbeddingStamp | None,
    verify_interval: float,
) -> GraphWork:
    """One round trip: the sweep state, one pending lifecycle row, one candidate owed a vector.

    ``graph_stamp`` is what the graph sweep's provider writes today; with
    none there is no sweep to owe. ``embedding_stamp`` is what the
    promoted-embedding repair's provider writes, asked for on its own
    because that repair resolves its provider itself; with none it would
    enqueue nothing. The lifecycle row is always asked for.
    """
    group_id = str(client.group_id)
    params: dict[str, Any] = {"group_id": group_id, "cursor": "", "limit": 1}
    statements: list[str] = []
    if graph_stamp is not None:
        statements.append(GRAPH_STATE_STATEMENT)
        params["state_key"] = embedding_state_key(group_id, GRAPH_EMBEDDING_STATE_PLANE)
    statements.append(LIFECYCLE_PENDING_STATEMENT)
    if embedding_stamp is not None:
        statements.append(EMBEDDING_PENDING_STATEMENT)
        params["stamp"] = dict(embedding_stamp)
    results = _statement_results(
        await client.execute_query_batch(" ".join(statements), **params), len(statements)
    )
    sweep: PlaneFacts | None = None
    if graph_stamp is not None:
        states = normalize_records(results.pop(0))
        sweep = (
            plane_facts(
                states[0],
                plane=GRAPH_EMBEDDING_STATE_PLANE,
                stamp=graph_stamp,
                verify_interval=verify_interval,
            )
            if states
            else PlaneFacts(current=False)
        )
    lifecycle_pending = bool(normalize_records(results.pop(0)))
    embeddings_pending = embedding_stamp is not None and bool(normalize_records(results.pop(0)))
    return GraphWork(
        sweep=sweep, lifecycle_pending=lifecycle_pending, embeddings_pending=embeddings_pending
    )


@dataclass(frozen=True, slots=True)
class ContentWork:
    """What the shared content namespace says each organization still owes."""

    planes: Mapping[tuple[str, str], PlaneFacts]
    validation_pending: frozenset[str]

    def facts(self, organization_id: str, plane: str) -> PlaneFacts | None:
        return self.planes.get((organization_id, plane))

    def idle(self, organization_id: str, *, planes: Iterable[str]) -> bool:
        """Whether the organization owes nothing on the content side.

        ``planes`` names the content planes with a configured embedder; each
        must be current. Source validation must have nothing pending.
        """
        if organization_id in self.validation_pending:
            return False
        return all(
            (facts := self.planes.get((organization_id, plane))) is not None and facts.current
            for plane in planes
        )


async def probe_content_work(
    client: Any, *, stamps: Mapping[str, EmbeddingStamp], verify_interval: float
) -> ContentWork:
    """One round trip for every organization: content plane facts and pending source checks.

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
    facts: dict[tuple[str, str], PlaneFacts] = {}
    if planes:
        for row in normalize_records(results[0]):
            plane = str(row.get("plane") or "")
            stamp = stamps.get(plane)
            if stamp is None:
                continue
            facts[(str(row.get("organization_id") or ""), plane)] = plane_facts(
                row, plane=plane, stamp=stamp, verify_interval=verify_interval
            )
    pending = {
        str(row["organization_id"])
        for row in normalize_records(results[-1])
        if row.get("organization_id")
    }
    return ContentWork(planes=facts, validation_pending=frozenset(pending))


def _statement_results(response: object, expected: int) -> list[object]:
    if not isinstance(response, list) or len(response) != expected:
        raise RuntimeError(
            f"lifecycle probe expected {expected} statement results, got {type(response).__name__}"
        )
    return list(response)


def _optional_str(value: object) -> str | None:
    return str(value) if value else None


__all__ = [
    "CONTENT_STATES_STATEMENT",
    "EMBEDDING_PENDING_STATEMENT",
    "GRAPH_STATE_STATEMENT",
    "LIFECYCLE_PENDING_STATEMENT",
    "VALIDATION_PENDING_STATEMENT",
    "ContentWork",
    "GraphWork",
    "PlaneFacts",
    "plane_facts",
    "probe_content_work",
    "probe_graph_work",
]
