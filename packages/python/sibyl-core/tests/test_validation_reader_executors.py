"""Explicit validation readers preserve existing authority and ancestry gates."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.services import content_client, graph_runtime, procedure_validation
from sibyl_core.services.graph_read_validation import GraphReadValidation
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.reflection_validation import (
    prepare_stored_reflection,
    validate_reflection_stage,
)
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.validation_execution import ValidationExecution
from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest
from tests.test_eval_publication import admitted_pair as admitted_pair
from tests.test_eval_publication import evidence as evidence
from tests.test_eval_publication import proposal as proposal
from tests.test_operational_projection import authority as authority
from tests.test_operational_projection import capture
from tests.test_ordinary_cohort import content_store as content_store
from tests.test_ordinary_packet_correction import create_candidate, install
from tests.test_reflection_identity import runtime as runtime
from tests.test_validation_execution import candidate as candidate


def forbid_global_reads(monkeypatch):
    attempted = []

    def denied(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("explicit validation touched a global factory")

    monkeypatch.setattr(content_client, "surreal_content_client", denied)
    monkeypatch.setattr(content_client, "get_shared_surreal_content_client", denied)
    monkeypatch.setattr(graph_runtime, "get_surreal_graph_runtime", denied)
    monkeypatch.setattr(
        "sibyl_core.services.graph_read_availability.get_surreal_graph_runtime", denied
    )
    return attempted


class FalseyReader:
    def __init__(self, execute):
        self.execute = execute
        self.calls = []

    def __bool__(self):
        return False

    async def __call__(self, query, **params):
        self.calls.append((query, params))
        return await self.execute(query, **params)


def selected_read(org, content, graph):
    return GraphReadValidation(org, content_execute_query=content, graph_execute_query=graph)


def test_validation_reader_context_requires_a_pair_and_readonly_accessors():
    callback = AsyncMock()
    for partial in (
        {"content_execute_query": callback},
        {"graph_execute_query": callback},
    ):
        with pytest.raises(ValueError, match="both"):
            GraphReadValidation("org", **partial)
    read = selected_read("org", callback, callback)
    assert read.content_execute_query is callback and read.graph_execute_query is callback
    with pytest.raises(AttributeError):
        read.content_execute_query = AsyncMock()
    with pytest.raises(AttributeError):
        read.graph_execute_query = AsyncMock()
    default = GraphReadValidation("org")
    assert default.content_execute_query is None and default.graph_execute_query is None


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("corrected", [False, True])
async def test_validation_readers_keep_packet_projection_and_recursive_correction(
    content_store, monkeypatch, complete, corrected
):
    from sibyl_core.services.automatic_reflection import _persist_corrected

    parent, resolver, origin = await create_candidate(content_store, monkeypatch, complete=complete)
    expected = parent
    if corrected:
        payload = json.loads(parent.prepared.payload_json)
        finding = {
            "claim_path": "/content",
            "claim_sha256": payload["assertion_hashes"]["/content"],
            "evidence_refs": [{"evidence_id": "s0.goal"}],
            "basis": "factual_contradiction",
            "disposition": "reconsider",
            "critique": "Keep the observation local to the cited evidence.",
        }
        install(monkeypatch, {"findings": [finding]})
        critique = await validate_reflection_stage(parent, resolver)
        review = ReviewSubmission.model_validate(critique["submission"])
        install(
            monkeypatch,
            {
                "content": parent.candidate.content + "\nThe observation remains local.",
                "abstention_reason": None,
                "assessments": [
                    {
                        "finding_id": review.finding_ids()[0],
                        "disposition": "accepted",
                        "explanation": "Keep the source-local qualification.",
                        "evidence_refs": [{"evidence_id": "s0.goal"}],
                    }
                ],
            },
        )
        correction = await validate_reflection_stage(
            parent, resolver, review, review_execution_id=critique["execution_id"]
        )
        assert correction["status"] == "corrected"
        child = await _persist_corrected(
            parent, resolver, correction, review_execution_id=critique["execution_id"]
        )
        expected = await prepare_stored_reflection("org", "owner", child.id, resolver)
    content = FalseyReader(content_store.execute_query)
    graph = AsyncMock(side_effect=AssertionError("ordinary raw evidence needs no graph lookup"))
    attempted = forbid_global_reads(monkeypatch)
    prepared = await prepare_stored_reflection(
        "org",
        "owner",
        expected.memory.id,
        resolver,
        read=selected_read("org", content, graph),
    )
    assert prepared.prepared == expected.prepared
    assert prepared.evidence == parent.evidence
    assert origin in {item["execution_id"] for item in prepared.origin_dependencies}
    assert content.calls and graph.await_count == 0 and attempted == []
    execution_queries = [
        params["uuid"]
        for query, params in content.calls
        if "SELECT * FROM memory_validation_executions" in query
    ]
    assert origin in execution_queries
    if corrected:
        assert correction["execution_id"] in execution_queries


async def test_validation_readers_procedure_keeps_original_admissions(
    candidate, content_store, monkeypatch
):
    original = await procedure_validation.prepare_stored_procedure_validation(
        "org", "owner", candidate.id
    )
    reader = FalseyReader(content_store.execute_query)
    attempted = forbid_global_reads(monkeypatch)
    result = await procedure_validation.prepare_stored_procedure_validation(
        "org",
        "owner",
        candidate.id,
        read=selected_read("org", reader, AsyncMock()),
    )
    assert result == original
    assert any("publication_operation_id" in query for query, _ in reader.calls)
    assert attempted == []


@pytest.mark.parametrize("kind", [SourceKind.RAW_CAPTURE, SourceKind.GRAPH_ENTITY])
async def test_validation_reader_snapshot_rejects_wrong_org_without_any_lookup(monkeypatch, kind):
    callback = AsyncMock()
    attempted = forbid_global_reads(monkeypatch)
    with pytest.raises(SourceUnavailableError):
        await selected_read("org", callback, callback).source_snapshot(
            SourceIdentity("foreign", kind, "id"), SourceReadAuthority("owner")
        )
    assert callback.await_count == 0 and attempted == []


async def test_validation_execution_read_selection_does_not_redirect_begin_writes(monkeypatch):
    from sibyl_core.services import validation_execution

    request = {
        "org": "org",
        "principal": "owner",
        "parent": "parent",
        "source_bindings": [],
        "policy": "{}",
    }
    reads = AsyncMock(return_value=[])
    writes = []

    async def writer(query, **params):
        writes.append((query, params))
        return [params["row"]]

    monkeypatch.setattr(validation_execution, "_query", writer)
    stage = ValidationExecution(review_digest(request), "org", "owner", read_execute_query=reads)
    assert await stage.begin(parent_id="parent", source_ids=[], policy="{}", request=request)
    assert reads.await_count == 1
    assert "SELECT * FROM memory_validation_executions" in reads.call_args.args[0]
    assert len(writes) == 1 and "CREATE" in writes[0][0]
    assert "CREATE" not in reads.call_args.args[0]


@pytest.mark.parametrize("fault", ["none", "content", "graph", "duplicate", "ceiling"])
async def test_validation_readers_mixed_protected_and_legacy_availability(
    runtime, content_store, authority, monkeypatch, fault
):
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services.eval_publication_guards import available_graph_entity_rows
    from sibyl_core.services.graph_common import normalize_graph_records
    from sibyl_core.services.graph_records import entity_from_surreal_row
    from tests.test_capture_corrections import captured_note

    _, source = await capture(runtime, authority)
    protected = await runtime.entity_manager.publish_operational_entities(source)
    _, note = await captured_note(runtime, monkeypatch)
    child = Entity(
        id="legacy-child",
        name="Legacy child",
        entity_type=EntityType.NOTE,
        metadata={"parent_entity_id": note.id, "memory_scope": "private", "principal_id": "user_a"},
    )
    await runtime.entity_manager.create_direct(child, generate_embedding=False)
    ids = [*protected.manifest.entity_ids, note.id, child.id]
    native = normalize_graph_records(
        await runtime.client.execute_query("SELECT * FROM entity WHERE uuid IN $ids;", ids=ids)
    )
    rows = {row["uuid"]: entity_from_surreal_row(row) for row in native}
    expected = await available_graph_entity_rows(
        runtime.client.group_id, rows, graph_client=runtime.client
    )
    assert set(expected) == set(ids)
    from sibyl_core.services.graph_read_availability import available_graph_entities

    assert await available_graph_entities(runtime.client.group_id, ids, runtime=runtime) == expected
    content_calls, graph_calls = [], []

    async def content(query, **params):
        content_calls.append(query)
        if fault == "content":
            raise RuntimeError("selected content reader failed")
        return await content_store.execute_query(query, **params)

    async def graph(query, **params):
        graph_calls.append(query)
        if fault == "graph":
            raise RuntimeError("selected graph reader failed")
        result = await runtime.client.execute_query(query, **params)
        if fault == "duplicate" and "associations:" in query:
            normalized = content_client.normalize_records(result)
            if normalized and normalized[0].get("associations"):
                normalized[0]["associations"].append(dict(normalized[0]["associations"][0]))
            return normalized
        return result

    if fault == "ceiling":
        assert await runtime.client.execute_query(
            "UPDATE memory_derivations SET authority_ceiling.projects=[];",
            org=runtime.client.group_id,
        )
    attempted = forbid_global_reads(monkeypatch)
    unrelated = type(
        "UnrelatedClient",
        (),
        {"execute_query": AsyncMock(side_effect=AssertionError("unrelated client used"))},
    )()
    read = selected_read(runtime.client.group_id, content, graph)
    actual = await available_graph_entity_rows(
        runtime.client.group_id, rows, graph_client=unrelated, read=read
    )
    if fault == "none":
        assert actual == expected
    elif fault in {"content", "graph"}:
        assert actual == {}
    else:
        assert set(actual) < set(expected)
        assert note.id in actual and child.id in actual
    assert graph_calls and attempted == []
    if fault != "graph":
        assert content_calls
    assert unrelated.execute_query.await_count == 0


@pytest.mark.parametrize("materializer", ["entities", "relationships"])
@pytest.mark.parametrize("identifiers", [[], ["identifier"]])
@pytest.mark.parametrize("supplied_runtime", [False, True])
async def test_validation_readers_reject_outer_materializers_before_acquisition(
    monkeypatch, materializer, identifiers, supplied_runtime
):
    from sibyl_core.services import graph_read_availability

    reader = AsyncMock()
    attempted = forbid_global_reads(monkeypatch)
    read = selected_read("org", reader, reader)
    fn = getattr(graph_read_availability, "available_graph_" + materializer)
    kwargs = {"runtime": object()} if supplied_runtime else {}
    with pytest.raises(ValueError, match="explicit validation readers"):
        await fn("org", identifiers, read=read, **kwargs)
    assert reader.await_count == 0 and attempted == []
    if not identifiers:
        assert await fn("org", identifiers, read=GraphReadValidation("org")) == {}


@pytest.mark.parametrize("mutation", ["none", "missing", "purged", "body", "inventory"])
async def test_validation_readers_result_keeps_recursive_dependency_and_prior_guards(
    content_store, monkeypatch, mutation
):
    import hashlib

    from sibyl_core.services import validation_execution as owner
    from sibyl_core.services.validation_dependencies import dependency_reference
    from sibyl_core.services.validation_result_codec import encode_validation_result
    from sibyl_core.tasks._evidence_json import canonical
    from tests import test_memory_progress, test_memory_validation, test_validation_progress_codec

    sources = test_memory_validation.sources.__wrapped__()
    citations = test_memory_validation.citations.__wrapped__()
    prepared = test_memory_validation.prepared.__wrapped__(
        test_memory_validation.candidate.__wrapped__(), sources, citations
    )
    progress = test_memory_progress.progress.__wrapped__(prepared, sources, citations)
    prior, binding, result, _ = await test_validation_progress_codec.historical.__wrapped__(
        progress
    )
    prior = {
        **prior,
        "source_ids": ["parent"],
        "usage_json": canonical(json.loads(prior["result_json"])["usage"]),
        "claim_id": "historical",
    }
    ancestor_request = {**json.loads(prior["request_json"]), "parent": "ancestor"}
    ancestor = {
        **prior,
        "uuid": review_digest(ancestor_request),
        "request_sha256": review_digest(ancestor_request),
        "request_json": canonical(ancestor_request),
        "parent_id": "ancestor",
        "source_ids": ["ancestor"],
        "dependency_ids": [],
    }
    prior_request = {
        **json.loads(prior["request_json"]),
        "execution_dependencies": [dependency_reference(ancestor).model_dump(mode="json")],
    }
    prior.update(
        uuid=review_digest(prior_request),
        request_sha256=review_digest(prior_request),
        request_json=canonical(prior_request),
        dependency_ids=[ancestor["uuid"]],
    )
    binding = binding.model_copy(
        update={"execution_id": prior["uuid"], "request_sha256": prior["uuid"]}
    )
    request = {
        "org": "org",
        "principal": "owner",
        "parent": "child",
        "policy": "{}",
        "input": result.input_sha256,
        "source_bindings": [{"source_id": "child", "incarnation": "initial", "generation": 1}],
        "progress_history": binding.model_dump(mode="json"),
    }
    child = {
        **prior,
        "uuid": review_digest(request),
        "request_sha256": review_digest(request),
        "request_json": canonical(request),
        "parent_id": "child",
        "source_ids": ["child"],
        "dependency_ids": sorted([ancestor["uuid"], prior["uuid"]]),
        "result_json": canonical(encode_validation_result(result)),
        "usage_json": canonical(result.usage.model_dump(mode="json")),
    }
    for row in (ancestor, prior, child):
        await owner._query("CREATE memory_validation_executions CONTENT $row;", row=row)
    original = await ValidationExecution(child["uuid"], "org", "owner").result()
    reader = FalseyReader(content_store.execute_query)

    async def selected(query, **params):
        if params["uuid"] == ancestor["uuid"]:
            if mutation == "missing":
                return []
            records = content_client.normalize_records(await reader(query, **params))
            records = [dict(row) for row in records]
            if mutation == "purged":
                records[0]["purged"] = True
            elif mutation == "body":
                records[0]["result_json"] += " "
            elif mutation == "inventory":
                records[0]["dependency_ids"] = [prior["uuid"]]
            return records
        return await reader(query, **params)

    attempted = forbid_global_reads(monkeypatch)
    stage = ValidationExecution(child["uuid"], "org", "owner", read_execute_query=selected)
    if mutation == "none":
        actual = await stage.result()
        assert actual == original
        assert actual["prior_assessments"]
        assert stage._dependency_ids == sorted([ancestor["uuid"], prior["uuid"]])
        ids = [params["uuid"] for _, params in reader.calls]
        assert prior["uuid"] in ids and ancestor["uuid"] in ids and child["uuid"] in ids
        assert hashlib.sha256(prior["result_json"].encode()).hexdigest() == binding.result_sha256
    else:
        with pytest.raises(ValueError):
            await stage.result()
    assert attempted == []


@pytest.fixture
async def native_reader_stores(monkeypatch, tmp_path):
    from surrealdb.connections.async_ws import AsyncWsSurrealConnection

    from sibyl_core.backends.surreal import SurrealContentClient
    from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services.content_raw_persistence import remember_raw_memory
    from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
    from sibyl_core.services.graph_entities import EntityManager

    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL")
    if not url:
        pytest.skip("cross-store reader snapshots require independent native sockets")
    username = os.environ["SIBYL_ARCHIVE_TEST_SURREAL_USERNAME"]
    password = os.environ["SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD"]
    organization = str(uuid4())
    content_namespace = "validation_reader_content_" + uuid4().hex
    graph_prefix = "validation_reader_graph_"
    graph_namespace = graph_prefix + organization.replace("-", "")
    names = [content_namespace, graph_namespace]
    registry = Path(
        os.environ.get("SIBYL_VALIDATION_READER_REGISTRY", str(tmp_path / "registry.jsonl"))
    )
    registry.parent.mkdir(parents=True, exist_ok=True)

    def record(**evidence):
        with registry.open("a") as stream:
            stream.write(json.dumps({"namespaces": names, **evidence}, sort_keys=True) + "\n")

    record(phase="registered", organization_id=organization)
    root = AsyncWsSurrealConnection(url)
    content = SurrealContentClient(
        url=url,
        username=username,
        password=password,
        namespace=content_namespace,
        database="content",
        pool_size=4,
    )
    graph = SurrealGraphClient(
        group_id=organization,
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
        await bootstrap_content_schema(content)
        await prepare_graph_schema(graph)

        @asynccontextmanager
        async def session():
            yield content

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        raw = await remember_raw_memory(
            organization_id=organization,
            principal_id="owner",
            source_id="raw-source",
            raw_content="Original raw evidence",
            memory_scope="private",
            scope_key="owner",
            embedding_provider=None,
        )
        entity = Entity(
            id="graph-source",
            name="Original",
            content="Original graph evidence",
            entity_type=EntityType.NOTE,
            metadata={"memory_scope": "private", "principal_id": "owner"},
        )
        await EntityManager(graph, group_id=organization).create_direct(
            entity, generate_embedding=False
        )
        yield organization, raw.id, entity.id, content, graph, username, password
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


async def test_validation_readers_native_common_cut_and_independent_overlapping_contexts(
    native_reader_stores, monkeypatch
):
    from sibyl_core.backends.surreal.native_transaction import (
        NativeCredentialProfile,
        NativeStoreScope,
        NativeTransactionAuthorization,
        NativeTransactionBinding,
        open_native_transaction,
    )

    org, raw_id, graph_id, content, graph, username, password = native_reader_stores
    content_scope = NativeStoreScope(
        "content", content._namespace, "content", org, ("raw_captures", "source_states")
    )
    graph_scope = NativeStoreScope(
        "graph", graph._namespace, "graph", org, ("entity", "source_states", "memory_derivations")
    )
    binding = NativeTransactionBinding(
        os.environ["SIBYL_ARCHIVE_TEST_SURREAL_URL"],
        "synthetic-owned-provider",
        "reader-test",
        "synthetic-root",
        (content_scope, graph_scope),
    )

    async def authorize():
        return NativeTransactionAuthorization(binding, "owner", "fresh-reader-decision")

    async def credentials(profile):
        return NativeCredentialProfile(profile, username, password)

    raw_source = SourceIdentity(org, SourceKind.RAW_CAPTURE, raw_id)
    graph_source = SourceIdentity(org, SourceKind.GRAPH_ENTITY, graph_id)
    authority = SourceReadAuthority("owner")
    attempted = forbid_global_reads(monkeypatch)
    begun = [asyncio.Event(), asyncio.Event()]
    mutated = asyncio.Event()
    readers = []

    async def retain_snapshot(index):
        async with open_native_transaction(
            binding, authorize=authorize, credentials=credentials
        ) as tx:
            read = selected_read(org, tx.executor(content_scope), tx.executor(graph_scope))
            readers.append(read)
            raw_before = await read.source_snapshot(raw_source, authority)
            assert raw_before.memory.raw_content == "Original raw evidence"
            begun[index].set()
            await asyncio.wait_for(mutated.wait(), 5)
            graph_before = await read.source_snapshot(graph_source, authority)
            assert graph_before.entity.content == "Original graph evidence"
            assert (await read.source_snapshot(raw_source, authority)) == raw_before
            with pytest.raises(SourceUnavailableError):
                await read.source_snapshot(graph_source, SourceReadAuthority("foreign"))
            await tx.commit()
            return raw_before, graph_before

    async def write_after_both_readers():
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in begun)), 5)
        await content.execute_query(
            "BEGIN TRANSACTION;"
            + f"USE NS {graph._namespace} DB graph;"
            + "UPDATE entity SET content='Changed graph evidence', revision+=1 WHERE uuid=$graph_id;"
            + f"USE NS {content._namespace} DB content;"
            + "UPDATE raw_captures SET raw_content='Changed raw evidence', revision+=1 WHERE uuid=$raw_id;"
            + "COMMIT TRANSACTION;",
            graph_id=graph_id,
            raw_id=raw_id,
        )
        mutated.set()

    first, second, _ = await asyncio.wait_for(
        asyncio.gather(retain_snapshot(0), retain_snapshot(1), write_after_both_readers()), 10
    )
    assert readers[0] is not readers[1] and first == second
    async with open_native_transaction(binding, authorize=authorize, credentials=credentials) as tx:
        fresh = selected_read(org, tx.executor(content_scope), tx.executor(graph_scope))
        raw_now = await fresh.source_snapshot(raw_source, authority)
        graph_now = await fresh.source_snapshot(graph_source, authority)
        assert raw_now.memory.raw_content == "Changed raw evidence"
        assert graph_now.entity.content == "Changed graph evidence"
        assert raw_now.observation.revision > first[0].observation.revision
        assert graph_now.observation.revision > first[1].observation.revision
        await tx.commit()
    assert attempted == []
