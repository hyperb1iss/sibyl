"""Authorize destination facts and bind source-local checked preview witnesses."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from anyio import to_thread
from fastapi import HTTPException, Request
from pydantic import ValidationError

from sibyl.api.routes import memory_auth
from sibyl.auth.context import AuthContext
from sibyl.persistence.surreal.content import surreal_content_client
from sibyl_core.auth.memory_policy import (
    MEMORY_PROVENANCE_METADATA_KEYS,
    MemoryPolicyAction,
    stamp_memory_scope_metadata,
)
from sibyl_core.backends.surreal.records import normalize_records, raise_on_error
from sibyl_core.memory_pipeline.audit import decode_audit_metadata
from sibyl_core.memory_pipeline.lifecycle import (
    graph_metadata_recallable,
    raw_memory_lifecycle_recallable,
)
from sibyl_core.migrate.personal_archive_candidates import (
    ArchiveCandidate,
    normalize_archive_candidates,
)
from sibyl_core.migrate.personal_archive_intake import ParsedPersonalArchive
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveAudience,
    ArchiveDisposition,
    ArchiveKind,
    ArchiveMappings,
    ArchiveStoreWitness,
    CheckedArchivePlan,
    PlannedArchiveRow,
    archive_digest,
    canonical_json,
)
from sibyl_core.services.content_models import raw_memory_from_record
from sibyl_core.services.graph_client import get_surreal_graph_client
from sibyl_core.services.graph_records import (
    entity_from_surreal_row,
    relationship_from_surreal_row,
)

_CONTENT_CUT = """
RETURN {
    LET $rows = SELECT *, crypto::sha256(type::string($this)) AS archive_row_sha256
        OMIT id FROM raw_captures WHERE uuid IN $identities ORDER BY uuid;
    LET $states = SELECT *, crypto::sha256(type::string($this)) AS archive_state_sha256
        OMIT id FROM source_states WHERE organization_id=$organization_id
        AND source_kind='raw_capture' AND source_id IN $identities ORDER BY source_id;
    LET $associations = SELECT *,
        crypto::sha256(type::string($this)) AS archive_association_sha256
        OMIT id FROM memory_derivations WHERE organization_id=$organization_id
        AND target_kind='raw_capture' AND target_id IN $identities ORDER BY target_id;
    RETURN {rows:$rows, states:$states, associations:$associations, edges:[]};
};
"""
_GRAPH_CUT = """
RETURN {
    LET $edges = SELECT *, crypto::sha256(type::string($this)) AS archive_row_sha256,
        in.uuid AS source_uuid, out.uuid AS target_uuid
        OMIT id, in, out FROM relates_to WHERE uuid IN $edge_ids ORDER BY uuid;
    LET $identities = array::distinct(array::concat(
        $node_ids, $edges.source_uuid, $edges.target_uuid
    ));
    LET $rows = SELECT *, crypto::sha256(type::string($this)) AS archive_row_sha256
        OMIT id FROM entity WHERE uuid IN $identities ORDER BY uuid;
    LET $states = SELECT *, crypto::sha256(type::string($this)) AS archive_state_sha256
        OMIT id FROM source_states WHERE organization_id=$organization_id
        AND source_kind='graph_entity' AND source_id IN $identities ORDER BY source_id;
    LET $associations = SELECT *,
        crypto::sha256(type::string($this)) AS archive_association_sha256
        OMIT id FROM memory_derivations WHERE organization_id=$organization_id
        AND target_kind='graph_entity' AND target_id IN $identities ORDER BY target_id;
    RETURN {rows:$rows, states:$states, associations:$associations, edges:$edges};
};
"""
_GRAPH_FIELDS = ("id", "entity_type", "name", "description", "content", "metadata")
_RAW_FIELDS = (
    "raw_content",
    "title",
    "entity_type",
    "source_id",
    "principal_id",
    "memory_scope",
    "scope_key",
    "agent_id",
    "project_id",
    "review_state",
    "metadata",
    "tags",
)
_PHYSICAL_METADATA = frozenset(
    {
        "record_id",
        "organization_id",
        "group_id",
        "created_by",
        "modified_by",
        # Destination use is measured locally; foreign usage is staged inertly.
        "last_recalled_at",
        "last_used_at",
        "retrieval_count",
        "citation_count",
        "misled_count",
    }
)
_PRIVATE = "private"
_WORK_ITEMS = frozenset({"task", "epic", "milestone"})
_INLINE_REFERENCES = ("epic_id", "parent_task_id", "task_id", "milestone_id")


def _unavailable() -> HTTPException:
    return HTTPException(status_code=403, detail="archive_destination_unavailable")


def _principal(context: AuthContext) -> tuple[str, str]:
    if context.organization_id is None or context.user_id is None:
        raise HTTPException(status_code=401, detail="archive_principal_required")
    return context.organization_id, context.user_id


@dataclass
class _PolicyGate:
    context: AuthContext
    request: Request
    admitted: set[tuple[MemoryPolicyAction, str, str]] = field(default_factory=set)

    async def audience(self, audience: ArchiveAudience, action: MemoryPolicyAction) -> None:
        _, actor_id = _principal(self.context)
        if audience.memory_scope == _PRIVATE and audience.scope_key != actor_id:
            raise _unavailable()
        key = action, audience.memory_scope, audience.scope_key
        if key in self.admitted:
            return
        try:
            await memory_auth.authorize_memory_policy(
                ctx=self.context,
                action=action,
                memory_scope=audience.memory_scope,
                scope_key=audience.scope_key,
                project_id=audience.scope_key if audience.memory_scope == "project" else None,
                surface="archive_import_check",
                request=self.request,
            )
            if action is MemoryPolicyAction.WRITE:
                await memory_auth.authorize_project_scope_write(
                    ctx=self.context,
                    memory_scope=audience.memory_scope,
                    scope_key=audience.scope_key,
                )
        except HTTPException as exc:
            raise _unavailable() from exc
        self.admitted.add(key)

    async def mappings(self, mappings: ArchiveMappings) -> None:
        audiences = [mappings.quarantine]
        audiences.extend(
            ArchiveAudience(memory_scope="project", scope_key=identity)
            for identity in mappings.projects.values()
        )
        audiences.extend(
            ArchiveAudience(memory_scope="team", scope_key=identity)
            for identity in mappings.teams.values()
        )
        for audience in audiences:
            await self.audience(audience, MemoryPolicyAction.WRITE)


def _indexed(rows: object, field_name: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise TypeError("archive destination snapshot returned invalid rows")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(field_name), str):
            raise TypeError("archive destination snapshot returned invalid identities")
        identity = row[field_name]
        if identity in result:
            raise _unavailable()
        result[identity] = row
    return result


@dataclass(frozen=True)
class _StoreCut:
    store: str
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    states: dict[str, dict[str, Any]] = field(default_factory=dict)
    associations: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def decode(cls, store: str, result: object) -> _StoreCut:
        raise_on_error(result)
        values = normalize_records(result)
        if len(values) != 1:
            raise RuntimeError("archive destination snapshot returned an invalid envelope")
        value = values[0]
        return cls(
            store,
            _indexed(value.get("rows"), "uuid"),
            _indexed(value.get("states"), "source_id"),
            _indexed(value.get("associations"), "target_id"),
            _indexed(value.get("edges"), "uuid"),
        )

    def witness(self, identity: str, *, edge: bool = False) -> ArchiveStoreWitness:
        def digest(row: dict[str, Any] | None, field_name: str) -> str | None:
            if row is None:
                return None
            value = row.get(field_name)
            if not isinstance(value, str) or len(value) != 64:
                raise RuntimeError("archive destination snapshot omitted a native witness")
            return value

        return ArchiveStoreWitness(
            store="content" if self.store == "content" else "graph",
            identity=(
                "relates_to:" if edge else "entity:" if self.store == "graph" else "raw_captures:"
            )
            + identity,
            row_sha256=digest(
                (self.edges if edge else self.rows).get(identity), "archive_row_sha256"
            ),
            state_sha256=None
            if edge
            else digest(self.states.get(identity), "archive_state_sha256"),
            associations_sha256=None
            if edge
            else digest(self.associations.get(identity), "archive_association_sha256"),
        )

    def protected(self, identity: str, row: dict[str, Any], metadata: dict[str, Any]) -> bool:
        return (
            row.get("derivation_required") is True
            or row.get("operational_derivation_required") is True
            or row.get("operational_source_binding") is not None
            or row.get("deleted_at") is not None
            or self.states.get(identity, {}).get("deleted") is True
            or identity in self.associations
            or any(
                metadata.get(key) not in (None, "", False)
                for key in MEMORY_PROVENANCE_METADATA_KEYS
                | {"reflection_identity", "origin_execution_id", "operational_source_binding"}
            )
        )


async def _read_cuts(
    *, organization_id: str, raw_ids: list[str], node_ids: list[str], edge_ids: list[str]
) -> tuple[_StoreCut, _StoreCut]:
    content = _StoreCut("content")
    graph = _StoreCut("graph")
    if raw_ids:
        async with surreal_content_client() as client:
            content = _StoreCut.decode(
                "content",
                await client.execute_query(
                    _CONTENT_CUT,
                    organization_id=organization_id,
                    identities=raw_ids,
                ),
            )
    if node_ids or edge_ids:
        client = await get_surreal_graph_client(organization_id)
        graph = _StoreCut.decode(
            "graph",
            await client.execute_query(
                _GRAPH_CUT,
                organization_id=organization_id,
                node_ids=node_ids,
                edge_ids=edge_ids,
            ),
        )
    return content, graph


def _metadata(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in decode_audit_metadata(value).items()
        if key not in _PHYSICAL_METADATA
    }


def _graph_metadata(value: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(value)
    # The ordinary graph writer mirrors its storage update clock into attributes.
    # User timestamps under other metadata keys remain semantic.
    metadata.pop("updated_at", None)
    return metadata


def _scope_fields(
    fields: dict[str, Any], *, actor_id: str, identity: str, entity_type: str | None = None
) -> ArchiveAudience:
    scope, key = fields.get("memory_scope"), fields.get("scope_key")
    if entity_type in {"project", "team"}:
        scope, key = entity_type, identity
    elif scope is None and entity_type in _WORK_ITEMS and fields.get("project_id"):
        scope, key = "project", fields["project_id"]
    if scope == _PRIVATE:
        if (fields.get("principal_id") or key) != actor_id:
            raise _unavailable()
        key = actor_id
    if scope not in {_PRIVATE, "project", "team"} or not isinstance(key, str) or not key:
        raise _unavailable()
    if scope == "project" and fields.get("project_id") not in (None, key):
        raise _unavailable()
    return ArchiveAudience(memory_scope=scope, scope_key=key)


def _existing_bodies(
    content: _StoreCut, graph: _StoreCut, *, organization_id: str, actor_id: str
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[ArchiveAudience],
]:
    raw_bodies, graph_bodies, edge_bodies = {}, {}, {}
    audiences: list[ArchiveAudience] = []
    # History without an authorized retained row cannot reveal a tombstone or
    # protected target. This also denies foreign active UUID collisions opaquely.
    for cut in (content, graph):
        if (cut.states.keys() | cut.associations.keys()) - cut.rows.keys():
            raise _unavailable()
        for identity, row in cut.rows.items():
            org_field = "organization_id" if cut.store == "content" else "group_id"
            if row.get(org_field) != organization_id:
                raise _unavailable()
            try:
                if cut.store == "content":
                    memory = raw_memory_from_record(row)
                    body = {key: row.get(key) for key in _RAW_FIELDS}
                    body.update(
                        metadata=_metadata(memory.metadata),
                        tags=memory.tags,
                        title=memory.title,
                        raw_content=memory.raw_content,
                        entity_type=memory.entity_type,
                        review_state=memory.review_state,
                        source_id=memory.source_id,
                        principal_id=memory.principal_id,
                        memory_scope=memory.memory_scope.value,
                        scope_key=memory.scope_key,
                        agent_id=memory.agent_id,
                        project_id=memory.project_id,
                    )
                    audience = _scope_fields(body, actor_id=actor_id, identity=identity)
                    raw_bodies[identity] = body
                else:
                    entity = entity_from_surreal_row(row)
                    public = entity.model_dump(mode="json")
                    body = {key: public[key] for key in _GRAPH_FIELDS}
                    body["metadata"] = _graph_metadata(public["metadata"])
                    audience = _scope_fields(
                        body["metadata"],
                        actor_id=actor_id,
                        identity=identity,
                        entity_type=entity.entity_type.value,
                    )
                    graph_bodies[identity] = body
            except (ValidationError, ValueError, TypeError) as exc:
                raise _unavailable() from exc
            audiences.append(audience)
    for identity, row in graph.edges.items():
        if row.get("group_id") != organization_id:
            raise _unavailable()
        endpoints = row.get("source_uuid"), row.get("target_uuid")
        if any(
            not isinstance(endpoint, str) or endpoint not in graph_bodies for endpoint in endpoints
        ):
            raise _unavailable()
        try:
            relationship = relationship_from_surreal_row(row)
        except (ValidationError, ValueError, TypeError) as exc:
            raise _unavailable() from exc
        body = relationship.model_dump(mode="json", exclude={"created_at"})
        body["metadata"] = _metadata(body["metadata"])
        if body["metadata"].get("memory_scope") is not None:
            audiences.append(_scope_fields(body["metadata"], actor_id=actor_id, identity=identity))
        edge_bodies[identity] = body
    return raw_bodies, graph_bodies, edge_bodies, audiences


async def _authorize_existing(
    content: _StoreCut, graph: _StoreCut, gate: _PolicyGate
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    organization_id, actor_id = _principal(gate.context)
    raw_bodies, graph_bodies, edge_bodies, audiences = await to_thread.run_sync(
        partial(
            _existing_bodies,
            content,
            graph,
            organization_id=organization_id,
            actor_id=actor_id,
        )
    )
    for audience in audiences:
        await gate.audience(audience, MemoryPolicyAction.READ)
    return raw_bodies, graph_bodies, edge_bodies


def _prepared_body(
    candidate: ArchiveCandidate,
    preview: PlannedArchiveRow,
    *,
    actor_id: str,
    node_ids: dict[str, str],
) -> dict[str, Any]:
    body = json.loads(candidate.semantic_json)
    if candidate.kind is ArchiveKind.GRAPH_RELATIONSHIP:
        body["metadata"] = _edge_semantic_metadata(body)
    metadata = stamp_memory_scope_metadata(
        (_graph_metadata if candidate.kind is ArchiveKind.GRAPH_ENTITY else _metadata)(
            body.get("metadata", {})
        ),
        memory_scope=candidate.audience.memory_scope,
        scope_key=candidate.audience.scope_key,
        principal_id=actor_id,
    )
    metadata.pop("agent_id", None)
    metadata.pop("project_id", None)
    if candidate.audience.memory_scope == "project":
        metadata["project_id"] = candidate.audience.scope_key
    for key in _INLINE_REFERENCES:
        if key in metadata:
            original = metadata[key]
            if isinstance(original, str) and original in node_ids:
                metadata[key] = node_ids[original]
            else:
                # An unmapped foreign reference is retained only in the inert
                # artifact. It cannot point at an unrelated destination row.
                metadata.pop(key)
    body["metadata"] = metadata
    if candidate.kind is ArchiveKind.RAW_CAPTURE:
        body.update(
            source_id=preview.destination_id,
            principal_id=actor_id,
            memory_scope=candidate.audience.memory_scope,
            scope_key=None
            if candidate.audience.memory_scope == _PRIVATE
            else candidate.audience.scope_key,
            agent_id=None,
            project_id=candidate.audience.scope_key
            if candidate.audience.memory_scope == "project"
            else None,
        )
    else:
        body["id"] = preview.destination_id
    if candidate.kind is ArchiveKind.GRAPH_RELATIONSHIP:
        body["source_id"], body["target_id"] = preview.endpoint_ids[:2]
    return body


def _edge_semantic_metadata(body: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(body["metadata"])
    # The ordinary writer/read adapter adds these redundant defaults. Other
    # facts and nonempty episode inventories remain part of the semantic body.
    metadata.pop("weight", None)
    if metadata.get("source_id") == body["source_id"]:
        metadata.pop("source_id")
    if metadata.get("episodes") == []:
        metadata.pop("episodes")
    default_fact = f"{body['source_id']} {body['relationship_type'].lower()} {body['target_id']}"
    if metadata.get("fact") in (None, "", default_fact):
        metadata.pop("fact", None)
    return metadata


def _semantic_body(body: dict[str, Any], kind: ArchiveKind) -> str:
    if kind is ArchiveKind.GRAPH_RELATIONSHIP:
        body = {**body, "metadata": _edge_semantic_metadata(body)}
    return canonical_json(body)


def _normalize_preview_inputs(
    parsed: ParsedPersonalArchive,
    mappings: ArchiveMappings,
    *,
    organization_id: str,
    actor_id: str,
) -> tuple[
    tuple[ArchiveCandidate, ...],
    dict[tuple[ArchiveKind, str], PlannedArchiveRow],
    dict[str, str],
    dict[tuple[ArchiveKind, str], tuple[str, ...]],
]:
    candidates = normalize_archive_candidates(parsed, mappings, actor_id=actor_id)
    previews = {
        (row.kind, row.original_id): row.initial_preview(
            organization_id=organization_id,
            actor_id=actor_id,
            origin=parsed.origin,
        )
        for row in candidates
    }
    node_ids = {
        row.original_id: row.destination_id
        for row in previews.values()
        if row.kind is ArchiveKind.GRAPH_ENTITY and row.destination_id is not None
    }
    graph_original_ids = {
        row.original_id for row in candidates if row.kind is ArchiveKind.GRAPH_ENTITY
    }
    dependencies: dict[tuple[ArchiveKind, str], tuple[str, ...]] = {}
    for candidate in candidates:
        if candidate.protection != "ordinary":
            continue
        key = candidate.kind, candidate.original_id
        metadata = json.loads(candidate.semantic_json).get("metadata", {})
        inline = [
            value
            for field in _INLINE_REFERENCES
            if isinstance(value := metadata.get(field), str) and value in graph_original_ids
        ]
        originals = candidate.original_endpoint_ids + tuple(inline)
        dependencies[key] = tuple(dict.fromkeys(originals))
        # Preserve relationship source/target order (including a self-edge).
        endpoints = tuple(
            node_ids[identity]
            for identity in candidate.original_endpoint_ids
            if identity in node_ids
        )
        endpoints += tuple(
            node_ids[identity]
            for identity in dict.fromkeys(inline)
            if identity in node_ids and node_ids[identity] not in endpoints
        )
        previews[key] = previews[key].model_copy(update={"endpoint_ids": endpoints})
    return candidates, previews, node_ids, dependencies


async def build_archive_preview(
    *,
    parsed: ParsedPersonalArchive,
    mappings: ArchiveMappings,
    context: AuthContext,
    request: Request,
) -> tuple[PlannedArchiveRow, ...]:
    """Build a checked preview, with separate authorization and store-local cuts."""
    organization_id, actor_id = _principal(context)
    mappings = ArchiveMappings.model_validate(mappings.model_dump(mode="python"))
    gate = _PolicyGate(context, request)
    await gate.mappings(mappings)
    candidates, previews, node_ids, dependencies = await to_thread.run_sync(
        partial(
            _normalize_preview_inputs,
            parsed,
            mappings,
            organization_id=organization_id,
            actor_id=actor_id,
        )
    )
    for candidate in candidates:
        await gate.audience(candidate.audience, MemoryPolicyAction.WRITE)
    rows = tuple(previews.values())
    content, graph = await _read_cuts(
        organization_id=organization_id,
        raw_ids=[
            row.destination_id
            for row in rows
            if row.kind is ArchiveKind.RAW_CAPTURE and row.destination_id is not None
        ],
        node_ids=list(node_ids.values()),
        edge_ids=[
            row.destination_id
            for row in rows
            if row.kind is ArchiveKind.GRAPH_RELATIONSHIP and row.destination_id is not None
        ],
    )
    raw_bodies, graph_bodies, edge_bodies = await _authorize_existing(content, graph, gate)
    return await to_thread.run_sync(
        partial(
            _resolve_previews,
            candidates,
            previews,
            actor_id=actor_id,
            node_ids=node_ids,
            dependencies=dependencies,
            content=content,
            graph=graph,
            raw_bodies=raw_bodies,
            graph_bodies=graph_bodies,
            edge_bodies=edge_bodies,
        )
    )


def _resolve_previews(
    candidates: tuple[ArchiveCandidate, ...],
    previews: dict[tuple[ArchiveKind, str], PlannedArchiveRow],
    *,
    actor_id: str,
    node_ids: dict[str, str],
    dependencies: dict[tuple[ArchiveKind, str], tuple[str, ...]],
    content: _StoreCut,
    graph: _StoreCut,
    raw_bodies: dict[str, dict[str, Any]],
    graph_bodies: dict[str, dict[str, Any]],
    edge_bodies: dict[str, dict[str, Any]],
) -> tuple[PlannedArchiveRow, ...]:
    for candidate in candidates:
        row = previews[candidate.kind, candidate.original_id]
        if row.destination_id is None:
            continue
        identity = row.destination_id
        cut = content if row.kind is ArchiveKind.RAW_CAPTURE else graph
        edge = row.kind is ArchiveKind.GRAPH_RELATIONSHIP
        body = (
            edge_bodies if edge else raw_bodies if cut.store == "content" else graph_bodies
        ).get(identity)
        native = (cut.edges if edge else cut.rows).get(identity)
        witnesses = (cut.witness(identity, edge=edge),)
        witnesses += tuple(
            graph.witness(endpoint)
            for endpoint in dict.fromkeys(row.endpoint_ids)
            if edge or cut.store != "graph" or endpoint != identity
        )
        updated: dict[str, Any] = {"witnesses": witnesses}
        if candidate.fixed_destination_id is not None:
            expected_kind = json.loads(candidate.semantic_json)["entity_type"]
            if body is None or native is None:
                raise _unavailable()
            if body["entity_type"] != expected_kind:
                updated.update(
                    disposition=ArchiveDisposition.CONFLICTED,
                    reason="destination_anchor_type_conflict",
                )
            elif cut.protected(identity, native, body["metadata"]) or not graph_metadata_recallable(
                body["metadata"]
            ):
                updated.update(
                    disposition=ArchiveDisposition.CONFLICTED,
                    reason="destination_protected_or_retired",
                )
        else:
            expected = _prepared_body(candidate, row, actor_id=actor_id, node_ids=node_ids)
            updated["semantic_sha256"] = archive_digest(
                "sibyl-archive-destination-v1",
                {
                    "kind": row.kind.value,
                    "body": _semantic_body(expected, row.kind),
                    "audience": row.audience.model_dump(mode="json"),
                },
            )
            if native is not None and body is not None:
                metadata = body["metadata"]
                recallable = (
                    raw_memory_lifecycle_recallable(raw_memory_from_record(native))
                    if cut.store == "content"
                    else graph_metadata_recallable(metadata)
                )
                if cut.protected(identity, native, metadata) or not recallable:
                    updated.update(
                        disposition=ArchiveDisposition.CONFLICTED,
                        reason="destination_protected_or_retired",
                    )
                elif _semantic_body(body, row.kind) == _semantic_body(expected, row.kind):
                    updated.update(
                        disposition=ArchiveDisposition.SKIPPED,
                        reason="destination_canonical_identical",
                    )
                else:
                    updated.update(
                        disposition=ArchiveDisposition.CONFLICTED,
                        reason="destination_body_conflict",
                    )
        previews[candidate.kind, candidate.original_id] = PlannedArchiveRow.model_validate(
            {**row.model_dump(mode="python"), **updated}
        )
    reverse: dict[tuple[ArchiveKind, str], list[tuple[ArchiveKind, str]]] = defaultdict(list)
    for dependent, originals in dependencies.items():
        for original in originals:
            reverse[ArchiveKind.GRAPH_ENTITY, original].append(dependent)
    blocked = {
        key
        for key, row in previews.items()
        if row.disposition not in {ArchiveDisposition.CREATED, ArchiveDisposition.SKIPPED}
    }
    pending = deque(blocked)
    while pending:
        for dependent in reverse.get(pending.popleft(), ()):
            row = previews[dependent]
            previews[dependent] = row.model_copy(
                update={
                    "disposition": ArchiveDisposition.QUARANTINED,
                    "reason": "dependent_destination_conflict",
                }
            )
            if dependent not in blocked:
                blocked.add(dependent)
                pending.append(dependent)
    return tuple(previews.values())


def _validated_plan_snapshot(plan: CheckedArchivePlan) -> CheckedArchivePlan:
    return CheckedArchivePlan.model_validate(plan.model_dump(mode="python"))


async def authorize_archive_plan(
    *, plan: CheckedArchivePlan, context: AuthContext, request: Request
) -> None:
    """Recheck a saved POST result's current authority before original diagnostics."""
    plan = await to_thread.run_sync(_validated_plan_snapshot, plan)
    organization_id, actor_id = _principal(context)
    if plan.organization_id != organization_id or plan.actor_id != actor_id:
        raise _unavailable()
    gate = _PolicyGate(context, request)
    await gate.mappings(plan.mappings)
    for row in plan.rows:
        await gate.audience(row.audience, MemoryPolicyAction.WRITE)
    nodes = {
        row.destination_id: row
        for row in plan.rows
        if row.kind is ArchiveKind.GRAPH_ENTITY and row.destination_id is not None
    }
    for row in plan.rows:
        for endpoint in row.endpoint_ids:
            if endpoint not in nodes:
                raise _unavailable()
    content, graph = await _read_cuts(
        organization_id=organization_id,
        raw_ids=[
            row.destination_id
            for row in plan.rows
            if row.kind is ArchiveKind.RAW_CAPTURE and row.destination_id is not None
        ],
        node_ids=list(nodes),
        edge_ids=[
            row.destination_id
            for row in plan.rows
            if row.kind is ArchiveKind.GRAPH_RELATIONSHIP and row.destination_id is not None
        ],
    )
    await _authorize_existing(content, graph, gate)
    for identity, row in nodes.items():
        if identity not in graph.rows and row.disposition not in {
            ArchiveDisposition.CREATED,
            ArchiveDisposition.QUARANTINED,
        }:
            raise _unavailable()
