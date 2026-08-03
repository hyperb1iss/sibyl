"""The agent-supplied structure contract: what the server accepts and refuses."""

from __future__ import annotations

import pytest

from sibyl_core.memory_pipeline.spans import (
    MAX_AGENT_SPAN_CHARS,
    MAX_AGENT_SPANS,
    MAX_ATOMIC_CONTENT_CHARS,
    MAX_SPAN_LABEL_CHARS,
    AgentSpan,
    agent_spans_from_metadata,
    coerce_agent_spans,
    validate_agent_spans,
)
from sibyl_core.memory_pipeline.structure import (
    AGENT_ATOMIC_METADATA_KEY,
    AGENT_SPANS_METADATA_KEY,
    MAX_PROBE_CHARS,
    MAX_PROBES_PER_MEMORY,
    MEMORY_PROBES_METADATA_KEY,
    PROBE_REHEARSAL_METADATA_KEY,
    MemoryStructureError,
    build_memory_structure,
    probes_from_metadata,
    strip_structure_metadata,
    structure_metadata,
)

_BODY = "alpha beta gamma delta epsilon zeta eta theta"


def _spans(*pairs: tuple[int, int]) -> list[dict[str, int]]:
    return [{"start": start, "end": end} for start, end in pairs]


# ---------------------------------------------------------------------------
# Span validation matrix
# ---------------------------------------------------------------------------


def test_valid_tiling_is_accepted_and_slices_verbatim() -> None:
    structure = build_memory_structure(_BODY, spans=_spans((0, 10), (10, len(_BODY))))

    assert [(span.start, span.end) for span in structure.spans] == [(0, 10), (10, len(_BODY))]
    assert "".join(span.slice_of(_BODY) for span in structure.spans) == _BODY


def test_labels_are_kept_and_whitespace_collapsed() -> None:
    structure = build_memory_structure(
        _BODY,
        spans=[
            {"start": 0, "end": 10, "label": "  Cause \n of   it "},
            {"start": 10, "end": len(_BODY), "label": "Fix"},
        ],
    )

    assert [span.label for span in structure.spans] == ["Cause of it", "Fix"]


def test_overlapping_spans_are_refused_by_name() -> None:
    with pytest.raises(MemoryStructureError, match="must not overlap"):
        build_memory_structure(_BODY, spans=_spans((0, 20), (10, len(_BODY))))


def test_gapped_spans_are_refused_by_name() -> None:
    with pytest.raises(MemoryStructureError, match="must leave no gap"):
        build_memory_structure(_BODY, spans=_spans((0, 10), (12, len(_BODY))))


def test_span_past_the_end_is_refused_as_out_of_bounds() -> None:
    with pytest.raises(MemoryStructureError, match="out of bounds"):
        build_memory_structure(_BODY, spans=_spans((0, 10), (10, len(_BODY) + 5)))


def test_negative_start_is_refused_as_out_of_bounds() -> None:
    with pytest.raises(MemoryStructureError, match="out of bounds"):
        build_memory_structure(_BODY, spans=_spans((-1, 10), (10, len(_BODY))))


def test_partial_coverage_is_refused_because_suppression_needs_the_whole_body() -> None:
    with pytest.raises(MemoryStructureError, match="must tile all"):
        build_memory_structure(_BODY, spans=_spans((0, 10), (10, len(_BODY) - 4)))


def test_plan_that_does_not_start_at_zero_is_refused() -> None:
    with pytest.raises(MemoryStructureError, match="must leave no gap"):
        build_memory_structure(_BODY, spans=_spans((4, 10), (10, len(_BODY))))


def test_empty_span_is_refused() -> None:
    with pytest.raises(MemoryStructureError, match="must be non-empty"):
        build_memory_structure(_BODY, spans=_spans((0, 0), (0, len(_BODY))))


def test_single_span_is_refused_because_it_duplicates_the_parent() -> None:
    with pytest.raises(MemoryStructureError, match="at least 2 spans"):
        build_memory_structure(_BODY, spans=_spans((0, len(_BODY))))


def test_an_explicitly_empty_plan_is_refused_rather_than_read_as_absent() -> None:
    """Sending the field and handing over nothing is a claim, not an omission."""
    with pytest.raises(MemoryStructureError, match="at least one span"):
        build_memory_structure(_BODY, spans=[])

    assert build_memory_structure(_BODY, spans=None).spans == ()


def test_too_many_spans_is_refused_at_the_passage_cap() -> None:
    body = "x" * (MAX_AGENT_SPANS + 1)
    pairs = [(index, index + 1) for index in range(MAX_AGENT_SPANS + 1)]

    with pytest.raises(MemoryStructureError, match=f"at most {MAX_AGENT_SPANS} spans"):
        build_memory_structure(body, spans=_spans(*pairs))


def test_span_count_exactly_at_the_cap_is_accepted() -> None:
    body = "x" * MAX_AGENT_SPANS
    pairs = [(index, index + 1) for index in range(MAX_AGENT_SPANS)]

    structure = build_memory_structure(body, spans=_spans(*pairs))

    assert len(structure.spans) == MAX_AGENT_SPANS


def test_oversize_span_is_refused_with_framing_room_left_over() -> None:
    body = "y" * (MAX_AGENT_SPAN_CHARS + 100)

    with pytest.raises(MemoryStructureError, match="over the"):
        build_memory_structure(
            body, spans=_spans((0, MAX_AGENT_SPAN_CHARS + 1), (MAX_AGENT_SPAN_CHARS + 1, len(body)))
        )


