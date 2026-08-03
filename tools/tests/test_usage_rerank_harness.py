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
age = _load("age")
extract = _load("extract")
whatif = _load("whatif")

BASE = datetime(2026, 7, 15, 16, 52, 42, tzinfo=UTC)
ORG = "e7b94a25-dd4c-4fb8-b300-0c75e83998e2"

# The default fixture page serves three items and cites the middle one.
PAGE_ITEMS = 3
TWO_SESSIONS = 2
UNCITED_IN_PAGE = 2
RANK_OF_SECOND = 2
RANK_OF_THIRD = 3
NULL_TRIALS = 25
P95_OF_200 = 190
LONE_VALUE = 5.0


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
    assert attributions[0].outcome == join.NO_PRECEDING_EXPOSURE


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


def test_an_unrecognized_signal_type_is_neither_exposure_nor_feedback() -> None:
    """A signal the harness does not know about must not be scored as feedback.

    Treating anything non-exposure as feedback would silently mislabel a future
    signal type as a citation, which is the kind of drift that shows up as an
    unexplained jump in the positive count.
    """
    rows = [
        *_exposure_page(["a", "b"]),
        _event(
            signal="dwell",
            item_id="a",
            offset_us=1_000_000,
            surface="future_surface",
            session_key="future_surface:x",
        ),
    ]
    sessions = join.group_exposure_sessions(rows)
    assert join.attribute_feedback(sessions, rows) == ()

    overlap = join.measure_session_key_overlap(rows)
    assert overlap.exposure_sessions == 1
    assert overlap.feedback_sessions == 0


def test_gap_summary_reports_nothing_without_attributions() -> None:
    assert join.gap_summary(())["count"] == 0


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

OTHER_ORG = "11111111-2222-3333-4444-555555555555"


def _other_org_event(
    *,
    signal: str,
    session_key: str,
    seconds: float,
    item_id: str = "shared_item",
    surface: str = "search",
) -> Any:
    return events.normalize_event_row(
        {
            "organization_id": OTHER_ORG,
            "session_key": session_key,
            "message_key": f"{surface}:{signal}:{session_key}",
            "source_surface": surface,
            "item_kind": events.GRAPH_ENTITY,
            "item_id": item_id,
            "signal_type": signal,
            "event_at": (BASE + timedelta(seconds=seconds)).isoformat(),
            "metadata": {},
        }
    )


def _cross_tenant_rows() -> list[Any]:
    """Two tenants serve an item with the same id, and only tenant B cites it.

    Item ids are only unique inside a tenant, so an org-blind join key would let
    tenant B's citation label tenant A's exposure of a same-named item. The
    ordering is what makes that visible: tenant A's exposure is the LATEST one
    before the citation, so an org-blind "latest preceding exposure" rule picks
    the wrong tenant. With tenant B served last instead, both the correct and the
    broken key would agree and the fixture would prove nothing.
    """
    return [
        _other_org_event(signal=events.EXPOSURE, session_key="search:orgB", seconds=0),
        *[
            _event(
                signal=events.EXPOSURE,
                item_id=item_id,
                offset_us=5_000_000 + index * 2,
                session_key="search:orgA",
            )
            for index, item_id in enumerate(["shared_item", "other"])
        ],
        _other_org_event(
            signal=events.CITATION,
            session_key="cli_cite:orgB",
            seconds=10,
            surface="cli_cite",
        ),
    ]


def test_attribution_does_not_cross_tenants() -> None:
    rows = _cross_tenant_rows()
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows)
    assert len(attributions) == 1
    assert attributions[0].organization_id == OTHER_ORG
    assert attributions[0].session_key == "search:orgB"


def test_labels_do_not_cross_tenants() -> None:
    """Tenant A served the same item id and must come back unlabeled."""
    rows = _cross_tenant_rows()
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows)
    labeled = join.build_labeled_sessions(
        sessions, attributions, join.flag_eval_suspect_sessions(sessions)
    )
    by_ref = {entry.session.ref: entry for entry in labeled}
    assert by_ref[(ORG, "search:orgA")].cited_keys == frozenset()
    assert by_ref[(OTHER_ORG, "search:orgB")].cited_keys == frozenset(
        {(events.GRAPH_ENTITY, "shared_item")}
    )


