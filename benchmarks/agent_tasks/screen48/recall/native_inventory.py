"""Complete checkpoint inventory through existing scoped storage/read owners.

This module has no SQL, rank queries, provider calls or write entry. Native
snapshot fingerprints bind the complete rows, ledgers and graph companions;
only authorized, current public candidates leave the snapshot boundary.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict

from benchmarks.agent_tasks.screen48.contract import digest
from benchmarks.agent_tasks.screen48.recall.native_evidence import native_evidence
from benchmarks.agent_tasks.screen48.recall.whole_items import MissingPack, native_key

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.migrate.source_integrity import encode_record
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.retrieval._search_candidates import (
    _candidate_allowed,
    _candidate_from_edge_record,
    _candidate_from_episode_record,
    _candidate_from_node_record,
    _candidate_from_raw_memory,
)
from sibyl_core.retrieval._search_fusion import _search_result_from_candidate
from sibyl_core.retrieval._search_lifecycle import _apply_supersession_gate
from sibyl_core.retrieval._search_plan import RetrievalSignal, build_context_retrieval_plan
from sibyl_core.services.content_client import surreal_content_client
from sibyl_core.services.content_models import raw_memory_currently_recallable
from sibyl_core.services.content_raw_recall import list_raw_memories_for_scope
from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
from sibyl_core.services.graph_read_availability import available_graph_entities
from sibyl_core.services.graph_runtime import get_surreal_graph_runtime
from sibyl_core.services.source_archive_store import read_source_archive_snapshot
from sibyl_core.services.source_observations import observe_graph_snapshot, observe_raw_capture
from sibyl_core.services.source_state_store import source_snapshot_from_records

# A native snapshot fingerprint is a hex sha256 and nothing else.
FINGERPRINT_LENGTH = 64


def _index(rows, key, org, org_key):
    result = {}
    for row in rows:
        identifier = row.get(key)
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in result
            or row.get(org_key) != org
        ):
            raise MissingPack("invalid_complete_snapshot_membership")
        result[identifier] = row
    return result


def _snapshot_identity(snapshot):
    # The native fingerprint includes every field, including metadata with names
    # also used by retrieval diagnostics. The typed digest independently binds
    # exactly the complete records consumed by Python, including date precision.
    if (
        not isinstance(snapshot.get("fingerprint"), str)
        or len(snapshot["fingerprint"]) != FINGERPRINT_LENGTH
    ):
        raise MissingPack("native_snapshot_fingerprint_missing")
    return {
        "native_fingerprint": snapshot["fingerprint"],
        "consumed_sha256": digest(encode_record(snapshot)),
    }


async def _snapshots(org):
    runtime = await get_surreal_graph_runtime(org, ensure_schema=False)
    graph = await read_source_archive_snapshot(
        runtime.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[org],
        include_graph_auxiliary=True,
    )
    async with surreal_content_client() as client:
        raw = await read_source_archive_snapshot(
            client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=[org]
        )
    return runtime, graph, raw


# One pass over the complete snapshot: graph nodes, legacy episodes, edges and
# every scoped raw capture. Splitting it would let a partial universe escape.
async def enumerate_current(*, organization_id, authority, reader):  # noqa: PLR0912, PLR0915
    """Enumerate the query-independent native universe, with no result limit."""
    org = organization_id
    if authority.principal_id != reader.principal_id:
        raise MissingPack("inventory_reader_changed")
    runtime, graph, raw = await _snapshots(org)
    before = {"graph": _snapshot_identity(graph), "raw": _snapshot_identity(raw)}
    nodes = _index(graph["source_rows"], "uuid", org, "group_id")
    captures = _index(raw["source_rows"], "uuid", org, "organization_id")
    graph_states = _index(graph["source_states"], "source_id", org, "organization_id")
    raw_states = _index(raw["source_states"], "source_id", org, "organization_id")
    auxiliary = graph["graph_auxiliary"]
    episodes = _index(auxiliary["episode"], "uuid", org, "group_id")
    edges = _index(auxiliary["relates_to"], "uuid", org, "group_id")
    plan = build_context_retrieval_plan(
        query="",
        organization_id=org,
        facets=(),
        facet_types={},
        principal_id=reader.principal_id,
        project=reader.project,
        accessible_projects=authority.projects,
        allowed_memory_scope_keys=authority.scope_keys,
        limit=50,
    )
    candidates, provenance, excluded = [], {}, Counter()
    visible_nodes = await available_graph_entities(org, list(nodes), runtime=runtime)

    def add(candidate, source):
        if not _candidate_allowed(candidate, plan=plan, requested_types=set(), facet=None):
            excluded["scope_denied"] += 1
            return
        key = native_key(candidate)
        if key in provenance:
            raise MissingPack("ambiguous_native_identity")
        candidates.append(candidate)
        provenance[key] = source

    for sid, row in nodes.items():
        candidate = _candidate_from_node_record(row, signal=RetrievalSignal.NODE_FULLTEXT, score=0)
        if sid not in visible_nodes or not _candidate_allowed(
            candidate, plan=plan, requested_types=set(), facet=None
        ):
            excluded["graph_unavailable_or_scope_denied"] += 1
            continue
        source = SourceIdentity(org, SourceKind.GRAPH_ENTITY, sid)
        snapshot = source_snapshot_from_records(source, row, graph_states.get(sid))
        if snapshot is None:
            raise MissingPack("eligible_graph_source_has_no_durable_observation")
        if visible_nodes[sid].model_dump(mode="json") != snapshot.entity.model_dump(mode="json"):
            raise MissingPack("available_graph_row_changed")
        observation = observe_graph_snapshot(snapshot, source, authority)
        add(
            candidate,
            {
                "kind": "graph_entity",
                "observation": asdict(observation),
                "source_record_sha256": digest(encode_record(row)),
            },
        )
    for _sid, row in episodes.items():
        add(
            _candidate_from_episode_record(row, signal=RetrievalSignal.EPISODE_FULLTEXT, score=0),
            {"kind": "legacy_episode", "source_record_sha256": digest(encode_record(row))},
        )
    # Companion rows carry exact immutable record keys. Recover endpoint aliases
    # from this same snapshot, rather than interpreting entity UUIDs as record IDs.
    records = {row["archive_record_key"]: row for row in nodes.values()}
    if len(records) != len(nodes):
        raise MissingPack("ambiguous_native_record_identity")
    for _sid, original in edges.items():
        row = dict(original)
        for side, field in [("source", "source_record_key"), ("target", "target_record_key")]:
            endpoint = records.get(row[field])
            row[side + "_node_uuid"] = endpoint["uuid"] if endpoint else None
            row[side + "_node_project_id"] = (
                (endpoint.get("project_id") or endpoint.get("attributes", {}).get("project_id"))
                if endpoint
                else None
            )
        add(
            _candidate_from_edge_record(row, signal=RetrievalSignal.EDGE_FULLTEXT, score=0),
            {"kind": "relationship", "source_record_sha256": digest(encode_record(original))},
        )
    seen_raw = set()
    for scope in plan.scopes:
        if scope.memory_scope not in {
            MemoryScope.PRIVATE,
            MemoryScope.PROJECT,
            MemoryScope.DELEGATED,
        }:
            continue
        # A complete snapshot supplies an exact upper bound, not a tuned recall
        # limit. The existing scoped list owner supplies the real SQL predicates.
        memories = await list_raw_memories_for_scope(
            organization_id=org,
            principal_id=scope.principal_id,
            memory_scope=scope.memory_scope,
            scope_key=scope.scope_key,
            agent_id=scope.agent_id,
            project_id=scope.project_id,
            limit=len(captures) + 1,
            include_lifecycle_hidden=True,
        )
        if len({m.id for m in memories}) != len(memories) or any(
            m.id not in captures for m in memories
        ):
            raise MissingPack("scoped_raw_inventory_changed")
        current = []
        for listed in memories:
            if listed.id in seen_raw:
                continue
            seen_raw.add(listed.id)
            row = captures[listed.id]
            source = SourceIdentity(org, SourceKind.RAW_CAPTURE, listed.id)
            snapshot = source_snapshot_from_records(source, row, raw_states.get(listed.id))
            if not raw_memory_currently_recallable(listed):
                excluded["raw_lifecycle"] += 1
                continue
            if snapshot is None:
                raise MissingPack("eligible_raw_source_has_no_durable_observation")
            observe_raw_capture(snapshot.memory, authority)
            if not raw_memory_currently_recallable(snapshot.memory):
                raise MissingPack("scoped_raw_lifecycle_changed")
            current.append(snapshot)
        unavailable = await unavailable_publication_ids(
            org,
            {s.memory.id: s.memory.metadata for s in current},
            raw_memories=[s.memory for s in current],
            source_authority=authority,
        )
        for snapshot in current:
            if snapshot.memory.id in unavailable:
                excluded["raw_publication_unavailable"] += 1
                continue
            add(
                _candidate_from_raw_memory(snapshot.memory, scope),
                {
                    "kind": "raw_capture",
                    "observation": asdict(snapshot.observation),
                    "source_record_sha256": digest(encode_record(captures[snapshot.memory.id])),
                },
            )
    gated, lifecycle = await _apply_supersession_gate(
        client=runtime.client,
        group_id=org,
        source_lists=[(RetrievalSignal.NODE_FULLTEXT, candidates)],
        plan=plan,
    )
    items = {}
    for _, surviving in gated:
        for candidate in surviving:
            result = _search_result_from_candidate(
                candidate, score=0, fusion_metadata={}, include_content=True
            )
            key = native_key(result)
            if key in items or key not in provenance:
                raise MissingPack("native_lifecycle_identity_changed")
            items[key] = native_evidence(result)
    _, graph_after, raw_after = await _snapshots(org)
    after = {"graph": _snapshot_identity(graph_after), "raw": _snapshot_identity(raw_after)}
    if after != before:
        raise MissingPack("native_source_snapshot_changed")
    return items, {
        "schema": "sibyl-authorized-native-inventory-v1",
        "organization_id": org,
        "reader": asdict(reader),
        "authority": authority.ceiling_metadata(),
        "snapshots": before,
        "source_counts": {
            "entity": len(nodes),
            "episode": len(episodes),
            "relationship": len(edges),
            "raw_capture": len(captures),
        },
        "authorized_count": len(items),
        "eligible_catalog_sha256": digest(items),
        "provenance": {key: provenance[key] for key in sorted(items)},
        "excluded": dict(excluded),
        "lifecycle": lifecycle,
        "provider_calls": 0,
    }


class NativeCheckpoints:
    """Qualified in-process checkpoint bindings for RecallAdapter's v2 callback."""

    def __init__(self, *, catalog, reader, resolve_authority, verify_owners):
        self.catalog, self.reader = catalog, reader
        self.resolve_authority, self.verify_owners = resolve_authority, verify_owners
        self.checkpoints = {}

    async def produce(self, checkpoint):
        if (
            type(checkpoint) is not int
            or checkpoint not in (0, 1)
            or checkpoint in self.checkpoints
        ):
            raise MissingPack("checkpoint_inventory_already_bound_or_unknown")
        before = self.verify_owners()
        authority = await self.resolve_authority(
            self.catalog.organization_id, self.reader.principal_id
        )
        await self.catalog.check(authority)
        items, receipt = await enumerate_current(
            organization_id=self.catalog.organization_id, authority=authority, reader=self.reader
        )
        final_authority = await self.resolve_authority(
            self.catalog.organization_id, self.reader.principal_id
        )
        await self.catalog.check(final_authority)
        if final_authority != authority or self.verify_owners() != before:
            raise MissingPack("inventory_qualification_changed")
        receipt.update(
            checkpoint=checkpoint,
            source_owners=before,
            original_catalog_sha256=self.catalog.catalog_sha256,
        )
        self.checkpoints[checkpoint] = deepcopy((items, receipt))
        return deepcopy((items, receipt))

    async def verify(self, checkpoint, items, authority):
        expected = self.checkpoints.get(checkpoint)
        if (
            expected is None
            or expected[0] != items
            or expected[1]["source_owners"] != self.verify_owners()
        ):
            raise MissingPack("checkpoint_native_inventory_unbound")
        current, receipt = await enumerate_current(
            organization_id=self.catalog.organization_id, authority=authority, reader=self.reader
        )
        prior = {
            k: v
            for k, v in expected[1].items()
            if k not in {"checkpoint", "source_owners", "original_catalog_sha256"}
        }
        if current != items or receipt != prior:
            raise MissingPack("checkpoint_native_inventory_changed")
        return digest(items)
