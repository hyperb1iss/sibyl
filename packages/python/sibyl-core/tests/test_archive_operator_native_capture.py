"""Actual product capture against native ordinary schemas and owned scopes."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from surrealdb import AsyncSurreal, RecordID
from surrealdb.cbor import CBORSimpleValue

from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
    bootstrap_content_schema,
)
from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.backends.surreal.schema_ownership import _LEASE_DEFINITIONS
from sibyl_core.migrate.archive_native_values import archive_native_value_parameters
from sibyl_core.services import archive_operator_native_capture as product
from sibyl_core.services.graph_client import SurrealGraphClient


def evidence(value):
    target = os.environ.get("SIBYL_OPERATOR_NATIVE_ROOT_EVIDENCE")
    if target:
        with Path(target).open("a") as stream:
            stream.write(json.dumps(value, default=str, sort_keys=True) + "\n")


async def raw(connection, query, **params):
    return product._last_result(await connection.query_raw(query, params))


@pytest.fixture
async def native_operator():
    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "")
    if not url.startswith(("ws://", "wss://")):
        pytest.skip("native WebSocket transaction handles required")
    credentials = {
        "username": os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        "password": os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
    }
    organization = str(uuid4())
    prefix = "operator_capture_" + uuid4().hex + "_"
    scopes = (
        product.ArchiveOperatorScope(
            "graph", prefix + organization.replace("-", ""), "graph", organization
        ),
        product.ArchiveOperatorScope("content", prefix + "content", "content"),
        product.ArchiveOperatorScope("auth", prefix + "auth", "auth"),
    )
    authorization = product.ArchiveOperatorAuthorization(
        "native-test-operator", str(uuid4()), url, prefix, scopes
    )
    clients = [
        SurrealGraphClient(
            group_id=organization, namespace_prefix=prefix, url=url, pool_size=1, **credentials
        ),
        SurrealContentClient(namespace=scopes[1].namespace, url=url, pool_size=1, **credentials),
        SurrealAuthClient(namespace=scopes[2].namespace, url=url, pool_size=1, **credentials),
    ]
    admin = AsyncSurreal(url)
    namespaces = [scope.namespace for scope in scopes]
    try:
        await admin.connect()
        await admin.signin(credentials)
        await bootstrap_schema(clients[0])
        await bootstrap_content_schema(clients[1])
        await bootstrap_auth_schema(clients[2])
        await clients[0].execute_query(
            "CREATE entity:same SET uuid=$uuid,group_id=$org,name='snapshot',entity_type='note',attributes={values:[NULL,NONE],clock:d'2026-10-02T00:00:00.123456789Z',reference:entity:[7,u'12345678-1234-4234-8234-123456789abc']};",
            uuid=str(uuid4()),
            org=organization,
        )
        await clients[1].execute_query(
            "CREATE entity:same SET uuid=$uuid,organization_id=$org;",
            uuid=str(uuid4()),
            org=organization,
        )
        await clients[2].execute_query(
            "CREATE organizations:same SET uuid=$org,settings={secret:'operator-only',values:[NULL,NONE]};",
            org=organization,
        )
        evidence({"namespaces": namespaces, "fixture": "owned_ordinary_schemas"})
        yield authorization, credentials, admin, clients, namespaces
    finally:
        for client in clients:
            await client.close()
        try:
            present = (await raw(admin, "INFO FOR ROOT;"))["namespaces"]
            removed = []
            for namespace in namespaces:
                if namespace in present:
                    await raw(admin, f"REMOVE NS {namespace};")
                    removed.append(namespace)
            inventory = await raw(admin, "INFO FOR ROOT;")
            assert not set(namespaces) & set(inventory["namespaces"])
            evidence({"namespaces": namespaces, "removed": removed, "inventory_absent": True})
        finally:
            await admin.close()


@pytest.mark.asyncio
async def test_operator_root_complete_snapshot_fidelity_catalog_and_qualified_ids(
    native_operator, monkeypatch
):
    authorization, credentials, admin, clients, namespaces = native_operator
    read = asyncio.Event()
    written = asyncio.Event()
    real_factory = product.AsyncSurreal
    calls = []
    handles = []

    class Observed:
        def __init__(self):
            self.connection = real_factory(authorization.endpoint)

        def __getattr__(self, key):
            return getattr(self.connection, key)

        async def begin(self):
            handle = await self.connection.begin()
            handles.append(handle)
            return handle

        async def query_raw(self, query, params=None, *, txn_id=None):
            assert txn_id == handles[0]
            calls.append(query)
            response = await self.connection.query_raw(query, params, txn_id=txn_id)
            if len(calls) == 1:
                read.set()
                await written.wait()
                await writer_task
            return response

    monkeypatch.setattr(product, "AsyncSurreal", lambda _: Observed())

    async def writer():
        await read.wait()
        await clients[0].execute_query(
            "UPDATE entity:same SET name='current',attributes.clock=d'2026-10-02T00:00:00.123456790Z'; CREATE entity:phantom SET uuid=$uuid,group_id=$org,name='added',entity_type='note';",
            uuid=str(uuid4()),
            org=authorization.scopes[0].organization_id,
        )
        await clients[1].execute_query(
            "CREATE entity:phantom SET uuid=$uuid,organization_id=$org; DEFINE TABLE future_authority SCHEMAFULL;",
            uuid=str(uuid4()),
            org=authorization.scopes[0].organization_id,
        )
        await clients[2].execute_query(
            "UPDATE organizations:same SET settings.secret='current'; CREATE organizations:phantom SET uuid=$uuid,slug=$slug;",
            uuid=str(uuid4()),
            slug="phantom-" + uuid4().hex,
        )
        extra = "operator_capture_extra_" + uuid4().hex
        namespaces.append(extra)
        await raw(
            admin,
            f"DEFINE NS {extra}; USE NS {authorization.scopes[0].namespace} DB future_database; DEFINE TABLE future_authority SCHEMAFULL;",
        )
        written.set()

    async def authorize():
        return authorization

    writer_task = asyncio.create_task(writer())
    writer_task.add_done_callback(lambda _: written.set())
    try:
        captured = await asyncio.wait_for(
            product.capture_archive_operator_native_root(authorize, credentials=credentials), 60
        )
        await writer_task
    finally:
        if not writer_task.done():
            writer_task.cancel()
            await asyncio.gather(writer_task, return_exceptions=True)
    native = archive_native_value_parameters(captured.native)
    graph, content, auth = native["scopes"]
    assert sum(len(scope["tables"]) for scope in native["scopes"]) == 73
    assert graph["namespace"] != content["namespace"]
    graph_row = next(table for table in graph["tables"] if table["name"] == "entity")["rows"][0]
    content_row = next(table for table in content["tables"] if table["name"] == "entity")["rows"][0]
    assert graph_row["id"] == content_row["id"] == RecordID("entity", "same")
    assert graph_row["name"] == "snapshot"
    assert graph_row["attributes"]["clock"].dt.endswith(".123456789Z")
    assert type(graph_row["attributes"]["values"][0]) is CBORSimpleValue
    assert graph_row["attributes"]["values"][1] is None
    assert graph_row["attributes"]["reference"].id[0] == 7
    assert "future_authority" not in content["database_catalog"]["tables"]
    assert "future_database" not in graph["namespace_catalog"]["databases"]
    assert namespaces[-1] not in native["namespace_catalog"]["namespaces"]
    assert any(not table["rows"] for table in auth["tables"])
    auth_rows = next(table for table in auth["tables"] if table["name"] == "organizations")["rows"]
    assert len(auth_rows) == 1 and auth_rows[0]["settings"]["secret"] == "operator-only"
    assert len(calls) > 10
    monkeypatch.setattr(product, "AsyncSurreal", real_factory)
    with pytest.raises(ValueError, match="unclassified"):
        await product.capture_archive_operator_native_root(authorize, credentials=credentials)
    await admin.use(authorization.scopes[0].namespace, "graph")
    stale = await admin.query_raw("RETURN true;", txn_id=handles[0])
    assert stale.get("error")
    evidence({"snapshot": captured.payload, "calls": len(calls), "closed_handle": str(handles[0])})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["foreign", "missing_table", "schema", "version", "organization", "unsupported"]
)
async def test_operator_root_rejects_invalid_native_scope(native_operator, mutation):
    authorization, credentials, _admin, clients, _ = native_operator
    if mutation == "foreign":
        await clients[0].execute_query(
            "CREATE entity:foreign SET uuid=$uuid,group_id=$org,name='foreign',entity_type='note';",
            uuid=str(uuid4()),
            org=str(uuid4()),
        )
    elif mutation == "missing_table":
        await clients[1].execute_query("REMOVE TABLE backups;")
    elif mutation == "schema":
        await clients[1].execute_query("ALTER TABLE backups SCHEMALESS;")
    elif mutation == "unsupported":
        await clients[0].execute_query(
            "UPDATE entity:same SET attributes.unsupported = <decimal>'1.25';"
        )
    elif mutation == "version":
        await clients[1].execute_query("UPDATE schema_version:content SET version=999;")
    else:
        await clients[2].execute_query("DELETE organizations:same;")

    async def authorize():
        return authorization

    with pytest.raises(ValueError):
        await product.capture_archive_operator_native_root(authorize, credentials=credentials)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["later_frame", "commit", "lost_commit", "cancelled", "revoked"])
async def test_operator_root_native_failure_lifecycle(native_operator, monkeypatch, fault):
    authorization, credentials, admin, _, _ = native_operator
    real_factory = product.AsyncSurreal
    calls = []
    handle = None
    waiting = asyncio.Event()
    suspended = asyncio.Event()

    class Observed:
        def __init__(self):
            self.connection = real_factory(authorization.endpoint)

        def __getattr__(self, key):
            return getattr(self.connection, key)

        async def begin(self):
            nonlocal handle
            handle = await self.connection.begin()
            return handle

        async def query_raw(self, query, params=None, *, txn_id=None):
            if fault == "cancelled":
                waiting.set()
                await suspended.wait()
            if fault == "later_frame":
                query += " THROW 'owned later-frame failure';"
            return await self.connection.query_raw(query, params, txn_id=txn_id)

        async def commit(self, txn_id):
            calls.append("commit")
            if fault == "lost_commit":
                await self.connection.commit(txn_id)
            if fault in ("commit", "lost_commit"):
                raise RuntimeError("owned commit failure")
            return await self.connection.commit(txn_id)

        async def cancel(self, txn_id):
            calls.append("cancel")
            return await self.connection.cancel(txn_id)

        async def close(self):
            calls.append("close")
            return await self.connection.close()

    monkeypatch.setattr(product, "AsyncSurreal", lambda _: Observed())
    decisions = 0

    async def authorize():
        nonlocal decisions
        decisions += 1
        return (
            replace(authorization, decision_id="revoked")
            if fault == "revoked" and decisions > 1
            else authorization
        )

    expected = {
        "later_frame": ValueError,
        "commit": RuntimeError,
        "lost_commit": BaseExceptionGroup,
        "cancelled": asyncio.CancelledError,
        "revoked": PermissionError,
    }[fault]
    task = asyncio.create_task(
        product.capture_archive_operator_native_root(authorize, credentials=credentials)
    )
    if fault == "cancelled":
        await asyncio.wait_for(waiting.wait(), 10)
        task.cancel()
    with pytest.raises(expected):
        await task
    assert calls[-1] == "close"
    assert ("cancel" in calls) == (fault != "revoked")
    await admin.use(authorization.scopes[0].namespace, "graph")
    assert (await admin.query_raw("RETURN true;", txn_id=handle)).get("error")
    evidence({"fault": fault, "calls": calls, "decisions": decisions})


@pytest.mark.asyncio
async def test_operator_root_empty_present_optional_diagnostics(native_operator):
    authorization, credentials, _admin, clients, _ = native_operator
    for client in clients[1:]:
        for definition in _LEASE_DEFINITIONS:
            await client.execute_query(definition)

    async def authorize():
        return authorization

    captured = await product.capture_archive_operator_native_root(
        authorize, credentials=credentials
    )
    root = captured.native.value
    assert sum(len(scope["tables"]) for scope in root["scopes"]) == 75
    assert all(scope["absent_diagnostics"] == [] for scope in root["scopes"])
    for scope in root["scopes"][1:]:
        lease = next(table for table in scope["tables"] if table["name"] == "schema_lease")
        assert lease["rows"] == []
    evidence({"optional_diagnostics": captured.payload})


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["namespace", "database", "schema_version", "entity"])
async def test_operator_root_missing_membership_never_creates_catalog(native_operator, missing):
    authorization, credentials, admin, clients, namespaces = native_operator
    graph = authorization.scopes[0]
    if missing == "namespace":
        organization = str(uuid4())
        absent = replace(
            graph,
            namespace=authorization.graph_namespace_prefix + organization.replace("-", ""),
            organization_id=organization,
        )
        namespaces.append(absent.namespace)
        authorization = replace(authorization, scopes=(absent, *authorization.scopes[1:]))
        before = await raw(admin, "INFO FOR ROOT;")
        assert absent.namespace not in before["namespaces"]
    elif missing == "database":
        absent = replace(graph, database="missing_database")
        authorization = replace(authorization, scopes=(absent, *authorization.scopes[1:]))
        before = await raw(admin, f"USE NS {graph.namespace}; INFO FOR NS;")
        assert absent.database not in before["databases"]
    else:
        await clients[0].execute_query(f"REMOVE TABLE {missing};")
        before = await raw(admin, f"USE NS {graph.namespace} DB {graph.database}; INFO FOR DB;")
        assert missing not in before["tables"]

    async def authorize():
        return authorization

    match = {"namespace": "namespace is missing", "database": "database membership"}.get(
        missing, "missing or unclassified"
    )
    with pytest.raises(ValueError, match=match):
        await product.capture_archive_operator_native_root(authorize, credentials=credentials)
    if missing == "namespace":
        after = await raw(admin, "INFO FOR ROOT;")
        assert absent.namespace not in after["namespaces"]
    elif missing == "database":
        after = await raw(admin, f"USE NS {graph.namespace}; INFO FOR NS;")
        assert after["databases"] == before["databases"]
    else:
        after = await raw(admin, f"USE NS {graph.namespace} DB {graph.database}; INFO FOR DB;")
        assert after["tables"] == before["tables"]
    evidence({"missing": missing, "no_catalog_creation": True, "namespaces": namespaces})


@pytest.mark.asyncio
async def test_operator_root_second_graph_org_keeps_scope_on_every_rpc(native_operator):
    authorization, credentials, _admin, clients, namespaces = native_operator
    organization = str(uuid4())
    scope = product.ArchiveOperatorScope(
        "graph",
        authorization.graph_namespace_prefix + organization.replace("-", ""),
        "graph",
        organization,
    )
    namespaces.append(scope.namespace)
    client = SurrealGraphClient(
        group_id=organization,
        namespace_prefix=authorization.graph_namespace_prefix,
        url=authorization.endpoint,
        pool_size=1,
        **credentials,
    )
    try:
        await bootstrap_schema(client)
        await client.execute_query(
            "CREATE entity:same SET uuid=$uuid,group_id=$org,name='second organization',entity_type='note';",
            uuid=str(uuid4()),
            org=organization,
        )
        await clients[2].execute_query(
            "CREATE organizations:second SET uuid=$org,slug=$slug;",
            org=organization,
            slug="second-" + uuid4().hex,
        )
        authorization = replace(
            authorization, scopes=(authorization.scopes[0], scope, *authorization.scopes[1:])
        )

        async def authorize():
            return authorization

        captured = await product.capture_archive_operator_native_root(
            authorize, credentials=credentials
        )
        graphs = [
            item
            for item in archive_native_value_parameters(captured.native)["scopes"]
            if item["store"] == "graph"
        ]
        assert [item["organization_id"] for item in graphs] == [
            authorization.scopes[0].organization_id,
            organization,
        ]
        rows = [
            next(table for table in item["tables"] if table["name"] == "entity")["rows"]
            for item in graphs
        ]
        assert [item[0]["name"] for item in rows] == ["snapshot", "second organization"]
        assert [item[0]["group_id"] for item in rows] == [
            authorization.scopes[0].organization_id,
            organization,
        ]
        assert rows[0][0]["id"] == rows[1][0]["id"] == RecordID("entity", "same")
        evidence({"two_graph_organizations": True, "namespaces": namespaces})
    finally:
        await client.close()
