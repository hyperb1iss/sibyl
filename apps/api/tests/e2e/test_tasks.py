"""E2E tests for task workflow via CLI."""

import pytest


class TestTaskWorkflow:
    """Test full task lifecycle."""

    @pytest.fixture
    def project_id(self, cli, test_project_name) -> str:
        """Create a project for task tests."""
        result = cli.project_create(test_project_name)
        assert result.success
        return result.json().get("id")

    def test_task_create(self, cli, project_id, test_task_title) -> None:
        """Create a task via CLI."""
        result = cli.task_create(test_task_title, project_id, priority="high")
        assert result.success, f"Task create failed: {result.stderr}"

        data = result.json()
        assert data.get("name") == test_task_title
        assert "id" in data
        assert data.get("metadata", {}).get("priority") == "high"

    def test_task_list_by_status(self, cli, project_id, test_task_title) -> None:
        """Create task and list by status."""
        # Create
        create_result = cli.task_create(test_task_title, project_id)
        assert create_result.success

        # List todo tasks
        list_result = cli.task_list(status="todo")
        assert list_result.success

        tasks = list_result.json()
        assert isinstance(tasks, list)

    def test_task_start(self, cli, project_id, test_task_title) -> None:
        """Create and start a task."""
        # Create (sync=True by default ensures task exists before start)
        create_result = cli.task_create(test_task_title, project_id)
        assert create_result.success, f"Task create failed: {create_result.stderr}"
        task_id = create_result.json().get("id")

        # Start
        start_result = cli.task_start(task_id)
        assert start_result.success, f"Task start failed: {start_result.stdout}"

        data = start_result.json()
        assert data.get("success") is True
        assert data.get("data", {}).get("status") == "doing"

    def test_task_complete(self, cli, project_id, test_task_title) -> None:
        """Create, start, and complete a task."""
        # Create (sync=True by default ensures task exists)
        create_result = cli.task_create(test_task_title, project_id)
        assert create_result.success, f"Task create failed: {create_result.stderr}"
        task_id = create_result.json().get("id")

        # Start
        start_result = cli.task_start(task_id)
        assert start_result.success, f"Task start failed: {start_result.stdout}"

        # Complete
        complete_result = cli.task_complete(task_id, learnings="E2E test learning")
        assert complete_result.success, f"Task complete failed: {complete_result.stdout}"

        data = complete_result.json()
        assert data.get("success") is True
        assert data.get("data", {}).get("status") == "done"

    def test_task_full_lifecycle(self, cli, project_id, unique_id) -> None:
        """Full task lifecycle: create → start → complete."""
        task_title = f"Lifecycle Test {unique_id}"

        # Create (sync=True by default ensures immediate availability)
        create_result = cli.task_create(
            task_title, project_id, priority="medium", feature="testing"
        )
        assert create_result.success, f"Task create failed: {create_result.stderr}"
        task_id = create_result.json().get("id")

        # Verify task exists via show command
        show_result = cli.run("task", "show", task_id)
        assert show_result.success, f"Task show failed: {show_result.stdout}"

        # Start
        start_result = cli.task_start(task_id)
        assert start_result.success, f"Task start failed: {start_result.stdout}"

        # Verify status changed to doing
        show_result = cli.run("task", "show", task_id)
        assert show_result.is_json, f"Task show not JSON: {show_result.stdout}"
        task_data = show_result.json()
        status = task_data.get("metadata", {}).get("status") or task_data.get("status")
        assert status == "doing", f"Expected status 'doing', got '{status}'"

        # Complete
        complete_result = cli.task_complete(task_id, learnings="Full lifecycle test passed")
        assert complete_result.success, f"Task complete failed: {complete_result.stdout}"

        # Verify status changed to done
        show_result = cli.run("task", "show", task_id)
        assert show_result.is_json, f"Task show not JSON: {show_result.stdout}"
        task_data = show_result.json()
        status = task_data.get("metadata", {}).get("status") or task_data.get("status")
        assert status == "done", f"Expected status 'done', got '{status}'"
