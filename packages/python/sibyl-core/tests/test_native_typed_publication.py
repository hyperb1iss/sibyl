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
            (
                "raw_captures",
                "source_states",
                "memory_derivations",
                "eval_attempts",
                "eval_consolidations",
                "memory_validation_executions",
                "memory_validation_attempts",
            ),
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


@pytest_asyncio.fixture
async def native_validated_procedure(native_typed_stores, monkeypatch, tmp_path):
    """Signed canonical admissions and a real returned offline validation."""
    from hashlib import sha256
    from unittest.mock import AsyncMock

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from pydantic import TypeAdapter
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from sibyl_core.ai.llm.extractor import Extractor
    from sibyl_core.config import settings
    from sibyl_core.models.memory_scope import MemoryScope
    from sibyl_core.services import (
        content_models,
        eval_consolidation,
        eval_publication,
        procedure_validation,
    )
    from sibyl_core.services.eval_admission import admit_eval_outcome, register_eval_assignment
    from sibyl_core.services.memory_promotion import (
        _candidate_from_review_memory,
        _entity_from_candidate,
    )
    from sibyl_core.services.validation_execution import ValidationExecution
    from sibyl_core.services.validation_promotion import ValidatedPromotion, ValidationBinding
    from sibyl_core.tasks import consolidation as c
    from sibyl_core.tasks.eval_receipts import TaskAssignment, sign_outcome
    from sibyl_core.tasks.memory_validation import CriticOutput, MemoryValidationResult
    from tests.validation_policy import offline_policy

    s = native_typed_stores
    monkeypatch.setattr(content_models, "configured_raw_memory_embedding_provider", lambda: None)
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    monkeypatch.setattr(settings, "validation_receipt_dir", str(receipts))
    monkeypatch.setattr(settings, "consolidation_max_input_chars", 40000)
    key = Ed25519PrivateKey.generate()
    assignment = TaskAssignment(
        organization_id=s.org,
        owner_principal_id="owner",
        experiment_id="experiment",
        experiment_revision="revision-1",
        task_id="task",
        task_revision="task-1",
        task_sha256="a" * 64,
        family_id="family",
        split="learning",
        arm_id="raw",
        checkpoint=0,
        seed=7,
        memory_pack_sha256="b" * 64,
        controller_policy_sha256="c" * 64,
        attempt_id="d" * 32,
        checker_sha256="4" * 64,
        oracle_sha256="e" * 64,
        evaluator_sha256="f" * 64,
        runtime_sha256="1" * 64,
        image="sha256:" + "2" * 64,
    )
    attempts = []
    for index, status in enumerate(("passed", "task_failed")):
        task = assignment.model_copy(update={"attempt_id": str(index) * 32})
        outcome = {
            "schema_version": "sibyl-json-cli-outcome-v1",
            "attempt_id": task.attempt_id,
            "snapshot_sha256": "3" * 64,
            "checker_sha256": task.checker_sha256,
            "oracle_sha256": task.oracle_sha256,
            "evaluator_sha256": task.evaluator_sha256,
            "runtime_sha256": task.runtime_sha256,
            "image": task.image,
            "status": status,
            "passed": index == 0,
        }
        materials = {
            "outcome_bytes": json.dumps(outcome).encode(),
            "transcript_bytes": b'{"action":"check"}\n',
            "episode_bytes": f"Observed {status} episode.\n".encode(),
        }
        await register_eval_assignment(organization_id=s.org, assignment=task)
        admitted = await admit_eval_outcome(
            organization_id=s.org,
            experiment_id=task.experiment_id,
            attempt_id=task.attempt_id,
            principal_id="owner",
            issuer_id="oracle-1",
            trusted_public_key=key.public_key(),
            expected_controller_policy_sha256=task.controller_policy_sha256,
            receipt_bytes=sign_outcome(
                assignment=task, issuer_id="oracle-1", private_key=key, **materials
            ),
            **materials,
        )
        attempts.append((task, admitted))
    group = await eval_consolidation.load_admitted_consolidation_group(
        organization_id=s.org,
        principal_id="owner",
        experiment_id=assignment.experiment_id,
        experiment_revision=assignment.experiment_revision,
        arm_id="raw",
        through_checkpoint=0,
        attempt_ids=tuple(task.attempt_id for task, _ in attempts),
        group_id="contrast",
        mechanism="verify observed output",
        trusted_issuer_id="oracle-1",
        trusted_public_key=key.public_key(),
        expected_controller_policy_sha256=assignment.controller_policy_sha256,
    )

    def assertion(index):
        episode = group.episodes[index]
        return c.ConditionalAssertion(
            statement="Check the actual output",
            label="inferred",
            support=[
                c.SupportRef(
                    episode_id=episode.episode_id, start_byte=0, end_byte=len(episode.artifact)
                )
            ],
        )

    draft = c.DraftConditionalProcedure(
        goal=assertion(0),
        environment=[assertion(0)],
        preconditions=[assertion(0)],
        actions=[c.ConditionalAction(order=1, action=assertion(0), success_criteria=assertion(0))],
        expected_result=assertion(0),
        failure_modes=[assertion(1)],
        abstain_when=[assertion(1)],
    )
    original = Extractor.extract_with_usage

    async def extract(_self, _prompt):
        return SimpleNamespace(
            output=c.ProcedureProposal(procedure=draft),
            usage=SimpleNamespace(model_dump=lambda **_: {}),
        )

    monkeypatch.setattr(Extractor, "extract_with_usage", extract)
    try:
        result = await c.propose_conditional_procedure(group)
    finally:
        monkeypatch.setattr(Extractor, "extract_with_usage", original)
    operation = eval_publication.ConsolidationOperation(
        organization_id=s.org,
        principal_id="owner",
        experiment_id=assignment.experiment_id,
        experiment_revision=assignment.experiment_revision,
        arm_id="raw",
        checkpoint=0,
        group_id="contrast",
        attempt_ids=tuple(task.attempt_id for task, _ in attempts),
        mechanism="verify observed output",
        controller_policy_sha256=assignment.controller_policy_sha256,
        extractor_revision="offline-stored-procedure-v1",
    )
    stored = await eval_publication.store_consolidation(operation, result)
    reader = Extractor(
        CriticOutput,
        agent=Agent(TestModel(custom_output_args={"findings": []}), output_type=CriticOutput),
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(side_effect=lambda *_: (reader, offline_policy(max_input_chars=40000))),
    )
    authorize = AsyncMock()
    validated = await procedure_validation.validate_stored_procedure(
        organization_id=s.org,
        principal_id="owner",
        parent_id=stored.memory.id,
        authorize=authorize,
    )
    row = await ValidationExecution(validated["execution_id"], s.org, "owner").load()
    returned = TypeAdapter(MemoryValidationResult).validate_json(row["result_json"])
    promotion = ValidatedPromotion(
        s.org,
        "owner",
        stored.memory.id,
        ValidationBinding(
            execution_id=validated["execution_id"],
            request_sha256=validated["execution_id"],
            result_sha256=sha256(row["result_json"].encode()).hexdigest(),
            input_sha256=returned.input_sha256,
        ),
        authorize,
    )
    candidate = SourceIdentity(s.org, SourceKind.RAW_CAPTURE, stored.memory.id)
    cut = await load_native_source_cut(candidate, execute_query=s.content.execute_query)
    admitted_memories = [admitted.memory for _, admitted in attempts]
    source_ids = [memory.id for memory in admitted_memories]
    review_candidate = _candidate_from_review_memory(
        stored.memory,
        raw_source_ids=source_ids,
        target_scope=MemoryScope.PRIVATE,
        target_scope_key=None,
        domain=None,
    )
    entity = _entity_from_candidate(
        review_candidate,
        organization_id=s.org,
        principal_id="owner",
        domain=None,
        project=None,
        source_id=source_ids[0],
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        policy_metadata={},
        source_memories=admitted_memories,
    )
    assert entity.metadata["review_capture_id"] == stored.memory.id
    derivation = {
        "organization_id": s.org,
        "target_kind": "graph_entity",
        "target_id": entity.id,
        "active": True,
        "principal_id": "owner",
        "authority_ceiling": SourceReadAuthority("owner").ceiling_metadata(),
        "observations": [asdict(cut.snapshot.observation)],
    }
    return SimpleNamespace(
        stores=s,
        promotion=promotion,
        entity=entity,
        derivation=derivation,
        candidate=candidate,
        sources={candidate}
        | {SourceIdentity(s.org, SourceKind.RAW_CAPTURE, r.memory.id) for _, r in attempts},
        execution_id=validated["execution_id"],
    )


