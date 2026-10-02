"""Real native full-stage insertion, native evidence and conflict causality."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from surrealdb.connections.async_ws import AsyncWsSurrealConnection

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.native_transaction import (
    NativeCommitOutcome,
    NativeCredentialProfile,
    NativeStoreScope,
    NativeTransactionAuthorization,
    NativeTransactionBinding,
    open_native_transaction,
)
from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import content_client, graph_derivations, graph_entity_store
from sibyl_core.services.content_raw_persistence import remember_raw_memory
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_publication_fence import stage_native_typed_graph_publication
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.source_state_store import load_native_source_cut


@pytest_asyncio.fixture
async def native_typed_stores(tmp_path, monkeypatch):
    from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema

    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL")
    if not url:
        pytest.skip("native publication conflict proof requires independent native sockets")
    username = os.environ["SIBYL_ARCHIVE_TEST_SURREAL_USERNAME"]
    password = os.environ["SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD"]
    org = str(uuid4())
    content_ns = "native_typed_author_content_" + uuid4().hex
    graph_prefix = "native_typed_author_graph_"
    graph_ns = graph_prefix + org.replace("-", "")
    names = [content_ns, graph_ns]
    registry = Path(
        os.environ.get("SIBYL_TYPED_PUBLICATION_REGISTRY", str(tmp_path / "registry.jsonl"))
    )
    registry.parent.mkdir(parents=True, exist_ok=True)

    def record(**evidence):
        with registry.open("a") as stream:
            stream.write(json.dumps({"namespaces": names, **evidence}, sort_keys=True) + "\n")

    record(phase="registered", organization_id=org)
    root = AsyncWsSurrealConnection(url)
    content = SurrealContentClient(
        url=url,
        username=username,
        password=password,
        namespace=content_ns,
        database="content",
        pool_size=4,
    )
    graph = SurrealGraphClient(
        group_id=org,
        url=url,
        username=username,
        password=password,
        namespace_prefix=graph_prefix,
        pool_size=4,
    )
    try:
        await root.connect()
        await root.signin({"username": username, "password": password})
        before = await root.query("INFO FOR ROOT;")
        assert not set(names) & set(before["namespaces"])
        record(phase="before", present=[])
        for name in names:
            await root.query("DEFINE NAMESPACE " + name + ";")
        await bootstrap_content_schema(content)
        await prepare_graph_schema(graph)

        @asynccontextmanager
        async def session():
            yield content

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        raw = await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id="seed",
            raw_content="Source evidence",
            memory_scope="private",
            scope_key="owner",
            embedding_provider=None,
        )
        content_scope = NativeStoreScope(
            "content",
            content_ns,
            "content",
            org,
            ("raw_captures", "source_states", "memory_derivations"),
        )
        graph_scope = NativeStoreScope(
            "graph", graph_ns, "graph", org, ("entity", "source_states", "memory_derivations")
        )
        binding = NativeTransactionBinding(
            url,
            "owned-synthetic-provider",
            "typed-publication",
            "owned-synthetic-root",
            (content_scope, graph_scope),
        )

        async def authorize():
            return NativeTransactionAuthorization(binding, "owner", "fresh-synthetic-decision")

        async def credentials(profile):
            return NativeCredentialProfile(profile, username, password)

        @asynccontextmanager
        async def transaction():
            async with open_native_transaction(
                binding, authorize=authorize, credentials=credentials
            ) as tx:
                yield tx

        async def resolver(organization, principal):
            assert organization == org
            return SourceReadAuthority(principal)

        source = SourceIdentity(org, SourceKind.RAW_CAPTURE, raw.id)
        cut = await load_native_source_cut(source, execute_query=content.execute_query)

        def proposal(target="target"):
            entity = Entity(
                id=target,
                name="Target",
                content="Derived evidence",
                entity_type=EntityType.NOTE,
                organization_id=org,
                metadata={"memory_scope": "private", "principal_id": "owner"},
            )
            derivation = {
                "organization_id": org,
                "target_kind": "graph_entity",
                "target_id": target,
                "active": True,
                "principal_id": "owner",
                "authority_ceiling": SourceReadAuthority("owner").ceiling_metadata(),
                "observations": [asdict(cut.snapshot.observation)],
            }
            return entity, derivation

        async def stage(tx, target="target"):
            entity, derivation = proposal(target)
            return await stage_native_typed_graph_publication(
                tx,
                content_scope=content_scope,
                graph_scope=graph_scope,
                entity=entity,
                derivation=derivation,
                resolver=resolver,
            )

        yield SimpleNamespace(
            org=org,
            raw=raw,
            source=source,
            cut=cut,
            content=content,
            graph=graph,
            content_scope=content_scope,
            graph_scope=graph_scope,
            transaction=transaction,
            resolver=resolver,
            proposal=proposal,
            stage=stage,
            record=record,
        )
    finally:
        await content.close()
        await graph.close()
        try:
            for name in names:
                await root.query("REMOVE NAMESPACE " + name + ";")
            after = await root.query("INFO FOR ROOT;")
            present = sorted(set(names) & set(after["namespaces"]))
            record(phase="cleanup", present=present)
            assert present == []
        finally:
            await root.close()
        observer = AsyncWsSurrealConnection(url)
        await observer.connect()
        try:
            await observer.signin({"username": username, "password": password})
            catalog = await observer.query("INFO FOR ROOT;")
            present = sorted(set(names) & set(catalog["namespaces"]))
            record(phase="fresh-root-cleanup-audit", present=present, sdk_use_called=False)
            assert present == []
        finally:
            await observer.close()


@pytest.mark.asyncio
async def test_native_typed_stage_owner_commit_and_exact_replay(native_typed_stores, monkeypatch):
    s = native_typed_stores

    def forbidden():
        raise AssertionError("Explicit stage used global resolver")

    monkeypatch.setattr(graph_derivations, "get_source_authority_resolver", forbidden)
    async with s.transaction() as tx:
        staged = await s.stage(tx)
        assert staged.created and staged.physical_id
        assert len(staged.sources) == 1 and staged.sources[0].source == s.source
        assert (
            normalize_records(
                await s.graph.execute_query("SELECT * FROM entity WHERE uuid='target';")
            )
            == []
        )
        assert tx.commit_outcome is NativeCommitOutcome.NOT_REQUESTED
        await tx.commit()
    async with s.transaction() as tx:
        replay = await s.stage(tx)
        assert not replay.created and replay.body_sha256 == staged.body_sha256
        assert replay.entity.id == staged.entity.id
        await tx.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [RuntimeError("post-insert"), asyncio.CancelledError("post-insert")],
    ids=["exception", "cancellation"],
)
async def test_native_typed_post_insert_failure_cannot_commit(
    native_typed_stores, monkeypatch, failure
):
    s = native_typed_stores
    original = graph_entity_store._insert_entity_if_absent

    async def fail_after(*args, **kwargs):
        await original(*args, **kwargs)
        raise failure

    monkeypatch.setattr(graph_entity_store, "_insert_entity_if_absent", fail_after)
    async with s.transaction() as tx:
        with pytest.raises(type(failure), match="post-insert"):
            await s.stage(tx)
        with pytest.raises(Exception, match="not ready"):
            await tx.commit()
    assert (
        normalize_records(await s.graph.execute_query("SELECT * FROM entity WHERE uuid='target';"))
        == []
    )


@pytest.mark.asyncio
async def test_native_typed_native_unknown_values_hash_before_normalization(native_typed_stores):
    s = native_typed_stores
    await s.content.execute_query(
        "UPDATE raw_captures SET metadata.native_clock=<datetime>'2026-10-02T01:02:03.123456789Z',"
        "metadata.native_null=NULL,metadata.native_none=NONE,"
        "metadata.native_record=type::record('nested',['id',17]) WHERE uuid=$uuid;",
        uuid=s.raw.id,
    )
    expected = normalize_records(
        await s.content.execute_query(
            "RETURN {sha256:crypto::sha256(type::string((SELECT * FROM raw_captures WHERE uuid=$uuid)[0]))};",
            uuid=s.raw.id,
        )
    )[0]["sha256"]
    cut = await load_native_source_cut(s.source, execute_query=s.content.execute_query)
    assert cut.descriptor["row_sha256"] == expected
    async with s.transaction() as tx:
        staged = await s.stage(tx)
        assert staged.sources[0].row_sha256 == expected
        await tx.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["publication", "retirement"])
async def test_native_typed_retirement_real_witness_both_winner_orders(native_typed_stores, first):
    s = native_typed_stores
    staged = asyncio.Event()
    retired = asyncio.Event()
    committed = asyncio.Event()
    results = {}

    async def publish():
        async with s.transaction() as tx:
            await s.stage(tx)
            staged.set()
            await asyncio.wait_for(retired.wait(), 5)
            if first == "retirement":
                await asyncio.wait_for(committed.wait(), 5)
            try:
                await tx.commit()
                results["publication"] = "committed"
            except Exception as failure:
                results["publication"] = tx.commit_outcome.value
                results["publication_error"] = str(failure)
            if first == "publication":
                committed.set()

    async def retire():
        await asyncio.wait_for(staged.wait(), 5)
        async with s.transaction() as tx:
            await tx.executor(s.content_scope).execute_query(
                "UPDATE raw_captures SET deleted_at=time::now(),revision+=1 WHERE uuid=$uuid;",
                uuid=s.raw.id,
            )
            retired.set()
            if first == "publication":
                await asyncio.wait_for(committed.wait(), 5)
            try:
                await tx.commit()
                results["retirement"] = "committed"
            except Exception as failure:
                results["retirement"] = tx.commit_outcome.value
                results["retirement_error"] = str(failure)
            if first == "retirement":
                committed.set()

    await asyncio.wait_for(asyncio.gather(publish(), retire()), 15)
    assert (
        results[first] == "committed"
        and results["retirement" if first == "publication" else "publication"] == "rejected"
    )
    s.record(phase="race", first=first, outcomes=results)


@pytest.mark.asyncio
async def test_native_typed_protected_graph_transitive_closure(native_typed_stores, monkeypatch):
    s = native_typed_stores
    async with s.transaction() as tx:
        await s.stage(tx, "parent")
        await tx.commit()
    parent = SourceIdentity(s.org, SourceKind.GRAPH_ENTITY, "parent")
    cut = await load_native_source_cut(parent, execute_query=s.graph.execute_query)
    entity, derivation = s.proposal("child")
    derivation["observations"] = [asdict(cut.snapshot.observation)]

    def forbidden():
        raise AssertionError("Protected graph ancestry used global resolver")

    monkeypatch.setattr(graph_derivations, "get_source_authority_resolver", forbidden)
    async with s.transaction() as tx:
        staged = await stage_native_typed_graph_publication(
            tx,
            content_scope=s.content_scope,
            graph_scope=s.graph_scope,
            entity=entity,
            derivation=derivation,
            resolver=s.resolver,
        )
        assert {e.source for e in staged.sources} == {s.source, parent}
        assert next(e for e in staged.sources if e.source == parent).association_id
        await tx.commit()


@pytest.mark.asyncio
async def test_native_typed_unsupported_legacy_dependency_rejects_before_witness(
    native_typed_stores,
):
    s = native_typed_stores
    await s.content.execute_query(
        "UPDATE raw_captures SET metadata.raw_source_ids=['unowned'] WHERE uuid=$uuid;",
        uuid=s.raw.id,
    )
    before = normalize_records(
        await s.content.execute_query(
            "RETURN {sha256:crypto::sha256(type::string((SELECT * FROM source_states WHERE organization_id=$org AND source_id=$uuid)[0]))};",
            org=s.org,
            uuid=s.raw.id,
        )
    )
    from sibyl_core.services.source_observations import SourceUnavailableError

    async with s.transaction() as tx:
        with pytest.raises(SourceUnavailableError):
            await s.stage(tx)
        with pytest.raises(Exception, match="not ready"):
            await tx.commit()
    after = normalize_records(
        await s.content.execute_query(
            "RETURN {sha256:crypto::sha256(type::string((SELECT * FROM source_states WHERE organization_id=$org AND source_id=$uuid)[0]))};",
            org=s.org,
            uuid=s.raw.id,
        )
    )
    assert after == before
    assert normalize_records(await s.graph.execute_query("SELECT * FROM entity;")) == []


@pytest.mark.asyncio
async def test_native_typed_unrelated_same_org_publications_overlap(native_typed_stores):
    s = native_typed_stores
    second = await remember_raw_memory(
        organization_id=s.org,
        principal_id="owner",
        source_id="second",
        raw_content="Independent evidence",
        memory_scope="private",
        scope_key="owner",
        embedding_provider=None,
    )
    second_cut = await load_native_source_cut(
        SourceIdentity(s.org, SourceKind.RAW_CAPTURE, second.id),
        execute_query=s.content.execute_query,
    )
    ready = [asyncio.Event(), asyncio.Event()]

    async def publish(index, cut):
        entity, derivation = s.proposal("overlap-" + str(index))
        derivation["observations"] = [asdict(cut.snapshot.observation)]
        async with s.transaction() as tx:
            result = await stage_native_typed_graph_publication(
                tx,
                content_scope=s.content_scope,
                graph_scope=s.graph_scope,
                entity=entity,
                derivation=derivation,
                resolver=s.resolver,
            )
            ready[index].set()
            await asyncio.wait_for(ready[1 - index].wait(), 5)
            await tx.commit()
            return result

    results = await asyncio.wait_for(asyncio.gather(publish(0, s.cut), publish(1, second_cut)), 15)
    assert all(result.created for result in results)
    assert {r.entity.id for r in results} == {"overlap-0", "overlap-1"}
    s.record(
        phase="unrelated-overlap",
        both_staged_before_commit=True,
        created=[r.entity.id for r in results],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["foreign_target", "body", "orphan_association"])
async def test_native_typed_exact_target_preflight_rejects_divergence(
    native_typed_stores, mutation
):
    s = native_typed_stores
    if mutation == "foreign_target":
        entity, _ = s.proposal()
        record = graph_entity_store._entity_record(entity, group_id="foreign-org")
        await s.graph.execute_query("INSERT INTO entity $rows;", rows=[record | {"id": entity.id}])
    else:
        async with s.transaction() as tx:
            await s.stage(tx)
            await tx.commit()
        if mutation == "body":
            await s.graph.execute_query(
                "UPDATE entity SET content='Changed',revision+=1 WHERE uuid='target';"
            )
        else:
            await s.graph.execute_query("DELETE entity WHERE uuid='target';")
    from sibyl_core.services.source_observations import SourceUnavailableError

    async with s.transaction() as tx:
        with pytest.raises(SourceUnavailableError):
            await s.stage(tx)
        with pytest.raises(Exception, match="not ready"):
            await tx.commit()
