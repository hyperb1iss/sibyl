from sibyl_core.retrieval.refinement import (
    RetrievalFeedbackDocument,
    plan_deterministic_refinement_queries,
)


def _document(
    document_id: str,
    text: str,
    *,
    raw_observation_projection: bool = False,
    evidence_content_type: str | None = None,
) -> RetrievalFeedbackDocument:
    return RetrievalFeedbackDocument(
        id=document_id,
        text=text,
        raw_observation_projection=raw_observation_projection,
        evidence_content_type=evidence_content_type,
    )


def test_refinement_starts_with_a_focused_query() -> None:
    planned = plan_deterministic_refinement_queries(
        "What deployment workflow did the release team use?",
        [],
        max_queries=2,
    )

    assert [query.query for query in planned] == ["deployment workflow release team"]
    assert planned[0].facet == "focus"


def test_refinement_uses_consensus_terms_from_retrieved_evidence() -> None:
    planned = plan_deterministic_refinement_queries(
        "Which deployment workflow did we use?",
        [
            _document("one", "Canary rollout used a promotion receipt after validation."),
            _document("two", "The canary promotion paused until validation completed."),
        ],
        max_queries=2,
    )

    feedback = planned[1]
    assert feedback.facet == "corroboration"
    assert feedback.added_terms[:3] == ("canary", "promotion", "validation")
    assert feedback.source_result_ids == ("one", "two")
    assert feedback.query.endswith("canary promotion validation")


def test_refinement_can_diversify_from_distinctive_top_result_terms() -> None:
    planned = plan_deterministic_refinement_queries(
        "What failed during deployment?",
        [
            _document("one", "Buildkite artifact signing rejected the provenance envelope."),
            _document("two", "Argo rollout remained healthy."),
        ],
        max_queries=3,
    )

    assert [query.facet for query in planned] == ["focus", "feedback"]
    assert "buildkite" in planned[1].added_terms
    assert planned[1].source_result_ids == ("one",)


def test_refinement_does_not_repeat_seen_queries_or_question_terms() -> None:
    planned = plan_deterministic_refinement_queries(
        "Which canary promotion passed validation?",
        [_document("one", "Canary promotion passed validation with receipt alpha.")],
        max_queries=3,
        seen_queries=["canary promotion passed validation"],
    )

    assert len(planned) == 1
    assert planned[0].facet == "feedback"
    assert planned[0].added_terms == ("receipt", "alpha")
    assert planned[0].query.endswith("receipt alpha")


def test_refinement_bounds_focused_queries_without_splitting_terms() -> None:
    terms = [f"deployment-term-{index}" for index in range(100)]
    planned = plan_deterministic_refinement_queries(
        " ".join(terms),
        [],
        max_queries=1,
    )

    assert len(planned[0].query) <= 500
    assert planned[0].query.split() == terms[: len(planned[0].query.split())]


def test_refinement_bounds_expanded_query_prefix_without_splitting_terms() -> None:
    terms = [f"questionterm{index}" for index in range(100)]
    planned = plan_deterministic_refinement_queries(
        " ".join(terms),
        [_document("one", "feedbackanchor")],
        max_queries=2,
    )

    expanded_terms = planned[1].query.split()
    assert len(planned[1].query) <= 500
    assert expanded_terms[-1] == "feedbackanchor"
    assert expanded_terms[:-1] == terms[: len(expanded_terms) - 1]


def test_refinement_ignores_memory_envelope_and_accessibility_syntax() -> None:
    planned = plan_deterministic_refinement_queries(
        "Which change request was closed successfully?",
        [
            _document(
                "one",
                """Operational observation 2 part 1/2
Goal: Which change request was closed successfully?
Reported outcome: success
Observation: 2
URI: https://example.invalid/change/CHG0000095
Evidence content type: text/plain; profile=accessibility-tree
Evidence:
Trajectory: abc123
State: 2
Accessibility tree:
[a1] generic, live='polite', relevant='additions text'
[a2] gridcell 'CAB approval recorded for CHG0000095', visible
""",
                raw_observation_projection=True,
                evidence_content_type="text/plain; profile=accessibility-tree",
            ),
            _document(
                "two",
                """Operational observation 3 part 1/1
Goal: Which change request was closed successfully?
Reported outcome: success
Observation: 3
Evidence content type: text/plain; profile=accessibility-tree
Evidence:
[a3] generic, live='assertive', relevant='all'
[a4] status 'CAB approval completed', visible
""",
                raw_observation_projection=True,
                evidence_content_type="text/plain; profile=accessibility-tree",
            ),
        ],
        max_queries=2,
    )

    feedback = planned[1]
    assert {"cab", "approval"} <= set(feedback.added_terms)
    assert not {"operational", "observation", "part", "goal"} & set(feedback.added_terms)


def test_refinement_preserves_plain_content_that_looks_like_ui_syntax() -> None:
    planned = plan_deterministic_refinement_queries(
        "Which regional rollout detail changed?",
        [
            _document(
                "one",
                """State: California
Part: replacement schedule
Navigation planning retained the coastal sequence.
[release] checklist item remained visible.
""",
            )
        ],
        max_queries=2,
    )

    assert planned[1].added_terms == ("state", "california", "part", "replacement")


def test_refinement_ignores_truncated_raw_observation_headers() -> None:
    planned = plan_deterministic_refinement_queries(
        "Which regional rollout detail changed?",
        [
            _document(
                "one",
                """Operational observation 2 part 1/2
Goal: Which regional rollout detail changed?
Reported outcome: success
Observation: 2
""",
                raw_observation_projection=True,
                evidence_content_type="text/plain; profile=accessibility-tree",
            )
        ],
        max_queries=2,
    )

    assert [query.facet for query in planned] == ["focus"]
