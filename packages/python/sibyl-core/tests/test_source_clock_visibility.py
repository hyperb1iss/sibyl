"""Live source clocks stay internal while authored provenance remains visible."""

from copy import deepcopy
from dataclasses import replace

import pytest

from sibyl_core.memory_pipeline.source_lifecycle import public_memory_metadata
from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextPack,
    ContextRelatedItem,
    ContextSection,
)
from sibyl_core.tools.context import (
    _drop_retired_items,
    _item_from_result,
    context_pack_to_dict,
)
from sibyl_core.tools.responses import SearchResult


def test_source_clock_visibility_preserves_authored_metadata() -> None:
    metadata = {
        "correction_blockers": {"foreign-root": {"revision": 17, "blocking": False}},
        "source_bindings": {"known-source": 2},
        "source_validation_pending": False,
        "raw_source_ids": ["declared-source"],
        "authored": {"metadata": {"correction_blockers": "literal example"}},
    }
    before = deepcopy(metadata)
    public = public_memory_metadata(metadata)
    assert public == {
        key: value
        for key, value in before.items()
        if key not in {"correction_blockers", "source_bindings"}
    }
    assert metadata == before


def test_source_clock_visibility_filters_context_only_after_lifecycle_gate() -> None:
    metadata = {
        "correction_blockers": {"foreign-root": {"revision": 17, "blocking": False}},
        "source_bindings": {"known-source": 2},
        "source_validation_pending": False,
        "raw_source_ids": ["declared-source"],
        "authored": {"metadata": {"correction_blockers": "literal example"}},
    }
    before = deepcopy(metadata)
    result = SearchResult(
        id="derivative",
        type="pattern",
        name="Pattern",
        content="Body",
        score=1,
        metadata=metadata,
    )
    admitted = _item_from_result(result, ContextFacet.PRIOR_ART, audit=True)
    related = ContextRelatedItem(
        id="related",
        type="pattern",
        name="Related",
        relationship="supports",
        direction="outgoing",
        metadata=metadata,
    )
    admitted = replace(admitted, related=[related])
    blocked_metadata = {
        **metadata,
        "correction_blockers": {"foreign-root": {"revision": 18, "blocking": True}},
    }
    blocked = _item_from_result(
        replace(result, id="retired", metadata=blocked_metadata),
        ContextFacet.PRIOR_ART,
        audit=True,
    )
    assert blocked.metadata["correction_blockers"] == blocked_metadata["correction_blockers"]
    sections = _drop_retired_items(
        [ContextSection(facet=ContextFacet.PRIOR_ART, title="Prior art", items=[blocked, admitted])]
    )
    pack = ContextPack(
        goal="Use memory",
        intent=ContextIntent.BUILD,
        query="memory",
        domain=None,
        project=None,
        sections=sections,
        total_items=1,
    )
    payload = context_pack_to_dict(pack)
    assert [item["id"] for item in payload["sections"][0]["items"]] == ["derivative"]
    item = payload["sections"][0]["items"][0]
    for public in (item["metadata"], item["related"][0]["metadata"]):
        assert "correction_blockers" not in public
        assert "source_bindings" not in public
        assert public["source_validation_pending"] is False
        assert public["raw_source_ids"] == metadata["raw_source_ids"]
        assert public["authored"] == metadata["authored"]
    assert admitted.metadata["correction_blockers"] == metadata["correction_blockers"]
    assert metadata == before


@pytest.mark.parametrize("lane", ["active", "lean", "audit"])
@pytest.mark.parametrize("state", ["clear", "blocked", "pending", "reconcile_pending"])
def test_source_clock_visibility_keeps_admission_state_until_final_output(lane, state):
    from types import SimpleNamespace

    from sibyl_core.tools.context import _item_from_active_entity

    metadata = {
        "correction_blockers": {"foreign-root": {"revision": 17, "blocking": state == "blocked"}},
        "source_validation_pending": state == "pending",
        "lifecycle_reconciliation_pending": state == "reconcile_pending",
    }
    if lane == "active":
        item = _item_from_active_entity(
            SimpleNamespace(
                id="task",
                name="Task",
                entity_type="task",
                description="Task instructions",
                metadata=metadata,
                status="doing",
            )
        )
    else:
        item = _item_from_result(
            SearchResult(
                id="task",
                type="task",
                name="Task",
                content="Task instructions",
                score=1,
                metadata=metadata,
            ),
            ContextFacet.ACTIVE_WORK,
            audit=lane == "audit",
        )
    sections = _drop_retired_items(
        [ContextSection(facet=ContextFacet.ACTIVE_WORK, title="Active work", items=[item])]
    )
    admitted = [entry for section in sections for entry in section.items]
    assert [entry.id for entry in admitted] == (["task"] if state == "clear" else [])
    pack = ContextPack(
        goal="Work",
        intent=ContextIntent.BUILD,
        query="task",
        domain=None,
        project=None,
        sections=sections,
        total_items=len(admitted),
    )
    assert "correction_blockers" not in str(context_pack_to_dict(pack))
    assert "correction_blockers" in metadata
