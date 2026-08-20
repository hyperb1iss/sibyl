"""Task and note client operations."""

from typing import Any


class ClientWorkMixin:
    """Task and note client operations."""

    async def start_task(
        self,
        task_id: str,
        assignee: str | None = None,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Start working on a task."""
        data: dict[str, Any] = {}
        if assignee:
            data["assignee"] = assignee
        if expected_revision is not None:
            data["expected_revision"] = expected_revision
        return await self._request("POST", f"/tasks/{task_id}/start", json=data or None)

    async def block_task(
        self,
        task_id: str,
        reason: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Block a task with a reason."""
        data: dict[str, Any] = {"reason": reason}
        if expected_revision is not None:
            data["expected_revision"] = expected_revision
        return await self._request("POST", f"/tasks/{task_id}/block", json=data)

    async def unblock_task(
        self,
        task_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Unblock a task."""
        data = {"expected_revision": expected_revision} if expected_revision is not None else None
        return await self._request("POST", f"/tasks/{task_id}/unblock", json=data)

    async def submit_review(
        self,
        task_id: str,
        pr_url: str | None = None,
        commit_shas: list[str] | None = None,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Submit a task for review."""
        data: dict[str, Any] = {}
        if pr_url:
            data["pr_url"] = pr_url
        if commit_shas:
            data["commit_shas"] = commit_shas
        if expected_revision is not None:
            data["expected_revision"] = expected_revision
        return await self._request("POST", f"/tasks/{task_id}/review", json=data or None)

    async def complete_task(
        self,
        task_id: str,
        actual_hours: float | None = None,
        learnings: str | None = None,
        *,
        cited_ids: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Complete a task."""
        data: dict[str, Any] = {}
        if actual_hours is not None:
            data["actual_hours"] = actual_hours
        if learnings:
            data["learnings"] = learnings
        if cited_ids:
            data["cited_ids"] = cited_ids
        if expected_revision is not None:
            data["expected_revision"] = expected_revision
        return await self._request("POST", f"/tasks/{task_id}/complete", json=data or None)

    async def archive_task(
        self,
        task_id: str,
        reason: str | None = None,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Archive a task."""
        data: dict[str, Any] = {}
        if reason:
            data["reason"] = reason
        if expected_revision is not None:
            data["expected_revision"] = expected_revision
        return await self._request("POST", f"/tasks/{task_id}/archive", json=data or None)

    async def create_task(
        self,
        title: str,
        project_id: str,
        description: str | None = None,
        priority: str = "medium",
        complexity: str = "medium",
        status: str = "todo",
        assignees: list[str] | None = None,
        epic_id: str | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        technologies: list[str] | None = None,
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a task via the dedicated POST /tasks endpoint.

        Uses the task-specific endpoint which handles BELONGS_TO relationships
        and DEPENDS_ON dependencies automatically.
        """
        data: dict[str, Any] = {
            "title": title,
            "project_id": project_id,
            "priority": priority,
            "complexity": complexity,
            "status": status,
        }
        if description:
            data["description"] = description
        if assignees:
            data["assignees"] = assignees
        if epic_id:
            data["epic_id"] = epic_id
        if feature:
            data["feature"] = feature
        if tags:
            data["tags"] = tags
        if technologies:
            data["technologies"] = technologies
        if depends_on:
            data["depends_on"] = depends_on

        return await self._request("POST", "/tasks", json=data)

    async def update_task(
        self,
        task_id: str,
        status: str | None = None,
        priority: str | None = None,
        complexity: str | None = None,
        title: str | None = None,
        description: str | None = None,
        assignees: list[str] | None = None,
        epic_id: str | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        technologies: list[str] | None = None,
        add_depends_on: list[str] | None = None,
        remove_depends_on: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Update task fields."""
        data: dict[str, Any] = {}
        if status:
            data["status"] = status
        if priority:
            data["priority"] = priority
        if complexity:
            data["complexity"] = complexity
        if title:
            data["title"] = title
        if description:
            data["description"] = description
        if assignees:
            data["assignees"] = assignees
        if epic_id:
            data["epic_id"] = epic_id
        if feature:
            data["feature"] = feature
        if tags:
            data["tags"] = tags
        if technologies:
            data["technologies"] = technologies
        if add_depends_on:
            data["add_depends_on"] = add_depends_on
        if remove_depends_on:
            data["remove_depends_on"] = remove_depends_on
        if expected_revision is not None:
            data["expected_revision"] = expected_revision

        return await self._request("PATCH", f"/tasks/{task_id}", json=data)

    async def create_note(
        self,
        task_id: str,
        content: str,
        author_type: str = "user",
        author_name: str = "",
    ) -> dict[str, Any]:
        """Create a note on a task."""
        data = {
            "content": content,
            "author_type": author_type,
            "author_name": author_name,
        }
        return await self._request("POST", f"/tasks/{task_id}/notes", json=data)

    async def list_notes(
        self,
        task_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List notes for a task."""
        params = {"limit": limit}
        return await self._request("GET", f"/tasks/{task_id}/notes", params=params)
