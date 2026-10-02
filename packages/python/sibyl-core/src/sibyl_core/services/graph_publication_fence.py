"""Internal typed publication on one caller-owned native transaction.

The result is staged evidence. Only the transaction owner can acknowledge a
commit or reconcile an unknown outcome; this module activates no host writer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sibyl_core.backends.surreal.native_transaction import NativeStoreScope, NativeTransaction
from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.models.entities import Entity
from sibyl_core.services.graph_derivations import graph_target_digest
from sibyl_core.services.graph_entity_store import _entity_record
from sibyl_core.services.graph_read_validation import GraphReadValidation
from sibyl_core.services.graph_records import entity_from_surreal_row
from sibyl_core.services.memory_derivations import observation_from_record, validate_observations
from sibyl_core.services.memory_source_validation import (
    SourceAuthorityResolver,
    source_authority_ceiling,
)
from sibyl_core.services.ordinary_publication import OrdinaryValidatedPromotion
from sibyl_core.services.source_observations import (
    GraphSourceSnapshot,
    SourceUnavailableError,
    observe_graph_snapshot,
    observe_raw_capture,
)
from sibyl_core.services.source_state_store import (
    NativeSourceCut,
    RawSourceSnapshot,
    check_native_source_cuts,
    load_native_source_cut,
)
from sibyl_core.services.validation_promotion import ValidatedPromotion


@dataclass(frozen=True, slots=True)
class PublicationSourceEvidence:
    """Qualified native hashes, never an authorization or reusable permit."""

    namespace: str
    database: str
    source: SourceIdentity
    row_id: str
    row_sha256: str
    state_id: str
    state_sha256: str
    association_id: str | None
    association_sha256: str | None


@dataclass(frozen=True, slots=True)
class StagedTypedGraphPublication:
    entity: Entity
    created: bool
    physical_id: str
    body_sha256: str
    sources: tuple[PublicationSourceEvidence, ...]


# Every source/State is collected by its domain owner. This additional cut binds
# terminal admission artifacts; mutable promotion pointers are fenced by the
# candidate State under the canonical content writer.
_PROCEDURE_CUT = """
LET $ledgers=(SELECT * FROM eval_consolidations WHERE organization_id=$org AND candidate_id=$candidate);
IF array::len($ledgers)!=1 { THROW 'publication_admission_cardinality'; };
LET $ledger=$ledgers[0];
LET $bindings=$ledger.admission_bindings;
IF $ledger.result_kind!='candidate' OR $ledger.candidate_id!=$candidate
    OR !type::is_array($bindings) OR array::len($bindings)<2
    OR array::len(array::distinct($bindings.map(|$binding| $binding.capture_id)))!=array::len($bindings) {
    THROW 'publication_admission_bindings';
};
LET $attempts=$bindings.map(|$binding| {
    LET $rows=(SELECT * FROM eval_attempts WHERE organization_id=$org
        AND experiment_id=$binding.experiment_id AND attempt_id=$binding.attempt_id);
    IF array::len($rows)!=1 { THROW 'publication_admission_cardinality'; };
    LET $row=$rows[0];
    IF $row.admitted_at=NONE OR $row.capture_id!=$binding.capture_id
        OR $row.assignment_sha256!=$binding.assignment_sha256
        OR $row.receipt_sha256!=$binding.receipt_sha256
        OR $row.episode_sha256!=$binding.episode_sha256
        OR $row.outcome_sha256!=$binding.outcome_sha256
        OR $row.transcript_sha256!=$binding.transcript_sha256
        OR crypto::sha256($row.assignment_json)!=$binding.assignment_artifact_sha256
        OR crypto::sha256($row.receipt_base64)!=$binding.receipt_artifact_sha256 {
        THROW 'publication_admission_changed';
    };
    RETURN {id:type::string($row.id),sha256:crypto::sha256(type::string($row))};
});
LET $admission_cut={ledger_id:type::string($ledger.id),ledger_uuid:$ledger.uuid,
    ledger_sha256:crypto::sha256(type::string($ledger)),attempts:$attempts,
    source_ids:array::sort(array::distinct(array::concat([$candidate],$bindings.map(|$b| $b.capture_id))))};
"""

_EXECUTION_CUT = """
LET $executions=(SELECT * FROM memory_validation_executions
    WHERE (organization_id=$org AND uuid=$execution_id AND principal_id=$principal)
        OR id=type::record('memory_validation_executions',$execution_id));
