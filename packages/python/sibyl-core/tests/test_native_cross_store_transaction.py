"""Native leaf integration with real SourceState and canonical graph publication."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from surrealdb import AsyncSurreal
from surrealdb.errors import SurrealError

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import _content_schema_migrations
from sibyl_core.backends.surreal.dedicated_client import _checked_query_result
from sibyl_core.backends.surreal.native_transaction import (
    NativeCommitOutcome,
    NativeCredentialProfile,
    NativeStoreScope,
    NativeTransactionAuthorization,
    NativeTransactionBinding,
    NativeTransactionError,
    open_native_transaction,
)
from sibyl_core.backends.surreal.schema import (
    ANALYZER_DEFINITIONS,
    EDGE_DEFINITIONS,
    NODE_DEFINITIONS,
    _graph_schema_migrations,
    render_surreal_compatible_sql,
)
from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.backends.surreal.schema_version import apply_schema_migrations
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.migrate.archive_phase_receipts import ArchivePhaseKey
from sibyl_core.models import Entity
from sibyl_core.models.entities import EntityType
from sibyl_core.services.archive_phase_store import (
    prepare_archive_phase_transaction,
    read_archive_phase_receipt,
)
from sibyl_core.services.content_models import raw_memory_record
from sibyl_core.services.content_raw_persistence import (
    _RAW_MEMORY_BULK_UPSERT_QUERY,
    replace_raw_memory_records_bulk,
)
from sibyl_core.services.graph_derivations import graph_target_digest
from sibyl_core.services.graph_entity_store import _insert_entity_if_absent
from sibyl_core.services.graph_records import entity_from_surreal_row
from sibyl_core.services.memory_derivations import observation_from_record
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.source_state_store import (
    load_source_snapshot,
    source_snapshot_from_records,
)
from tests import test_archive_raw_compiler as raw_fixture

URL = os.environ.get("SIBYL_TEST_NATIVE_SURREAL_URL", "")
USER = os.environ.get("SIBYL_TEST_NATIVE_SURREAL_USER", "root")
PASSWORD = os.environ.get("SIBYL_TEST_NATIVE_SURREAL_PASSWORD", "archive-proof")
OUT = os.environ.get("SIBYL_NATIVE_TRANSACTION_EVIDENCE")
pytestmark = pytest.mark.skipif(
    not URL, reason="native transaction handle requires opted-in WS fixture"
)


def event(case, kind, **values):
    if not OUT:
        return
    directory = Path(OUT)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (case + ".jsonl")).open("a") as f:
        f.write(
            json.dumps(
                dict(case=case, kind=kind, monotonic_ns=time.monotonic_ns(), **values),
                sort_keys=True,
                default=str,
            )
            + "\n"
        )


async def connect(case, role, ns, db):
    conn = AsyncSurreal(URL)
    await conn.connect()
    await conn.signin({"username": USER, "password": PASSWORD})
    await conn.use(ns, db)
    event(
        case,
        "connection",
        role=role,
        socket_object=id(conn),
        namespace=ns,
        database=db,
        version=await conn.version(),
    )
    return conn


class Actor:
    """Trusted test domain adapter; canonical boundary lifting stays caller-owned."""

    def __init__(self, n, role, manager, tx):
        self.case, self.role, self.manager, self.tx = n.case, role, manager, tx
        self.conn, self.txn = tx._client, tx._txn
        self.scope = "content"
        self.executors = {s.store: tx.executor(s) for s in tx._binding.scopes}
        self.allowed_boundary_sql = set()
        self.closed = False

    async def execute_query(self, sql, **params):
        original = sql
        if sql == _RAW_MEMORY_BULK_UPSERT_QUERY or sql in self.allowed_boundary_sql:
            assert sql.count("BEGIN TRANSACTION;") == sql.count("COMMIT TRANSACTION;") == 1
            sql = sql.replace("BEGIN TRANSACTION;", "", 1).replace("COMMIT TRANSACTION;", "", 1)
            sql = "RETURN { " + sql + " };"
        result = await self.executors[self.scope](sql, **params)
        event(
            self.case,
            "leaf_query",
            role=self.role,
            txn=str(self.txn),
            scope=self.scope,
            canonical_sql=original,
            result=result,
        )
        return result

    async def commit(self):
        event(self.case, "commit_requested", role=self.role, txn=str(self.txn))
        try:
            await self.tx.commit()
        except BaseException as exc:
            event(
                self.case,
                "commit_error",
                role=self.role,
                txn=str(self.txn),
                outcome=self.tx.commit_outcome,
                error_type=type(exc).__name__,
                error=str(exc),
                details=getattr(exc, "details", None),
                error_kind=getattr(exc, "kind", None),
                error_code=getattr(exc, "code", None),
            )
            raise
        event(
            self.case,
            "commit_ack",
            role=self.role,
            txn=str(self.txn),
            outcome=self.tx.commit_outcome,
        )

    async def cleanup(self):
        if not self.closed:
            self.closed = True
            await self.manager.__aexit__(None, None, None)
            event(self.case, "leaf_closed", role=self.role, outcome=self.tx.commit_outcome)


def binding_for(n):
    return NativeTransactionBinding(
        URL,
        "owned-native-fixture",
        "test-runtime-config",
        "test-service",
        (
            NativeStoreScope(
                "content",
                n.content_ns,
                "content",
                n.org,
                ("raw_captures", "source_states", "archive_phase_receipts"),
            ),
            NativeStoreScope(
                "graph",
                n.graph_ns,
                "graph",
                n.org,
                ("entity", "source_states", "memory_derivations"),
            ),
        ),
    )


async def actor(native, role):
    binding = binding_for(native)

    async def authorize():
        event(native.case, "fresh_actor_authorization", role=role, actor=native.principal)
        return NativeTransactionAuthorization(binding, native.principal, role)

    async def credentials(profile):
        return NativeCredentialProfile(profile, USER, PASSWORD)

    manager = open_native_transaction(binding, authorize=authorize, credentials=credentials)
    tx = await manager.__aenter__()
    a = Actor(native, role, manager, tx)
    event(native.case, "leaf_begin_ack", role=role, txn=str(a.txn), socket_object=id(a.conn.socket))
    native.actors.append(a)
    return a


RAW_SNAPSHOT = """RETURN {
 LET $raws=(SELECT * FROM raw_captures WHERE organization_id=$org AND uuid=$uuid);
 LET $states=(SELECT * FROM source_states WHERE organization_id=$org AND source_kind='raw_capture' AND source_id=$uuid);
 LET $native_state=(SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id=$org AND source_kind='raw_capture' AND source_id=$uuid)[0];
 RETURN {rows:$raws,states:$states,row_sha:crypto::sha256(type::string($raws[0])),
 state_sha:crypto::sha256(type::string($native_state))};
};"""
RAW_GUARD = """LET $raws=(SELECT * FROM raw_captures WHERE organization_id=$org AND uuid=$uuid);
 LET $states=(SELECT * FROM source_states WHERE organization_id=$org AND source_kind='raw_capture' AND source_id=$uuid);
 LET $native_state=(SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id=$org AND source_kind='raw_capture' AND source_id=$uuid)[0];
 IF array::len($raws)!=1 OR array::len($states)!=1 OR $raws[0].deleted_at!=NONE OR $states[0].deleted!=false
 OR $states[0].generation!=$generation OR $states[0].incarnation!=$incarnation OR $states[0].revision!=$revision
 OR crypto::sha256(type::string($raws[0]))!=$row_sha
 OR crypto::sha256(type::string($native_state))!=$state_sha { THROW 'actual_content_source_changed'; };
 LET $source_states_to_fence=$states;
