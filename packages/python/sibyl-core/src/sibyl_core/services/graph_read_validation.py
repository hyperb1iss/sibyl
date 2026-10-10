"""Explicit, single-read reuse of current ancestry inputs.

The owner creates a new instance for each validation pass. No proof survives a
read boundary, and callers still evaluate ancestry cycles for their own path.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Coroutine, Hashable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.services.memory_source_validation import (
    SourceAuthorityResolver,
    SourceReadAuthority,
)
from sibyl_core.services.source_observations import SourceUnavailableError

if TYPE_CHECKING:
    from sibyl_core.services.graph_publication_fence import _PublicationCollector


# Marks evidence the caller did not compute, since None is a valid evidence.
_UNCOMPUTED: Any = object()


@dataclass(frozen=True, slots=True)
class GraphReadFootprint:
    """A frozen copy of one validation pass's recorded reads.

    A worker thread compares final snapshots against the footprint while
    the pass's own tasks may still record on the loop; the copy is what it
    reads, so no dictionary changes under it.
    """

    organization_id: str
    graph_rows: Mapping[SourceIdentity, object]
    graph_ancestry: Mapping[SourceIdentity, object]
    raw_rows: Mapping[SourceIdentity, object]
    raw_content_digests: Mapping[SourceIdentity, object]
    associations: Mapping[SourceIdentity, object]
    observations: Mapping[SourceIdentity, Any]
    dependencies: Mapping[SourceIdentity, frozenset[SourceIdentity]]
    conflicts: frozenset[SourceIdentity]

    def affected(self, source: SourceIdentity, changed: set[SourceIdentity]) -> bool:
        return _affected(self.dependencies, source, changed)


def _affected(
    dependencies: Mapping[SourceIdentity, Iterable[SourceIdentity]],
    source: SourceIdentity,
    changed: set[SourceIdentity],
) -> bool:
    pending = [source]
    seen = set()
    while pending:
        current = pending.pop()
        if current in changed:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(dependencies.get(current, ()))
    return False


def entity_evidence_values(entities: Sequence[Any], *, ancestry: bool = False) -> list[Any]:
    """Each entity's recorded evidence, or None where computing it raised.

    Pure, so a worker thread can run it. record_entity_evidence recomputes
    a None on the loop, where the error raises exactly as record_entity's.
    """
    from sibyl_core.services.graph_records import _jsonable

    values: list[Any] = []
    for entity in entities:
        try:
            values.append(_jsonable(entity_read_evidence(entity, ancestry=ancestry)))
        except Exception:
            values.append(None)
    return values


@dataclass(frozen=True, slots=True)
class AssociationProofInputs:
    """The CPU-heavy parts of association_proof, computed off the loop."""

    entity_evidence: Any
    association_evidence: Any
    identity: str


def association_proof_inputs(entity, association) -> AssociationProofInputs | None:
    """What association_proof derives from its row pair, or None where that raised.

    Pure, so a worker thread can run it; a None proof takes the loop path,
    which raises where association_proof always has.
    """
    from sibyl_core.memory_pipeline.observations import evidence_hash
    from sibyl_core.services.graph_records import _jsonable

    try:
        association_evidence = association_read_evidence(association)
        return AssociationProofInputs(
            entity_evidence=_jsonable(entity_read_evidence(entity)),
            association_evidence=_jsonable(association_evidence),
            identity=evidence_hash(
                _jsonable(
                    {
                        "entity": entity.model_dump(mode="json"),
                        "derivation_required": entity.derivation_required,
                        "observed_revision": entity.observed_revision,
                        "association": association_evidence,
                    }
                )
            ),
        )
    except Exception:
        return None


class _PublicationExecutionKwargs(TypedDict, total=False):
    publication_read: GraphReadValidation
    publication_owner: SourceIdentity | None


class GraphReadValidation:
    """Coalesce input reads within one organization's validation pass.

    Paired readers cover stored validation, recursive ancestry, and availability
    checks over supplied entity rows. Outer entity and relationship materializers
    reject this explicit mode. Callers own reader lifetime, store affinity, and
    current authorization; selecting readers does not redirect any writer or
    make writer operations atomic.
    """

    def __init__(
        self,
        organization_id: str,
        *,
        content_execute_query: SurrealExecute | None = None,
        graph_execute_query: SurrealExecute | None = None,
        source_authority_resolver: SourceAuthorityResolver | None = None,
        _publication_collector: _PublicationCollector | None = None,
    ) -> None:
        if (content_execute_query is None) != (graph_execute_query is None):
            raise ValueError("validation requires both content and graph readers")
        self.organization_id = organization_id
        self._source_authority_resolver = source_authority_resolver
        self._publication_collector = _publication_collector
        self._content_execute_query = content_execute_query
        self._graph_execute_query = graph_execute_query
        self._inputs: dict[Hashable, asyncio.Task[Any]] = {}
        self._validated_graph: dict[str, bool] = {}
        self._graph_loads: dict[str, asyncio.Task[dict[str, bool]]] = {}
        self.graph_rows: dict[SourceIdentity, object] = {}
        self.graph_ancestry: dict[SourceIdentity, object] = {}
        self.raw_rows: dict[SourceIdentity, object] = {}
        self.raw_content_digests: dict[SourceIdentity, object] = {}
        self.associations: dict[SourceIdentity, object] = {}
        self.observations: dict[SourceIdentity, Any] = {}
        self.dependencies: dict[SourceIdentity, set[SourceIdentity]] = {}
        self.conflicts: set[SourceIdentity] = set()

    @property
    def source_authority_resolver(self) -> SourceAuthorityResolver | None:
        """Operation-selected resolver; recursive callers may not replace it."""
        return self._source_authority_resolver

    @property
    def content_execute_query(self) -> SurrealExecute | None:
        """Selected content reader for this validation pass."""
        return self._content_execute_query

    @property
    def graph_execute_query(self) -> SurrealExecute | None:
        """Selected graph reader; callable ownership remains with the caller."""
        return self._graph_execute_query

    async def _once[T](self, key: Hashable, load: Callable[[], Coroutine[Any, Any, T]]) -> T:
        task = self._inputs.get(key)
        if task is None:
            task = asyncio.create_task(load())
            self._inputs[key] = task
        return await task

    def _check_org(self, organization_id: str) -> None:
        if organization_id != self.organization_id:
            raise SourceUnavailableError()

    async def resolve_authority(self, organization_id: str, principal_id: str, resolver):
        self._check_org(organization_id)
        if (
            self.source_authority_resolver is not None
            and resolver is not self.source_authority_resolver
        ):
            raise SourceUnavailableError()
        return await self._once(
            ("authority", organization_id, principal_id),
            lambda: resolver(organization_id, principal_id),
        )

    async def source_snapshot(self, source: SourceIdentity, authority: SourceReadAuthority):
        from sibyl_core.services.observed_sources import load_authorized_source_snapshot
        from sibyl_core.services.source_state_store import RawSourceSnapshot

        self._check_org(source.organization_id)
        if self._publication_collector is not None:
            snapshot = await self._publication_collector.authorized_snapshot(source, authority)
        else:
            snapshot = await self._once(
                ("source", source, authority),
                lambda: load_authorized_source_snapshot(
                    source,
                    authority,
                    organization_id=self.organization_id,
                    **(
                        {
                            "execute_query": self.content_execute_query
                            if source.kind is SourceKind.RAW_CAPTURE
                            else self.graph_execute_query
                        }
                        if self.content_execute_query is not None
                        else {}
                    ),
                ),
            )
        self.record_observation(snapshot.observation)
        if isinstance(snapshot, RawSourceSnapshot):
            self.record_capture(snapshot.memory)
            self._remember(
                self.raw_content_digests,
                source,
                hashlib.sha256(snapshot.memory.raw_content.encode()).hexdigest(),
            )
        else:
            self.record_entity(snapshot.entity)
        return snapshot

    async def _publication_source(self, source: SourceIdentity, *, candidate: bool = False) -> None:
        self._check_org(source.organization_id)
        if self._publication_collector is not None:
            await self._publication_collector.capture_source(source, candidate=candidate)

    def _publication_execution_kwargs(
        self, owner: SourceIdentity | None = None
    ) -> _PublicationExecutionKwargs:
        return (
            {"publication_read": self, "publication_owner": owner}
            if self._publication_collector is not None
            else {}
        )

    def _publication_execution(
        self, execution_id: str, principal: str, row, owner: SourceIdentity | None = None
    ) -> None:
        if self._publication_collector is not None:
            self._publication_collector.register_execution(execution_id, principal, row, owner)

    def _publication_execution_dependency(
        self, execution_id: str, principal: str, child: str
    ) -> None:
        if self._publication_collector is not None:
            self._publication_collector.execution_dependency(execution_id, principal, child)

    async def _publication_procedure(self, candidate: str) -> None:
        if self._publication_collector is not None:
            await self._publication_collector.capture_procedure(candidate)

    async def raw_association(self, organization_id: str, memory_id: str):
        from sibyl_core.services.memory_derivations import load_raw_derivation

        self._check_org(organization_id)
        association = await self._once(
            ("raw_association", organization_id, memory_id),
            lambda: load_raw_derivation(
                organization_id,
                memory_id,
                **(
                    {"execute_query": self.content_execute_query}
                    if self.content_execute_query is not None
                    else {}
                ),
            ),
        )
        self.record_association(
            SourceIdentity(organization_id, SourceKind.RAW_CAPTURE, memory_id), association
        )
        return association

    async def prepare_graph(self, entity_ids: Sequence[str]) -> None:
        from sibyl_core.services.validation_promotion import validated_graph_currents

        identifiers = set(entity_ids)
        missing = sorted(identifiers - self._graph_loads.keys())
        if missing:
            # Reserve every key before yielding, so overlapping batches share
            # the same snapshot without serializing unrelated graph inputs.
            task = asyncio.create_task(
                validated_graph_currents(
                    self.organization_id,
                    missing,
                    **({"read": self} if self.content_execute_query is not None else {}),
                )
            )
            self._graph_loads.update(dict.fromkeys(missing, task))
        for task in {self._graph_loads[identifier] for identifier in identifiers}:
            self._validated_graph.update(await asyncio.shield(task))

    async def graph_validated(self, organization_id: str, entity_id: str) -> bool:
        self._check_org(organization_id)
        await self.prepare_graph([entity_id])
        return self._validated_graph[entity_id]

    async def association_proof(
        self,
        entity,
        association,
        ancestors,
        check,
        *,
        inputs: AssociationProofInputs | None = None,
    ):
        from sibyl_core.backends.surreal.records import normalize_record
        from sibyl_core.memory_pipeline.observations import evidence_hash
        from sibyl_core.services.graph_records import _jsonable

        self._check_org(entity.organization_id)
        source = SourceIdentity(self.organization_id, SourceKind.GRAPH_ENTITY, entity.id)
        if inputs is not None:
            self.record_entity_evidence(entity, inputs.entity_evidence)
            self.record_association(source, association, evidence=inputs.association_evidence)
            identity = inputs.identity
        else:
            self.record_entity(entity)
            self.record_association(source, association)
            normalized = normalize_record(association) if association is not None else None
            if normalized is not None:
                normalized.pop("validation_write_witness", None)
            identity = evidence_hash(
                _jsonable(
                    {
                        "entity": entity.model_dump(mode="json"),
                        "derivation_required": entity.derivation_required,
                        "observed_revision": entity.observed_revision,
                        "association": normalized,
                    }
                )
            )
        return await self._once(("association_proof", identity, ancestors), check)

    async def observation_proof(self, observations, authority, ancestors, check):
        for observation in observations:
            self._check_org(observation.source.organization_id)
        return await self._once(
            ("observation_proof", tuple(observations), authority, ancestors), check
        )

    def _remember(self, records, source: SourceIdentity, evidence) -> None:
        from sibyl_core.services.graph_records import _jsonable

        self._check_org(source.organization_id)
        self._remember_value(records, source, _jsonable(evidence))

    def _remember_value(self, records, source: SourceIdentity, value) -> None:
        if source in records and records[source] != value:
            self.conflicts.add(source)
        else:
            records[source] = value

    def depend_on(self, source: SourceIdentity, dependencies) -> None:
        """Keep source-specific dependencies even when their proofs coalesce."""
        self._check_org(source.organization_id)
        for dependency in dependencies:
            self._check_org(dependency.organization_id)
            self.dependencies.setdefault(source, set()).add(dependency)

    def record_entity(self, entity, *, ancestry: bool = False) -> None:
        self._check_org(entity.organization_id)
        source = SourceIdentity(self.organization_id, SourceKind.GRAPH_ENTITY, entity.id)
        if self._publication_collector is not None:
            self._publication_collector.register_source(source)
        evidence = entity_read_evidence(entity, ancestry=ancestry)
        self._remember(self.graph_ancestry if ancestry else self.graph_rows, source, evidence)

    def record_entity_evidence(self, entity, evidence: Any, *, ancestry: bool = False) -> None:
        """record_entity with its evidence from entity_evidence_values."""
        if evidence is None:
            self.record_entity(entity, ancestry=ancestry)
            return
        self._check_org(entity.organization_id)
        source = SourceIdentity(self.organization_id, SourceKind.GRAPH_ENTITY, entity.id)
        if self._publication_collector is not None:
            self._publication_collector.register_source(source)
        self._remember_value(self.graph_ancestry if ancestry else self.graph_rows, source, evidence)

    async def record_entities(self, entities: Sequence[Any], *, ancestry: bool = False) -> None:
        """record_entity for each row, with the evidence computed off the loop.

        Rows are recorded in order, and a row that would have raised raises
        at the same position, after the rows before it are recorded.
        """
        from sibyl_core.services.graph_compute import compute_rows

        if not entities:
            return
        values = await compute_rows(
            len(entities), entity_evidence_values, list(entities), ancestry=ancestry
        )
        for entity, evidence in zip(entities, values, strict=True):
            self.record_entity_evidence(entity, evidence, ancestry=ancestry)

    def record_capture(self, memory) -> None:
        self._check_org(memory.organization_id)
        source = SourceIdentity(self.organization_id, SourceKind.RAW_CAPTURE, memory.id)
        if self._publication_collector is not None:
            self._publication_collector.register_source(source)
        self._remember(self.raw_rows, source, capture_read_evidence(memory))

    def record_association(
        self, source: SourceIdentity, association, *, evidence: Any = _UNCOMPUTED
    ) -> None:
        from sibyl_core.services.memory_derivations import observation_from_record

        if self._publication_collector is not None:
            self._publication_collector.register_source(source)
        if evidence is _UNCOMPUTED:
            self._remember(self.associations, source, association_read_evidence(association))
        else:
            self._check_org(source.organization_id)
            self._remember_value(self.associations, source, evidence)
        if association is not None:
            for value in association.get("observations", []):
                try:
                    observation = observation_from_record(value)
                    self.depend_on(source, [observation.source])
                except (TypeError, ValueError):
                    self.conflicts.add(source)

    def record_observation(self, observation) -> None:
        source = observation.source
        self._check_org(source.organization_id)
        previous = self.observations.get(source)
        if previous is not None and not previous.same_evidence(observation):
            self.conflicts.add(source)
        else:
            self.observations[source] = observation

    def affected(self, source: SourceIdentity, changed: set[SourceIdentity]) -> bool:
        """Walk only this source's closure; a peer failure does not deny it."""
        return _affected(self.dependencies, source, changed)

    def footprint(self) -> GraphReadFootprint:
        """Freeze what this pass has recorded so far, for a worker thread."""
        return GraphReadFootprint(
            organization_id=self.organization_id,
            graph_rows=dict(self.graph_rows),
            graph_ancestry=dict(self.graph_ancestry),
            raw_rows=dict(self.raw_rows),
            raw_content_digests=dict(self.raw_content_digests),
            associations=dict(self.associations),
            observations=dict(self.observations),
            dependencies={
                source: frozenset(values) for source, values in self.dependencies.items()
            },
            conflicts=frozenset(self.conflicts),
        )


