"""Sandbox lifecycle and task-plane endpoints."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import select

from sibyl.agents.sandbox_controller import SandboxController, SandboxControllerError
from sibyl.auth.dependencies import require_org_role
from sibyl.auth.rls import AuthSession, get_auth_session
from sibyl.db.models import Organization, OrganizationRole

log = structlog.get_logger()

_ROLES = (OrganizationRole.MEMBER, OrganizationRole.ADMIN, OrganizationRole.OWNER)

router = APIRouter(
    prefix="/sandboxes",
    tags=["sandboxes"],
    dependencies=[Depends(require_org_role(*_ROLES))],
)


class SandboxEnsureRequest(BaseModel):
    """Ensure/create sandbox request."""

    user_id: UUID | None = None
    context: dict[str, Any] | None = None
    # Backward compatibility alias for callers still sending metadata.
    metadata: dict[str, Any] | None = None


class SandboxResponse(BaseModel):
    """Sandbox details response."""

    id: UUID
    organization_id: UUID
    user_id: UUID | None = None
    status: str
    runner_id: UUID | None = None
    pod_name: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    context: dict[str, Any] | None = None
    error_message: str | None = None
    # Backward compatibility fields.
    metadata: dict[str, Any] | None = None
    last_error: str | None = None


class SandboxListResponse(BaseModel):
    """List response."""

    sandboxes: list[SandboxResponse]
    total: int


def _require_org(auth: AuthSession) -> Organization:
    if auth.ctx.organization is None:
        raise HTTPException(status_code=403, detail="Organization context required")
    return auth.ctx.organization


def _sandbox_model() -> type[Any]:
    from sibyl.db import models as db_models

    model = getattr(db_models, "Sandbox", None)
    if model is None:
        raise HTTPException(
            status_code=503,
            detail="Sandbox DB model is unavailable; apply sandbox migrations first",
        )
    return model


def _require_controller(request: Request) -> SandboxController:
    controller = getattr(request.app.state, "sandbox_controller", None)
    if controller is None:
        raise HTTPException(status_code=503, detail="Sandbox controller is not initialized")
    return controller


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    with_value = getattr(value, "isoformat", None)
    return with_value() if callable(with_value) else str(value)


def _to_sandbox_response(sandbox: Any) -> SandboxResponse:
    context = getattr(sandbox, "context", None)
    error_message = getattr(sandbox, "error_message", None)
    return SandboxResponse(
        id=sandbox.id,
        organization_id=sandbox.organization_id,
        user_id=getattr(sandbox, "user_id", None),
        status=str(getattr(sandbox, "status", "")),
        runner_id=getattr(sandbox, "runner_id", None),
        pod_name=getattr(sandbox, "pod_name", None),
        created_at=_to_iso(getattr(sandbox, "created_at", None)),
        updated_at=_to_iso(getattr(sandbox, "updated_at", None)),
        context=context,
        error_message=error_message,
        metadata=context,
        last_error=error_message,
    )


@router.post("", response_model=SandboxResponse)
async def ensure_sandbox(
    request: Request,
    body: SandboxEnsureRequest,
    auth: AuthSession = Depends(get_auth_session),
) -> SandboxResponse:
    """Ensure sandbox exists and is active for the target user."""
    org = _require_org(auth)
    controller = _require_controller(request)

    target_user_id = body.user_id or auth.ctx.user.id
    if target_user_id != auth.ctx.user.id and auth.ctx.org_role not in (
        OrganizationRole.ADMIN,
        OrganizationRole.OWNER,
    ):
        raise HTTPException(status_code=403, detail="Only admins can ensure sandbox for another user")

    try:
        context = body.context if body.context is not None else body.metadata
        sandbox = await controller.ensure(
            organization_id=org.id,
            user_id=target_user_id,
            metadata=context,
        )
    except SandboxControllerError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return _to_sandbox_response(sandbox)


@router.get("", response_model=SandboxListResponse)
async def list_sandboxes(
    status: str | None = None,
    auth: AuthSession = Depends(get_auth_session),
) -> SandboxListResponse:
    """List sandboxes in this organization."""
    org = _require_org(auth)
    sandbox_model = _sandbox_model()

    stmt = select(sandbox_model).where(sandbox_model.organization_id == org.id)
    if status:
        stmt = stmt.where(sandbox_model.status == status)
    stmt = stmt.order_by(sandbox_model.created_at.desc())
    result = await auth.session.execute(stmt)
    sandboxes = result.scalars().all()
    return SandboxListResponse(
        sandboxes=[_to_sandbox_response(s) for s in sandboxes],
        total=len(sandboxes),
    )


@router.get("/{sandbox_id}", response_model=SandboxResponse)
async def get_sandbox(
    sandbox_id: UUID,
    auth: AuthSession = Depends(get_auth_session),
) -> SandboxResponse:
    """Get sandbox details."""
    org = _require_org(auth)
    sandbox_model = _sandbox_model()

    result = await auth.session.execute(
        select(sandbox_model).where(
            sandbox_model.id == sandbox_id, sandbox_model.organization_id == org.id
        )
    )
    sandbox = result.scalar_one_or_none()
    if sandbox is None:
        raise HTTPException(status_code=404, detail="Sandbox not found")
    return _to_sandbox_response(sandbox)


@router.post("/{sandbox_id}/suspend", response_model=SandboxResponse)
async def suspend_sandbox(
    request: Request,
    sandbox_id: UUID,
    auth: AuthSession = Depends(get_auth_session),
) -> SandboxResponse:
    """Suspend a sandbox."""
    org = _require_org(auth)
    controller = _require_controller(request)
    try:
        sandbox = await controller.suspend(sandbox_id, organization_id=org.id)
    except SandboxControllerError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail="Sandbox not found") from e
        raise HTTPException(status_code=503, detail=str(e)) from e
    return _to_sandbox_response(sandbox)


@router.post("/{sandbox_id}/resume", response_model=SandboxResponse)
async def resume_sandbox(
    request: Request,
    sandbox_id: UUID,
    auth: AuthSession = Depends(get_auth_session),
) -> SandboxResponse:
    """Resume a sandbox."""
    org = _require_org(auth)
    controller = _require_controller(request)
    try:
        sandbox = await controller.resume(sandbox_id, organization_id=org.id)
    except SandboxControllerError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail="Sandbox not found") from e
        raise HTTPException(status_code=503, detail=str(e)) from e
    return _to_sandbox_response(sandbox)


@router.delete("/{sandbox_id}")
async def delete_sandbox(
    request: Request,
    sandbox_id: UUID,
    auth: AuthSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """Destroy a sandbox."""
    org = _require_org(auth)
    controller = _require_controller(request)
    try:
        sandbox = await controller.destroy(sandbox_id, organization_id=org.id)
    except SandboxControllerError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail="Sandbox not found") from e
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"status": "deleted", "sandbox_id": str(getattr(sandbox, "id", sandbox_id))}


@router.get("/{sandbox_id}/logs")
async def get_sandbox_logs(
    request: Request,
    sandbox_id: UUID,
    tail_lines: int = Query(default=200, ge=1, le=2000),
    auth: AuthSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """Fetch sandbox logs.

    MVP behavior:
    - Returns clear "not implemented" when there is no runtime pod yet.
    """
    org = _require_org(auth)
    controller = _require_controller(request)
    try:
        logs_text = await controller.get_logs(
            sandbox_id=sandbox_id, organization_id=org.id, tail_lines=tail_lines
        )
        return {"sandbox_id": str(sandbox_id), "logs": logs_text}
    except SandboxControllerError as e:
        detail = str(e)
        if "no associated pod" in detail.lower():
            raise HTTPException(
                status_code=501,
                detail="Sandbox logs are not implemented until a runtime pod is provisioned",
            ) from e
        if "not found" in detail.lower():
            raise HTTPException(status_code=404, detail="Sandbox not found") from e
        raise HTTPException(status_code=503, detail=detail) from e