def test_labels_do_not_cross_tenants_sharing_a_session_key() -> None:
    """The label map must key on the tenant, not the session key alone.

    Production digests happen to include the org, so two tenants are unlikely to
    collide on a session_key today. The harness must not depend on that: a
    truncated digest, a restored archive, or a synthetic key would collide, and
    the failure is silent because the wrong tenant simply gains a positive label.
    """
    shared_key = "search:same"
    rows = [
        *[
            _event(
                signal=events.EXPOSURE,
                item_id=item_id,
                offset_us=index * 2,
                session_key=shared_key,
            )
            for index, item_id in enumerate(["shared_item", "other"])
        ],
        _other_org_event(signal=events.EXPOSURE, session_key=shared_key, seconds=5),
        _other_org_event(
            signal=events.CITATION,
            session_key="cli_cite:orgB",
            seconds=10,
            surface="cli_cite",
        ),
    ]
    sessions = join.group_exposure_sessions(rows)
    assert len(sessions) == TWO_SESSIONS

    attributions = join.attribute_feedback(sessions, rows)
    labeled = join.build_labeled_sessions(
        sessions, attributions, join.flag_eval_suspect_sessions(sessions)
    )
    by_ref = {entry.session.ref: entry for entry in labeled}
    assert by_ref[(OTHER_ORG, shared_key)].cited_keys == frozenset(
        {(events.GRAPH_ENTITY, "shared_item")}
    )
    # Same session key, same item id, different tenant: must stay unlabeled.
    assert by_ref[(ORG, shared_key)].cited_keys == frozenset()


def test_burst_detection_buckets_per_tenant() -> None:
    """Two tenants each running three sessions is not one six-session burst."""
    rows: list[Any] = []
    for index in range(3):
        rows.extend(_exposure_page([f"a{index}"], session_key=f"search:A{index}"))
        rows.append(
            _other_org_event(
                signal=events.EXPOSURE,
                session_key=f"search:B{index}",
                seconds=0,
                item_id=f"b{index}",
            )
        )
    sessions = join.group_exposure_sessions(rows)
    origins = join.flag_eval_suspect_sessions(sessions, burst_threshold=6)
    assert set(origins.values()) == {join.ORIGIN_INTERACTIVE}


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


def test_rerank_promotes_past_an_item_carrying_misled_history() -> None:
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
    # "a" carries the misled history and is demoted, which lifts the cited "b" past it.
    assert outcomes[0].item_id == "b"
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


def test_history_days_before_measures_observable_age() -> None:
    rows = [
        _event(signal=events.EXPOSURE, item_id="a", offset_us=0, session_key="search:old"),
        _event(
            signal=events.EXPOSURE,
            item_id="a",
            offset_us=86_400_000_000,
            session_key="search:new",
        ),
    ]
    counts = prior.PointInTimeCounts(rows)
    cutoff = BASE + timedelta(days=1)
    assert counts.first_seen_at(events.GRAPH_ENTITY, "a") == BASE
    assert counts.history_days_before(events.GRAPH_ENTITY, "a", cutoff) == pytest.approx(1.0)


def test_history_days_before_is_none_before_the_item_was_ever_seen() -> None:
    counts = prior.PointInTimeCounts(_exposure_page(["a"]))
    assert counts.first_seen_at(events.GRAPH_ENTITY, "unknown") is None
    assert counts.history_days_before(events.GRAPH_ENTITY, "unknown", BASE) is None
    assert counts.history_days_before(events.GRAPH_ENTITY, "a", BASE) is None