@pytest.mark.asyncio
async def test_native_typed_real_stored_procedure_promotion(native_validated_procedure):
    p = native_validated_procedure
    s = p.stores
    async with s.transaction() as tx:
        staged = await stage_native_typed_graph_publication(
            tx,
            content_scope=s.content_scope,
            graph_scope=s.graph_scope,
            entity=p.entity,
            derivation=p.derivation,
            resolver=s.resolver,
            promotion=p.promotion,
        )
        assert staged.created
        assert {cut.source for cut in staged.sources} == p.sources
        await tx.commit()
    rows = normalize_records(
        await s.content.execute_query(
            "SELECT * FROM memory_validation_executions WHERE uuid=$uuid;",
            uuid=p.execution_id,
        )
    )
    assert len(rows) == 1 and rows[0]["promotion_write_witness"] > 0
    async with s.transaction() as tx:
        replay = await stage_native_typed_graph_publication(
            tx,
            content_scope=s.content_scope,
            graph_scope=s.graph_scope,
            entity=p.entity,
            derivation=p.derivation,
            resolver=s.resolver,
            promotion=p.promotion,
        )
        assert not replay.created and replay.physical_id == staged.physical_id
        assert replay.body_sha256 == staged.body_sha256
        await tx.commit()
    s.record(
        phase="real-procedure",
        candidate_id=p.candidate.id,
        execution_id=p.execution_id,
        source_ids=sorted(source.id for source in p.sources),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["exact", "omitted", "created_by", "review_capture_id", "source_file"]
)
async def test_native_typed_reflection_identity_replay(native_typed_stores, change):
    from sibyl_core.services.graph_derivations import graph_target_digest
    from sibyl_core.services.memory_identity import (
        IDENTITY_KEY,
        reflection_entity_id,
        reflection_identity,
    )

    s = native_typed_stores
    entity, derivation = s.proposal()
    entity.created_by = "owner"
    entity.source_file = s.raw.id
    entity.metadata["review_capture_id"] = "canonical-review"
    entity.id = reflection_entity_id(entity)
    entity.metadata[IDENTITY_KEY] = reflection_identity(entity)
    derivation["target_id"] = entity.id
    async with s.transaction() as tx:
        staged = await stage_native_typed_graph_publication(
            tx,
            content_scope=s.content_scope,
            graph_scope=s.graph_scope,
            entity=entity,
            derivation=derivation,
            resolver=s.resolver,
        )
        assert staged.created
        await tx.commit()
    replay_entity = entity.model_copy(deep=True)
    if change != "exact":
        replay_entity.metadata.pop(IDENTITY_KEY)
    if change == "created_by":
        replay_entity.created_by = "another-owner"
    elif change == "review_capture_id":
        replay_entity.metadata["review_capture_id"] = "another-review"
    elif change == "source_file":
        replay_entity.source_file = "another-source"
    assert graph_target_digest(replay_entity) == graph_target_digest(entity)
    async with s.transaction() as tx:
        if change in {"exact", "omitted"}:
            replay = await stage_native_typed_graph_publication(
                tx,
                content_scope=s.content_scope,
                graph_scope=s.graph_scope,
                entity=replay_entity,
                derivation=derivation,
                resolver=s.resolver,
            )
            assert not replay.created and replay.physical_id == staged.physical_id
            await tx.commit()
        else:
            with pytest.raises(ValueError, match="reflection identity conflict"):
                await stage_native_typed_graph_publication(
                    tx,
                    content_scope=s.content_scope,
                    graph_scope=s.graph_scope,
                    entity=replay_entity,
                    derivation=derivation,
                    resolver=s.resolver,
                )
            with pytest.raises(Exception, match="not ready"):
                await tx.commit()
    s.record(phase="reflection-replay", change=change)


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_target", [False, True])
async def test_native_typed_foreign_graph_association_rejected(
    native_typed_stores, retained_target
):
    s = native_typed_stores
    if retained_target:
        async with s.transaction() as tx:
            await s.stage(tx)
            await tx.commit()
    _, association = s.proposal()
    association["organization_id"] = str(uuid4())
    association["body_sha256"] = "a" * 64
    await s.graph.execute_query(
        "CREATE type::record('source_states',crypto::sha256(type::string([$foreign,'graph_entity','target']))) "
        "CONTENT {organization_id:$foreign,source_kind:'graph_entity',source_id:'target',"
        "generation:1,revision:0,deleted:true,incarnation:type::string(rand::uuid())};",
        foreign=association["organization_id"],
    )
    await s.graph.execute_query(
        "CREATE memory_derivations CONTENT $association;",
        association=association,
    )
    before = normalize_records(
        await s.graph.execute_query(
            "SELECT type::string(id) AS physical_id,crypto::sha256(type::string($this)) AS sha256 "
            "FROM memory_derivations;"
        )
    )
    async with s.transaction() as tx:
        with pytest.raises(Exception, match=r"publication_target_(association|cardinality)"):
            await s.stage(tx)
        with pytest.raises(Exception, match="not ready"):
            await tx.commit()
    after = normalize_records(
        await s.graph.execute_query(
            "SELECT type::string(id) AS physical_id,crypto::sha256(type::string($this)) AS sha256 "
            "FROM memory_derivations;"
        )
    )
    assert after == before
    if retained_target:
        with pytest.raises(Exception, match=r"publication_source_(association|cardinality)"):
            await load_native_source_cut(
                SourceIdentity(s.org, SourceKind.GRAPH_ENTITY, "target"),
                execute_query=s.graph.execute_query,
            )
    s.record(phase="foreign-graph-association", retained_target=retained_target)


