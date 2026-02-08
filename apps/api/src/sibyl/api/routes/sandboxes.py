"""Sandbox lifecycle and task-plane endpoints."""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
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


def _check_sandbox_access(auth: AuthSession, sandbox: Any) -> None:
    """Non-admin users can only access their own sandbox."""
    if auth.ctx.org_role in (OrganizationRole.ADMIN, OrganizationRole.OWNER):
        return
    sandbox_user_id = getattr(sandbox, "user_id", None)
    if sandbox_user_id != auth.ctx.user.id:
        raise HTTPException(status_code=404, detail="Sandbox not found")


async def _preflight_sandbox_access(auth: AuthSession, sandbox_id: UUID, org_id: UUID) -> None:
    """Pre-fetch sandbox and enforce ownership for mutating endpoints."""
    if auth.ctx.org_role in (OrganizationRole.ADMIN, OrganizationRole.OWNER):
        return
    sandbox_model = _sandbox_model()
    result = await auth.session.execute(
        select(sandbox_model).where(
            sandbox_model.id == sandbox_id, sandbox_model.organization_id == org_id
        )
    )
    sandbox = result.scalar_one_or_none()
    if sandbox is None:
        raise HTTPException(status_code=404, detail="Sandbox not found")
    _check_sandbox_access(auth, sandbox)


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
    """List sandboxes in this organization.

    Non-admin users only see their own sandbox. Admins/owners see all.
    """
    org = _require_org(auth)
    sandbox_model = _sandbox_model()

    stmt = select(sandbox_model).where(sandbox_model.organization_id == org.id)
    # Non-admin users only see their own sandbox
    if auth.ctx.org_role not in (OrganizationRole.ADMIN, OrganizationRole.OWNER):
        stmt = stmt.where(sandbox_model.user_id == auth.ctx.user.id)
    if status:
        stmt = stmt.where(sandbox_model.status == status)
    stmt = stmt.order_by(sandbox_model.created_at.desc())
    result = await auth.session.execute(stmt)
    sandboxes = result.scalars().all()
    return SandboxListResponse(
        sandboxes=[_to_sandbox_response(s) for s in sandboxes],
        total=len(sandboxes),
    )


# ---------------------------------------------------------------------------
# Admin endpoints (must be defined before /{sandbox_id} to avoid route clash)
# ---------------------------------------------------------------------------
_ADMIN_ROLES = (OrganizationRole.ADMIN, OrganizationRole.OWNER)