def test_describe_candidate_priors_splits_cited_from_uncited() -> None:
    """The age columns are what keep a raw count gap from being over-read."""
    history = [
        _event(
            signal=events.EXPOSURE,
            item_id="a",
            offset_us=-864_000_000_000,
            session_key="search:ancient",
        ),
    ]
    labeled, rows = _labeled_fixture(cited="b")
    counts = prior.PointInTimeCounts([*history, *rows])
    contrast = prior.describe_candidate_priors([labeled], counts)
    assert contrast["cited"]["candidates"] == 1
    assert contrast["uncited"]["candidates"] == UNCITED_IN_PAGE
    # Item "a" was exposed ten days earlier, so only the uncited group has history.
    assert contrast["uncited"]["prior_exposures_mean"] > 0
    assert contrast["cited"]["prior_exposures_mean"] == 0
    assert contrast["uncited"]["history_days_median"] is not None


def test_describe_candidate_priors_flags_censored_history() -> None:
    """An item present at the window start has understated history, so it is flagged.

    Direction matters more than the count: censoring can only truncate long
    histories, so a higher censored share on one group means that group's age is
    understated rather than overstated.
    """
    labeled, rows = _labeled_fixture(cited="b")
    counts = prior.PointInTimeCounts(rows)
    contrast = prior.describe_candidate_priors([labeled], counts)
    # Every fixture item first appears at the window start, so all are censored.
    assert contrast["cited"]["censored_share"] == pytest.approx(1.0)
    assert contrast["uncited"]["censored_share"] == pytest.approx(1.0)


def test_earliest_event_at_marks_the_observation_window_start() -> None:
    rows = [
        _event(signal=events.EXPOSURE, item_id="a", offset_us=5_000),
        _event(signal=events.EXPOSURE, item_id="b", offset_us=0),
    ]
    assert prior.PointInTimeCounts(rows).earliest_event_at == BASE
    assert prior.PointInTimeCounts([]).earliest_event_at is None


def test_describe_candidate_priors_handles_an_empty_population() -> None:
    contrast = prior.describe_candidate_priors([], prior.PointInTimeCounts([]))
    assert contrast["cited"]["candidates"] == 0
    assert contrast["uncited"]["candidates"] == 0


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


# ---------------------------------------------------------------------------
# Verdict adjudication (whatif.py)
# ---------------------------------------------------------------------------


def _arm(name: str, mrr: float, ci: tuple[float, float]) -> dict[str, Any]:
    return {
        "name": name,
        "summary": {"mrr_delta": mrr},
        "bootstrap_ci_vs_zero": {"ci_low": ci[0], "ci_high": ci[1]},
    }


NULL_FIXTURE = {"mean": -0.06, "stdev": 0.02, "p95_abs": 0.09}


def test_adjudicate_calls_a_straddling_interval_indistinguishable_from_zero() -> None:
    """The observed win here is far above the null mean yet still not a win vs zero.

    This is the exact pair of facts that a one-sided permutation null misreads:
    the arm beats a same-strength random prior by 3+ sigma while its own interval
    contains zero, so it carries information and is not worth shipping.
    """
    verdicts = whatif.adjudicate([_arm("citation_only", 0.0037, (-0.037, 0.043))], NULL_FIXTURE)
    assert verdicts[0]["verdict"] == "indistinguishable_from_zero"
    assert verdicts[0]["beats_random_prior_of_equal_strength"] is True


def test_adjudicate_reports_harm_when_the_interval_sits_below_zero() -> None:
    verdicts = whatif.adjudicate(
        [_arm("production_retention_shape", -0.1439, (-0.19, -0.098))], NULL_FIXTURE
    )
    assert verdicts[0]["verdict"] == "harms_baseline"
    assert verdicts[0]["beats_random_prior_of_equal_strength"] is False


def test_adjudicate_reports_improvement_only_when_the_interval_clears_zero() -> None:
    verdicts = whatif.adjudicate([_arm("hypothetical", 0.08, (0.02, 0.14))], NULL_FIXTURE)
    assert verdicts[0]["verdict"] == "improves_on_baseline"


def test_adjudicate_marks_an_arm_without_an_interval_as_no_data() -> None:
    arm = {"name": "empty", "summary": {"mrr_delta": None}, "bootstrap_ci_vs_zero": {}}
    assert whatif.adjudicate([arm], NULL_FIXTURE)[0]["verdict"] == "no_data"


