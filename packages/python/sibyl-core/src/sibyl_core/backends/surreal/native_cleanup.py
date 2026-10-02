"""Owned native transport teardown, independent of scope and application authority."""

from __future__ import annotations

import asyncio
import math
from typing import Protocol
from uuid import UUID


class NativeTransactionError(RuntimeError):
    """A native connection or transaction contract was violated."""


class NativeOwnedSocket(Protocol):
    async def close(self) -> None: ...


class NativeOwnedConnection(Protocol):
    socket: NativeOwnedSocket | None

    async def cancel(self, txn_id: UUID) -> None: ...

    async def close(self) -> None: ...


def require_native_socket_affinity(
    connection: NativeOwnedConnection, original_socket: NativeOwnedSocket | None
) -> None:
    """Reject any dispatch that could reconnect or use a foreign transport."""
    if original_socket is None or connection.socket is not original_socket:
        raise NativeTransactionError("native socket affinity was lost")


def normalize_cancel_ack_timeout_seconds(value: float) -> float:
    """Validate a host teardown grace before callbacks or connection I/O."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("CANCEL acknowledgment grace must be positive and finite")
    try:
        grace = float(value)
    except OverflowError as exc:
        raise ValueError("CANCEL acknowledgment grace must be positive and finite") from exc
    if not math.isfinite(grace) or grace <= 0:
        raise ValueError("CANCEL acknowledgment grace must be positive and finite")
    return grace


async def cleanup_owned_native_connection(
    connection: NativeOwnedConnection,
    original_socket: NativeOwnedSocket | None,
    txn_id: UUID | None,
    *,
    cancel_ack_timeout_seconds: float,
) -> list[BaseException]:
    """Retain teardown failures without reconnecting or claiming rollback.

    The caller captures the original socket at its connect boundary, validates
    the host grace before authorization/I/O, and selects an optional CANCEL UUID.
    The caller retains commit outcomes, owner shielding and domain RPC draining.
    Only CANCEL acknowledgment has a deadline; original close precedes joining
    any unanswered waiter. An unexpected replacement transport remains untouched.
    """
    errors: list[BaseException] = []
    cancel_task: asyncio.Task[None] | None = None
    if txn_id is not None:
        try:
            # SDK cancellation connects lazily. Never transfer an owned UUID.
            require_native_socket_affinity(connection, original_socket)

            async def cancel_owned_transaction() -> None:
                # Scheduling can change affinity before this owned task runs.
                require_native_socket_affinity(connection, original_socket)
                await connection.cancel(txn_id)

            cancel_task = asyncio.create_task(cancel_owned_transaction())
            done, _ = await asyncio.wait((cancel_task,), timeout=cancel_ack_timeout_seconds)
            if done:
                completed, cancel_task = cancel_task, None
                completed.result()
            else:
                errors.append(TimeoutError("native CANCEL acknowledgment grace expired"))
                cancel_task.cancel("owned CANCEL acknowledgment deadline")
        except BaseException as exc:
            errors.append(exc)
    try:
        # The pinned SDK suppresses public websocket close errors. Retain them.
        if original_socket is not None:
            await original_socket.close()
    except BaseException as exc:
        errors.append(exc)
    try:
        if connection.socket is not None and connection.socket is not original_socket:
            raise NativeTransactionError("unbound replacement prevents SDK teardown")
        await connection.close()
    except BaseException as exc:
        errors.append(exc)
    if cancel_task is not None:
        try:
            await cancel_task
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                exc.add_note("CANCEL waiter cancelled by owned acknowledgment cleanup")
            errors.append(exc)
    return errors
