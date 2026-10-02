"""Strict inert envelope and operator authorization/lifecycle boundaries."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from sibyl_core.migrate.archive_native_values import prepare_archive_native_value
from sibyl_core.migrate.archive_operator_native_root import (
    PROFILE,
    prepare_archive_operator_root,
    validate_archive_operator_root,
)
from sibyl_core.services import archive_operator_native_capture as product


def authorization():
    org = str(uuid4())
    return product.ArchiveOperatorAuthorization(
        "verified-operator",
        "decision",
        "ws://127.0.0.1:23108/rpc",
        "org_",
        (
            product.ArchiveOperatorScope("graph", "org_" + org.replace("-", ""), "graph", org),
            product.ArchiveOperatorScope("content", "sibyl_content", "content"),
            product.ArchiveOperatorScope("auth", "sibyl_auth", "auth"),
        ),
    )


def inert_native():
    org = "12345678-1234-4234-8234-123456789abc"
    namespaces = {
        "test_" + org.replace("-", ""): "definition",
        "content": "definition",
        "auth": "definition",
    }
    scopes = []
    for store, namespace in zip(("graph", "content", "auth"), namespaces, strict=True):
        scopes.append(
            {
                "store": store,
                "namespace": namespace,
                "database": store,
                "organization_id": org if store == "graph" else None,
                "namespace_catalog": {"databases": {store: "definition"}},
                "database_catalog": {"tables": {"empty": "DEFINE TABLE empty SCHEMAFULL"}},
                "absent_diagnostics": [],
                "tables": [{"name": "empty", "catalog": {}, "rows": []}],
            }
        )
    value = {
        "profile": PROFILE,
        "endpoint": "ws://127.0.0.1:23108/rpc",
        "operator_id": "asserted-label",
        "decision_id": "asserted-decision",
        "server_version": "surrealdb-3.2.4",
        "graph_namespace_prefix": "test_",
        "namespace_catalog": {"namespaces": namespaces},
        "scopes": scopes,
    }
    types = [{"kind": "none", "path": ["scopes", i, "organization_id"]} for i in (1, 2)]
    return prepare_archive_native_value(value=value, native_types=types, native_sha256="a" * 64)


def test_operator_envelope_detached_and_legacy_native_unchanged():
    native = inert_native()
    before = native.payload_json
    root = prepare_archive_operator_root(native)
    assert validate_archive_operator_root(root.payload) == root
    payload = root.payload
    payload["capture"]["value"]["scopes"].clear()
    assert root.native.payload_json == before == native.payload_json
    assert not hasattr(root, "authorization")


@pytest.mark.parametrize(
    "fault", ["trusted", "version", "profile", "digest", "catalog", "duplicate"]
)
def test_operator_envelope_rejects_unsupported_or_tampered_claims(fault):
    root = prepare_archive_operator_root(inert_native()).payload
    if fault == "trusted":
        root["trusted"] = True
    elif fault == "version":
        root["version"] = True
    elif fault == "profile":
        root["profile"] = "org-admin-full-recovery"
    elif fault == "digest":
        root["sha256"] = "f" * 64
    else:
        value = deepcopy(inert_native().value)
        if fault == "catalog":
            value["scopes"][0]["database_catalog"]["tables"]["unknown"] = "definition"
        else:
            value["scopes"].append(deepcopy(value["scopes"][0]))
        types = [
            {"kind": "none", "path": ["scopes", i, "organization_id"]}
            for i in range(len(value["scopes"]))
            if value["scopes"][i]["organization_id"] is None
        ]
        native = prepare_archive_native_value(
            value=value, native_types=types, native_sha256="a" * 64
        )
        with pytest.raises(ValueError):
            prepare_archive_operator_root(native)
        return
    with pytest.raises(ValueError):
        validate_archive_operator_root(root)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"error": {"message": "RPC"}, "result": []},
        {"result": []},
        {
            "result": [
                {"status": "OK", "result": {"ok": True}},
                {"status": "ERR", "result": "later"},
            ]
        },
        {"result": [{"status": "OK"}]},
    ],
)
def test_operator_all_rpc_frames_fail_closed(value):
    with pytest.raises(ValueError):
        product._last_result(value)


@pytest.mark.asyncio
async def test_operator_denial_precedes_socket_construction(monkeypatch):
    def forbidden(_):
        pytest.fail("denied operator must never open a socket")

    monkeypatch.setattr(product, "AsyncSurreal", forbidden)

    async def deny():
        raise PermissionError("fresh operator denied")

    with pytest.raises(PermissionError, match="denied"):
        await product.capture_archive_operator_native_root(deny, credentials={"username": "root"})

    async def uploaded():
        return {"trusted": True, "operator_id": "root"}

    with pytest.raises(PermissionError):
        await product.capture_archive_operator_native_root(uploaded, credentials={})


@pytest.mark.parametrize("fault", ["endpoint_secret", "alias", "scope", "org", "prefix"])
def test_operator_server_decision_rejects_source_affinity_aliases(fault):
    grant = authorization()
    with pytest.raises(ValueError):
        if fault == "endpoint_secret":
            replace(grant, endpoint="ws://user:secret@localhost:23108/rpc")
        elif fault == "alias":
            replace(grant, scopes=(*grant.scopes, grant.scopes[0]))
        elif fault == "scope":
            replace(grant, scopes=grant.scopes[1:])
        elif fault == "org":
            replace(
                grant,
                scopes=(replace(grant.scopes[0], organization_id=str(uuid4())), *grant.scopes[1:]),
            )
        else:
            replace(grant, graph_namespace_prefix="uploaded_")


@pytest.mark.asyncio
@pytest.mark.parametrize("primary", ["connect", "signin", "begin", "codec", "cancelled", "commit"])
async def test_operator_primary_and_cleanup_failures_remain_visible(monkeypatch, primary):
    grant = authorization()
    calls = []

    class Socket:
        async def close(self):
            calls.append("socket-close")

    class Connection:
        socket = None

        async def connect(self):
            calls.append("connect")
            if primary == "connect":
                raise RuntimeError("primary-connect")
            self.socket = Socket()

        async def signin(self, _):
            if primary == "signin":
                raise RuntimeError("primary-signin")

        async def use(self, *_):
            pass

        async def version(self):
            return "surrealdb-3.2.4"

        async def begin(self):
            calls.append("begin")
            if primary == "begin":
                raise RuntimeError("primary-begin")
            return uuid4()

        async def query_raw(self, query, parameters, *, txn_id):
            assert txn_id is not None
            if query == "INFO FOR ROOT;":
                result = {
                    "namespaces": {scope.namespace: "DEFINE NAMESPACE" for scope in grant.scopes}
                }
            elif query.endswith("INFO FOR NS;"):
                scope = next(scope for scope in grant.scopes if f"NS {scope.namespace};" in query)
                result = {"databases": {scope.database: "DEFINE DATABASE"}}
            elif query.endswith("INFO FOR DB;"):
                scope = next(scope for scope in grant.scopes if f"NS {scope.namespace} DB" in query)
                result = {
                    "tables": {
                        table: "DEFINE TABLE " + table + " SCHEMAFULL"
                        for table in product._TABLES[scope.store]
                    },
                }
            elif "SELECT name,version" in query:
                scope = next(scope for scope in grant.scopes if f"NS {scope.namespace} DB" in query)
                result = [{"name": scope.store, "version": product._VERSIONS[scope.store]}]
            elif "SELECT VALUE uuid" in query:
                result = [grant.scopes[0].organization_id]
            else:
                result = 0
            return {"result": [{"status": "OK", "result": result}]}

        async def commit(self, _):
            if primary == "commit":
                raise RuntimeError("primary-commit")

        async def cancel(self, _):
            calls.append("cancel")
            raise RuntimeError("cleanup-cancel")

        async def close(self):
            calls.append("close")
            raise RuntimeError("cleanup-close")

    async def codec(*args, **kwargs):
        if primary == "cancelled":
            raise asyncio.CancelledError("primary-cancelled")
        if primary == "codec":
            raise RuntimeError("primary-codec")
        return inert_native()

    monkeypatch.setattr(product, "AsyncSurreal", lambda _: Connection())
    monkeypatch.setattr(product, "capture_archive_native_tree", codec)

    async def authorize():
        return grant

    with pytest.raises(BaseExceptionGroup) as caught:
        await product.capture_archive_operator_native_root(authorize, credentials={})
    messages = [str(error) for error in caught.value.exceptions]
    assert "primary-" + primary in messages
    assert "cleanup-close" in messages
    assert calls[-1] == "close"
    assert ("socket-close" in calls) == (primary != "connect")
    if primary != "connect":
        assert calls[-2] == "socket-close"
    assert ("cleanup-cancel" in messages) == (primary not in ("connect", "signin", "begin"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server_version", ["surrealdb-3.2.40+future", "surrealdb-3.3.0", "surrealdb-2.3.10"]
)
async def test_operator_unknown_engine_version_closes_without_begin(monkeypatch, server_version):
    grant = authorization()
    calls = []

    class Socket:
        async def close(self):
            calls.append("socket-close")

    class Connection:
        socket = None

        async def connect(self):
            calls.append("connect")
            self.socket = Socket()

        async def signin(self, credentials):
            calls.append("signin")

        async def use(self, *scope):
            calls.append("use")

        async def version(self):
            return server_version

        async def begin(self):
            pytest.fail("unsupported engine must not begin a transaction")

        async def close(self):
            calls.append("close")

    monkeypatch.setattr(product, "AsyncSurreal", lambda _: Connection())

    async def authorize():
        return grant

    with pytest.raises(ValueError, match="proven native"):
        await product.capture_archive_operator_native_root(authorize, credentials={})
    assert calls == ["connect", "signin", "socket-close", "close"]


@pytest.mark.parametrize(
    "field,bad",
    [
        ("namespaces", "test_12345678123442348234123456789abc"),
        ("namespaces", ["test_12345678123442348234123456789abc"]),
        ("databases", ["graph"]),
        ("databases", "graph"),
        ("tables", ["empty"]),
        ("tables", {"empty": None}),
    ],
)
def test_operator_rehashed_catalog_membership_requires_native_map(field, bad):
    value = deepcopy(inert_native().value)
    if field == "namespaces":
        value["namespace_catalog"][field] = bad
    elif field == "databases":
        value["scopes"][0]["namespace_catalog"][field] = bad
    else:
        value["scopes"][0]["database_catalog"][field] = bad
    types = deepcopy(inert_native().payload["native_types"])
    if field == "tables" and type(bad) is dict:
        types.append({"kind": "none", "path": ["scopes", 0, "database_catalog", "tables", "empty"]})
    rehashed = prepare_archive_native_value(value=value, native_types=types, native_sha256="a" * 64)
    with pytest.raises(ValueError, match="catalog"):
        prepare_archive_operator_root(rehashed)
