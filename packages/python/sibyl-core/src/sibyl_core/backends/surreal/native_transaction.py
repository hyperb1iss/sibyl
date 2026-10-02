"""Dedicated native transactions for trusted, pre-authorized store adapters.

This leaf does not authorize archived SQL, provide a SQL sandbox, or activate
ordinary writers. The host attests provider affinity and supplies fresh actor
authorization separately from the service credential profile.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from surrealdb.connections.async_ws import AsyncWsSurrealConnection
from surrealdb.errors import ServerError

from sibyl_core.backends.surreal.dedicated_client import _checked_query_result

if TYPE_CHECKING:
    from surrealdb.types import Value

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CONTROLS = {"USE", "BEGIN", "COMMIT", "CANCEL", "DEFINE", "REMOVE"}
_ORG_PARAMETERS = {"org", "group_id", "organization_id"}


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("native scope identifiers must be trusted SQL identifiers")


def _identity(value: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("native binding identity must be nonempty")


@dataclass(frozen=True, slots=True)
class NativeStoreScope:
    """A host-approved store locator, never obtained from an archive."""

    store: Literal["content", "graph"]
    namespace: str
    database: str
    organization_id: str
    required_tables: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.store not in {"content", "graph"}:
            raise ValueError("unsupported native store")
        _identifier(self.namespace)
        _identifier(self.database)
        if str(UUID(self.organization_id)) != self.organization_id:
            raise ValueError("organization identity must be a canonical UUID")
        if type(self.required_tables) is not tuple or not self.required_tables:
            raise ValueError("required tables must be an immutable nonempty tuple")
        if len(set(self.required_tables)) != len(self.required_tables):
            raise ValueError("duplicate required table")
        for table in self.required_tables:
            _identifier(table)


@dataclass(frozen=True, slots=True)
class NativeTransactionBinding:
    """Trusted configuration attestation of one runtime provider and profile.

    Endpoint equality alone does not attest engine identity. The host must
    establish that these scopes and its actual store ports use this provider.
    """

    endpoint: str
    provider_id: str
    configuration_generation: str
    credential_profile_id: str
    scopes: tuple[NativeStoreScope, ...]

    def __post_init__(self) -> None:
        parts = urlsplit(self.endpoint)
        if (
            parts.scheme not in {"ws", "wss"}
            or not parts.hostname
            or parts.path != "/rpc"
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("native transactions require a secret-free WS RPC endpoint")
        for identity in (
            self.provider_id,
            self.configuration_generation,
            self.credential_profile_id,
        ):
            _identity(identity)
        if type(self.scopes) is not tuple or not self.scopes:
            raise ValueError("approved scopes must be an immutable nonempty tuple")
        if any(type(scope) is not NativeStoreScope for scope in self.scopes):
            raise ValueError("invalid approved scope")
        keys = [(scope.store, scope.organization_id) for scope in self.scopes]
        if len(set(keys)) != len(keys):
            raise ValueError("ambiguous approved store scope")


@dataclass(frozen=True, slots=True)
class NativeTransactionAuthorization:
    """Fresh host actor decision bound to the separately attested topology."""

    binding: NativeTransactionBinding
    actor_id: str
    decision_id: str

    def __post_init__(self) -> None:
        _identity(self.actor_id)
        _identity(self.decision_id)


@dataclass(frozen=True, slots=True)
class NativeCredentialProfile:
    """Host-resolved service profile; secrets are excluded from representation."""

    profile_id: str
    username: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        _identity(self.profile_id)
        if not self.username or not self.password:
            raise ValueError("native catalog capability requires a service profile")


class NativeCommitOutcome(StrEnum):
    NOT_REQUESTED = "not_requested"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class NativeTransactionError(RuntimeError):
    """A local transaction contract or catalog requirement was violated."""


@dataclass(frozen=True, slots=True)
class NativeScopedExecutor:
    """Immutable SurrealExecute adapter for one approved scope.

    SQL is trusted application code. Result cardinality covers every top-level
    statement, including USE; callers retain their domain result validation.
    """

    _transaction: NativeTransaction = field(repr=False)
    scope: NativeStoreScope

    async def __call__(self, statement: str, /, **params: object) -> object:
        return await self._transaction._execute(self.scope, statement, params)

    async def execute_query(self, statement: str, /, **params: object) -> object:
        return await self(statement, **params)


class _NativeSocket(Protocol):
    async def close(self) -> None: ...


class NativeTransaction:
    """Opaque owned default-session transaction with explicit commit semantics."""

    def __init__(
        self, binding: NativeTransactionBinding, *, cancel_ack_timeout_seconds: float
    ) -> None:
        self._binding = binding
        self._cancel_ack_timeout_seconds = cancel_ack_timeout_seconds
        self._client: AsyncWsSurrealConnection | None = None
        self._socket: _NativeSocket | None = None
        self._txn: UUID | None = None
        self._state = "opening"
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._outcome = NativeCommitOutcome.NOT_REQUESTED

    @property
    def commit_outcome(self) -> NativeCommitOutcome:
        return self._outcome

    def executor(self, scope: NativeStoreScope) -> NativeScopedExecutor:
        self._ready()
        if scope not in self._binding.scopes:
            raise NativeTransactionError("scope was not approved by the host")
        return NativeScopedExecutor(self, scope)

    def _affine_client(self) -> AsyncWsSurrealConnection:
        client = self._client
        if client is None or client.socket is not self._socket or self._socket is None:
            raise NativeTransactionError("native socket affinity was lost")
        return client

    def _ready(self) -> None:
        if self._state != "ready":
            raise NativeTransactionError("native transaction is not ready")
        self._affine_client()
        if self._txn is None:
            raise NativeTransactionError("native transaction handle is missing")

    async def _raw(
        self,
        query: str,
        params: dict[str, object],
        frames: int,
        use_scope: tuple[str, str | None] | None = None,
    ) -> object:
        client = self._affine_client()
        self._inflight += 1
        self._idle.clear()
        try:
            response = await client.query_raw(
                query, cast("dict[str, Value]", params), txn_id=self._txn
            )
            results = cast(list[object], _checked_query_result(response, all_results=True))
            if len(results) != frames:
                raise NativeTransactionError("unexpected native statement result cardinality")
            if use_scope is not None and results[0] != {
                "namespace": use_scope[0],
                "database": use_scope[1],
            }:
                raise NativeTransactionError("native USE result does not match approved scope")
            return results[-1]
        finally:
            self._inflight -= 1
            if not self._inflight:
                self._idle.set()

    async def _catalog(self) -> None:
        root = await self._raw("INFO FOR ROOT;", {}, 1)
        namespaces = _catalog_members(root, "namespaces")
        # Membership precedes USE: SDK use() would durably create a missing NS.
        for scope in self._binding.scopes:
            if scope.namespace not in namespaces:
                raise NativeTransactionError("approved namespace is absent")
            ns = await self._raw(
                f"USE NS {scope.namespace}; INFO FOR NS;", {}, 2, (scope.namespace, None)
            )
            if scope.database not in _catalog_members(ns, "databases"):
                raise NativeTransactionError("approved database is absent")
            db = await self._raw(
                f"USE NS {scope.namespace} DB {scope.database}; INFO FOR DB;",
                {},
                2,
                (scope.namespace, scope.database),
            )
            if not set(scope.required_tables) <= _catalog_members(db, "tables").keys():
                raise NativeTransactionError("required native table is absent")

    async def _execute(
        self, scope: NativeStoreScope, statement: str, params: dict[str, object]
    ) -> object:
        self._ready()
        try:
            if scope not in self._binding.scopes:
                raise NativeTransactionError("scope was not approved by the host")
            frames, tokens = _domain_frames(statement)
            if _CONTROLS.intersection(tokens):
                raise NativeTransactionError("scope/schema/transaction control is leaf-owned")
            for key in _ORG_PARAMETERS.intersection(params):
                if params[key] != scope.organization_id:
                    raise NativeTransactionError("foreign organization parameter")
            prefix = f"USE NS {scope.namespace} DB {scope.database}; "
            return await self._raw(
                prefix + statement, params, frames + 1, (scope.namespace, scope.database)
            )
        except BaseException:
            self._state = "failed"
            raise

    async def commit(self) -> None:
        self._ready()
        if self._inflight:
            self._state = "failed"
            raise NativeTransactionError("owned RPCs must finish before COMMIT")
        self._state = "committing"
        # Sending may have succeeded even when the response or task is lost.
        self._outcome = NativeCommitOutcome.UNKNOWN
        try:
            await self._affine_client().commit(cast(UUID, self._txn))
        except BaseException as exc:
            # Native TransactionConflict (-32009) consumes the rejected handle.
            # Transport, cancellation, and unfamiliar server failures stay unknown.
            if (
                isinstance(exc, ServerError)
                and exc.kind == "Query"
                and exc.code == -32009
                and exc.details == {"kind": "TransactionConflict"}
            ):
                self._outcome = NativeCommitOutcome.REJECTED
            self._state = "failed"
            exc.add_note(f"native commit outcome: {self._outcome}")
            raise
        self._outcome = NativeCommitOutcome.ACKNOWLEDGED
        self._state = "committed"

    async def _cleanup(self) -> list[BaseException]:
        errors: list[BaseException] = []
        self._state = "closing"
        client = self._client
        cancel_task: asyncio.Task[None] | None = None
        if client is not None:
            if self._txn is not None and self._outcome not in {
                NativeCommitOutcome.ACKNOWLEDGED,
                NativeCommitOutcome.REJECTED,
            }:
                try:

                    async def _cancel_owned() -> None:
                        # Scheduling can change socket affinity before this task
                        # starts. Check at dispatch so lazy SDK connect cannot
                        # move the owned UUID onto a different transport.
                        await self._affine_client().cancel(cast(UUID, self._txn))

                    cancel_task = asyncio.create_task(_cancel_owned())
                    done, _ = await asyncio.wait(
                        (cancel_task,), timeout=self._cancel_ack_timeout_seconds
                    )
                    if done:
                        completed, cancel_task = cancel_task, None
                        completed.result()
                    else:
                        # Retain the deadline even if the SDK finalizer later fails.
                        errors.append(TimeoutError("native CANCEL acknowledgment grace expired"))
                        cancel_task.cancel("owned CANCEL acknowledgment deadline")
                except BaseException as exc:
                    errors.append(exc)
            try:
                # The pinned SDK close suppresses websocket exceptions. Retain
                # the bound socket's public close failure before SDK teardown.
                if self._socket is not None:
                    await self._socket.close()
            except BaseException as exc:
                errors.append(exc)
            try:
                if client.socket is not None and client.socket is not self._socket:
                    # An unexpected replacement is not this handle's transport.
                    # Public SDK close would close it; fail without touching it.
                    raise NativeTransactionError("unbound replacement prevents SDK teardown")
                await client.close()
            except BaseException as exc:
                errors.append(exc)
        if cancel_task is not None:
            # Join only after closing the original transport and SDK teardown.
            try:
                await cancel_task
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    exc.add_note("CANCEL waiter cancelled by owned acknowledgment cleanup")
                errors.append(exc)
        # Closing the dedicated transport releases SDK query waiters. Caller
        # task results stay caller-owned; drain their RPC boundaries only.
        await self._idle.wait()
        self._state = "closed"
        self._client = None
        return errors


def _domain_frames(query: str) -> tuple[int, set[str]]:
    """Count trusted top-level statements without treating literals as controls."""
    if not isinstance(query, str):
        raise NativeTransactionError("native query must be trusted text")
    depth: list[str] = []
    quote = ""
    escaped = False
    comment = False
    visible: list[str] = []
    frames = 0
    nonempty = False
    index = 0
    while index < len(query):
        char = query[index]
        pair = query[index : index + 2]
        if comment:
            if char == "\n":
                comment = False
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if pair in {"--", "//"}:
            comment = True
            index += 2
            continue
        if pair == "/*":
            raise NativeTransactionError("block comments require a domain query adapter")
        if char in {"'", '"', "`"}:
            quote = char
            nonempty = True
            visible.append(" ")
        elif char in "([{":
            depth.append(char)
            nonempty = True
        elif char in ")]}":
            if not depth or "([{".index(depth.pop()) != ")]}".index(char):
                raise NativeTransactionError("unbalanced native query")
        elif char == ";" and not depth:
            frames += int(nonempty)
            nonempty = False
            visible.append(" ")
        else:
            visible.append(char)
            nonempty = nonempty or not char.isspace()
        index += 1
    if quote or depth:
        raise NativeTransactionError("unbalanced native query")
    frames += int(nonempty)
    if not frames:
        raise NativeTransactionError("empty native query")
    return frames, set(re.findall(r"[A-Za-z_]+", "".join(visible).upper()))


def _catalog_members(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get(name), dict):
        raise NativeTransactionError("invalid native catalog result")
    members = value[name]
    if any(not isinstance(key, str) for key in members):
        raise NativeTransactionError("invalid native catalog member")
    return cast(dict[str, object], members)


async def _finish_cleanup(transaction: NativeTransaction) -> list[BaseException]:
    task = asyncio.create_task(transaction._cleanup())
    interruptions: list[BaseException] = []
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            interruptions.append(exc)
    return interruptions + task.result()


@asynccontextmanager
async def open_native_transaction(
    binding: NativeTransactionBinding,
    *,
    authorize: Callable[[], Awaitable[NativeTransactionAuthorization]],
    credentials: Callable[[str], Awaitable[NativeCredentialProfile]],
    cancel_ack_timeout_seconds: float = 5.0,
) -> AsyncIterator[NativeTransaction]:
    """Open one host-authorized handle; never implicitly commit or retry.

    A positive finite host grace bounds only teardown CANCEL acknowledgment.
    Actor authorization runs before credential resolution and socket I/O.
    Cleanup retains every failure. An unknown COMMIT remains unknown after a
    successful CANCEL; domain-specific durable receipt reconciliation is required.
    """

    if isinstance(cancel_ack_timeout_seconds, bool) or not isinstance(
        cancel_ack_timeout_seconds, (int, float)
    ):
        raise ValueError("CANCEL acknowledgment grace must be positive and finite")
    try:
        grace = float(cancel_ack_timeout_seconds)
    except OverflowError as exc:
        raise ValueError("CANCEL acknowledgment grace must be positive and finite") from exc
    if not math.isfinite(grace) or grace <= 0:
        raise ValueError("CANCEL acknowledgment grace must be positive and finite")
    authorization = await authorize()
    if (
        type(authorization) is not NativeTransactionAuthorization
        or authorization.binding != binding
    ):
        raise NativeTransactionError("fresh actor authorization does not match binding")
    profile = await credentials(binding.credential_profile_id)
    if (
        type(profile) is not NativeCredentialProfile
        or profile.profile_id != binding.credential_profile_id
    ):
        raise NativeTransactionError("credential profile does not match binding")
    transaction = NativeTransaction(binding, cancel_ack_timeout_seconds=grace)
    primary: BaseException | None = None
    try:
        client = AsyncWsSurrealConnection(binding.endpoint)
        transaction._client = client
        try:
            await client.connect()
        finally:
            transaction._socket = client.socket
        await client.signin({"username": profile.username, "password": profile.password})
        transaction._txn = await transaction._affine_client().begin()
        if not isinstance(transaction._txn, UUID):
            raise NativeTransactionError("invalid native transaction handle")
        await transaction._catalog()
        transaction._state = "ready"
        yield transaction
    except BaseException as exc:
        primary = exc
    errors = await _finish_cleanup(transaction)
    if primary is not None:
        if errors:
            raise BaseExceptionGroup("native transaction and cleanup failed", [primary, *errors])
        raise primary
    if errors:
        raise BaseExceptionGroup("native transaction cleanup failed", errors)
