"""Sandbox control plane for lifecycle and reconciliation.

This module is intentionally defensive:
- Sandbox feature can be disabled entirely.
- Kubernetes is optional; when unavailable we store clear error state.
- DB model availability is validated at runtime for graceful degradation.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

log = structlog.get_logger()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _set_if_present(model: Any, attr: str, value: Any) -> None:
    if hasattr(model, attr):
        setattr(model, attr, value)


def _status_of(model: Any) -> str:
    value = getattr(model, "status", "") or ""
    return str(value).lower()


class SandboxControllerError(RuntimeError):
    """Sandbox lifecycle operation error."""


class SandboxController:
    """Create/suspend/resume/destroy tenant sandboxes with reconciliation."""

    ACTIVE_STATUSES = {"creating", "resuming", "running", "ready", "online", "busy"}
    TERMINAL_STATUSES = {"destroyed", "deleted"}

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        enabled: bool = False,
        namespace: str | None = None,
        pod_prefix: str | None = None,
        reconcile_interval_seconds: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.enabled = enabled
        self.namespace = namespace or os.getenv("SIBYL_SANDBOX_K8S_NAMESPACE", "default")
        self.pod_prefix = pod_prefix or os.getenv("SIBYL_SANDBOX_POD_PREFIX", "sibyl-sandbox")
        self.sandbox_image = os.getenv("SIBYL_SANDBOX_IMAGE", "busybox:1.36")
        self.reconcile_interval_seconds = reconcile_interval_seconds or int(
            os.getenv("SIBYL_SANDBOX_RECONCILE_SECONDS", "20")
        )
        self.k8s_required = _env_bool("SIBYL_SANDBOX_K8S_REQUIRED", default=False)

        self._k8s_checked = False
        self._k8s_error: str | None = None
        self._core_api: Any | None = None
        self._api_exception_type: type[BaseException] | None = None

    @property
    def runtime_error(self) -> str | None:
        """Latest runtime error for diagnostics."""
        return self._k8s_error

    @property
    def k8s_available(self) -> bool:
        """Whether Kubernetes API is configured and ready."""
        return self._core_api is not None

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise SandboxControllerError(
                "Sandbox feature is disabled (set SIBYL_SANDBOX_ENABLED=true or mode shadow/enforced)"
            )

    def _sandbox_model(self) -> type[Any]:
        from sibyl.db import models as db_models

        model = getattr(db_models, "Sandbox", None)
        if model is None:
            raise SandboxControllerError(
                "Sandbox DB model is unavailable; ensure sandbox migrations/models are loaded"
            )
        return model

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _ensure_k8s_client(self) -> bool:
        if self._k8s_checked:
            return self._core_api is not None
        self._k8s_checked = True

        try:
            from kubernetes_asyncio import client as k8s_client, config as k8s_config
        except Exception as e:
            self._k8s_error = (
                "kubernetes_asyncio is not installed; install it or disable sandbox k8s runtime"
            )
            log.warning("sandbox_k8s_import_failed", error=str(e), detail=self._k8s_error)
            return False

        load_errors: list[str] = []
        configured = False

        if hasattr(k8s_config, "load_incluster_config"):
            try:
                await self._maybe_await(k8s_config.load_incluster_config())
                configured = True
            except Exception as e:
                load_errors.append(f"incluster={e}")

        if not configured and hasattr(k8s_config, "load_kube_config"):
            try:
                await self._maybe_await(k8s_config.load_kube_config())
                configured = True
            except Exception as e:
                load_errors.append(f"kubeconfig={e}")

        if not configured:
            self._k8s_error = (
                "Kubernetes configuration not found; "
                "tried in-cluster and local kubeconfig. Details: " + "; ".join(load_errors)
            )
            log.warning("sandbox_k8s_config_unavailable", error=self._k8s_error)
            return False

        self._core_api = k8s_client.CoreV1Api()
        rest_module = getattr(k8s_client, "rest", None)
        self._api_exception_type = (
            getattr(rest_module, "ApiException", None)
            if rest_module is not None
            else getattr(k8s_client, "ApiException", None)
        )
        self._k8s_error = None
        log.info("sandbox_k8s_ready", namespace=self.namespace)
        return True

    def _is_not_found(self, exc: BaseException) -> bool:
        status = getattr(exc, "status", None)
        return status == 404

    def _pod_name_for(self, sandbox_id: UUID | str) -> str:
        sid = str(sandbox_id).replace("_", "-")
        return f"{self.pod_prefix}-{sid[:24]}".lower()

    def _pod_manifest(self, *, pod_name: str, sandbox: Any) -> dict[str, Any]:
        labels = {
            "app": "sibyl-sandbox",
            "sandbox_id": str(getattr(sandbox, "id", "")),
            "organization_id": str(getattr(sandbox, "organization_id", "")),
        }
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "labels": labels},
            "spec": {
                "restartPolicy": "Always",
                "containers": [
                    {
                        "name": "runner",
                        "image": self.sandbox_image,
                        "command": ["sh", "-c", "sleep infinity"],
                    }
                ],
            },
        }

    async def _ensure_pod(self, sandbox: Any) -> bool:
        ready = await self._ensure_k8s_client()
        if not ready:
            if self.k8s_required:
                raise SandboxControllerError(self._k8s_error or "Kubernetes is unavailable")
            return False

        pod_name = getattr(sandbox, "pod_name", None) or self._pod_name_for(sandbox.id)
        _set_if_present(sandbox, "pod_name", pod_name)

        try:
            await self._core_api.read_namespaced_pod(name=pod_name, namespace=self.namespace)
            return True
        except Exception as e:
            if not self._is_not_found(e):
                self._k8s_error = f"Failed reading sandbox pod {pod_name}: {e}"
                if self.k8s_required:
                    raise SandboxControllerError(self._k8s_error) from e
                log.warning("sandbox_pod_read_failed", pod_name=pod_name, error=str(e))
                return False

        try:
            manifest = self._pod_manifest(pod_name=pod_name, sandbox=sandbox)
            await self._core_api.create_namespaced_pod(namespace=self.namespace, body=manifest)
            log.info("sandbox_pod_created", sandbox_id=str(getattr(sandbox, "id", "")), pod_name=pod_name)
            return True
        except Exception as e:
            self._k8s_error = f"Failed creating sandbox pod {pod_name}: {e}"
            if self.k8s_required:
                raise SandboxControllerError(self._k8s_error) from e
            log.warning("sandbox_pod_create_failed", pod_name=pod_name, error=str(e))
            return False

    async def _delete_pod_if_exists(self, pod_name: str | None) -> None:
        if not pod_name:
            return

        if not await self._ensure_k8s_client():
            if self.k8s_required:
                raise SandboxControllerError(self._k8s_error or "Kubernetes is unavailable")
            return

        try:
            await self._core_api.delete_namespaced_pod(name=pod_name, namespace=self.namespace)
            log.info("sandbox_pod_deleted", pod_name=pod_name)
        except Exception as e:
            if self._is_not_found(e):
                return
            self._k8s_error = f"Failed deleting sandbox pod {pod_name}: {e}"
            if self.k8s_required:
                raise SandboxControllerError(self._k8s_error) from e
            log.warning("sandbox_pod_delete_failed", pod_name=pod_name, error=str(e))

    async def _get_sandbox(
        self, session: AsyncSession, sandbox_id: UUID, organization_id: UUID | None
    ) -> Any | None:
        sandbox_model = self._sandbox_model()
        stmt = select(sandbox_model).where(sandbox_model.id == sandbox_id)
        if organization_id is not None and hasattr(sandbox_model, "organization_id"):
            stmt = stmt.where(sandbox_model.organization_id == organization_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def ensure(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Ensure an active sandbox exists for this org/user."""
        self._require_enabled()
        sandbox_model = self._sandbox_model()

        candidate: Any | None = None
        async with self._session_factory() as session:
            stmt = select(sandbox_model).where(
                sandbox_model.organization_id == organization_id,
                sandbox_model.user_id == user_id,
            )
            result = await session.execute(stmt)
            sandboxes = result.scalars().all()

            def _sort_key(item: Any) -> datetime:
                return (
                    getattr(item, "updated_at", None)
                    or getattr(item, "created_at", None)
                    or datetime.min.replace(tzinfo=UTC)
                )

            sandboxes = sorted(sandboxes, key=_sort_key, reverse=True)
            for sandbox in sandboxes:
                status = _status_of(sandbox)
                if status in self.TERMINAL_STATUSES:
                    continue
                candidate = sandbox
                break

        if candidate is None:
            return await self.create(organization_id=organization_id, user_id=user_id, metadata=metadata)

        if _status_of(candidate) in {"suspended", "offline", "stopped"}:
            return await self.resume(candidate.id, organization_id=organization_id)

        return candidate

    async def create(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Create and initialize a new sandbox record (and pod if available)."""
        self._require_enabled()
        sandbox_model = self._sandbox_model()

        async with self._session_factory() as session:
            sandbox = sandbox_model(organization_id=organization_id, user_id=user_id)
            _set_if_present(sandbox, "status", "creating")
            if metadata and hasattr(sandbox, "metadata"):
                sandbox.metadata = metadata
            session.add(sandbox)
            await session.commit()
            await session.refresh(sandbox)

            pod_ready = await self._ensure_pod(sandbox)
            if pod_ready:
                _set_if_present(sandbox, "status", "running")
                _set_if_present(sandbox, "last_error", None)
            elif self.runtime_error:
                _set_if_present(sandbox, "status", "error")
                _set_if_present(sandbox, "last_error", self.runtime_error)

            await session.commit()
            await session.refresh(sandbox)
            log.info(
                "sandbox_created",
                sandbox_id=str(getattr(sandbox, "id", "")),
                organization_id=str(organization_id),
                user_id=str(user_id),
                status=_status_of(sandbox),
            )
            return sandbox

    async def resume(self, sandbox_id: UUID, *, organization_id: UUID | None = None) -> Any:
        """Resume a sandbox by ensuring runtime pod and setting active status."""
        self._require_enabled()

        async with self._session_factory() as session:
            sandbox = await self._get_sandbox(session, sandbox_id, organization_id)
            if sandbox is None:
                raise SandboxControllerError(f"Sandbox not found: {sandbox_id}")

            _set_if_present(sandbox, "status", "resuming")
            pod_ready = await self._ensure_pod(sandbox)
            if pod_ready:
                _set_if_present(sandbox, "status", "running")
                _set_if_present(sandbox, "last_error", None)
            elif self.runtime_error:
                _set_if_present(sandbox, "status", "error")
                _set_if_present(sandbox, "last_error", self.runtime_error)

            await session.commit()
            await session.refresh(sandbox)
            log.info("sandbox_resumed", sandbox_id=str(sandbox_id), status=_status_of(sandbox))
            return sandbox

    async def suspend(self, sandbox_id: UUID, *, organization_id: UUID | None = None) -> Any:
        """Suspend a sandbox and tear down pod when possible."""
        self._require_enabled()

        async with self._session_factory() as session:
            sandbox = await self._get_sandbox(session, sandbox_id, organization_id)
            if sandbox is None:
                raise SandboxControllerError(f"Sandbox not found: {sandbox_id}")

            pod_name = getattr(sandbox, "pod_name", None)
            await self._delete_pod_if_exists(pod_name)

            _set_if_present(sandbox, "status", "suspended")
            _set_if_present(sandbox, "runner_id", None)
            await session.commit()
            await session.refresh(sandbox)
            log.info("sandbox_suspended", sandbox_id=str(sandbox_id))
            return sandbox

    async def destroy(self, sandbox_id: UUID, *, organization_id: UUID | None = None) -> Any:
        """Destroy sandbox runtime and mark DB record as destroyed."""
        self._require_enabled()

        async with self._session_factory() as session:
            sandbox = await self._get_sandbox(session, sandbox_id, organization_id)
            if sandbox is None:
                raise SandboxControllerError(f"Sandbox not found: {sandbox_id}")

            await self._delete_pod_if_exists(getattr(sandbox, "pod_name", None))

            _set_if_present(sandbox, "status", "destroyed")
            _set_if_present(sandbox, "runner_id", None)
            if hasattr(sandbox, "deleted_at"):
                sandbox.deleted_at = datetime.now(UTC).replace(tzinfo=None)
            await session.commit()
            await session.refresh(sandbox)
            log.info("sandbox_destroyed", sandbox_id=str(sandbox_id))
            return sandbox

    async def sync_runner_connection(
        self,
        *,
        sandbox_id: UUID,
        runner_id: UUID | None,
        connected: bool,
    ) -> None:
        """Sync sandbox DB state when a sandbox runner connects/disconnects."""
        if not self.enabled:
            return

        async with self._session_factory() as session:
            sandbox = await self._get_sandbox(session, sandbox_id, organization_id=None)
            if sandbox is None:
                log.warning(
                    "sandbox_runner_sync_missing_sandbox",
                    sandbox_id=str(sandbox_id),
                    runner_id=str(runner_id) if runner_id else None,
                )
                return

            _set_if_present(sandbox, "runner_id", runner_id if connected else None)
            _set_if_present(sandbox, "status", "running" if connected else "ready")
            await session.commit()

    async def get_logs(
        self,
        *,
        sandbox_id: UUID,
        organization_id: UUID | None = None,
        tail_lines: int = 200,
    ) -> str:
        """Fetch pod logs for a sandbox when runtime pod exists."""
        self._require_enabled()
        async with self._session_factory() as session:
            sandbox = await self._get_sandbox(session, sandbox_id, organization_id)
            if sandbox is None:
                raise SandboxControllerError(f"Sandbox not found: {sandbox_id}")
            pod_name = getattr(sandbox, "pod_name", None)

        if not pod_name:
            raise SandboxControllerError(
                "Logs not available: sandbox has no associated pod name yet"
            )
        if not await self._ensure_k8s_client():
            raise SandboxControllerError(
                self.runtime_error or "Kubernetes integration is unavailable for log retrieval"
            )

        try:
            logs_text = await self._core_api.read_namespaced_pod_log(
                name=pod_name,
                namespace=self.namespace,
                tail_lines=tail_lines,
            )
            return str(logs_text or "")
        except Exception as e:
            raise SandboxControllerError(f"Failed to read sandbox logs: {e}") from e

    async def _reconcile_once(self) -> None:
        if not self.enabled:
            return

        sandbox_model = self._sandbox_model()
        tracked_statuses = list(self.ACTIVE_STATUSES | {"error"})

        async with self._session_factory() as session:
            stmt = select(sandbox_model).where(sandbox_model.status.in_(tracked_statuses))
            result = await session.execute(stmt)
            sandboxes = result.scalars().all()
            if not sandboxes:
                return

            k8s_ready = await self._ensure_k8s_client()
            for sandbox in sandboxes:
                if not k8s_ready:
                    if _status_of(sandbox) in {"creating", "resuming"} and self.runtime_error:
                        _set_if_present(sandbox, "status", "error")
                        _set_if_present(sandbox, "last_error", self.runtime_error)
                    continue

                pod_name = getattr(sandbox, "pod_name", None)
                if not pod_name:
                    continue

                try:
                    pod = await self._core_api.read_namespaced_pod(
                        name=pod_name, namespace=self.namespace
                    )
                    phase = (
                        getattr(getattr(pod, "status", None), "phase", None)
                        or ""
                    ).lower()
                    if phase == "running":
                        _set_if_present(sandbox, "status", "running")
                        _set_if_present(sandbox, "last_error", None)
                    elif phase == "pending":
                        _set_if_present(sandbox, "status", "creating")
                    elif phase in {"failed", "unknown"}:
                        _set_if_present(sandbox, "status", "error")
                        _set_if_present(sandbox, "last_error", f"pod_phase={phase}")
                except Exception as e:
                    if self._is_not_found(e):
                        if _status_of(sandbox) in {"running", "resuming", "creating"}:
                            _set_if_present(sandbox, "status", "error")
                            _set_if_present(sandbox, "last_error", "sandbox pod not found")
                    else:
                        log.warning(
                            "sandbox_reconcile_pod_read_failed",
                            sandbox_id=str(getattr(sandbox, "id", "")),
                            pod_name=pod_name,
                            error=str(e),
                        )
            await session.commit()

    async def reconcile_loop(self, stop_event: asyncio.Event | None = None) -> None:
        """Periodic reconcile loop for sandbox runtime/DB consistency."""
        self._require_enabled()
        event = stop_event or asyncio.Event()
        log.info("sandbox_reconcile_started", interval=self.reconcile_interval_seconds)

        while not event.is_set():
            try:
                await self._reconcile_once()
            except Exception as e:
                log.warning("sandbox_reconcile_failed", error=str(e))

            try:
                await asyncio.wait_for(event.wait(), timeout=self.reconcile_interval_seconds)
            except TimeoutError:
                continue

        log.info("sandbox_reconcile_stopped")
