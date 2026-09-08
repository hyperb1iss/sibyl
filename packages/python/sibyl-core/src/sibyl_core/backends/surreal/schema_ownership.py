"""Namespace-local ownership for schema mutations and migration checkpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from sibyl_core.backends.surreal.connection import _query_tokens

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.schema_version import SurrealExecute

_LEASE_DEFINITIONS = (
    "DEFINE TABLE IF NOT EXISTS schema_lease SCHEMAFULL;",
    "DEFINE FIELD IF NOT EXISTS owner ON schema_lease TYPE string;",
    "DEFINE FIELD IF NOT EXISTS deadline ON schema_lease TYPE datetime;",
    "DEFINE FIELD IF NOT EXISTS claimed_at ON schema_lease TYPE datetime;",
    "DEFINE FIELD IF NOT EXISTS stage ON schema_lease TYPE option<string>;",
    "DEFINE FIELD IF NOT EXISTS mutation_revision ON schema_lease TYPE option<int>;",
)
_LOST_MARKER = "sibyl_schema_lease_lost"
_PARAMETER_PREFIX = "sibyl_schema_"


def _lease_duration(seconds: int) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise ValueError("schema lease duration must be a positive integer number of seconds")
    return f"{seconds}s"


def _refresh_statement(seconds: int, *, require_live: bool = True) -> str:
    live_guard = " AND deadline > time::now()" if require_live else ""
    return (
        f"UPDATE schema_lease:graph SET deadline = time::now() + {_lease_duration(seconds)}, "
        "stage = $sibyl_schema_stage "
        f"WHERE owner = $sibyl_schema_owner{live_guard} RETURN AFTER"
    )


class SchemaOwnershipLost(RuntimeError):
    """A migration must stop because its lease expired or another owner took over."""


@dataclass(slots=True)
class SchemaOwnership:
    """Keep reads unchanged and fence explicitly declared mutations.

    The executor must be checked normal-query execution, already scoped to one
    organization's graph namespace. Raw response envelopes are rejected.
    Mutation bodies contain no transaction boundaries and discard their results.
    Each body commits with an ownership-token write in one transaction. Renewal
    updates a separate record, so heartbeats do not conflict with a long body.
    A successor changes both records atomically and fences out the old body.
    Network callers renew throughout operations longer than the lease duration.
    Embedded callers renew at statement completion before releasing their sole
    connection; a background renewal cannot execute while that connection is busy.
    """

    execute: SurrealExecute
    owner: str
    stage: str = "bootstrap"
    lease_seconds: int = 60
    lease_execute: SurrealExecute | None = None
    renew_after_operation: bool = False
    _lost: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _lease_duration(self.lease_seconds)

    def _completion_renewal(self) -> str:
        return (
            f"LET $sibyl_schema_renewed = ({_refresh_statement(self.lease_seconds, require_live=False)}); "
            "IF array::len($sibyl_schema_renewed) != 1 "
            f"{{ THROW '{_LOST_MARKER}'; }};"
        )

    async def read(self, statement: str, /, **params: object) -> object:
        if not self.renew_after_operation:
            return await self.execute(statement, **params)
        if any(name.startswith(_PARAMETER_PREFIX) for name in params):
            raise ValueError("schema read parameters overlap ownership parameters")
        await self.heartbeat()
        try:
            return await self.execute(
                f"{statement.rstrip().rstrip(';')}\n;\n{self._completion_renewal()}",
                **params,
                sibyl_schema_owner=self.owner,
                sibyl_schema_stage=self.stage,
            )
        except Exception as exc:
            if _LOST_MARKER not in str(exc):
                raise
            self._lost = True
            raise SchemaOwnershipLost("schema migration ownership lost") from exc

    async def mutate(self, body: str, /, **params: object) -> None:
        """Commit a trusted schema mutation body only while this lease is live."""
        self._require_owned()
        if not body.strip().strip(";").strip():
            raise ValueError("schema mutation body cannot be empty")
        # Conservative lexical rejection also covers boundary words in literals
        # and comments. Schema callers supply trusted, pre-rendered bodies.
        if {"BEGIN", "COMMIT", "CANCEL"}.intersection(_query_tokens(body)):
            raise ValueError("schema mutation body must not contain transaction boundaries")
        if any(name.startswith(_PARAMETER_PREFIX) for name in params):
            raise ValueError("schema mutation parameters overlap ownership parameters")
        await self.heartbeat()
        # Even an otherwise idempotent body must write the fence, so a
        # successor's ownership change conflicts with its transaction.
        completion = self._completion_renewal() if self.renew_after_operation else ""
        try:
            result = await self.execute(
                "BEGIN TRANSACTION; "
                "LET $sibyl_schema_live = (SELECT owner FROM schema_lease:graph "
                "WHERE owner = $sibyl_schema_owner AND deadline > time::now()); "
                "LET $sibyl_schema_owned = (UPDATE schema_lease:graph_fence "
                "SET stage = $sibyl_schema_stage, mutation_revision = (mutation_revision ?? 0) + 1 "
                "WHERE owner = $sibyl_schema_owner "
                "AND array::len($sibyl_schema_live) = 1 RETURN AFTER); "
                "IF array::len($sibyl_schema_owned) != 1 "
                f"{{ THROW '{_LOST_MARKER}'; }}; "
                f"{body.rstrip().rstrip(';')}\n;\n{completion}COMMIT TRANSACTION;",
                **params,
                sibyl_schema_owner=self.owner,
                sibyl_schema_stage=self.stage,
            )
            # Checked normal execution returns the leading BEGIN's None result.
            # A raw envelope can contain a rejected fence without raising.
            if result is not None:
                self._lost = True
                raise TypeError("schema mutations require checked query execution")
        except Exception as exc:
            if _LOST_MARKER not in str(exc):
                raise
            self._lost = True
            raise SchemaOwnershipLost("schema migration ownership lost") from exc

    async def heartbeat(self) -> None:
        """Extend ownership during index readiness polling, or stop on lease loss."""
        self._require_owned()
        result = await (self.lease_execute or self.execute)(
            _refresh_statement(self.lease_seconds) + ";",
            sibyl_schema_owner=self.owner,
            sibyl_schema_stage=self.stage,
        )
        if not isinstance(result, list) or len(result) > 1:
            self._lost = True
            raise TypeError(
                "schema ownership heartbeat requires checked query execution "
                "returning at most one record"
            )
        if not result:
            self._lost = True
            raise SchemaOwnershipLost("schema migration ownership lost")

    async def release(self) -> None:
        """Expire only this owner's lease, preserving its diagnostic record."""
        if not self._lost:
            await (self.lease_execute or self.execute)(
                "UPDATE schema_lease:graph SET deadline = time::now() "
                "WHERE owner = $owner RETURN NONE;",
                owner=self.owner,
            )
            self._lost = True

    def _require_owned(self) -> None:
        if self._lost:
            raise SchemaOwnershipLost("schema migration ownership lost")


