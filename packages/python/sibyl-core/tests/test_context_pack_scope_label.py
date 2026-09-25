"""A context pack says whether it read one project or every accessible one."""

from __future__ import annotations

from sibyl_core.models.context import ContextIntent, ContextPack
from sibyl_core.tools.context import context_pack_scope, context_pack_to_dict
from sibyl_core.tools.context_rendering import render_context_pack


def _pack(project: str | None) -> ContextPack:
    return ContextPack(
        goal="ship faster",
        intent=ContextIntent.BUILD,
        query="ship faster",
        domain=None,
        project=project,
        sections=[],
        total_items=0,
    )


def test_a_scoped_pack_names_its_project_everywhere() -> None:
    pack = _pack("project_123")

    assert context_pack_scope(pack) == "project"
    assert context_pack_to_dict(pack)["scope"] == "project"
    markdown = render_context_pack(pack).markdown
    assert "Project: project_123" in markdown
    assert "all accessible projects" not in markdown


def test_a_cross_project_pack_is_labelled_in_json_and_markdown() -> None:
    pack = _pack(None)

    assert context_pack_scope(pack) == "all_projects"
    assert context_pack_to_dict(pack)["scope"] == "all_projects"
    assert "Scope: all accessible projects" in render_context_pack(pack).markdown
