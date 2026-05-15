"""Helpers for API idempotency replay."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

import structlog
from fastapi import HTTPException, Request
from pydantic import BaseModel

from sibyl.persistence import content_runtime
from sibyl.persistence.content_common import ApiIdempotencyRecord

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
_MAX_IDEMPOTENCY_KEY_LENGTH = 255

log = structlog.get_logger()


def _idempotency_key(request: Request) -> str | None:
    key = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if key is None:
        return None
    key = key.strip()
    if not key:
        return None
    if len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
    return key


def idempotency_request_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def replay_idempotent_response[ResponseModelT: BaseModel](
    request: Request,
    *,
    organization_id: UUID,
    principal_id: str,
    method: str,
    path: str,
    payload: object,
    response_model: type[ResponseModelT],
    content_session: object,
) -> ResponseModelT | None:
    key = _idempotency_key(request)
    if key is None:
        return None

    request_hash = idempotency_request_hash(payload)
    record = await content_runtime.get_api_idempotency_record(
        content_session,
        organization_id=organization_id,
        principal_id=principal_id,
        idempotency_key=key,
        method=method.upper(),
        path=path,
    )
    if record is None:
        return None
    if record.request_hash != request_hash:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used for a different request",
        )
    return response_model.model_validate(record.response_body)


async def save_idempotent_response(
    request: Request,
    *,
    organization_id: UUID,
    principal_id: str,
    method: str,
    path: str,
    payload: object,
    response: BaseModel,
    status_code: int,
    content_session: object,
) -> None:
    key = _idempotency_key(request)
    if key is None:
        return

    record = ApiIdempotencyRecord(
        organization_id=organization_id,
        principal_id=principal_id,
        idempotency_key=key,
        method=method.upper(),
        path=path,
        request_hash=idempotency_request_hash(payload),
        response_status_code=status_code,
        response_body=response.model_dump(mode="json"),
    )
    try:
        await content_runtime.save_api_idempotency_record(content_session, record=record)
    except Exception as exc:
        log.exception(
            "api_idempotency_save_failed",
            method=method.upper(),
            path=path,
            organization_id=str(organization_id),
            error=str(exc),
        )
