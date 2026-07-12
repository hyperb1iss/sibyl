"""Helpers for serialized API idempotency execution and replay."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import wraps
from inspect import signature
from uuid import UUID, uuid4

import structlog
from fastapi import HTTPException, Request
from pydantic import BaseModel

from sibyl.api.schemas.common import MutationReceipt
from sibyl.coordination.locks import LOCK_TTL_SECONDS, LockAcquisitionError, get_locks
from sibyl.persistence import content_runtime
from sibyl.persistence.content_common import ApiIdempotencyRecord

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
_MAX_IDEMPOTENCY_KEY_LENGTH = 255
_PENDING_RESPONSE_STATUS = 102
_REQUEST_CLAIM_ATTRIBUTE = "_sibyl_idempotency_claim"
PROVIDER_REQUEST_HASH_METADATA_KEY = "provider_request_hash"

log = structlog.get_logger()


class IdempotencyConflictCode(StrEnum):
    LOCK_HELD = "idempotency_lock_held"
    PAYLOAD_MISMATCH = "idempotency_payload_mismatch"
    INTERRUPTED_PENDING = "idempotency_interrupted_pending"
    RECOVERY_CONFLICT = "idempotency_recovery_conflict"


def idempotency_conflict(code: IdempotencyConflictCode, message: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": code.value, "message": message},
    )


def idempotency_key(request: object | None) -> str | None:
    key = getattr(request, "headers", {}).get(IDEMPOTENCY_KEY_HEADER)
    if key is None:
        return None
    key = key.strip()
    if not key:
        return None
    if len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long")
    return key


def mutation_receipt(
    request: Request | None,
    *,
    applied: bool,
    revision: int | None,
    affected_records: list[str],
) -> MutationReceipt:
    key = idempotency_key(request)
    return MutationReceipt(
        operation_id=key or str(uuid4()),
        applied=applied,
        revision=revision,
        affected_records=affected_records,
        idempotency_key=key,
    )


def idempotency_request_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def provider_request_hash(body: dict[str, object]) -> str:
    normalized = copy.deepcopy(body)
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop(PROVIDER_REQUEST_HASH_METADATA_KEY, None)
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def provider_request_body(
    request: object,
    parsed_body: BaseModel,
) -> dict[str, object]:
    raw_body: object | None = None
    load_json = getattr(request, "json", None)
    if callable(load_json):
        try:
            raw_body = await load_json()
        except (TypeError, ValueError):
            raw_body = None
    return (
        copy.deepcopy(raw_body)
        if isinstance(raw_body, dict)
        else parsed_body.model_dump(mode="json", exclude_unset=True)
    )


async def reserve_idempotency_record(
    *,
    organization_id: UUID,
    principal_id: str,
    idempotency_key: str,
    method: str,
    path: str,
    payload: object,
    content_session: object,
) -> tuple[ApiIdempotencyRecord, bool]:
    record = await content_runtime.get_api_idempotency_record(
        content_session,
        organization_id=organization_id,
        principal_id=principal_id,
        idempotency_key=idempotency_key,
        method=method.upper(),
        path=path,
    )
    if record is not None:
        return record, False

    pending = ApiIdempotencyRecord(
        organization_id=organization_id,
        principal_id=principal_id,
        idempotency_key=idempotency_key,
        method=method.upper(),
        path=path,
        request_hash=idempotency_request_hash(payload),
        response_status_code=_PENDING_RESPONSE_STATUS,
        response_body={},
    )
    try:
        saved = await content_runtime.save_api_idempotency_record(
            content_session,
            record=pending,
        )
    except Exception as exc:
        record = await content_runtime.get_api_idempotency_record(
            content_session,
            organization_id=organization_id,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            method=method.upper(),
            path=path,
        )
        if record is None:
            log.exception(
                "api_idempotency_reservation_failed",
                method=method.upper(),
                path=path,
                organization_id=str(organization_id),
            )
            raise HTTPException(
                status_code=503,
                detail="Could not reserve the idempotent operation. No mutation was attempted.",
            ) from exc
        return record, False
    return saved, True


async def complete_idempotency_record(
    claim: ApiIdempotencyRecord,
    *,
    response_status_code: int,
    response_body: dict[str, object],
    content_session: object,
) -> ApiIdempotencyRecord:
    now = datetime.now(UTC).replace(tzinfo=None)
    completed = replace(
        claim,
        response_status_code=response_status_code,
        response_body=response_body,
        claim_revision=claim.claim_revision + 1,
        updated_at=now,
    )
    try:
        saved = await content_runtime.compare_and_set_api_idempotency_record(
            content_session,
            record=completed,
            expected_claim_token=claim.claim_token,
            expected_claim_revision=claim.claim_revision,
            pending_status_code=_PENDING_RESPONSE_STATUS,
        )
        if saved is not None:
            return saved
    except Exception as exc:
        completion_error = exc
    else:
        completion_error = RuntimeError("idempotency claim changed before receipt completion")

    try:
        confirmed = await content_runtime.get_api_idempotency_record(
            content_session,
            organization_id=claim.organization_id,
            principal_id=claim.principal_id,
            idempotency_key=claim.idempotency_key,
            method=claim.method,
            path=claim.path,
        )
    except Exception:
        confirmed = None
    if (
        confirmed is not None
        and confirmed.id == claim.id
        and confirmed.request_hash == claim.request_hash
        and confirmed.response_status_code == response_status_code
        and confirmed.response_body == response_body
    ):
        return confirmed
    log.error(
        "api_idempotency_completion_failed",
        method=claim.method,
        path=claim.path,
        organization_id=str(claim.organization_id),
        error_type=type(completion_error).__name__,
    )
    raise HTTPException(
        status_code=503,
        detail={
            "code": "idempotency_receipt_pending",
            "message": (
                "Mutation applied, but its receipt is still pending. "
                "Retrying this idempotency key will not execute it again."
            ),
        },
    ) from completion_error


async def try_reclaim_idempotency_record(
    claim: ApiIdempotencyRecord,
    *,
    stale_before: datetime,
    content_session: object,
) -> ApiIdempotencyRecord | None:
    """Rotate a stale claim after the caller has verified its mutation did not land."""
    if not idempotency_record_pending(claim) or claim.updated_at > stale_before:
        return None

    now = datetime.now(UTC).replace(tzinfo=None)
    reclaimed = replace(
        claim,
        claim_token=str(uuid4()),
        claim_revision=claim.claim_revision + 1,
        updated_at=now,
    )
    return await content_runtime.compare_and_set_api_idempotency_record(
        content_session,
        record=reclaimed,
        expected_claim_token=claim.claim_token,
        expected_claim_revision=claim.claim_revision,
        pending_status_code=_PENDING_RESPONSE_STATUS,
        stale_before=stale_before,
    )


def idempotency_record_pending(record: ApiIdempotencyRecord) -> bool:
    return record.response_status_code == _PENDING_RESPONSE_STATUS


def _store_request_claim(request: object, claim: ApiIdempotencyRecord) -> None:
    state = getattr(request, "state", request)
    setattr(state, _REQUEST_CLAIM_ATTRIBUTE, claim)


def _request_claim(request: object) -> ApiIdempotencyRecord | None:
    state = getattr(request, "state", request)
    claim = getattr(state, _REQUEST_CLAIM_ATTRIBUTE, None)
    return claim if isinstance(claim, ApiIdempotencyRecord) else None


class IdempotencyClaimLease:
    """Keep a durable mutation claim fresh and fence its irreversible write."""

    def __init__(self, request: object, *, content_session: object) -> None:
        self._request = request
        self._content_session = content_session
        self._lock = asyncio.Lock()
        self._lost = False

    async def assert_active(self) -> None:
        """Refresh the durable claim immediately before an irreversible write."""
        async with self._lock:
            if self._lost:
                self._raise_lost()
            claim = _request_claim(self._request)
            if claim is None:
                return
            refreshed = replace(
                claim,
                claim_revision=claim.claim_revision + 1,
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )
            try:
                saved = await content_runtime.compare_and_set_api_idempotency_record(
                    self._content_session,
                    record=refreshed,
                    expected_claim_token=claim.claim_token,
                    expected_claim_revision=claim.claim_revision,
                    pending_status_code=_PENDING_RESPONSE_STATUS,
                )
            except Exception:
                self._lost = True
                raise
            if saved is None:
                self._lost = True
                self._raise_lost()
            assert saved is not None
            _store_request_claim(self._request, saved)

    def _raise_lost(self) -> None:
        raise idempotency_conflict(
            IdempotencyConflictCode.RECOVERY_CONFLICT,
            "The durable idempotency claim changed before the mutation could be applied",
        )


@contextlib.asynccontextmanager
async def idempotency_claim_lease(
    request: object,
    *,
    content_session: object,
    heartbeat_seconds: float = LOCK_TTL_SECONDS / 3,
) -> AsyncGenerator[IdempotencyClaimLease]:
    """Heartbeat a claimed operation until its mutation reaches durable storage."""
    lease = IdempotencyClaimLease(request, content_session=content_session)
    if _request_claim(request) is None:
        yield lease
        return

    await lease.assert_active()

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_seconds)
            try:
                await lease.assert_active()
            except Exception:
                log.exception("api_idempotency_claim_heartbeat_failed")
                return

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        yield lease
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


def _idempotency_lock_id(
    *,
    principal_id: str,
    method: str,
    path: str,
    key: str,
) -> str:
    scope = idempotency_request_hash(
        {
            "principal_id": principal_id,
            "method": method.upper(),
            "path": path,
            "key": key,
        }
    )
    return f"idempotency:{scope}"


@contextlib.asynccontextmanager
async def idempotency_lock(
    *,
    organization_id: str,
    principal_id: str,
    method: str,
    path: str,
    key: str | None,
) -> AsyncGenerator[None]:
    if key is None:
        yield
        return

    manager = get_locks()
    lock_id = _idempotency_lock_id(
        principal_id=principal_id,
        method=method,
        path=path,
        key=key,
    )
    token = await manager.acquire(organization_id, lock_id)
    if token is None:
        raise LockAcquisitionError(lock_id, organization_id)

    async def renew_lease() -> None:
        while True:
            await asyncio.sleep(LOCK_TTL_SECONDS / 3)
            if not await manager.extend(organization_id, lock_id, token):
                log.error(
                    "idempotency_lock_lease_lost",
                    organization_id=organization_id,
                    lock_id=lock_id,
                )
                return

    renew_task = asyncio.create_task(renew_lease())
    try:
        yield
    finally:
        renew_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renew_task
        await manager.release(organization_id, lock_id, token)


def serialize_idempotent_request[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    parameters = signature(func)

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        arguments = parameters.bind_partial(*args, **kwargs).arguments
        request = arguments.get("http_request") or arguments.get("request")
        key = idempotency_key(request)
        if key is None:
            return await func(*args, **kwargs)

        org = arguments.get("org")
        organization_id = str(getattr(org, "id", "unknown"))
        actor = arguments.get("auth") or arguments.get("ctx") or arguments.get("user")
        principal_id = str(
            getattr(actor, "user_id", None)
            or getattr(getattr(actor, "user", None), "id", None)
            or getattr(actor, "id", "unknown")
        )
        method = str(getattr(request, "method", "REQUEST"))
        url = getattr(request, "url", None)
        path = str(getattr(url, "path", "/direct"))

        try:
            async with idempotency_lock(
                organization_id=organization_id,
                principal_id=principal_id,
                method=method,
                path=path,
                key=key,
            ):
                return await func(*args, **kwargs)
        except LockAcquisitionError as exc:
            raise idempotency_conflict(
                IdempotencyConflictCode.LOCK_HELD,
                "An identical idempotent request is still in progress. Please retry.",
            ) from exc

    return wrapper


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
    recover_pending: Callable[[ApiIdempotencyRecord], Awaitable[ResponseModelT | None]]
    | None = None,
    pending_stale_after_seconds: float = LOCK_TTL_SECONDS,
) -> ResponseModelT | None:
    key = idempotency_key(request)
    if key is None:
        return None

    request_hash = idempotency_request_hash(payload)
    record, claimed = await reserve_idempotency_record(
        organization_id=organization_id,
        principal_id=principal_id,
        idempotency_key=key,
        method=method,
        path=path,
        payload=payload,
        content_session=content_session,
    )
    if claimed:
        _store_request_claim(request, record)
        return None
    if record.request_hash != request_hash:
        raise idempotency_conflict(
            IdempotencyConflictCode.PAYLOAD_MISMATCH,
            "Idempotency-Key was already used for a different request",
        )
    if idempotency_record_pending(record):
        if recover_pending is not None:
            recovered = await recover_pending(record)
            if recovered is not None:
                receipt = getattr(recovered, "mutation_receipt", None)
                if isinstance(receipt, MutationReceipt):
                    receipt.replayed = True
                await complete_idempotency_record(
                    record,
                    response_status_code=200,
                    response_body=recovered.model_dump(mode="json"),
                    content_session=content_session,
                )
                return recovered

            stale_before = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                seconds=pending_stale_after_seconds
            )
            reclaimed = await try_reclaim_idempotency_record(
                record,
                stale_before=stale_before,
                content_session=content_session,
            )
            if reclaimed is not None:
                _store_request_claim(request, reclaimed)
                return None
        raise idempotency_conflict(
            IdempotencyConflictCode.INTERRUPTED_PENDING,
            (
                "Idempotent operation is still in progress or was interrupted before "
                "its receipt completed"
            ),
        )
    response = response_model.model_validate(record.response_body)
    receipt = getattr(response, "mutation_receipt", None)
    if isinstance(receipt, MutationReceipt):
        receipt.replayed = True
    return response


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
    key = idempotency_key(request)
    if key is None:
        return

    claim = _request_claim(request)
    if claim is None:
        claim, _ = await reserve_idempotency_record(
            organization_id=organization_id,
            principal_id=principal_id,
            idempotency_key=key,
            method=method,
            path=path,
            payload=payload,
            content_session=content_session,
        )
    await complete_idempotency_record(
        claim,
        response_status_code=status_code,
        response_body=response.model_dump(mode="json"),
        content_session=content_session,
    )
