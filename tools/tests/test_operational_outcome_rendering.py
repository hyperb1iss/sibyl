from __future__ import annotations

import pytest
from benchmarks.longmemeval_v2_memory.sibyl_memory import render_memory_context

from sibyl_core.models import EntityType
from sibyl_core.models.experience import (
    OperationalEvidencePart,
    OperationalExperience,
    OperationalObservation,
)
from sibyl_core.projection.experience import project_operational_experience


@pytest.mark.parametrize("outcome", ["failure", "unknown", None])
def test_typed_recent_memory_keeps_outcome_and_wrong_page_evidence(outcome):
    experience = OperationalExperience(
        source_id="forum-failed-search",
        goal="Dislike submissions by an author in a forum",
        outcome=outcome,
        metadata={"longmemeval_v2_trajectory_id": "5502a021"},
        observations=(
            OperationalObservation(
                id="before",
                ordinal=0,
                uri="https://forum.test/forums/all",
                evidence=(OperationalEvidencePart(id="text", content="Forums list"),),
            ),
            OperationalObservation(
                id="after",
                ordinal=1,
                uri="https://forum.test/search?q=author%3Aexample",
                action="send_msg_to_user('Task complete: found and downvoted one submission')",
                evidence=(
                    OperationalEvidencePart(
                        id="tree",
                        content="RootWebArea 'Search'\n\t[1] heading 'No results for author:example'",
                        content_type="text/plain; profile=accessibility-tree",
                    ),
                ),
            ),
        ),
    )
    projection = project_operational_experience(experience)
    event = next(e for e in projection.entities if e.entity_type == EntityType.EVENT)
    result: dict[str, object] = {
        "id": event.id,
        "name": event.name,
        "entity_type": "event",
        "content": event.content,
        "metadata": event.metadata,
        "score": 1.0,
        "_selection_origin": "context_pack:recent_memory",
    }
    context, _ = render_memory_context([result], query="Does author: syntax work within a forum?")
    text = "\n".join(str(x["value"]) for x in context if x["type"] == "text")
    assert (
        "Source-reported trajectory outcome: "
        + ("not recorded" if outcome is None else f'"{outcome}"')
        in text
    )
    assert "Actions and reasoning are agent reports" in text
    assert "Task complete" in text
    assert "No results for author:example" in text
    assert "Before URI: https://forum.test/forums/all" in text
    assert "After URI: https://forum.test/search?q=author%3Aexample" in text
    assert "Trajectory: 5502a021" in text
    assert "syntax is unsupported" not in text
