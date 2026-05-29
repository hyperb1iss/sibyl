"""Bootstrap Surreal runtime schemas during startup."""

from __future__ import annotations

import structlog

log = structlog.get_logger()


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
    bootstrap_auth = True

    bootstrapped = False
    if bootstrap_auth:
        try:
            log.info("Bootstrapping Surreal auth schema")
            await bootstrap_surreal_auth_schema()
            bootstrapped = True
        except Exception as exc:
            log.warning("Surreal auth schema bootstrap failed", error=str(exc))

    try:
        log.info("Bootstrapping Surreal content schema")
        await bootstrap_surreal_content_schema()
        bootstrapped = True
    except Exception as exc:
        log.warning("Surreal content schema bootstrap failed", error=str(exc))

    return bootstrapped


__all__ = [
    "bootstrap_surreal_auth_schema",
    "bootstrap_surreal_content_schema",
    "bootstrap_surreal_runtime_schemas",
]
