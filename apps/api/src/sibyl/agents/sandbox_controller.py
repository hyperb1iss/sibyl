"""Sandbox control plane backed by kubernetes-sigs/agent-sandbox CRDs.

This controller keeps the existing Sibyl sandbox API contract while delegating
runtime lifecycle to the `agents.x-k8s.io/v1alpha1 Sandbox` custom resource.
Sibyl remains responsible for:
- Sandbox DB records and orchestration metadata
- Runner/task binding and auth token minting
- Policy-driven suspend/resume/reconcile behavior
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.agents.sandbox_utils import set_if_present as _set_if_present, status_of as _status_of

log = structlog.get_logger()


class SandboxControllerError(RuntimeError):
    """Sandbox lifecycle operation error."""


class SandboxController:
    """Create/suspend/resume/destroy tenant sandboxes with reconciliation."""

    ACTIVE_STATUSES = {"pending", "starting", "running", "terminating"}
    TERMINAL_STATUSES = {"deleted"}

    _SANDBOX_GROUP = "agents.x-k8s.io"
    _SANDBOX_VERSION = "v1alpha1"
    _SANDBOX_PLURAL = "sandboxes"
    _SANDBOX_POD_NAME_ANNOTATION = "agents.x-k8s.io/pod-name"

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        enabled: bool = False,
        namespace: str | None = None,
        pod_prefix: str | None = None,
        reconcile_interval_seconds: int | None = None,
        idle_ttl_seconds: int | None = None,
        max_lifetime_seconds: int | None = None,
        sandbox_image: str | None = None,
        server_url: str | None = None,
        k8s_required: bool = False,
        dispatcher: Any | None = None,
        worktree_base: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.enabled = enabled
        self._dispatcher = dispatcher
        self.namespace = namespace or "default"
        self.pod_prefix = pod_prefix or "sibyl-sandbox"
        self.sandbox_image = sandbox_image or "ghcr.io/hyperb1iss/sibyl-sandbox:latest"
        self.reconcile_interval_seconds = reconcile_interval_seconds or 20
        self.idle_ttl_seconds = idle_ttl_seconds or 1800
        self.max_lifetime_seconds = max_lifetime_seconds or 14400
        self.k8s_required = k8s_required
        self.server_url = server_url or ""
        self.worktree_base = worktree_base or "/tmp/sibyl/sandboxes"  # noqa: S108

        self._k8s_checked = False
        self._k8s_error: str | None = None
        self._core_api: Any | None = None
        self._custom_api: Any | None = None
        self._api_exception_type: type[BaseException] | None = None

    @property
    def runtime_error(self) -> str | None:
        """Latest runtime error for diagnostics."""
        return self._k8s_error

    @property
    def k8s_available(self) -> bool:
        """Whether Kubernetes API is configured and ready."""
        return self._core_api is not None and self._custom_api is not None

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

    def _runner_model(self) -> type[Any]:
        from sibyl.db import models as db_models

        model = getattr(db_models, "Runner", None)
        if model is None:
            raise SandboxControllerError(
                "Runner DB model is unavailable; ensure runner migrations/models are loaded"
            )
        return model

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _ensure_k8s_client(self) -> bool:
        if self._k8s_checked:
            return self.k8s_available
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
        self._custom_api = k8s_client.CustomObjectsApi()
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

    def _runtime_name_for(self, sandbox: Any) -> str:
        context = getattr(sandbox, "context", {}) or {}
        runtime_name: str | None = None
        if isinstance(context, dict):
            raw = context.get("agent_sandbox_name")
            if isinstance(raw, str) and raw.strip():
                runtime_name = raw.strip()
        if not runtime_name:
            raw_pod_name = getattr(sandbox, "pod_name", None)
            if isinstance(raw_pod_name, str) and raw_pod_name.strip():
                runtime_name = raw_pod_name.strip()
        if not runtime_name:
            runtime_name = self._pod_name_for(getattr(sandbox, "id", "sandbox"))
        return runtime_name

    def _persist_runtime_identity(self, sandbox: Any, runtime_name: str) -> None:
        _set_if_present(sandbox, "pod_name", runtime_name)
        _set_if_present(sandbox, "namespace", self.namespace)
        if hasattr(sandbox, "context"):
            context = dict(getattr(sandbox, "context", {}) or {})
            context["agent_sandbox_name"] = runtime_name
            sandbox.context = context

    async def _read_runtime_sandbox(self, runtime_name: str) -> dict[str, Any] | None:
        if not self.k8s_available:
            return None
        try:
            return await self._custom_api.get_namespaced_custom_object(
                group=self._SANDBOX_GROUP,
                version=self._SANDBOX_VERSION,
                namespace=self.namespace,
                plural=self._SANDBOX_PLURAL,
                name=runtime_name,
            )
        except Exception as e:
            if self._is_not_found(e):
                return None
            raise

    async def _create_or_patch_runtime_sandbox(self, sandbox: Any) -> dict[str, Any] | None:
        ready = await self._ensure_k8s_client()
        if not ready:
            if self.k8s_required:
                raise SandboxControllerError(self._k8s_error or "Kubernetes is unavailable")
            return None

        runtime_name = self._runtime_name_for(sandbox)
        self._persist_runtime_identity(sandbox, runtime_name)
        manifest = self._sandbox_manifest(runtime_name=runtime_name, sandbox=sandbox)
        existing = await self._read_runtime_sandbox(runtime_name)

        if existing is None:
            try:
                created = await self._custom_api.create_namespaced_custom_object(
                    group=self._SANDBOX_GROUP,
                    version=self._SANDBOX_VERSION,
                    namespace=self.namespace,
                    plural=self._SANDBOX_PLURAL,
                    body=manifest,
                )
                log.info(
                    "sandbox_runtime_created",
                    sandbox_id=str(getattr(sandbox, "id", "")),
                    runtime_name=runtime_name,
                )
                return created
            except Exception as e:
                self._k8s_error = f"Failed creating runtime sandbox {runtime_name}: {e}"
                if self.k8s_required:
                    raise SandboxControllerError(self._k8s_error) from e
                log.warning("sandbox_runtime_create_failed", runtime_name=runtime_name, error=str(e))
                return None

        patch = {
            "metadata": {
                "labels": manifest["metadata"].get("labels", {}),
                "annotations": manifest["metadata"].get("annotations", {}),
            },
            "spec": manifest["spec"],
        }
        try:
            return await self._custom_api.patch_namespaced_custom_object(
                group=self._SANDBOX_GROUP,
                version=self._SANDBOX_VERSION,
                namespace=self.namespace,
                plural=self._SANDBOX_PLURAL,
                name=runtime_name,
                body=patch,
            )
        except Exception as e:
            self._k8s_error = f"Failed patching runtime sandbox {runtime_name}: {e}"
            if self.k8s_required:
                raise SandboxControllerError(self._k8s_error) from e
            log.warning("sandbox_runtime_patch_failed", runtime_name=runtime_name, error=str(e))
            return None

    async def _scale_runtime_sandbox(self, runtime_name: str, replicas: int) -> None:
        if not await self._ensure_k8s_client():
            if self.k8s_required:
                raise SandboxControllerError(self._k8s_error or "Kubernetes is unavailable")
            return

        try:
            await self._custom_api.patch_namespaced_custom_object(
                group=self._SANDBOX_GROUP,
                version=self._SANDBOX_VERSION,
                namespace=self.namespace,
                plural=self._SANDBOX_PLURAL,
                name=runtime_name,
                body={"spec": {"replicas": replicas}},
            )
        except Exception as e:
            if self._is_not_found(e):
                return
            self._k8s_error = f"Failed scaling runtime sandbox {runtime_name}: {e}"
            if self.k8s_required:
                raise SandboxControllerError(self._k8s_error) from e
            log.warning(
                "sandbox_runtime_scale_failed",
                runtime_name=runtime_name,
                replicas=replicas,
                error=str(e),
            )

    async def _delete_runtime_sandbox(self, runtime_name: str | None) -> None:
        if not runtime_name:
            return
        if not await self._ensure_k8s_client():
            if self.k8s_required:
                raise SandboxControllerError(self._k8s_error or "Kubernetes is unavailable")
            return

        try:
            await self._custom_api.delete_namespaced_custom_object(
                group=self._SANDBOX_GROUP,
                version=self._SANDBOX_VERSION,
                namespace=self.namespace,
                plural=self._SANDBOX_PLURAL,
                name=runtime_name,
                body={},
            )
            log.info("sandbox_runtime_deleted", runtime_name=runtime_name)
        except Exception as e:
            if self._is_not_found(e):
                return
            self._k8s_error = f"Failed deleting runtime sandbox {runtime_name}: {e}"
            if self.k8s_required:
                raise SandboxControllerError(self._k8s_error) from e
            log.warning("sandbox_runtime_delete_failed", runtime_name=runtime_name, error=str(e))

    def _runtime_ready(self, runtime: dict[str, Any]) -> bool:
        conditions = runtime.get("status", {}).get("conditions", [])
        if not isinstance(conditions, list):
            return False
        for cond in conditions:
            if not isinstance(cond, dict):
                continue
            if cond.get("type") == "Ready":
                return str(cond.get("status", "")).lower() == "true"
        return False

    def _runtime_status_replicas(self, runtime: dict[str, Any]) -> int:
        raw = runtime.get("status", {}).get("replicas", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def _runtime_spec_replicas(self, runtime: dict[str, Any]) -> int:
        raw = runtime.get("spec", {}).get("replicas", 1)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 1

    def _runtime_pod_name(self, runtime: dict[str, Any]) -> str | None:
        metadata = runtime.get("metadata", {}) if isinstance(runtime, dict) else {}
        if not isinstance(metadata, dict):
            return None
        annotations = metadata.get("annotations", {})
        if isinstance(annotations, dict):
            raw = annotations.get(self._SANDBOX_POD_NAME_ANNOTATION)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        raw_name = metadata.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            return raw_name.strip()
        return None

    async def _new_graph_runner_id(self, session: AsyncSession) -> str:
        runner_model = self._runner_model()
        for _ in range(10):
            candidate = f"runner_{secrets.token_hex(8)}"
            result = await session.execute(
                select(runner_model).where(runner_model.graph_runner_id == candidate)
            )
            if result.scalar_one_or_none() is None:
                return candidate
        raise SandboxControllerError("Failed generating unique graph runner ID")

    async def _ensure_sandbox_runner(self, session: AsyncSession, sandbox: Any) -> Any:
        runner_model = self._runner_model()
        from sibyl.db.models import RunnerStatus

        sandbox_id = getattr(sandbox, "id", None)
        org_id = getattr(sandbox, "organization_id", None)
        user_id = getattr(sandbox, "user_id", None)
        if sandbox_id is None or org_id is None or user_id is None:
            raise SandboxControllerError("Sandbox missing id/org/user; cannot bind sandbox runner")

        current_runner_id = getattr(sandbox, "runner_id", None)
        if current_runner_id:
            result = await session.execute(
                select(runner_model).where(
                    runner_model.id == current_runner_id,
                    runner_model.organization_id == org_id,
                )
            )
            runner = result.scalar_one_or_none()
            if runner is not None:
                _set_if_present(runner, "sandbox_id", sandbox_id)
                _set_if_present(runner, "is_sandbox_runner", True)
                return runner

        existing_result = await session.execute(
            select(runner_model).where(
                runner_model.organization_id == org_id,
                runner_model.sandbox_id == sandbox_id,
            )
        )
        existing_runner = existing_result.scalar_one_or_none()
        if existing_runner is not None:
            _set_if_present(existing_runner, "is_sandbox_runner", True)
            _set_if_present(sandbox, "runner_id", existing_runner.id)
            return existing_runner

        graph_runner_id = await self._new_graph_runner_id(session)
        name = f"sandbox-runner-{str(sandbox_id)[:8]}"
        runner = runner_model(
            organization_id=org_id,
            user_id=user_id,
            graph_runner_id=graph_runner_id,
            name=name,
            hostname=name,
            capabilities=["sandbox", "kubernetes"],
            max_concurrent_agents=1,
            status=RunnerStatus.OFFLINE.value,
            sandbox_id=sandbox_id,
            is_sandbox_runner=True,
        )
        session.add(runner)
        await session.flush()
        _set_if_present(sandbox, "runner_id", runner.id)
        log.info(
            "sandbox_runner_created",
            sandbox_id=str(sandbox_id),
            runner_id=str(getattr(runner, "id", "")),
            org_id=str(org_id),
        )
        return runner

    def _sandbox_manifest(self, *, runtime_name: str, sandbox: Any) -> dict[str, Any]:
        labels = {
            "app": "sibyl-sandbox",
            "sandbox_id": str(getattr(sandbox, "id", "")),
            "organization_id": str(getattr(sandbox, "organization_id", "")),
        }
        annotations = {
            "sibyl.ai/sandbox-id": str(getattr(sandbox, "id", "")),
            "sibyl.ai/organization-id": str(getattr(sandbox, "organization_id", "")),
        }
        image = str(getattr(sandbox, "image", "") or self.sandbox_image)

        env_vars = [
            {"name": "SIBYL_SANDBOX_ID", "value": str(getattr(sandbox, "id", ""))},
            {"name": "SIBYL_SERVER_URL", "value": self.server_url},
            {"name": "SIBYL_SANDBOX_WORKTREE_BASE", "value": self.worktree_base},
            {"name": "SIBYL_SANDBOX_MODE", "value": "true"},
        ]

        runner_id = getattr(sandbox, "runner_id", None)
        if runner_id:
            env_vars.append({"name": "SIBYL_RUNNER_ID", "value": str(runner_id)})

        sandbox_id = getattr(sandbox, "id", None)
        org_id = getattr(sandbox, "organization_id", None)
        user_id = getattr(sandbox, "user_id", None)
        if runner_id and org_id and user_id:
            try:
                from sibyl.auth.jwt import create_runner_token

                token = create_runner_token(
                    user_id=user_id,
                    organization_id=org_id,
                    runner_id=runner_id,
                    sandbox_id=sandbox_id,
                )
                env_vars.append({"name": "SIBYL_RUNNER_TOKEN", "value": token})
            except Exception as e:
                log.warning("sandbox_token_mint_failed", error=str(e))

        resources = {
            "requests": {
                "cpu": str(getattr(sandbox, "cpu_request", "250m")),
                "memory": str(getattr(sandbox, "memory_request", "512Mi")),
                "ephemeral-storage": str(getattr(sandbox, "ephemeral_storage_request", "1Gi")),
            },
            "limits": {
                "cpu": str(getattr(sandbox, "cpu_limit", "1000m")),
                "memory": str(getattr(sandbox, "memory_limit", "2Gi")),
                "ephemeral-storage": str(getattr(sandbox, "ephemeral_storage_limit", "4Gi")),
            },
        }

        security_context = {
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": False,
        }

        return {
            "apiVersion": f"{self._SANDBOX_GROUP}/{self._SANDBOX_VERSION}",
            "kind": "Sandbox",
            "metadata": {
                "name": runtime_name,
                "labels": labels,
                "annotations": annotations,
            },
            "spec": {
                "replicas": 1,
                "podTemplate": {
                    "metadata": {
                        "labels": labels,
                        "annotations": annotations,
                    },
                    "spec": {
                        "restartPolicy": "Always",
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "runner",
                                "image": image,
                                "env": env_vars,
                                "resources": resources,
                                "securityContext": security_context,
                                "volumeMounts": [
                                    {
                                        "name": "worktree-storage",
                                        "mountPath": self.worktree_base,
                                    }
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "worktree-storage",
                                "emptyDir": {},
                            }
                        ],
                    },
                },
            },
        }

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
            try:
                return await self.create(
                    organization_id=organization_id, user_id=user_id, metadata=metadata
                )
            except IntegrityError:
                async with self._session_factory() as session:
                    stmt = select(self._sandbox_model()).where(
                        self._sandbox_model().organization_id == organization_id,
                        self._sandbox_model().user_id == user_id,
                        self._sandbox_model().status.notin_(list(self.TERMINAL_STATUSES)),
                    )
                    result = await session.execute(stmt)
                    candidate = result.scalar_one()

        if candidate is None:
            raise SandboxControllerError("Failed resolving sandbox candidate")

        if _status_of(candidate) in {"suspended", "failed"}:
            return await self.resume(candidate.id, organization_id=organization_id)

        # Backfill runner binding if this sandbox predates the runner bootstrap.
        async with self._session_factory() as session:
            sandbox = await self._get_sandbox(session, candidate.id, organization_id)
            if sandbox is None:
                return candidate
            await self._ensure_sandbox_runner(session, sandbox)
            await session.commit()
            await session.refresh(sandbox)
            return sandbox

    async def create(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Create and initialize a new sandbox record and runtime Sandbox CR."""
        start = time.monotonic()
        self._require_enabled()
        sandbox_model = self._sandbox_model()
        now = datetime.now(UTC).replace(tzinfo=None)

        async with self._session_factory() as session:
            sandbox = sandbox_model(organization_id=organization_id, user_id=user_id)
            _set_if_present(sandbox, "status", "starting")
            if metadata and hasattr(sandbox, "context"):
                context = dict(getattr(sandbox, "context", {}) or {})
                context.update(metadata)
                sandbox.context = context

            session.add(sandbox)
            await session.flush()
            await self._ensure_sandbox_runner(session, sandbox)
            runtime = await self._create_or_patch_runtime_sandbox(sandbox)

            if runtime is not None:
                _set_if_present(
                    sandbox,
                    "status",
                    "running" if self._runtime_ready(runtime) else "starting",
                )
                _set_if_present(sandbox, "error_message", None)
                if getattr(sandbox, "started_at", None) is None:
                    _set_if_present(sandbox, "started_at", now)
                runtime_pod_name = self._runtime_pod_name(runtime)
                if runtime_pod_name:
                    _set_if_present(sandbox, "pod_name", runtime_pod_name)
            elif self.runtime_error:
                _set_if_present(sandbox, "status", "failed")
                _set_if_present(sandbox, "error_message", self.runtime_error)

            await session.commit()
            await session.refresh(sandbox)
            duration_ms = (time.monotonic() - start) * 1000
            log.info(
                "sandbox_created",
                sandbox_id=str(getattr(sandbox, "id", "")),
                organization_id=str(organization_id),
                user_id=str(user_id),
                status=_status_of(sandbox),
                duration_ms=round(duration_ms, 1),
            )
            return sandbox

    async def resume(self, sandbox_id: UUID, *, organization_id: UUID | None = None) -> Any:
        """Resume a sandbox by scaling runtime Sandbox CR to one replica."""
        start = time.monotonic()
        self._require_enabled()
        now = datetime.now(UTC).replace(tzinfo=None)

        async with self._session_factory() as session:
            sandbox = await self._get_sandbox(session, sandbox_id, organization_id)
            if sandbox is None:
                raise SandboxControllerError(f"Sandbox not found: {sandbox_id}")

            _set_if_present(sandbox, "status", "starting")
            _set_if_present(sandbox, "stopped_at", None)
            _set_if_present(sandbox, "last_heartbeat", None)
            await self._ensure_sandbox_runner(session, sandbox)
            runtime = await self._create_or_patch_runtime_sandbox(sandbox)

            if runtime is not None:
                _set_if_present(
                    sandbox,
                    "status",
                    "running" if self._runtime_ready(runtime) else "starting",
                )
                _set_if_present(sandbox, "error_message", None)
                if getattr(sandbox, "started_at", None) is None:
                    _set_if_present(sandbox, "started_at", now)
                runtime_pod_name = self._runtime_pod_name(runtime)
                if runtime_pod_name:
                    _set_if_present(sandbox, "pod_name", runtime_pod_name)
            elif self.runtime_error:
                _set_if_present(sandbox, "status", "failed")
                _set_if_present(sandbox, "error_message", self.runtime_error)

            await session.commit()
            await session.refresh(sandbox)
            log.info(
                "sandbox_resumed",
                sandbox_id=str(sandbox_id),
                status=_status_of(sandbox),
                duration_ms=round((time.monotonic() - start) * 1000, 1),
            )
            return sandbox

    async def suspend(self, sandbox_id: UUID, *, organization_id: UUID | None = None) -> Any:
        """Suspend a sandbox by scaling runtime Sandbox CR down to zero."""
        start = time.monotonic()
        self._require_enabled()
        now = datetime.now(UTC).replace(tzinfo=None)

        async with self._session_factory() as session:
            sandbox = await self._get_sandbox(session, sandbox_id, organization_id)
            if sandbox is None:
                raise SandboxControllerError(f"Sandbox not found: {sandbox_id}")

            runtime_name = self._runtime_name_for(sandbox)
            await self._scale_runtime_sandbox(runtime_name, replicas=0)

            _set_if_present(sandbox, "status", "suspended")
            _set_if_present(sandbox, "stopped_at", now)
            await session.commit()
            await session.refresh(sandbox)
            log.info(
                "sandbox_suspended",
                sandbox_id=str(sandbox_id),
                org_id=str(organization_id),
                duration_ms=round((time.monotonic() - start) * 1000, 1),
            )
            return sandbox

    async def destroy(self, sandbox_id: UUID, *, organization_id: UUID | None = None) -> Any:
        """Destroy sandbox runtime and mark DB record as destroyed."""
        start = time.monotonic()
        self._require_enabled()
        now = datetime.now(UTC).replace(tzinfo=None)

        async with self._session_factory() as session:
            sandbox = await self._get_sandbox(session, sandbox_id, organization_id)
            if sandbox is None:
                raise SandboxControllerError(f"Sandbox not found: {sandbox_id}")

            _set_if_present(sandbox, "status", "terminating")
            await self._delete_runtime_sandbox(self._runtime_name_for(sandbox))

            _set_if_present(sandbox, "status", "deleted")
            _set_if_present(sandbox, "stopped_at", now)
            await session.commit()
            await session.refresh(sandbox)
            log.info(
                "sandbox_destroyed",
                sandbox_id=str(sandbox_id),
                org_id=str(organization_id),
                duration_ms=round((time.monotonic() - start) * 1000, 1),
            )
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

            if connected:
                _set_if_present(sandbox, "runner_id", runner_id or getattr(sandbox, "runner_id", None))
                _set_if_present(sandbox, "status", "running")
                _set_if_present(sandbox, "error_message", None)

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
            pod_name = getattr(sandbox, "pod_name", None) or self._runtime_name_for(sandbox)

        if not pod_name:
            raise SandboxControllerError(
                "Logs not available: sandbox has no associated runtime name yet"
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

    def _is_idle_expired(self, sandbox: Any, now: datetime) -> bool:
        """Check if sandbox has exceeded idle TTL based on last heartbeat or update."""
        last_activity = (
            getattr(sandbox, "last_heartbeat", None)
            or getattr(sandbox, "updated_at", None)
        )
        if last_activity is None:
            return False
        if hasattr(last_activity, "tzinfo") and last_activity.tzinfo is not None:
            last_activity = last_activity.replace(tzinfo=None)
        idle_seconds = (now - last_activity).total_seconds()
        return idle_seconds > self.idle_ttl_seconds

    def _is_lifetime_expired(self, sandbox: Any, now: datetime) -> bool:
        """Check if sandbox has exceeded max lifetime since startup."""
        started_at = getattr(sandbox, "started_at", None)
        if started_at is None:
            return False
        if hasattr(started_at, "tzinfo") and started_at.tzinfo is not None:
            started_at = started_at.replace(tzinfo=None)
        lifetime_seconds = (now - started_at).total_seconds()
        return lifetime_seconds > self.max_lifetime_seconds

    async def _auto_suspend(self, sandbox: Any, now: datetime, reason: str) -> None:
        """Suspend a sandbox due to idle/lifetime timeout."""
        await self._scale_runtime_sandbox(self._runtime_name_for(sandbox), replicas=0)
        _set_if_present(sandbox, "status", "suspended")
        _set_if_present(sandbox, "stopped_at", now)
        _set_if_present(sandbox, "error_message", f"auto-suspended: {reason}")

    async def _reconcile_sandbox(
        self, sandbox: Any, *, now: datetime, k8s_ready: bool
    ) -> None:
        """Reconcile a single sandbox against runtime state."""
        sandbox_id_str = str(getattr(sandbox, "id", ""))
        status = _status_of(sandbox)

        if status == "running":
            if self._is_lifetime_expired(sandbox, now):
                log.info("sandbox_max_lifetime_exceeded", sandbox_id=sandbox_id_str)
                await self._auto_suspend(sandbox, now, "max lifetime exceeded")
                return
            if self._is_idle_expired(sandbox, now):
                log.info("sandbox_idle_timeout", sandbox_id=sandbox_id_str)
                await self._auto_suspend(sandbox, now, "idle timeout")
                return

        if not k8s_ready:
            if status in {"pending", "starting"} and self.runtime_error:
                _set_if_present(sandbox, "status", "failed")
                _set_if_present(sandbox, "error_message", self.runtime_error)
            return

        runtime_name = self._runtime_name_for(sandbox)
        try:
            runtime = await self._read_runtime_sandbox(runtime_name)
        except Exception as e:
            log.warning(
                "sandbox_reconcile_runtime_read_failed",
                sandbox_id=sandbox_id_str,
                runtime_name=runtime_name,
                error=str(e),
            )
            return

        if runtime is None:
            if status in {"running", "starting", "pending"}:
                _set_if_present(sandbox, "status", "failed")
                _set_if_present(sandbox, "error_message", "runtime sandbox not found")
            return

        desired_replicas = self._runtime_spec_replicas(runtime)
        actual_replicas = self._runtime_status_replicas(runtime)
        ready = self._runtime_ready(runtime)
        runtime_pod_name = self._runtime_pod_name(runtime)
        if runtime_pod_name:
            _set_if_present(sandbox, "pod_name", runtime_pod_name)
        _set_if_present(sandbox, "namespace", self.namespace)
        if hasattr(sandbox, "context"):
            context = dict(getattr(sandbox, "context", {}) or {})
            context["agent_sandbox_name"] = runtime_name
            sandbox.context = context

        if desired_replicas == 0:
            _set_if_present(sandbox, "status", "suspended")
            return

        if actual_replicas == 0:
            _set_if_present(sandbox, "status", "starting")
            return

        if ready and actual_replicas >= 1:
            _set_if_present(sandbox, "status", "running")
            _set_if_present(sandbox, "error_message", None)
            if getattr(sandbox, "started_at", None) is None:
                _set_if_present(sandbox, "started_at", now)
            return

        _set_if_present(sandbox, "status", "starting")

    async def _reap_stale_tasks(self) -> None:
        """Reap stale dispatched/acked tasks via dispatcher (if wired)."""
        if self._dispatcher is None:
            return
        try:
            reaped = await self._dispatcher.reap_stale_tasks()
            if reaped:
                log.info("sandbox_reconcile_reaped_tasks", count=reaped)
        except Exception as e:
            log.warning("sandbox_reconcile_reap_failed", error=str(e))

    async def suspend_all(self, org_id: UUID) -> int:
        """Suspend all active sandboxes for an org. Returns count."""
        self._require_enabled()
        sandbox_model = self._sandbox_model()
        now = datetime.now(UTC).replace(tzinfo=None)
        count = 0

        async with self._session_factory() as session:
            stmt = select(sandbox_model).where(
                sandbox_model.organization_id == org_id,
                sandbox_model.status.in_(list(self.ACTIVE_STATUSES)),
            )
            result = await session.execute(stmt)
            sandboxes = result.scalars().all()

            for sandbox in sandboxes:
                await self._scale_runtime_sandbox(self._runtime_name_for(sandbox), replicas=0)
                _set_if_present(sandbox, "status", "suspended")
                _set_if_present(sandbox, "stopped_at", now)
                _set_if_present(sandbox, "error_message", "admin_rollback")
                count += 1

            await session.commit()

        if count:
            log.info("sandbox_suspend_all", org_id=str(org_id), count=count)
        return count

    async def find_orphaned_runtimes(self, org_id: UUID) -> list[str]:
        """Find runtime Sandbox CRs with no matching active DB record."""
        if not await self._ensure_k8s_client():
            return []

        sandbox_model = self._sandbox_model()
        async with self._session_factory() as session:
            stmt = select(sandbox_model).where(
                sandbox_model.organization_id == org_id,
                sandbox_model.status.in_(list(self.ACTIVE_STATUSES)),
            )
            result = await session.execute(stmt)
            active_runtime_names = {self._runtime_name_for(s) for s in result.scalars().all()}

        try:
            resources = await self._custom_api.list_namespaced_custom_object(
                group=self._SANDBOX_GROUP,
                version=self._SANDBOX_VERSION,
                namespace=self.namespace,
                plural=self._SANDBOX_PLURAL,
                label_selector=f"app=sibyl-sandbox,organization_id={org_id}",
            )
        except Exception as e:
            log.warning("sandbox_orphan_scan_failed", error=str(e))
            return []

        orphaned: list[str] = []
        for item in resources.get("items", []):
            metadata = item.get("metadata", {})
            name = metadata.get("name")
            if isinstance(name, str) and name and name not in active_runtime_names:
                orphaned.append(name)

        if orphaned:
            log.info("sandbox_orphaned_runtime_found", org_id=str(org_id), count=len(orphaned))
        return orphaned

    async def delete_orphaned_runtime(self, runtime_name: str) -> None:
        """Delete orphaned runtime sandbox by name."""
        await self._delete_runtime_sandbox(runtime_name)

    async def find_orphaned_pods(self, org_id: UUID) -> list[str]:
        """Backward-compatible alias for runtime orphan discovery."""
        return await self.find_orphaned_runtimes(org_id)

    async def delete_orphaned_pod(self, pod_name: str) -> None:
        """Backward-compatible alias for runtime orphan deletion."""
        await self.delete_orphaned_runtime(pod_name)

    async def _reconcile_once(self) -> None:
        if not self.enabled:
            return

        start = time.monotonic()
        sandbox_model = self._sandbox_model()
        tracked_statuses = list(self.ACTIVE_STATUSES | {"failed", "suspended"})
        now = datetime.now(UTC).replace(tzinfo=None)

        sandboxes: list[Any] = []
        async with self._session_factory() as session:
            stmt = select(sandbox_model).where(sandbox_model.status.in_(tracked_statuses))
            result = await session.execute(stmt)
            sandboxes = list(result.scalars().all())
            if not sandboxes:
                await self._reap_stale_tasks()
                duration_ms = (time.monotonic() - start) * 1000
                log.info(
                    "sandbox_reconcile_complete",
                    total=0,
                    suspended=0,
                    failed=0,
                    duration_ms=round(duration_ms, 1),
                )
                return

            k8s_ready = await self._ensure_k8s_client()
            for sandbox in sandboxes:
                await self._reconcile_sandbox(sandbox, now=now, k8s_ready=k8s_ready)
            await session.commit()

        await self._reap_stale_tasks()
        duration_ms = (time.monotonic() - start) * 1000
        suspended_count = sum(
            1 for s in sandboxes if str(getattr(s, "status", "")).lower() == "suspended"
        )
        failed_count = sum(
            1 for s in sandboxes if str(getattr(s, "status", "")).lower() == "failed"
        )
        log.info(
            "sandbox_reconcile_complete",
            total=len(sandboxes),
            suspended=suspended_count,
            failed=failed_count,
            duration_ms=round(duration_ms, 1),
        )

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
