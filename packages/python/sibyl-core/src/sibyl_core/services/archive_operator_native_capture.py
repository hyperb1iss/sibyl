"""Privileged, read-only native capture of explicitly approved deployment scopes.

This core API has no org-download route. Its authorization callback is supplied
by trusted server/operator code, never reconstructed from an uploaded envelope.
Native signin credentials provide database capability, not application authority.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from surrealdb import AsyncSurreal
from surrealdb.connections.async_ws import AsyncWsSurrealConnection

from sibyl_core.backends.surreal.auth_schema import AUTH_SCHEMA_CURRENT_VERSION, AUTH_TABLES
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_CURRENT_VERSION,
    CONTENT_TABLES,
)
from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES
from sibyl_core.backends.surreal.schema_version import GRAPH_SCHEMA_CURRENT_VERSION
from sibyl_core.migrate.archive_operator_native_root import (
    PROFILE,
    PreparedArchiveOperatorRoot,
    prepare_archive_operator_root,
)
from sibyl_core.services.archive_native_capture import capture_archive_native_tree

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TABLES = {
    "graph": tuple(
        sorted(
            {
                *GRAPH_TABLES,
                *GRAPH_EDGES,
                "source_states",
                "memory_derivations",
                "embedding_states",
                "schema_version",
                "schema_lease",
            }
        )
    ),
    "content": tuple(
        sorted(
            {
                *CONTENT_TABLES,
                "source_states",
                "memory_derivations",
                "schema_version",
                "schema_lease",
            }
        )
    ),
    "auth": tuple(sorted({*AUTH_TABLES, "schema_version", "schema_lease"})),
}
_VERSIONS = {
    "graph": GRAPH_SCHEMA_CURRENT_VERSION,
    "content": CONTENT_SCHEMA_CURRENT_VERSION,
    "auth": AUTH_SCHEMA_CURRENT_VERSION,
}


@dataclass(frozen=True, slots=True)
class ArchiveOperatorScope:
    """A server-resolved scope, not a locator accepted from personal archive text."""

    store: Literal["graph", "content", "auth"]
    namespace: str
    database: str
    organization_id: str | None = None

    def __post_init__(self) -> None:
        if self.store not in _TABLES:
            raise ValueError("unsupported operator capture store")
        for value in (self.namespace, self.database):
            if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError("operator capture locator must be a fixed native identifier")
        if self.store == "graph":
            if (
                type(self.organization_id) is not str
                or str(UUID(self.organization_id)) != self.organization_id
            ):
                raise ValueError("graph capture requires a canonical organization UUID")
        elif self.organization_id is not None:
            raise ValueError("shared operator stores do not use an org-download projection")


@dataclass(frozen=True, slots=True)
class ArchiveOperatorAuthorization:
    """Fresh app/operator authorization for full data in these approved scopes.

    The callback must independently authenticate a privileged operator and resolve
    scope from server configuration. OWNER/ADMIN membership, DB credentials and
    an uploaded trusted flag are insufficient. No serialized grant is accepted.
    """

    operator_id: str
    decision_id: str
    endpoint: str
    graph_namespace_prefix: str
    scopes: tuple[ArchiveOperatorScope, ...]

    def __post_init__(self) -> None:
        for value in (self.operator_id, self.decision_id):
            if type(value) is not str or not value:
                raise ValueError("operator capture requires an explicit authorization identity")
        if type(self.endpoint) is not str:
            raise ValueError("operator capture endpoint must be native WebSocket")
        endpoint = urlsplit(self.endpoint)
        if (
            endpoint.scheme not in ("ws", "wss")
            or not endpoint.hostname
            or endpoint.path != "/rpc"
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("operator endpoint must be secret-free native WebSocket RPC")
        if (
            type(self.graph_namespace_prefix) is not str
            or _IDENTIFIER.fullmatch(self.graph_namespace_prefix) is None
        ):
            raise ValueError("graph prefix must come from server configuration")
        if type(self.scopes) is not tuple or any(
            type(scope) is not ArchiveOperatorScope for scope in self.scopes
        ):
            raise ValueError("operator scope inventory must be an immutable server-owned tuple")
        if {scope.store for scope in self.scopes} != set(_TABLES):
            raise ValueError("operator capture requires graph, content and auth scopes")
        if (
            sum(scope.store == "auth" for scope in self.scopes) != 1
            or sum(scope.store == "content" for scope in self.scopes) != 1
        ):
            raise ValueError("operator capture has one shared auth and content scope")
        locators = {(scope.namespace, scope.database) for scope in self.scopes}
        if len(locators) != len(self.scopes):
            raise ValueError("operator scopes must have unique physical locators")
        for scope in self.scopes:
            if (
                scope.store == "graph"
                and scope.namespace != self.graph_namespace_prefix + UUID(scope.organization_id).hex
            ):
                raise ValueError("graph namespace differs from its server-owned organization")


OperatorAuthorize = Callable[[], Awaitable[ArchiveOperatorAuthorization]]


def _last_result(response: object) -> Any:
    """Check every frame, including errors after a successful RETURN."""
    if type(response) is not dict or response.get("error") is not None:
        raise ValueError(f"operator capture RPC failed: {response!r}")
    response = cast(dict[str, Any], response)
    frames = response.get("result")
    if type(frames) is not list or not frames:
        raise ValueError("operator capture RPC has no statement results")
    for frame in frames:
        if type(frame) is not dict or frame.get("status") != "OK" or "result" not in frame:
            raise ValueError(f"operator capture statement failed: {frame!r}")
    return cast(dict[str, Any], frames[-1])["result"]


def _root_program(
    authorization: ArchiveOperatorAuthorization, inventory: list[tuple[str, ...]]
) -> tuple[str, str]:
    first = authorization.scopes[0]
    parts = [
        f"USE NS {first.namespace} DB {first.database};",
        "LET $namespace_catalog = {namespaces:(INFO FOR ROOT).namespaces};",
    ]
    objects: list[str] = []
    for index, scope in enumerate(authorization.scopes):
        parts.append(f"USE NS {scope.namespace} DB {scope.database};")
        parts.append(f"LET $namespace{index} = {{databases:(INFO FOR NS).databases}};")
        parts.append(f"LET $database{index} = (INFO FOR DB);")
        tables = []
        for number, table in enumerate(inventory[index]):
            variable = f"$table{index}_{number}"
            parts.append(
                f"LET {variable} = {{name:'{table}',catalog:(INFO FOR TABLE {table}),rows:(SELECT * FROM {table} ORDER BY id)}};"
            )
            tables.append(variable)
        org = f"$organizations[{index}]"
        objects.append(
            f"{{store:'{scope.store}',namespace:'{scope.namespace}',database:'{scope.database}',organization_id:{org},namespace_catalog:$namespace{index},database_catalog:$database{index},absent_diagnostics:$absent_diagnostics[{index}],tables:[{','.join(tables)}]}}"
        )
    parts.append(
        f"LET $rows = {{profile:$profile,endpoint:$endpoint,operator_id:$operator_id,decision_id:$decision_id,server_version:$server_version,graph_namespace_prefix:$graph_namespace_prefix,namespace_catalog:$namespace_catalog,scopes:[{','.join(objects)}]}};"
    )
    selection = """
    LET $fingerprint = crypto::sha256(type::string($rows));
    IF $expected != NONE AND $fingerprint != $expected {
        THROW 'Qualified native operator root changed during capture';
    };
    """
    return "\n".join(parts), selection


async def _cleanup(connection: Any, txn_id: UUID | None) -> list[BaseException]:
    errors: list[BaseException] = []
    if txn_id is not None:
        try:
            await connection.cancel(txn_id)
        except BaseException as error:
            errors.append(error)
    try:
        await connection.close()
    except BaseException as error:
        errors.append(error)
    return errors


async def capture_archive_operator_native_root(
    authorize_operator: OperatorAuthorize,
    *,
    credentials: dict[str, Any],
) -> PreparedArchiveOperatorRoot:
    """Capture all approved scopes on one owned native RPC transaction.

    Authorization is checked before connecting and refreshed before publishing.
    The result binds complete physical tables and catalogs for approved scopes,
    including empty tables and catalog-listed exclusions outside those scopes.
    It makes no full-server, filesystem, restore, or personal-import claim.
    """
    authorization = await authorize_operator()
    if type(authorization) is not ArchiveOperatorAuthorization:
        raise PermissionError("operator capture requires fresh server-owned authorization")
    if version("surrealdb") != "2.0.0":
        raise ValueError("operator capture requires the proven native SDK 2.0.0")
    connection = cast(AsyncWsSurrealConnection, AsyncSurreal(authorization.endpoint))
    txn_id: UUID | None = None
    failure: BaseException | None = None
    native = None
    try:
        await connection.connect()
        await connection.signin(credentials)
        server_version = await connection.version()
        if server_version.split("+", 1)[0] != "surrealdb-3.2.4":
            raise ValueError("operator capture requires the proven native 3.2.4 backend")
        transaction = await connection.begin()
        if type(transaction) is not UUID:
            raise ValueError("native capture did not receive a valid transaction handle")
        txn_id = transaction

        async def execute(query: str, **parameters: Any) -> Any:
            return _last_result(await connection.query_raw(query, parameters, txn_id=transaction))

        root_catalog = await execute("INFO FOR ROOT;")
        if type(root_catalog) is not dict or type(root_catalog.get("namespaces")) is not dict:
            raise ValueError("invalid native namespace inventory")
        namespaces = root_catalog["namespaces"]
        inventory: list[tuple[str, ...]] = []
        absent_diagnostics: list[list[str]] = []
        for scope in authorization.scopes:
            if scope.namespace not in namespaces:
                raise ValueError("approved namespace is missing")
            namespace_catalog = await execute(f"USE NS {scope.namespace}; INFO FOR NS;")
            if (
                type(namespace_catalog) is not dict
                or type(namespace_catalog.get("databases")) is not dict
                or scope.database not in namespace_catalog["databases"]
            ):
                raise ValueError("missing or invalid native database membership")
            catalog = await execute(f"USE NS {scope.namespace} DB {scope.database}; INFO FOR DB;")
            if type(catalog) is not dict or type(catalog.get("tables")) is not dict:
                raise ValueError("invalid native scope catalog")
            if any(type(name) is not str for name in catalog["tables"]):
                raise ValueError("invalid native table names")
            present = set(cast(dict[str, Any], catalog["tables"]))
            registered = set(_TABLES[scope.store])
            missing = registered - present
            if present - registered or missing - {"schema_lease"}:
                raise ValueError(
                    f"missing or unclassified {scope.store} tables: missing={sorted(missing)}, extra={sorted(present - registered)}"
                )
            inventory.append(tuple(sorted(present)))
            absent_diagnostics.append(sorted(missing))
            if any(
                type(definition) is not str or " SCHEMAFULL" not in definition
                for definition in catalog["tables"].values()
            ):
                raise ValueError("unsupported native table schema")

        # Native SDK use() can create missing scopes. Guard every locator in the
        # snapshot before selecting rows, and keep SQL USE in each scoped RPC.
        for scope in authorization.scopes:
            versions = await execute(
                f"USE NS {scope.namespace} DB {scope.database}; RETURN (SELECT name,version FROM schema_version);"
            )
            if versions != [{"name": scope.store, "version": _VERSIONS[scope.store]}]:
                raise ValueError("unsupported native store schema version")
            if scope.store == "graph":
                for table in (
                    "entity",
                    "episode",
                    "relates_to",
                    "mentions",
                    "source_states",
                    "memory_derivations",
                    "embedding_states",
                    "archive_phase_controls",
                    "archive_phase_receipts",
                ):
                    field = (
                        "group_id"
                        if table in ("entity", "episode", "relates_to", "mentions")
                        else "organization_id"
                    )
                    foreign = await execute(
                        f"USE NS {scope.namespace} DB {scope.database}; RETURN count((SELECT * FROM {table} WHERE {field} != $organization));",
                        organization=scope.organization_id,
                    )
                    if foreign != 0:
                        raise ValueError("graph physical scope contains foreign organization rows")
        auth = next(scope for scope in authorization.scopes if scope.store == "auth")
        organization_ids = await execute(
            f"USE NS {auth.namespace} DB {auth.database}; RETURN (SELECT VALUE uuid FROM organizations);"
        )
        if type(organization_ids) is not list or any(
            scope.organization_id not in organization_ids
            for scope in authorization.scopes
            if scope.store == "graph"
        ):
            raise ValueError("approved graph organization is absent from the native auth inventory")
        preamble, selection = _root_program(authorization, inventory)
        native = await capture_archive_native_tree(
            execute,
            url=authorization.endpoint,
            selection_sql=selection,
            preamble_sql=preamble,
            parameters={
                "profile": PROFILE,
                "endpoint": authorization.endpoint,
                "operator_id": authorization.operator_id,
                "decision_id": authorization.decision_id,
                "server_version": server_version,
                "graph_namespace_prefix": authorization.graph_namespace_prefix,
                "organizations": [scope.organization_id for scope in authorization.scopes],
                "absent_diagnostics": absent_diagnostics,
            },
        )
        await connection.commit(transaction)
        txn_id = None
    except BaseException as error:
        failure = error
    cleanup = asyncio.create_task(_cleanup(connection, txn_id))
    interruptions: list[BaseException] = []
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as error:
            interruptions.append(error)
    errors = ([failure] if failure is not None else []) + interruptions + cleanup.result()
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("operator native capture and cleanup failed", errors)
    refreshed = await authorize_operator()
    if type(refreshed) is not ArchiveOperatorAuthorization or refreshed != authorization:
        raise PermissionError("operator authorization changed before capture publication")
    if native is None:
        raise ValueError("operator capture did not produce native evidence")
    return prepare_archive_operator_root(native)
