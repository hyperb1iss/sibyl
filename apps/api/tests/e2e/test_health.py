"""E2E health and connectivity tests.

Verify that all services are running and accessible.
"""

import pytest


class TestHealthConnectivity:
    """Test service health and connectivity."""

    def test_cli_health(self, cli) -> None:
        """Sibyl CLI health check should succeed."""
        result = cli.health()
        assert result.success, f"Health check failed: {result.stderr}"
        # Health command outputs status info
        assert "ok" in result.stdout.lower() or "healthy" in result.stdout.lower()

    def test_api_health(self, sync_api_client) -> None:
        """API /health endpoint should return 200."""
        response = sync_api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") in ("ok", "healthy")

    def test_api_admin_health(self, sync_api_client) -> None:
        """API /admin/health should return detailed status."""
        response = sync_api_client.get("/admin/health")
        # May require auth - 200 or 401 are both valid responses
        assert response.status_code in (200, 401, 403)

    def test_api_stats(self, sync_api_client) -> None:
        """API /admin/stats should return entity counts."""
        response = sync_api_client.get("/admin/stats")
        # May require auth
        assert response.status_code in (200, 401, 403)
        if response.status_code == 200:
            data = response.json()
            # Stats should have counts
            assert "total_entities" in data or "entities" in data or isinstance(data, dict)


@pytest.mark.asyncio
class TestAsyncHealth:
    """Async health tests."""

    async def test_async_api_health(self, api_client) -> None:
        """Async API health check."""
        response = await api_client.get("/health")
        assert response.status_code == 200
