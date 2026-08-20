"""Administrative health, statistics, and job client operations."""

from typing import Any


class ClientAdminMixin:
    """Administrative health, statistics, and job client operations."""

    async def list_jobs(
        self,
        *,
        function: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """List background jobs visible to the active organization."""
        params: dict[str, Any] = {"limit": limit}
        if function:
            params["function"] = function
        return await self._request("GET", "/jobs", params=params)

    async def health(self) -> dict[str, Any]:
        """Get server health status."""
        return await self._request("GET", "/admin/health")

    async def stats(self) -> dict[str, Any]:
        """Get knowledge graph statistics."""
        return await self._request("GET", "/admin/stats")
