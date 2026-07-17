from sibyl_core.retrieval.refinement import (
    RetrievalFeedbackDocument,
    plan_deterministic_refinement_queries,
)


def _document(
    document_id: str,
    text: str,
    *,
    source_id: str | None = None,
    raw_observation_projection: bool = False,
    evidence_content_type: str | None = None,
) -> RetrievalFeedbackDocument:
    return RetrievalFeedbackDocument(
        id=document_id,
        text=text,
        source_id=source_id,
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


def test_refinement_strips_answer_formatting_and_focuses_explicit_anchors() -> None:
    planned = plan_deterministic_refinement_queries(
        "Which column is between `Store` and `Results`? Your final answer should be wrapped in "
        r"\boxed{}.",
        [],
        max_queries=2,
    )

    assert [query.facet for query in planned] == ["anchor", "focus"]
    assert planned[0].query == '"store" "result"'
    assert planned[0].added_terms == ("store", "result")
    assert planned[1].query == "column store result"
    assert all("boxed" not in query.query for query in planned)


def test_refinement_preserves_question_after_prefix_answer_formatting() -> None:
    planned = plan_deterministic_refinement_queries(
        r"Provide the final answer in \boxed{}. Which column changed?",
        [],
        max_queries=1,
    )

    assert planned[0].query == "column changed"


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


def test_refinement_excludes_corpus_scaffolding_from_consensus_terms() -> None:
    documents = [
        _document(
            str(index),
            "Workarena success configuration item routing table"
            if index < 3
            else "Workarena success unrelated catalog entry",
        )
        for index in range(8)
    ]

    planned = plan_deterministic_refinement_queries(
        "Which routing detail changed?",
        documents,
        max_queries=2,
    )

    assert planned[1].facet == "corroboration"
    assert {"configuration", "item", "table"} <= set(planned[1].added_terms)
    assert not {"workarena", "success"} & set(planned[1].added_terms)


def test_refinement_excludes_terms_present_in_all_three_sources() -> None:
    planned = plan_deterministic_refinement_queries(
        "Which detail changed?",
        [
            _document("one", "Workarena routing configuration"),
            _document("two", "Workarena routing table"),
            _document("three", "Workarena catalog item"),
        ],
        max_queries=2,
    )

    assert planned[1].added_terms == ("routing",)


def test_refinement_rejects_numeric_and_opaque_feedback_terms() -> None:
    planned = plan_deterministic_refinement_queries(
        "Which field changed?",
        [_document("one", "a47 115 notice-w8sgtt3 meaningful configuration")],
        max_queries=2,
    )

    assert planned[1].added_terms == ("meaningful", "configuration")


def test_refinement_does_not_expand_from_uncorroborated_multi_source_terms() -> None:
    planned = plan_deterministic_refinement_queries(
        "What failed during deployment?",
        [
            _document("one", "Buildkite artifact signing rejected the provenance envelope."),
            _document("two", "Argo rollout remained healthy."),
        ],
        max_queries=3,
    )

    assert [query.facet for query in planned] == ["focus"]


def test_refinement_counts_repeated_chunks_as_one_feedback_source() -> None:
    planned = plan_deterministic_refinement_queries(
        "Which incident filter changed?",
        [
            _document(
                "chunk-one",
                "Activity stream created follow-up entry",
                source_id="trajectory-one",
            ),
            _document(
                "chunk-two",
                "Activity stream created another entry",
                source_id="trajectory-one",
            ),
            _document(
                "chunk-three",
                "Mobile portal option labels",
                source_id="trajectory-two",
            ),
        ],
        max_queries=3,
    )

    assert [query.facet for query in planned] == ["focus"]


def test_refinement_receipts_include_unique_sources_beyond_eight_chunks() -> None:
    planned = plan_deterministic_refinement_queries(
        "Which deployment detail changed?",
        [
            _document(
                f"first-{index}",
                "Nebula promotion receipt",
                source_id="trajectory-one",
            )
            for index in range(8)
        ]
        + [
            _document(
                "second-source",
                "Nebula rollout evidence",
                source_id="trajectory-two",
            )
        ],
        max_queries=2,
    )

    assert planned[1].added_terms == ("nebula",)
    assert planned[1].source_result_ids == ("first-0", "second-source")


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