IF array::len($executions)!=1 { THROW 'publication_execution_cardinality'; };
LET $execution=$executions[0];
IF $execution.organization_id!=$org OR $execution.uuid!=$execution_id
    OR $execution.principal_id!=$principal
    OR $execution.state!='returned' OR $execution.purged!=false
    OR !type::is_string($execution.request_json) OR !type::is_string($execution.result_json) {
    THROW 'publication_execution_unavailable';
};
LET $execution_cut={physical_id:type::string($execution.id),
    sha256:crypto::sha256(type::string((SELECT * OMIT promotion_write_witness FROM $execution)[0]))};
"""


@dataclass(frozen=True, slots=True)
class _ExecutionIdentity:
    execution_id: str
    principal_id: str


@dataclass(frozen=True, slots=True)
class _ProcedureIdentity:
    candidate_id: str


class _PublicationCollector:
    """Private obligation-specific sink owned by one staged operation."""

    def __init__(
        self,
        content: SurrealExecute,
        graph: SurrealExecute,
        organization: str,
        root: SourceIdentity,
    ) -> None:
        self.content = content
        self.graph = graph
        self.org = organization
        self.root = root
        self.sources: dict[SourceIdentity, set[str]] = {}
        self.dependencies: dict[object, set[object]] = {}
        self.execution_sources: dict[_ExecutionIdentity, set[SourceIdentity]] = {}
        self.cuts: dict[SourceIdentity, NativeSourceCut] = {}
        self.executions: set[tuple[str, str]] = set()
        self.execution_cuts: dict[tuple[str, str], dict[str, object]] = {}
        self.procedure_cuts: dict[str, dict[str, object]] = {}
        self.sealed = False

    def register_source(self, source: SourceIdentity, *, candidate: bool = False) -> None:
        if self.sealed or source.organization_id != self.org:
            raise SourceUnavailableError()
        self.sources.setdefault(source, set()).add("candidate" if candidate else "ordinary")

    def register_execution(
        self, execution: str, principal: str, row, owner: SourceIdentity | None
    ) -> None:
        if self.sealed or not execution or not principal:
            raise SourceUnavailableError()
        self.executions.add((execution, principal))
        identity = _ExecutionIdentity(execution, principal)
        if owner is not None:
            self.dependencies.setdefault(owner, set()).add(identity)
        # Existing request validators own this fixed source-binding schema.
        # Only actually consumed sources become dependencies, never new reads.
        request = json.loads(row["request_json"])
        sources = request.get("source_bindings", [])
        if not isinstance(sources, list):
            raise SourceUnavailableError()
        references = {
            SourceIdentity(self.org, SourceKind.RAW_CAPTURE, item["source_id"]) for item in sources
        }
        self.execution_sources.setdefault(identity, set()).update(references)

    def execution_dependency(self, execution: str, principal: str, child: str) -> None:
        if self.sealed:
            raise SourceUnavailableError()
        self.dependencies.setdefault(_ExecutionIdentity(execution, principal), set()).add(
            _ExecutionIdentity(child, principal)
        )

    async def capture_source(
        self, source: SourceIdentity, *, candidate: bool = False
    ) -> NativeSourceCut:
        self.register_source(source, candidate=candidate)
        cut = self.cuts.get(source)
        if cut is None:
            cut = await load_native_source_cut(
                source,
                execute_query=self.graph
                if source.kind is SourceKind.GRAPH_ENTITY
                else self.content,
            )
            previous = self.cuts.setdefault(source, cut)
            if previous.descriptor != cut.descriptor:
                raise SourceUnavailableError()
            cut = previous
        return cut

    async def authorized_snapshot(self, source, authority):
        cut = await self.capture_source(source)
        snapshot = cut.snapshot
        if isinstance(snapshot, RawSourceSnapshot):
            observe_raw_capture(snapshot.memory, authority)
        else:
            observe_graph_snapshot(snapshot, source, authority)
        return snapshot

    async def capture_procedure(self, candidate: str) -> None:
        if self.sealed:
            raise SourceUnavailableError()
        rows = normalize_records(
            await self.content(
                "RETURN {" + _PROCEDURE_CUT + "RETURN $admission_cut; };",
                org=self.org,
                candidate=candidate,
            )
        )
        if len(rows) != 1 or not isinstance(rows[0].get("source_ids"), list):
            raise SourceUnavailableError()
        identifiers = rows[0].get("source_ids")
        if not isinstance(identifiers, list) or any(
            not isinstance(value, str) for value in identifiers
        ):
            raise SourceUnavailableError()
        previous = self.procedure_cuts.setdefault(candidate, rows[0])
        if previous != rows[0]:
            raise SourceUnavailableError()
        self.dependencies.setdefault(
            SourceIdentity(self.org, SourceKind.RAW_CAPTURE, candidate), set()
        ).add(_ProcedureIdentity(candidate))
        self.dependencies.setdefault(_ProcedureIdentity(candidate), set()).update(
            SourceIdentity(self.org, SourceKind.RAW_CAPTURE, identifier)
            for identifier in identifiers
        )
        await asyncio.gather(
            *(
                self.capture_source(
                    SourceIdentity(self.org, SourceKind.RAW_CAPTURE, identifier),
                    candidate=identifier == candidate,
                )
                for identifier in identifiers
            )
        )

    async def finish(self, read: GraphReadValidation) -> None:
        if read.conflicts:
            raise SourceUnavailableError()
        for dependencies in read.dependencies.values():
            if any(source not in self.sources for source in dependencies):
                raise SourceUnavailableError()
        for source, dependencies in read.dependencies.items():
            self.dependencies.setdefault(source, set()).update(dependencies)
        for identity, sources in self.execution_sources.items():
            self.dependencies.setdefault(identity, set()).update(sources & self.sources.keys())
        reached = set()
        pending: list[object] = [self.root]
        while pending:
            current = pending.pop()
            if current not in reached:
                reached.add(current)
                pending.extend(self.dependencies.get(current, ()))
        obligations = (
            set(self.sources)
            | {_ExecutionIdentity(*item) for item in self.executions}
            | {_ProcedureIdentity(candidate) for candidate in self.procedure_cuts}
        )
        if not obligations <= reached:
            raise SourceUnavailableError()
        await asyncio.gather(
            *(
                self.capture_source(source, candidate="candidate" in roles)
                for source, roles in tuple(self.sources.items())
            )
        )
        for source, cut in self.cuts.items():
            snapshot = cut.snapshot
            metadata = (
                snapshot.memory.metadata
                if isinstance(snapshot, RawSourceSnapshot)
                else snapshot.entity.metadata
            )
            if (
                "ordinary" in self.sources[source]
                and cut.association is None
                and any(
                    metadata.get(key)
                    for key in ("raw_source_ids", "source_bindings", "operational_source")
                )
            ):
                raise SourceUnavailableError()
            if isinstance(snapshot, GraphSourceSnapshot) and snapshot.entity.metadata.get(
                "operational_projection"
            ):
                raise SourceUnavailableError()
        for execution_id, principal in sorted(self.executions):
            rows = normalize_records(
                await self.content(
                    "RETURN {" + _EXECUTION_CUT + "RETURN $execution_cut; };",
                    org=self.org,
                    execution_id=execution_id,
                    principal=principal,
                )
            )
            if len(rows) != 1 or set(rows[0]) != {"physical_id", "sha256"}:
                raise SourceUnavailableError()
            physical_id = rows[0]["physical_id"]
            sha256 = rows[0]["sha256"]
            if (
                not isinstance(physical_id, str)
                or not physical_id
                or not isinstance(sha256, str)
                or len(sha256) != 64
                or any(character not in "0123456789abcdef" for character in sha256)
            ):
                raise SourceUnavailableError()
            self.execution_cuts[execution_id, principal] = rows[0]
        self.sealed = True

    async def check(self, *, witness: bool) -> None:
        # All source and extra native cuts are checked without writes first.
        for kind, executor in (
            (SourceKind.RAW_CAPTURE, self.content),
            (SourceKind.GRAPH_ENTITY, self.graph),
        ):
            await check_native_source_cuts(
                [cut for source, cut in self.cuts.items() if source.kind is kind],
                execute_query=executor,
                witness=witness,
            )
        for candidate, expected in self.procedure_cuts.items():
            await self.content(
                "RETURN {"
                + _PROCEDURE_CUT
                + "IF $admission_cut!=$expected { THROW 'publication_admission_changed'; }; RETURN true; };",
                org=self.org,
                candidate=candidate,
                expected=expected,
            )
        for (execution_id, principal), expected in self.execution_cuts.items():
            await self.content(
                "RETURN {"
                + _EXECUTION_CUT
                + "IF $execution_cut!=$expected { THROW 'publication_execution_changed'; };"
                + (
                    "UPDATE $execution.id SET promotion_write_witness=(promotion_write_witness ?? 0)+1;"
                    if witness
                    else ""
                )
                + "RETURN true; };",
                org=self.org,
                execution_id=execution_id,
                principal=principal,
                expected=expected,
            )

    def evidence(
        self, content_scope: NativeStoreScope, graph_scope: NativeStoreScope
    ) -> tuple[PublicationSourceEvidence, ...]:
        result = []
        for source, cut in sorted(self.cuts.items(), key=lambda item: item[0].key):
            scope = graph_scope if source.kind is SourceKind.GRAPH_ENTITY else content_scope
            result.append(
                PublicationSourceEvidence(
                    scope.namespace,
                    scope.database,
                    source,
                    cut.descriptor["row_id"],
                    cut.descriptor["row_sha256"],
                    cut.descriptor["state_id"],
                    cut.descriptor["state_sha256"],
                    cut.descriptor.get("association_id"),
                    cut.descriptor.get("association_sha256"),
                )
            )
        return tuple(result)


async def _target_cut(execute: SurrealExecute, org: str, target: str) -> dict[str, Any]:
    rows = normalize_records(
        await execute(
            """RETURN {
            LET $targets=(SELECT * FROM entity WHERE uuid=$uuid OR id=type::record('entity',$uuid));
            LET $associations=(SELECT * FROM memory_derivations
                WHERE target_kind='graph_entity' AND target_id=$uuid);
            IF array::len($targets)>1 OR array::len($associations)>1 {
                THROW 'publication_target_cardinality';
            };
            IF array::len($associations)=1 AND $associations[0].organization_id!=$org {
                THROW 'publication_target_association';
            };
            RETURN {targets:$targets,associations:$associations,
                canonical:IF array::len($targets)=1 {
                    $targets[0].id=type::record('entity',$uuid)
                    AND $targets[0].uuid=$uuid AND $targets[0].group_id=$org
                } ELSE { false },
                physical_id:IF array::len($targets)=1 { type::string($targets[0].id) } ELSE { NONE }};
        };""",
            org=org,
            uuid=target,
        )
    )
    if len(rows) != 1:
        raise SourceUnavailableError()
    return rows[0]


def _replay_identity(cut, entity: Entity, association: Mapping[str, object]) -> Entity:
    targets, associations = cut["targets"], cut["associations"]
    if len(targets) != 1 or len(associations) != 1 or cut["canonical"] is not True:
        raise SourceUnavailableError()
    stored = entity_from_surreal_row(targets[0])
    prior = associations[0]
    if entity.organization_id is None:
        raise SourceUnavailableError()
    expected_digest = graph_target_digest(
        entity_from_surreal_row(_entity_record(entity, group_id=entity.organization_id))
    )
    if (
        not stored.derivation_required
        or graph_target_digest(stored) != expected_digest
        or prior.get("body_sha256") != expected_digest
        or any(
            prior.get(key) != association.get(key)
            for key in (
                "active",
                "principal_id",
                "authority_ceiling",
                "organization_id",
                "target_kind",
                "target_id",
            )
        )
    ):
        raise SourceUnavailableError()
    before = prior.get("observations")
    after = association.get("observations")
    if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
        raise SourceUnavailableError()
    if any(
        not observation_from_record(a).same_evidence(observation_from_record(b))
        for a, b in zip(before, after, strict=True)
    ):
        raise SourceUnavailableError()
    if (
        entity.metadata.get("reflection_identity") is not None
        or stored.metadata.get("reflection_identity") is not None
    ):
        from sibyl_core.services.memory_identity import verify_reflection_identity

        verify_reflection_identity(entity, stored)
    return stored


async def stage_native_typed_graph_publication(
    transaction: NativeTransaction,
    *,
    content_scope: NativeStoreScope,
    graph_scope: NativeStoreScope,
    entity: Entity,
    derivation: Mapping[str, object],
    resolver: SourceAuthorityResolver,
    promotion: ValidatedPromotion | OrdinaryValidatedPromotion | None = None,
) -> StagedTypedGraphPublication:
    """Collect, fence and stage a typed target; never acknowledge or commit it."""
    try:
        org = content_scope.organization_id
        principal = derivation.get("principal_id")
        if (
            content_scope.store != "content"
            or graph_scope.store != "graph"
            or org != graph_scope.organization_id
            or entity.organization_id != org
            or (content_scope.namespace, content_scope.database)
            == (graph_scope.namespace, graph_scope.database)
            or derivation.get("organization_id") != org
            or derivation.get("target_kind") != "graph_entity"
            or derivation.get("target_id") != entity.id
            or derivation.get("active") is not True
            or not isinstance(principal, str)
            or not principal
            or not callable(resolver)
        ):
            raise SourceUnavailableError()
        if promotion is not None:
            if type(promotion) not in (ValidatedPromotion, OrdinaryValidatedPromotion) or (
                promotion.organization_id != org
                or promotion.principal_id != principal
                or entity.metadata.get("review_capture_id") != promotion.candidate_id
            ):
                raise SourceUnavailableError()
            if (
                isinstance(promotion, OrdinaryValidatedPromotion)
                and promotion.resolver is not resolver
            ):
                raise SourceUnavailableError()
        values = derivation.get("observations")
        if not isinstance(values, list) or not values:
            raise SourceUnavailableError()
        observations = [observation_from_record(value) for value in values]
        target = SourceIdentity(org, SourceKind.GRAPH_ENTITY, entity.id)
        if any(
            not o.durable or o.source.organization_id != org or o.source == target
            for o in observations
        ):
            raise SourceUnavailableError()
        if len({o.source for o in observations}) != len(observations):
            raise SourceUnavailableError()
        if promotion is not None and SourceIdentity(
            org, SourceKind.RAW_CAPTURE, promotion.candidate_id
        ) not in {o.source for o in observations}:
            raise SourceUnavailableError()
        content = transaction.executor(content_scope).execute_query
        graph = transaction.executor(graph_scope).execute_query
        collector = _PublicationCollector(content, graph, org, target)
        read = GraphReadValidation(
            org,
            content_execute_query=content,
            graph_execute_query=graph,
            source_authority_resolver=resolver,
            _publication_collector=collector,
        )
        read.depend_on(target, [observation.source for observation in observations])
        guard, params = ("", {})
        if promotion is not None:
            guard, params = await promotion.current_guard(read=read)
        authority = await read.resolve_authority(org, principal, resolver)
        ceiling = source_authority_ceiling(derivation.get("authority_ceiling"), principal)
        if authority is None or authority.principal_id != principal or ceiling is None:
            raise SourceUnavailableError()
        from dataclasses import replace

        scopes = ceiling.scope_keys
        if authority.scope_keys is not None:
            scopes = authority.scope_keys if scopes is None else scopes & authority.scope_keys
        authority = replace(
            authority,
            projects=authority.projects & ceiling.projects,
            teams=authority.teams & ceiling.teams,
            delegations=authority.delegations & ceiling.delegations,
            scope_keys=scopes,
        )
        # Named owners verify their pending candidate under its admission rules.
        ordinary = [
            o
            for o in observations
            if promotion is None
            or o.source.id != promotion.candidate_id
            or o.source.kind is not SourceKind.RAW_CAPTURE
        ]
        if not await validate_observations(
            ordinary, authority, organization_id=org, ancestors=frozenset({target}), read=read
        ):
            raise SourceUnavailableError()
        if promotion is not None:
            candidate = await collector.capture_source(
                SourceIdentity(org, SourceKind.RAW_CAPTURE, promotion.candidate_id), candidate=True
            )
            expected = next(o for o in observations if o.source == candidate.source)
            if not expected.same_evidence(candidate.snapshot.observation):
                raise SourceUnavailableError()
        before = await _target_cut(graph, org, entity.id)
        if before["targets"] or before["associations"]:
            _replay_identity(before, entity, derivation)
            await collector.capture_source(target)
        await collector.finish(read)
        await collector.check(witness=False)
        if guard and promotion is not None:
            await content(
                "RETURN {" + guard + "RETURN true; };",
                organization_id=org,
                uuid=promotion.candidate_id,
                **params,
            )
        await collector.check(witness=True)
        from sibyl_core.services.graph_entity_store import _insert_entity_if_absent

        await _insert_entity_if_absent(
            None, entity, group_id=org, derivation=derivation, execute_query=graph
        )
        after = await _target_cut(graph, org, entity.id)
        stored = _replay_identity(after, entity, derivation)
        return StagedTypedGraphPublication(
            stored,
            not bool(before["targets"]),
            after["physical_id"],
            graph_target_digest(stored),
            collector.evidence(content_scope, graph_scope),
        )
    except BaseException:
        transaction.invalidate()
        raise
