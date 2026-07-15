"""Sibyl live-API memory backend for the official LongMemEval-V2 harness."""

from __future__ import annotations

import gzip
import hashlib
import io
import itertools
import json as json_module
import math
import os
import re
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "packages" / "python" / "sibyl-core" / "src"
CLI_SRC = ROOT / "apps" / "cli" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))
if str(CLI_SRC) not in sys.path:
    sys.path.insert(0, str(CLI_SRC))

from sibyl_core.evals.longmemeval_v2 import (  # noqa: E402
    LongMemEvalV2State,
    LongMemEvalV2Trajectory,
)
from sibyl_core.retrieval.query_ranking import (  # noqa: E402
    QueryCoverageCandidate,
    rank_by_query_coverage,
)

try:
    from memory_modules.memory import Memory, MemoryContextItem, register_memory
except ModuleNotFoundError:
    MemoryContextItem = dict[str, str]  # type: ignore[misc,assignment]

    class Memory:  # type: ignore[no-redef]
        memory_type = ""

        def __init__(self, memory_params: dict[str, object]) -> None:
            self.memory_params = dict(memory_params)
            self._query_context = {}

        def set_query_context(self, **kwargs: object) -> None:
            self._query_context = dict(kwargs)

        def clear_query_context(self) -> None:
            self._query_context = {}

        def get_query_context(self) -> dict[str, object]:
            return dict(self._query_context)

    def register_memory(memory_cls: type[Memory]) -> type[Memory]:
        return memory_cls


DEFAULT_API_URL = "http://127.0.0.1:3334/api"
DEFAULT_CONTENT_MAX_CHARS = 18_000
DEFAULT_SEARCH_LIMIT = 12
DEFAULT_CONTEXT_ITEMS = 8
DEFAULT_CONTEXT_CHARS_PER_ITEM = 18_000
DEFAULT_API_TIMEOUT_SECONDS = 600.0
DEFAULT_API_RETRY_ATTEMPTS = 3
DEFAULT_API_RETRY_BASE_DELAY_SECONDS = 2.0
DEFAULT_API_RETRY_MAX_DELAY_SECONDS = 30.0
DEFAULT_EMBEDDING_JOB_WAIT_TIMEOUT_SECONDS = 1_800.0
DEFAULT_EMBEDDING_JOB_POLL_SECONDS = 0.5
DEFAULT_BULK_MAX_ENTITIES = 32
DEFAULT_BULK_MAX_CONTENT_CHARS = 512_000
DEFAULT_EMBEDDING_BACKFILL_MAX_PENDING_JOBS = 8
DEFAULT_MAX_CHUNKS_PER_TRAJECTORY = 2
DEFAULT_NEIGHBOR_STITCH_ITEMS = 2
DEFAULT_NEIGHBOR_STITCH_SPAN = 1
DEFAULT_STATE_PART_COMPLETION_ITEMS = 0
DEFAULT_STATE_PART_REFINEMENT = False
DEFAULT_CONTEXT_EXPANSION_MAX_RATIO = 0.0
CONTEXT_TOKENIZER_MODEL = "Qwen/Qwen3.5-9B"
STATE_PART_REFINEMENT_MIN_SCORE_GAIN = 0.05
DEFAULT_CHUNKING_MODE = "state"
CHUNKING_MODES = frozenset({"trajectory", "state"})
JOB_STATUS_BATCH_SIZE = 64
MAX_BULK_CREATE = 128
CHUNK_CATALOG_FILENAME = "chunk_catalog.jsonl.gz"
MEMORY_MANIFEST_FILENAME = "memory_manifest.json"
MEMORY_MANIFEST_SCHEMA_VERSION = "sibyl-longmemeval-v2-memory-state-v1"
CHECKPOINT_CATALOG_FILENAME = "checkpoint_catalog.jsonl"
CHECKPOINT_MANIFEST_FILENAME = "checkpoint_manifest.json"
CHECKPOINT_SCHEMA_VERSION = "sibyl-longmemeval-v2-ingest-checkpoint-v1"
RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 409, 425, 429})
SAVED_MEMORY_SECRET_KEYS = frozenset({"api_token", "email", "password"})
LOADED_MEMORY_RUNTIME_KEYS = frozenset(
    {
        *SAVED_MEMORY_SECRET_KEYS,
        "allow_localhost",
        "allow_signup",
        "api_timeout_seconds",
        "api_retry_attempts",
        "api_retry_base_delay_seconds",
        "api_retry_max_delay_seconds",
        "search_limit",
        "max_context_items",
        "max_context_chars_per_item",
        "max_chunks_per_trajectory",
        "neighbor_stitch_items",
        "neighbor_stitch_span",
        "state_part_completion_items",
        "state_part_refinement",
        "context_expansion_max_ratio",
        "checkpoint_dir",
    }
)
SAVED_MEMORY_IDENTITY_KEYS = frozenset(
    {
        "api_url",
        "project_id",
        "run_id",
        "content_max_chars",
        "chunking_mode",
        "include_screenshot_refs",
        "defer_embeddings",
    }
)

_AUTH_CACHE: dict[tuple[str, str, str], dict[str, str]] = {}
_AUTH_LOCK = threading.Lock()
_INSTANCE_COUNTER = itertools.count(1)
_CONTEXT_PROCESSOR_LOCAL = threading.local()


class TrajectoryTextChunk:
    __slots__ = (
        "content",
        "state_index",
        "state_indices",
        "state_part_count",
        "state_part_index",
    )

    def __init__(
        self,
        *,
        content: str,
        state_index: int,
        state_indices: tuple[int, ...] | None = None,
        state_part_index: int,
        state_part_count: int,
    ) -> None:
        self.content = content
        self.state_index = state_index
        self.state_indices = state_indices or (state_index,)
        self.state_part_index = state_part_index
        self.state_part_count = state_part_count


def _string_key_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def build_entity_payloads_for_trajectory(
    trajectory_raw: dict[str, object],
    *,
    project_id: str,
    run_id: str,
    content_max_chars: int = DEFAULT_CONTENT_MAX_CHARS,
    chunking_mode: str = DEFAULT_CHUNKING_MODE,
    include_screenshot_refs: bool = False,
) -> list[dict[str, object]]:
    trajectory = LongMemEvalV2Trajectory.from_mapping(trajectory_raw)
    if chunking_mode not in CHUNKING_MODES:
        msg = f"Unknown chunking_mode {chunking_mode!r}; expected one of {sorted(CHUNKING_MODES)}"
        raise ValueError(msg)
    chunk_builder = _grouped_trajectory_chunks if chunking_mode == "trajectory" else _trajectory_chunks
    chunks = chunk_builder(
        trajectory,
        max_chars=content_max_chars,
        include_screenshot_refs=include_screenshot_refs,
    )
    payloads: list[dict[str, object]] = []
    for chunk_index, chunk in enumerate(chunks):
        payloads.append(
            {
                "name": _entity_name(trajectory.id, chunk_index, len(chunks)),
                "description": f"{trajectory.goal} ({trajectory.outcome})",
                "content": chunk.content,
                "entity_type": "session",
                "skip_conflicts": True,
                "metadata": {
                    "project_id": project_id,
                    "longmemeval_v2_run_id": run_id,
                    "longmemeval_v2_trajectory_id": trajectory.id,
                    "longmemeval_v2_chunk_index": chunk_index,
                    "longmemeval_v2_chunk_count": len(chunks),
                    "longmemeval_v2_state_index": chunk.state_index,
                    "longmemeval_v2_state_indices": list(chunk.state_indices),
                    "longmemeval_v2_state_part_index": chunk.state_part_index,
                    "longmemeval_v2_state_part_count": chunk.state_part_count,
                    "longmemeval_v2_domain": trajectory.domain,
                    "longmemeval_v2_environment": trajectory.environment,
                    "longmemeval_v2_goal": trajectory.goal,
                    "longmemeval_v2_outcome": trajectory.outcome,
                    "capture_surface": "longmemeval-v2-official",
                    "longmemeval_v2_chunking_mode": chunking_mode,
                    "entity_content_projection_policy": (
                        "v2-trajectory-state-chunks-v1"
                        if chunking_mode == "trajectory"
                        else "v2-identity-state-chunks-v2"
                    ),
                },
                "tags": ["longmemeval-v2", trajectory.domain, trajectory.environment],
            }
        )
    return payloads


def build_operational_experience_payload(
    trajectory_raw: dict[str, object],
    *,
    project_id: str,
    run_id: str,
    content_max_chars: int = DEFAULT_CONTENT_MAX_CHARS,
    include_screenshot_refs: bool = False,
) -> dict[str, object]:
    trajectory = LongMemEvalV2Trajectory.from_mapping(trajectory_raw)
    header = _trajectory_header(trajectory)
    observations: list[dict[str, object]] = []
    chunk_index = 0
    for state in trajectory.states:
        chunks = _state_chunks(
            header,
            state,
            max_chars=content_max_chars,
            include_screenshot_refs=include_screenshot_refs,
        )
        evidence: list[dict[str, object]] = []
        for chunk in chunks:
            evidence.append(
                {
                    "id": f"chunk-{chunk_index}",
                    "content": chunk.content,
                    "content_type": "text/plain; profile=accessibility-tree",
                    "metadata": {
                        "longmemeval_v2_chunk_index": chunk_index,
                        "longmemeval_v2_state_index": state.state_index,
                        "longmemeval_v2_state_part_index": chunk.state_part_index,
                        "longmemeval_v2_state_part_count": chunk.state_part_count,
                    },
                }
            )
            chunk_index += 1
        observations.append(
            {
                "id": f"state-{state.state_index}",
                "ordinal": state.state_index,
                "uri": state.url,
                "action": state.action,
                "reasoning": state.thought,
                "evidence": evidence,
                "image_refs": [state.screenshot]
                if include_screenshot_refs and state.screenshot
                else [],
                "metadata": {"longmemeval_v2_state_index": state.state_index},
            }
        )
    return {
        "experience": {
            "source_id": f"longmemeval-v2:{run_id}:{trajectory.id}",
            "goal": trajectory.goal,
            "outcome": trajectory.outcome,
            "start_uri": trajectory.start_url,
            "observations": observations,
            "project_id": project_id,
            "metadata": {
                "longmemeval_v2_run_id": run_id,
                "longmemeval_v2_trajectory_id": trajectory.id,
                "longmemeval_v2_domain": trajectory.domain,
                "longmemeval_v2_environment": trajectory.environment,
                "capture_surface": "longmemeval-v2-official",
            },
        }
    }


