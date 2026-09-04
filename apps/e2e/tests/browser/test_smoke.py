"""Production-shaped browser contracts for Sibyl's primary web workflows."""

import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect


def assert_path(page: Page, expected_path: str) -> None:
    """Fail with an explicit redirect error when navigation leaves the target path."""
    actual = urlparse(page.url)
    assert actual.path == expected_path, (
        f"Unexpected redirect while loading {expected_path}: browser reached {page.url}"
    )


def assert_cli_success(result, operation: str) -> dict | list:
    """Require a seed operation to succeed before asserting its browser surface."""
    assert result.success, f"{operation} failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    return result.json()


@pytest.mark.browser
class TestFrontendBrowserContracts:
    """Exercise the release-critical web surfaces in a real Chromium browser."""

    def test_unauthenticated_task_route_redirects_to_login(
        self,
        unauthenticated_page: Page,
    ) -> None:
        """Protected routes preserve the requested path in the login redirect."""
        unauthenticated_page.goto("/tasks", wait_until="domcontentloaded")

        actual = urlparse(unauthenticated_page.url)
        assert actual.path == "/login"
        assert parse_qs(actual.query) == {"next": ["/tasks"]}
        expect(
            unauthenticated_page.locator('form[action="/api/auth/local/login"]')
        ).to_be_visible()

    def test_authenticated_dashboard_renders(self, authenticated_page: Page) -> None:
        """An authenticated session renders dashboard content instead of login."""
        authenticated_page.goto("/", wait_until="domcontentloaded")
        assert_path(authenticated_page, "/")

        expect(authenticated_page.get_by_role("main")).to_be_visible()
        expect(authenticated_page.get_by_role("link", name="Dashboard", exact=True)).to_be_visible()
        expect(authenticated_page.get_by_role("main")).to_contain_text(
            re.compile(r"Welcome to Sibyl|memories")
        )

    def test_graph_renders_seeded_knowledge(
        self,
        authenticated_page: Page,
        cli,
        test_project_name: str,
        test_task_title: str,
    ) -> None:
        """The graph route renders a canvas backed by seeded graph records."""
        project = assert_cli_success(cli.project_create(test_project_name), "project seed")
        project_id = project.get("id") if isinstance(project, dict) else None
        assert project_id, f"Project seed did not return an id: {project!r}"
        assert_cli_success(
            cli.task_create(test_task_title, project_id, sync=True),
            "task seed",
        )

        authenticated_page.goto("/graph", wait_until="domcontentloaded")
        assert_path(authenticated_page, "/graph")

        expect(authenticated_page.locator("canvas")).to_be_visible(timeout=20_000)
        # Zoom drives the level of detail; the toolbar buttons jump between
        # levels. Which level a seeded graph lands on depends on whether it is
        # big enough to form domains, so the stable contract is that jumping
        # to entities leaves the domain map behind.
        entities_jump = authenticated_page.get_by_role("button", name="Entities", exact=True)
        domains_jump = authenticated_page.get_by_role("button", name="Domains", exact=True)
        entities_jump.click()
        expect(domains_jump).to_have_attribute("aria-pressed", "false")
        expect(authenticated_page.get_by_placeholder("Search nodes...")).to_be_visible()

    def test_global_search_renders_seeded_result(
        self,
        authenticated_page: Page,
        cli,
        unique_id: str,
    ) -> None:
        """The global search submits a query and renders the indexed result."""
        title = f"Browser Search Contract {unique_id}"
        query = f"chromatic-oracle-{unique_id}"
        assert_cli_success(
            cli.remember(
                title,
                f"Release browser search marker {query}",
                kind="note",
                wait_searchable=True,
                all_projects=True,
            ),
            "search seed",
        )

        authenticated_page.goto("/search", wait_until="networkidle")
        assert_path(authenticated_page, "/search")
        search_form = authenticated_page.get_by_role("main").locator("form").first
        search_form.get_by_label("Search", exact=True).fill(query)
        search_form.get_by_role("button", name="Search", exact=True).click()
        authenticated_page.wait_for_url(re.compile(r"/search\?q="))
        assert_path(authenticated_page, "/search")

        result_titles = authenticated_page.get_by_role("heading", name=title, exact=True)
        expect(result_titles.first).to_be_visible(timeout=20_000)
        assert result_titles.count() >= 1

    def test_task_can_be_created_and_opened(
        self,
        authenticated_page: Page,
        cli,
        test_project_name: str,
        test_task_title: str,
    ) -> None:
        """The task board creates a task and opens its rendered detail route."""
        assert_cli_success(cli.project_create(test_project_name), "task project seed")

        authenticated_page.goto("/tasks", wait_until="domcontentloaded")
        assert_path(authenticated_page, "/tasks")

        authenticated_page.get_by_role("button", name=re.compile(r"^New Task")).click()
        expect(authenticated_page.get_by_role("dialog", name="Quick Task")).to_be_visible()
        authenticated_page.get_by_label("Task title", exact=True).fill(test_task_title)
        authenticated_page.get_by_role("combobox", name="Project", exact=True).click()
        authenticated_page.get_by_role("option", name=test_project_name, exact=True).click()
        authenticated_page.get_by_role("button", name="Create Task", exact=True).click()

        expect(authenticated_page.get_by_text("Task created", exact=True)).to_be_visible()
        authenticated_page.reload(wait_until="networkidle")
        assert_path(authenticated_page, "/tasks")
        task_heading = authenticated_page.get_by_role(
            "heading",
            name=test_task_title,
            exact=True,
            level=4,
        )
        expect(task_heading).to_be_visible(timeout=20_000)
        task_heading.click()
        authenticated_page.wait_for_url(re.compile(r"/tasks/[^/?#]+$"))
        assert urlparse(authenticated_page.url).path.startswith("/tasks/")
        expect(authenticated_page.get_by_role("heading", name=test_task_title, exact=True)).to_be_visible()
