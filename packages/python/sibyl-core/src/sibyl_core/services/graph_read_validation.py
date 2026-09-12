"""Explicit, single-read reuse of current ancestry inputs.

The owner creates a new instance for each validation pass. No proof survives a
read boundary, and callers still evaluate ancestry cycles for their own path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Hashable, Sequence
from typing import Any

from sibyl_core.memory_pipeline.observations import SourceIdentity
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.source_observations import SourceUnavailableError


class GraphReadValidation:
    """Coalesce identical input reads within one organization's validation pass."""

    def __init__(self, organization_id: str) -> None:
        self.organization_id = organization_id
        self._inputs: dict[Hashable, asyncio.Task[Any]] = {}
        self._validated_graph: dict[str, bool] = {}
        self._graph_loads: dict[str, asyncio.Task[dict[str, bool]]] = {}

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
        return await self._once(
            ("authority", organization_id, principal_id),
            lambda: resolver(organization_id, principal_id),
        )

    async def source_snapshot(self, source: SourceIdentity, authority: SourceReadAuthority):
        from sibyl_core.services.observed_sources import load_authorized_source_snapshot

        self._check_org(source.organization_id)
        return await self._once(
            ("source", source, authority),
            lambda: load_authorized_source_snapshot(
                source, authority, organization_id=self.organization_id
            ),
        )

    async def raw_association(self, organization_id: str, memory_id: str):
        from sibyl_core.services.memory_derivations import load_raw_derivation

        self._check_org(organization_id)
        return await self._once(
            ("raw_association", organization_id, memory_id),
            lambda: load_raw_derivation(organization_id, memory_id),
        )

    async def prepare_graph(self, entity_ids: Sequence[str]) -> None:
        from sibyl_core.services.validation_promotion import validated_graph_currents

        identifiers = set(entity_ids)
        missing = sorted(identifiers - self._graph_loads.keys())
        if missing:
            # Reserve every key before yielding, so overlapping batches share
            # the same snapshot without serializing unrelated graph inputs.
            task = asyncio.create_task(validated_graph_currents(self.organization_id, missing))
            self._graph_loads.update(dict.fromkeys(missing, task))
        for task in {self._graph_loads[identifier] for identifier in identifiers}:
            self._validated_graph.update(await asyncio.shield(task))

    async def graph_validated(self, organization_id: str, entity_id: str) -> bool:
        self._check_org(organization_id)
        await self.prepare_graph([entity_id])
        return self._validated_graph[entity_id]

    async def association_proof(self, entity, association, ancestors, check):
        from sibyl_core.backends.surreal.records import normalize_record
        from sibyl_core.memory_pipeline.observations import evidence_hash
        from sibyl_core.services.graph_records import _jsonable

        self._check_org(entity.organization_id)
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
