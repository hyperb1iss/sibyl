"""Separate source comparison from the pinned materializer's retrieval fields.

The checkpoint producer also binds complete native source snapshots. Removing a
materializer field here therefore cannot conceal edits to a same-named authored
field in source metadata. Public returned metadata is retained without this split
in the model block and diagnostic receipt (original raw blocks stay matched).
"""

from __future__ import annotations

from benchmarks.agent_tasks.screen48.contract import sha

from sibyl_core.memory_pipeline.source_lifecycle import public_memory_metadata
from sibyl_core.migrate.source_integrity import encode_record
from sibyl_core.retrieval._search_candidates import _GRAPH_EXPANSION_METADATA_KEYS

# These are the complete query/runtime-owned fields in _search_fusion's result
# materializer and _search_candidates/_search_sources/_search_expansion. The
# source manifest pins those owners; unknown public metadata remains evidence.
RETRIEVAL_FIELDS = frozenset(
    {
        "freshness",
        "retrieval_signals",
        "retrieval_ranks",
        "retrieval_scores",
        "vector_only_demoted",
        "filter_selectivity",
        "vector_only_demote_multiplier",
        "graph_expansion_only_demoted",
        "graph_expansion_only_multiplier",
        "graph_native_signal_boost",
        "exact_key_boost",
        "matched_retrieval_keys",
        "temporal_decay_multiplier",
        "embedding_metadata",
        *_GRAPH_EXPANSION_METADATA_KEYS,
    }
)


def _metadata_fields(metadata):
    encoded = encode_record(public_memory_metadata(metadata))
    fields = {"public_metadata": encoded["record"]}
    if encoded["datetimes"]:
        fields["public_metadata_datetimes"] = encoded["datetimes"]
    return fields


def native_evidence(result):
    metadata = {k: v for k, v in result.metadata.items() if k not in RETRIEVAL_FIELDS}
    return {
        "type": result.type,
        "id": result.id,
        "name": result.name,
        "content_sha256": sha(result.content.encode()),
        "source": result.source,
        "url": result.url,
        "result_origin": result.result_origin,
        "source_revision": result.source_revision,
        **_metadata_fields(metadata),
    }


def returned_native_evidence(result):
    evidence = native_evidence(result)
    evidence.pop("public_metadata_datetimes", None)
    return {**evidence, "score": result.score, **_metadata_fields(result.metadata)}
