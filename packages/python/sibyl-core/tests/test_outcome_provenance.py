from __future__ import annotations

import json

import pytest

from sibyl_core.models import EntityType
from sibyl_core.models.experience import (
    OperationalEvidencePart,
    OperationalExperience,
    OperationalObservation,
)
from sibyl_core.projection.experience import project_operational_experience
from sibyl_core.projection.outcome import (
    OUTCOME_METADATA_KEY,
    outcome_context,
    outcome_provenance,
)


def failed_search(*, outcome: str | None = "failure") -> OperationalExperience:
    return OperationalExperience(
        source_id="forum-author-search",
        goal="Find submissions by an author in a forum",
        outcome=outcome,
        metadata={OUTCOME_METADATA_KEY: outcome_provenance("success")},
        observations=(
            OperationalObservation(
                id="forums",
                ordinal=0,
                uri="https://forum.test/forums/all",
                evidence=(
                    OperationalEvidencePart(
                        id="tree",
                        content="RootWebArea 'Forums'\n\t[1] searchbox 'Search query'",
                        content_type="text/plain; profile=accessibility-tree",
                    ),
                ),
            ),
            OperationalObservation(
                id="search",
                ordinal=1,
                uri="https://forum.test/search?q=author%3Aexample",
                action="send_msg_to_user('Task complete: found and downvoted the submission.')",
                reasoning="The search has no results, so there are no more posts.",
                evidence=(
                    OperationalEvidencePart(
                        id="tree",
                        content="RootWebArea 'Search'\n\t[2] heading 'No results for author:example'",
                        content_type="text/plain; profile=accessibility-tree",
                    ),
                ),
            ),
        ),
    )


@pytest.mark.parametrize("value", ["failure", "success", "unknown", None, "", "partially complete"])
def test_every_projected_fragment_retains_source_outcome(value):
    experience = failed_search(outcome=value)
    original = experience.model_dump(mode="json")
    projection = project_operational_experience(experience)
    expected = outcome_provenance(value)
    header = outcome_context(projection.entities[0].metadata)
    assert header is not None
    assert {e.entity_type for e in projection.entities} >= {
        EntityType.SESSION,
        EntityType.EVENT,
        EntityType.PASSAGE,
        EntityType.PROCEDURE,
    }
    for entity in projection.entities:
        assert entity.metadata[OUTCOME_METADATA_KEY] == expected
        if entity.entity_type == EntityType.PASSAGE:
            assert header.split("\n", 1)[0] in entity.description
        elif entity.entity_type != EntityType.ARTIFACT:
            assert header in entity.content
    assert all(r.metadata[OUTCOME_METADATA_KEY] == expected for r in projection.relationships)
    assert experience.model_dump(mode="json") == original


def test_failed_completion_claim_stays_a_claim_on_its_actual_page():
    projection = project_operational_experience(failed_search())
    event = next(e for e in projection.entities if e.entity_type == EntityType.EVENT)
    assert 'Source-reported trajectory outcome: "failure"' in event.content
    assert "Actions and reasoning are agent reports" in event.content
    assert "does not establish success of each action" in event.content
    assert "Task complete: found and downvoted" in event.content
    assert "No results for author:example" in event.content
    assert "Before URI: https://forum.test/forums/all" in event.content
    assert "After URI: https://forum.test/search?q=author%3Aexample" in event.content
    assert "syntax is unsupported" not in event.content
    assert any(e.entity_type == EntityType.ERROR_PATTERN for e in projection.entities)


def test_every_procedure_segment_retains_outcome_without_changing_support():
    base = failed_search()
    observations = tuple(
        base.observations[1].model_copy(
            update={"id": f"step-{i}", "ordinal": i, "action": "click('" + "x" * 1_000 + "')"}
        )
        for i in range(50)
    )
    projection = project_operational_experience(
        base.model_copy(update={"observations": observations})
    )
    procedures = [e for e in projection.entities if e.entity_type == EntityType.PROCEDURE]
    assert len(procedures) > 1
    assert all('Source-reported trajectory outcome: "failure"' in e.content for e in procedures)
    assert all(
        len(e.content) <= 18_000 for e in projection.entities if e.entity_type != EntityType.SESSION
    )
    assert any(
        r.metadata.get("source_observation_id") == "step-49" for r in projection.relationships
    )


def test_long_outcome_is_retained_exactly_without_invented_shortened_claim():
    value = "failure\n" + "unverified details " * 900
    projection = project_operational_experience(failed_search(outcome=value))
    for entity in projection.entities:
        assert entity.metadata[OUTCOME_METADATA_KEY]["value"] == value
        if entity.entity_type != EntityType.ARTIFACT:
            assert (
                "full value in operational_outcome metadata" in entity.content + entity.description
            )
    assert (
        value
        == json.loads(json.dumps(projection.entities[0].metadata))[OUTCOME_METADATA_KEY]["value"]
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"reported_outcome": "success"},
        {OUTCOME_METADATA_KEY: {"value": "success"}},
        {OUTCOME_METADATA_KEY: {**outcome_provenance("success"), "version": True}},
        {OUTCOME_METADATA_KEY: {**outcome_provenance(None), "value": "success"}},
    ],
)
def test_incomplete_metadata_does_not_gain_outcome_authority(value):
    if isinstance(value, dict):
        value = {"category": "operational_experience", "operational_schema_version": 7, **value}
    assert outcome_context(value) is None


