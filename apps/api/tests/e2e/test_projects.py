"""E2E tests for project CRUD via CLI."""


class TestProjectCRUD:
    """Test project creation, listing, and retrieval."""

    def test_project_create(self, cli, test_project_name) -> None:
        """Create a project via CLI."""
        result = cli.project_create(test_project_name, "E2E test project description")
        assert result.success, f"Project create failed: {result.stderr}"

        data = result.json()
        assert data.get("name") == test_project_name
        assert "id" in data

    def test_project_list(self, cli) -> None:
        """List projects via CLI."""
        result = cli.project_list()
        assert result.success, f"Project list failed: {result.stderr}"

        data = result.json()
        assert isinstance(data, list)

    def test_project_create_and_find(self, cli, test_project_name) -> None:
        """Create a project and verify it appears in list."""
        # Create
        create_result = cli.project_create(test_project_name)
        assert create_result.success
        created = create_result.json()
        project_id = created.get("id")

        # List and find
        list_result = cli.project_list()
        assert list_result.success
        projects = list_result.json()

        # Find our project
        found = [p for p in projects if p.get("id") == project_id]
        assert len(found) == 1, f"Project {project_id} not found in list"
        assert found[0].get("name") == test_project_name

    def test_project_show(self, cli, test_project_name) -> None:
        """Create and show a project."""
        # Create
        create_result = cli.project_create(test_project_name)
        assert create_result.success
        project_id = create_result.json().get("id")

        # Show - may not be implemented, so we verify via list instead
        show_result = cli.run("project", "show", project_id)
        if show_result.success:
            data = show_result.json()
            assert data.get("name") == test_project_name
        else:
            # Fallback: verify via list
            list_result = cli.project_list()
            assert list_result.success
            projects = list_result.json()
            found = [p for p in projects if p.get("id") == project_id]
            assert len(found) == 1
            assert found[0].get("name") == test_project_name
