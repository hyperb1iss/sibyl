"""Redis scheduler placeholder.

Redis mode keeps using arq cron jobs in the worker process for now.
"""

from __future__ import annotations


class RedisScheduler:
    """No-op scheduler while Redis mode stays on arq cron."""

    async def startup(self) -> None:
        """Redis scheduling already lives in the worker."""

    async def shutdown(self) -> None:
        """No scheduler state to tear down for Redis mode."""
