"""Actual native serialization and mutation controls in owned namespaces."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from surrealdb import RecordID
from surrealdb.cbor import CBORSimpleValue
from surrealdb.data.types.datetime import Datetime

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.migrate.archive_native_values import archive_native_value_parameters
from sibyl_core.services.archive_native_capture import (
    ARCHIVE_NATIVE_RECORD_TABLES,
    capture_archive_native_records,
)


def evidence(value: object) -> None:
    destination = os.environ.get("SIBYL_NATIVE_CAPTURE_EVIDENCE")
    if destination:
        with Path(destination).open("a") as stream:
            stream.write(json.dumps(value, sort_keys=True, default=str) + "\n")


@pytest.fixture
async def native_capture_store() -> AsyncIterator[tuple[SurrealContentClient, str, str]]:
    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://")
    namespace = "archive_native_capture_" + uuid4().hex
    client = SurrealContentClient(
        url=url,
        namespace=namespace,
        pool_size=1,
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
    )
    try:
        for table in sorted(ARCHIVE_NATIVE_RECORD_TABLES):
            await client.execute_query(f"DEFINE TABLE {table} SCHEMALESS PERMISSIONS NONE;")
        evidence({"namespace": namespace, "url": url, "fixture": "owned"})
        yield client, url, namespace
    finally:
        await client.execute_query(f"REMOVE NAMESPACE {namespace};")
        assert namespace not in (await client.execute_query("INFO FOR ROOT;"))["namespaces"]
        evidence({"namespace": namespace, "cleanup": "removed_and_root_inventory_checked"})
        await client.close()


async def create(client, record, value):
    await client.execute_query(
        "CREATE $record CONTENT $value RETURN NONE;", record=record, value=value
    )


@pytest.mark.asyncio
async def test_archive_native_capture_null_none_nanos_keys_and_typed_ids(
    native_capture_store,
) -> None:
    client, url, namespace = native_capture_store
    key = "a.b" + chr(96) + "$[0]\0雪"
    uid = UUID("12345678-1234-4234-8234-123456789abc")
    clock = Datetime("2026-09-30T01:02:03.123456789Z")
    row = {
        "unknown": {key: ["nul\0雪", None, CBORSimpleValue(22), clock, uid, True, 7, 1.25]},
        "null": CBORSimpleValue(22),
        "ref": RecordID(
            "entity", [uid, {"key": "nul\0雪", "clock": clock, "null": CBORSimpleValue(22)}]
        ),
        "query_shaped_ref": RecordID("ref table; RETURN 'text'", "id:$caller"),
        "string_ref": "entity:7",
        "string_date": "2026-09-30T01:02:03.123456789Z",
        "string_uuid": str(uid),
    }
    selected = (RecordID("raw_captures", "7"), RecordID("raw_captures", 7))
    await create(client, selected[0], row)
    await create(client, selected[1], {"physical": "integer"})
    calls = []

    async def logged(query, **params):
        calls.append(
            {"query": query, "requests": params.get("requests"), "expected": params.get("expected")}
        )
        return await client.execute_query(query, **params)

    captured = await capture_archive_native_records(logged, url=url, record_ids=selected)
    restored = archive_native_value_parameters(captured.envelope)
    proof = await client.execute_query(
        """RETURN {
            fingerprint:crypto::sha256(type::string($rows)),
            none:$rows[0].unknown[$key][1]=NONE,
            null:$rows[0].unknown[$key][2]=NULL,
            clock:type::string($rows[0].unknown[$key][3]),
            uuid:type::string($rows[0].unknown[$key][4]),
            string_ref:$rows[0].string_ref='entity:7',
            identifier:record::id($rows[0].id),
            reference_clock:type::string(record::id($rows[0].ref)[1].clock)
        };""",
        rows=restored,
        key=key,
    )
    assert proof["fingerprint"] == captured.native_sha256
    assert proof["none"] and proof["null"] and proof["string_ref"]
    assert proof["clock"] == proof["reference_clock"] == clock.dt
    assert proof["uuid"] == str(uid)
    assert type(proof["identifier"]) is str and proof["identifier"] == "7"
    assert restored[1]["id"].id == 7 and type(restored[1]["id"].id) is int
    assert restored[0]["unknown"][key][0] == "nul\0雪"
    assert type(restored[0]["ref"].id[0]) is UUID
    assert restored[0]["query_shaped_ref"].table_name == "ref table; RETURN 'text'"
    assert all("nul\0雪" not in call["query"] and key not in call["query"] for call in calls)
    assert len(calls) >= 4 and all(call["expected"] == captured.native_sha256 for call in calls[1:])
    evidence(
        {
            "namespace": namespace,
            "proof": proof,
            "envelope": captured.envelope.payload,
            "calls": calls,
        }
    )


@pytest.mark.asyncio
async def test_archive_native_capture_history_tables_whole_row_and_order(
    native_capture_store,
) -> None:
    client, url, namespace = native_capture_store
    selected = tuple(RecordID(table, uuid4()) for table in sorted(ARCHIVE_NATIVE_RECORD_TABLES))
    for index, record in enumerate(selected):
        await create(
            client, record, {"ordinal": index, "history": {"retired": CBORSimpleValue(22)}}
        )
    captured = await capture_archive_native_records(
        client.execute_query, url=url, record_ids=selected
    )
    assert [row["ordinal"] for row in captured.rows] == list(range(len(selected)))
    restored = archive_native_value_parameters(captured.envelope)
    assert [row["id"].table_name for row in restored] == [record.table_name for record in selected]
    assert all(type(row["id"].id) is UUID for row in restored)
    evidence(
        {
            "namespace": namespace,
            "history_cut_sha256": captured.native_sha256,
            "tables": [r.table_name for r in selected],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_kind", ["unknown", "one_nanosecond", "delete"])
async def test_archive_native_capture_rejects_real_between_round_mutation(
    native_capture_store,
    mutation_kind,
) -> None:
    client, url, namespace = native_capture_store
    record = RecordID("source_states", "ledger")
    await create(
        client,
        record,
        {
            "history": {"absent": True},
            "unknown": 1,
            "clock": Datetime("2026-09-30T01:02:03.123456789Z"),
        },
    )
    mutation = SurrealContentClient(
        url=url,
        namespace=namespace,
        pool_size=1,
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
    )
    calls = 0
    mutator = client if is_embedded_surreal_url(url) else mutation
    try:

        async def changed(query, **params):
            nonlocal calls
            calls += 1
            result = await client.execute_query(query, **params)
            if calls == 1:
                query = {
                    "unknown": "UPDATE $record SET unknown = 2 RETURN NONE;",
                    "one_nanosecond": "UPDATE $record SET clock = $clock RETURN NONE;",
                    "delete": "DELETE $record RETURN NONE;",
                }[mutation_kind]
                await mutator.execute_query(
                    query, record=record, clock=Datetime("2026-09-30T01:02:03.123456790Z")
                )
            return result

        message = (
            "missing physical record" if mutation_kind == "delete" else "changed during capture"
        )
        with pytest.raises(Exception, match=message):
            await capture_archive_native_records(changed, url=url, record_ids=(record,))
        assert calls == 2
        proof = await client.execute_query(
            "RETURN {unknown:(SELECT VALUE unknown FROM $record)[0], "
            "clock:IF (SELECT * FROM $record)[0].clock = NONE THEN '' "
            "ELSE type::string((SELECT * FROM $record)[0].clock) END, "
            "rows:array::len(SELECT id FROM $record)};",
            record=record,
        )
        assert proof["unknown"] == (
            None if mutation_kind == "delete" else 2 if mutation_kind == "unknown" else 1
        )
        assert proof["rows"] == (0 if mutation_kind == "delete" else 1)
        if mutation_kind != "delete":
            assert proof["clock"] == (
                "2026-09-30T01:02:03.123456790Z"
                if mutation_kind == "one_nanosecond"
                else "2026-09-30T01:02:03.123456789Z"
            )
        evidence(
            {
                "namespace": namespace,
                "mutation": "actual_same_engine"
                if is_embedded_surreal_url(url)
                else "actual_second_client",
                "kind": mutation_kind,
                "rounds": calls,
                "proof": proof,
            }
        )
    finally:
        await mutation.close()


@pytest.mark.asyncio
async def test_archive_native_capture_missing_unsupported_kind_and_selectors(
    native_capture_store,
) -> None:
    client, url, _ = native_capture_store
    with pytest.raises(Exception, match="missing physical record"):
        await capture_archive_native_records(
            client.execute_query, url=url, record_ids=(RecordID("raw_captures", "absent"),)
        )
    record = RecordID("raw_captures", "decimal")
    await create(client, record, {"decimal": Decimal("0.1000000000000000000001")})
    with pytest.raises(Exception, match="Unsupported native value"):
        await capture_archive_native_records(client.execute_query, url=url, record_ids=(record,))
    calls = 0

    async def forbidden(query, **params):
        nonlocal calls
        calls += 1
        raise AssertionError("invalid selection queried the store")

    for selected in [
        (),
        [record],
        (record, record),
        (RecordID("user", "authority"),),
        ("raw_captures:1",),
    ]:
        with pytest.raises(ValueError):
            await capture_archive_native_records(forbidden, url=url, record_ids=selected)
    assert calls == 0


@pytest.mark.asyncio
async def test_archive_native_capture_rejects_wrong_descriptors_before_sealing() -> None:
    async def malformed(query, **params):
        return {"fingerprint": "a" * 64, "descriptors": []}

    with pytest.raises(ValueError, match="cardinality"):
        await capture_archive_native_records(
            malformed, url="memory://", record_ids=(RecordID("raw_captures", 1),)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("nul_physical_key", [False, True])
async def test_archive_native_capture_complex_physical_identifier_snapshot(
    native_capture_store,
    nul_physical_key,
) -> None:
    client, url, namespace = native_capture_store
    identifier = [
        "nul\0雪" if nul_physical_key else "plain雪",
        7,
        True,
        1.25,
        None,
        CBORSimpleValue(22),
        Datetime("2026-09-30T01:02:03.000000001Z"),
        {"a.b": UUID("12345678-1234-4234-8234-123456789abc")},
    ]
    record = RecordID("raw_captures", identifier)
    if nul_physical_key and is_embedded_surreal_url(url):
        # Embedded storage cannot encode NUL physical keys. This ordinary
        # CREATE control precedes capture and records the backend boundary.
        with pytest.raises(
            Exception, match="Key encoding error: to be serialized string contained a null byte"
        ):
            await create(client, record, {"payload": "physical"})
        count = await client.execute_query("RETURN array::len(SELECT id FROM raw_captures);")
        assert count == 0
        with pytest.raises(
            Exception, match="Key encoding error: to be serialized string contained a null byte"
        ):
            await capture_archive_native_records(
                client.execute_query, url=url, record_ids=(record,)
            )
        evidence(
            {
                "namespace": namespace,
                "baseline": "embedded_physical_NUL_key_rejected",
                "count": count,
            }
        )
        return
    await create(client, record, {"payload": "physical"})
    original = identifier[0]
    calls = 0

    async def mutate_caller(query, **params):
        nonlocal calls
        calls += 1
        if calls == 1:
            identifier[0] = "caller changed after selection"
        return await client.execute_query(query, **params)

    captured = await capture_archive_native_records(mutate_caller, url=url, record_ids=(record,))
    restored = archive_native_value_parameters(captured.envelope)[0]["id"]
    assert restored.id[0] == original
    assert restored.id[4] is None and restored.id[5] == CBORSimpleValue(22)
    assert restored.id[6].dt == "2026-09-30T01:02:03.000000001Z"
    assert type(restored.id[7]["a.b"]) is UUID
    selected = (
        restored,
        RecordID(
            "raw_captures",
            [
                *restored.id[:6],
                Datetime("2026-09-30T01:02:03.000000001Z"),
                restored.id[7],
            ],
        ),
    )
    with pytest.raises(ValueError, match="unique"):
        await capture_archive_native_records(client.execute_query, url=url, record_ids=selected)
    evidence(
        {
            "namespace": namespace,
            "complex_physical_id_sha256": captured.native_sha256,
            "calls": calls,
        }
    )