@router.post("/admin/rollback")
async def sandbox_rollback(
    request: Request,
    auth: AuthSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """Emergency rollback: suspend all running sandboxes and fail pending tasks."""
    org = _require_org(auth)
    if auth.ctx.org_role not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin or owner role required")

    controller = _require_controller(request)
    sandboxes_suspended = await controller.suspend_all(org.id)

    tasks_failed = 0
    dispatcher = getattr(request.app.state, "sandbox_dispatcher", None)
    if dispatcher is not None:
        tasks_failed = await dispatcher.fail_all_pending(org.id)

    log.info(
        "sandbox_admin_rollback",
        org_id=str(org.id),
        sandboxes_suspended=sandboxes_suspended,
        tasks_failed=tasks_failed,
    )

    return {
        "status": "rolled_back",
        "sandboxes_suspended": sandboxes_suspended,
        "tasks_failed": tasks_failed,
    }


@router.post("/admin/cleanup")
async def sandbox_cleanup(
    request: Request,
    auth: AuthSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """On-demand stale resource cleanup."""
    org = _require_org(auth)
    if auth.ctx.org_role not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin or owner role required")

    controller = _require_controller(request)

    # Reap stale tasks via dispatcher
    reaped_tasks = 0
    dispatcher = getattr(request.app.state, "sandbox_dispatcher", None)
    if dispatcher is not None:
        reaped_tasks = await dispatcher.reap_stale_tasks()

    # Find and delete orphaned pods
    orphaned_pods = await controller.find_orphaned_pods(org.id)
    deleted_pods = 0
    for pod_name in orphaned_pods:
        try:
            await controller._delete_pod_if_exists(pod_name)
            deleted_pods += 1
        except Exception as e:
            log.warning("sandbox_cleanup_pod_delete_failed", pod_name=pod_name, error=str(e))

    log.info(
        "sandbox_admin_cleanup",
        org_id=str(org.id),
        reaped_tasks=reaped_tasks,
        orphaned_pods=len(orphaned_pods),
        deleted_pods=deleted_pods,
    )

    return {
        "status": "cleaned_up",
        "reaped_tasks": reaped_tasks,
        "orphaned_pods_found": len(orphaned_pods),
        "orphaned_pods_deleted": deleted_pods,
    }


@router.get("/admin/rollout")
async def sandbox_rollout_status(
    request: Request,
    auth: AuthSession = Depends(get_auth_session),
) -> dict[str, Any]:
    """Show current rollout config and this org's effective mode."""
    org = _require_org(auth)
    if auth.ctx.org_role not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin or owner role required")

    from sibyl.agents.sandbox_rollout import resolve_sandbox_mode
    from sibyl.config import settings

    effective_mode = resolve_sandbox_mode(
        global_mode=settings.sandbox_mode,
        org_id=org.id,
        rollout_percent=settings.sandbox_rollout_percent,
        rollout_orgs=settings.sandbox_rollout_orgs,
        canary_mode=settings.sandbox_canary_mode,
    )

    return {
        "global_mode": settings.sandbox_mode,
        "rollout_percent": settings.sandbox_rollout_percent,
        "rollout_orgs": settings.sandbox_rollout_orgs,
        "canary_mode": settings.sandbox_canary_mode,
        "org_id": str(org.id),
        "is_in_rollout": str(org.id) in settings.sandbox_rollout_orgs,
        "effective_mode": effective_mode,
    }


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
    _check_sandbox_access(auth, sandbox)
    return _to_sandbox_response(sandbox)


@router.post("/{sandbox_id}/suspend", response_model=SandboxResponse)
async def suspend_sandbox(
    request: Request,
    sandbox_id: UUID,
    auth: AuthSession = Depends(get_auth_session),
) -> SandboxResponse:
    """Suspend a sandbox."""
    org = _require_org(auth)
    await _preflight_sandbox_access(auth, sandbox_id, org.id)
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
    await _preflight_sandbox_access(auth, sandbox_id, org.id)
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
    await _preflight_sandbox_access(auth, sandbox_id, org.id)
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
    await _preflight_sandbox_access(auth, sandbox_id, org.id)
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


@router.websocket("/{sandbox_id}/attach")
async def sandbox_attach(websocket: WebSocket, sandbox_id: UUID) -> None:
    """WebSocket exec proxy: browser terminal <-> K8s pod shell.

    Auth: JWT from ?token= query param, Authorization header, or sibyl_access_token cookie.
    Protocol:
      - Client sends raw text (stdin) or {"type": "resize", "cols": N, "rows": M}
      - Server sends raw text (stdout/stderr from pod)
    """
    from sibyl.agents.sandbox_exec import SandboxExecProxy
    from sibyl.auth.jwt import JwtError, verify_access_token
    from sibyl.config import settings

    # 1. Extract JWT from query param, Authorization header, or cookie
    token = (
        websocket.query_params.get("token")
        or websocket.cookies.get("sibyl_access_token")
    )
    auth_header = websocket.headers.get("authorization")
    if not token and auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]

    if not token and not settings.disable_auth:
        await websocket.close(code=4001, reason="Missing token")
        return

    org_id: UUID | None = None
    user_id: UUID | None = None

    if not settings.disable_auth:
        try:
            claims = verify_access_token(token)  # type: ignore[arg-type]
        except JwtError:
            await websocket.close(code=4001, reason="Invalid token")
            return

        org_claim = claims.get("org")
        user_claim = claims.get("sub")
        if not (org_claim and user_claim):
            await websocket.close(code=4001, reason="Invalid token claims")
            return

        try:
            org_id = UUID(str(org_claim))
            user_id = UUID(str(user_claim))
        except (TypeError, ValueError):
            await websocket.close(code=4001, reason="Invalid token claims")
            return

    # 2. Look up sandbox, verify status and ownership
    sandbox_model = _sandbox_model()
    controller = _require_controller(websocket)  # type: ignore[arg-type]

    from sibyl.db.connection import get_session

    async with get_session() as session:
        stmt = select(sandbox_model).where(sandbox_model.id == sandbox_id)
        if org_id is not None:
            stmt = stmt.where(sandbox_model.organization_id == org_id)
        result = await session.execute(stmt)
        sandbox = result.scalar_one_or_none()

    if sandbox is None:
        await websocket.close(code=4004, reason="Sandbox not found")
        return

    status = str(getattr(sandbox, "status", "")).lower()
    if status != "running":
        await websocket.close(code=4009, reason=f"Sandbox is not running (status={status})")
        return

    pod_name = getattr(sandbox, "pod_name", None)
    if not pod_name:
        await websocket.close(code=4009, reason="Sandbox has no pod assigned")
        return

    # 3. Check ownership (non-admin users can only attach to their own sandbox)
    if user_id is not None and not settings.disable_auth:
        sandbox_user_id = getattr(sandbox, "user_id", None)
        # Allow if same user or if we can't determine (let it through)
        if sandbox_user_id is not None and sandbox_user_id != user_id:
            # Would need role check here -- for now, close
            await websocket.close(code=4003, reason="Access denied")
            return

    # 4. Accept WebSocket and start relay
    await websocket.accept()
    namespace = getattr(sandbox, "namespace", None) or controller.namespace

    proxy = SandboxExecProxy(namespace=namespace)
    try:
        await proxy.connect(pod_name=pod_name, namespace=namespace)
        log.info(
            "sandbox_exec_attached",
            sandbox_id=str(sandbox_id),
            pod_name=pod_name,
            user_id=str(user_id) if user_id else None,
        )
        await proxy.relay(browser_ws=websocket)
    except Exception as e:
        log.warning(
            "sandbox_exec_error",
            sandbox_id=str(sandbox_id),
            pod_name=pod_name,
            error=str(e),
        )
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason=str(e)[:120])
    finally:
        await proxy.close()
        log.info("sandbox_exec_detached", sandbox_id=str(sandbox_id), pod_name=pod_name)
