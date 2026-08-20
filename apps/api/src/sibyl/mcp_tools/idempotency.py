"""Shared idempotency mechanics for MCP mutation tools."""

from collections.abc import Awaitable, Callable, Mapping
from functools import wraps
from typing import Any
from uuid import uuid4

import sibyl.mcp_tools.context as mcp_context
from sibyl.api.idempotency import idempotency_lock


def serialize_request[**P, R](
    path: str,
    *,
    action_scoped: bool = False,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            raw_key: object = kwargs.get("idempotency_key")
            data = kwargs.get("data")
            if raw_key is None and isinstance(data, Mapping):
                raw_key = data.get("idempotency_key")
            if not isinstance(raw_key, str) or not raw_key.strip():
                return await func(*args, **kwargs)

            scoped_path = path
            if action_scoped:
                action = str(kwargs.get("action", "unknown")).lower().strip()
                scoped_path = f"{path}/{action}"
            ctx = await mcp_context.require_context(write=True)
            async with idempotency_lock(
                organization_id=ctx.org_id,
                principal_id=ctx.user_id or "unknown",
                method="MCP",
                path=scoped_path,
                key=raw_key.strip(),
            ):
                return await func(*args, **kwargs)

        return wrapper

    return decorator


def mutation_receipt(
    data: Mapping[str, Any],
    *,
    applied: bool,
    revision: int | None,
    affected_records: list[str],
) -> dict[str, Any]:
    key = data.get("idempotency_key")
    idempotency_key = key.strip() if isinstance(key, str) and key.strip() else None
    return {
        "operation_id": idempotency_key or str(uuid4()),
        "applied": applied,
        "revision": revision,
        "affected_records": affected_records,
        "idempotency_key": idempotency_key,
        "replayed": False,
    }
