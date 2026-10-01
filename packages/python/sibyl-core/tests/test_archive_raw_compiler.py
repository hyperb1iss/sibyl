"""Checked raw compilation exercised through canonical native phase writes."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from surrealdb.errors import SurrealError

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import _content_schema_migrations
from sibyl_core.backends.surreal.schema_version import apply_schema_migrations
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.archive_phase_receipts import ArchivePhaseKey
from sibyl_core.migrate.personal_archive_candidates import normalize_archive_candidates
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveCredentialCeiling,
    ArchiveDisposition,
    ArchiveKind,
    ArchiveStoreWitness,
    CheckedArchivePlan,
    canonical_json,
    preview_counts,
)
from sibyl_core.migrate.personal_archive_prepared import (
    destination_semantic_digest,
    prepare_archive_body,
    prepare_archive_records,
)
from sibyl_core.migrate.source_integrity import build_integrity_archive
from sibyl_core.services.archive_phase_store import (
    prepare_archive_phase_transaction,
    read_archive_phase_receipt,
)
from sibyl_core.services.archive_raw_compiler import prepare_archive_raw_apply
from sibyl_core.services.content_models import raw_memory_from_record, raw_memory_record
from sibyl_core.services.content_raw_persistence import replace_raw_memory_records_bulk
from tests import test_personal_archive_candidates as fixtures

_TIMESTAMP = "2026-09-30T01:02:03.123456789Z"


def observe(value):
    if name := os.environ.get("SIBYL_RAW_COMPILER_EVIDENCE_PATH"):
        with Path(name).open("a") as output:
            output.write(json.dumps(value, sort_keys=True, default=str) + "\n")


def inputs(tmp_path, *, count=1, protected=(), metadata=None):
    org, owner, actor, destination = (str(uuid4()) for _ in range(4))
    records = [fixtures._raw(org, owner, protected=index in protected) for index in range(count)]
    for record in records:
        record["metadata"].update(user_timestamp=_TIMESTAMP, user_nested={"values": [17, "keep"]})
        record["metadata"].update(metadata or {})
    payload = {
        "version": "2.0",
        "organization_id": org,
        "tables": {"raw_captures": records},
        "row_counts": {"raw_captures": count},
        "total_rows": count,
        "source_integrity": build_integrity_archive(
            kind=SourceKind.RAW_CAPTURE,
            organizations=[org],
            source_rows=records,
            source_states=[
                fixtures._state(org, row["uuid"], SourceKind.RAW_CAPTURE) for row in records
            ],
            derivations=[],
        ),
    }
    parsed = fixtures._parsed(tmp_path, org, content=payload)
    mappings = fixtures._mapping(actor, owner)
    rows = []
    for candidate in normalize_archive_candidates(parsed, mappings, actor_id=actor):
        row = candidate.initial_preview(
            organization_id=destination, actor_id=actor, origin=parsed.origin
        )
        if row.kind is ArchiveKind.RAW_CAPTURE and row.disposition is ArchiveDisposition.CREATED:
            body = prepare_archive_body(candidate, row, actor_id=actor, node_ids={})
            row = row.model_copy(
                update={
                    "semantic_sha256": destination_semantic_digest(row, body),
                    "witnesses": (
                        ArchiveStoreWitness(
                            store="content", identity="raw_captures:" + row.destination_id
                        ),
                    ),
                }
            )
        rows.append(row)
    plan = CheckedArchivePlan(
        organization_id=destination,
        actor_id=actor,
        origin=parsed.origin,
        archive_sha256=parsed.archive_sha256,
        artifact_sha256=parsed.artifact_sha256,
        mappings=mappings,
        credential=ArchiveCredentialCeiling(credential_kind="session"),
        rows=tuple(rows),
        counts=preview_counts(tuple(rows)),
    )
    return parsed, plan


def prepared(parsed, plan):
    return prepare_archive_records(parsed, plan, run_id=str(uuid4()), artifact_id=str(uuid4()))


def phase(value, *, token=None, url="memory://", key=None, revision=0):
    key = key or ArchivePhaseKey(
        binding=value.binding, store="content", action="apply", batch_sequence=0
    )
    token = token or str(uuid4())
    tx = prepare_archive_raw_apply(
        prepared=value, key=key, url=url, expected_revision=revision, expected_token=token
    )
    return key, token, tx


def change_rows(plan, changes):
    rows = tuple(row.model_copy(update=changes.get(row.original_id, {})) for row in plan.rows)
    return plan.model_copy(update={"rows": rows, "counts": preview_counts(rows)})


def raw_rows(value):
    return sorted(
        (item for item in value.rows if item.row.kind is ArchiveKind.RAW_CAPTURE),
        key=lambda item: (
            item.row.disposition is ArchiveDisposition.QUARANTINED,
            item.row.original_id,
        ),
    )


def test_raw_compiler_pure_immutable_complete_batch(tmp_path):
    parsed, plan = inputs(tmp_path, count=2)
    value = prepared(parsed, plan)
    key, token, tx = phase(value)
    assert len(tx.parameters["archive_raw_candidates"]) == 2
    assert len(tx.parameters["sibyl_archive_phase_planned_creates"]) == 2
    first = tx.parameters
    first["archive_raw_candidates"][0]["record"]["metadata"]["user_nested"]["values"].append(
        "changed"
    )
    assert tx.parameters["archive_raw_candidates"][0]["record"]["metadata"]["user_nested"][
        "values"
    ] == [17, "keep"]
    assert phase(value, token=token, key=key)[2] == tx
    assert all(item.row.declarations > 1 for item in raw_rows(value))
    item = raw_rows(value)[0]
    changed = item.body
    changed["metadata"]["user_timestamp"] = _TIMESTAMP[:-2] + "8Z"
    assert destination_semantic_digest(item.row, changed) != item.row.semantic_sha256
    assert all(
        row["record"]["captured_at"] is None and row["record"]["created_at"] is None
        for row in tx.parameters["archive_raw_candidates"]
    )


@pytest.mark.parametrize(
    "field",
    [
        "organization_id",
        "actor_id",
        "run_id",
        "artifact_id",
        "archive_sha256",
        "artifact_sha256",
        "mappings_sha256",
        "checked_plan_sha256",
        "credential",
    ],
)
def test_raw_compiler_rejects_binding_mismatch(tmp_path, field):
    parsed, plan = inputs(tmp_path)
    value = prepared(parsed, plan)
    changed = {
        field: {"credential_kind": "api_key", "api_key_id": str(uuid4())}
        if field == "credential"
        else "0" * 64
        if field.endswith("sha256")
        else str(uuid4())
    }
    binding = value.binding.model_copy(update=changed)
    key = ArchivePhaseKey(binding=binding, store="content", action="apply", batch_sequence=0)
    with pytest.raises(ValueError):
        phase(value, key=key)


@pytest.mark.parametrize(
    "changes", [{"store": "graph"}, {"action": "rollback"}, {"batch_sequence": 1}]
)
def test_raw_compiler_rejects_another_phase(tmp_path, changes):
    parsed, plan = inputs(tmp_path)
    value = prepared(parsed, plan)
    key = ArchivePhaseKey(
        binding=value.binding, store="content", action="apply", batch_sequence=0
    ).model_copy(update=changes)
    with pytest.raises(ValueError, match="phase key"):
        phase(value, key=key)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "wrong_identity",
        "graph",
        "duplicate",
        "malformed_digest",
        "not_absent",
        "endpoint",
        "missing_state",
    ],
)
def test_raw_compiler_rejects_unsupported_witnesses(tmp_path, change):
    parsed, plan = inputs(tmp_path)
    row = next(row for row in plan.rows if row.kind is ArchiveKind.RAW_CAPTURE)
    witness = row.witnesses[0]
    changes = {
        "missing": {"witnesses": ()},
        "wrong_identity": {
            "witnesses": (witness.model_copy(update={"identity": "raw_captures:" + str(uuid4())}),)
        },
        "graph": {"witnesses": (witness.model_copy(update={"store": "graph"}),)},
        "duplicate": {"witnesses": (witness, witness)},
        "malformed_digest": {"witnesses": (witness.model_copy(update={"row_sha256": "x" * 64}),)},
        "not_absent": {"witnesses": (witness.model_copy(update={"state_sha256": "a" * 64}),)},
        "endpoint": {"endpoint_ids": (str(uuid4()),)},
        "missing_state": {"disposition": ArchiveDisposition.CONFLICTED},
    }
    if change == "endpoint":
        # A consistent saved prepared snapshot must still reject unsupported guards.
        value = prepared(parsed, plan)
        from sibyl_core.migrate.personal_archive_plan import checked_plan_bytes, checked_plan_digest

        new_plan = change_rows(plan, {row.original_id: changes[change]})
        value = replace(
            value,
            checked_plan_json=checked_plan_bytes(new_plan),
            checked_plan_sha256=checked_plan_digest(new_plan),
            rows=tuple(
                replace(
                    item,
                    row_json=canonical_json(
                        next(
                            r
                            for r in new_plan.rows
                            if (r.kind, r.original_id) == (item.row.kind, item.row.original_id)
                        )
                    ),
                )
                for item in value.rows
            ),
        )
    else:
        value = prepared(parsed, change_rows(plan, {row.original_id: changes[change]}))
    with pytest.raises(ValueError):
        phase(value)


def test_raw_compiler_rejects_tampered_bytes_and_empty_selection(tmp_path):
    parsed, plan = inputs(tmp_path)
    value = prepared(parsed, plan)
    for changed in [
        replace(value, checked_plan_sha256="0" * 64),
        replace(value, rows=value.rows[:-1]),
    ]:
        with pytest.raises(ValueError):
            phase(changed)
    rows = tuple(row for row in plan.rows if row.kind is not ArchiveKind.RAW_CAPTURE)
    from sibyl_core.migrate.personal_archive_plan import checked_plan_bytes, checked_plan_digest

    empty_plan = plan.model_copy(update={"rows": rows, "counts": preview_counts(rows)})
    empty = replace(
        value,
        checked_plan_json=checked_plan_bytes(empty_plan),
        checked_plan_sha256=checked_plan_digest(empty_plan),
        rows=tuple(item for item in value.rows if item.row.kind is not ArchiveKind.RAW_CAPTURE),
    )
    with pytest.raises(ValueError, match="selection is empty"):
        phase(empty)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def native():
    namespace = "archive_raw_compiler_author_" + uuid4().hex
    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://")
    options = {
        "url": url,
        "namespace": namespace,
        "username": os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        "password": os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
        "pool_size": 1,
    }
    client = SurrealContentClient(**options)
    writer = client if is_embedded_surreal_url(url) else SurrealContentClient(**options)
    trace = []
    try:
        await apply_schema_migrations(
            client.execute_query, _content_schema_migrations(url=url), name="content"
        )
        for role, connection in [("phase", client), ("canonical_writer", writer)]:
            if connection is client and role != "phase":
                continue
            original = connection._send_query

            async def send(native_socket, query, *, params, raw, original=original, role=role):
                response = await original(native_socket, query, params=params, raw=raw)
                event = {
                    "role": role,
                    "socket": id(native_socket),
                    "query": query,
                    "params": params,
                    "response": response,
                }
                trace.append(event)
                observe({"native": event, "namespace": namespace})
                return response

            connection._send_query = send
        await writer.execute_query("RETURN {namespace: session::ns(), database: session::db()};")
        yield SimpleNamespace(client=client, writer=writer, url=url, trace=trace)
    finally:
        await client.execute_query(f"REMOVE NAMESPACE {namespace};")
        remaining = await client.execute_query("INFO FOR ROOT;")
        assert namespace not in remaining["namespaces"]
        observe({"namespace": namespace, "cleanup": "owned_namespace_removed"})
        if writer is not client:
            await writer.close()
        await client.close()


async def snapshot(client, org, identity):
    return await client.execute_query(
        """RETURN {
        LET $row=(SELECT * FROM raw_captures WHERE uuid=$identity)[0];
        LET $state=(SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id=$org AND source_kind='raw_capture' AND source_id=$identity)[0];
        LET $association=(SELECT * FROM memory_derivations WHERE organization_id=$org AND target_kind='raw_capture' AND target_id=$identity)[0];
        RETURN {row:$row,state:$state,association:$association,
            row_sha:IF $row=NONE THEN NULL ELSE crypto::sha256(type::string($row)) END,
            state_sha:IF $state=NONE THEN NULL ELSE crypto::sha256(type::string($state)) END,
            association_sha:IF $association=NONE THEN NULL ELSE crypto::sha256(type::string($association)) END,
            body_sha:IF $row=NONE THEN NULL ELSE crypto::sha256(type::string([$row.title,$row.raw_content,$row.metadata,$row.derivation_required])) END,
            audience_sha:IF $row=NONE THEN NULL ELSE crypto::sha256(type::string([$row.memory_scope,$row.scope_key,$row.principal_id,$row.project_id])) END};
    };""",
        org=org,
        identity=identity,
    )


def witness(row, cut):
    return ArchiveStoreWitness(
        store="content",
        identity="raw_captures:" + row.destination_id,
        row_sha256=cut["row_sha"],
        state_sha256=cut["state_sha"],
        associations_sha256=cut["association_sha"],
    )


async def install(native, value, indexes):
    for index in indexes:
        item = raw_rows(value)[index]
        record = raw_memory_record(
            raw_memory_from_record(
                {
                    **item.body,
                    "uuid": item.row.destination_id,
                    "organization_id": value.plan.organization_id,
                }
            )
        )
        await replace_raw_memory_records_bulk(native.client, [record])


async def no_receipt(native, value):
    result = await native.client.execute_query(
        "SELECT * FROM archive_phase_receipts WHERE run_id=$run; SELECT * FROM archive_phase_controls WHERE run_id=$run;",
        run=value.run_id,
    )
    assert result == [] or result == [[], []]


@pytest.mark.asyncio(loop_scope="module")
async def test_raw_compiler_native_mixed_actual_outcomes_and_nanosecond_metadata(native, tmp_path):
    parsed, plan = inputs(tmp_path, count=5, protected=(4,))
    initial = prepared(parsed, plan)
    await install(native, initial, (1, 2, 3))
    items = raw_rows(initial)
    org = plan.organization_id
    await native.client.execute_query(
        "UPDATE raw_captures SET metadata.user_timestamp=$timestamp WHERE uuid=$id;",
        timestamp=_TIMESTAMP[:-2] + "8Z",
        id=items[2].row.destination_id,
    )
    await native.client.execute_query(
        "DELETE raw_captures WHERE uuid=$id;", id=items[3].row.destination_id
    )
    changes, before = {}, {}
    for index, disposition in [
        (1, ArchiveDisposition.SKIPPED),
        (2, ArchiveDisposition.CONFLICTED),
        (3, ArchiveDisposition.CONFLICTED),
    ]:
        row = items[index].row
        cut = await snapshot(native.client, org, row.destination_id)
        before[row.destination_id] = cut
        changes[row.original_id] = {"disposition": disposition, "witnesses": (witness(row, cut),)}
    value = prepared(parsed, change_rows(plan, changes))
    key, token, tx = phase(value, url=native.url)
    await native.client.execute_query(tx.query, **tx.parameters)
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    assert receipt is not None
    counts = receipt.counts[0]
    assert (counts.created, counts.skipped, counts.conflicted, counts.quarantined) == (1, 1, 2, 1)
    assert len(receipt.introduced) == 1
    introduced = receipt.introduced[0]
    created = await snapshot(native.client, org, items[0].row.destination_id)
    assert introduced.destination_id == items[0].row.destination_id
    assert introduced.physical_id == str(created["row"]["id"])
    assert introduced.row_sha256 == created["row_sha"]
    assert introduced.body_sha256 == created["body_sha"]
    assert introduced.audience_sha256 == created["audience_sha"]
    assert introduced.source_incarnation == created["state"]["incarnation"]
    assert (
        introduced.source_generation == introduced.source_state_revision == introduced.revision == 1
    )
    assert created["row"]["metadata"]["user_timestamp"] == _TIMESTAMP
    assert created["row"]["metadata"]["user_nested"] == {"values": [17, "keep"]}
    for identity, cut in before.items():
        assert await snapshot(native.client, org, identity) == cut
    assert all(
        row["destination_id"] is None
        for row in tx.parameters["archive_raw_candidates"]
        if row["disposition"] == "quarantined"
    )
    assert (
        await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
        == receipt
    )
    with pytest.raises(SurrealError, match=r"admission changed|already exists|index"):
        await native.client.execute_query(tx.query, **tx.parameters)
    observe(
        {"case": "mixed_outcomes", "receipt": receipt.model_dump(mode="json"), "created": created}
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_raw_compiler_native_all_quarantined_is_metadata_only(native, tmp_path):
    parsed, plan = inputs(tmp_path, count=2, protected=(0, 1))
    value = prepared(parsed, plan)
    key, token, tx = phase(value, url=native.url)
    await native.client.execute_query(tx.query, **tx.parameters)
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    assert receipt.counts[0].quarantined == 2 and not receipt.introduced
    assert not await native.client.execute_query(
        "SELECT * FROM raw_captures WHERE organization_id=$org;", org=plan.organization_id
    )
    assert not await native.client.execute_query(
        "SELECT * FROM source_states WHERE organization_id=$org;", org=plan.organization_id
    )
    with pytest.raises(SurrealError, match=r"admission changed|already exists|index"):
        await native.client.execute_query(tx.query, **tx.parameters)


async def existing_plan(
    native, tmp_path, *, association=False, disposition=ArchiveDisposition.SKIPPED
):
    parsed, plan = inputs(tmp_path)
    initial = prepared(parsed, plan)
    await install(native, initial, (0,))
    row = raw_rows(initial)[0].row
    if association:
        await publish(native.client, plan.organization_id, row.destination_id)
    cut = await snapshot(native.client, plan.organization_id, row.destination_id)
    changed = change_rows(
        plan, {row.original_id: {"disposition": disposition, "witnesses": (witness(row, cut),)}}
    )
    return parsed, changed, prepared(parsed, changed), cut


async def publish(client, org, identity):
    await client.execute_query(
        "CREATE memory_derivations CONTENT $association;",
        association={
            "organization_id": org,
            "target_kind": "raw_capture",
            "target_id": identity,
            "body_sha256": "a" * 64,
            "principal_id": str(uuid4()),
            "authority_ceiling": {},
            "observations": [],
            "active": True,
        },
    )


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    "mutation",
    [
        "body",
        "one_ns_metadata",
        "clock",
        "physical_replacement",
        "incarnation",
        "generation",
        "revision",
        "deleted",
        "missing_ledger",
        "protection",
        "association",
    ],
)
async def test_raw_compiler_native_stale_guard_rolls_back(native, tmp_path, mutation):
    _, plan, value, before = await existing_plan(native, tmp_path)
    identity = raw_rows(value)[0].row.destination_id
    key, token, tx = phase(value, url=native.url)
    if mutation in {"body", "one_ns_metadata", "clock", "protection"}:
        fields = {
            "body": "raw_content='changed'",
            "one_ns_metadata": "metadata.user_timestamp=$timestamp",
            "clock": "captured_at=<datetime>$timestamp",
            "protection": "derivation_required=true",
        }
        await native.client.execute_query(
            "UPDATE raw_captures SET " + fields[mutation] + " WHERE uuid=$id;",
            id=identity,
            timestamp=_TIMESTAMP[:-2] + "8Z",
        )
    elif mutation in {"incarnation", "generation", "revision", "deleted"}:
        changed = (
            str(uuid4())
            if mutation == "incarnation"
            else True
            if mutation == "deleted"
            else before["state"][mutation] + 1
        )
        await native.client.execute_query(
            f"UPDATE source_states SET {mutation}=$changed WHERE organization_id=$org AND source_kind='raw_capture' AND source_id=$id;",
            changed=changed,
            org=plan.organization_id,
            id=identity,
        )
    elif mutation == "missing_ledger":
        await native.client.execute_query(
            "DELETE source_states WHERE organization_id=$org AND source_id=$id;",
            org=plan.organization_id,
            id=identity,
        )
    elif mutation == "association":
        await publish(native.client, plan.organization_id, identity)
    else:
        await native.client.execute_query("DELETE raw_captures WHERE uuid=$id;", id=identity)
        await install(native, value, (0,))
        assert str(
            (await snapshot(native.client, plan.organization_id, identity))["row"]["id"]
        ) != str(before["row"]["id"])
    with pytest.raises(SurrealError, match=r"witness changed|decision requires|skip"):
        await native.client.execute_query(tx.query, **tx.parameters)
    await no_receipt(native, value)
    observe(
        {
            "case": "stale_guard",
            "mutation": mutation,
            "before": before,
            "after": await snapshot(native.client, plan.organization_id, identity),
            "key": key.model_dump(mode="json"),
            "token": token,
        }
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_raw_compiler_native_full_association_guard_and_state_bookkeeping(native, tmp_path):
    first_path, second_path = tmp_path / "ordinary", tmp_path / "protected"
    first_path.mkdir()
    second_path.mkdir()
    _, plan, value, before = await existing_plan(native, first_path)
    key, token, tx = phase(value, url=native.url)
    await native.client.execute_query(
        "UPDATE source_states SET validation_write_witness=type::string(rand::uuid()) WHERE organization_id=$org AND source_id=$id;",
        org=plan.organization_id,
        id=raw_rows(value)[0].row.destination_id,
    )
    await native.client.execute_query(tx.query, **tx.parameters)
    assert (
        await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    ).counts[0].skipped == 1
    assert (
        await snapshot(native.client, plan.organization_id, raw_rows(value)[0].row.destination_id)
        == before
    )
    _, plan, value, before = await existing_plan(
        native, second_path, association=True, disposition=ArchiveDisposition.CONFLICTED
    )
    _, _, tx = phase(value, url=native.url)
    identity = raw_rows(value)[0].row.destination_id
    await native.client.execute_query(
        "UPDATE memory_derivations SET active=false WHERE organization_id=$org AND target_kind='raw_capture' AND target_id=$id;",
        org=plan.organization_id,
        id=identity,
    )
    after = await snapshot(native.client, plan.organization_id, identity)
    assert before["row_sha"] == after["row_sha"] and before["state_sha"] == after["state_sha"]
    assert before["association_sha"] != after["association_sha"]
    with pytest.raises(SurrealError, match="witness changed"):
        await native.client.execute_query(tx.query, **tx.parameters)
    await no_receipt(native, value)


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("history", ["retained_state", "association", "foreign_org"])
async def test_raw_compiler_native_create_absence_includes_history_and_global_uuid(
    native, tmp_path, history
):
    parsed, plan = inputs(tmp_path)
    value = prepared(parsed, plan)
    identity = raw_rows(value)[0].row.destination_id
    _, _, tx = phase(value, url=native.url)
    if history == "foreign_org":
        item = raw_rows(value)[0]
        record = raw_memory_record(
            raw_memory_from_record({**item.body, "uuid": identity, "organization_id": str(uuid4())})
        )
        await replace_raw_memory_records_bulk(native.client, [record])
    else:
        await install(native, value, (0,))
        if history == "association":
            await publish(native.client, plan.organization_id, identity)
        await native.client.execute_query("DELETE raw_captures WHERE uuid=$id;", id=identity)
    with pytest.raises(SurrealError, match=r"not absent|witness changed"):
        await native.client.execute_query(tx.query, **tx.parameters)
    await no_receipt(native, value)


def native_conflicts(trace):
    return [
        item
        for event in trace
        if isinstance(event["response"], dict)
        for item in event["response"].get("result", [])
        if isinstance(item, dict)
        and item.get("status") == "ERR"
        and "Transaction write conflict" in str(item.get("result"))
    ]


def delay_before(client, marker):
    original = client._send_query

    async def delayed(socket, query, *, params, raw):
        # Connection health queries pass through; only the actual transaction pauses.
        return await original(
            socket, query.replace(marker, "SLEEP 1s; " + marker), params=params, raw=raw
        )

    client._send_query = delayed
    return original


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("winner", ["canonical", "phase"])
@pytest.mark.parametrize(
    "mutation",
    [
        "source_edit",
        "source_create",
        "association_create",
        "association_update",
        "association_delete",
        "rebind_old",
        "rebind_new",
        "unrelated",
    ],
)
async def test_raw_compiler_native_actual_commit_overlap(native, tmp_path, winner, mutation):
    if is_embedded_surreal_url(native.url):
        pytest.skip("actual write conflict controls require separate native server sockets")
    parsed, plan = inputs(tmp_path)
    initial = prepared(parsed, plan)
    row = raw_rows(initial)[0].row
    identity, org = row.destination_id, plan.organization_id
    record = raw_memory_record(
        raw_memory_from_record(
            {**raw_rows(initial)[0].body, "uuid": identity, "organization_id": org}
        )
    )
    other = str(uuid4())
    other_record = {**record, "uuid": other, "source_id": other}
    if mutation != "source_create":
        await install(native, initial, (0,))
    if mutation in {"rebind_old", "rebind_new", "unrelated"}:
        await replace_raw_memory_records_bulk(native.client, [other_record])
    if mutation.startswith("association") or mutation.startswith("rebind"):
        await publish(native.client, org, identity)
        if mutation == "association_create":
            await native.client.execute_query(
                "DELETE memory_derivations WHERE organization_id=$org AND target_id=$id;",
                org=org,
                id=identity,
            )
        if mutation.startswith("rebind"):
            await publish(native.client, org, other)
            absent = identity if mutation == "rebind_new" else other
            await native.client.execute_query(
                "DELETE memory_derivations WHERE organization_id=$org AND target_id=$id;",
                org=org,
                id=absent,
            )
    before = await snapshot(native.client, org, identity)
    disposition = (
        ArchiveDisposition.CREATED
        if mutation == "source_create"
        else ArchiveDisposition.CONFLICTED
        if mutation.startswith(("association", "rebind"))
        else ArchiveDisposition.SKIPPED
    )
    plan = change_rows(
        plan, {row.original_id: {"disposition": disposition, "witnesses": (witness(row, before),)}}
    )
    value = prepared(parsed, plan)
    key, token, tx = phase(value, url=native.url)
    start = len(native.trace)
    if mutation in {"source_edit", "source_create", "unrelated"}:
        if mutation == "source_create":
            saved = record
        else:
            existing = await snapshot(
                native.client, org, other if mutation == "unrelated" else identity
            )
            saved = raw_memory_record(raw_memory_from_record(existing["row"]))
        saved = {**saved, "raw_content": "canonical overlap mutation"}

        async def canonical():
            return await replace_raw_memory_records_bulk(native.writer, [saved])
    else:
        if mutation == "association_create":
            operation = "CREATE memory_derivations CONTENT $association;"
        elif mutation == "association_update":
            operation = "UPDATE memory_derivations SET active=false WHERE organization_id=$org AND target_id=$old;"
        elif mutation == "association_delete":
            operation = "DELETE memory_derivations WHERE organization_id=$org AND target_id=$old;"
        else:
            operation = "UPDATE memory_derivations SET target_id=$new WHERE organization_id=$org AND target_id=$old;"
        old, new = (other, identity) if mutation == "rebind_new" else (identity, other)

        async def canonical():
            return await native.writer.execute_query(
                "BEGIN TRANSACTION;" + operation + "COMMIT TRANSACTION;",
                org=org,
                old=old,
                new=new,
                association={
                    "organization_id": org,
                    "target_kind": "raw_capture",
                    "target_id": identity,
                    "body_sha256": "a" * 64,
                    "principal_id": str(uuid4()),
                    "authority_ceiling": {},
                    "observations": [],
                    "active": True,
                },
            )

    delayed_client = native.client if winner == "canonical" else native.writer
    marker = (
        "CREATE raw_captures CONTENT $archive_raw_record;"
        if mutation == "source_create"
        else "LET $source_states_to_fence = [$archive_raw_state];"
    )
    original = delay_before(
        delayed_client, marker if winner == "canonical" else "COMMIT TRANSACTION;"
    )

    async def apply():
        return await native.client.execute_query(tx.query, **tx.parameters)

    try:
        pending = asyncio.create_task(apply() if winner == "canonical" else canonical())
        await asyncio.sleep(0.3)
        assert not pending.done()
        result = await asyncio.gather(
            canonical() if winner == "canonical" else apply(), return_exceptions=True
        )
        assert not isinstance(result[0], Exception), str(result[0])
        assert not pending.done()
        delayed_result = await asyncio.gather(pending, return_exceptions=True)
    finally:
        delayed_client._send_query = original
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    trace = native.trace[start:]
    roles = {
        role: {event["socket"] for event in trace if event["role"] == role}
        for role in ["phase", "canonical_writer"]
    }
    assert (
        roles["phase"]
        and roles["canonical_writer"]
        and roles["phase"].isdisjoint(roles["canonical_writer"])
    )
    if mutation == "unrelated":
        assert not isinstance(delayed_result[0], Exception), str(delayed_result[0])
        assert not native_conflicts(trace)
        assert receipt is not None and receipt.counts[0].skipped == 1
        assert (await snapshot(native.client, org, identity)) == before
    else:
        assert native_conflicts(trace)
        assert (receipt is None) == (winner == "canonical")
        if winner == "phase":
            assert (await snapshot(native.client, org, identity))["row_sha"] == (
                receipt.introduced[0].row_sha256
                if mutation == "source_create"
                else before["row_sha"]
            )
    observe(
        {
            "case": "actual_commit_overlap",
            "mutation": mutation,
            "winner": winner,
            "native_conflicts": native_conflicts(trace),
            "sockets": {k: sorted(v) for k, v in roles.items()},
            "receipt": None if receipt is None else receipt.model_dump(mode="json"),
            "before": before,
            "after": await snapshot(native.client, org, identity),
        }
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"raw_source_ids": ["requires-source-observation"]},
        {"raw_source_ids": {"malformed": "unsupported"}},
    ],
)
def test_raw_compiler_rejects_raw_source_dependency_before_compilation(tmp_path, metadata):
    parsed, plan = inputs(tmp_path, metadata=metadata)
    value = prepared(parsed, plan)
    assert raw_rows(value)[0].row.disposition is ArchiveDisposition.CREATED
    with pytest.raises(ValueError, match="dependency or provenance"):
        phase(value)


@pytest.mark.asyncio(loop_scope="module")
async def test_raw_compiler_native_user_nulls_and_query_shaped_data_remain_exact(native, tmp_path):
    nulls = {"nullable": None, "values": [17, None, "keep"], "nested": {"x": None}}
    payload = {
        "user_nulls": nulls,
        'odd"key; COMMIT TRANSACTION;': 'NULL; BEGIN TRANSACTION; "quoted" \\ path\nnext',
    }
    parsed, plan = inputs(tmp_path, metadata=payload)
    value = prepared(parsed, plan)
    item = raw_rows(value)[0]
    assert item.body["metadata"]["user_nulls"] == nulls
    key, token, tx = phase(value, url=native.url)
    await native.client.execute_query(tx.query, **tx.parameters)
    cut = await snapshot(native.client, plan.organization_id, item.row.destination_id)
    assert cut["row"]["metadata"]["user_nulls"] == nulls
    assert (
        cut["row"]["metadata"]['odd"key; COMMIT TRANSACTION;']
        == payload['odd"key; COMMIT TRANSACTION;']
    )
    null_proof = await native.client.execute_query(
        "SELECT metadata.user_nulls.nullable = NULL AS object_null, metadata.user_nulls.nested.x = NULL AS nested_null, metadata.user_nulls.values[1] = NULL AS array_null, type::string(metadata.user_nulls) AS native_metadata FROM raw_captures WHERE uuid=$id;",
        id=item.row.destination_id,
    )
    assert (
        null_proof[0]["object_null"]
        and null_proof[0]["nested_null"]
        and null_proof[0]["array_null"]
    )
    assert "nullable: NULL" in null_proof[0]["native_metadata"]
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    assert receipt.introduced[0].row_sha256 == cut["row_sha"]
    assert receipt.introduced[0].body_sha256 == cut["body_sha"]
    observe(
        {
            "case": "exact_user_nulls",
            "receipt": receipt.model_dump(mode="json"),
            "native_null_proof": null_proof,
            "body": item.body,
        }
    )


@pytest.mark.parametrize(
    "field,value",
    [("expected_revision", True), ("expected_revision", -1), ("expected_token", "invalid")],
)
def test_raw_compiler_pure_phase_admission_shape_is_strict(tmp_path, field, value):
    parsed, plan = inputs(tmp_path)
    records = prepared(parsed, plan)
    key = ArchivePhaseKey(
        binding=records.binding, store="content", action="apply", batch_sequence=0
    )
    options = {"expected_revision": 0, "expected_token": str(uuid4()), field: value}
    with pytest.raises(ValueError):
        prepare_archive_raw_apply(prepared=records, key=key, url="memory://", **options)


@pytest.mark.asyncio(loop_scope="module")
async def test_raw_compiler_native_quarantine_batch_cannot_relabel_or_double_count(
    native, tmp_path
):
    parsed, plan = inputs(tmp_path, protected=(0,))
    value = prepared(parsed, plan)
    key, token, first = phase(value, url=native.url)
    await native.client.execute_query(first.query, **first.parameters)
    _, _, second = phase(value, url=native.url, key=key, token=token, revision=1)
    with pytest.raises(SurrealError, match=r"already exists|index"):
        await native.client.execute_query(second.query, **second.parameters)
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    assert receipt.committed_revision == 1 and receipt.counts[0].quarantined == 1
    controls = await native.client.execute_query(
        "SELECT revision FROM archive_phase_controls WHERE run_id=$run;", run=value.run_id
    )
    assert controls == [{"revision": 1}]
    receipts = await native.client.execute_query(
        "SELECT * FROM archive_phase_receipts WHERE run_id=$run;", run=value.run_id
    )
    assert len(receipts) == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_raw_compiler_native_prior_retirement_still_denies_create_with_missing_ledger(
    native, tmp_path
):
    parsed, plan = inputs(tmp_path)
    value = prepared(parsed, plan)
    key, token, apply = phase(value, url=native.url)
    await native.client.execute_query(apply.query, **apply.parameters)
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    identity = receipt.introduced[0].destination_id
    rollback_key = key.model_copy(update={"action": "rollback"})
    rollback = prepare_archive_phase_transaction(
        key=rollback_key,
        url=native.url,
        expected_revision=1,
        expected_token=token,
        rollback_token=str(uuid4()),
        terminal=True,
        retirement_candidates=receipt.introduced,
        writer_statements="DELETE raw_captures WHERE organization_id=$org AND uuid=$identity; LET $sibyl_archive_phase_outcomes=[{kind:'raw_capture',disposition:'retired',destination_id:$identity}];",
        writer_parameters={"org": plan.organization_id, "identity": identity},
    )
    await native.client.execute_query(rollback.query, **rollback.parameters)
    retired = await native.client.execute_query(
        "SELECT * FROM archive_phase_receipts WHERE run_id=$run AND action='rollback';",
        run=value.run_id,
    )
    assert (
        len(retired) == 1 and retired[0]["retired"][0]["introduced"]["destination_id"] == identity
    )
    tombstone = await snapshot(native.client, plan.organization_id, identity)
    assert tombstone["row"] is None and tombstone["state"]["deleted"]
    # Owned corrupt-history control isolates durable retirement from the ledger cut.
    await native.client.execute_query(
        "DELETE source_states WHERE organization_id=$org AND source_kind='raw_capture' AND source_id=$identity;",
        org=plan.organization_id,
        identity=identity,
    )
    new_run = prepared(parsed, plan)
    _, _, create = phase(new_run, url=native.url)
    with pytest.raises(SurrealError, match="not absent"):
        await native.client.execute_query(create.query, **create.parameters)
    await no_receipt(native, new_run)
    assert (await snapshot(native.client, plan.organization_id, identity))["row"] is None
    assert (
        await native.client.execute_query(
            "SELECT * FROM archive_phase_receipts WHERE run_id=$run AND action='rollback';",
            run=value.run_id,
        )
        == retired
    )
