"""E2E frontend smoke tests.

Basic smoke tests to verify frontend pages load.
For more complex browser automation, use Playwright.

These tests don't require the frontend to be running - they skip gracefully
if the frontend is unavailable.
"""

import os

import httpx
import pytest

FRONTEND_URL = os.getenv("SIBYL_FRONTEND_URL", "http://localhost:3337")


@pytest.mark.browser
class TestFrontendSmoke:
    """Smoke tests for frontend pages."""

    @pytest.fixture(autouse=True)
    def skip_if_no_frontend(self, frontend_available) -> None:
        """Skip tests if frontend is not running."""
        if not frontend_available:
            pytest.skip("Frontend not running on port 3337")

    def test_home_page(self) -> None:
        """Home page loads."""
        with httpx.Client(base_url=FRONTEND_URL, timeout=10.0, follow_redirects=True) as client:
            response = client.get("/")
            # 200 or redirect to login is fine
            assert response.status_code in (200, 301, 302, 307, 308)

    def test_tasks_page(self) -> None:
        """Tasks page loads."""
        with httpx.Client(base_url=FRONTEND_URL, timeout=10.0, follow_redirects=True) as client:
            response = client.get("/tasks")
            assert response.status_code in (200, 301, 302, 307, 308)

    def test_graph_page(self) -> None:
        """Graph page loads."""
        with httpx.Client(base_url=FRONTEND_URL, timeout=10.0, follow_redirects=True) as client:
            response = client.get("/graph")
            assert response.status_code in (200, 301, 302, 307, 308)

    def test_settings_page(self) -> None:
        """Settings page loads."""
        with httpx.Client(base_url=FRONTEND_URL, timeout=10.0, follow_redirects=True) as client:
            response = client.get("/settings")
            assert response.status_code in (200, 301, 302, 307, 308)

    def test_login_page(self) -> None:
        """Login page loads."""
        with httpx.Client(base_url=FRONTEND_URL, timeout=10.0, follow_redirects=True) as client:
            response = client.get("/login")
            assert response.status_code in (200, 301, 302, 307, 308)