"""


@pytest_asyncio.fixture
async def native(request, tmp_path):
    case = request.node.name.replace("[", "_").replace("]", "")
    ident = uuid4().hex
    cn = "native_leaf_content_" + ident
    gn = "native_leaf_graph_" + ident
    event(case, "owned_namespace_inventory", namespaces=[cn, gn])
    options = {"url": URL, "username": USER, "password": PASSWORD, "pool_size": 1}
    content = SurrealContentClient(**options, namespace=cn, database="content")
    graph = SurrealContentClient(**options, namespace=gn, database="graph")
    n = SimpleNamespace(
        case=case, content_ns=cn, graph_ns=gn, content=content, graph=graph, actors=[]
    )
    try:
        await apply_schema_migrations(
            content.execute_query, _content_schema_migrations(url=URL), name="content"
        )
        await graph.execute_query(
            render_surreal_compatible_sql(
                ANALYZER_DEFINITIONS + NODE_DEFINITIONS + EDGE_DEFINITIONS, url=URL
            )
        )
        await apply_schema_migrations(
            graph.execute_query, _graph_schema_migrations(url=URL), name="graph"
        )
        parsed, plan = raw_fixture.inputs(tmp_path, count=2)
        value = raw_fixture.prepared(parsed, plan)
        apply_key, apply_token, tx = raw_fixture.phase(value, url=URL)
        result = await content.execute_query(tx.query, **tx.parameters)
        n.org, n.principal = plan.organization_id, plan.actor_id
        n.apply_key, n.apply_token = apply_key, apply_token
        n.receipt = await read_archive_phase_receipt(
            content.execute_query, key=apply_key, token=apply_token
        )
        assert n.receipt is not None and len(n.receipt.introduced) == 2
        n.ids = [i.row.destination_id for i in raw_fixture.raw_rows(value)]
        event(
            case,
            "canonical_raw_archive_apply",
            sql=tx.query,
            params=tx.parameters,
            response=result,
            org=n.org,
            ids=n.ids,
        )
        n.cuts = []
        n.snapshots = []
        for identifier in n.ids:
            cut = await content.execute_query(RAW_SNAPSHOT, org=n.org, uuid=identifier)
            assert len(cut["rows"]) == len(cut["states"]) == 1
            source = SourceIdentity(n.org, SourceKind.RAW_CAPTURE, identifier)
            snap = source_snapshot_from_records(source, cut["rows"][0], cut["states"][0])
            assert snap is not None and snap.observation.durable
            n.cuts.append(cut)
            n.snapshots.append(snap)
        event(
            case,
            "initial_actual_source_cut",
            cuts=n.cuts,
            typed=[dataclasses.asdict(s.observation) for s in n.snapshots],
        )
        yield n
    finally:
        for a in n.actors:
            await a.cleanup()
        await graph.close()
        cleanup = await connect(case, "cleanup", cn, "content")
        try:
            for name in [cn, gn]:
                response = await cleanup.query_raw(f"REMOVE NAMESPACE {name};")
                _checked_query_result(response, all_results=True)
            result = _checked_query_result(
                await cleanup.query_raw("INFO FOR ROOT;"), all_results=True
            )[-1]
            assert cn not in result["namespaces"] and gn not in result["namespaces"]
            event(case, "cleanup_absence", removed=[cn, gn], remaining_owned=[])
        finally:
            await cleanup.close()
            await content.close()


async def publish(n, a, *, index=0, witness=True):
    a.scope = "content"
    cut, snap = n.cuts[index], n.snapshots[index]
    state = cut["states"][0]
    await a.execute_query(
        RAW_GUARD + (SOURCE_STATE_WRITE_WITNESS if witness else "") + " RETURN true;",
        org=n.org,
        uuid=n.ids[index],
        generation=state["generation"],
        incarnation=state["incarnation"],
        revision=state["revision"],
        row_sha=cut["row_sha"],
        state_sha=cut["state_sha"],
    )
    # Actual durable decoder runs inside the exact same native handle.
    observed = await a.execute_query(RAW_SNAPSHOT, org=n.org, uuid=n.ids[index])
    current = await load_source_snapshot(
        snap.observation.source, organization_id=n.org, execute_query=a.executors["content"]
    )
    assert observed["states"][0]["id"] == state["id"]
    assert current is not None and current.observation.same_evidence(snap.observation)
    target = Entity(
        id=str(uuid4()),
        entity_type=EntityType.PATTERN,
        name="foreign graph dependency",
        content="Derived from the actual imported ordinary raw body",
        description="Canonical direct insertion capability",
        organization_id=n.org,
        created_by=n.principal,
        metadata={"memory_scope": "private", "principal_id": n.principal},
    )
    authority = SourceReadAuthority(n.principal)
    association = {
        "organization_id": n.org,
        "target_kind": "graph_entity",
        "target_id": target.id,
        "principal_id": authority.principal_id,
        "authority_ceiling": authority.ceiling_metadata(),
        "observations": [dataclasses.asdict(current.observation)],
        "active": True,
    }
    a.scope = "graph"
    stored, created = await _insert_entity_if_absent(
        a, target, group_id=n.org, derivation=association
    )
    assert created
    event(
        n.case,
        "canonical_graph_insert_result",
        stored=stored,
        target_id=target.id,
        typed_association=association,
        witness=witness,
    )
    return target.id


async def retire(n, a, *, index=0, mode="soft"):
    a.scope = "content"
    if mode == "soft":
        memory = dataclasses.replace(n.snapshots[index].memory, deleted_at=datetime.now(UTC))
        record = raw_memory_record(memory)
        saved = await replace_raw_memory_records_bulk(a, [record])
        assert len(saved) == 1 and saved[0]["deleted_at"] is not None
    else:
        introduced = next(i for i in n.receipt.introduced if i.destination_id == n.ids[index])
        rollback_key = ArchivePhaseKey(
            binding=n.apply_key.binding, store="content", action="rollback", batch_sequence=0
        )
        tx = prepare_archive_phase_transaction(
            key=rollback_key,
            url=URL,
            expected_revision=1,
            expected_token=n.apply_token,
            rollback_token=str(uuid4()),
            retirement_candidates=(introduced,),
            writer_statements="DELETE raw_captures WHERE organization_id=$org AND uuid=$source_id; LET $sibyl_archive_phase_outcomes=[{kind:'raw_capture',disposition:'retired',destination_id:$source_id}];",
            writer_parameters={"org": n.org, "source_id": n.ids[index]},
        )
        a.allowed_boundary_sql.add(tx.query)
        saved = await a.execute_query(tx.query, **tx.parameters)
        event(
            n.case,
            "actual_composer_retirement_staged",
            original_introduction=introduced.model_dump(mode="json"),
            response=saved,
        )
    inside = await a.execute_query(RAW_SNAPSHOT, org=n.org, uuid=n.ids[index])
    assert inside["states"][0]["deleted"] is True
    assert inside["states"][0]["id"] == n.cuts[index]["states"][0]["id"]
    assert inside["states"][0]["generation"] == n.cuts[index]["states"][0]["generation"] + 1
    assert bool(inside["rows"]) is (mode == "soft")
    event(
        n.case,
        "canonical_raw_retirement",
        source_index=index,
        mode=mode,
        saved=saved,
        state=inside["states"][0],
    )


async def readback(n, target):
    content = await n.content.execute_query(RAW_SNAPSHOT, org=n.org, uuid=n.ids[0])
    graph = await n.graph.execute_query(
        """RETURN {targets:(SELECT * FROM entity WHERE group_id=$org AND uuid=$target),
      associations:(SELECT * FROM memory_derivations WHERE organization_id=$org AND target_kind='graph_entity' AND target_id=$target),
      states:(SELECT * FROM source_states WHERE organization_id=$org AND source_kind='graph_entity' AND source_id=$target)};""",
        org=n.org,
        target=target,
    )
    event(n.case, "committed_readback", content=content, graph=graph)
    return content, graph


async def inventory(n, target):
    a = await actor(n, "fresh_rollback_inventory")
    a.scope = "content"
    cut = await a.execute_query(RAW_SNAPSHOT, org=n.org, uuid=n.ids[0])
    assert cut["states"][0]["deleted"] is False
    a.scope = "graph"
    rows = await a.execute_query(
        """SELECT * FROM memory_derivations WHERE organization_id=$org AND active=true
      AND target_kind='graph_entity' AND array::len(observations[WHERE source.organization_id=$org
        AND source.kind='raw_capture' AND source.id=$source])>0;""",
        org=n.org,
        source=n.ids[0],
    )
    assert len(rows) == 1 and rows[0]["target_id"] == target
    assert observation_from_record(rows[0]["observations"][0]).same_evidence(
        n.snapshots[0].observation
    )
    # Preservation gate occurs before actual raw retirement, inside this handle.
    event(
        n.case,
        "foreign_dependency_preservation_required",
        association=rows[0],
        content_state=cut["states"][0],
        raw_retirement_executed=False,
        txn=str(a.txn),
    )
    await a.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["publisher", "retirer"])
@pytest.mark.parametrize("iteration", [0, 1])
@pytest.mark.parametrize("retirement_mode", ["soft", "physical"])
async def test_native_cross_store_transaction_both_first_commit(
    native, first, iteration, retirement_mode
):
    n = native
    publisher, retirer = await asyncio.gather(actor(n, "publisher"), actor(n, "retirer"))
    target = await publish(n, publisher)
    await retire(n, retirer, mode=retirement_mode)
    event(
        n.case,
        "both_staged_before_first_commit",
        publisher_txn=str(publisher.txn),
        retirer_txn=str(retirer.txn),
    )
    winner, loser = (publisher, retirer) if first == "publisher" else (retirer, publisher)
    await winner.commit()
    with pytest.raises(SurrealError, match=r"(?i)conflict.*retried") as failure:
        await loser.commit()
    assert winner.tx.commit_outcome == NativeCommitOutcome.ACKNOWLEDGED
    assert loser.tx.commit_outcome == NativeCommitOutcome.REJECTED
    event(n.case, "expected_native_write_conflict", winner=first, error=str(failure.value))
    content, graph = await readback(n, target)
    if first == "publisher":
        assert (
            content["states"][0]["deleted"] is False
            and content["rows"][0].get("deleted_at") is None
        )
        assert len(graph["targets"]) == len(graph["associations"]) == len(graph["states"]) == 1
        assert graph["targets"][0]["derivation_required"] is True
        assert graph["states"][0]["deleted"] is False
        assert graph["associations"][0]["body_sha256"] == graph_target_digest(
            entity_from_surreal_row(graph["targets"][0])
        )
        assert content["states"][0].get("validation_write_witness") != n.cuts[0]["states"][0].get(
            "validation_write_witness"
        )
        assert content["states"][0]["generation"] == n.cuts[0]["states"][0]["generation"]
        await inventory(n, target)
    else:
        assert content["states"][0]["deleted"] is True
        assert (
            (content["rows"][0].get("deleted_at") is not None)
            if retirement_mode == "soft"
            else content["rows"] == []
        )
        assert graph == {"targets": [], "associations": [], "states": []}
        retry = await actor(n, "publisher_retry_after_tombstone")
        with pytest.raises(SurrealError, match="actual_content_source_changed"):
            await publish(n, retry)
        assert (await readback(n, target))[1] == {"targets": [], "associations": [], "states": []}
        empty = await n.graph.execute_query(
            "RETURN {targets:(SELECT * FROM entity),associations:(SELECT * FROM memory_derivations),states:(SELECT * FROM source_states)};"
        )
        assert empty == {"targets": [], "associations": [], "states": []}
        event(n.case, "publisher_retry_tombstone_rejected_without_graph_effects")


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["publisher", "retirer"])
async def test_native_cross_store_transaction_unrelated_source_overlap(native, first):
    n = native
    publisher, retirer = await asyncio.gather(actor(n, "publisher"), actor(n, "retirer"))
    target = await publish(n, publisher)
    await retire(n, retirer, index=1)
    winner, other = (publisher, retirer) if first == "publisher" else (retirer, publisher)
    await winner.commit()
    await other.commit()
    source, graph = await readback(n, target)
    separate = await n.content.execute_query(RAW_SNAPSHOT, org=n.org, uuid=n.ids[1])
    assert source["states"][0]["deleted"] is False and separate["states"][0]["deleted"] is True
    assert len(graph["targets"]) == 1
    event(n.case, "unrelated_same_org_both_committed", first=first, separate_source=separate)


@pytest.mark.asyncio
async def test_native_cross_store_transaction_omission_counterexample(native):
    n = native
    publisher, retirer = await asyncio.gather(
        actor(n, "publisher_without_witness"), actor(n, "retirer")
    )
    target = await publish(n, publisher, witness=False)
    await retire(n, retirer)
    await retirer.commit()
    await publisher.commit()
    source, graph = await readback(n, target)
    assert source["states"][0]["deleted"] is True and len(graph["targets"]) == 1
    event(n.case, "negative_control_unfenced_dependency_survives_retirement", target=target)


@pytest.mark.asyncio
async def test_native_cross_store_transaction_scope_and_transaction_affinity(native):
    n = native
    a = await actor(n, "guard")
    a.scope = "content"
    original = a.txn
    foreign = dataclasses.replace(a.executors["graph"].scope, namespace="foreign")
    with pytest.raises(NativeTransactionError, match="approved"):
        a.tx.executor(foreign)
    # Native rejection of a real handle on another socket, not only a local check.
    other = await connect(n.case, "foreign_socket", n.content_ns, "content")
    try:
        response = await other.query_raw("RETURN true;", txn_id=original)
        event(n.case, "foreign_socket_native_response", response=response, txn=str(original))
        with pytest.raises(SurrealError):
            _checked_query_result(response, all_results=True)
    finally:
        await other.close()
    session_id = await a.conn.attach()
    try:
        response = await a.conn.query_raw("RETURN true;", session_id=session_id, txn_id=original)
        event(n.case, "foreign_session_native_response", response=response, txn=str(original))
        with pytest.raises(SurrealError):
            _checked_query_result(response, all_results=True)
    finally:
        await a.conn.detach(session_id)
    assert await a.execute_query("RETURN true;") is True
    await a.commit()
    with pytest.raises(NativeTransactionError):
        await a.execute_query("RETURN true;")
    event(n.case, "local_scope_endpoint_affinity_guards_and_native_handle_rejections_pass")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["namespace", "database", "table"])
async def test_native_cross_store_transaction_missing_catalog_is_nonmutating(missing):
    case = "missing_" + missing + "_" + uuid4().hex
    namespace = "native_leaf_catalog_" + uuid4().hex
    database = "data"
    event(case, "owned_namespace_inventory", namespaces=[namespace])
    admin = AsyncSurreal(URL)
    await admin.connect()
    await admin.signin({"username": USER, "password": PASSWORD})
    try:
        if missing != "namespace":
            _checked_query_result(
                await admin.query_raw(f"DEFINE NAMESPACE {namespace};"), all_results=True
            )
        if missing == "table":
            _checked_query_result(
                await admin.query_raw(
                    f"USE NS {namespace}; DEFINE DATABASE {database}; USE DB {database}; "
                    "DEFINE TABLE probe SCHEMALESS; CREATE probe:original SET value='unchanged';"
                ),
                all_results=True,
            )
        root_before = _checked_query_result(await admin.query_raw("INFO FOR ROOT;"))
        scope = NativeStoreScope(
            "content", namespace, database, str(uuid4()), ("missing_required_table",)
        )
        binding = NativeTransactionBinding(
            URL, "owned-fixture", "catalog-control", "test-service", (scope,)
        )

        async def authorize():
            return NativeTransactionAuthorization(binding, "test-actor", case)

        async def credentials(profile):
            return NativeCredentialProfile(profile, USER, PASSWORD)

        with pytest.raises(NativeTransactionError, match="absent"):
            async with open_native_transaction(
                binding, authorize=authorize, credentials=credentials
            ):
                pytest.fail("missing catalog yielded a data executor")
        root_after = _checked_query_result(await admin.query_raw("INFO FOR ROOT;"))
        assert root_after["namespaces"].get(namespace) == root_before["namespaces"].get(namespace)
        if missing == "database":
            ns = _checked_query_result(
                await admin.query_raw(f"USE NS {namespace}; INFO FOR NS;"), all_results=True
            )[-1]
            assert ns["databases"] == {}
        if missing == "table":
            db = _checked_query_result(
                await admin.query_raw(f"USE NS {namespace} DB {database}; INFO FOR DB;"),
                all_results=True,
            )[-1]
            assert set(db["tables"]) == {"probe"}
            rows = _checked_query_result(
                await admin.query_raw(f"USE NS {namespace} DB {database}; SELECT * FROM probe;"),
                all_results=True,
            )[-1]
            assert len(rows) == 1 and rows[0]["value"] == "unchanged"
        event(case, "missing_catalog_no_mutation", missing=missing)
    finally:
        root = _checked_query_result(await admin.query_raw("INFO FOR ROOT;"))
        if namespace in root["namespaces"]:
            _checked_query_result(await admin.query_raw(f"REMOVE NAMESPACE {namespace};"))
        root = _checked_query_result(await admin.query_raw("INFO FOR ROOT;"))
        assert namespace not in root["namespaces"]
        event(case, "cleanup_absence", removed=[namespace], remaining_owned=[])
        await admin.close()


@pytest.mark.asyncio
async def test_native_cross_store_transaction_explicit_two_org_routing(native):
    n = native
    second_org = str(uuid4())
    second_ns = "native_leaf_second_graph_" + uuid4().hex
    event(n.case, "owned_namespace_inventory", namespaces=[second_ns])
    second = SurrealContentClient(
        url=URL,
        username=USER,
        password=PASSWORD,
        namespace=second_ns,
        database="graph",
        pool_size=1,
    )
    try:
        await second.execute_query(
            render_surreal_compatible_sql(
                ANALYZER_DEFINITIONS + NODE_DEFINITIONS + EDGE_DEFINITIONS, url=URL
            )
        )
        await apply_schema_migrations(
            second.execute_query, _graph_schema_migrations(url=URL), name="graph"
        )
        targets = []
        for client, org, name in [
            (n.graph, n.org, "first org"),
            (second, second_org, "second org"),
        ]:
            target = Entity(
                id=str(uuid4()),
                name=name,
                entity_type=EntityType.PATTERN,
                content="Canonical organization routing control",
                organization_id=org,
            )
            _, created = await _insert_entity_if_absent(client, target, group_id=org)
            assert created
            targets.append(target)
        scopes = (
            *binding_for(n).scopes,
            NativeStoreScope(
                "graph",
                second_ns,
                "graph",
                second_org,
                ("entity", "source_states", "memory_derivations"),
            ),
            NativeStoreScope(
                "content", n.content_ns, "content", second_org, ("raw_captures", "source_states")
            ),
        )
        binding = dataclasses.replace(binding_for(n), scopes=scopes)

        async def authorize():
            return NativeTransactionAuthorization(binding, n.principal, "two-org-routing")

        async def credentials(profile):
            return NativeCredentialProfile(profile, USER, PASSWORD)

        async with open_native_transaction(
            binding, authorize=authorize, credentials=credentials
        ) as tx:
            first_execute, second_execute = tx.executor(scopes[1]), tx.executor(scopes[2])

            async def lookup(execute, target):
                result = await execute(
                    "RETURN {namespace:session::ns(), rows:(SELECT * FROM entity WHERE group_id=$org AND uuid=$uuid)};",
                    org=target.organization_id,
                    uuid=target.id,
                )
                assert len(result["rows"]) == 1 and result["rows"][0]["name"] == target.name
                return result

            # Same-handle concurrent RPCs carry their own scopes; USE never leaks.
            first, other = await asyncio.gather(
                lookup(first_execute, targets[0]), lookup(second_execute, targets[1])
            )
            assert first["namespace"] == n.graph_ns and other["namespace"] == second_ns
            again = await lookup(first_execute, targets[0])
            assert again["namespace"] == n.graph_ns
            shared = await tx.executor(scopes[3])(
                "SELECT * FROM raw_captures WHERE organization_id=$org;", org=second_org
            )
            assert shared == []
            response = await tx._client.query_raw("SELECT * FROM entity;", txn_id=tx._txn)
            with pytest.raises(SurrealError, match="namespace"):
                _checked_query_result(response, all_results=True)
            await tx.commit()
            event(
                n.case, "two_org_scope_routing", first=first, second=other, shared_other_org=shared
            )
    finally:
        await second.close()
        admin = await connect(n.case, "second-cleanup", n.content_ns, "content")
        try:
            _checked_query_result(await admin.query_raw(f"REMOVE NAMESPACE {second_ns};"))
            root = _checked_query_result(await admin.query_raw("INFO FOR ROOT;"))
            assert second_ns not in root["namespaces"]
            event(n.case, "cleanup_absence", removed=[second_ns], remaining_owned=[])
        finally:
            await admin.close()


@pytest.mark.asyncio
async def test_native_cross_store_transaction_lost_commit_ack_retains_uncertainty(
    native, monkeypatch
):
    n = native
    publisher = await actor(n, "publisher_lost_ack")
    target = await publish(n, publisher)
    real_commit = publisher.conn.commit

    async def lose_ack(txn):
        await real_commit(txn)
        raise ConnectionError("synthetic response loss after native COMMIT")

    monkeypatch.setattr(publisher.conn, "commit", lose_ack)
    with pytest.raises(ConnectionError, match="response loss"):
        await publisher.commit()
    assert publisher.tx.commit_outcome == NativeCommitOutcome.UNKNOWN
    # Native cancellation now reports the consumed handle; preserve that evidence.
    with pytest.raises(BaseExceptionGroup) as cleanup:
        await publisher.cleanup()
    assert len(cleanup.value.exceptions) == 1
    assert "Transaction not found" in str(cleanup.value.exceptions[0])
    assert publisher.tx.commit_outcome == NativeCommitOutcome.UNKNOWN
    assert publisher.conn.socket is None
    content, graph = await readback(n, target)
    assert content["states"][0]["deleted"] is False
    assert len(graph["targets"]) == len(graph["associations"]) == 1
    event(
        n.case,
        "unknown_commit_with_durable_publication",
        outcome=publisher.tx.commit_outcome,
        cleanup_errors=[str(e) for e in cleanup.value.exceptions],
        requires_domain_receipt_reconciliation=True,
    )


@pytest.mark.asyncio
async def test_native_cross_store_transaction_cancellation_discards_staged_publication(native):
    n = native
    binding = binding_for(n)
    staged = asyncio.Event()
    targets = []
    handles = []

    async def authorize():
        return NativeTransactionAuthorization(binding, n.principal, "cancelled-publisher")

    async def credentials(profile):
        return NativeCredentialProfile(profile, USER, PASSWORD)

    async def work():
        async with open_native_transaction(
            binding, authorize=authorize, credentials=credentials
        ) as tx:
            handles.append((tx, tx._client))
            adapter = Actor(n, "cancelled-publisher", None, tx)
            targets.append(await publish(n, adapter))
            staged.set()
            await asyncio.Future()

    task = asyncio.create_task(work())
    await staged.wait()
    task.cancel("cancel native staged publication")
    with pytest.raises(asyncio.CancelledError):
        await task
    tx, conn = handles[0]
    assert tx.commit_outcome == NativeCommitOutcome.NOT_REQUESTED
    assert conn.socket is None
    source, graph = await readback(n, targets[0])
    assert source["states"][0].get("validation_write_witness") == n.cuts[0]["states"][0].get(
        "validation_write_witness"
    )
    assert graph == {"targets": [], "associations": [], "states": []}
    event(n.case, "cancelled_publication_rolled_back", target=targets[0], outcome=tx.commit_outcome)


@pytest.mark.asyncio
async def test_native_cross_store_transaction_missing_real_state_refuses_publication(native):
    n = native
    await n.content.execute_query(
        "DELETE source_states WHERE organization_id=$org AND source_kind='raw_capture' AND source_id=$uuid;",
        org=n.org,
        uuid=n.ids[0],
    )
    publisher = await actor(n, "missing-real-state")
    with pytest.raises(SurrealError, match="actual_content_source_changed"):
        await publish(n, publisher)
    await publisher.cleanup()
    content, graph = await readback(n, "absent")
    assert len(content["rows"]) == 1 and content["states"] == []
    assert graph == {"targets": [], "associations": [], "states": []}
    event(n.case, "missing_real_state_rejected_without_reconstruction")


@pytest.mark.asyncio
async def test_native_cross_store_transaction_socket_close_failure_is_retained(native, monkeypatch):
    n = native
    publisher = await actor(n, "socket-close-fault")
    target = await publish(n, publisher)
    socket = publisher.conn.socket
    real_close = socket.close

    async def close_with_error(*args, **kwargs):
        await real_close(*args, **kwargs)
        raise ConnectionError("synthetic socket close failure")

    monkeypatch.setattr(socket, "close", close_with_error)
    with pytest.raises(BaseExceptionGroup) as errors:
        await publisher.cleanup()
    assert [str(error) for error in errors.value.exceptions] == ["synthetic socket close failure"]
    assert publisher.conn.socket is None
    source, graph = await readback(n, target)
    assert source["states"][0].get("validation_write_witness") == n.cuts[0]["states"][0].get(
        "validation_write_witness"
    )
    assert graph == {"targets": [], "associations": [], "states": []}
    event(n.case, "public_socket_close_failure_retained", error=str(errors.value.exceptions[0]))


@pytest.mark.asyncio
async def test_native_cross_store_transaction_inflight_commit_rejects_and_drains(native):
    n = native
    publisher = await actor(n, "inflight-commit-control")
    execute = publisher.executors["content"]
    query = asyncio.create_task(execute("RETURN { sleep(250ms); RETURN true; };"))
    await asyncio.sleep(0)
    assert publisher.tx._inflight == 1
    with pytest.raises(NativeTransactionError, match="finish before COMMIT"):
        await publisher.commit()
    assert publisher.tx.commit_outcome == NativeCommitOutcome.NOT_REQUESTED
    await publisher.cleanup()
    result = (await asyncio.gather(query, return_exceptions=True))[0]
    assert query.done() and publisher.tx._inflight == 0
    assert publisher.conn.socket is None
    assert result is True or isinstance(result, BaseException)
    source, graph = await readback(n, "absent")
    assert source["states"][0].get("validation_write_witness") == n.cuts[0]["states"][0].get(
        "validation_write_witness"
    )
    assert graph == {"targets": [], "associations": [], "states": []}
    event(
        n.case,
        "inflight_native_rpc_drained",
        result=result,
        error_type=type(result).__name__ if isinstance(result, BaseException) else None,
        outcome=publisher.tx.commit_outcome,
    )


@pytest.mark.asyncio
async def test_native_cross_store_transaction_unbound_replacement_stays_open(native):
    n = native
    publisher = await actor(n, "replacement-affinity-control")
    original = publisher.conn.socket
    foreign = AsyncSurreal(URL)
    await foreign.connect()
    await foreign.signin({"username": USER, "password": PASSWORD})
    try:
        publisher.conn.socket = foreign.socket
        with pytest.raises(NativeTransactionError, match="affinity"):
            await publisher.execute_query("RETURN true;")
        with pytest.raises(BaseExceptionGroup) as cleanup:
            await publisher.cleanup()
        assert [str(error) for error in cleanup.value.exceptions] == [
            "native socket affinity was lost",
            "unbound replacement prevents SDK teardown",
        ]
        result = _checked_query_result(await foreign.query_raw("INFO FOR ROOT;"))
        assert n.content_ns in result["namespaces"]
        # Restore only the already-closed owned public socket for fixture teardown.
        publisher.conn.socket = original
        await publisher.conn.close()
        event(
            n.case,
            "unbound_socket_was_not_closed",
            errors=[str(e) for e in cleanup.value.exceptions],
        )
    finally:
        await foreign.close()


@pytest.mark.parametrize("first_cancellation", ["body", "deadline"])
@pytest.mark.asyncio
async def test_native_cross_store_transaction_lost_cancel_ack_closes_and_drains(
    native, monkeypatch, first_cancellation
):
    n = native
    binding = binding_for(n)
    ready, ack_dropped = asyncio.Event(), asyncio.Event()
    handles, queries, targets, close_calls = [], [], [], []
    cancel_ids = set()

    async def authorize():
        return NativeTransactionAuthorization(binding, n.principal, "lost-cancel-response")

    async def credentials(profile):
        return NativeCredentialProfile(profile, USER, PASSWORD)

    class DropCancelFuture:
        def __init__(self, future):
            self.future = future

        def __bool__(self):
            return True

        def set_result(self, value):
            event(n.case, "actual_cancel_ack_dropped", response=value, socket_kept_open=True)
            ack_dropped.set()

        def done(self):
            return self.future.done()

        def cancel(self):
            return self.future.cancel()

    class Queries(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, DropCancelFuture(value) if key in cancel_ids else value)

    async def work():
        async with open_native_transaction(
            binding,
            authorize=authorize,
            credentials=credentials,
            cancel_ack_timeout_seconds=0.15,
        ) as tx:
            conn, socket = tx._client, tx._socket
            handles.append((tx, conn, socket))
            adapter = Actor(n, "lost-cancel-response", None, tx)
            targets.append(await publish(n, adapter))
            original_send = conn._send

            async def send(message, process, bypass=False):
                if process == "cancel":
                    cancel_ids.add(message.id)
                return await original_send(message, process, bypass)

            monkeypatch.setattr(conn, "_send", send)
            conn.qry = Queries(conn.qry)
            original_close = socket.close

            async def tracked_close(*args, **kwargs):
                close_calls.append(time.monotonic_ns())
                return await original_close(*args, **kwargs)

            monkeypatch.setattr(socket, "close", tracked_close)
            queries.append(
                asyncio.create_task(
                    tx.executor(binding.scopes[0])("RETURN { sleep(500ms); RETURN true; };")
                )
            )
            await asyncio.sleep(0)
            assert tx._inflight == 1
            ready.set()
            if first_cancellation == "body":
                await asyncio.Future()

    owner = asyncio.create_task(work())
    try:
        await asyncio.wait_for(ready.wait(), 30)
        if first_cancellation == "body":
            owner.cancel("first owner cancellation")
        await asyncio.wait_for(ack_dropped.wait(), 30)
        if first_cancellation == "deadline":
            owner.cancel("first owner cancellation")
        await asyncio.sleep(0)
        owner.cancel("second owner cancellation")
        with pytest.raises(BaseExceptionGroup) as errors:
            await asyncio.wait_for(owner, 5)
        flat = errors.value.exceptions
        assert sum(isinstance(e, TimeoutError) for e in flat) == 1
        owner_errors = [e for e in flat if str(e).endswith("owner cancellation")]
        assert [str(e) for e in owner_errors] == [
            "first owner cancellation",
            "second owner cancellation",
        ]
        tx, conn, socket = handles[0]
        assert close_calls and conn.socket is None and tx._inflight == 0
        assert socket.state.name == "CLOSED"
        assert tx.commit_outcome == NativeCommitOutcome.NOT_REQUESTED
        results = await asyncio.gather(*queries, return_exceptions=True)
        assert all(q.done() for q in queries)
        source, graph = await readback(n, targets[0])
        assert source["states"][0].get("validation_write_witness") == n.cuts[0]["states"][0].get(
            "validation_write_witness"
        )
        assert graph == {"targets": [], "associations": [], "states": []}
        event(
            n.case,
            "lost_cancel_grace_closed_original_and_drained",
            first_cancellation=first_cancellation,
            close_calls=len(close_calls),
            socket_state=socket.state.name,
            inflight=tx._inflight,
            commit_outcome=tx.commit_outcome,
            errors=[
                {"type": type(e).__name__, "message": str(e), "notes": getattr(e, "__notes__", [])}
                for e in flat
            ],
            caller_owned_query_results=[repr(result) for result in results],
            staged_publication_absent=True,
        )
    finally:
        if handles and not owner.done():
            # Test-only safety rescue preserves evidence if the production leaf regresses.
            event(n.case, "harness_rescue_original_socket", leaf_failed_to_finish=True)
            await handles[0][2].close()
        await asyncio.gather(owner, *queries, return_exceptions=True)
