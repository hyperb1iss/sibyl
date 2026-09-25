"""A project-less MCP context pack is refused unless every project was asked for."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sibyl.mcp_tools import retrieval


async def test_context_pack_refuses_to_widen_without_a_project() -> None:
    with (
        patch.object(retrieval.mcp_context, "require_context", AsyncMock()) as require_context,
        patch.object(retrieval.mcp_context, "resolve_project_scope", AsyncMock()) as resolve_scope,
        pytest.raises(ValueError, match="all_projects=True"),
    ):
        await retrieval.compile_context_pack(
            goal="ship faster",
            intent="build",
            layer="recall",
            domain=None,
            project=None,
            agent_id=None,
            limit=24,
            include_related=True,
            related_limit=3,
        )

    # The credential check still runs first; the scope rule stops the read after it.
    require_context.assert_awaited_once()
    resolve_scope.assert_not_awaited()


async def test_context_pack_reads_every_project_only_on_purpose() -> None:
    class ProceededError(Exception):
        pass

    with (
        patch.object(
            retrieval.mcp_context, "require_context", AsyncMock(side_effect=ProceededError())
        ),
        pytest.raises(ProceededError),
    ):
        await retrieval.compile_context_pack(
            goal="ship faster",
            intent="build",
            layer="recall",
            domain=None,
            project=None,
            agent_id=None,
            limit=24,
            include_related=True,
            related_limit=3,
            all_projects=True,
        )


def test_context_tool_exposes_the_all_projects_opt_in() -> None:
    import inspect

    from sibyl.mcp_tools.retrieval import register_retrieval_tools

    registered: dict[str, object] = {}

    class FakeMcp:
        def tool(self, *_args, **_kwargs):
            def register(fn):
                registered[fn.__name__] = fn
                return fn

            return register

    register_retrieval_tools(FakeMcp())  # type: ignore[arg-type]
    parameters = inspect.signature(registered["context"]).parameters  # type: ignore[arg-type]
    assert parameters["all_projects"].default is False
    assert "all_projects" in (registered["context"].__doc__ or "")  # type: ignore[union-attr]
