"""Pin the span contract's restated caps to the constants they mirror.

``memory_pipeline.spans`` is a leaf module: the projection reads it and the
capture pipeline writes it, and importing either package from there closes a
cycle through the graph runtime. So its caps restate their sources as literals,
and these assertions are what keep the two copies from drifting apart.
"""

from __future__ import annotations

from sibyl_core.memory_pipeline.spans import (
    MAX_AGENT_SPAN_CHARS,
    MAX_AGENT_SPANS,
    MAX_ATOMIC_CONTENT_CHARS,
    MAX_PASSAGE_CONTENT_CHARS,
    MAX_PASSAGE_TITLE_CHARS,
    MAX_PASSAGES_PER_SOURCE,
    MAX_SPAN_FRAMING_CHARS,
    MAX_SPAN_LABEL_CHARS,
)
from sibyl_core.memory_pipeline.structure import MAX_PROBE_CHARS
from sibyl_core.projection.slicing import HEADER_MAX_CHARS
from sibyl_core.retrieval.refinement import MAX_REFINEMENT_QUERY_CHARS
from sibyl_core.tools.helpers import MAX_TITLE_LENGTH


def test_label_cap_matches_the_slice_header_cap() -> None:
    assert MAX_SPAN_LABEL_CHARS == HEADER_MAX_CHARS


def test_title_cap_matches_the_add_title_cap() -> None:
    assert MAX_PASSAGE_TITLE_CHARS == MAX_TITLE_LENGTH


def test_probe_cap_matches_the_refinement_query_cap() -> None:
    assert MAX_PROBE_CHARS == MAX_REFINEMENT_QUERY_CHARS


def test_span_cap_leaves_room_for_every_possible_framing() -> None:
    """An accepted span must render inside one passage row, framing included."""
    worst_case_header = MAX_PASSAGE_TITLE_CHARS + len(" · passage 64/64")
    assert worst_case_header + MAX_SPAN_LABEL_CHARS + 2 <= MAX_SPAN_FRAMING_CHARS
    assert MAX_AGENT_SPAN_CHARS + MAX_SPAN_FRAMING_CHARS == MAX_PASSAGE_CONTENT_CHARS


def test_span_count_cap_is_the_passage_cap() -> None:
    assert MAX_AGENT_SPANS == MAX_PASSAGES_PER_SOURCE


def test_atomic_ceiling_is_one_passage_row() -> None:
    assert MAX_ATOMIC_CONTENT_CHARS == MAX_PASSAGE_CONTENT_CHARS


def test_the_projection_still_reads_its_caps_from_the_contract() -> None:
    from sibyl_core.projection import passages

    assert passages.MAX_PASSAGE_CONTENT_CHARS == MAX_PASSAGE_CONTENT_CHARS
    assert passages.MAX_PASSAGES_PER_SOURCE == MAX_PASSAGES_PER_SOURCE


def test_snapshot_shadowed_keys_are_exactly_the_server_owned_ones() -> None:
    """The storage layer restates the key set, so it has to stay in step.

    A row keeps its metadata twice, and the graph reader refuses to read these
    keys from the stale JSON snapshot because their absence is what carries the
    meaning. A key added to the contract without being added there would come
    back from the snapshot after being withdrawn.
    """
    from sibyl_core.memory_pipeline.structure import STRUCTURE_METADATA_KEYS
    from sibyl_core.services.graph import _SNAPSHOT_SHADOWED_METADATA_KEYS

    assert _SNAPSHOT_SHADOWED_METADATA_KEYS == STRUCTURE_METADATA_KEYS
