"""Bootstrap Surreal runtime schemas during startup."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import structlog

from sibyl import config as config_module

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class SchemaBootstrapFailure:
    plane: Literal["auth", "content"]
    target_version: int
    error: str


@dataclass(frozen=True, slots=True)
class RuntimeSchemaBootstrapStatus:
    attempted: bool = False
    auth_ready: bool = False
    content_ready: bool = False
    failures: tuple[SchemaBootstrapFailure, ...] = ()

    @property
    def ready(self) -> bool:
        return self.attempted and self.auth_ready and self.content_ready and not self.failures


class RuntimeSchemaBootstrapError(RuntimeError):
    def __init__(self, status: RuntimeSchemaBootstrapStatus) -> None:
        self.status = status
        failures = ", ".join(
            f"{failure.plane} v{failure.target_version}: {failure.error}"
            for failure in status.failures
        )
        super().__init__(f"Surreal schema bootstrap failed: {failures}")


@dataclass(slots=True)
class _RuntimeSchemaBootstrapState:
    status: RuntimeSchemaBootstrapStatus = field(default_factory=RuntimeSchemaBootstrapStatus)


_schema_bootstrap_state = _RuntimeSchemaBootstrapState()


def get_runtime_schema_bootstrap_status() -> RuntimeSchemaBootstrapStatus:
    return _schema_bootstrap_state.status


def reset_runtime_schema_bootstrap_status() -> None:
    _schema_bootstrap_state.status = RuntimeSchemaBootstrapStatus()


async def bootstrap_surreal_auth_schema() -> None:
    from sibyl.persistence.surreal.auth import build_surreal_auth_client
    from sibyl_core.backends.surreal import bootstrap_auth_schema

    client = build_surreal_auth_client()
    try:
        await bootstrap_auth_schema(client)
    finally:
        await client.close()


async def bootstrap_surreal_content_schema() -> None:
    from sibyl.persistence.surreal.content import build_surreal_content_client
    from sibyl_core.backends.surreal import bootstrap_content_schema

    client = build_surreal_content_client()
    try:
        await bootstrap_content_schema(client)
    finally:
        await client.close()


async def bootstrap_surreal_runtime_schemas() -> bool:
    from sibyl_core.backends.surreal.auth_schema import AUTH_SCHEMA_CURRENT_VERSION
    from sibyl_core.backends.surreal.content_schema import CONTENT_SCHEMA_CURRENT_VERSION

    failures: list[SchemaBootstrapFailure] = []
    auth_ready = False
    content_ready = False

    try:
        log.info("Bootstrapping Surreal auth schema", target_version=AUTH_SCHEMA_CURRENT_VERSION)
        await bootstrap_surreal_auth_schema()
        auth_ready = True
    except Exception as exc:
        failures.append(
            SchemaBootstrapFailure(
                plane="auth",
                target_version=AUTH_SCHEMA_CURRENT_VERSION,
                error=str(exc),
            )
        )
        log.exception(
            "Surreal auth schema bootstrap failed",
            plane="auth",
            target_version=AUTH_SCHEMA_CURRENT_VERSION,
            error=str(exc),
            remediation="run `sibyld db init` to retry the pending migrations",
        )

    try:
        log.info(
            "Bootstrapping Surreal content schema",
            target_version=CONTENT_SCHEMA_CURRENT_VERSION,
        )
        await bootstrap_surreal_content_schema()
        content_ready = True
    except Exception as exc:
        failures.append(
            SchemaBootstrapFailure(
                plane="content",
                target_version=CONTENT_SCHEMA_CURRENT_VERSION,
                error=str(exc),
            )
        )
        log.exception(
            "Surreal content schema bootstrap failed",
            plane="content",
            target_version=CONTENT_SCHEMA_CURRENT_VERSION,
            error=str(exc),
            remediation="run `sibyld db init` to retry the pending migrations",
        )

    _schema_bootstrap_state.status = RuntimeSchemaBootstrapStatus(
        attempted=True,
        auth_ready=auth_ready,
        content_ready=content_ready,
        failures=tuple(failures),
    )
    if failures and config_module.settings.environment != "development":
        raise RuntimeSchemaBootstrapError(_schema_bootstrap_state.status)
    return _schema_bootstrap_state.status.ready


__all__ = [
    "RuntimeSchemaBootstrapError",
    "RuntimeSchemaBootstrapStatus",
    "SchemaBootstrapFailure",
    "bootstrap_surreal_auth_schema",
    "bootstrap_surreal_content_schema",
    "bootstrap_surreal_runtime_schemas",
    "get_runtime_schema_bootstrap_status",
    "reset_runtime_schema_bootstrap_status",
]
