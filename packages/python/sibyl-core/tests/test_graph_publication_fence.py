"""Denied inputs, reader resolver ownership and rooted collector obligations."""

from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal.native_transaction import NativeStoreScope
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, SourceObservation
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph_publication_fence import (
    _PublicationCollector,
    stage_native_typed_graph_publication,
)
from sibyl_core.services.graph_read_validation import GraphReadValidation
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.ordinary_publication import OrdinaryValidatedPromotion
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.validation_promotion import ValidationBinding


def inputs():
    org = str(uuid4())
    content = NativeStoreScope(
        "content",
        "content_ns",
        "content",
        org,
        ("raw_captures", "source_states", "memory_derivations"),
    )
    graph = NativeStoreScope(
        "graph", "graph_ns", "graph", org, ("entity", "source_states", "memory_derivations")
    )
    entity = Entity(
        id="target",
        entity_type=EntityType.NOTE,
        name="Target",
        content="Evidence",
        organization_id=org,
        metadata={"memory_scope": "private", "principal_id": "owner"},
    )
    observation = SourceObservation(
        SourceIdentity(org, SourceKind.RAW_CAPTURE, "candidate"),
        1,
        "a" * 64,
        1,
        True,
        "incarnation",
    )
    derivation = {
        "organization_id": org,
        "target_kind": "graph_entity",
        "target_id": entity.id,
        "active": True,
        "principal_id": "owner",
        "authority_ceiling": SourceReadAuthority("owner").ceiling_metadata(),
        "observations": [asdict(observation)],
    }
    return org, content, graph, entity, derivation


class NoIO:
    def __init__(self):
        self.invalidated = False
        self.attempted = []

    def executor(self, scope):
        self.attempted.append(scope)
        raise AssertionError("Denied stage acquired an executor")

    def invalidate(self):
        self.invalidated = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["aliased_scope", "foreign_org", "promotion_resolver", "duplicate_observation"]
)
async def test_publication_fence_input_denial_before_io(failure):
    org, content, graph, entity, derivation = inputs()
    transaction = NoIO()

    async def resolver(org, principal):
        raise AssertionError("Denied stage resolved authority")

    promotion = None
    if failure == "aliased_scope":
        graph = NativeStoreScope(
            "graph", content.namespace, content.database, org, graph.required_tables
        )
    elif failure == "foreign_org":
        entity.organization_id = str(uuid4())
    elif failure == "duplicate_observation":
        derivation["observations"] *= 2
    else:

        async def divergent(org, principal):
            raise AssertionError("Divergent promotion resolver ran")

        promotion = OrdinaryValidatedPromotion(
            org,
            "owner",
            "candidate",
            ValidationBinding(
                execution_id="a" * 64,
                request_sha256="a" * 64,
                result_sha256="b" * 64,
                input_sha256="c" * 64,
            ),
            lambda: None,
            divergent,
        )
    with pytest.raises(SourceUnavailableError):
        await stage_native_typed_graph_publication(
            transaction,
            content_scope=content,
            graph_scope=graph,
            entity=entity,
            derivation=derivation,
            resolver=resolver,
            promotion=promotion,
        )
    assert transaction.invalidated and transaction.attempted == []


@pytest.mark.asyncio
async def test_publication_fence_resolver_identity_precedes_cached_authority():
    calls = []

    async def resolver(org, principal):
        calls.append((org, principal))
        return SourceReadAuthority(principal)

    async def divergent(org, principal):
        raise AssertionError("Divergent resolver ran")

    org = str(uuid4())
    read = GraphReadValidation(org, source_authority_resolver=resolver)
    assert (await read.resolve_authority(org, "owner", resolver)).principal_id == "owner"
    with pytest.raises(SourceUnavailableError):
        await read.resolve_authority(org, "owner", divergent)
    assert calls == [(org, "owner")]
    with pytest.raises(AttributeError):
        read.source_authority_resolver = divergent


@pytest.mark.asyncio
async def test_publication_fence_unrelated_registration_rejects_before_native_cut():
    org = str(uuid4())
    root = SourceIdentity(org, SourceKind.GRAPH_ENTITY, "target")
    unrelated = SourceIdentity(org, SourceKind.RAW_CAPTURE, "unrelated")

    async def no_query(*args, **kwargs):
        raise AssertionError("Unreachable obligation reached native cut")

    collector = _PublicationCollector(no_query, no_query, org, root)
    collector.register_source(unrelated)
    read = GraphReadValidation(org, _publication_collector=collector)
    with pytest.raises(SourceUnavailableError):
        await collector.finish(read)


def test_publication_fence_candidate_role_retains_ordinary_obligation():
    org = str(uuid4())
    root = SourceIdentity(org, SourceKind.GRAPH_ENTITY, "target")
    source = SourceIdentity(org, SourceKind.RAW_CAPTURE, "candidate")
    collector = _PublicationCollector(None, None, org, root)
    collector.register_source(source, candidate=True)
    collector.register_source(source)
    assert collector.sources[source] == {"candidate", "ordinary"}


@pytest.mark.asyncio
async def test_publication_fence_named_ordinary_resolver_denial_before_authorization():
    from unittest.mock import AsyncMock

    from sibyl_core.services.ordinary_publication import ordinary_promotion_binding
    from sibyl_core.services.validation_execution import ValidationExecutionUnavailable

    org, _, _, _, _ = inputs()

    async def resolver(org, principal):
        raise AssertionError("Authority resolved before identity denial")

    async def divergent(org, principal):
        raise AssertionError("Divergent resolver used")

    authorize = AsyncMock()
    read = GraphReadValidation(org, source_authority_resolver=resolver)
    promotion = OrdinaryValidatedPromotion(
        org,
        "owner",
        "candidate",
        ValidationBinding(
            execution_id="a" * 64,
            request_sha256="a" * 64,
            result_sha256="b" * 64,
            input_sha256="c" * 64,
        ),
        authorize,
        divergent,
    )
    with pytest.raises(ValidationExecutionUnavailable, match="resolver differs"):
        await promotion.current_guard(read=read)
    with pytest.raises(ValidationExecutionUnavailable, match="resolver differs"):
        await ordinary_promotion_binding(
            org, "owner", "candidate", "a" * 64, divergent, authorize, read=read
        )
    authorize.assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_fence_default_origin_missing_row_preserves_unavailable():
    from sibyl_core.services.validation_origin import load_validation_origin

    calls = []

    async def empty(query, **params):
        calls.append((query, params))
        return []

    org = str(uuid4())
    read = GraphReadValidation(org, content_execute_query=empty, graph_execute_query=empty)
    with pytest.raises(SourceUnavailableError):
        await load_validation_origin(
            {"organization_id": org, "principal_id": "owner", "origin_execution_id": "a" * 64},
            read=read,
        )
    assert len(calls) == 1