async def try_acquire_schema_ownership(
    execute: SurrealExecute,
    *,
    lease_seconds: int = 60,
    initialize: bool = True,
    mutation_execute: SurrealExecute | None = None,
    renew_after_operation: bool = False,
) -> SchemaOwnership | None:
    """Claim an absent or expired lease without stealing from a live migration.

    Lease bootstrap is idempotent DDL with no data movement. The separate table
    survives schema-version resets, so reset cannot discard its own ownership.
    The caller decides whether to wait for a current migration or fail readiness.
    """
    duration = _lease_duration(lease_seconds)
    if initialize:
        for statement in _LEASE_DEFINITIONS:
            await execute(statement)
    owner = uuid4().hex
    claimed = await execute(
        "BEGIN TRANSACTION; "
        "LET $sibyl_schema_claimed = (UPSERT schema_lease:graph SET owner = $owner, "
        f"deadline = time::now() + {duration}, claimed_at = time::now(), stage = 'bootstrap' "
        "WHERE owner IS NONE OR deadline <= time::now() RETURN AFTER); "
        "IF array::len($sibyl_schema_claimed) = 1 { "
        "UPSERT schema_lease:graph_fence SET owner = $owner, "
        "deadline = $sibyl_schema_claimed[0].deadline, "
        "claimed_at = $sibyl_schema_claimed[0].claimed_at, stage = 'bootstrap'; "
        "}; COMMIT TRANSACTION;",
        owner=owner,
    )
    if claimed is not None:
        raise TypeError("schema ownership claims require checked query execution")
    result = await execute(
        "SELECT owner FROM schema_lease:graph WHERE owner = $owner AND deadline > time::now();",
        owner=owner,
    )
    if not isinstance(result, list):
        raise TypeError("schema ownership claim expected a record list")
    if not result:
        return None
    if len(result) != 1 or not isinstance(result[0], dict) or result[0].get("owner") != owner:
        raise ValueError("schema ownership claim returned an unexpected owner")
    return SchemaOwnership(
        mutation_execute or execute,
        owner,
        lease_seconds=lease_seconds,
        lease_execute=execute,
        renew_after_operation=renew_after_operation,
    )