# ---------------------------------------------------------------------------
# Summary assembly (extract.py)
# ---------------------------------------------------------------------------


def _summary_fixture() -> dict[str, Any]:
    rows = [
        *_exposure_page(["a", "b", "c"]),
        _event(
            signal=events.CITATION,
            item_id="b",
            offset_us=1_000_000,
            surface="cli_cite",
            session_key="cli_cite:x",
        ),
    ]
    sessions = join.group_exposure_sessions(rows)
    attributions = join.attribute_feedback(sessions, rows)
    origins = join.flag_eval_suspect_sessions(sessions)
    labeled = join.build_labeled_sessions(sessions, attributions, origins)
    return extract.build_summary(
        rows,
        sessions,
        attributions,
        labeled,
        target="fixture",
        window_seconds=join.DEFAULT_ATTRIBUTION_WINDOW_SECONDS,
        burst_threshold=join.DEFAULT_BURST_THRESHOLD,
        burst_window_seconds=join.DEFAULT_BURST_WINDOW_SECONDS,
    )


def test_build_summary_reports_the_headline_query_recoverability() -> None:
    """The "0 of N exposures carry a query" headline comes from here, so pin it."""
    summary = _summary_fixture()
    assert summary["query_recoverability"]["exposure"]["with_value"] == 0
    assert summary["query_recoverability"]["exposure"]["events"] == PAGE_ITEMS
    assert summary["query_recoverability"]["exposure"]["share"] == pytest.approx(0.0)
    assert summary["query_recoverability"]["observed_exposure_metadata_keys"] == {}


def test_build_summary_counts_labels_and_the_dead_join() -> None:
    summary = _summary_fixture()
    assert summary["events"]["total"] == PAGE_ITEMS + 1
    assert summary["session_key_join"]["session_key_join_viable"] is False
    assert summary["labels"]["positive_labels"] == 1
    assert summary["sessions"]["contrastive_sessions"] == 1
    assert summary["attribution"]["outcomes"]["attributed"] == 1


def test_metadata_key_coverage_finds_a_present_key() -> None:
    rows = [
        _event(signal=events.EXPOSURE, item_id="a", metadata={"query": "hello"}),
        _event(signal=events.EXPOSURE, item_id="b", offset_us=2),
    ]
    coverage = extract.metadata_key_coverage(rows, events.QUERY_METADATA_KEYS)
    assert coverage == {"events": 2, "with_value": 1, "share": 0.5}


def test_observed_metadata_keys_counts_every_key() -> None:
    rows = [
        _event(signal=events.EXPOSURE, item_id="a", metadata={"response_id": "a"}),
        _event(
            signal=events.EXPOSURE,
            item_id="b",
            offset_us=2,
            metadata={"response_id": "b", "source_surface": "search"},
        ),
    ]
    assert extract.observed_metadata_keys(rows) == {"response_id": 2, "source_surface": 1}


# ---------------------------------------------------------------------------
# True-age lookup (age.py)
# ---------------------------------------------------------------------------


def test_graph_namespace_strips_dashes_and_lowercases() -> None:
    assert (
        age.graph_namespace("E7B94A25-DD4C-4FB8-B300-0C75E83998E2")
        == "org_e7b94a25dd4c4fb8b3000c75e83998e2"
    )


def test_fetch_created_at_routes_each_kind_to_its_own_table() -> None:
    """Graph entities live in the org namespace, raw captures in content."""

    class _Recorder:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows
            self.statements: list[str] = []

        def query(self, statement: str) -> list[dict[str, Any]]:
            self.statements.append(statement)
            return self.rows

    graph = _Recorder([{"uuid": "g1", "created_at": "2026-07-01T00:00:00Z"}])
    content = _Recorder([{"uuid": "r1", "created_at": "2026-07-02T00:00:00Z"}])
    resolved = age.fetch_created_at(
        "e7b94a25-dd4c-4fb8-b300-0c75e83998e2",
        [(events.GRAPH_ENTITY, "g1"), (events.RAW_CAPTURE, "r1")],
        graph_store=graph,
        content_store=content,
    )
    assert resolved[(events.GRAPH_ENTITY, "g1")] == datetime(2026, 7, 1, tzinfo=UTC)
    assert resolved[(events.RAW_CAPTURE, "r1")] == datetime(2026, 7, 2, tzinfo=UTC)
    assert "FROM entity" in graph.statements[0]
    assert "FROM raw_captures" in content.statements[0]


