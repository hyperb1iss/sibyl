"""Correction lineage: one root's correction reaching independent derived rows.

The adversarial sequences pinned here are the ones a plain "blocked" set and
per-immediate-parent blockers both get wrong: propagations that arrive in the
wrong order, a revise whose staleness has to outlive a later restore, and two
roots whose verdicts must not read each other's mail.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from sibyl_core.memory_pipeline.lifecycle import (
    memory_lifecycle_state,
    raw_memory_lifecycle_recallable,
)
from sibyl_core.memory_pipeline.source_lifecycle import (
    CORRECTION_BLOCKERS_KEY,
    CORRECTION_HISTORY_KEY,
    RAW_SOURCE_IDS_KEY,
    SOURCE_BINDINGS_KEY,
    UNKNOWN_SOURCE_REVISION,
    SourceCorrection,
    correction_blocked,
    correction_event,
    merge_source_correction,
    source_revision_bindings,
)
from sibyl_core.models.reflection import (
    MemoryLifecycle,
    MemoryLifecycleState,
    with_memory_lifecycle_metadata,
)
from sibyl_core.services.memory_identity import IDENTITY_KEY

ROOT_A = "source-a"
ROOT_B = "source-b"


@dataclass(slots=True)
class SourceView:
    """A capture as this model reads it: an id, a revision, a metadata bag.

    ``source_id`` and ``review_state`` are here only so the same fake can be
    handed to the local lifecycle policy, which is what shows the two verdicts
    stay apart.
    """

    id: str = ROOT_A
    revision: int = 1
    metadata: Mapping[str, object] = field(default_factory=dict)
    source_id: str = ""
    review_state: str = "pending"


def blocker_state(entries: Mapping[str, tuple[int, bool]]) -> dict[str, object]:
    return {
        CORRECTION_BLOCKERS_KEY: {
            root: {"revision": revision, "blocking": blocking}
            for root, (revision, blocking) in entries.items()
        }
    }


def stored_entry(metadata: Mapping[str, object], root: str) -> object:
    blockers = metadata[CORRECTION_BLOCKERS_KEY]
    assert isinstance(blockers, Mapping)
    return blockers[root]


MALFORMED_BLOCKER_STATES: list[tuple[dict[str, object], str]] = [
    ({CORRECTION_BLOCKERS_KEY: [{"revision": 2, "blocking": True}]}, "must be a mapping"),
    ({CORRECTION_BLOCKERS_KEY: "blocked"}, "must be a mapping"),
    ({CORRECTION_BLOCKERS_KEY: {ROOT_A: "blocked"}}, "entry must be a mapping"),
    ({CORRECTION_BLOCKERS_KEY: {ROOT_A: {"blocking": True}}}, "revision must be a positive"),
    (
        {CORRECTION_BLOCKERS_KEY: {ROOT_A: {"revision": 0, "blocking": True}}},
        "revision must be a positive",
    ),
    (
        {CORRECTION_BLOCKERS_KEY: {ROOT_A: {"revision": True, "blocking": True}}},
        "revision must be a positive",
    ),
    (
        {CORRECTION_BLOCKERS_KEY: {ROOT_A: {"revision": "2", "blocking": True}}},
        "revision must be a positive",
    ),
    (
        {CORRECTION_BLOCKERS_KEY: {ROOT_A: {"revision": 2, "blocking": "yes"}}},
        "blocking must be a boolean",
    ),
    (
        {CORRECTION_BLOCKERS_KEY: {ROOT_A: {"revision": 2, "blocking": 1}}},
        "blocking must be a boolean",
    ),
    (
        {CORRECTION_BLOCKERS_KEY: {"": {"revision": 2, "blocking": True}}},
        "root id must be a non-empty string",
    ),
]

MALFORMED_BINDING_STATES: list[tuple[dict[str, object], str]] = [
    ({SOURCE_BINDINGS_KEY: [2]}, "source_bindings must be a mapping"),
    ({SOURCE_BINDINGS_KEY: "3"}, "source_bindings must be a mapping"),
    ({SOURCE_BINDINGS_KEY: {ROOT_A: -1}}, "revision must be a non-negative integer"),
    ({SOURCE_BINDINGS_KEY: {ROOT_A: True}}, "revision must be a non-negative integer"),
    ({SOURCE_BINDINGS_KEY: {ROOT_A: "3"}}, "revision must be a non-negative integer"),
    ({SOURCE_BINDINGS_KEY: {"  ": 3}}, "source id must be a non-empty string"),
]

MALFORMED_EVENTS: list[tuple[SourceCorrection, str]] = [
    (SourceCorrection(root_id="", revision=2, blocking=True), "root_id must be a non-empty"),
    (SourceCorrection(root_id="   ", revision=2, blocking=True), "root_id must be a non-empty"),
    (SourceCorrection(root_id=ROOT_A, revision=0, blocking=True), "revision must be a positive"),
    (SourceCorrection(root_id=ROOT_A, revision=-2, blocking=True), "revision must be a positive"),
    (SourceCorrection(root_id=ROOT_A, revision=True, blocking=True), "revision must be a positive"),
    (SourceCorrection(root_id=ROOT_A, revision="2", blocking=True), "revision must be a positive"),
    (SourceCorrection(root_id=ROOT_A, revision=2, blocking="yes"), "blocking must be a boolean"),
    (SourceCorrection(root_id=ROOT_A, revision=2, blocking=1), "blocking must be a boolean"),
    (
        SourceCorrection(root_id=ROOT_A, revision=2, blocking=True, content_revision=3),
        "content_revision must be a non-negative integer",
    ),
    (
        SourceCorrection(root_id=ROOT_A, revision=2, blocking=True, content_revision=-1),
        "content_revision must be a non-negative integer",
    ),
    (
        SourceCorrection(root_id=ROOT_A, revision=2, blocking=True, content_revision=True),
        "content_revision must be a non-negative integer",
    ),
]


# --- correction_blocked -----------------------------------------------------


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"lifecycle_state": "active"},
        {CORRECTION_BLOCKERS_KEY: {}},
        {CORRECTION_BLOCKERS_KEY: None},
        # An empty container of any shape says the same thing: no exclusions.
        {CORRECTION_BLOCKERS_KEY: []},
    ],
)
def test_correction_blocked_reads_empty_state_as_unblocked(
    metadata: Mapping[str, object] | None,
) -> None:
    assert correction_blocked(metadata) is False


def test_correction_blocked_ignores_all_false_tombstones() -> None:
    metadata = blocker_state({ROOT_A: (3, False), ROOT_B: (7, False)})

    assert correction_blocked(metadata) is False


def test_correction_blocked_reports_any_blocking_root() -> None:
    metadata = blocker_state({ROOT_A: (3, False), ROOT_B: (7, True)})

    assert correction_blocked(metadata) is True


@pytest.mark.parametrize(("metadata", "_message"), MALFORMED_BLOCKER_STATES)
def test_correction_blocked_fails_closed_on_malformed_blockers(
    metadata: dict[str, object],
    _message: str,
) -> None:
    assert correction_blocked(metadata) is True


@pytest.mark.parametrize(("metadata", "_message"), MALFORMED_BINDING_STATES)
def test_correction_blocked_does_not_invent_an_exclusion_from_a_bad_binding(
    metadata: dict[str, object],
    _message: str,
) -> None:
    # A binding is provenance, not a verdict: nothing has excluded this row, so
    # recall says so. The refusal happens at propagation instead.
    assert correction_blocked(metadata) is False


# --- correction_event -------------------------------------------------------


def test_correction_event_reads_root_and_revision_from_persisted_capture() -> None:
    memory = SourceView(id=ROOT_A, revision=4)

    event = correction_event(memory, blocking=True)

    assert event == SourceCorrection(
        root_id=ROOT_A,
        revision=4,
        blocking=True,
        content_revision=UNKNOWN_SOURCE_REVISION,
    )


def test_correction_event_derives_content_revision_from_revise_history() -> None:
    memory = SourceView(
        revision=2,
        metadata={CORRECTION_HISTORY_KEY: [{"action": "revise", "prior_revision": 1}]},
    )

    event = correction_event(memory, blocking=False)

    assert (event.revision, event.content_revision, event.blocking) == (2, 2, False)


def test_correction_event_retains_content_revision_across_later_restore() -> None:
    memory = SourceView(
        revision=3,
        metadata={
            CORRECTION_HISTORY_KEY: [
                {"action": "revise", "prior_revision": 1},
                {"action": "restore", "prior_revision": 2},
            ]
        },
    )

    event = correction_event(memory, blocking=False)

    assert (event.revision, event.content_revision, event.blocking) == (3, 2, False)


def test_correction_event_takes_the_newest_revise_and_skips_unreadable_entries() -> None:
    memory = SourceView(
        revision=5,
        metadata={
            CORRECTION_HISTORY_KEY: [
                {"action": "revise", "prior_revision": 3},
                "revise",
                {"action": "hide", "prior_revision": 4},
                {"action": "REVISE", "prior_revision": 1},
            ]
        },
    )

    event = correction_event(memory, blocking=False)

    assert event.content_revision == 4


def test_correction_event_uses_current_revision_for_a_legacy_unstamped_revise() -> None:
    memory = SourceView(revision=6, metadata={CORRECTION_HISTORY_KEY: [{"action": "revise"}]})

    event = correction_event(memory, blocking=False)

    assert event.content_revision == 6


@pytest.mark.parametrize("prior_revision", [3, 4, 9])
def test_correction_event_rejects_an_impossible_future_revision_history(
    prior_revision: int,
) -> None:
    memory = SourceView(
        revision=3,
        metadata={CORRECTION_HISTORY_KEY: [{"action": "revise", "prior_revision": prior_revision}]},
    )

    with pytest.raises(ValueError, match="newer than the persisted capture"):
        correction_event(memory, blocking=False)


@pytest.mark.parametrize("prior_revision", [True, "2", -1, 1.5])
def test_correction_event_rejects_a_malformed_prior_revision(prior_revision: object) -> None:
    memory = SourceView(
        revision=4,
        metadata={CORRECTION_HISTORY_KEY: [{"action": "revise", "prior_revision": prior_revision}]},
    )

    with pytest.raises(ValueError, match="prior_revision must be a non-negative integer"):
        correction_event(memory, blocking=False)


def test_correction_event_rejects_a_history_that_is_not_a_list() -> None:
    memory = SourceView(revision=2, metadata={CORRECTION_HISTORY_KEY: {"action": "revise"}})

    with pytest.raises(ValueError, match="must be a list of correction entries"):
        correction_event(memory, blocking=False)


@pytest.mark.parametrize(
    ("memory", "message"),
    [
        (SourceView(id="", revision=2), "root_id must be a non-empty string"),
        (SourceView(id="  ", revision=2), "root_id must be a non-empty string"),
        (SourceView(revision=0), "revision must be a positive integer"),
        (SourceView(revision=True), "revision must be a positive integer"),
        (SourceView(revision="2"), "revision must be a positive integer"),
    ],
)
def test_correction_event_rejects_a_malformed_capture(memory: SourceView, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        correction_event(memory, blocking=True)


@pytest.mark.parametrize("blocking", [1, 0, "true", None])
def test_correction_event_rejects_non_boolean_blocking(blocking: object) -> None:
    with pytest.raises(ValueError, match="blocking must be a boolean"):
        correction_event(SourceView(revision=2), blocking=blocking)


# --- merge_source_correction: ordering ---------------------------------------


def test_merge_blocks_a_derived_row_on_a_hidden_root() -> None:
    merged = merge_source_correction({}, SourceCorrection(ROOT_A, 2, True))

    assert stored_entry(merged, ROOT_A) == {"revision": 2, "blocking": True}
    assert correction_blocked(merged) is True


def test_merge_clears_a_root_as_a_tombstone_not_a_deletion() -> None:
    hidden = merge_source_correction({}, SourceCorrection(ROOT_A, 2, True))

    restored = merge_source_correction(hidden, SourceCorrection(ROOT_A, 3, False))

    assert stored_entry(restored, ROOT_A) == {"revision": 3, "blocking": False}
    assert correction_blocked(restored) is False


def test_merge_keeps_the_restore_when_the_earlier_hide_arrives_last() -> None:
    restored = merge_source_correction({}, SourceCorrection(ROOT_A, 3, False))

    delayed = merge_source_correction(restored, SourceCorrection(ROOT_A, 2, True))

    assert stored_entry(delayed, ROOT_A) == {"revision": 3, "blocking": False}
    assert correction_blocked(delayed) is False


def test_merge_does_not_weaken_blocking_at_an_equal_revision() -> None:
    stored = blocker_state({ROOT_A: (2, True)})

    merged = merge_source_correction(stored, SourceCorrection(ROOT_A, 2, False))

    assert stored_entry(merged, ROOT_A) == {"revision": 2, "blocking": True}
    assert correction_blocked(merged) is True


def test_merge_strengthens_blocking_at_an_equal_revision() -> None:
    stored = blocker_state({ROOT_A: (2, False)})

    merged = merge_source_correction(stored, SourceCorrection(ROOT_A, 2, True))

    assert stored_entry(merged, ROOT_A) == {"revision": 2, "blocking": True}


# --- merge_source_correction: content epochs ---------------------------------


def test_merge_revise_then_restore_still_blocks_a_stale_binding() -> None:
    derived = {SOURCE_BINDINGS_KEY: {ROOT_A: 1}}
    revise = SourceCorrection(ROOT_A, 2, False, content_revision=2)
    restore = SourceCorrection(ROOT_A, 3, False, content_revision=2)

    merged = merge_source_correction(merge_source_correction(derived, revise), restore)

    assert stored_entry(merged, ROOT_A) == {"revision": 3, "blocking": True}
    assert correction_blocked(merged) is True


def test_merge_revise_still_blocks_a_stale_binding_when_it_arrives_after_restore() -> None:
    derived = {SOURCE_BINDINGS_KEY: {ROOT_A: 1}}
    restore = SourceCorrection(ROOT_A, 3, False, content_revision=2)
    revise = SourceCorrection(ROOT_A, 2, False, content_revision=2)

    merged = merge_source_correction(merge_source_correction(derived, restore), revise)

    assert stored_entry(merged, ROOT_A) == {"revision": 3, "blocking": True}
    assert correction_blocked(merged) is True


@pytest.mark.parametrize("bound_revision", [2, 3])
def test_merge_leaves_a_fresh_binding_clear_through_revise_and_restore(
    bound_revision: int,
) -> None:
    derived = {SOURCE_BINDINGS_KEY: {ROOT_A: bound_revision}}
    revise = SourceCorrection(ROOT_A, 2, False, content_revision=2)
    restore = SourceCorrection(ROOT_A, 3, False, content_revision=2)

    after_revise = merge_source_correction(derived, revise)
    after_restore = merge_source_correction(after_revise, restore)

    assert correction_blocked(after_revise) is False
    assert correction_blocked(after_restore) is False
    assert stored_entry(after_restore, ROOT_A) == {"revision": 3, "blocking": False}


def test_merge_blocks_a_row_with_no_binding_for_the_revised_root() -> None:
    derived = {SOURCE_BINDINGS_KEY: {ROOT_B: 9}}

    merged = merge_source_correction(derived, SourceCorrection(ROOT_A, 2, False, 2))

    assert stored_entry(merged, ROOT_A) == {"revision": 2, "blocking": True}


def test_merge_blocks_a_row_bound_to_an_unknown_epoch() -> None:
    derived = {SOURCE_BINDINGS_KEY: {ROOT_A: UNKNOWN_SOURCE_REVISION}}

    merged = merge_source_correction(derived, SourceCorrection(ROOT_A, 2, False, 2))

    assert stored_entry(merged, ROOT_A) == {"revision": 2, "blocking": True}


def test_merge_hides_a_fresh_binding_when_the_root_itself_left_recall() -> None:
    derived = {SOURCE_BINDINGS_KEY: {ROOT_A: 5}}

    merged = merge_source_correction(derived, SourceCorrection(ROOT_A, 6, True))

    assert correction_blocked(merged) is True


def test_merge_does_not_rebind_the_row_to_newer_content() -> None:
    derived = {SOURCE_BINDINGS_KEY: {ROOT_A: 1}}

    merged = merge_source_correction(derived, SourceCorrection(ROOT_A, 3, False, 2))

    assert merged[SOURCE_BINDINGS_KEY] == {ROOT_A: 1}


# --- merge_source_correction: independence -----------------------------------


def test_merge_keeps_other_roots_blocking_when_one_root_is_cleared() -> None:
    stored = blocker_state({ROOT_A: (2, True), ROOT_B: (4, True)})

    merged = merge_source_correction(stored, SourceCorrection(ROOT_A, 3, False))

    assert stored_entry(merged, ROOT_A) == {"revision": 3, "blocking": False}
    assert stored_entry(merged, ROOT_B) == {"revision": 4, "blocking": True}
    assert correction_blocked(merged) is True


def test_merge_runs_two_roots_on_independent_clocks() -> None:
    derived: Mapping[str, object] = {SOURCE_BINDINGS_KEY: {ROOT_A: 1, ROOT_B: 4}}

    # Root A: revise at 2 then restore at 3, arriving out of order.
    # Root B: hide at 5 then restore at 6, arriving in order.
    for event in (
        SourceCorrection(ROOT_A, 3, False, 2),
        SourceCorrection(ROOT_B, 5, True),
        SourceCorrection(ROOT_A, 2, False, 2),
        SourceCorrection(ROOT_B, 6, False),
    ):
        derived = merge_source_correction(derived, event)

    assert stored_entry(derived, ROOT_A) == {"revision": 3, "blocking": True}
    assert stored_entry(derived, ROOT_B) == {"revision": 6, "blocking": False}
    assert correction_blocked(derived) is True


def test_merge_preserves_unrelated_metadata() -> None:
    derived = {
        "content_sha256": "abc",
        RAW_SOURCE_IDS_KEY: [ROOT_A],
        "review_capture_id": "capture-1",
        SOURCE_BINDINGS_KEY: {ROOT_A: 2},
    }

    merged = merge_source_correction(derived, SourceCorrection(ROOT_A, 3, True))

    assert merged["content_sha256"] == "abc"
    assert merged[RAW_SOURCE_IDS_KEY] == [ROOT_A]
    assert merged["review_capture_id"] == "capture-1"
    assert merged[SOURCE_BINDINGS_KEY] == {ROOT_A: 2}


def test_merge_does_not_touch_reflection_identity() -> None:
    identity = {"version": 2, "source_ids": [ROOT_A]}
    derived = {IDENTITY_KEY: dict(identity)}

    merged = merge_source_correction(derived, SourceCorrection(ROOT_A, 3, True, 3))

    assert merged[IDENTITY_KEY] == identity


def test_merge_does_not_mutate_its_inputs() -> None:
    derived = {SOURCE_BINDINGS_KEY: {ROOT_A: 1}, **blocker_state({ROOT_B: (4, True)})}
    snapshot = copy.deepcopy(derived)
    event = SourceCorrection(ROOT_A, 3, False, 2)

    merged = merge_source_correction(derived, event)

    assert derived == snapshot
    assert event == SourceCorrection(ROOT_A, 3, False, 2)
    assert merged[CORRECTION_BLOCKERS_KEY] is not derived[CORRECTION_BLOCKERS_KEY]
    assert merged[SOURCE_BINDINGS_KEY] is not derived[SOURCE_BINDINGS_KEY]

    blockers = merged[CORRECTION_BLOCKERS_KEY]
    assert isinstance(blockers, dict)
    blockers[ROOT_B] = {"revision": 99, "blocking": False}

    assert derived == snapshot


# --- merge_source_correction: refusals ---------------------------------------


@pytest.mark.parametrize(
    ("metadata", "message"),
    [*MALFORMED_BLOCKER_STATES, *MALFORMED_BINDING_STATES],
)
def test_merge_refuses_malformed_stored_state(
    metadata: dict[str, object],
    message: str,
) -> None:
    snapshot = copy.deepcopy(metadata)

    with pytest.raises(ValueError, match=message):
        merge_source_correction(metadata, SourceCorrection(ROOT_A, 3, True))

    assert metadata == snapshot


@pytest.mark.parametrize(("event", "message"), MALFORMED_EVENTS)
def test_merge_refuses_a_malformed_event(event: SourceCorrection, message: str) -> None:
    derived = blocker_state({ROOT_B: (4, True)})
    snapshot = copy.deepcopy(derived)

    with pytest.raises(ValueError, match=message):
        merge_source_correction(derived, event)

    assert derived == snapshot


# --- local lifecycle stays local ---------------------------------------------


def test_merge_preserves_a_derived_rows_own_archived_verdict() -> None:
    archived = with_memory_lifecycle_metadata(
        {},
        MemoryLifecycle(
            state=MemoryLifecycleState.ARCHIVED,
            source_id="review-1",
            action="archive",
            reason="the reviewer retired it",
        ),
    )

    merged = merge_source_correction(archived, SourceCorrection(ROOT_A, 3, False))
    row = SourceView(id="review-1", revision=3, metadata=merged, source_id="review-1")

    assert memory_lifecycle_state(merged, source_id="review-1") == "archived"
    assert raw_memory_lifecycle_recallable(row) is False
    assert correction_blocked(merged) is False


def test_inherited_correction_state_does_not_forge_a_local_verdict() -> None:
    active = with_memory_lifecycle_metadata(
        {},
        MemoryLifecycle(
            state=MemoryLifecycleState.ACTIVE,
            source_id="review-1",
            action="promote",
            reason="promoted from a frozen episode",
        ),
    )

    merged = merge_source_correction(active, SourceCorrection(ROOT_A, 3, True))
    row = SourceView(id="review-1", revision=3, metadata=merged, source_id="review-1")

    assert memory_lifecycle_state(merged, source_id="review-1") == "active"
    assert raw_memory_lifecycle_recallable(row) is False
    assert correction_blocked(merged) is True


# --- source_revision_bindings ------------------------------------------------


def test_source_revision_bindings_binds_directly_observed_revisions() -> None:
    bindings = source_revision_bindings(
        [SourceView(id=ROOT_A, revision=3), SourceView(id=ROOT_B, revision=1)]
    )

    assert bindings == {ROOT_A: 3, ROOT_B: 1}


def test_source_revision_bindings_takes_the_minimum_over_overlapping_paths() -> None:
    left = SourceView(id="review-1", revision=2, metadata={SOURCE_BINDINGS_KEY: {ROOT_A: 5}})
    right = SourceView(
        id="review-2",
        revision=9,
        metadata={SOURCE_BINDINGS_KEY: {ROOT_A: 2, ROOT_B: 7}},
    )

    bindings = source_revision_bindings([left, right])

    assert bindings == {"review-1": 2, "review-2": 9, ROOT_A: 2, ROOT_B: 7}


def test_source_revision_bindings_is_order_independent() -> None:
    left = SourceView(id="review-1", revision=2, metadata={SOURCE_BINDINGS_KEY: {ROOT_A: 5}})
    right = SourceView(id="review-2", revision=9, metadata={SOURCE_BINDINGS_KEY: {ROOT_A: 2}})

    assert source_revision_bindings([left, right]) == source_revision_bindings([right, left])


def test_source_revision_bindings_marks_unbound_declared_support_unknown() -> None:
    review = SourceView(id="review-1", revision=4, metadata={RAW_SOURCE_IDS_KEY: [ROOT_A]})
    capture = SourceView(id=ROOT_A, revision=7)

    bindings = source_revision_bindings([review, capture])

    assert bindings == {"review-1": 4, ROOT_A: UNKNOWN_SOURCE_REVISION}


def test_source_revision_bindings_keeps_an_inherited_binding_for_declared_support() -> None:
    review = SourceView(
        id="review-1",
        revision=4,
        metadata={RAW_SOURCE_IDS_KEY: [ROOT_A, ROOT_B], SOURCE_BINDINGS_KEY: {ROOT_A: 3}},
    )
    capture = SourceView(id=ROOT_A, revision=7)

    bindings = source_revision_bindings([review, capture])

    assert bindings == {"review-1": 4, ROOT_A: 3, ROOT_B: UNKNOWN_SOURCE_REVISION}


def test_source_revision_bindings_reads_a_bare_declared_id_as_one_source() -> None:
    review = SourceView(id="review-1", revision=1, metadata={RAW_SOURCE_IDS_KEY: ROOT_A})

    bindings = source_revision_bindings([review])

    assert bindings == {"review-1": 1, ROOT_A: UNKNOWN_SOURCE_REVISION}


def test_source_revision_bindings_keeps_canonical_source_ids() -> None:
    anchor = "Anchor:Team-Norms_1"
    review = SourceView(id="review-1", revision=1, metadata={RAW_SOURCE_IDS_KEY: [anchor, "  "]})

    bindings = source_revision_bindings([review])

    assert bindings == {"review-1": 1, anchor: UNKNOWN_SOURCE_REVISION}


def test_source_revision_bindings_accepts_an_unsaved_zero_revision() -> None:
    bindings = source_revision_bindings([SourceView(id=ROOT_A, revision=0)])

    assert bindings == {ROOT_A: UNKNOWN_SOURCE_REVISION}


def test_source_revision_bindings_does_not_mutate_its_inputs() -> None:
    metadata = {SOURCE_BINDINGS_KEY: {ROOT_A: 3}, RAW_SOURCE_IDS_KEY: [ROOT_A, ROOT_B]}
    snapshot = copy.deepcopy(metadata)

    bindings = source_revision_bindings([SourceView(id="review-1", revision=2, metadata=metadata)])
    bindings[ROOT_A] = 99

    assert metadata == snapshot


@pytest.mark.parametrize(
    ("memory", "message"),
    [
        (SourceView(id="", revision=1), "observed source id must be a non-empty string"),
        (SourceView(id="  ", revision=1), "observed source id must be a non-empty string"),
        (SourceView(revision=-1), "observed source revision must be a non-negative integer"),
        (SourceView(revision=True), "observed source revision must be a non-negative integer"),
        (SourceView(revision="3"), "observed source revision must be a non-negative integer"),
        (
            SourceView(revision=1, metadata={SOURCE_BINDINGS_KEY: {ROOT_B: "3"}}),
            "source_bindings revision must be a non-negative integer",
        ),
        (
            SourceView(revision=1, metadata={SOURCE_BINDINGS_KEY: [ROOT_B]}),
            "source_bindings must be a mapping",
        ),
        (
            SourceView(revision=1, metadata={RAW_SOURCE_IDS_KEY: {ROOT_B: 1}}),
            "raw_source_ids must be a list of source id strings",
        ),
        (
            SourceView(revision=1, metadata={RAW_SOURCE_IDS_KEY: [ROOT_B, 7]}),
            "raw_source_ids entries must be strings",
        ),
    ],
)
def test_source_revision_bindings_refuses_malformed_provenance(
    memory: SourceView,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        source_revision_bindings([memory])


# --- the two halves together -------------------------------------------------


def test_an_old_unbound_review_is_not_rebound_to_newly_corrected_content() -> None:
    """The promotion-time failure this model exists for.

    A review written before bindings existed declares its support but records no
    epoch. Reading the corrected capture in the same batch must not let the
    review claim the revised text it never saw.
    """

    review = SourceView(id="review-1", revision=4, metadata={RAW_SOURCE_IDS_KEY: [ROOT_A]})
    capture = SourceView(
        id=ROOT_A,
        revision=3,
        metadata={CORRECTION_HISTORY_KEY: [{"action": "revise", "prior_revision": 1}]},
    )

    promoted: Mapping[str, object] = {
        RAW_SOURCE_IDS_KEY: [ROOT_A],
        SOURCE_BINDINGS_KEY: source_revision_bindings([review, capture]),
    }
    merged = merge_source_correction(promoted, correction_event(capture, blocking=False))

    assert promoted[SOURCE_BINDINGS_KEY] == {"review-1": 4, ROOT_A: UNKNOWN_SOURCE_REVISION}
    assert correction_blocked(merged) is True


def test_a_freshly_bound_promotion_survives_a_revise_and_restore_of_its_source() -> None:
    capture = SourceView(
        id=ROOT_A,
        revision=2,
        metadata={CORRECTION_HISTORY_KEY: [{"action": "revise", "prior_revision": 1}]},
    )
    restored = SourceView(
        id=ROOT_A,
        revision=3,
        metadata={
            CORRECTION_HISTORY_KEY: [
                {"action": "revise", "prior_revision": 1},
                {"action": "restore", "prior_revision": 2},
            ]
        },
    )
    promoted = {SOURCE_BINDINGS_KEY: source_revision_bindings([capture])}

    after_revise = merge_source_correction(promoted, correction_event(capture, blocking=False))
    after_restore = merge_source_correction(
        after_revise,
        correction_event(restored, blocking=False),
    )

    assert promoted[SOURCE_BINDINGS_KEY] == {ROOT_A: 2}
    assert correction_blocked(after_revise) is False
    assert correction_blocked(after_restore) is False
