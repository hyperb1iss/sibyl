"""Memory, reflection, sharing, and synthesis client operations."""

from typing import Any
from urllib.parse import quote


class ClientMemoryMixin:
    """Memory, reflection, sharing, and synthesis client operations."""

    async def remember_raw_memory(
        self,
        *,
        title: str,
        raw_content: str,
        source_id: str | None = None,
        memory_scope: str = "private",
        scope_key: str | None = None,
        diary: bool = False,
        agent_id: str | None = None,
        project_id: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
        capture_surface: str = "cli",
    ) -> dict[str, Any]:
        """Store verbatim raw memory."""
        data: dict[str, Any] = {
            "title": title,
            "raw_content": raw_content,
            "memory_scope": memory_scope,
            "diary": diary,
            "tags": tags or [],
            "metadata": metadata or {},
            "provenance": provenance or {},
            "capture_surface": capture_surface,
        }
        if source_id:
            data["source_id"] = source_id
        if scope_key:
            data["scope_key"] = scope_key
        if agent_id:
            data["agent_id"] = agent_id
        if project_id:
            data["project_id"] = project_id
        return await self._request("POST", "/memory/raw", json=data)

    async def recall_raw_memory(
        self,
        *,
        query: str,
        memory_scope: str = "private",
        scope_key: str | None = None,
        diary: bool = False,
        agent_id: str | None = None,
        project_id: str | None = None,
        participants: list[str] | None = None,
        labels: list[str] | None = None,
        thread_id: str | None = None,
        occurred_after: str | None = None,
        occurred_before: str | None = None,
        as_of: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Recall verbatim raw memories."""
        data: dict[str, Any] = {
            "query": query,
            "memory_scope": memory_scope,
            "diary": diary,
            "limit": limit,
        }
        if scope_key:
            data["scope_key"] = scope_key
        if agent_id:
            data["agent_id"] = agent_id
        if project_id:
            data["project_id"] = project_id
        if participants:
            data["participants"] = participants
        if labels:
            data["labels"] = labels
        if thread_id:
            data["thread_id"] = thread_id
        if occurred_after:
            data["occurred_after"] = occurred_after
        if occurred_before:
            data["occurred_before"] = occurred_before
        if as_of:
            data["as_of"] = as_of
        return await self._request("POST", "/memory/raw/recall", json=data)

    async def memory_audit(
        self,
        *,
        action: str | None = None,
        actor_user_id: str | None = None,
        source_id: str | None = None,
        derived_id: str | None = None,
        memory_scope: str | None = None,
        project_id: str | None = None,
        policy_allowed: bool | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List memory audit events."""
        params: dict[str, Any] = {"limit": limit}
        if action:
            params["action"] = action
        if actor_user_id:
            params["actor_user_id"] = actor_user_id
        if source_id:
            params["source_id"] = source_id
        if derived_id:
            params["derived_id"] = derived_id
        if memory_scope:
            params["memory_scope"] = memory_scope
        if project_id:
            params["project_id"] = project_id
        if policy_allowed is not None:
            params["policy_allowed"] = policy_allowed
        return await self._request("GET", "/memory/audit", params=params)

    async def cite_memory(
        self,
        cited_ids: list[str],
        *,
        project_id: str | None = None,
        source_surface: str = "cli_cite",
        metadata: dict[str, Any] | None = None,
        misled: bool = False,
    ) -> dict[str, Any]:
        """Record cited memories as positive or misleading usage feedback."""
        data: dict[str, Any] = {
            "cited_ids": cited_ids,
            "source_surface": source_surface,
            "metadata": metadata or {},
            "misled": misled,
        }
        if project_id:
            data["project_id"] = project_id
        return await self._request("POST", "/memory/cite", json=data)

    async def memory_inspect(self, source_id: str) -> dict[str, Any]:
        """Inspect a raw memory source."""
        encoded_source_id = quote(source_id, safe="")
        return await self._request("GET", f"/memory/inspect/{encoded_source_id}")

    async def memory_blame(self, source_id: str) -> dict[str, Any]:
        """Inspect revisions, corrections, audits, and lineage for a raw memory."""
        encoded_source_id = quote(source_id, safe="")
        return await self._request("GET", f"/memory/blame/{encoded_source_id}")

    async def correct_memory(
        self,
        source_id: str,
        *,
        action: str,
        reason: str,
        replacement_source_id: str | None = None,
        duplicate_of_source_id: str | None = None,
        revised_content: str | None = None,
        expected_revision: int | None = None,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Preview or apply a correction to a raw memory source."""
        encoded_source_id = quote(source_id, safe="")
        suffix = "/preview" if preview else ""
        data: dict[str, Any] = {
            "action": action,
            "reason": reason,
            "replacement_source_id": replacement_source_id,
            "duplicate_of_source_id": duplicate_of_source_id,
            "revised_content": revised_content,
            "expected_revision": expected_revision,
            "metadata": {"command": "sibyl correct"},
        }
        return await self._request(
            "POST",
            f"/memory/inspect/{encoded_source_id}/corrections{suffix}",
            json=data,
        )

    async def preview_reflection_promotion(
        self,
        *,
        candidate_id: str,
        promote_to_scope: str | None = None,
        promote_to_scope_key: str | None = None,
        domain: str | None = None,
        project: str | None = None,
        related_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Preview reflection candidate promotion without mutating memory."""
        data: dict[str, Any] = {
            "candidate_id": candidate_id,
            "related_to": related_to or [],
        }
        if promote_to_scope:
            data["promote_to_scope"] = promote_to_scope
        if promote_to_scope_key:
            data["promote_to_scope_key"] = promote_to_scope_key
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        return await self._request("POST", "/memory/reflection/promote/preview", json=data)

    async def preview_memory_promotion(
        self,
        *,
        candidate_id: str,
        promote_to_scope: str | None = None,
        promote_to_scope_key: str | None = None,
        domain: str | None = None,
        project: str | None = None,
        related_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Preview promotion for a reflection candidate or raw memory."""
        data: dict[str, Any] = {
            "candidate_id": candidate_id,
            "related_to": related_to or [],
        }
        if promote_to_scope:
            data["promote_to_scope"] = promote_to_scope
        if promote_to_scope_key:
            data["promote_to_scope_key"] = promote_to_scope_key
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        return await self._request("POST", "/memory/promote/preview", json=data)

    async def promote_memory(
        self,
        *,
        candidate_id: str,
        promote_to_scope: str | None = None,
        promote_to_scope_key: str | None = None,
        domain: str | None = None,
        project: str | None = None,
        related_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Promote a reflection candidate or raw memory."""
        data: dict[str, Any] = {
            "candidate_id": candidate_id,
            "related_to": related_to or [],
        }
        if promote_to_scope:
            data["promote_to_scope"] = promote_to_scope
        if promote_to_scope_key:
            data["promote_to_scope_key"] = promote_to_scope_key
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        return await self._request("POST", "/memory/promote", json=data)

    async def auto_review_reflection_promotion(
        self,
        *,
        candidate_id: str,
        promote_to_scope: str | None = None,
        promote_to_scope_key: str | None = None,
        domain: str | None = None,
        project: str | None = None,
        related_to: list[str] | None = None,
        dry_run: bool = False,
        confidence_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Auto-review a reflection candidate and promote it when safe."""
        data: dict[str, Any] = {
            "candidate_id": candidate_id,
            "dry_run": dry_run,
            "related_to": related_to or [],
        }
        if promote_to_scope:
            data["promote_to_scope"] = promote_to_scope
        if promote_to_scope_key:
            data["promote_to_scope_key"] = promote_to_scope_key
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        if confidence_threshold is not None:
            data["confidence_threshold"] = confidence_threshold
        return await self._request("POST", "/memory/reflection/review/auto", json=data)

    async def drain_reflection_review(
        self,
        *,
        dry_run: bool = True,
        limit: int = 50,
        promote_to_scope: str | None = None,
        promote_to_scope_key: str | None = None,
        domain: str | None = None,
        project: str | None = None,
        related_to: list[str] | None = None,
        confidence_threshold: float | None = None,
        archive_exceptions: bool = False,
        archive_exception_reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        """Drain pending reflection candidates through automatic review."""
        data: dict[str, Any] = {
            "dry_run": dry_run,
            "limit": limit,
            "related_to": related_to or [],
            "archive_exceptions": archive_exceptions,
            "archive_exception_reasons": archive_exception_reasons
            or ["duplicate_candidate", "stale_candidate"],
        }
        if promote_to_scope:
            data["promote_to_scope"] = promote_to_scope
        if promote_to_scope_key:
            data["promote_to_scope_key"] = promote_to_scope_key
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        if confidence_threshold is not None:
            data["confidence_threshold"] = confidence_threshold
        return await self._request("POST", "/memory/reflection/review/drain", json=data)

    async def enqueue_reflection_dream_cycle(
        self,
        *,
        dry_run: bool = True,
        source_limit: int = 20,
        candidate_limit: int = 50,
        archive_exceptions: bool = True,
    ) -> dict[str, Any]:
        """Queue an org-scoped automatic reflection maintenance run."""
        return await self._request(
            "POST",
            "/jobs/reflection-dream",
            params={
                "dry_run": dry_run,
                "source_limit": source_limit,
                "candidate_limit": candidate_limit,
                "archive_exceptions": archive_exceptions,
            },
        )

    async def preview_memory_share(
        self,
        *,
        source_ids: list[str],
        target_scope: str,
        target_scope_key: str | None = None,
        recipient_organization_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Preview memory sharing without mutating memory."""
        data: dict[str, Any] = {
            "source_ids": source_ids,
            "target_scope": target_scope,
        }
        if target_scope_key:
            data["target_scope_key"] = target_scope_key
        if recipient_organization_id:
            data["recipient_organization_id"] = recipient_organization_id
        if project_id:
            data["project_id"] = project_id
        return await self._request("POST", "/memory/share/preview", json=data)

    async def share_memory(
        self,
        *,
        source_ids: list[str],
        target_scope: str,
        target_scope_key: str | None = None,
        recipient_organization_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply memory sharing through promotion-backed writes."""
        data: dict[str, Any] = {
            "source_ids": source_ids,
            "target_scope": target_scope,
        }
        if target_scope_key:
            data["target_scope_key"] = target_scope_key
        if recipient_organization_id:
            data["recipient_organization_id"] = recipient_organization_id
        if project_id:
            data["project_id"] = project_id
        return await self._request("POST", "/memory/share", json=data)

    def _synthesis_payload(
        self,
        *,
        goal: str,
        output_type: str = "documentation",
        audience: str | None = None,
        depth: str = "standard",
        seed_query: str | None = None,
        project: str | None = None,
        domain: str | None = None,
        entity_ids: list[str] | None = None,
        decision_ids: list[str] | None = None,
        task_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        required_sections: list[dict[str, Any]] | None = None,
        constraints: list[str] | None = None,
        max_sections: int = 6,
        include_neighborhoods: bool = True,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "goal": goal,
            "output_type": output_type,
            "depth": depth,
            "entity_ids": entity_ids or [],
            "decision_ids": decision_ids or [],
            "task_ids": task_ids or [],
            "artifact_ids": artifact_ids or [],
            "required_sections": required_sections or [],
            "constraints": constraints or [],
            "max_sections": max_sections,
            "include_neighborhoods": include_neighborhoods,
        }
        if audience:
            data["audience"] = audience
        if seed_query:
            data["seed_query"] = seed_query
        if project:
            data["project"] = project
        if domain:
            data["domain"] = domain
        return data

    async def synthesis_plan(
        self,
        *,
        goal: str,
        output_type: str = "documentation",
        audience: str | None = None,
        depth: str = "standard",
        seed_query: str | None = None,
        project: str | None = None,
        domain: str | None = None,
        entity_ids: list[str] | None = None,
        decision_ids: list[str] | None = None,
        task_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        required_sections: list[dict[str, Any]] | None = None,
        constraints: list[str] | None = None,
        max_sections: int = 6,
        include_neighborhoods: bool = True,
    ) -> dict[str, Any]:
        """Plan source-grounded synthesis through the API."""
        data = self._synthesis_payload(
            goal=goal,
            output_type=output_type,
            audience=audience,
            depth=depth,
            seed_query=seed_query,
            project=project,
            domain=domain,
            entity_ids=entity_ids,
            decision_ids=decision_ids,
            task_ids=task_ids,
            artifact_ids=artifact_ids,
            required_sections=required_sections,
            constraints=constraints,
            max_sections=max_sections,
            include_neighborhoods=include_neighborhoods,
        )
        return await self._request("POST", "/synthesis/plan", json=data)

    async def synthesis_draft(
        self,
        *,
        goal: str,
        output_type: str = "documentation",
        audience: str | None = None,
        depth: str = "standard",
        seed_query: str | None = None,
        project: str | None = None,
        domain: str | None = None,
        entity_ids: list[str] | None = None,
        decision_ids: list[str] | None = None,
        task_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        required_sections: list[dict[str, Any]] | None = None,
        constraints: list[str] | None = None,
        max_sections: int = 6,
        include_neighborhoods: bool = True,
        output_format: str = "markdown",
        remember: bool = False,
        memory_scope: str = "private",
        scope_key: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Draft and optionally remember source-grounded synthesis."""
        data = self._synthesis_payload(
            goal=goal,
            output_type=output_type,
            audience=audience,
            depth=depth,
            seed_query=seed_query,
            project=project,
            domain=domain,
            entity_ids=entity_ids,
            decision_ids=decision_ids,
            task_ids=task_ids,
            artifact_ids=artifact_ids,
            required_sections=required_sections,
            constraints=constraints,
            max_sections=max_sections,
            include_neighborhoods=include_neighborhoods,
        )
        data.update(
            {
                "output_format": output_format,
                "remember": remember,
                "memory_scope": memory_scope,
                "tags": tags or [],
            }
        )
        if scope_key:
            data["scope_key"] = scope_key
        return await self._request("POST", "/synthesis/draft", json=data)

    async def synthesis_handbook(self, project: str) -> dict[str, Any]:
        """Compose the distilled handbook for one project."""
        return await self._request("GET", "/synthesis/handbook", params={"project": project})

    async def context_pack(
        self,
        goal: str,
        intent: str = "build",
        layer: str = "recall",
        domain: str | None = None,
        project: str | None = None,
        agent_id: str | None = None,
        limit: int = 24,
        include_related: bool = True,
        related_limit: int = 3,
        audit: bool = False,
        markdown_token_budget: int | None = None,
    ) -> dict[str, Any]:
        """Compile a structured context pack for an agent goal."""
        data: dict[str, Any] = {
            "goal": goal,
            "intent": intent,
            "layer": layer,
            "limit": limit,
            "include_related": include_related,
            "related_limit": related_limit,
            "audit": audit,
        }
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        if agent_id:
            data["agent_id"] = agent_id
        if markdown_token_budget is not None:
            data["markdown_token_budget"] = markdown_token_budget
        return await self._request("POST", "/context/pack", json=data)

    async def reflect(
        self,
        content: str,
        source_title: str = "Session reflection",
        intent: str = "general",
        domain: str | None = None,
        project: str | None = None,
        related_to: list[str] | None = None,
        persist: bool = False,
        persist_source: bool = True,
        persist_review: bool = False,
        limit: int = 12,
        *,
        cited_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Reflect raw notes into durable memory candidates."""
        data: dict[str, Any] = {
            "content": content,
            "source_title": source_title,
            "intent": intent,
            "persist": persist,
            "persist_source": persist_source,
            "persist_review": persist_review,
            "limit": limit,
        }
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        if related_to:
            data["related_to"] = related_to
        if cited_ids:
            data["cited_ids"] = cited_ids
        return await self._request("POST", "/context/reflect", json=data)
