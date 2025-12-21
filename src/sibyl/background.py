"""Background task queue for async enrichment operations.

Handles embedding generation and knowledge graph enrichment without
blocking the main request/response cycle.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from sibyl.graph.entities import EntityManager

log = structlog.get_logger()


class TaskStatus(Enum):
    """Status of a background task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class BackgroundTask:
    """A background task to be executed."""

    id: str
    task_type: str
    payload: dict[str, Any]
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    error: str | None = None


class BackgroundQueue:
    """Simple async background task queue.

    Uses asyncio to process tasks without blocking the main thread.
    Tasks are fire-and-forget - results are logged but not returned.
    """

    def __init__(self, max_workers: int = 3) -> None:
        """Initialize the background queue.

        Args:
            max_workers: Maximum concurrent background tasks.
        """
        self._queue: asyncio.Queue[BackgroundTask] = asyncio.Queue()
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {}
        self._max_workers = max_workers
        self._workers: list[asyncio.Task[None]] = []
        self._running = False
        self._stats = {"queued": 0, "completed": 0, "failed": 0}

    def register_handler(
        self,
        task_type: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Register a handler for a task type.

        Args:
            task_type: Type identifier for the task.
            handler: Async function to handle tasks of this type.
        """
        self._handlers[task_type] = handler
        log.debug("Registered background handler", task_type=task_type)

    async def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        task_id: str | None = None,
    ) -> str:
        """Add a task to the background queue.

        Args:
            task_type: Type of task (must have registered handler).
            payload: Data to pass to the handler.
            task_id: Optional task ID (generated if not provided).

        Returns:
            Task ID for tracking.
        """
        import uuid

        if task_type not in self._handlers:
            log.warning("No handler for task type", task_type=task_type)
            return ""

        task_id = task_id or f"bg_{uuid.uuid4().hex[:12]}"
        task = BackgroundTask(id=task_id, task_type=task_type, payload=payload)

        await self._queue.put(task)
        self._stats["queued"] += 1

        log.debug(
            "Enqueued background task",
            task_id=task_id,
            task_type=task_type,
            queue_size=self._queue.qsize(),
        )

        return task_id

    async def _worker(self, worker_id: int) -> None:
        """Background worker that processes tasks from the queue."""
        log.debug("Background worker started", worker_id=worker_id)

        while self._running:
            try:
                # Wait for a task with timeout to allow graceful shutdown
                try:
                    task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except TimeoutError:
                    continue

                task.status = TaskStatus.RUNNING
                handler = self._handlers.get(task.task_type)

                if not handler:
                    log.warning("No handler for task", task_type=task.task_type)
                    task.status = TaskStatus.FAILED
                    task.error = f"No handler for task type: {task.task_type}"
                    self._stats["failed"] += 1
                    self._queue.task_done()
                    continue

                try:
                    await handler(task.payload)
                    task.status = TaskStatus.COMPLETED
                    task.completed_at = datetime.now(UTC)
                    self._stats["completed"] += 1
                    log.debug(
                        "Background task completed",
                        task_id=task.id,
                        task_type=task.task_type,
                    )
                except Exception as e:
                    task.status = TaskStatus.FAILED
                    task.error = str(e)
                    self._stats["failed"] += 1
                    log.exception(
                        "Background task failed",
                        task_id=task.id,
                        task_type=task.task_type,
                        error=str(e),
                    )

                self._queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Worker error", worker_id=worker_id, error=str(e))

        log.debug("Background worker stopped", worker_id=worker_id)

    async def start(self) -> None:
        """Start the background workers."""
        if self._running:
            return

        self._running = True
        self._workers = [
            asyncio.create_task(self._worker(i)) for i in range(self._max_workers)
        ]
        log.info("Background queue started", workers=self._max_workers)

    async def stop(self) -> None:
        """Stop the background workers gracefully."""
        if not self._running:
            return

        self._running = False

        # Wait for queue to drain (with timeout)
        try:
            await asyncio.wait_for(self._queue.join(), timeout=5.0)
        except TimeoutError:
            log.warning("Queue drain timeout, forcing shutdown")

        # Cancel workers
        for worker in self._workers:
            worker.cancel()

        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

        log.info("Background queue stopped", stats=self._stats)

    @property
    def stats(self) -> dict[str, int]:
        """Get queue statistics."""
        return {**self._stats, "pending": self._queue.qsize()}


# Global queue instance
_background_queue: BackgroundQueue | None = None


def get_background_queue() -> BackgroundQueue:
    """Get or create the global background queue."""
    global _background_queue
    if _background_queue is None:
        _background_queue = BackgroundQueue()
    return _background_queue


async def init_background_queue() -> BackgroundQueue:
    """Initialize and start the background queue with handlers."""
    queue = get_background_queue()

    # Register enrichment handlers
    queue.register_handler("enrich_entity", _handle_enrich_entity)
    queue.register_handler("generate_embeddings", _handle_generate_embeddings)

    await queue.start()
    return queue


async def shutdown_background_queue() -> None:
    """Shutdown the background queue."""
    global _background_queue
    if _background_queue is not None:
        await _background_queue.stop()
        _background_queue = None


# =============================================================================
# Background Task Handlers
# =============================================================================


async def _handle_enrich_entity(payload: dict[str, Any]) -> None:
    """Enrich an entity with embeddings and knowledge connections.

    Payload:
        entity_id: ID of the entity to enrich
        content: Text content to generate embeddings from
        find_related: Whether to find and link related knowledge
    """
    from sibyl.graph.client import get_graph_client
    from sibyl.graph.entities import EntityManager

    entity_id = payload["entity_id"]
    content = payload.get("content", "")
    title = payload.get("title", "")
    find_related = payload.get("find_related", True)

    log.info("Enriching entity", entity_id=entity_id, find_related=find_related)

    client = await get_graph_client()
    entity_manager = EntityManager(client)

    # Generate embedding for the entity
    combined_text = f"{title}\n{content}" if title else content

    if combined_text:
        embedding = await _generate_embedding(combined_text)
        if embedding:
            await entity_manager.update(entity_id, {"embedding": embedding})
            log.debug("Updated entity embedding", entity_id=entity_id)

    # Find and link related knowledge (optional)
    if find_related and combined_text:
        await _link_related_knowledge(entity_manager, entity_id, combined_text)


async def _handle_generate_embeddings(payload: dict[str, Any]) -> None:
    """Generate embeddings for an entity.

    Payload:
        entity_id: ID of the entity
        text: Text to embed
    """
    from sibyl.graph.client import get_graph_client
    from sibyl.graph.entities import EntityManager

    entity_id = payload["entity_id"]
    text = payload["text"]

    embedding = await _generate_embedding(text)
    if embedding:
        client = await get_graph_client()
        entity_manager = EntityManager(client)
        await entity_manager.update(entity_id, {"embedding": embedding})
        log.debug("Generated embedding", entity_id=entity_id)


async def _generate_embedding(text: str) -> list[float] | None:
    """Generate embedding using OpenAI.

    Args:
        text: Text to embed.

    Returns:
        Embedding vector or None on error.
    """
    import os

    try:
        from openai import AsyncOpenAI

        from sibyl.config import settings

        api_key = settings.openai_api_key.get_secret_value()
        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY", "")

        if not api_key:
            log.warning("No OpenAI API key for embeddings")
            return None

        client = AsyncOpenAI(api_key=api_key)
        response = await client.embeddings.create(
            model=settings.embedding_model,
            input=text[:8000],  # Truncate to avoid token limits
        )
        return response.data[0].embedding

    except Exception as e:
        log.exception("Embedding generation failed", error=str(e))
        return None


async def _link_related_knowledge(
    entity_manager: "EntityManager",  # noqa: ARG001
    entity_id: str,
    content: str,  # noqa: ARG001
    limit: int = 5,  # noqa: ARG001
) -> None:
    """Find and link related knowledge entities.

    Uses embedding similarity to find related patterns, rules, etc.
    Creates RELATES_TO edges to connect the new entity to existing knowledge.

    Note: This is a placeholder for future enhancement. Currently skipped
    as search_by_embedding is not yet implemented.
    """
    # TODO: Implement embedding-based similarity search
    # For now, we just generate embeddings - related knowledge linking
    # will be added when we implement vector search in FalkorDB
    log.debug(
        "Skipping related knowledge linking (not yet implemented)",
        entity_id=entity_id,
    )
