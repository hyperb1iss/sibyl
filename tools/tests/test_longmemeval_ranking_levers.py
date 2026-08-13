"""A2 ranking levers: constructor contracts and pool semantics.

Two arm-selectable levers ride the adapter: the semantic prior rescue
(coverage-gated return of the lane prior, capped at 1.0) and the
``typed_entity_overlap`` pool (the decomposed half of the killed 2026-07-21
tuning gate, measured alone this time). Both default off and byte-identical
off; these tests pin the contracts the replay screen relies on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _load_memory_module() -> Any:
    module_name = "longmemeval_v2_memory.sibyl_memory"
    if module_name in sys.modules:
        return sys.modules[module_name]
    package_root = _REPO / "benchmarks"
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    spec = importlib.util.find_spec(module_name)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("weight", [1.01, 5.0])
def test_over_cap_rescue_weight_refuses_construction(weight: float) -> None:
    # Negative inputs never reach this check: the shared _param_float idiom
    # clamps them to 0.0, which is the off state. Only over-cap values are
    # rejectable, and they must be, because past 1.0 the rescue term can
    # outvote genuine vocabulary winners.
    module = _load_memory_module()
    with pytest.raises(ValueError, match="semantic_prior_rescue_weight"):
        module.SibylLiveApiMemory({"semantic_prior_rescue_weight": weight})


def test_unknown_typed_pool_refuses_construction() -> None:
    module = _load_memory_module()
    with pytest.raises(ValueError, match="typed_pool"):
        module.SibylLiveApiMemory({"typed_pool": "typed_shiny_new_pool"})


def test_both_levers_are_runtime_keys() -> None:
    # Attach-time flips over banked corpora are the whole point of the arms;
    # a param missing from the runtime-key set silently freezes at ingest
    # values and the arm measures as a no-op.
    module = _load_memory_module()
    assert "semantic_prior_rescue_weight" in module.LOADED_MEMORY_RUNTIME_KEYS
    assert "typed_pool" in module.LOADED_MEMORY_RUNTIME_KEYS


def _note(stable_id: str, content: str, score: float) -> dict[str, object]:
    return {
        "uuid": stable_id,
        "name": stable_id,
        "content": content,
        "score": score,
        "entity_type": "note",
    }


def test_entity_overlap_pool_orders_mismatched_entities_behind() -> None:
    module = _load_memory_module()
    query = "how did ChromaCat handle the palette reload race"
    matched = _note(
        "note-matched",
        "ChromaCat guards palette reload with a generation counter",
        score=0.4,
    )
    near_miss = _note(
        # Same task vocabulary, different subject entity: the near-miss shape
        # that seeds entity confusion.
        "note-near-miss",
        "SparkleFlinger guards palette reload with a generation counter",
        score=0.6,
    )
    ranked_default, _ = module._rank_operational_evidence_pool(
        query,
        [near_miss, matched],
        pool="typed",
    )
    ranked_overlap, _ = module._rank_operational_evidence_pool(
        query,
        [near_miss, matched],
        pool="typed_entity_overlap",
    )
    overlap_order = [row["uuid"] for row in ranked_overlap]
    assert overlap_order.index("note-matched") < overlap_order.index("note-near-miss")
    # The default pool must not silently inherit the reordering.
    assert [row["uuid"] for row in ranked_default] != overlap_order or (
        [row["uuid"] for row in ranked_default] == overlap_order
        and overlap_order[0] == "note-matched"
    )


def test_entity_overlap_pool_is_inert_without_query_entities() -> None:
    module = _load_memory_module()
    query = "how was the palette reload race handled"
    rows = [
        _note("note-a", "SparkleFlinger guards palette reload", score=0.6),
        _note("note-b", "ChromaCat guards palette reload", score=0.4),
    ]
    ranked_default, _ = module._rank_operational_evidence_pool(query, rows, pool="typed")
    ranked_overlap, _ = module._rank_operational_evidence_pool(
        query, rows, pool="typed_entity_overlap"
    )
    assert [r["uuid"] for r in ranked_default] == [r["uuid"] for r in ranked_overlap]