def context_pack_to_search_results(
    response: dict[str, object],
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    sections = response.get("sections")
    if not isinstance(sections, list):
        return candidates
    for section in sections:
        if not isinstance(section, dict):
            continue
        facet = _stripped_str(section.get("facet")) or "context"
        items = section.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = _stripped_str(item.get("type"))
            if item_type not in {"procedure", "error_pattern", "event"}:
                continue
            candidate = _string_key_dict(item)
            candidate["_selection_origin"] = f"context_pack:{facet}"
            candidates.append(candidate)
    type_order = {"procedure": 0, "error_pattern": 1, "event": 2}
    return sorted(
        candidates,
        key=lambda item: (
            type_order.get(_stripped_str(item.get("type")), 3),
            -float(item.get("score", 0.0))
            if isinstance(item.get("score"), int | float)
            else 0.0,
        ),
    )


def _flatten_operational_result_metadata(
    results: list[dict[str, object]],
) -> list[dict[str, object]]:
    flattened: list[dict[str, object]] = []
    for result in results:
        candidate = dict(result)
        metadata = _string_key_dict(candidate.get("metadata"))
        for nested_key in ("source_metadata", "evidence_metadata"):
            nested = metadata.get(nested_key)
            if not isinstance(nested, dict):
                continue
            for key, value in nested.items():
                metadata.setdefault(str(key), value)
        candidate["metadata"] = metadata
        flattened.append(candidate)
    return flattened


def compile_operational_evidence_set(
    *,
    typed_results: list[dict[str, object]],
    raw_results: list[dict[str, object]],
    max_items: int,
) -> list[dict[str, object]]:
    max_items = max(1, max_items)
    typed_budget = min(3, max(1, max_items // 3))
    selected: list[dict[str, object]] = []
    seen_typed: set[tuple[str, str]] = set()
    for result in typed_results:
        metadata = _string_key_dict(result.get("metadata"))
        key = (
            _stripped_str(result.get("type")),
            _stripped_str(metadata.get("longmemeval_v2_trajectory_id")),
        )
        if key in seen_typed:
            continue
        selected.append(result)
        seen_typed.add(key)
        if len(selected) >= typed_budget:
            break
    selected_ids = {_stripped_str(result.get("id")) for result in selected}
    for result in raw_results:
        result_id = _stripped_str(result.get("id"))
        if result_id and result_id in selected_ids:
            continue
        selected.append(result)
        selected_ids.add(result_id)
        if len(selected) >= max_items:
            break
    return selected


def search_results_to_memory_context(
    results: list[dict[str, object]],
    *,
    max_items: int = DEFAULT_CONTEXT_ITEMS,
    max_chars_per_item: int = DEFAULT_CONTEXT_CHARS_PER_ITEM,
) -> list[MemoryContextItem]:
    context: list[MemoryContextItem] = []
    for rank, result in enumerate(results[:max_items], start=1):
        content = _stripped_str(result.get("content"))
        if not content:
            continue
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        trajectory_id = _stripped_str(metadata.get("longmemeval_v2_trajectory_id"))
        chunk_index = metadata.get("longmemeval_v2_chunk_index")
        score = result.get("score")
        selection_origin = _stripped_str(result.get("_selection_origin")) or "search"
        header = [
            f"Retrieved evidence rank {rank}",
            f"Retrieval: {selection_origin}",
            f"Trajectory: {trajectory_id or 'unknown'}",
            f"Chunk: {chunk_index if isinstance(chunk_index, int) else 'unknown'}",
            f"Score: {score if isinstance(score, int | float) else 'unknown'}",
        ]
        context.append(
            {
                "type": "text",
                "value": "\n".join(header) + "\n\n" + content[:max_chars_per_item].rstrip(),
            }
        )
    return context


def count_memory_context_tokens(memory_context: list[MemoryContextItem]) -> int:
    if not memory_context:
        return 0
    from transformers import AutoProcessor

    processor = getattr(_CONTEXT_PROCESSOR_LOCAL, "processor", None)
    if processor is None:
        processor = AutoProcessor.from_pretrained(CONTEXT_TOKENIZER_MODEL)
        _CONTEXT_PROCESSOR_LOCAL.processor = processor
    content_parts = []
    for item in memory_context:
        if item["type"] != "text":
            raise ValueError("Sibyl LongMemEval context token counting only supports text items")
        content_parts.append({"type": "text", "text": item["value"]})
    prompt_text = processor.apply_chat_template(
        [{"role": "user", "content": content_parts}],
        tokenize=False,
        add_generation_prompt=False,
    )
    encoded = processor(text=prompt_text, images=None, return_tensors="pt")
    return int(encoded["input_ids"].shape[-1])


def build_retrieval_trace(
    results: list[dict[str, object]],
    *,
    max_items: int = DEFAULT_CONTEXT_ITEMS,
    max_chars_per_item: int = DEFAULT_CONTEXT_CHARS_PER_ITEM,
) -> list[dict[str, object]]:
    trace: list[dict[str, object]] = []
    for rank, result in enumerate(results[:max_items], start=1):
        content = _stripped_str(result.get("content"))
        if not content:
            continue
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        metadata_state_indices = metadata.get("longmemeval_v2_state_indices")
        state_indices = (
            [int(value) for value in metadata_state_indices if isinstance(value, int)]
            if isinstance(metadata_state_indices, list)
            else [
                int(value)
                for value in re.findall(r"^State\s+(\d+)\b", content, re.MULTILINE)
            ]
        )
        trace.append(
            {
                "rank": rank,
                "entity_id": _stripped_str(result.get("id")),
                "trajectory_id": _stripped_str(
                    metadata.get("longmemeval_v2_trajectory_id")
                ),
                "chunk_index": metadata.get("longmemeval_v2_chunk_index"),
                "chunk_count": metadata.get("longmemeval_v2_chunk_count"),
                "state_indices": state_indices,
                "score": result.get("score"),
                "content_chars": len(content),
                "exposed_chars": min(len(content), max_chars_per_item),
                "result_origin": _stripped_str(result.get("result_origin")),
                "selection_origin": _stripped_str(result.get("_selection_origin"))
                or "search",
                "search_rank": result.get("_search_rank"),
                "state_part_of_search_rank": result.get("_state_part_of_search_rank"),
                "state_part_refined_from_chunk": result.get(
                    "_state_part_refined_from_chunk"
                ),
                "neighbor_of_search_rank": result.get("_neighbor_of_search_rank"),
                "neighbor_distance": result.get("_neighbor_distance"),
            }
        )
    return trace


def assemble_context_results(
    results: list[dict[str, object]],
    *,
    chunk_catalog: dict[str, dict[int, dict[str, object]]],
    max_items: int,
    max_chunks_per_trajectory: int,
    neighbor_stitch_items: int,
    neighbor_stitch_span: int,
    query: str = "",
    state_part_completion_items: int = 0,
    state_part_refinement: bool = False,
    context_expansion_max_ratio: float = DEFAULT_CONTEXT_EXPANSION_MAX_RATIO,
    context_token_counter: Callable[[list[dict[str, object]]], int] | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    max_items = max(1, max_items)
    max_chunks_per_trajectory = max(1, max_chunks_per_trajectory)
    neighbor_stitch_items = max(0, min(neighbor_stitch_items, max_items - 1))
    neighbor_stitch_span = max(0, neighbor_stitch_span)
    state_part_completion_items = max(
        0,
        min(state_part_completion_items, max_items - neighbor_stitch_items - 1),
    )
    invalid_expansion_ratio = (
        not math.isfinite(context_expansion_max_ratio)
        or context_expansion_max_ratio < 0.0
        or 0.0 < context_expansion_max_ratio < 1.0
    )
    if invalid_expansion_ratio:
        raise ValueError("context_expansion_max_ratio must be zero or at least 1.0")
    if context_expansion_max_ratio > 0.0 and context_token_counter is None:
        raise ValueError("context_token_counter is required when expansion budgeting is enabled")
    ranked = []
    for search_rank, result in enumerate(results, start=1):
        candidate = dict(result)
        candidate["_selection_origin"] = "search"
        candidate["_search_rank"] = search_rank
        ranked.append(candidate)

    neighbor_budget = neighbor_stitch_items if chunk_catalog and neighbor_stitch_span else 0
    state_part_budget = state_part_completion_items if chunk_catalog else 0
    seed_limit = max_items - neighbor_budget - state_part_budget
    seeds = _select_diverse_results(
        ranked,
        limit=seed_limit,
        max_chunks_per_trajectory=max_chunks_per_trajectory,
    )
    refined_seeds, state_part_refinement_metadata = _refine_state_parts(
        query,
        seeds,
        chunk_catalog=chunk_catalog,
        enabled=state_part_refinement,
    )
    selected = list(refined_seeds)
    selected_keys = {_result_chunk_key(result) for result in selected}
    selected_keys.update(_result_chunk_key(result) for result in seeds)
    state_parts, state_part_metadata = _state_part_results(
        query,
        refined_seeds,
        chunk_catalog=chunk_catalog,
        selected_keys=selected_keys,
        limit=state_part_budget,
    )
    selected.extend(state_parts)
    neighbors = _neighbor_results(
        refined_seeds,
        chunk_catalog=chunk_catalog,
        selected_keys=selected_keys,
        limit=neighbor_budget,
        span=neighbor_stitch_span,
    )
    selected.extend(neighbors)
    if len(selected) < max_items:
        fallback = _select_diverse_results(
            ranked,
            limit=max_items,
            max_chunks_per_trajectory=max_chunks_per_trajectory,
        )
        for result in fallback:
            key = _result_chunk_key(result)
            if key in selected_keys:
                continue
            selected.append(result)
            selected_keys.add(key)
            if len(selected) >= max_items:
                break
    selected, expansion_budget = _apply_context_expansion_budget(
        selected,
        base_item_count=len(refined_seeds),
        max_ratio=context_expansion_max_ratio,
        token_counter=context_token_counter,
    )
    retained_state_parts = sum(
        _stripped_str(result.get("_selection_origin")) == "state_part"
        for result in selected
    )
    retained_neighbors = sum(
        _stripped_str(result.get("_selection_origin")) == "neighbor" for result in selected
    )
    return selected, {
        "input_result_count": len(results),
        "selected_search_seed_count": len(seeds),
        "completed_state_part_count": retained_state_parts,
        "stitched_neighbor_count": retained_neighbors,
        "output_result_count": len(selected),
        "max_chunks_per_trajectory": max_chunks_per_trajectory,
        "neighbor_stitch_items": neighbor_stitch_items,
        "neighbor_stitch_span": neighbor_stitch_span,
        "state_part_completion": state_part_metadata,
        "state_part_refinement": state_part_refinement_metadata,
        "context_expansion_budget": expansion_budget,
    }


def _apply_context_expansion_budget(
    selected: list[dict[str, object]],
    *,
    base_item_count: int,
    max_ratio: float,
    token_counter: Callable[[list[dict[str, object]]], int] | None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    metadata: dict[str, object] = {
        "enabled": max_ratio > 0.0,
        "max_ratio": max_ratio if max_ratio > 0.0 else None,
        "base_item_count": base_item_count,
        "unbounded_item_count": len(selected),
        "final_item_count": len(selected),
        "base_token_count": None,
        "max_token_count": None,
        "unbounded_token_count": None,
        "final_token_count": None,
        "dropped_item_count": 0,
        "dropped_chunk_keys": [],
        "binding": False,
    }
    if max_ratio <= 0.0:
        return selected, metadata
    if token_counter is None:
        raise ValueError("context_token_counter is required when expansion budgeting is enabled")

    base = selected[:base_item_count]
    bounded = list(selected)
    base_tokens = token_counter(base)
    max_tokens = int(base_tokens * max_ratio)
    unbounded_tokens = token_counter(bounded)
    dropped_keys: list[list[object]] = []
    final_tokens = unbounded_tokens
    while len(bounded) > base_item_count and final_tokens > max_tokens:
        dropped = bounded.pop()
        dropped_keys.append(list(_result_chunk_key(dropped)))
        final_tokens = token_counter(bounded)
    metadata.update(
        {
            "base_token_count": base_tokens,
            "max_token_count": max_tokens,
            "unbounded_token_count": unbounded_tokens,
            "final_token_count": final_tokens,
            "dropped_item_count": len(dropped_keys),
            "dropped_chunk_keys": dropped_keys,
            "binding": bool(dropped_keys),
            "final_item_count": len(bounded),
        }
    )
    return bounded, metadata


def _refine_state_parts(
    query: str,
    seeds: list[dict[str, object]],
    *,
    chunk_catalog: dict[str, dict[int, dict[str, object]]],
    enabled: bool,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    metadata: dict[str, object] = {
        "enabled": bool(enabled and query and chunk_catalog),
        "inspected_state_count": 0,
        "candidate_count": 0,
        "ranking_applied_count": 0,
        "replacements": [],
        "min_score_gain": STATE_PART_REFINEMENT_MIN_SCORE_GAIN,
    }
    if not metadata["enabled"]:
        return seeds, metadata

    selected_keys = {_result_chunk_key(seed) for seed in seeds}
    refined: list[dict[str, object]] = []
    replacements: list[dict[str, object]] = []
    for seed in seeds:
        siblings = _sibling_state_parts(
            seed,
            chunk_catalog=chunk_catalog,
            excluded_keys=selected_keys,
        )
        if not siblings:
            refined.append(seed)
            continue
        metadata["inspected_state_count"] = int(metadata["inspected_state_count"]) + 1
        metadata["candidate_count"] = int(metadata["candidate_count"]) + len(siblings)
        candidates = [seed, *siblings]
        prior_score = _numeric_score(seed.get("score"))
        ranking = rank_by_query_coverage(
            query,
            [
                QueryCoverageCandidate(
                    item=candidate,
                    stable_id=_result_stable_id(candidate),
                    text=_stripped_str(candidate.get("content")),
                    prior_score=prior_score,
                    original_rank=1,
                )
                for candidate in candidates
            ],
        )
        if not ranking.applied:
            refined.append(seed)
            continue
        metadata["ranking_applied_count"] = int(metadata["ranking_applied_count"]) + 1
        seed_key = _result_chunk_key(seed)
        seed_ranked = next(
            ranked for ranked in ranking.ranked if _result_chunk_key(ranked.item) == seed_key
        )
        best = ranking.ranked[0]
        best_key = _result_chunk_key(best.item)
        if (
            best_key == seed_key
            or best.score < seed_ranked.score + STATE_PART_REFINEMENT_MIN_SCORE_GAIN
        ):
            refined.append(seed)
            continue
        replacement = dict(best.item)
        replacement["score"] = best.score
        replacement["_selection_origin"] = "state_part_refinement"
        replacement["_search_rank"] = seed.get("_search_rank")
        replacement["_state_part_of_search_rank"] = seed.get("_search_rank")
        replacement["_state_part_refined_from_chunk"] = seed_key[1]
        refined.append(replacement)
        replacements.append(
            {
                "search_rank": seed.get("_search_rank"),
                "from_chunk_key": list(seed_key),
                "to_chunk_key": list(best_key),
                "score_gain": best.score - seed_ranked.score,
                "overlap_gain": best.overlap - seed_ranked.overlap,
            }
        )
    metadata["replacements"] = replacements
    return refined, metadata


def _state_part_results(
    query: str,
    seeds: list[dict[str, object]],
    *,
    chunk_catalog: dict[str, dict[int, dict[str, object]]],
    selected_keys: set[tuple[str, int | str]],
    limit: int,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    metadata: dict[str, object] = {
        "enabled": bool(query and limit and chunk_catalog),
        "candidate_count": 0,
        "ranking_applied": False,
        "admitted_chunk_keys": [],
    }
    if not metadata["enabled"]:
        return [], metadata

    candidates: list[dict[str, object]] = []
    candidate_keys: set[tuple[str, int | str]] = set()
    for seed in seeds:
        for candidate in _sibling_state_parts(
            seed,
            chunk_catalog=chunk_catalog,
            excluded_keys=selected_keys | candidate_keys,
        ):
            key = _result_chunk_key(candidate)
            candidate["score"] = seed.get("score")
            candidate["_selection_origin"] = "state_part"
            candidate["_state_part_of_search_rank"] = seed.get("_search_rank")
            candidates.append(candidate)
            candidate_keys.add(key)

    metadata["candidate_count"] = len(candidates)
    if not candidates:
        return [], metadata

    ranking = rank_by_query_coverage(
        query,
        [
            QueryCoverageCandidate(
                item=candidate,
                stable_id=_result_stable_id(candidate),
                text=_stripped_str(candidate.get("content")),
                prior_score=_numeric_score(candidate.get("score")),
                original_rank=index,
            )
            for index, candidate in enumerate(candidates, start=1)
        ],
    )
    metadata["ranking_applied"] = ranking.applied
    ranked_candidates = ranking.ranked
    admitted: list[dict[str, object]] = []
    for ranked in ranked_candidates[:limit]:
        candidate = dict(ranked.item)
        candidate["score"] = ranked.score
        admitted.append(candidate)
        selected_keys.add(_result_chunk_key(candidate))
    metadata["admitted_chunk_keys"] = [
        list(_result_chunk_key(candidate)) for candidate in admitted
    ]
    return admitted, metadata


def _sibling_state_parts(
    seed: dict[str, object],
    *,
    chunk_catalog: dict[str, dict[int, dict[str, object]]],
    excluded_keys: set[tuple[str, int | str]],
) -> list[dict[str, object]]:
    seed_metadata = seed.get("metadata") if isinstance(seed.get("metadata"), dict) else {}
    state_index = seed_metadata.get("longmemeval_v2_state_index")
    state_part_count = seed_metadata.get("longmemeval_v2_state_part_count")
    trajectory_id, _chunk_index = _result_chunk_key(seed)
    if not isinstance(state_index, int) or not isinstance(state_part_count, int):
        return []
    if state_part_count < 2:
        return []
    siblings: list[dict[str, object]] = []
    for chunk_index, catalog_result in sorted(chunk_catalog.get(trajectory_id, {}).items()):
        key = (trajectory_id, chunk_index)
        catalog_metadata = (
            catalog_result.get("metadata")
            if isinstance(catalog_result.get("metadata"), dict)
            else {}
        )
        if (
            key in excluded_keys
            or catalog_metadata.get("longmemeval_v2_state_index") != state_index
        ):
            continue
        siblings.append(dict(catalog_result))
    return siblings


def _numeric_score(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _result_stable_id(result: dict[str, object]) -> str:
    result_id = _stripped_str(result.get("id"))
    if result_id:
        return result_id
    trajectory_id, chunk_index = _result_chunk_key(result)
    return f"{trajectory_id}:{chunk_index}"


def _select_diverse_results(
    results: list[dict[str, object]],
    *,
    limit: int,
    max_chunks_per_trajectory: int,
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    selected_keys: set[tuple[str, int | str]] = set()
    trajectory_counts: Counter[str] = Counter()
    for diversity_pass in ("trajectory", "state"):
        for result in results:
            trajectory_id, state_key = _result_diversity_key(result)
            result_key = _result_chunk_key(result)
            if result_key in selected_keys:
                continue
            if trajectory_counts[trajectory_id] >= max_chunks_per_trajectory:
                continue
            if diversity_pass == "trajectory" and trajectory_counts[trajectory_id] > 0:
                continue
            if diversity_pass == "state" and any(
                _result_diversity_key(existing) == (trajectory_id, state_key)
                for existing in selected
            ):
                continue
            selected.append(result)
            selected_keys.add(result_key)
            trajectory_counts[trajectory_id] += 1
            if len(selected) >= limit:
                return selected
    return selected


def _neighbor_results(
    seeds: list[dict[str, object]],
    *,
    chunk_catalog: dict[str, dict[int, dict[str, object]]],
    selected_keys: set[tuple[str, int | str]],
    limit: int,
    span: int,
) -> list[dict[str, object]]:
    neighbors: list[dict[str, object]] = []
    for seed in seeds:
        trajectory_id, chunk_index = _result_chunk_key(seed)
        if not isinstance(chunk_index, int):
            continue
        trajectory_catalog = chunk_catalog.get(trajectory_id, {})
        for distance in range(1, span + 1):
            for neighbor_index in (chunk_index - distance, chunk_index + distance):
                key = (trajectory_id, neighbor_index)
                catalog_result = trajectory_catalog.get(neighbor_index)
                if catalog_result is None or key in selected_keys:
                    continue
                neighbor = dict(catalog_result)
                neighbor["score"] = seed.get("score")
                neighbor["_selection_origin"] = "neighbor"
                neighbor["_neighbor_of_search_rank"] = seed.get("_search_rank")
                neighbor["_neighbor_distance"] = distance
                neighbors.append(neighbor)
                selected_keys.add(key)
                if len(neighbors) >= limit:
                    return neighbors
    return neighbors


def _result_diversity_key(result: dict[str, object]) -> tuple[str, int | str]:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    trajectory_id = _stripped_str(metadata.get("longmemeval_v2_trajectory_id"))
    if not trajectory_id:
        trajectory_id = _stripped_str(result.get("id")) or f"unknown:{id(result)}"
    state_index = metadata.get("longmemeval_v2_state_index")
    state_key: int | str = state_index if isinstance(state_index, int) else "unknown"
    return trajectory_id, state_key


def _result_chunk_key(result: dict[str, object]) -> tuple[str, int | str]:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    trajectory_id = _stripped_str(metadata.get("longmemeval_v2_trajectory_id"))
    if not trajectory_id:
        trajectory_id = _stripped_str(result.get("id")) or f"unknown:{id(result)}"
    chunk_index = metadata.get("longmemeval_v2_chunk_index")
    chunk_key: int | str = chunk_index if isinstance(chunk_index, int) else trajectory_id
    return trajectory_id, chunk_key


def _catalog_results(
    payloads: list[dict[str, object]],
) -> dict[str, dict[int, dict[str, object]]]:
    catalog: dict[str, dict[int, dict[str, object]]] = {}
    for payload in payloads:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        trajectory_id = _stripped_str(metadata.get("longmemeval_v2_trajectory_id"))
        chunk_index = metadata.get("longmemeval_v2_chunk_index")
        if not trajectory_id or not isinstance(chunk_index, int):
            continue
        catalog.setdefault(trajectory_id, {})[chunk_index] = {
            "id": f"catalog:{trajectory_id}:{chunk_index}",
            "type": payload.get("entity_type"),
            "name": payload.get("name"),
            "content": payload.get("content"),
            "score": 0.0,
            "result_origin": "graph",
            "metadata": dict(metadata),
        }
    return catalog


def _memory_config_params(
    config: dict[str, object],
    *,
    expected_type: str,
) -> dict[str, object]:
    if config.get("memory_type") != expected_type:
        msg = f"Expected memory_type {expected_type!r}, got {config.get('memory_type')!r}"
        raise RuntimeError(msg)
    memory_params = config.get("memory_params")
    if not isinstance(memory_params, dict):
        raise RuntimeError("Memory config missing object memory_params")
    return dict(memory_params)


@register_memory
class SibylLiveApiMemory(Memory):
    memory_type = "sibyl_live_api"

    @property
    def memory_config(self) -> dict[str, object]:
        memory_params = {
            key: value
            for key, value in self.memory_params.items()
            if key not in SAVED_MEMORY_SECRET_KEYS | {"checkpoint_dir"}
        }
        memory_params.update(
            {
                "api_url": self.api_url,
                "project_id": self.project_id,
                "run_id": self.run_id,
            }
        )
        return {"memory_type": self.memory_type, "memory_params": memory_params}

    @classmethod
    def reconcile_loaded_memory_config(
        cls,
        saved_config: dict[str, object],
        requested_config: dict[str, object] | None,
    ) -> dict[str, object]:
        saved_params = _memory_config_params(saved_config, expected_type=cls.memory_type)
        if requested_config is None:
            return {
                "memory_type": cls.memory_type,
                "memory_params": dict(saved_params),
            }
        requested_params = _memory_config_params(
            requested_config,
            expected_type=cls.memory_type,
        )
        for key in SAVED_MEMORY_IDENTITY_KEYS:
            if key not in requested_params or requested_params[key] == saved_params.get(key):
                continue
            msg = (
                f"Loaded Sibyl memory cannot change ingest identity parameter {key!r}: "
                f"saved={saved_params.get(key)!r}, requested={requested_params[key]!r}"
            )
            raise RuntimeError(msg)
        effective_params = dict(saved_params)
        effective_params.update(
            {
                key: requested_params[key]
                for key in LOADED_MEMORY_RUNTIME_KEYS
                if key in requested_params
            }
        )
        return {"memory_type": cls.memory_type, "memory_params": effective_params}

    def __init__(self, memory_params: dict[str, object]) -> None:
        super().__init__(memory_params)
        self.api_url = _normalize_api_url(_param_str(memory_params, "api_url", DEFAULT_API_URL))
        self.run_id = _param_str(memory_params, "run_id", f"lme-v2-{uuid4().hex[:12]}")
        self.allow_localhost = _param_bool(memory_params, "allow_localhost", False)
        self.content_max_chars = _param_int(
            memory_params,
            "content_max_chars",
            DEFAULT_CONTENT_MAX_CHARS,
        )
        checkpoint_dir = _param_str(memory_params, "checkpoint_dir", "")
        self.checkpoint_dir = Path(checkpoint_dir).expanduser().resolve() if checkpoint_dir else None
        self.chunking_mode = _param_str(
            memory_params,
            "chunking_mode",
            DEFAULT_CHUNKING_MODE,
        )
        if self.chunking_mode not in CHUNKING_MODES:
            msg = (
                f"Unknown chunking_mode {self.chunking_mode!r}; "
                f"expected one of {sorted(CHUNKING_MODES)}"
            )
            raise ValueError(msg)
        if self.chunking_mode != DEFAULT_CHUNKING_MODE:
            raise ValueError(
                "trajectory chunking is incompatible with operational experience; "
                "use chunking_mode='state'"
            )
        self.search_limit = _param_int(memory_params, "search_limit", DEFAULT_SEARCH_LIMIT)
        self.max_context_items = _param_int(
            memory_params,
            "max_context_items",
            DEFAULT_CONTEXT_ITEMS,
        )
        self.max_context_chars_per_item = _param_int(
            memory_params,
            "max_context_chars_per_item",
            DEFAULT_CONTEXT_CHARS_PER_ITEM,
        )
        self.max_chunks_per_trajectory = max(
            1,
            _param_int(
                memory_params,
                "max_chunks_per_trajectory",
                DEFAULT_MAX_CHUNKS_PER_TRAJECTORY,
            ),
        )
        self.neighbor_stitch_items = max(
            0,
            _param_int(
                memory_params,
                "neighbor_stitch_items",
                DEFAULT_NEIGHBOR_STITCH_ITEMS,
                minimum=0,
            ),
        )
        self.neighbor_stitch_span = max(
            0,
            _param_int(
                memory_params,
                "neighbor_stitch_span",
                DEFAULT_NEIGHBOR_STITCH_SPAN,
                minimum=0,
            ),
        )
        self.state_part_completion_items = max(
            0,
            _param_int(
                memory_params,
                "state_part_completion_items",
                DEFAULT_STATE_PART_COMPLETION_ITEMS,
                minimum=0,
            ),
        )
        self.state_part_refinement = _param_bool(
            memory_params,
            "state_part_refinement",
            DEFAULT_STATE_PART_REFINEMENT,
        )
        self.context_expansion_max_ratio = _param_context_expansion_ratio(
            memory_params,
            "context_expansion_max_ratio",
            DEFAULT_CONTEXT_EXPANSION_MAX_RATIO,
        )
        self.include_screenshot_refs = _param_bool(
            memory_params,
            "include_screenshot_refs",
            False,
        )
        self.api_timeout_seconds = _param_float(
            memory_params,
            "api_timeout_seconds",
            DEFAULT_API_TIMEOUT_SECONDS,
        )
        self.api_retry_attempts = max(
            1,
            _param_int(memory_params, "api_retry_attempts", DEFAULT_API_RETRY_ATTEMPTS),
        )
        self.api_retry_base_delay_seconds = max(
            0.0,
            _param_float(
                memory_params,
                "api_retry_base_delay_seconds",
                DEFAULT_API_RETRY_BASE_DELAY_SECONDS,
            ),
        )
        self.api_retry_max_delay_seconds = max(
            0.0,
            _param_float(
                memory_params,
                "api_retry_max_delay_seconds",
                DEFAULT_API_RETRY_MAX_DELAY_SECONDS,
            ),
        )
        self.defer_embeddings = _param_bool(memory_params, "defer_embeddings", True)
        self.embedding_job_wait_timeout_seconds = _param_float(
            memory_params,
            "embedding_job_wait_timeout_seconds",
            DEFAULT_EMBEDDING_JOB_WAIT_TIMEOUT_SECONDS,
        )
        self.embedding_job_poll_seconds = _param_float(
            memory_params,
            "embedding_job_poll_seconds",
            DEFAULT_EMBEDDING_JOB_POLL_SECONDS,
        )
        self.bulk_max_entities = max(
            1,
            min(
                _param_int(memory_params, "bulk_max_entities", DEFAULT_BULK_MAX_ENTITIES),
                MAX_BULK_CREATE,
            ),
        )
        self.bulk_max_content_chars = max(
            1,
            _param_int(
                memory_params,
                "bulk_max_content_chars",
                DEFAULT_BULK_MAX_CONTENT_CHARS,
            ),
        )
        self.embedding_backfill_max_pending_jobs = max(
            1,
            _param_int(
                memory_params,
                "embedding_backfill_max_pending_jobs",
                DEFAULT_EMBEDDING_BACKFILL_MAX_PENDING_JOBS,
            ),
        )
        self.project_id = _param_str(memory_params, "project_id", "")
        self.inserted_trajectories = 0
        self.created_entities = 0
        self.last_experience_write_receipt: dict[str, object] = {}
        self._pending_embedding_job_ids: set[str] = set()
        self._pending_projection_job_ids: set[str] = set()
        self._pending_job_entity_ids: dict[str, list[str]] = {}
        self.ingest_embedding_usage: dict[str, object] = {}
        self._finalize_lock = threading.Lock()
        self._ingest_finalized = False
        self._query_local = threading.local()
        self._chunk_catalog: dict[str, dict[int, dict[str, object]]] = {}
        self._completed_trajectory_ids: set[str] = set()
        self._operational_trajectory_ids: set[str] = set()
        self._client = _new_http_client(
            self.api_url,
            timeout_seconds=self.api_timeout_seconds,
        )
        self._closed = False
        self._refresh_token = ""
        self._cli_auth: dict[str, str] = {}
        self._authenticate(memory_params)
        self.api_runtime = self._request_json("GET", "/health")
        self.ingest_api_runtime = dict(self.api_runtime)
        if not self.project_id:
            self.project_id = self._create_project()
        else:
            self._verify_project_visibility()
        self.memory_params["project_id"] = self.project_id
        if self.checkpoint_dir is not None and (
            self.checkpoint_dir / CHECKPOINT_MANIFEST_FILENAME
        ).is_file():
            self._load_checkpoint(self.checkpoint_dir)

    def set_query_context(self, **kwargs: object) -> None:
        question_item = kwargs.get("question_item")
        safe_context = {
            "question": question_item.get("question") if isinstance(question_item, dict) else None,
            "image": question_item.get("image") if isinstance(question_item, dict) else None,
        }
        super().set_query_context(**safe_context)

    def insert(self, trajectory: dict[str, object]) -> None:
        trajectory_id = _stripped_str(trajectory.get("id"))
        completed_trajectory_ids = getattr(self, "_completed_trajectory_ids", None)
        if completed_trajectory_ids is None:
            completed_trajectory_ids = set()
            self._completed_trajectory_ids = completed_trajectory_ids
        operational_trajectory_ids = getattr(self, "_operational_trajectory_ids", None)
        if operational_trajectory_ids is None:
            operational_trajectory_ids = set()
            self._operational_trajectory_ids = operational_trajectory_ids
        if trajectory_id and trajectory_id in operational_trajectory_ids:
            return
        already_cataloged = bool(trajectory_id and trajectory_id in completed_trajectory_ids)
        self._ingest_finalized = False
        payloads = build_entity_payloads_for_trajectory(
            trajectory,
            project_id=self.project_id,
            run_id=self.run_id,
            content_max_chars=self.content_max_chars,
            chunking_mode=getattr(self, "chunking_mode", DEFAULT_CHUNKING_MODE),
            include_screenshot_refs=self.include_screenshot_refs,
        )
        chunk_catalog = getattr(self, "_chunk_catalog", None)
        if chunk_catalog is None:
            chunk_catalog = {}
            self._chunk_catalog = chunk_catalog
        if not already_cataloged:
            chunk_catalog.update(_catalog_results(payloads))
        experience_payload = build_operational_experience_payload(
            trajectory,
            project_id=self.project_id,
            run_id=self.run_id,
            content_max_chars=self.content_max_chars,
            include_screenshot_refs=self.include_screenshot_refs,
        )
        experience_payload["defer_embeddings"] = self.defer_embeddings
        created = self._request_json(
            "POST",
            "/memory/experience",
            json=experience_payload,
        )
        self.last_experience_write_receipt = dict(created)
        self.created_entities += _created_count(created)
        self._remember_embedding_backfill_jobs(created)
        if len(self._pending_embedding_job_ids) >= self.embedding_backfill_max_pending_jobs:
            self._drain_embedding_backfills()
        if trajectory_id:
            completed_trajectory_ids.add(trajectory_id)
            operational_trajectory_ids.add(trajectory_id)
        self.inserted_trajectories = len(completed_trajectory_ids)
        if getattr(self, "checkpoint_dir", None) is not None:
            if already_cataloged:
                self._write_checkpoint_manifest(finalized=False)
            else:
                self._append_checkpoint(payloads)

    def finalize_ingest(self) -> None:
        with self._finalize_lock:
            if self._ingest_finalized:
                return
            self._drain_embedding_backfills()
            self._drain_memory_projections()
            self._ingest_finalized = True
            if getattr(self, "checkpoint_dir", None) is not None:
                self._write_checkpoint_manifest(finalized=True)

    def query(self, query: str, query_image: str | None = None) -> list[MemoryContextItem]:
        pending_jobs = len(self._pending_embedding_job_ids) + len(
            self._pending_projection_job_ids
        )
        if pending_jobs or not self._ingest_finalized:
            msg = f"memory ingestion has {pending_jobs} pending jobs; call finalize_ingest first"
            raise RuntimeError(msg)
        context_response = self._request_json(
            "POST",
            "/context/pack",
            json={
                "goal": query,
                "intent": "learn",
                "layer": "deep_search",
                "project": self.project_id,
                "limit": min(max(self.search_limit, self.max_context_items), 50),
                "include_related": True,
                "related_limit": 3,
                "audit": True,
                "record_exposure": False,
            },
        )
        typed_results = context_pack_to_search_results(context_response)
        payload = {
            "query": query,
            "types": ["session"],
            "project": self.project_id,
            "include_documents": False,
            "include_graph": True,
            "include_content": True,
            "content_max_chars": self.max_context_chars_per_item,
            "use_enhanced": True,
            "boost_recent": False,
            "include_retrieval_diagnostics": True,
            "record_exposure": False,
            "limit": min(max(self.search_limit, self.max_context_items), 50),
        }
        response = self._request_json("POST", "/search", json=payload)
        filters = response.get("filters")
        self._query_local.search_metadata = (
            dict(filters) if isinstance(filters, dict) else {}
        )
        raw_results = response.get("results")
        results = (
            [_string_key_dict(item) for item in raw_results if isinstance(item, dict)]
            if isinstance(raw_results, list)
            else []
        )
        results = _flatten_operational_result_metadata(results)
        assembled_results, assembly_metadata = assemble_context_results(
            results,
            chunk_catalog=getattr(self, "_chunk_catalog", {}),
            max_items=self.max_context_items,
            max_chunks_per_trajectory=getattr(
                self,
                "max_chunks_per_trajectory",
                DEFAULT_MAX_CHUNKS_PER_TRAJECTORY,
            ),
            neighbor_stitch_items=getattr(
                self,
                "neighbor_stitch_items",
                DEFAULT_NEIGHBOR_STITCH_ITEMS,
            ),
            neighbor_stitch_span=getattr(
                self,
                "neighbor_stitch_span",
                DEFAULT_NEIGHBOR_STITCH_SPAN,
            ),
            query=query,
            state_part_completion_items=getattr(
                self,
                "state_part_completion_items",
                DEFAULT_STATE_PART_COMPLETION_ITEMS,
            ),
            state_part_refinement=getattr(
                self,
                "state_part_refinement",
                DEFAULT_STATE_PART_REFINEMENT,
            ),
            context_expansion_max_ratio=getattr(
                self,
                "context_expansion_max_ratio",
                DEFAULT_CONTEXT_EXPANSION_MAX_RATIO,
            ),
            context_token_counter=self._count_context_result_tokens,
        )
        evidence_set = compile_operational_evidence_set(
            typed_results=typed_results,
            raw_results=assembled_results,
            max_items=self.max_context_items,
        )
        assembly_metadata["typed_context_candidate_count"] = len(typed_results)
        assembly_metadata["typed_context_selected_count"] = sum(
            _stripped_str(item.get("_selection_origin")).startswith("context_pack:")
            for item in evidence_set
        )
        self._query_local.search_metadata["adapter_assembly"] = assembly_metadata
        self._query_local.retrieval_trace = build_retrieval_trace(
            evidence_set,
            max_items=self.max_context_items,
            max_chars_per_item=self.max_context_chars_per_item,
        )
        return search_results_to_memory_context(
            evidence_set,
            max_items=self.max_context_items,
            max_chars_per_item=self.max_context_chars_per_item,
        )

    def _count_context_result_tokens(self, results: list[dict[str, object]]) -> int:
        return count_memory_context_tokens(
            search_results_to_memory_context(
                results,
                max_items=self.max_context_items,
                max_chars_per_item=self.max_context_chars_per_item,
            )
        )

    def _save_backend(self, output_dir: Path) -> None:
        self.finalize_ingest()
        catalog_path = output_dir / CHUNK_CATALOG_FILENAME
        with catalog_path.open("wb") as raw_handle:
            with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as gzip_handle:
                with io.TextIOWrapper(gzip_handle, encoding="utf-8") as handle:
                    for trajectory_id in sorted(self._chunk_catalog):
                        for chunk_index in sorted(self._chunk_catalog[trajectory_id]):
                            handle.write(
                                json_module.dumps(
                                    self._chunk_catalog[trajectory_id][chunk_index],
                                    sort_keys=True,
                                )
                                + "\n"
                            )
        memory_config_path = output_dir / "memory_config.json"
        manifest = {
            "schema_version": MEMORY_MANIFEST_SCHEMA_VERSION,
            "api_url": getattr(self, "api_url", None),
            "project_id": getattr(self, "project_id", None),
            "run_id": getattr(self, "run_id", None),
            "chunking_mode": getattr(self, "chunking_mode", DEFAULT_CHUNKING_MODE),
            "content_max_chars": getattr(self, "content_max_chars", DEFAULT_CONTENT_MAX_CHARS),
            "inserted_trajectories": getattr(
                self,
                "inserted_trajectories",
                len(self._chunk_catalog),
            ),
            "created_entities": getattr(
                self,
                "created_entities",
                sum(len(chunks) for chunks in self._chunk_catalog.values()),
            ),
            "ingest_api_runtime": dict(getattr(self, "ingest_api_runtime", {})),
            "ingest_embedding_usage": dict(getattr(self, "ingest_embedding_usage", {})),
            "completed_trajectory_ids": sorted(
                getattr(self, "_completed_trajectory_ids", self._chunk_catalog)
            ),
            "operational_trajectory_ids": sorted(
                getattr(self, "_operational_trajectory_ids", self._chunk_catalog)
            ),
            "pending_embedding_job_ids": sorted(self._pending_embedding_job_ids),
            "pending_projection_job_ids": sorted(self._pending_projection_job_ids),
            "ingest_finalized": True,
            "memory_config_sha256": _sha256_file(memory_config_path),
            "chunk_catalog_sha256": _sha256_file(catalog_path),
        }
        (output_dir / MEMORY_MANIFEST_FILENAME).write_text(
            json_module.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _load_backend(self, input_dir: Path) -> None:
        catalog_path = input_dir / CHUNK_CATALOG_FILENAME
        if not catalog_path.is_file():
            msg = f"Missing saved chunk catalog: {catalog_path}"
            raise RuntimeError(msg)
        manifest_path = input_dir / MEMORY_MANIFEST_FILENAME
        if not manifest_path.is_file():
            msg = f"Missing saved memory manifest: {manifest_path}"
            raise RuntimeError(msg)
        manifest = json_module.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != MEMORY_MANIFEST_SCHEMA_VERSION:
            raise RuntimeError(f"Invalid saved memory manifest: {manifest_path}")
        expected_catalog_hash = manifest.get("chunk_catalog_sha256")
        if expected_catalog_hash != _sha256_file(catalog_path):
            raise RuntimeError(f"Saved chunk catalog hash mismatch: {catalog_path}")
        memory_config_path = input_dir / "memory_config.json"
        if manifest.get("memory_config_sha256") != _sha256_file(memory_config_path):
            raise RuntimeError(f"Saved memory config hash mismatch: {memory_config_path}")
        catalog: dict[str, dict[int, dict[str, object]]] = {}
        with gzip.open(catalog_path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                loaded = json_module.loads(line)
                if not isinstance(loaded, dict):
                    continue
                metadata = (
                    loaded.get("metadata")
                    if isinstance(loaded.get("metadata"), dict)
                    else {}
                )
                trajectory_id = _stripped_str(
                    metadata.get("longmemeval_v2_trajectory_id")
                )
                chunk_index = metadata.get("longmemeval_v2_chunk_index")
                if trajectory_id and isinstance(chunk_index, int):
                    catalog.setdefault(trajectory_id, {})[chunk_index] = loaded
        self._chunk_catalog = catalog
        self.created_entities = sum(len(chunks) for chunks in catalog.values())
        self.inserted_trajectories = len(catalog)
        self._completed_trajectory_ids = set(catalog)
        operational_ids = manifest.get("operational_trajectory_ids")
        self._operational_trajectory_ids = (
            {
                str(value)
                for value in operational_ids
                if isinstance(value, str) and value in catalog
            }
            if isinstance(operational_ids, list)
            else set()
        )
        if self._operational_trajectory_ids != set(catalog):
            raise RuntimeError(
                "Legacy saved memory cannot be upgraded in place to operational experience; "
                "start a fresh project and memory directory"
            )
        self.ingest_api_runtime = (
            dict(manifest["ingest_api_runtime"])
            if isinstance(manifest.get("ingest_api_runtime"), dict)
            else {}
        )
        self.ingest_embedding_usage = (
            dict(manifest["ingest_embedding_usage"])
            if isinstance(manifest.get("ingest_embedding_usage"), dict)
            else {}
        )
        self._pending_embedding_job_ids.clear()
        self._pending_projection_job_ids.clear()
        self._pending_job_entity_ids = {}
        self._ingest_finalized = True

    def _append_checkpoint(self, payloads: list[dict[str, object]]) -> None:
        if self.checkpoint_dir is None:
            return
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        config_path = self.checkpoint_dir / "memory_config.json"
        if not config_path.exists():
            _write_json_atomic(config_path, self.memory_config)
        catalog_path = self.checkpoint_dir / CHECKPOINT_CATALOG_FILENAME
        self._truncate_checkpoint_catalog(catalog_path)
        checkpoint_results = _catalog_results(payloads)
        with catalog_path.open("a", encoding="utf-8") as handle:
            for trajectory_id in sorted(checkpoint_results):
                for chunk_index in sorted(checkpoint_results[trajectory_id]):
                    handle.write(
                        json_module.dumps(
                            checkpoint_results[trajectory_id][chunk_index],
                            sort_keys=True,
                        )
                        + "\n"
                    )
            handle.flush()
            os.fsync(handle.fileno())
        self._write_checkpoint_manifest(finalized=False)

    def _truncate_checkpoint_catalog(self, catalog_path: Path) -> None:
        if self.checkpoint_dir is None:
            return
        manifest_path = self.checkpoint_dir / CHECKPOINT_MANIFEST_FILENAME
        if not manifest_path.is_file():
            return
        manifest = json_module.loads(manifest_path.read_text(encoding="utf-8"))
        catalog_size = manifest.get("catalog_size") if isinstance(manifest, dict) else None
        if isinstance(catalog_size, bool) or not isinstance(catalog_size, int):
            raise RuntimeError(f"Invalid ingest checkpoint catalog size: {manifest_path}")
        actual_size = catalog_path.stat().st_size
        if actual_size < catalog_size:
            msg = f"Ingest checkpoint catalog is shorter than its manifest: {catalog_path}"
            raise RuntimeError(msg)
        if actual_size == catalog_size:
            return
        with catalog_path.open("r+b") as handle:
            handle.truncate(catalog_size)
            handle.flush()
            os.fsync(handle.fileno())

    def _write_checkpoint_manifest(self, *, finalized: bool) -> None:
        if self.checkpoint_dir is None:
            return
        config_path = self.checkpoint_dir / "memory_config.json"
        catalog_path = self.checkpoint_dir / CHECKPOINT_CATALOG_FILENAME
        if not config_path.exists():
            _write_json_atomic(config_path, self.memory_config)
        if not catalog_path.exists():
            catalog_path.touch()
        manifest = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "api_url": self.api_url,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "chunking_mode": self.chunking_mode,
            "content_max_chars": self.content_max_chars,
            "completed_trajectory_ids": sorted(self._completed_trajectory_ids),
            "operational_trajectory_ids": sorted(
                getattr(self, "_operational_trajectory_ids", set())
            ),
            "pending_embedding_job_ids": sorted(self._pending_embedding_job_ids),
            "pending_projection_job_ids": sorted(self._pending_projection_job_ids),
            "pending_job_entity_ids": {
                job_id: entity_ids
                for job_id, entity_ids in sorted(self._pending_job_entity_ids.items())
                if job_id
                in self._pending_embedding_job_ids | self._pending_projection_job_ids
            },
            "ingest_embedding_usage": dict(self.ingest_embedding_usage),
            "ingest_api_runtime": dict(self.ingest_api_runtime),
            "ingest_finalized": finalized,
            "catalog_size": catalog_path.stat().st_size,
            "memory_config_sha256": _sha256_file(config_path),
        }
        _write_json_atomic(
            self.checkpoint_dir / CHECKPOINT_MANIFEST_FILENAME,
            manifest,
        )

    def _load_checkpoint(self, checkpoint_dir: Path) -> None:
        manifest_path = checkpoint_dir / CHECKPOINT_MANIFEST_FILENAME
        manifest = json_module.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise RuntimeError(f"Invalid ingest checkpoint manifest: {manifest_path}")
        config_path = checkpoint_dir / "memory_config.json"
        if manifest.get("memory_config_sha256") != _sha256_file(config_path):
            raise RuntimeError(f"Ingest checkpoint config hash mismatch: {config_path}")
        for key, current in {
            "api_url": self.api_url,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "chunking_mode": self.chunking_mode,
            "content_max_chars": self.content_max_chars,
        }.items():
            if manifest.get(key) != current:
                msg = (
                    f"Ingest checkpoint identity mismatch for {key!r}: "
                    f"checkpoint={manifest.get(key)!r}, current={current!r}"
                )
                raise RuntimeError(msg)
        catalog_path = checkpoint_dir / CHECKPOINT_CATALOG_FILENAME
        catalog_size = manifest.get("catalog_size")
        if isinstance(catalog_size, bool) or not isinstance(catalog_size, int):
            raise RuntimeError(f"Invalid ingest checkpoint catalog size: {manifest_path}")
        catalog_bytes = catalog_path.read_bytes()[:catalog_size]
        completed = {
            str(value)
            for value in manifest.get("completed_trajectory_ids", [])
            if isinstance(value, str) and value
        }
        catalog: dict[str, dict[int, dict[str, object]]] = {}
        for line in catalog_bytes.splitlines():
            loaded = json_module.loads(line)
            if not isinstance(loaded, dict):
                continue
            metadata = loaded.get("metadata") if isinstance(loaded.get("metadata"), dict) else {}
            trajectory_id = _stripped_str(metadata.get("longmemeval_v2_trajectory_id"))
            chunk_index = metadata.get("longmemeval_v2_chunk_index")
            if trajectory_id in completed and isinstance(chunk_index, int):
                catalog.setdefault(trajectory_id, {})[chunk_index] = loaded
        self._chunk_catalog = catalog
        self._completed_trajectory_ids = completed
        operational_ids = manifest.get("operational_trajectory_ids")
        self._operational_trajectory_ids = (
            {
                str(value)
                for value in operational_ids
                if isinstance(value, str) and value in completed
            }
            if isinstance(operational_ids, list)
            else set()
        )
        if self._operational_trajectory_ids != completed:
            raise RuntimeError(
                "Legacy ingest checkpoint cannot be upgraded in place to operational "
                "experience; start a fresh project and checkpoint directory"
            )
        self.inserted_trajectories = len(completed)
        self.created_entities = sum(len(chunks) for chunks in catalog.values())
        self._pending_embedding_job_ids = {
            str(value)
            for value in manifest.get("pending_embedding_job_ids", [])
            if isinstance(value, str) and value
        }
        self._pending_projection_job_ids = {
            str(value)
            for value in manifest.get("pending_projection_job_ids", [])
            if isinstance(value, str) and value
        }
        pending_job_entity_ids = manifest.get("pending_job_entity_ids")
        self._pending_job_entity_ids = (
            {
                str(job_id): [str(entity_id) for entity_id in entity_ids]
                for job_id, entity_ids in pending_job_entity_ids.items()
                if isinstance(job_id, str)
                and isinstance(entity_ids, list)
                and all(isinstance(entity_id, str) for entity_id in entity_ids)
            }
            if isinstance(pending_job_entity_ids, dict)
            else {}
        )
        self.ingest_embedding_usage = (
            dict(manifest["ingest_embedding_usage"])
            if isinstance(manifest.get("ingest_embedding_usage"), dict)
            else {}
        )
        self.ingest_api_runtime = (
            dict(manifest["ingest_api_runtime"])
            if isinstance(manifest.get("ingest_api_runtime"), dict)
            else {}
        )
        self._ingest_finalized = bool(manifest.get("ingest_finalized"))

    def post_query_hook(
        self,
        *,
        query: str,
        query_image: str | None,
        memory_context: list[MemoryContextItem],
    ) -> dict[str, object] | None:
        return {
            "memory_type": self.memory_type,
            "api_url": self.api_url,
            "api_runtime": dict(getattr(self, "api_runtime", {})),
            "ingest_api_runtime": dict(getattr(self, "ingest_api_runtime", {})),
            "project_id": self.project_id,
            "run_id": self.run_id,
            "inserted_trajectories": self.inserted_trajectories,
            "created_entities": self.created_entities,
            "defer_embeddings": self.defer_embeddings,
            "pending_embedding_backfill_jobs": len(self._pending_embedding_job_ids),
            "pending_memory_projection_jobs": len(self._pending_projection_job_ids),
            "ingest_embedding_usage": dict(self.ingest_embedding_usage),
            "returned_context_items": len(memory_context),
            "search_content_max_chars": self.max_context_chars_per_item,
            "search_metadata": dict(
                getattr(self._query_local, "search_metadata", {})
            ),
            "retrieval_trace": list(
                getattr(self._query_local, "retrieval_trace", [])
            ),
        }

    def _remember_embedding_backfill_jobs(self, response: dict[str, object]) -> None:
        if not self.defer_embeddings:
            return
        background_jobs = response.get("background_jobs")
        embedding_job = (
            background_jobs.get("embedding_backfill")
            if isinstance(background_jobs, dict)
            else None
        )
        if isinstance(embedding_job, dict) and embedding_job.get("status") == "degraded":
            error = _stripped_str(embedding_job.get("error")) or "unknown error"
            msg = f"deferred embedding backfill enqueue degraded: {error}"
            raise RuntimeError(msg)
        job_ids = _background_job_ids(response, "embedding_backfill")
        if not job_ids and _created_count(response) > 0:
            msg = "/entities/bulk deferred embeddings but returned no backfill job ids"
            raise RuntimeError(msg)
        self._pending_embedding_job_ids.update(job_ids)
        self._remember_job_entity_ids(job_ids, response)

    def _remember_memory_projection_jobs(self, response: dict[str, object]) -> None:
        job_ids = _background_job_ids(response, "memory_projection")
        self._pending_projection_job_ids.update(job_ids)
        self._remember_job_entity_ids(job_ids, response)

    def _remember_job_entity_ids(
        self,
        job_ids: list[str],
        response: dict[str, object],
    ) -> None:
        entity_ids = _response_entity_ids(response)
        if not entity_ids:
            return
        pending_job_entity_ids = getattr(self, "_pending_job_entity_ids", None)
        if pending_job_entity_ids is None:
            pending_job_entity_ids = {}
            self._pending_job_entity_ids = pending_job_entity_ids
        for job_id in job_ids:
            pending_job_entity_ids[job_id] = entity_ids

    def _drain_embedding_backfills(self) -> None:
        self._drain_background_jobs(
            self._pending_embedding_job_ids,
            job_name="embedding backfill",
            job_kind="embedding_backfill",
            progress_label="Embedding backfills",
        )

    def _drain_memory_projections(self) -> None:
        self._drain_background_jobs(
            self._pending_projection_job_ids,
            job_name="memory projection",
            job_kind="memory_projection",
            progress_label="Memory projections",
        )

    def _drain_background_jobs(
        self,
        job_ids: set[str],
        *,
        job_name: str,
        job_kind: str,
        progress_label: str,
    ) -> None:
        if not job_ids:
            return
        if getattr(self, "_pending_job_entity_ids", None) is None:
            self._pending_job_entity_ids = {}

        pending = set(job_ids)
        total = len(pending)
        stall_deadline = time.monotonic() + self.embedding_job_wait_timeout_seconds
        last_statuses: dict[str, str] = {}
        recovered_job_ids: set[str] = set()
        while pending:
            made_progress = False
            pending_job_ids = sorted(pending)
            statuses: dict[str, object] = {}
            for offset in range(0, len(pending_job_ids), JOB_STATUS_BATCH_SIZE):
                job_id_batch = pending_job_ids[offset : offset + JOB_STATUS_BATCH_SIZE]
                response = self._request_json(
                    "POST",
                    "/jobs/status",
                    json={"job_ids": job_id_batch},
                )
                batch_statuses = response.get("jobs")
                if not isinstance(batch_statuses, dict):
                    msg = f"invalid {job_name} batch status response"
                    raise RuntimeError(msg)
                statuses.update(batch_statuses)
            for job_id in sorted(pending):
                status = statuses.get(job_id)
                if not isinstance(status, dict):
                    msg = f"missing {job_name} status for {job_id}"
                    raise RuntimeError(msg)
                status_value = _stripped_str(status.get("status")) or "unknown"
                if last_statuses.get(job_id) != status_value:
                    made_progress = True
                last_statuses[job_id] = status_value
                recoverable_failure = status_value == "complete" and bool(status.get("error"))
                if status_value == "not_found" or recoverable_failure:
                    if job_id in recovered_job_ids:
                        if recoverable_failure:
                            msg = f"requeued {job_name} job {job_id} failed: {status['error']}"
                        else:
                            msg = f"requeued {job_name} job {job_id} is still not found"
                        raise RuntimeError(msg)
                    replacements = self._recover_background_job(
                        job_id,
                        job_kind=job_kind,
                    )
                    pending.remove(job_id)
                    pending.update(replacements)
                    recovered_job_ids.update(replacements)
                    last_statuses.pop(job_id, None)
                    made_progress = True
                    continue
                if status_value == "complete":
                    result = status.get("result")
                    if isinstance(result, dict):
                        result_errors = result.get("errors")
                        projection_state = _stripped_str(result.get("projection_state"))
                        if result_errors or projection_state == "partial":
                            msg = (
                                f"{job_name} job {job_id} completed partially: "
                                f"{result_errors or projection_state}"
                            )
                            raise RuntimeError(msg)
                        _merge_usage_totals(
                            self.ingest_embedding_usage,
                            result.get("embedding_usage"),
                        )
                    pending.remove(job_id)
                    self._pending_job_entity_ids.pop(job_id, None)
                    made_progress = True
                elif status_value == "cancelled":
                    msg = f"{job_name} job {job_id} ended as {status_value}"
                    raise RuntimeError(msg)

            if made_progress:
                stall_deadline = time.monotonic() + self.embedding_job_wait_timeout_seconds
                _report_background_job_progress(
                    progress_label,
                    total,
                    pending,
                    last_statuses,
                )
            if not pending:
                break
            if time.monotonic() >= stall_deadline:
                statuses = ", ".join(
                    f"{job_id}={last_statuses.get(job_id, 'unknown')}"
                    for job_id in sorted(pending)
                )
                msg = (
                    f"timed out after {self.embedding_job_wait_timeout_seconds:g}s "
                    f"without {job_name} progress: {statuses}"
                )
                raise RuntimeError(msg)
            time.sleep(self.embedding_job_poll_seconds)

        job_ids.clear()

    def _recover_background_job(self, job_id: str, *, job_kind: str) -> set[str]:
        entity_ids = self._pending_job_entity_ids.get(job_id, [])
        if not entity_ids:
            msg = f"cannot recover lost {job_kind} job {job_id}: checkpoint has no entity ids"
            raise RuntimeError(msg)
        response = self._request_json(
            "POST",
            "/entities/bulk/requeue-background-jobs",
            json={"entity_ids": entity_ids, "jobs": [job_kind]},
        )
        replacements = set(_background_job_ids(response, job_kind))
        if not replacements:
            msg = f"requeue returned no replacement {job_kind} job for {job_id}"
            raise RuntimeError(msg)
        self._pending_embedding_job_ids.discard(job_id)
        self._pending_projection_job_ids.discard(job_id)
        self._pending_job_entity_ids.pop(job_id, None)
        self._remember_embedding_backfill_jobs(response)
        self._remember_memory_projection_jobs(response)
        self._write_checkpoint_manifest(finalized=False)
        return replacements

    def _authenticate(self, memory_params: dict[str, object]) -> None:
        if _is_loopback_url(self.api_url) and not self.allow_localhost:
            msg = "Refusing to mutate localhost without allow_localhost=true"
            raise RuntimeError(msg)
        token = _param_str(memory_params, "api_token", "") or os.environ.get("SIBYL_API_TOKEN", "")
        if token:
            self._client.headers.update({"Authorization": f"Bearer {token}"})
            return
        cli_auth = _load_cli_auth(self.api_url)
        cli_token = cli_auth.get("access_token", "")
        if cli_token:
            self._refresh_token = cli_auth.get("refresh_token", "")
            self._cli_auth = cli_auth
            self._client.headers.update({"Authorization": f"Bearer {cli_token}"})
            return
        email = _param_str(memory_params, "email", "") or os.environ.get("LME_SIBYL_EMAIL", "")
        password = _param_str(memory_params, "password", "") or os.environ.get(
            "LME_SIBYL_PASSWORD",
            "",
        )
        if not email:
            email = f"longmemeval-v2-{self.run_id}@example.invalid"
        if not password:
            password = f"SibylLongMemEvalV2-{self.run_id}-password"
        cache_key = (self.api_url, email, password)
        with _AUTH_LOCK:
            cached = _AUTH_CACHE.get(cache_key)
            if cached is not None:
                self._client.headers.update({"Authorization": f"Bearer {cached['access_token']}"})
                self._refresh_token = cached.get("refresh_token", "")
                return
            issued = self._login_or_signup(
                email=email,
                password=password,
                allow_signup=_param_bool(memory_params, "allow_signup", True),
            )
            _AUTH_CACHE[cache_key] = issued
            self._client.headers.update({"Authorization": f"Bearer {issued['access_token']}"})
            self._refresh_token = issued.get("refresh_token", "")

    def _login_or_signup(
        self,
        *,
        email: str,
        password: str,
        allow_signup: bool,
    ) -> dict[str, str]:
        login = self._auth_request("/auth/local/login", email=email, password=password)
        if login is not None:
            return login
        if not allow_signup:
            msg = "Could not log in to Sibyl and allow_signup=false"
            raise RuntimeError(msg)
        signup = self._auth_request(
            "/auth/local/signup",
            email=email,
            password=password,
            name="LongMemEval V2 Runner",
        )
        if signup is not None:
            return signup
        second_login = self._auth_request("/auth/local/login", email=email, password=password)
        if second_login is not None:
            return second_login
        msg = "Could not authenticate Sibyl benchmark user"
        raise RuntimeError(msg)

    def _auth_request(self, path: str, **payload: str) -> dict[str, str] | None:
        response = self._client.post(path, json=payload)
        if response.status_code >= 400:
            return None
        body = response.json()
        if not isinstance(body, dict) or not body.get("access_token"):
            return None
        issued = {"access_token": str(body["access_token"])}
        if body.get("refresh_token"):
            issued["refresh_token"] = str(body["refresh_token"])
        return issued

    def _create_project(self) -> str:
        sequence = next(_INSTANCE_COUNTER)
        response = self._request_json(
            "POST",
            "/entities",
            params={"sync": "true"},
            json={
                "name": f"LongMemEval V2 {self.run_id} memory {sequence}",
                "description": "Isolated LongMemEval-V2 memory workspace",
                "content": "LongMemEval-V2 isolated memory workspace.",
                "entity_type": "project",
                "skip_conflicts": True,
                "defer_embeddings": self.defer_embeddings,
                "metadata": {
                    "longmemeval_v2_run_id": self.run_id,
                    "capture_surface": "longmemeval-v2-official",
                },
            },
        )
        project_id = _stripped_str(response.get("id"))
        if not project_id:
            msg = "Sibyl project creation did not return an id"
            raise RuntimeError(msg)
        self._remember_embedding_backfill_jobs(response)
        self._remember_memory_projection_jobs(response)
        return project_id

    def _verify_project_visibility(self) -> None:
        try:
            response = self._request_json(
                "GET",
                f"/entities/{self.project_id}",
                params={"include_summary": "false", "related_limit": "0"},
            )
        except Exception as exc:
            msg = (
                f"Saved Sibyl project {self.project_id!r} is not visible to the "
                "current API credentials"
            )
            raise RuntimeError(msg) from exc
        if response.get("id") != self.project_id or response.get("entity_type") != "project":
            msg = f"Saved Sibyl project identity mismatch for {self.project_id!r}"
            raise RuntimeError(msg)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        response: httpx.Response | None = None
        for attempt in range(1, self.api_retry_attempts + 1):
            try:
                response = self._client.request(method, path, json=json, params=params)
                if (
                    response.status_code == 401
                    and self._refresh_token
                    and self._refresh_access_token()
                ):
                    response = self._client.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                if attempt >= self.api_retry_attempts or not _is_retryable_http_error(exc):
                    raise
                self._sleep_before_retry(path, exc.__class__.__name__, attempt=attempt)
                continue

            if not _is_retryable_response(response) or attempt >= self.api_retry_attempts:
                break
            self._sleep_before_retry(
                path,
                f"HTTP {response.status_code}",
                attempt=attempt,
            )

        if response is None:
            msg = f"{path} request produced no response"
            raise RuntimeError(msg)
        if response.status_code >= 400:
            msg = f"{path} failed with HTTP {response.status_code}: {response.text[:500]}"
            raise RuntimeError(msg)
        body = response.json()
        if not isinstance(body, dict):
            msg = f"{path} returned non-object JSON"
            raise RuntimeError(msg)
        return body

    def _sleep_before_retry(self, path: str, reason: str, *, attempt: int) -> None:
        delay = _retry_delay_seconds(
            base_delay=self.api_retry_base_delay_seconds,
            max_delay=self.api_retry_max_delay_seconds,
            failed_attempt=attempt,
        )
        print(
            f"Sibyl API request {path} failed with {reason}; "
            f"retrying attempt {attempt + 1}/{self.api_retry_attempts} "
            f"after {delay:.1f}s.",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)

    def _refresh_access_token(self) -> bool:
        response = self._client.post("/auth/refresh", json={"refresh_token": self._refresh_token})
        if response.status_code != 200:
            return False
        body = response.json()
        if not isinstance(body, dict) or not body.get("access_token"):
            return False
        access_token = str(body["access_token"])
        refresh_token = str(body.get("refresh_token") or self._refresh_token)
        self._refresh_token = refresh_token
        self._client.headers.update({"Authorization": f"Bearer {access_token}"})
        _store_cli_auth(self._cli_auth, access_token, refresh_token, body.get("expires_in"))
        return True


def _trajectory_text_chunks(
    trajectory: LongMemEvalV2Trajectory,
    *,
    max_chars: int,
    include_screenshot_refs: bool,
    chunking_mode: str = DEFAULT_CHUNKING_MODE,
) -> list[str]:
    chunk_builder = _grouped_trajectory_chunks if chunking_mode == "trajectory" else _trajectory_chunks
    return [
        chunk.content
        for chunk in chunk_builder(
            trajectory,
            max_chars=max_chars,
            include_screenshot_refs=include_screenshot_refs,
        )
    ]


def _grouped_trajectory_chunks(
    trajectory: LongMemEvalV2Trajectory,
    *,
    max_chars: int,
    include_screenshot_refs: bool,
) -> list[TrajectoryTextChunk]:
    header = _trajectory_header(trajectory)
    chunks: list[TrajectoryTextChunk] = []
    current = header
    current_state_indices: list[int] = []
    for state in trajectory.states:
        block = _state_text(state, include_screenshot_refs=include_screenshot_refs)
        candidate = f"{current}\n\n{block}"
        if len(candidate) <= max_chars:
            current = candidate
            current_state_indices.append(state.state_index)
            continue
        if current != header:
            chunks.append(
                TrajectoryTextChunk(
                    content=current,
                    state_index=current_state_indices[0],
                    state_indices=tuple(current_state_indices),
                    state_part_index=0,
                    state_part_count=1,
                )
            )
            current = f"{header}\n\n{block}"
            current_state_indices = [state.state_index]
            if len(current) <= max_chars:
                continue
        pieces = _split_oversized_block(header, block, max_chars=max_chars)
        part_count = len(pieces)
        chunks.extend(
            TrajectoryTextChunk(
                content=piece,
                state_index=state.state_index,
                state_indices=(state.state_index,),
                state_part_index=part_index,
                state_part_count=part_count,
            )
            for part_index, piece in enumerate(pieces)
        )
        current = header
        current_state_indices = []
    if current != header or not chunks:
        chunks.append(
            TrajectoryTextChunk(
                content=current,
                state_index=current_state_indices[0] if current_state_indices else 0,
                state_indices=tuple(current_state_indices) or (0,),
                state_part_index=0,
                state_part_count=1,
            )
        )
    return chunks


def _trajectory_chunks(
    trajectory: LongMemEvalV2Trajectory,
    *,
    max_chars: int,
    include_screenshot_refs: bool,
) -> list[TrajectoryTextChunk]:
    header = _trajectory_header(trajectory)
    chunks: list[TrajectoryTextChunk] = []
    for state in trajectory.states:
        chunks.extend(
            _state_chunks(
                header,
                state,
                max_chars=max_chars,
                include_screenshot_refs=include_screenshot_refs,
            )
        )
    if not chunks:
        chunks.append(
            TrajectoryTextChunk(
                content=header[:max_chars],
                state_index=0,
                state_part_index=0,
                state_part_count=1,
            )
        )
    return chunks


def _trajectory_header(trajectory: LongMemEvalV2Trajectory) -> str:
    return "\n".join(
        [
            f"Trajectory: {trajectory.id}",
            f"Domain: {trajectory.domain}",
            f"Environment: {trajectory.environment}",
            f"Outcome: {trajectory.outcome}",
            f"Goal: {trajectory.goal}",
            f"Start URL: {trajectory.start_url}",
        ]
    )


def _state_text(state: LongMemEvalV2State, *, include_screenshot_refs: bool) -> str:
    parts = [f"State {state.state_index}", f"URL: {state.url}"]
    if state.action:
        parts.append(f"Action: {state.action}")
    if state.thought:
        parts.append(f"Thought: {state.thought}")
    if include_screenshot_refs and state.screenshot:
        parts.append(f"Screenshot: {state.screenshot}")
    parts.append(f"Accessibility tree:\n{state.accessibility_tree}")
    return "\n".join(parts)


def _state_chunks(
    header: str,
    state: LongMemEvalV2State,
    *,
    max_chars: int,
    include_screenshot_refs: bool,
) -> list[TrajectoryTextChunk]:
    identity = "\n".join((f"State {state.state_index}", f"URL: {state.url}"))
    body_parts = []
    if state.action:
        body_parts.append(f"Action: {state.action}")
    if state.thought:
        body_parts.append(f"Thought: {state.thought}")
    if include_screenshot_refs and state.screenshot:
        body_parts.append(f"Screenshot: {state.screenshot}")
    body_parts.append(f"Accessibility tree:\n{state.accessibility_tree}")
    body = "\n".join(body_parts)
    prefix = f"{header}\n\n{identity}"
    candidate = f"{prefix}\n{body}"
    if len(candidate) <= max_chars:
        return [
            TrajectoryTextChunk(
                content=candidate,
                state_index=state.state_index,
                state_part_index=0,
                state_part_count=1,
            )
        ]

    part_label_reserve = "\nPart 999999999/999999999\n\n"
    budget = max_chars - len(prefix) - len(part_label_reserve)
    if budget <= 0:
        msg = (
            f"content_max_chars={max_chars} cannot preserve trajectory and state identity; "
            f"requires more than {len(prefix) + len(part_label_reserve)} characters"
        )
        raise ValueError(msg)
    pieces = _split_text_lines(body, max_chars=budget)
    part_count = len(pieces)
    return [
        TrajectoryTextChunk(
            content=(f"{prefix}\nPart {part_index + 1}/{part_count}\n\n{piece}"),
            state_index=state.state_index,
            state_part_index=part_index,
            state_part_count=part_count,
        )
        for part_index, piece in enumerate(pieces)
    ]


def _split_oversized_block(header: str, block: str, *, max_chars: int) -> list[str]:
    prefix = f"{header}\n\n"
    budget = max(1, max_chars - len(prefix))
    pieces: list[str] = []
    current = ""
    for line in block.splitlines(keepends=True):
        if len(line) > budget:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(line[index : index + budget] for index in range(0, len(line), budget))
            continue
        if current and len(current) + len(line) > budget:
            pieces.append(current)
            current = ""
        current += line
    if current or not pieces:
        pieces.append(current)
    return [prefix + piece for piece in pieces]


def _split_text_lines(value: str, *, max_chars: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for line in value.splitlines(keepends=True):
        if len(line) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(
                line[index : index + max_chars]
                for index in range(0, len(line), max_chars)
            )
            continue
        if current and len(current) + len(line) > max_chars:
            pieces.append(current)
            current = ""
        current += line
    if current or not pieces:
        pieces.append(current)
    return pieces


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json_module.dumps(value, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _entity_name(trajectory_id: str, chunk_index: int, chunk_count: int) -> str:
    suffix = f" chunk {chunk_index + 1} of {chunk_count}" if chunk_count > 1 else ""
    return f"LongMemEval-V2 trajectory {trajectory_id}{suffix}"[:200]


def _payload_batches(
    items: list[dict[str, object]],
    *,
    max_entities: int,
    max_content_chars: int,
) -> list[list[dict[str, object]]]:
    batches: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    current_chars = 0
    max_entities = max(1, min(max_entities, MAX_BULK_CREATE))
    max_content_chars = max(1, max_content_chars)
    for item in items:
        item_chars = _payload_content_chars(item)
        would_exceed_count = len(current) >= max_entities
        would_exceed_chars = current_chars + item_chars > max_content_chars
        if current and (would_exceed_count or would_exceed_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars
    if current:
        batches.append(current)
    return batches


def _payload_content_chars(item: dict[str, object]) -> int:
    return sum(
        len(value)
        for value in (
            item.get("name"),
            item.get("description"),
            item.get("content"),
        )
        if isinstance(value, str)
    )


def _background_job_ids(response: dict[str, object], key: str) -> list[str]:
    background_jobs = response.get("background_jobs")
    if not isinstance(background_jobs, dict):
        return []
    job_info = background_jobs.get(key)
    if not isinstance(job_info, dict):
        return []
    job_ids = job_info.get("job_ids")
    if not isinstance(job_ids, list):
        return []
    return [_stripped_str(job_id) for job_id in job_ids if _stripped_str(job_id)]


def _response_entity_ids(response: dict[str, object]) -> list[str]:
    entity_ids = response.get("entity_ids")
    if isinstance(entity_ids, list):
        return [_stripped_str(value) for value in entity_ids if _stripped_str(value)]
    entities = response.get("entities")
    if isinstance(entities, list):
        return [
            entity_id
            for entity in entities
            if isinstance(entity, dict)
            and (entity_id := _stripped_str(entity.get("id")))
        ]
    entity_id = _stripped_str(response.get("id"))
    return [entity_id] if entity_id else []


def _report_background_job_progress(
    label: str,
    total: int,
    pending: set[str],
    statuses: dict[str, str],
) -> None:
    pending_counts = Counter(statuses.get(job_id, "unknown") for job_id in pending)
    pending_summary = ", ".join(
        f"{status}={count}" for status, count in sorted(pending_counts.items())
    )
    print(
        f"{label}: {total - len(pending)}/{total} complete; "
        f"pending {pending_summary or 'none'}.",
        file=sys.stderr,
        flush=True,
    )


def _created_count(response: dict[str, object]) -> int:
    value = response.get("written_entities", response.get("created"))
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return int(value)
    return 0


def _is_retryable_response(response: httpx.Response) -> bool:
    return response.status_code in RETRYABLE_HTTP_STATUS_CODES or response.status_code >= 500


def _is_retryable_http_error(exc: httpx.HTTPError) -> bool:
    return isinstance(
        exc,
        httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError,
    )


def _retry_delay_seconds(
    *,
    base_delay: float,
    max_delay: float,
    failed_attempt: int,
) -> float:
    return min(max_delay, base_delay * (2 ** max(0, failed_attempt - 1)))


def _merge_usage_totals(total: dict[str, object], usage: object) -> None:
    if not isinstance(usage, dict):
        return
    for field_name in ("provider", "model"):
        value = _stripped_str(usage.get(field_name))
        if value:
            total[field_name] = value
    for field_name in (
        "requests",
        "inputs",
        "prompt_tokens",
        "total_tokens",
        "cost_reported_requests",
        "cost_usd",
    ):
        value = usage.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        current = total.get(field_name, 0)
        current_number = current if isinstance(current, int | float) else 0
        total[field_name] = current_number + value


def _new_http_client(api_url: str, *, timeout_seconds: float) -> httpx.Client:
    return httpx.Client(base_url=api_url, timeout=timeout_seconds, follow_redirects=True)


def _normalize_api_url(raw_url: str) -> str:
    url = raw_url.rstrip("/")
    if not url.endswith("/api"):
        url = f"{url}/api"
    return url


def _is_loopback_url(api_url: str) -> bool:
    host = urlparse(api_url).hostname
    if host is None:
        return False
    return host in {"localhost", "::1"} or host.startswith("127.")


def _api_url_variants(api_url: str) -> list[str]:
    variants = [api_url]
    parsed = urlparse(api_url)
    if parsed.hostname == "127.0.0.1":
        variants.append(api_url.replace("127.0.0.1", "localhost", 1))
    elif parsed.hostname == "localhost":
        variants.append(api_url.replace("localhost", "127.0.0.1", 1))
    return list(dict.fromkeys(variants))


def _load_cli_auth(api_url: str) -> dict[str, str]:
    try:
        from sibyl_cli import config_store
        from sibyl_cli.auth_store import credential_scope, read_server_credentials
    except Exception:
        return {}

    scopes: list[str | None] = [None]
    try:
        active = config_store.get_active_context()
    except Exception:
        active = None
    if active is not None:
        scopes.insert(0, credential_scope(active.name, active.org_slug))

    for candidate_url in _api_url_variants(api_url):
        for scope in scopes:
            try:
                credentials = read_server_credentials(candidate_url, credential_scope=scope)
            except Exception:
                continue
            access_token = _stripped_str(credentials.get("access_token"))
            if not access_token:
                continue
            return {
                "access_token": access_token,
                "refresh_token": _stripped_str(credentials.get("refresh_token")),
                "api_url": candidate_url,
                "credential_scope": scope or "",
            }
    return {}


def _store_cli_auth(
    cli_auth: dict[str, str],
    access_token: str,
    refresh_token: str,
    expires_in: object,
) -> None:
    if not cli_auth:
        return
    try:
        from sibyl_cli.auth_store import set_tokens
    except Exception:
        return
    expires = expires_in if isinstance(expires_in, int) and not isinstance(expires_in, bool) else None
    try:
        set_tokens(
            cli_auth["api_url"],
            access_token,
            refresh_token=refresh_token,
            expires_in=expires,
            credential_scope=cli_auth.get("credential_scope") or None,
        )
    except Exception:
        return


def _param_str(params: dict[str, object], key: str, default: str) -> str:
    value = params.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _param_bool(params: dict[str, object], key: str, default: bool) -> bool:
    value = params.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _param_int(
    params: dict[str, object],
    key: str,
    default: int,
    *,
    minimum: int = 1,
) -> int:
    value = params.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(minimum, value)
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return max(minimum, int(value))
    return default


def _param_float(params: dict[str, object], key: str, default: float) -> float:
    value = params.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value.strip()))
        except ValueError:
            return default
    return default


def _param_context_expansion_ratio(
    params: dict[str, object], key: str, default: float
) -> float:
    value = params.get(key, default)
    if isinstance(value, bool):
        raise TypeError(f"{key} must be numeric")
    if not isinstance(value, int | float | str):
        raise TypeError(f"{key} must be numeric")
    try:
        ratio = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{key} must be numeric") from exc
    if not math.isfinite(ratio) or ratio < 0.0 or 0.0 < ratio < 1.0:
        raise ValueError(f"{key} must be zero or at least 1.0")
    return ratio


def _stripped_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