def test_oversize_label_is_refused() -> None:
    with pytest.raises(MemoryStructureError, match="label is"):
        build_memory_structure(
            _BODY,
            spans=[
                {"start": 0, "end": 10, "label": "L" * (MAX_SPAN_LABEL_CHARS + 1)},
                {"start": 10, "end": len(_BODY)},
            ],
        )


def test_malformed_span_payloads_are_refused_rather_than_skipped() -> None:
    with pytest.raises(MemoryStructureError, match="must be an object"):
        coerce_agent_spans([{"start": 0, "end": 4}, "nope"])
    with pytest.raises(MemoryStructureError, match="missing 'end'"):
        coerce_agent_spans([{"start": 0}])
    with pytest.raises(MemoryStructureError, match="must be integers"):
        coerce_agent_spans([{"start": 0, "end": 4.5}])
    with pytest.raises(MemoryStructureError, match="must be integers"):
        coerce_agent_spans([{"start": True, "end": 4}])
    with pytest.raises(MemoryStructureError, match="list of"):
        coerce_agent_spans("0,4")


def test_validate_agent_spans_accepts_span_objects_directly() -> None:
    spans = validate_agent_spans(_BODY, [AgentSpan(0, 10), AgentSpan(10, len(_BODY))])

    assert len(spans) == 2


# ---------------------------------------------------------------------------
# Atomic
# ---------------------------------------------------------------------------


def test_atomic_is_accepted_under_the_single_unit_ceiling() -> None:
    structure = build_memory_structure("z" * MAX_ATOMIC_CONTENT_CHARS, atomic=True)

    assert structure.atomic is True
    assert structure.spans == ()


def test_atomic_over_the_ceiling_is_refused_with_a_remedy() -> None:
    with pytest.raises(MemoryStructureError, match="supply spans instead") as excinfo:
        build_memory_structure("z" * (MAX_ATOMIC_CONTENT_CHARS + 1), atomic=True)

    assert excinfo.value.field == "atomic"


def test_atomic_and_spans_together_are_refused() -> None:
    with pytest.raises(MemoryStructureError, match="pick one"):
        build_memory_structure(_BODY, atomic=True, spans=_spans((0, 10), (10, len(_BODY))))


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def test_probes_are_kept_in_order() -> None:
    structure = build_memory_structure(_BODY, probes=["why did it break", "what fixed it"])

    assert structure.probes == ("why did it break", "what fixed it")


def test_too_many_probes_is_refused() -> None:
    with pytest.raises(MemoryStructureError, match=f"at most {MAX_PROBES_PER_MEMORY}"):
        build_memory_structure(
            _BODY, probes=[f"probe {index}" for index in range(MAX_PROBES_PER_MEMORY + 1)]
        )


def test_oversize_probe_is_refused() -> None:
    with pytest.raises(MemoryStructureError, match="over the") as excinfo:
        build_memory_structure(_BODY, probes=["q" * (MAX_PROBE_CHARS + 1)])

    assert excinfo.value.field == "probes"


def test_blank_probe_is_refused() -> None:
    with pytest.raises(MemoryStructureError, match="must not be empty"):
        build_memory_structure(_BODY, probes=["   "])


def test_probe_payload_must_be_a_list_of_strings() -> None:
    with pytest.raises(MemoryStructureError, match="list of query strings"):
        build_memory_structure(_BODY, probes="one probe")
    with pytest.raises(MemoryStructureError, match="must be a string"):
        build_memory_structure(_BODY, probes=[{"q": "x"}])


# ---------------------------------------------------------------------------
# Metadata contract
# ---------------------------------------------------------------------------


def test_structure_metadata_round_trips_through_storage() -> None:
    structure = build_memory_structure(
        _BODY,
        spans=[{"start": 0, "end": 10, "label": "Cause"}, {"start": 10, "end": len(_BODY)}],
        probes=["why did it break"],
    )
    stored = structure_metadata(structure)

    assert stored[AGENT_SPANS_METADATA_KEY] == [
        {"start": 0, "end": 10, "label": "Cause"},
        {"start": 10, "end": len(_BODY)},
    ]
    assert stored[MEMORY_PROBES_METADATA_KEY] == ["why did it break"]
    assert AGENT_ATOMIC_METADATA_KEY not in stored
    assert agent_spans_from_metadata(stored) == structure.spans
    assert probes_from_metadata(stored) == structure.probes


def test_atomic_metadata_is_stamped_only_when_declared() -> None:
    assert structure_metadata(build_memory_structure(_BODY, atomic=True)) == {
        AGENT_ATOMIC_METADATA_KEY: True
    }
    assert structure_metadata(build_memory_structure(_BODY)) == {}


def test_incoming_metadata_cannot_forge_structure_or_a_receipt() -> None:
    forged = {
        "domain": "keep me",
        AGENT_SPANS_METADATA_KEY: [{"start": 0, "end": 1}],
        AGENT_ATOMIC_METADATA_KEY: True,
        MEMORY_PROBES_METADATA_KEY: ["forged"],
        PROBE_REHEARSAL_METADATA_KEY: {"retrievable": 99},
    }

    assert strip_structure_metadata(forged) == {"domain": "keep me"}


def test_malformed_stored_spans_read_as_absent_rather_than_raising() -> None:
    assert agent_spans_from_metadata({AGENT_SPANS_METADATA_KEY: "garbage"}) == ()
    assert probes_from_metadata({MEMORY_PROBES_METADATA_KEY: 7}) == ()