def test_legacy_user_metadata_cannot_supply_new_source_outcome_envelope():
    metadata = {
        "category": "operational_experience",
        "operational_schema_version": 6,
        OUTCOME_METADATA_KEY: outcome_provenance("success"),
    }
    assert outcome_context(metadata) is None
    projection = project_operational_experience(failed_search())
    assert all(
        e.metadata[OUTCOME_METADATA_KEY] == outcome_provenance("failure")
        for e in projection.entities
    )


def test_near_limit_unsplittable_passage_keeps_complete_original_body():
    body = "StaticText '" + "x" * 17_800 + "'"
    base = failed_search()
    observation = base.observations[0].model_copy(
        update={
            "evidence": (
                OperationalEvidencePart(
                    id="tree", content=body, content_type="text/plain; profile=accessibility-tree"
                ),
            )
        }
    )
    projection = project_operational_experience(
        base.model_copy(update={"observations": (observation,)})
    )
    passages = [e for e in projection.entities if e.entity_type == EntityType.PASSAGE]
    assert len(passages) == 1
    assert body in passages[0].content
    assert len(passages[0].content) <= 18_000
    assert 'Source-reported trajectory outcome: "failure"' in passages[0].description


@pytest.mark.parametrize("include_content", [True, False])
def test_product_search_and_lean_context_keep_outcome_and_bind_render_receipt(include_content):
    from dataclasses import replace

    from sibyl_core.models.context import ContextFacet, ContextIntent, ContextPack, ContextSection
    from sibyl_core.tools.context import _item_from_result
    from sibyl_core.tools.context_rendering import render_context_pack
    from sibyl_core.tools.search import graph_entity_to_search_result

    projection = project_operational_experience(failed_search())
    passage = next(e for e in projection.entities if e.entity_type == EntityType.PASSAGE)
    original_content = passage.content
    result = graph_entity_to_search_result(
        passage,
        organization_id="org",
        principal_id="reader",
        score=1,
        include_content=include_content,
    )
    item = _item_from_result(result, ContextFacet.RECENT_MEMORY)
    assert item.metadata[OUTCOME_METADATA_KEY] == outcome_provenance("failure")
    pack = ContextPack(
        goal="Review a failed attempt",
        intent=ContextIntent.GENERAL,
        query="author search",
        domain=None,
        project=None,
        sections=[ContextSection(ContextFacet.RECENT_MEMORY, "Recent memory", [item])],
        total_items=1,
    )
    rendered = render_context_pack(pack, max_content_chars=80)
    assert 'Source-reported trajectory outcome: "failure"' in rendered.markdown
    assert "Actions and reasoning are agent reports" in rendered.markdown
    assert passage.content == original_content
    content_span = next(s for s in rendered.receipt.spans if s.field == "content")
    visible = rendered.markdown.encode()[content_span.start_byte : content_span.end_byte]
    assert (
        visible
        == " ".join(
            item.content.encode()[content_span.input_start_byte : content_span.input_end_byte]
            .decode()
            .split()
        ).encode()
    )
    original_span = next(s for s in rendered.receipt.spans if s.field == "item")
    changed = replace(
        item, metadata={**item.metadata, OUTCOME_METADATA_KEY: outcome_provenance("unknown")}
    )
    changed_pack = replace(
        pack, sections=[ContextSection(ContextFacet.RECENT_MEMORY, "Recent memory", [changed])]
    )
    changed_render = render_context_pack(changed_pack, max_content_chars=80)
    assert (
        next(s for s in changed_render.receipt.spans if s.field == "item").input_sha256
        != original_span.input_sha256
    )
    assert original_span.transform == "markdown_item_outcome_v1"


def test_near_limit_action_keeps_original_inventory_and_procedure_body():
    base = failed_search()
    action = "x" * 17_900
    observations = tuple(
        observation.model_copy(
            update={
                "uri": None,
                "reasoning": None,
                "action": action if index else None,
            }
        )
        for index, observation in enumerate(base.observations)
    )
    projection = project_operational_experience(
        base.model_copy(update={"goal": "g", "observations": observations})
    )
    event = next(e for e in projection.entities if e.entity_type == EntityType.EVENT)
    procedure = next(e for e in projection.entities if e.entity_type == EntityType.PROCEDURE)
    assert event.content.startswith(f"Goal: g\nAction: {action}\n")
    assert "No results for author:example" in event.content
    assert event.metadata["ui_inventory_truncated"] is False
    assert procedure.content == f"Goal: g\n1. {action}\nReported outcome: failure"
    for entity in (event, procedure):
        assert len(entity.content) <= 18_000
        assert outcome_context(entity.metadata) is not None