def _semantic_metadata(metadata):
    return {
        key: value
        for key, value in metadata.items()
        if key
        not in {
            "record_id",
            "embedding",
            "name_embedding",
            "fact_embedding",
            "embedding_metadata",
            "operational_write_witness",
            "validation_write_witness",
        }
    }


def entity_read_evidence(entity, *, ancestry: bool = False):
    """Compare audience and lifecycle for ancestors without omitted prose."""
    body = entity.model_dump(mode="json", exclude={"embedding", "metadata"})
    if ancestry:
        for key in ("name", "description", "content"):
            body.pop(key, None)
    body["metadata"] = _semantic_metadata(entity.metadata)
    if ancestry:
        body["metadata"].pop("content", None)
    body["derivation_required"] = entity.derivation_required
    body["observed_revision"] = entity.observed_revision
    return body


def capture_read_evidence(memory):
    """Canonical stored policy and lifecycle, without fetching source prose."""
    body = asdict(memory)
    for key in (
        "raw_content",
        "embedding",
        "score",
        "snippet",
        "last_recalled_at",
        "last_used_at",
        "retrieval_count",
        "citation_count",
        "misled_count",
    ):
        body.pop(key, None)
    body["metadata"] = _semantic_metadata(memory.metadata)
    return body


def association_read_evidence(association):
    from sibyl_core.backends.surreal.records import normalize_record

    normalized = normalize_record(association) if association is not None else None
    if normalized is not None:
        normalized.pop("validation_write_witness", None)
    return normalized
