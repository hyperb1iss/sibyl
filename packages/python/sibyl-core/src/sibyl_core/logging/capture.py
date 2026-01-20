"""Log capture system for developer introspection.

Captures log entries to an in-memory ring buffer that can be queried
via REST API, WebSocket, or MCP tools.

Usage:
    from sibyl_core.logging.capture import LogBuffer

    # Get recent logs
    entries = LogBuffer.get().tail(n=50, service="worker")

    # Subscribe to real-time log stream
    queue = LogBuffer.get().subscribe()
    try:
        while True:
            entry = await queue.get()
            print(entry)
    finally:
        LogBuffer.get().unsubscribe(queue)
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from structlog.typing import EventDict, WrappedLogger


@dataclass(frozen=True)
class LogEntry:
    """A captured log entry."""

    timestamp: datetime
    service: str
    level: str
    event: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "service": self.service,
            "level": self.level,
            "event": self.event,
            "context": self.context,
        }


class LogBuffer:
    """Thread-safe ring buffer for captured log entries.

    Singleton pattern - use LogBuffer.get() to access the shared instance.
    """

    _instance: ClassVar[LogBuffer | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, max_size: int = 1000) -> None:
        """Initialize the buffer.

        Args:
            max_size: Maximum entries to retain (oldest dropped when full)
        """
        self._buffer: deque[LogEntry] = deque(maxlen=max_size)
        self._buffer_lock = threading.Lock()
        self._subscribers: list[asyncio.Queue[LogEntry]] = []
        self._subscribers_lock = threading.Lock()

    @classmethod
    def get(cls, max_size: int = 1000) -> LogBuffer:
        """Get the singleton buffer instance.

        Args:
            max_size: Buffer size (only used on first call)

        Returns:
            The shared LogBuffer instance
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(max_size=max_size)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance (for testing)."""
        with cls._lock:
            cls._instance = None

    def append(self, entry: LogEntry) -> None:
        """Add an entry to the buffer and notify subscribers.

        Args:
            entry: The log entry to add
        """
        with self._buffer_lock:
            self._buffer.append(entry)

        # Notify async subscribers (non-blocking)
        with self._subscribers_lock:
            for queue in self._subscribers:
                try:
                    queue.put_nowait(entry)
                except asyncio.QueueFull:
                    # Drop if subscriber is slow - they'll catch up via tail()
                    pass

    def tail(
        self,
        n: int = 50,
        service: str | None = None,
        level: str | None = None,
    ) -> list[LogEntry]:
        """Get the most recent log entries.

        Args:
            n: Maximum entries to return
            service: Filter by service name
            level: Filter by log level

        Returns:
            List of matching entries (newest last)
        """
        with self._buffer_lock:
            entries = list(self._buffer)

        # Apply filters
        if service:
            entries = [e for e in entries if e.service == service]
        if level:
            entries = [e for e in entries if e.level.lower() == level.lower()]

        return entries[-n:]

    def clear(self) -> None:
        """Clear all buffered entries."""
        with self._buffer_lock:
            self._buffer.clear()

    def subscribe(self, max_queue_size: int = 100) -> asyncio.Queue[LogEntry]:
        """Subscribe to real-time log entries.

        Args:
            max_queue_size: Max entries to queue before dropping

        Returns:
            An asyncio Queue that receives new entries
        """
        queue: asyncio.Queue[LogEntry] = asyncio.Queue(maxsize=max_queue_size)
        with self._subscribers_lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[LogEntry]) -> None:
        """Unsubscribe from real-time log entries.

        Args:
            queue: The queue returned by subscribe()
        """
        with self._subscribers_lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass  # Already removed

    @property
    def size(self) -> int:
        """Current number of entries in the buffer."""
        with self._buffer_lock:
            return len(self._buffer)

    @property
    def subscriber_count(self) -> int:
        """Current number of active subscribers."""
        with self._subscribers_lock:
            return len(self._subscribers)


def capture_processor(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Structlog processor that captures entries to the ring buffer.

    This processor should be added to the structlog pipeline before the
    renderer. It captures log data without modifying the event dict.

    Args:
        logger: The wrapped logger (unused)
        method_name: Log level name (info, error, etc.)
        event_dict: The event dictionary being logged

    Returns:
        The unmodified event_dict (pass-through)
    """
    # Extract service name from event dict or use default
    service = event_dict.get("_service", "unknown")

    # Build context from remaining keys (exclude internal/handled keys)
    excluded_keys = {"event", "_service", "timestamp", "level", "exc_info"}
    context = {k: v for k, v in event_dict.items() if k not in excluded_keys}

    # Create and capture the entry
    entry = LogEntry(
        timestamp=datetime.now(),
        service=str(service),
        level=method_name,
        event=str(event_dict.get("event", "")),
        context=context,
    )
    LogBuffer.get().append(entry)

    return event_dict