def test_fetch_created_at_omits_items_it_cannot_resolve() -> None:
    class _Empty:
        def query(self, statement: str) -> list[dict[str, Any]]:
            return []

    resolved = age.fetch_created_at(
        "e7b94a25-dd4c-4fb8-b300-0c75e83998e2",
        [(events.GRAPH_ENTITY, "missing")],
        graph_store=_Empty(),
        content_store=_Empty(),
    )
    assert resolved == {}


def test_fetch_created_at_refuses_a_quoted_item_id() -> None:
    """An id carrying a quote is rejected rather than interpolated."""

    class _Unused:
        def query(self, statement: str) -> list[dict[str, Any]]:
            raise AssertionError("should not be reached")

    with pytest.raises(ValueError, match="refusing to interpolate"):
        age.fetch_created_at(
            "e7b94a25-dd4c-4fb8-b300-0c75e83998e2",
            [(events.GRAPH_ENTITY, "a' OR true --")],
            graph_store=_Unused(),
            content_store=_Unused(),
        )


# ---------------------------------------------------------------------------
# Age standardization and the against-zero interval
# ---------------------------------------------------------------------------


def test_age_standardization_separates_an_age_gap_from_a_residual_gap() -> None:
    """A pure age effect must standardize away; a within-band gap must survive.

    Both candidates carry the same exposure count per age here, and the uncited
    one is simply older, so holding age fixed has to collapse the ratio to 1.
    """
    history = [
        _event(
            signal=events.EXPOSURE,
            item_id=item_id,
            offset_us=-1_000_000 + index,
            session_key="search:earlier",
        )
        for index, item_id in enumerate(["a", "b", "c"])
    ]
    labeled, rows = _labeled_fixture(cited="b")
    counts = prior.PointInTimeCounts([*history, *rows])
    created = {
        (events.GRAPH_ENTITY, item_id): BASE - timedelta(days=20) for item_id in ("a", "b", "c")
    }
    contrast = prior.describe_candidate_priors([labeled], counts, created)
    standardized = contrast["age_standardized"]
    assert standardized["resolved"] is True
    assert standardized["cited_exposures_mean"] == pytest.approx(1.0)
    # One shared age band and one prior exposure each, so no gap survives.
    assert standardized["age_standardized_ratio"] == pytest.approx(1.0)


def test_age_standardization_reports_unresolved_without_timestamps() -> None:
    labeled, rows = _labeled_fixture()
    contrast = prior.describe_candidate_priors([labeled], prior.PointInTimeCounts(rows))
    assert contrast["age_standardized"]["resolved"] is False


def test_bootstrap_interval_brackets_a_zero_effect() -> None:
    labeled, rows = _labeled_fixture(cited="c")
    outcomes = prior.rerank_session_kind(
        labeled, events.GRAPH_ENTITY, prior.PointInTimeCounts(rows)
    )
    interval = prior.bootstrap_ci_vs_zero(outcomes, resamples=200, seed=3)
    assert interval["observed"] == pytest.approx(0.0)
    assert interval["excludes_zero"] is False


def test_bootstrap_interval_is_empty_without_outcomes() -> None:
    assert prior.bootstrap_ci_vs_zero(())["resamples"] == 0


def test_nearest_rank_percentile_uses_the_nearest_rank() -> None:
    values = list(range(1, 201))
    assert prior._nearest_rank_percentile(values, 0.95) == P95_OF_200
    assert prior._nearest_rank_percentile([LONE_VALUE], 0.95) == LONE_VALUE


# ---------------------------------------------------------------------------
# Interleaved sessions
# ---------------------------------------------------------------------------


