"""Shared runtime service lifecycle for Sibyl ASGI applications."""

from __future__ import annotations

from typing import Any, Literal

from sibyl.config import settings


class RuntimeServices:
    """Start and stop the shared runtime services used by API and MCP apps."""

    def __init__(self, *, log: Any) -> None:
        self._log = log
        self._coordination_backend: Literal["local", "redis"] = (
            settings.resolved_coordination_backend
        )
        self._queue_backend = "unknown"
        self._broker_initialized = False
        self._scheduler_initialized = False
        self._pubsub_initialized = False
        self._locks_initialized = False

    async def startup(self) -> None:
        self._coordination_backend = settings.resolved_coordination_backend

        await bootstrap_surreal_runtime_schemas()
        await load_runtime_settings_from_db()
        install_llm_db_config_source()

        from sibyl.services.surreal_connectivity import initialize_shared_surreal_connectivity

        await initialize_shared_surreal_connectivity()
        await self._startup_broker()
        await self._startup_scheduler()
        await self._startup_pubsub()
        await self._startup_locks()
        await self._recover_stuck_sources()

    async def shutdown(self) -> None:
        await self._shutdown_scheduler()
        await self._shutdown_broker()
        await self._stop_surreal_connectivity()
        await self._close_shared_surreal_clients()
        await self._shutdown_pubsub()
        await self._shutdown_locks()

    async def _startup_broker(self) -> None:
        try:
            from sibyl.coordination.broker import get_broker, get_queue_backend

            self._queue_backend = get_queue_backend()
            await get_broker().startup()
            self._broker_initialized = True
            self._log.info(
                "Coordination broker ready",
                backend=self._coordination_backend,
                queue_backend=self._queue_backend,
            )
        except Exception as e:
            self._log.warning(
                "Coordination broker unavailable",
                backend=self._coordination_backend,
                queue_backend=self._queue_backend,
                error=str(e),
            )

    async def _startup_scheduler(self) -> None:
        try:
            from sibyl.coordination.scheduler import get_scheduler

            await get_scheduler().startup()
            self._scheduler_initialized = True
            self._log.info("Coordination scheduler ready", backend=self._coordination_backend)
        except Exception as e:
            self._log.warning(
                "Coordination scheduler unavailable",
                backend=self._coordination_backend,
                error=str(e),
            )

    async def _startup_pubsub(self) -> None:
        try:
            from sibyl.api.pubsub import init_pubsub
            from sibyl.api.websocket import enable_pubsub, local_broadcast

            await init_pubsub(local_broadcast)
            enable_pubsub()
            self._pubsub_initialized = True
            self._log.info("Coordination event bus ready", backend=self._coordination_backend)
        except Exception as e:
            self._log.warning(
                "Coordination event bus unavailable",
                backend=self._coordination_backend,
                error=str(e),
            )

    async def _startup_locks(self) -> None:
        try:
            from sibyl.locks import init_locks

            await init_locks()
            self._locks_initialized = True
            self._log.info("Coordination locks ready", backend=self._coordination_backend)
        except Exception as e:
            self._log.warning(
                "Coordination locks unavailable",
                backend=self._coordination_backend,
                error=str(e),
            )

    async def _recover_stuck_sources(self) -> None:
        try:
            from sibyl.api.routes.admin import recover_stuck_sources

            await recover_stuck_sources()
        except Exception as e:
            self._log.warning("Startup source recovery failed", error=str(e))

    async def _shutdown_scheduler(self) -> None:
        if not self._scheduler_initialized:
            return

        try:
            from sibyl.coordination.scheduler import get_scheduler

            await get_scheduler().shutdown()
        except Exception as e:
            self._log.debug("Scheduler shutdown error", error=str(e))

    async def _shutdown_broker(self) -> None:
        if not self._broker_initialized:
            return

        try:
            from sibyl.coordination.broker import get_broker

            await get_broker().shutdown()
        except Exception as e:
            self._log.debug("Broker shutdown error", error=str(e))

    async def _stop_surreal_connectivity(self) -> None:
        from sibyl.services.surreal_connectivity import stop_surreal_connectivity_monitor

        await stop_surreal_connectivity_monitor()

    async def _close_shared_surreal_clients(self) -> None:
        try:
            from sibyl.persistence.surreal.auth import close_shared_surreal_auth_client
            from sibyl.persistence.surreal.content import close_shared_surreal_content_client
            from sibyl_core.services.surreal_content import (
                close_shared_surreal_content_client as close_core_surreal_content_client,
            )

            await close_shared_surreal_auth_client()
            await close_shared_surreal_content_client()
            await close_core_surreal_content_client()
        except Exception as e:
            self._log.debug("Shared Surreal client shutdown error", error=str(e))

    async def _shutdown_pubsub(self) -> None:
        if not self._pubsub_initialized:
            return

        try:
            from sibyl.api.pubsub import shutdown_pubsub
            from sibyl.api.websocket import disable_pubsub

            disable_pubsub()
            await shutdown_pubsub()
        except Exception as e:
            self._log.debug("Pub/sub shutdown error", error=str(e))

    async def _shutdown_locks(self) -> None:
        if not self._locks_initialized:
            return

        try:
            from sibyl.locks import shutdown_locks

            await shutdown_locks()
        except Exception as e:
            self._log.debug("Lock shutdown error", error=str(e))


async def bootstrap_surreal_runtime_schemas() -> bool:
    from sibyl.surreal_runtime_startup import bootstrap_surreal_runtime_schemas as bootstrap

    return await bootstrap()


async def load_runtime_settings_from_db() -> list[str]:
    from sibyl.services.settings import load_runtime_settings_from_db as load_settings

    return await load_settings()


def install_llm_db_config_source() -> None:
    from sibyl.ai.llm.service import install_db_config_source

    install_db_config_source()