@pytest.mark.asyncio
async def test_native_typed_shared_raw_association_remains_organization_scoped(native_typed_stores):
    s = native_typed_stores
    association = {
        "organization_id": str(uuid4()),
        "target_kind": "raw_capture",
        "target_id": s.raw.id,
        "body_sha256": "a" * 64,
        "principal_id": "foreign",
        "authority_ceiling": {},
        "active": True,
        "observations": [],
    }
    await s.content.execute_query(
        "CREATE type::record('source_states',crypto::sha256(type::string([$foreign,'raw_capture',$target]))) "
        "CONTENT {organization_id:$foreign,source_kind:'raw_capture',source_id:$target,"
        "generation:1,revision:0,deleted:true,incarnation:type::string(rand::uuid())};",
        foreign=association["organization_id"],
        target=s.raw.id,
    )
    await s.content.execute_query(
        "CREATE memory_derivations CONTENT $association;",
        association=association,
    )
    cut = await load_native_source_cut(s.source, execute_query=s.content.execute_query)
    assert cut.association is None and cut.descriptor == s.cut.descriptor
    async with s.transaction() as tx:
        staged = await s.stage(tx)
        assert staged.created
        await tx.commit()
    s.record(phase="shared-raw-foreign-association-ignored")


@pytest.mark.asyncio
async def test_native_typed_graph_source_cut_rejects_foreign_only_association(native_typed_stores):
    s = native_typed_stores
    async with s.transaction() as tx:
        await s.stage(tx)
        await tx.commit()
    _, association = s.proposal()
    association["organization_id"] = str(uuid4())
    association["body_sha256"] = "a" * 64
    await s.graph.execute_query(
        "CREATE type::record('source_states',crypto::sha256(type::string([$foreign,'graph_entity','target']))) "
        "CONTENT {organization_id:$foreign,source_kind:'graph_entity',source_id:'target',"
        "generation:1,revision:0,deleted:true,incarnation:type::string(rand::uuid())};",
        foreign=association["organization_id"],
    )
    await s.graph.execute_query(
        "CREATE memory_derivations CONTENT $association;",
        association=association,
    )
    await s.graph.execute_query(
        "DELETE memory_derivations WHERE organization_id=$org AND target_kind='graph_entity' AND target_id='target';",
        org=s.org,
    )
    with pytest.raises(Exception, match="publication_source_association"):
        await load_native_source_cut(
            SourceIdentity(s.org, SourceKind.GRAPH_ENTITY, "target"),
            execute_query=s.graph.execute_query,
        )
    s.record(phase="foreign-only-graph-source-association")