def _interleaved_session() -> Any:
    """A session whose kinds alternate, so timestamp order is not served order."""
    rows = [
        _event(signal=events.EXPOSURE, item_id="g1", offset_us=0),
        _event(signal=events.EXPOSURE, item_id="r1", item_kind=events.RAW_CAPTURE, offset_us=2),
        _event(signal=events.EXPOSURE, item_id="g2", offset_us=4),
        _event(
            signal=events.CITATION,
            item_id="g1",
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
    return labeled[0], rows


def test_interleaved_kinds_are_detected() -> None:
    labeled, _ = _interleaved_session()
    assert labeled.session.is_mixed_kind
    assert labeled.session.has_contiguous_kind_blocks is False


def test_run_whatif_drops_interleaved_sessions_by_default() -> None:
    """Their recovered rank is not a served order, so their delta is meaningless."""
    labeled, rows = _interleaved_session()
    counts = prior.PointInTimeCounts(rows)
    assert prior.run_whatif([labeled], counts) == ()
    assert prior.run_whatif([labeled], counts, require_contiguous_kinds=False) != ()


def test_rank_recovery_audit_names_the_interleaved_sessions() -> None:
    _, rows = _interleaved_session()
    exposures = [row for row in rows if row.signal_type == events.EXPOSURE]
    audit = join.rank_recovery_audit(exposures, join.group_exposure_sessions(rows))
    assert audit["mixed_kind_sessions_with_interleaved_kinds"] == 1
    assert audit["interleaved_session_keys"] == ["search:abc"]


def test_attribution_distinguishes_a_later_exposure_from_a_stale_one() -> None:
    later = [
        _event(
            signal=events.CITATION,
            item_id="a",
            offset_us=0,
            surface="cli_cite",
            session_key="cli_cite:x",
        ),
        _event(signal=events.EXPOSURE, item_id="a", offset_us=5_000_000),
    ]
    sessions = join.group_exposure_sessions(later)
    assert join.attribute_feedback(sessions, later)[0].outcome == join.NO_PRECEDING_EXPOSURE

    stale = [
        *_exposure_page(["a"]),
        _event(
            signal=events.CITATION,
            item_id="a",
            offset_us=200_000_000,
            surface="cli_cite",
            session_key="cli_cite:y",
        ),
    ]
    sessions = join.group_exposure_sessions(stale)
    assert (
        join.attribute_feedback(sessions, stale, window_seconds=60.0)[0].outcome
        == join.OUTSIDE_WINDOW
    )


def test_age_source_integrity_flags_a_rewritten_created_at() -> None:
    """A created_at later than first exposure means the timestamp drifted forward.

    The entity upsert assigns created_at unconditionally, so this check is what
    licenses using it as an age source at all.
    """
    labeled, rows = _labeled_fixture(cited="b")
    counts = prior.PointInTimeCounts(rows)

    sane = {(events.GRAPH_ENTITY, item): BASE - timedelta(days=1) for item in ("a", "b", "c")}
    clean = prior.describe_candidate_priors([labeled], counts, sane)["age_source_integrity"]
    assert clean["items_checked"] == PAGE_ITEMS
    assert clean["created_at_after_first_exposure"] == 0
    assert clean["trustworthy"] is True

    drifted = dict(sane)
    drifted[(events.GRAPH_ENTITY, "a")] = BASE + timedelta(days=1)
    dirty = prior.describe_candidate_priors([labeled], counts, drifted)["age_source_integrity"]
    assert dirty["created_at_after_first_exposure"] == 1
    assert dirty["trustworthy"] is False


# ---------------------------------------------------------------------------
# The standardization estimator itself
# ---------------------------------------------------------------------------

BAND_A_AGE_DAYS = 0.25
BAND_B_AGE_DAYS = 5.0
EXPECTED_RAW_RATIO = 4.0
EXPECTED_STANDARDIZED_RATIO = 2.0
FIXTURE_CITED_ITEMS = 4
POPULATED_BANDS = 2


def _weighting_fixture() -> tuple[Any, prior.PointInTimeCounts, dict[tuple[str, str], Any]]:
    """Two groups differing in age mix AND in within-band means.

    Within every age band the uncited mean is exactly twice the cited mean, so a
    correct age adjustment has to report 2.0. The age mixes are deliberately
    opposite (cited concentrated young, uncited concentrated old), which drives
    the unadjusted ratio to 4.0. Those two numbers differing is the whole point:
    a fixture where both groups share an age mix cannot tell a real adjustment
    apart from one that silently reproduces the raw ratio.
    """
    plan = {
        "cA1": (1, BAND_A_AGE_DAYS, True),
        "cA2": (1, BAND_A_AGE_DAYS, True),
        "cA3": (1, BAND_A_AGE_DAYS, True),
        "cB1": (5, BAND_B_AGE_DAYS, True),
        "uA1": (2, BAND_A_AGE_DAYS, False),
        "uB1": (10, BAND_B_AGE_DAYS, False),
        "uB2": (10, BAND_B_AGE_DAYS, False),
        "uB3": (10, BAND_B_AGE_DAYS, False),
    }
    item_ids = list(plan)

    history: list[Any] = []
    for item_id, (count, _, _) in plan.items():
        for repeat in range(count):
            history.append(
                _event(
                    signal=events.EXPOSURE,
                    item_id=item_id,
                    offset_us=-60_000_000 + repeat,
                    session_key=f"search:prior{repeat}",
                )
            )

    served = _exposure_page(item_ids, session_key="search:measured")
    citations = [
        _event(
            signal=events.CITATION,
            item_id=item_id,
            offset_us=5_000_000,
            surface="cli_cite",
            session_key=f"cli_cite:{item_id}",
        )
        for item_id, (_, _, cited) in plan.items()
        if cited
    ]
    rows = [*history, *served, *citations]
    sessions = [
        session
        for session in join.group_exposure_sessions(rows)
        if session.session_key == "search:measured"
    ]
    attributions = join.attribute_feedback(sessions, rows)
    labeled = join.build_labeled_sessions(
        sessions, attributions, join.flag_eval_suspect_sessions(sessions)
    )[0]
    created = {
        (events.GRAPH_ENTITY, item_id): BASE - timedelta(days=age)
        for item_id, (_, age, _) in plan.items()
    }
    return labeled, prior.PointInTimeCounts(rows), created


def test_age_standardization_weights_by_the_cited_age_distribution() -> None:
    """Pins the estimator, not just its plumbing.

    Weighting the band means by the uncited counts instead of the cited ones
    reproduces the raw ratio exactly, which is a silent no-op of the entire age
    adjustment. Asserting both numbers is what makes that mutation fail.
    """
    labeled, counts, created = _weighting_fixture()
    assert labeled.positive_count == FIXTURE_CITED_ITEMS
    standardized = prior.describe_candidate_priors([labeled], counts, created)["age_standardized"]

    assert standardized["resolved"] is True
    assert standardized["cited_exposures_mean"] == pytest.approx(2.0)
    assert standardized["uncited_exposures_mean_raw"] == pytest.approx(8.0)
    assert standardized["raw_ratio"] == pytest.approx(EXPECTED_RAW_RATIO)
    # The adjustment must land here and must not collapse onto raw_ratio.
    assert standardized["age_standardized_ratio"] == pytest.approx(EXPECTED_STANDARDIZED_RATIO)
    assert standardized["age_standardized_ratio"] != pytest.approx(standardized["raw_ratio"])


def test_age_standardization_holds_the_within_band_ratio_in_every_band() -> None:
    labeled, counts, created = _weighting_fixture()
    standardized = prior.describe_candidate_priors([labeled], counts, created)["age_standardized"]
    populated = [band for band in standardized["bands"] if band.get("ratio") is not None]
    assert len(populated) == POPULATED_BANDS
    for band in populated:
        assert band["ratio"] == pytest.approx(2.0)


def test_age_standardization_denominator_uses_only_usable_bands() -> None:
    """A cited candidate in a band with no uncited peer must not skew the ratio."""
    labeled, counts, created = _weighting_fixture()
    standardized = prior.describe_candidate_priors([labeled], counts, created)["age_standardized"]
    assert standardized["cited_usable_sample"] == FIXTURE_CITED_ITEMS
