"""E2E tests for API endpoints."""

import pytest


class TestPublicEndpoints:
    """Test endpoints that don't require authentication."""

    def test_health_endpoint(self, sync_api_client) -> None:
        """GET /health returns healthy status without auth."""
        response = sync_api_client.get("/health")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "healthy"


class TestAuthRequired:
    """Verify endpoints require authentication."""

    def test_search_requires_auth(self, sync_api_client) -> None:
        """POST /search returns 401 without auth."""
        response = sync_api_client.post("/search", json={"query": "test"})
        assert response.status_code == 401

    def test_entities_requires_auth(self, sync_api_client) -> None:
        """GET /entities returns 401 without auth."""
        response = sync_api_client.get("/entities")
        assert response.status_code == 401

    def test_admin_stats_requires_auth(self, sync_api_client) -> None:
        """GET /admin/stats returns 401 without auth."""
        response = sync_api_client.get("/admin/stats")
        assert response.status_code == 401

    def test_explore_requires_auth(self, sync_api_client) -> None:
        """POST /search/explore returns 401 without auth."""
        response = sync_api_client.post("/search/explore", json={"mode": "list"})
        assert response.status_code == 401


class TestAuthenticatedEndpoints:
    """Test endpoints with authentication."""

    def test_search_with_auth(self, sync_auth_client) -> None:
        """POST /search works with auth."""
        response = sync_auth_client.post("/search", json={"query": "test", "limit": 5})
        assert response.status_code == 200

        data = response.json()
        assert "results" in data or isinstance(data, list)

    def test_entities_list(self, sync_auth_client) -> None:
        """GET /entities returns entity list with auth."""
        response = sync_auth_client.get("/entities")
        assert response.status_code == 200

        data = response.json()
        assert "entities" in data or isinstance(data, list)

    def test_entities_with_type_filter(self, sync_auth_client) -> None:
        """GET /entities?entity_type=pattern filters correctly."""
        response = sync_auth_client.get("/entities", params={"entity_type": "pattern"})
        assert response.status_code == 200

        data = response.json()
        entities = data.get("entities", data)
        for entity in entities:
            if "entity_type" in entity:
                assert entity["entity_type"] == "pattern"

    def test_admin_stats(self, sync_auth_client) -> None:
        """GET /admin/stats returns statistics with auth."""
        response = sync_auth_client.get("/admin/stats")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, dict)

    def test_explore_list_mode(self, sync_auth_client) -> None:
        """POST /search/explore list mode works with auth."""
        response = sync_auth_client.post(
            "/search/explore",
            json={"mode": "list", "types": ["pattern"], "limit": 10},
        )
        assert response.status_code == 200

        data = response.json()
        assert "entities" in data or "results" in data or isinstance(data, list)


@pytest.mark.asyncio
class TestAsyncAuthenticatedAPI:
    """Async API tests with authentication."""

    async def test_async_search(self, auth_api_client) -> None:
        """Async POST /search works with auth."""
        response = await auth_api_client.post(
            "/search", json={"query": "authentication", "limit": 3}
        )
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, (dict, list))

    async def test_async_entities(self, auth_api_client) -> None:
        """Async GET /entities works with auth."""
        response = await auth_api_client.get("/entities")
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, (dict, list))
