"""Tests for the P5 usage-rerank feasibility harness.

Every test runs on fixture events, so the suite never needs a live store. The
fixtures deliberately reproduce the real emitter's key shapes and timestamp
behaviour, because the harness's conclusions rest on those details and a fixture
that idealizes them would test nothing.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from sibyl_core.retrieval.temporal import usage_retention_multiplier

HARNESS_DIR = Path(__file__).parents[2] / "benchmarks" / "usage_rerank"


def _load(name: str) -> ModuleType:
    if str(HARNESS_DIR) not in sys.path:
        sys.path.insert(0, str(HARNESS_DIR))
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HARNESS_DIR / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


events = _load("events")
join = _load("join")
prior = _load("prior")
store = _load("store")

BASE = datetime(2026, 7, 15, 16, 52, 42, tzinfo=UTC)
ORG = "e7b94a25-dd4c-4fb8-b300-0c75e83998e2"

# The default fixture page serves three items and cites the middle one.
PAGE_ITEMS = 3
TWO_SESSIONS = 2
UNCITED_IN_PAGE = 2
RANK_OF_SECOND = 2
RANK_OF_THIRD = 3
NULL_TRIALS = 25


def _event(
    *,
    signal: str,
    item_id: str,
    item_kind: str = events.GRAPH_ENTITY,
    offset_us: int = 0,
    session_key: str = "search:abc",
    message_key: str | None = None,
    surface: str = "search",
    metadata: dict[str, Any] | None = None,
) -> Any:
    if message_key is None:
        message_key = f"{surface}:{signal}:abc"
    return events.normalize_event_row(
        {
            "organization_id": ORG,
            "session_key": session_key,
            "message_key": message_key,
            "source_surface": surface,
            "item_kind": item_kind,
            "item_id": item_id,
            "signal_type": signal,
            "event_at": (BASE + timedelta(microseconds=offset_us)).isoformat(),
            "principal_id": "22a282f0-8dd0-45a4-959c-91b4ccba6e37",
            "project_id": "project_05eb5c8c782a",
            "metadata": metadata or {},
        }
    )


def _exposure_page(item_ids: list[str], **kwargs: Any) -> list[Any]:
    """One served page: consecutive items two microseconds apart, as the emitter writes them."""
    return [
        _event(signal=events.EXPOSURE, item_id=item_id, offset_us=index * 2, **kwargs)
        for index, item_id in enumerate(item_ids)
    ]


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------


def test_parse_event_datetime_truncates_sub_microsecond_precision() -> None:
    parsed = events.parse_event_datetime("2026-07-15T16:52:42.845958123Z")
    assert parsed == datetime(2026, 7, 15, 16, 52, 42, 845958, tzinfo=UTC)
    assert parsed.tzinfo is not None


def test_parse_event_datetime_assumes_utc_for_naive_input() -> None:
    assert events.parse_event_datetime("2026-07-15T16:52:42").tzinfo is UTC


def test_normalize_event_row_requires_identity_fields() -> None:
    with pytest.raises(ValueError, match="item_id is required"):
        events.normalize_event_row(
            {
                "organization_id": ORG,
                "session_key": "search:abc",
                "message_key": "search:exposure:abc",
                "source_surface": "search",
                "item_kind": events.GRAPH_ENTITY,
                "item_id": "",
                "signal_type": events.EXPOSURE,
                "event_at": BASE.isoformat(),
            }
        )


def test_recoverable_query_is_none_for_the_shipped_metadata_shape() -> None:
    """The emitter persists only response_id and source_surface."""
    row = _event(
        signal=events.EXPOSURE,
        item_id="claim_1",
        metadata={"response_id": "claim_1", "source_surface": "search"},
    )
    assert row.recoverable_query is None
    assert row.recorded_rank is None
    assert row.recorded_score is None


def test_recoverable_query_is_found_when_a_query_key_is_present() -> None:
    row = _event(
        signal=events.EXPOSURE,
        item_id="claim_1",
        metadata={"response_id": "claim_1", "query": "replay verification"},
    )
    assert row.recoverable_query == "replay verification"


# ---------------------------------------------------------------------------
# Session grouping and rank recovery
# ---------------------------------------------------------------------------


def test_group_exposure_sessions_recovers_served_rank_from_timestamps() -> None:
    rows = _exposure_page(["a", "b", "c"])
    sessions = join.group_exposure_sessions(rows)
    assert len(sessions) == 1
    assert [(item.item_id, item.rank_within_kind) for item in sessions[0].items] == [
        ("a", 1),
        ("b", 2),
        ("c", 3),
    ]


def test_group_exposure_sessions_ranks_within_kind_not_globally() -> None:
    """Raw and graph events arrive as two batches, so ranks restart per kind.

    A global rank would read the second batch as ranks 3 and 4 when the emitter
    actually served them interleaved and simply recorded them second.
    """
    rows = [
        *_exposure_page(["raw_1", "raw_2"], item_kind=events.RAW_CAPTURE),
        *[
            _event(
                signal=events.EXPOSURE,
                item_id=item_id,
                item_kind=events.GRAPH_ENTITY,
                offset_us=100 + index * 2,
            )
            for index, item_id in enumerate(["graph_1", "graph_2"])
        ],
    ]
    sessions = join.group_exposure_sessions(rows)
    ranks = {item.item_id: item.rank_within_kind for item in sessions[0].items}
    assert ranks == {"raw_1": 1, "raw_2": 2, "graph_1": 1, "graph_2": 2}
    assert sessions[0].is_mixed_kind


def test_group_exposure_sessions_splits_distinct_session_keys() -> None:
    rows = [
        *_exposure_page(["a", "b"], session_key="search:one"),
        *_exposure_page(["c"], session_key="search:two"),
    ]
    assert len(join.group_exposure_sessions(rows)) == TWO_SESSIONS


def test_group_exposure_sessions_ignores_feedback_rows() -> None:
    rows = [
        *_exposure_page(["a"]),
        _event(signal=events.CITATION, item_id="a", session_key="cli_cite:xyz"),
    ]
    sessions = join.group_exposure_sessions(rows)
    assert len(sessions) == 1
    assert sessions[0].item_count == 1


def test_rank_recovery_audit_reports_tied_timestamps() -> None:
    tied = [
        _event(signal=events.EXPOSURE, item_id="a", offset_us=0),
        _event(signal=events.EXPOSURE, item_id="b", offset_us=0),
    ]
    audit = join.rank_recovery_audit(tied, join.group_exposure_sessions(tied))
    assert audit["sessions_with_tied_timestamps"] == 1
    assert audit["sessions_strictly_ordered"] == 0


def test_rank_recovery_audit_reports_contiguous_kind_blocks() -> None:
    rows = [
        *_exposure_page(["raw_1"], item_kind=events.RAW_CAPTURE),
        _event(signal=events.EXPOSURE, item_id="graph_1", offset_us=50),
    ]
    audit = join.rank_recovery_audit(rows, join.group_exposure_sessions(rows))
    assert audit["mixed_kind_sessions"] == 1
    assert audit["mixed_kind_sessions_with_contiguous_kind_blocks"] == 1
    assert audit["global_rank_recoverable"] is False


# ---------------------------------------------------------------------------
# The structural join-key finding
# ---------------------------------------------------------------------------


def test_session_key_join_is_empty_for_the_shipped_key_shapes() -> None:
    """Exposure and citation digests cannot collide, so the natural join is dead.

    The citation digest folds cited_ids into its payload and the two families use
    disjoint surface prefixes, which is what this fixture reproduces.
    """
    rows = [
        *_exposure_page(["a", "b"], session_key="search:d1", surface="search"),
        _event(
            signal=events.CITATION,
            item_id="a",
            session_key="cli_cite:d2",
            surface="cli_cite",
        ),
    ]
    overlap = join.measure_session_key_overlap(rows)
    assert overlap.exposure_sessions == 1
    assert overlap.feedback_sessions == 1
    assert overlap.overlapping_sessions == 0
    assert overlap.overlapping_message_keys == 0
    assert overlap.to_json()["session_key_join_viable"] is False


# ---------------------------------------------------------------------------
# Feedback attribution
# ---------------------------------------------------------------------------


def test_attribute_feedback_picks_the_latest_preceding_exposure() -> None:
    first = _exposure_page(["a", "b"], session_key="search:first")
    second = [
        _event(
            signal=events.EXPOSURE,
            item_id=item_id,
            offset_us=10_000_000 + index * 2,
            session_key="search:second",
        )
        for index, item_id in enumerate(["a", "c"])
    ]
    citation = _event(
        signal=events.CITATION,
        item_id="a",
        offset_us=20_000_000,
        session_key="cli_cite:x",
        surface="cli_cite",
    )
    rows = [*first, *second, citation]
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows)
    assert len(attributions) == 1
    assert attributions[0].outcome == join.ATTRIBUTED
    assert attributions[0].session_key == "search:second"
    assert attributions[0].gap_seconds == pytest.approx(10.0, abs=0.001)


def test_attribute_feedback_rejects_feedback_beyond_the_window() -> None:
    rows = [
        *_exposure_page(["a"]),
        _event(
            signal=events.CITATION,
            item_id="a",
            offset_us=200_000_000,
            surface="cli_cite",
            session_key="cli_cite:x",
        ),
    ]
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows, window_seconds=60.0)
    assert attributions[0].outcome == join.OUTSIDE_WINDOW
    assert attributions[0].session_key is None


def test_attribute_feedback_flags_an_item_that_was_never_exposed() -> None:
    rows = [
        *_exposure_page(["a"]),
        _event(
            signal=events.CITATION,
            item_id="never_served",
            offset_us=1000,
            surface="cli_cite",
            session_key="cli_cite:x",
        ),
    ]
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows)
    assert attributions[0].outcome == join.ITEM_NEVER_EXPOSED


def test_attribute_feedback_ignores_exposure_that_postdates_the_citation() -> None:
    """Feedback is about an item the agent already saw, never a later exposure."""
    rows = [
        _event(
            signal=events.CITATION,
            item_id="a",
            offset_us=0,
            surface="cli_cite",
            session_key="cli_cite:x",
        ),
        _event(signal=events.EXPOSURE, item_id="a", offset_us=5_000_000),
    ]
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows)
    assert attributions[0].outcome == join.OUTSIDE_WINDOW


def test_attribution_window_sweep_is_monotone() -> None:
    rows = [
        *_exposure_page(["a", "b"]),
        _event(
            signal=events.CITATION,
            item_id="a",
            offset_us=2_000_000,
            surface="cli_cite",
            session_key="cli_cite:x",
        ),
    ]
    sessions = join.group_exposure_sessions(rows)
    sweep = join.attribution_window_sweep(sessions, rows, (1.0, 60.0, 3600.0))
    assert sweep["1s"] == 0
    assert sweep["60s"] == 1
    assert sweep["3600s"] == 1


def test_gap_summary_reports_nothing_without_attributions() -> None:
    assert join.gap_summary(())["count"] == 0


# ---------------------------------------------------------------------------
# Labels and contamination
# ---------------------------------------------------------------------------


def _labeled_fixture(
    cited: str = "b",
    item_ids: tuple[str, ...] = ("a", "b", "c"),
) -> tuple[Any, list[Any]]:
    rows = [
        *_exposure_page(list(item_ids)),
        _event(
            signal=events.CITATION,
            item_id=cited,
            offset_us=1_000_000,
            surface="cli_cite",
            session_key="cli_cite:x",
        ),
    ]
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows)
    origins = join.flag_eval_suspect_sessions(sessions)
    labeled = join.build_labeled_sessions(sessions, attributions, origins)
    return labeled[0], rows


def test_build_labeled_sessions_marks_positives_and_negatives() -> None:
    labeled, _ = _labeled_fixture()
    assert labeled.cited_keys == frozenset({(events.GRAPH_ENTITY, "b")})
    assert labeled.positive_count == 1
    assert labeled.negative_count == UNCITED_IN_PAGE
    assert labeled.is_contrastive()


def test_misled_feedback_is_not_counted_as_a_positive() -> None:
    rows = [
        *_exposure_page(["a", "b"]),
        _event(
            signal=events.MISLED,
            item_id="a",
            offset_us=1_000_000,
            surface="cli_cite_misled",
            session_key="cli_cite_misled:x",
        ),
    ]
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows)
    labeled = join.build_labeled_sessions(
        sessions, attributions, join.flag_eval_suspect_sessions(sessions)
    )[0]
    assert labeled.positive_count == 0
    assert labeled.misled_keys == frozenset({(events.GRAPH_ENTITY, "a")})
    assert not labeled.is_contrastive()


def test_is_contrastive_is_false_when_every_item_was_cited() -> None:
    """With nothing to promote past, a session teaches a reranker nothing."""
    rows = [
        *_exposure_page(["a"]),
        _event(
            signal=events.CITATION,
            item_id="a",
            offset_us=1_000_000,
            surface="cli_cite",
            session_key="cli_cite:x",
        ),
    ]
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows)
    labeled = join.build_labeled_sessions(
        sessions, attributions, join.flag_eval_suspect_sessions(sessions)
    )[0]
    assert labeled.positive_count == 1
    assert not labeled.is_contrastive()


def test_is_contrastive_is_scoped_to_one_item_kind() -> None:
    rows = [
        *_exposure_page(["raw_1", "raw_2"], item_kind=events.RAW_CAPTURE),
        _event(signal=events.EXPOSURE, item_id="graph_1", offset_us=100),
        _event(
            signal=events.CITATION,
            item_id="raw_1",
            item_kind=events.RAW_CAPTURE,
            offset_us=1_000_000,
            surface="cli_cite",
            session_key="cli_cite:x",
        ),
    ]
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows)
    labeled = join.build_labeled_sessions(
        sessions, attributions, join.flag_eval_suspect_sessions(sessions)
    )[0]
    assert labeled.is_contrastive(events.RAW_CAPTURE)
    assert not labeled.is_contrastive(events.GRAPH_ENTITY)


def test_flag_eval_suspect_sessions_flags_a_programmatic_burst() -> None:
    rows: list[Any] = []
    for index in range(6):
        rows.extend(
            _exposure_page(
                [f"item_{index}_0", f"item_{index}_1"],
                session_key=f"context_pack:burst{index}",
                surface="context_pack",
            )
        )
    sessions = join.group_exposure_sessions(rows)
    origins = join.flag_eval_suspect_sessions(sessions, burst_threshold=6)
    assert set(origins.values()) == {join.ORIGIN_BURST_SUSPECT}


def test_flag_eval_suspect_sessions_leaves_sparse_traffic_interactive() -> None:
    rows: list[Any] = []
    for index in range(3):
        rows.extend(
            _exposure_page(
                [f"item_{index}"],
                session_key=f"search:s{index}",
            )
        )
    sessions = join.group_exposure_sessions(rows)
    origins = join.flag_eval_suspect_sessions(sessions, burst_threshold=6)
    assert set(origins.values()) == {join.ORIGIN_INTERACTIVE}


def test_flag_eval_suspect_sessions_honours_an_explicit_eval_surface() -> None:
    rows = _exposure_page(["a"], session_key="context_pack:e1", surface="context_pack")
    sessions = join.group_exposure_sessions(rows)
    origins = join.flag_eval_suspect_sessions(sessions, eval_surfaces=frozenset({"context_pack"}))
    assert set(origins.values()) == {join.ORIGIN_EVAL_SURFACE}


# ---------------------------------------------------------------------------
# The usage prior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "counts",
    [
        (0, 0, 0),
        (5, 0, 0),
        (50, 0, 0),
        (999, 0, 0),
        (0, 3, 0),
        (0, 999, 0),
        (0, 0, 1),
        (0, 0, 999),
        (10, 2, 1),
    ],
)
def test_usage_prior_multiplier_matches_the_production_retention_curve(
    counts: tuple[int, int, int],
) -> None:
    """Pin the harness curve to the shipped one so the what-if measures production shape."""
    retrieval_count, citation_count, misled_count = counts
    harness = prior.usage_prior_multiplier(
        prior.UsageCounts(
            retrieval_count=retrieval_count,
            citation_count=citation_count,
            misled_count=misled_count,
        )
    )
    production = usage_retention_multiplier(
        {
            "retrieval_count": retrieval_count,
            "citation_count": citation_count,
            "misled_count": misled_count,
        }
    )
    assert harness == pytest.approx(production)


def test_usage_prior_multiplier_stays_inside_its_clamp() -> None:
    floor = prior.usage_prior_multiplier(prior.UsageCounts(misled_count=99))
    ceiling = prior.usage_prior_multiplier(
        prior.UsageCounts(retrieval_count=9999, citation_count=9999)
    )
    assert floor == pytest.approx(0.1)
    assert ceiling == pytest.approx(4.0)


def test_rrf_score_decreases_with_rank() -> None:
    assert prior.rrf_score(1) > prior.rrf_score(2) > prior.rrf_score(50)


def test_point_in_time_counts_exclude_the_session_being_scored() -> None:
    """The leakage guard: the citation under study can never feed its own score.

    Without this the what-if scores an item using the very citation it is trying
    to predict and reports a spectacular, meaningless win.
    """
    rows = [
        *_exposure_page(["a"]),
        _event(
            signal=events.CITATION,
            item_id="a",
            offset_us=1_000_000,
            surface="cli_cite",
            session_key="cli_cite:x",
        ),
    ]
    counts = prior.PointInTimeCounts(rows)
    at_session_start = counts.counts_before(events.GRAPH_ENTITY, "a", BASE)
    assert at_session_start.retrieval_count == 0
    assert at_session_start.citation_count == 0

    later = counts.counts_before(events.GRAPH_ENTITY, "a", BASE + timedelta(seconds=5))
    assert later.retrieval_count == 1
    assert later.citation_count == 1


def test_point_in_time_counts_are_zero_for_an_unknown_item() -> None:
    counts = prior.PointInTimeCounts(_exposure_page(["a"]))
    assert counts.counts_before(events.GRAPH_ENTITY, "unknown", BASE).is_empty


def test_rerank_promotes_an_item_with_prior_citation_history() -> None:
    history = [
        _event(
            signal=events.EXPOSURE,
            item_id="b",
            offset_us=-2_000_000,
            session_key="search:old",
        ),
        _event(
            signal=events.CITATION,
            item_id="b",
            offset_us=-1_000_000,
            surface="cli_cite",
            session_key="cli_cite:old",
        ),
    ]
    labeled, rows = _labeled_fixture(cited="b")
    counts = prior.PointInTimeCounts([*history, *rows])
    outcomes = prior.rerank_session_kind(labeled, events.GRAPH_ENTITY, counts)
    assert len(outcomes) == 1
    assert outcomes[0].baseline_rank == RANK_OF_SECOND
    assert outcomes[0].reweighted_rank == 1
    assert outcomes[0].rank_delta == 1
    assert outcomes[0].prior_counts.citation_count == 1


def test_rerank_keeps_baseline_order_when_no_item_has_history() -> None:
    """Equal multipliers must not shuffle anything, or every delta is a tie-break."""
    labeled, rows = _labeled_fixture(cited="c")
    outcomes = prior.rerank_session_kind(
        labeled, events.GRAPH_ENTITY, prior.PointInTimeCounts(rows)
    )
    assert outcomes[0].baseline_rank == RANK_OF_THIRD
    assert outcomes[0].reweighted_rank == RANK_OF_THIRD
    assert outcomes[0].rank_delta == 0


def test_rerank_demotes_an_item_carrying_misled_history() -> None:
    history = [
        _event(
            signal=events.MISLED,
            item_id="a",
            offset_us=-1_000_000,
            surface="cli_cite_misled",
            session_key="cli_cite_misled:old",
        ),
    ]
    labeled, rows = _labeled_fixture(cited="b")
    counts = prior.PointInTimeCounts([*history, *rows])
    outcomes = prior.rerank_session_kind(labeled, events.GRAPH_ENTITY, counts)
    assert outcomes[0].reweighted_rank < outcomes[0].baseline_rank


def test_rerank_returns_nothing_for_a_kind_that_was_not_served() -> None:
    labeled, rows = _labeled_fixture()
    assert (
        prior.rerank_session_kind(labeled, events.RAW_CAPTURE, prior.PointInTimeCounts(rows)) == ()
    )


def test_run_whatif_skips_non_contrastive_sessions() -> None:
    rows = [
        *_exposure_page(["a"]),
        _event(
            signal=events.CITATION,
            item_id="a",
            offset_us=1_000_000,
            surface="cli_cite",
            session_key="cli_cite:x",
        ),
    ]
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows)
    labeled = join.build_labeled_sessions(
        sessions, attributions, join.flag_eval_suspect_sessions(sessions)
    )
    assert prior.run_whatif(labeled, prior.PointInTimeCounts(rows)) == ()


def test_summarize_outcomes_is_explicit_about_an_empty_population() -> None:
    summary = prior.summarize_outcomes(())
    assert summary["cited_items_evaluated"] == 0
    assert summary["mrr_delta"] is None


def test_summarize_outcomes_splits_the_delta_distribution() -> None:
    labeled, rows = _labeled_fixture(cited="c")
    outcomes = prior.rerank_session_kind(
        labeled, events.GRAPH_ENTITY, prior.PointInTimeCounts(rows)
    )
    summary = prior.summarize_outcomes(outcomes)
    assert summary["cited_items_evaluated"] == 1
    assert summary["unchanged"] == 1
    assert summary["mrr_delta"] == pytest.approx(0.0)


def test_permutation_null_is_reproducible_for_a_fixed_seed() -> None:
    labeled, rows = _labeled_fixture()
    counts = prior.PointInTimeCounts(rows)
    first = prior.permutation_null([labeled], counts, trials=NULL_TRIALS, seed=7)
    second = prior.permutation_null([labeled], counts, trials=NULL_TRIALS, seed=7)
    assert first == second
    assert first["trials"] == NULL_TRIALS


def test_permutation_null_reports_no_trials_without_a_population() -> None:
    assert prior.permutation_null([], prior.PointInTimeCounts([]))["trials"] == 0


# ---------------------------------------------------------------------------
# Read-only store guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE memory_usage_events;",
        "UPDATE raw_captures SET citation_count = 0;",
        "INSERT INTO memory_usage_events $rows;",
        "REMOVE TABLE memory_usage_events;",
        "define field x on memory_usage_events type string;",
        "SELECT * FROM x; DELETE x;",
    ],
)
def test_assert_read_only_rejects_mutating_statements(statement: str) -> None:
    with pytest.raises(store.MutatingStatementError):
        store.assert_read_only(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM memory_usage_events;",
        "SELECT count() FROM memory_usage_events GROUP ALL;",
        "LET $x = (SELECT VALUE item_id FROM memory_usage_events); RETURN $x;",
        "SELECT * FROM memory_usage_events WHERE source_surface = 'source_import';",
        "SELECT * FROM t WHERE note = 'please update the docs'; -- delete later",
    ],
)
def test_assert_read_only_allows_reads(statement: str) -> None:
    store.assert_read_only(statement)


def test_extract_result_surfaces_a_failed_statement() -> None:
    with pytest.raises(RuntimeError, match="statement failed"):
        store.extract_result([{"status": "ERR", "result": "boom"}])


def test_extract_result_returns_the_last_statements_rows() -> None:
    payload = [
        {"status": "OK", "result": [{"ignored": True}]},
        {"status": "OK", "result": [{"item_id": "a"}]},
    ]
    assert store.extract_result(payload) == [{"item_id": "a"}]


def test_extract_result_treats_a_null_result_as_empty() -> None:
    assert store.extract_result([{"status": "OK", "result": None}]) == []


def test_extract_result_rejects_a_malformed_envelope() -> None:
    with pytest.raises(RuntimeError, match="unexpected SurrealDB response shape"):
        store.extract_result({"status": "OK"})
