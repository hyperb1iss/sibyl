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
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar
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
from sibyl_core.models import EntityType, OperationalExperience  # noqa: E402
from sibyl_core.projection import project_operational_experience  # noqa: E402
from sibyl_core.retrieval.query_ranking import (  # noqa: E402
    QueryCoverageCandidate,
    QueryCoverageRankedCandidate,
    QueryCoverageResult,
    rank_by_query_coverage,
)
from sibyl_core.retrieval.refinement import (  # noqa: E402
    MAX_REFINEMENT_QUERIES,
    plan_deterministic_refinement_queries,
)

try:
    from memory_modules.memory import Memory, MemoryContextItem
    from memory_modules.memory import register_memory as _register_memory
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

    def _register_memory(memory_cls: type[Memory]) -> type[Memory]:
        return memory_cls


MemoryT = TypeVar("MemoryT", bound=Memory)


def register_memory(memory_cls: type[MemoryT]) -> type[MemoryT]:
    _register_memory(memory_cls)
    return memory_cls


DEFAULT_API_URL = "http://127.0.0.1:3334/api"
DEFAULT_CONTENT_MAX_CHARS = 18_000
DEFAULT_SEARCH_LIMIT = 12
DEFAULT_CONTEXT_ITEMS = 8
DEFAULT_CONTEXT_CHARS_PER_ITEM = 18_000
DEFAULT_CONTEXT_TOTAL_CHARS = 60_000
QUERY_SLICE_RENDERING_VERSION = "query-aware-source-windows-v5"
QUERY_SLICE_WINDOW_LINES = 8
QUERY_SLICE_WINDOW_STRIDE_LINES = 4
QUERY_SLICE_COMPACT_THRESHOLD_CHARS = 2400
QUERY_SLICE_COMPACT_WINDOW_LINES = 4
QUERY_SLICE_SUCCESSOR_LINES = 8
QUERY_SLICE_MAX_WINDOWS = 4
QUERY_SLICE_STRUCTURED_WINDOWS = 2
QUERY_SLICE_STRUCTURED_RADIUS_LINES = 6
QUERY_SLICE_STRUCTURED_SECTION_LINES = 48
QUERY_SLICE_STRUCTURED_TRAILING_LINES = 0
QUERY_SLICE_DESCENDANT_ROLE_WEIGHT = 2
QUERY_SLICE_MIN_EXCERPT_CHARS = 160
MAX_BUNDLED_SOURCE_CHARS = 12_000
DEFAULT_EVIDENCE_COMPOSITION_MODE = "shared_relevance"
EVIDENCE_COMPOSITION_MODES = frozenset({"reserved_support", "shared_relevance"})
DEFAULT_SOURCE_EVIDENCE_BUNDLING = False
DEFAULT_RETRIEVAL_MODE = "fast"
RETRIEVAL_MODES = frozenset({"accurate", "fast"})
DEFAULT_TYPED_STREAM_RETRIEVAL = False
DEFAULT_TYPED_STREAM_LIMIT = 8
TYPED_STREAM_TYPES = ("event", "procedure", "error_pattern")
DEFAULT_RETRIEVAL_MAX_PLANNED_QUERIES = 3
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
SAVED_MEMORY_SECRET_KEYS = frozenset(
    {"api_token", "api_credentials_path", "refresh_token", "email", "password"}
)
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
        "max_context_total_chars",
        "max_chunks_per_trajectory",
        "neighbor_stitch_items",
        "neighbor_stitch_span",
        "state_part_completion_items",
        "state_part_refinement",
        "context_expansion_max_ratio",
        "evidence_composition_mode",
        "source_evidence_bundling",
        "retrieval_mode",
        "retrieval_max_planned_queries",
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

_QUERY_EXCLUSION_PATTERN = re.compile(
    r"\b(?:excluding|except(?:\s+for)?|other\s+than)\b.*?"
    r"(?=,\s*(?:which|what|who|where|when|how)\b|[?.!]|$)",
    re.IGNORECASE | re.DOTALL,
)
_QUERY_QUOTED_PHRASE_PATTERN = re.compile(
    r"`(?P<backtick>[^`\n]{1,160})`"
    r'|"(?P<double>[^"\n]{1,160})"'
    r"|(?<!\w)'(?P<single>[^'\n]{2,160})'(?!\w)"
)
_QUERY_TARGET_FOCUS_PATTERN = re.compile(
    r"\b(?:which|what)\s+"
    r"(?P<phrase>(?:[\w-]+\s+){1,5}?)"
    r"(?:choices?|options?|labels?|fields?|entries?|columns?|buttons?|links?|tabs?|"
    r"checkboxes?|rows?|records?)\b",
    re.IGNORECASE,
)
_QUERY_FOCUS_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "are",
        "button",
        "checkbox",
        "choice",
        "column",
        "entry",
        "field",
        "five",
        "four",
        "is",
        "label",
        "link",
        "one",
        "option",
        "record",
        "row",
        "tab",
        "the",
        "these",
        "those",
        "three",
        "two",
        "what",
        "which",
    }
)
_QUERY_UI_ROLE_PATTERNS = (
    (
        re.compile(r"\b(?:drop[ -]?down|menu\s+items?|menuitems?|options?)\b"),
        ("menuitem", "option"),
    ),
    (re.compile(r"\bchoices?\b"), ("option", "checkbox", "radio")),
    (re.compile(r"\bselected\s+(?:pane|list|labels?)\b"), ("option",)),
    (re.compile(r"\bbuttons?\b"), ("button",)),
    (re.compile(r"\blinks?\b"), ("link", "button")),
    (re.compile(r"\btabs?\b"), ("tab",)),
    (re.compile(r"\bcolumns?\b"), ("columnheader",)),
    (re.compile(r"\b(?:rows?|records?)\b"), ("row", "gridcell")),
    (re.compile(r"\bcheckbox(?:es)?\b"), ("checkbox",)),
    (re.compile(r"\bradio(?:\s+buttons?)?\b"), ("radio",)),
    (
        re.compile(r"\b(?:fields?|inputs?|textboxes?|comboboxes?|searchboxes?)\b"),
        ("textbox", "combobox", "searchbox"),
    ),
)
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


def _trajectory_source_id(run_id: str, trajectory_id: str) -> str:
    return f"longmemeval-v2:{run_id}:{trajectory_id}"


def _source_metadata_mismatch_receipt(
    mismatches: Iterable[tuple[str, str]],
) -> dict[str, object]:
    ordered = list(mismatches)
    digest = hashlib.sha256()
    for entity_id, source_id in ordered:
        digest.update(entity_id.encode())
        digest.update(b"\0")
        digest.update(source_id.encode())
        digest.update(b"\0")
    return {
        "source_metadata_mismatch_sample": [entity_id for entity_id, _ in ordered[:20]],
        "source_metadata_mismatches_sha256": digest.hexdigest(),
    }


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
    chunk_builder = (
        _grouped_trajectory_chunks if chunking_mode == "trajectory" else _trajectory_chunks
    )
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
                    "source_id": _trajectory_source_id(run_id, trajectory.id),
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
            "source_id": _trajectory_source_id(run_id, trajectory.id),
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


def build_operational_session_payloads_for_trajectory(
    trajectory_raw: dict[str, object],
    *,
    project_id: str,
    run_id: str,
    content_max_chars: int = DEFAULT_CONTENT_MAX_CHARS,
    include_screenshot_refs: bool = False,
) -> list[dict[str, object]]:
    payload = build_operational_experience_payload(
        trajectory_raw,
        project_id=project_id,
        run_id=run_id,
        content_max_chars=content_max_chars,
        include_screenshot_refs=include_screenshot_refs,
    )
    experience = OperationalExperience.model_validate(payload["experience"])
    projection = project_operational_experience(experience)
    return [
        entity.model_dump(mode="json", exclude_none=True)
        for entity in projection.entities
        if entity.entity_type is EntityType.SESSION
    ]


def context_pack_to_search_results(
    response: dict[str, object],
    *,
    query: str = "",
    include_source_support: bool = DEFAULT_SOURCE_EVIDENCE_BUNDLING,
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
            supports = _source_supports(item)
            support = _best_source_support(supports, query=query)
            if support is not None:
                source_content = _stripped_str(support.get("content"))
                typed_content = _stripped_str(candidate.get("content"))
                if include_source_support:
                    candidate["content"] = _clean_evidence_bundle(
                        typed_content=typed_content,
                        source_content=source_content,
                    )
                metadata = _string_key_dict(candidate.get("metadata"))
                metadata["source_support_entity_id"] = _stripped_str(support.get("id"))
                metadata["source_support_relationship"] = _stripped_str(support.get("relationship"))
                support_metadata = _string_key_dict(support.get("metadata"))
                support_ordinal = support_metadata.get("observation_ordinal")
                if isinstance(support_ordinal, int) and not isinstance(support_ordinal, bool):
                    metadata["source_support_state_indices"] = [support_ordinal]
                for key in ("operational_source_id", "source_observation_id", "evidence_part_id"):
                    value = support_metadata.get(key)
                    if value is not None:
                        metadata[f"source_support_{key}"] = value
                source_support_states = [
                    pointer
                    for source in supports
                    if (pointer := _source_support_state(source)) is not None
                ]
                if source_support_states:
                    metadata["source_support_states"] = source_support_states
                candidate["metadata"] = metadata
            candidate["_selection_origin"] = f"context_pack:{facet}"
            candidates.append(candidate)
    type_order = {"procedure": 0, "error_pattern": 1, "event": 2}
    return sorted(
        candidates,
        key=lambda item: (
            type_order.get(_stripped_str(item.get("type")), 3),
            -_numeric_score(item.get("score")),
            _stripped_str(item.get("id")),
        ),
    )


def merge_typed_stream_results(
    pack_typed: list[dict[str, object]],
    stream_typed: list[dict[str, object]],
) -> list[dict[str, object]]:
    seen = {item_id for item in pack_typed if (item_id := _stripped_str(item.get("id")))}
    merged = list(pack_typed)
    for item in stream_typed:
        item_id = _stripped_str(item.get("id"))
        if item_id and item_id in seen:
            continue
        merged.append(item)
        if item_id:
            seen.add(item_id)
    return merged


def _required_context_evidence(
    response: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    evidence = response.get("evidence")
    if not isinstance(evidence, dict):
        raise RuntimeError("context pack response is missing required enhanced evidence")
    results = evidence.get("results")
    filters = evidence.get("filters")
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise RuntimeError("context pack evidence results have an invalid shape")
    if not isinstance(filters, dict):
        raise RuntimeError("context pack evidence filters have an invalid shape")
    return (
        [_string_key_dict(item) for item in results],
        _string_key_dict(filters),
    )


def _source_supports(item: dict[str, object]) -> list[dict[str, object]]:
    related = item.get("related")
    if not isinstance(related, list):
        return []
    return [
        _string_key_dict(candidate)
        for candidate in related
        if isinstance(candidate, dict)
        and _stripped_str(candidate.get("relationship")) == "DERIVED_FROM"
        and _stripped_str(candidate.get("direction")) == "outgoing"
        and _stripped_str(candidate.get("content"))
    ]


def _best_source_support(
    supports: list[dict[str, object]],
    *,
    query: str,
) -> dict[str, object] | None:
    if not supports:
        return None
    ranking = rank_by_query_coverage(
        query,
        [
            QueryCoverageCandidate(
                item=support,
                stable_id=_stripped_str(support.get("id")) or str(index),
                text=_stripped_str(support.get("content")),
                prior_score=0.0,
                original_rank=index,
            )
            for index, support in enumerate(supports, start=1)
        ],
    )
    best = max(
        ranking.ranked,
        key=lambda candidate: (
            candidate.overlap,
            candidate.score,
            -candidate.original_rank,
        ),
    )
    return dict(best.item)


def _source_support_state(support: dict[str, object]) -> dict[str, object] | None:
    metadata = _string_key_dict(support.get("metadata"))
    operational_source_id = _stripped_str(metadata.get("operational_source_id"))
    observation_ordinal = metadata.get("observation_ordinal")
    if (
        not operational_source_id
        or not isinstance(observation_ordinal, int)
        or isinstance(observation_ordinal, bool)
    ):
        return None
    _, separator, trajectory_id = operational_source_id.rpartition(":")
    if not separator or not trajectory_id:
        return None
    return {
        "entity_id": _stripped_str(support.get("id")),
        "operational_source_id": operational_source_id,
        "trajectory_id": trajectory_id,
        "state_index": observation_ordinal,
    }


def _clean_evidence_bundle(*, typed_content: str, source_content: str) -> str:
    parts = []
    if typed_content:
        parts.append(f"Typed projection:\n{typed_content}")
    if source_content:
        parts.append(f"Source evidence:\n{source_content[:MAX_BUNDLED_SOURCE_CHARS].rstrip()}")
    return "\n\n".join(parts)


def _flatten_operational_result_metadata(
    results: list[dict[str, object]],
) -> list[dict[str, object]]:
    flattened: list[dict[str, object]] = []
    for result in results:
        candidate = dict(result)
        candidate["metadata"] = _flatten_operational_metadata(candidate.get("metadata"))
        flattened.append(candidate)
    return flattened


def _flatten_operational_metadata(value: object) -> dict[str, object]:
    metadata = _string_key_dict(value)
    for nested_key in ("source_metadata", "evidence_metadata"):
        nested = metadata.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for key, nested_value in nested.items():
            metadata.setdefault(str(key), nested_value)
    return metadata


def compile_operational_evidence_set(
    *,
    query: str,
    typed_results: list[dict[str, object]],
    raw_results: list[dict[str, object]],
    max_items: int,
    mode: str = DEFAULT_EVIDENCE_COMPOSITION_MODE,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    if mode not in EVIDENCE_COMPOSITION_MODES:
        msg = f"Unknown evidence composition mode {mode!r}; expected {sorted(EVIDENCE_COMPOSITION_MODES)}"
        raise ValueError(msg)
    max_items = max(1, max_items)
    typed_candidates: list[dict[str, object]] = []
    raw_candidates: list[dict[str, object]] = []
    seen_typed: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    for result in typed_results:
        metadata = _string_key_dict(result.get("metadata"))
        key = (
            _stripped_str(result.get("type")),
            _stripped_str(metadata.get("longmemeval_v2_trajectory_id")),
        )
        if key in seen_typed:
            continue
        result_id = _stripped_str(result.get("id"))
        if result_id and result_id in seen_ids:
            continue
        typed_candidates.append(dict(result))
        seen_typed.add(key)
        if result_id:
            seen_ids.add(result_id)
    for result in raw_results:
        result_id = _stripped_str(result.get("id"))
        if result_id and result_id in seen_ids:
            continue
        raw_candidates.append(dict(result))
        if result_id:
            seen_ids.add(result_id)

    candidates = [*typed_candidates, *raw_candidates]
    if mode == "reserved_support":
        typed_budget = min(3, max(1, max_items // 3))
        selected = [*typed_candidates[:typed_budget], *raw_candidates][:max_items]
        selected_typed = sum(
            _stripped_str(item.get("_selection_origin")).startswith("context_pack:")
            for item in selected
        )
        return selected, {
            "mode": mode,
            "candidate_count": len(candidates),
            "typed_candidate_count": len(seen_typed),
            "raw_candidate_count": len(candidates) - len(seen_typed),
            "ranking_applied": False,
            "ranking_changed": False,
            "selected_raw_support_count": sum(
                _stripped_str(item.get("_selection_origin")) in {"neighbor", "state_part"}
                for item in selected
            ),
            "selected_typed_count": selected_typed,
            "selected_raw_count": len(selected) - selected_typed,
        }

    ranked_typed, typed_ranking = _rank_operational_evidence_pool(
        query,
        typed_candidates,
        pool="typed",
    )
    ranked_raw, raw_ranking = _rank_operational_evidence_pool(
        query,
        raw_candidates,
        pool="raw",
    )
    typed_reservation = min(len(ranked_typed), max(1, math.ceil(max_items * 3 / 8)))
    raw_budget = min(len(ranked_raw), max_items - typed_reservation)
    selected_raw = _select_role_complete_raw_evidence(ranked_raw, budget=raw_budget)
    selected = [*ranked_typed[:typed_reservation], *selected_raw]
    if len(selected) < max_items:
        selected.extend(
            ranked_typed[typed_reservation : typed_reservation + max_items - len(selected)]
        )
    for selection_rank, candidate in enumerate(selected, start=1):
        candidate["_evidence_selection_rank"] = selection_rank

    selected_typed = sum(
        _stripped_str(item.get("_selection_origin")).startswith("context_pack:")
        for item in selected
    )
    return selected, {
        "mode": mode,
        "candidate_count": len(candidates),
        "typed_candidate_count": len(seen_typed),
        "raw_candidate_count": len(candidates) - len(seen_typed),
        "ranking_applied": typed_ranking.applied or raw_ranking.applied,
        "ranking_changed": typed_ranking.changed or raw_ranking.changed,
        "pool_calibration": "independent_query_coverage",
        "typed_reservation": typed_reservation,
        "selected_typed_overflow_count": max(0, selected_typed - typed_reservation),
        "selected_raw_support_count": sum(
            _stripped_str(item.get("_selection_origin")) in {"neighbor", "state_part"}
            for item in selected
        ),
        "selected_typed_count": selected_typed,
        "selected_raw_count": len(selected) - selected_typed,
    }


def _rank_operational_evidence_pool(
    query: str,
    candidates: list[dict[str, object]],
    *,
    pool: str,
) -> tuple[list[dict[str, object]], QueryCoverageResult[dict[str, object]]]:
    ranking = rank_by_query_coverage(
        query,
        [
            QueryCoverageCandidate(
                item=candidate,
                stable_id=_result_stable_id(candidate),
                text=_evidence_ranking_text(candidate),
                prior_score=_numeric_score(candidate.get("score")),
                original_rank=index,
            )
            for index, candidate in enumerate(candidates, start=1)
        ],
    )
    ranked_by_id = {candidate.stable_id: candidate for candidate in ranking.ranked}
    ordered_candidates = candidates if pool == "raw" else [row.item for row in ranking.ranked]
    ranked_candidates: list[dict[str, object]] = []
    ordered_ranking: list[QueryCoverageRankedCandidate[dict[str, object]]] = []
    for pool_rank, item in enumerate(ordered_candidates, start=1):
        stable_id = _result_stable_id(item)
        ranked = ranked_by_id[stable_id]
        candidate = dict(item)
        candidate["_evidence_selection_pool"] = pool
        candidate["_evidence_pool_rank"] = pool_rank
        candidate["_evidence_selection_score"] = ranked.score
        candidate["_evidence_selection_overlap"] = ranked.overlap
        ranked_candidates.append(candidate)
        ordered_ranking.append(
            QueryCoverageRankedCandidate(
                item=candidate,
                stable_id=stable_id,
                score=ranked.score,
                original_rank=ranked.original_rank,
                overlap=ranked.overlap,
            )
        )
    return ranked_candidates, QueryCoverageResult(
        ranked=ordered_ranking,
        applied=ranking.applied,
        changed=ranking.changed if pool != "raw" else False,
    )


def _evidence_ranking_text(candidate: dict[str, object]) -> str:
    content = _stripped_str(candidate.get("content"))
    if _stripped_str(candidate.get("type")) != "session":
        return content
    state_match = re.search(r"(?m)^State\s+\d+\b", content)
    return content[state_match.start() :] if state_match else content


def _select_role_complete_raw_evidence(
    candidates: list[dict[str, object]],
    *,
    budget: int,
) -> list[dict[str, object]]:
    if budget <= 0:
        return []
    primary = [candidate for candidate in candidates if not _is_support_candidate(candidate)]
    primary_by_search_rank: dict[int, dict[str, object]] = {}
    for candidate in primary:
        search_rank = _optional_positive_int(candidate.get("_search_rank"))
        if search_rank is not None:
            primary_by_search_rank.setdefault(search_rank, candidate)
    linked_support = [
        (candidate, primary_by_search_rank[parent_rank])
        for candidate in candidates
        if _is_support_candidate(candidate)
        and (parent_rank := _support_parent_search_rank(candidate)) in primary_by_search_rank
        and _support_adds_query_coverage(candidate, primary_by_search_rank[parent_rank])
    ]
    selected_links = _select_diverse_support_links(linked_support, limit=budget // 2)
    selected_support = [support for support, _parent in selected_links]
    selected_primary: list[dict[str, object]] = []
    selected_primary_ids: set[int] = set()
    for _support, parent in selected_links:
        if id(parent) not in selected_primary_ids:
            selected_primary.append(parent)
            selected_primary_ids.add(id(parent))
    primary_budget = budget - len(selected_support)
    for candidate in primary:
        if id(candidate) not in selected_primary_ids and len(selected_primary) < primary_budget:
            selected_primary.append(candidate)
            selected_primary_ids.add(id(candidate))
    candidate_order = {id(candidate): index for index, candidate in enumerate(candidates)}
    selected_primary.sort(key=lambda candidate: candidate_order[id(candidate)])
    selected_support_ids = {id(candidate) for candidate in selected_support}
    for support, parent in linked_support:
        if (
            id(support) not in selected_support_ids
            and id(parent) in selected_primary_ids
            and len(selected_primary) + len(selected_support) < budget
        ):
            selected_support.append(support)
            selected_support_ids.add(id(support))
    support_by_parent_id: dict[int, list[dict[str, object]]] = {}
    for support, parent in linked_support:
        if id(support) in selected_support_ids:
            support_by_parent_id.setdefault(id(parent), []).append(support)
    selected: list[dict[str, object]] = []
    for candidate in selected_primary:
        selected.append(candidate)
        selected.extend(support_by_parent_id.get(id(candidate), []))
    return selected


def _support_adds_query_coverage(
    support: dict[str, object],
    parent: dict[str, object],
) -> bool:
    if _stripped_str(support.get("_selection_origin")) == "state_part":
        return True
    return _numeric_score(support.get("_evidence_selection_overlap")) > _numeric_score(
        parent.get("_evidence_selection_overlap")
    )


def _select_diverse_support_links(
    linked_support: list[tuple[dict[str, object], dict[str, object]]],
    *,
    limit: int,
) -> list[tuple[dict[str, object], dict[str, object]]]:
    if limit <= 0:
        return []
    selected: list[tuple[dict[str, object], dict[str, object]]] = []
    selected_support_ids: set[int] = set()
    selected_parent_ids: set[int] = set()
    selected_parent_roles: set[tuple[int, str]] = set()
    for diversity in ("parent", "role", "rank"):
        for support, parent in linked_support:
            if id(support) in selected_support_ids:
                continue
            parent_id = id(parent)
            role = _stripped_str(support.get("_selection_origin"))
            if diversity == "parent" and parent_id in selected_parent_ids:
                continue
            if diversity == "role" and (parent_id, role) in selected_parent_roles:
                continue
            selected.append((support, parent))
            selected_support_ids.add(id(support))
            selected_parent_ids.add(parent_id)
            selected_parent_roles.add((parent_id, role))
            if len(selected) >= limit:
                return selected
    return selected


def _support_parent_search_rank(candidate: dict[str, object]) -> int | None:
    for key in ("_neighbor_of_search_rank", "_state_part_of_search_rank"):
        if (rank := _optional_positive_int(candidate.get(key))) is not None:
            return rank
    return None


def _is_support_candidate(candidate: dict[str, object]) -> bool:
    return _stripped_str(candidate.get("_selection_origin")) in {"neighbor", "state_part"}


def _optional_positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def search_results_to_memory_context(
    results: list[dict[str, object]],
    *,
    query: str = "",
    max_items: int = DEFAULT_CONTEXT_ITEMS,
    max_chars_per_item: int = DEFAULT_CONTEXT_CHARS_PER_ITEM,
    max_total_chars: int = DEFAULT_CONTEXT_TOTAL_CHARS,
) -> list[MemoryContextItem]:
    context, _metadata = render_memory_context(
        results,
        query=query,
        max_items=max_items,
        max_chars_per_item=max_chars_per_item,
        max_total_chars=max_total_chars,
    )
    return context


_UI_INVENTORY_HEADER = "Observed UI inventory:"


def annotate_inventory_completeness(content: str, metadata: object) -> str:
    if _UI_INVENTORY_HEADER not in content or not isinstance(metadata, dict):
        return content
    item_count = metadata.get("ui_inventory_item_count")
    if not isinstance(item_count, int) or isinstance(item_count, bool) or item_count <= 0:
        return content
    if metadata.get("ui_inventory_truncated"):
        replacement = (
            f"Partial UI element inventory for this page state (first {item_count} "
            "elements; the full page had more, so absence of an element cannot be "
            "inferred from this list):"
        )
    else:
        replacement = (
            f"Complete UI element inventory for this page state ({item_count} "
            "elements; an element absent from this list was not present on this "
            "page):"
        )
    return content.replace(_UI_INVENTORY_HEADER, replacement, 1)


def render_memory_context(
    results: list[dict[str, object]],
    *,
    query: str = "",
    max_items: int = DEFAULT_CONTEXT_ITEMS,
    max_chars_per_item: int = DEFAULT_CONTEXT_CHARS_PER_ITEM,
    max_total_chars: int = DEFAULT_CONTEXT_TOTAL_CHARS,
) -> tuple[list[MemoryContextItem], dict[str, object]]:
    max_items = max(1, max_items)
    max_chars_per_item = max(1, max_chars_per_item)
    max_total_chars = max(1, max_total_chars)
    rows: list[tuple[int, dict[str, object], str, str]] = []
    for rank, result in enumerate(results[:max_items], start=1):
        content = _stripped_str(result.get("content"))
        if not content:
            continue
        content = annotate_inventory_completeness(content, result.get("metadata"))
        rows.append((rank, result, _memory_context_header(rank, result), content))

    candidate_rows = list(rows)
    per_item_limited_chars = sum(
        len(header) + 2 + min(len(content), max_chars_per_item)
        for _rank, _result, header, content in candidate_rows
    )
    dropped: list[tuple[int, dict[str, object], str, str]] = []
    while (
        rows and sum(len(header) + 3 for _rank, _result, header, _content in rows) > max_total_chars
    ):
        dropped.append(rows.pop())

    context: list[MemoryContextItem] = []
    budget_items: list[dict[str, object]] = []
    remaining_content_chars = max_total_chars - sum(
        len(header) + 2 for _rank, _result, header, _content in rows
    )
    content_allocations = _allocate_context_chars(
        [min(len(content), max_chars_per_item) for _rank, _result, _header, content in rows],
        budget=remaining_content_chars,
    )
    for (rank, result, header, content), allocation in zip(
        rows,
        content_allocations,
        strict=True,
    ):
        exposed_content, compaction = compact_content_for_query(
            query,
            content,
            max_chars=allocation,
        )
        context.append(
            {
                "type": "text",
                "value": header + "\n\n" + exposed_content,
            }
        )
        budget_items.append(
            {
                "rank": rank,
                "entity_id": _stripped_str(result.get("id")),
                "content_chars": len(content),
                "exposed_content_chars": len(exposed_content),
                "truncated": len(exposed_content) < len(content),
                "dropped": False,
                "compaction": compaction,
            }
        )
    budget_items.extend(
        {
            "rank": rank,
            "entity_id": _stripped_str(result.get("id")),
            "content_chars": len(content),
            "exposed_content_chars": 0,
            "truncated": True,
            "dropped": True,
        }
        for rank, result, _header, content in dropped
    )
    budget_items.sort(key=lambda item: int(item["rank"]))
    rendered_chars = sum(len(item["value"]) for item in context)
    return context, {
        "enabled": True,
        "max_total_chars": max_total_chars,
        "max_chars_per_item": max_chars_per_item,
        "candidate_item_count": len(rows) + len(dropped),
        "rendered_item_count": len(context),
        "dropped_item_count": len(dropped),
        "dropped_entity_ids": [
            _stripped_str(result.get("id"))
            for _rank, result, _header, _content in reversed(dropped)
        ],
        "per_item_limited_chars": per_item_limited_chars,
        "rendered_context_chars": rendered_chars,
        "truncated_item_count": sum(bool(item["truncated"]) for item in budget_items),
        "binding": bool(dropped) or rendered_chars < per_item_limited_chars,
        "items": budget_items,
    }


def _allocate_context_chars(capacities: list[int], *, budget: int) -> list[int]:
    allocations = [0] * len(capacities)
    active = list(range(len(capacities)))
    remaining = max(0, budget)
    while active and remaining > 0:
        fair_share, remainder = divmod(remaining, len(active))
        saturated = [index for index in active if capacities[index] <= fair_share]
        if saturated:
            for index in saturated:
                allocations[index] = capacities[index]
                remaining -= capacities[index]
            saturated_set = set(saturated)
            active = [index for index in active if index not in saturated_set]
            continue
        for position, index in enumerate(active):
            allocations[index] = fair_share + int(position < remainder)
        break
    return allocations


def compact_content_for_query(
    query: str,
    content: str,
    *,
    max_chars: int,
) -> tuple[str, dict[str, object]]:
    max_chars = max(0, max_chars)
    base_metadata: dict[str, object] = {
        "version": QUERY_SLICE_RENDERING_VERSION,
        "source_content_chars": len(content),
        "max_chars": max_chars,
        "query_focus_phrases": list(_query_focus_phrases(query)),
        "query_ui_roles": list(_query_ui_roles(query)),
        "candidate_window_count": 0,
        "selected_window_count": 0,
        "structured_selected_window_count": 0,
        "ranking_applied": False,
        "selected_source_ranges": [],
    }
    if len(content) <= max_chars:
        return content, {**base_metadata, "mode": "full", "omitted_source_chars": 0}
    if not query.strip() or max_chars < QUERY_SLICE_MIN_EXCERPT_CHARS:
        return content[:max_chars], {
            **base_metadata,
            "mode": "prefix",
            "omitted_source_chars": len(content) - max_chars,
        }

    lines = content.splitlines(keepends=True)
    line_starts = _line_start_offsets(lines)
    window_lines, stride_lines, max_windows = _window_geometry(max_chars)
    base_metadata["window_lines"] = window_lines
    candidates, envelope_end_line = _windowed_candidates(
        lines,
        line_starts,
        query=query,
        window_lines=window_lines,
        stride_lines=stride_lines,
        base_metadata=base_metadata,
    )
    if candidates is None:
        return content[:max_chars], {
            **base_metadata,
            "mode": "prefix",
            "omitted_source_chars": len(content) - max_chars,
        }

    ranking = _coverage_ranking_with_fallback(query, candidates, base_metadata)
    if ranking is None:
        return content[:max_chars], {
            **base_metadata,
            "mode": "prefix",
            "omitted_source_chars": len(content) - max_chars,
        }

    covered = [ranked for ranked in ranking.ranked if ranked.overlap > 0.0]
    selection_order = covered or ranking.ranked
    selected: list[dict[str, object]] = []
    structured_order = sorted(
        ranking.ranked,
        key=lambda ranked: (
            _query_structured_candidate_rank(query, ranked.item),
            ranked.score,
            -ranked.original_rank,
        ),
        reverse=True,
    )
    structured_selected = 0
    for ranked in structured_order:
        signal = _query_structured_signal(query, str(ranked.item["ranking_text"]))
        if signal[0] <= 0:
            break
        candidate = dict(ranked.item)
        candidate["ranking_score"] = ranked.score
        candidate["ranking_overlap"] = ranked.overlap
        candidate["structured_signal"] = list(signal)
        if int(candidate["window_end_char"]) - int(candidate["window_start_char"]) >= max_chars:
            continue
        if (
            _render_query_slices(
                content,
                lines=lines,
                line_starts=line_starts,
                envelope_end_line=envelope_end_line,
                selected=[candidate],
                max_chars=max_chars,
            )
            is None
        ):
            continue
        if any(_query_slice_windows_overlap(candidate, existing) for existing in selected):
            continue
        selected.append(candidate)
        structured_selected += 1
        if structured_selected >= QUERY_SLICE_STRUCTURED_WINDOWS:
            break
    for ranked in selection_order:
        candidate = dict(ranked.item)
        if any(_query_slice_windows_overlap(candidate, existing) for existing in selected):
            continue
        candidate["ranking_score"] = ranked.score
        candidate["ranking_overlap"] = ranked.overlap
        selected.append(candidate)
        if len(selected) >= max_windows:
            break

    while selected:
        rendered = _render_query_slices(
            content,
            lines=lines,
            line_starts=line_starts,
            envelope_end_line=envelope_end_line,
            selected=selected,
            max_chars=max_chars,
        )
        if rendered is not None:
            exposed, ranges, covered_chars = rendered
            return exposed, {
                **base_metadata,
                "mode": "query_slices",
                "selected_window_count": len(ranges),
                "structured_selected_window_count": structured_selected,
                "selected_source_ranges": ranges,
                "omitted_source_chars": max(0, len(content) - covered_chars),
            }
        selected.pop()

    return content[:max_chars], {
        **base_metadata,
        "mode": "prefix",
        "omitted_source_chars": len(content) - max_chars,
    }


def _line_start_offsets(lines: list[str]) -> list[int]:
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    return starts


def _query_focus_phrases(query: str) -> tuple[str, ...]:
    focused_query = _QUERY_EXCLUSION_PATTERN.sub(" ", query)
    if not any(character.isalnum() for character in focused_query):
        return ()
    phrases: list[str] = []
    seen: set[str] = set()
    target_focus_added = False

    def add_phrase(raw_phrase: str) -> None:
        phrase = " ".join(raw_phrase.split())
        normalized = phrase.casefold()
        if not any(character.isalnum() for character in phrase) or normalized in seen:
            return
        phrases.append(phrase)
        seen.add(normalized)

    for match in _QUERY_QUOTED_PHRASE_PATTERN.finditer(focused_query):
        add_phrase(next(group for group in match.groups() if group is not None))
    for match in _QUERY_TARGET_FOCUS_PATTERN.finditer(focused_query):
        phrase = str(match.group("phrase") or "").strip()
        tokens = re.findall(r"[\w-]+", phrase.casefold())
        if not tokens or all(token in _QUERY_FOCUS_STOPWORDS for token in tokens):
            continue
        phrase_count = len(phrases)
        add_phrase(phrase)
        target_focus_added = target_focus_added or len(phrases) > phrase_count
    structural_queries = plan_deterministic_refinement_queries(
        focused_query,
        [],
        max_queries=MAX_REFINEMENT_QUERIES,
    )
    target_queries = [query for query in structural_queries if query.facet == "target"]
    for structural_query in target_queries:
        for term in structural_query.added_terms:
            if term.casefold() in {*_QUERY_FOCUS_STOPWORDS, "list", "page"}:
                continue
            add_phrase(term)
    if not target_focus_added and not target_queries:
        for structural_query in structural_queries:
            if structural_query.facet != "focus_clause":
                continue
            structural_terms = [
                token
                for token in re.findall(r"[A-Za-z][\w-]*", structural_query.query)
                if token.casefold() not in _QUERY_FOCUS_STOPWORDS and token.casefold() not in seen
            ]
            if len(structural_terms) < 2:
                continue
            for term in structural_terms:
                add_phrase(term)
    return tuple(phrases)


def _query_ui_roles(query: str) -> tuple[str, ...]:
    normalized = query.casefold()
    roles: list[str] = []
    for pattern, markers in _QUERY_UI_ROLE_PATTERNS:
        if not pattern.search(normalized):
            continue
        roles.extend(marker for marker in markers if marker not in roles)
    return tuple(roles)


def _query_structured_signal(query: str, text: str) -> tuple[int, int, int, int, int]:
    focus_phrases = tuple(phrase.casefold() for phrase in _query_focus_phrases(query))
    roles = _query_ui_roles(query)
    if not focus_phrases or not roles:
        return 0, 0, 0, 0, 0
    lines = text.casefold().splitlines()
    role_line_indices = {
        index
        for index, line in enumerate(lines)
        if any(re.search(rf"\b{re.escape(role)}\b", line) for role in roles)
    }
    weighted_proximity = 0
    proximity = 0
    matched_phrases: set[str] = set()
    matched_phrase_priority = 0
    for phrase_index, phrase in enumerate(focus_phrases, start=1):
        phrase_line_indices = {
            line_index for line_index, line in enumerate(lines) if phrase in line
        }
        nearby_role_lines = {
            role_line_index
            for role_line_index in role_line_indices
            if any(
                abs(role_line_index - phrase_line_index) <= QUERY_SLICE_STRUCTURED_RADIUS_LINES
                for phrase_line_index in phrase_line_indices
            )
        }
        for role_line_index in nearby_role_lines:
            strength = max(
                (QUERY_SLICE_STRUCTURED_RADIUS_LINES + 1 - abs(role_line_index - phrase_line_index))
                * (
                    QUERY_SLICE_DESCENDANT_ROLE_WEIGHT
                    if role_line_index >= phrase_line_index
                    else 1
                )
                for phrase_line_index in phrase_line_indices
            )
            proximity += strength
            weighted_proximity += phrase_index * strength
        if nearby_role_lines:
            matched_phrases.add(phrase)
            matched_phrase_priority = max(matched_phrase_priority, phrase_index)
    return (
        weighted_proximity,
        len(matched_phrases),
        proximity,
        len(role_line_indices),
        matched_phrase_priority,
    )


def _query_structured_rank(
    signal: tuple[int, int, int, int, int],
) -> tuple[int, int, int, int, int]:
    return signal[1], signal[4], signal[0], signal[2], -signal[3]


def _query_structured_candidate_rank(
    query: str,
    candidate: dict[str, object],
) -> tuple[int, int, int, int, int, int]:
    rank = _query_structured_rank(_query_structured_signal(query, str(candidate["ranking_text"])))
    return rank[:2] + (int(bool(candidate.get("structured_section"))),) + rank[2:]


_TOKEN_OVERLAP_STOPWORDS = frozenset(
    (
        "the",
        "and",
        "for",
        "was",
        "were",
        "with",
        "what",
        "which",
        "when",
        "where",
        "that",
        "this",
        "from",
        "have",
        "has",
        "had",
        "you",
        "your",
        "are",
        "is",
        "not",
        "but",
        "all",
        "any",
        "can",
        "could",
        "would",
        "should",
        "does",
        "did",
    )
)
QUERY_SLICE_MIN_CANDIDATES = 2


def _window_geometry(max_chars: int) -> tuple[int, int, int]:
    compact_allocation = max_chars < QUERY_SLICE_COMPACT_THRESHOLD_CHARS
    window_lines = (
        QUERY_SLICE_COMPACT_WINDOW_LINES if compact_allocation else QUERY_SLICE_WINDOW_LINES
    )
    stride_lines = max(1, window_lines // 2)
    max_windows = QUERY_SLICE_MAX_WINDOWS * (2 if compact_allocation else 1)
    return window_lines, stride_lines, max_windows


def _windowed_candidates(
    lines: list[str],
    line_starts: list[int],
    *,
    query: str,
    window_lines: int,
    stride_lines: int,
    base_metadata: dict[str, object],
) -> tuple[list[dict[str, object]] | None, int]:
    candidates, envelope_end_line = _query_slice_candidates(
        lines,
        line_starts,
        query=query,
        window_lines=window_lines,
        stride_lines=stride_lines,
    )
    if len(candidates) < QUERY_SLICE_MIN_CANDIDATES:
        candidates = _stride_window_candidates(
            lines,
            line_starts,
            window_lines=window_lines,
            stride_lines=stride_lines,
        )
        envelope_end_line = 0
        base_metadata["stride_window_fallback"] = True
    base_metadata["candidate_window_count"] = len(candidates)
    if len(candidates) < QUERY_SLICE_MIN_CANDIDATES:
        return None, 0
    return candidates, envelope_end_line


def _coverage_ranking_with_fallback(
    query: str,
    candidates: list[dict[str, object]],
    base_metadata: dict[str, object],
) -> QueryCoverageResult[dict[str, object]] | None:
    ranking = rank_by_query_coverage(
        query,
        [
            QueryCoverageCandidate(
                item=candidate,
                stable_id=str(candidate["stable_id"]),
                text=str(candidate["ranking_text"]),
                prior_score=0.0,
                original_rank=index,
            )
            for index, candidate in enumerate(candidates, start=1)
        ],
    )
    base_metadata["ranking_applied"] = ranking.applied
    if ranking.applied and any(
        ranked.score > 0.0 or ranked.overlap > 0.0 for ranked in ranking.ranked
    ):
        return ranking
    fallback_ranked = _token_overlap_ranking(query, candidates)
    if fallback_ranked is None:
        return None
    base_metadata["ranking_applied"] = True
    base_metadata["token_overlap_fallback"] = True
    return QueryCoverageResult(ranked=fallback_ranked, applied=True, changed=True)


def _token_overlap_terms(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", text.casefold())
        if token not in _TOKEN_OVERLAP_STOPWORDS
    }


def _token_overlap_ranking(
    query: str,
    candidates: list[dict[str, object]],
) -> list[QueryCoverageRankedCandidate[dict[str, object]]] | None:
    query_terms = _token_overlap_terms(query)
    if not query_terms:
        return None
    scored: list[tuple[float, int, dict[str, object]]] = []
    for index, candidate in enumerate(candidates, start=1):
        window_terms = _token_overlap_terms(str(candidate["ranking_text"]))
        score = len(query_terms & window_terms) / len(query_terms)
        scored.append((score, index, candidate))
    if not any(score > 0.0 for score, _index, _candidate in scored):
        return None
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [
        QueryCoverageRankedCandidate(
            item=candidate,
            stable_id=str(candidate["stable_id"]),
            score=score,
            original_rank=index,
            overlap=score,
        )
        for score, index, candidate in scored
    ]


def _stride_window_candidates(
    lines: list[str],
    line_starts: list[int],
    *,
    window_lines: int = QUERY_SLICE_WINDOW_LINES,
    stride_lines: int = QUERY_SLICE_WINDOW_STRIDE_LINES,
) -> list[dict[str, object]]:
    if not lines:
        return []
    candidates: list[dict[str, object]] = []
    for window_start in range(0, len(lines), stride_lines):
        window_end = min(len(lines), window_start + window_lines)
        candidates.append(
            {
                "stable_id": f"stride:{window_start}:{window_end}",
                "state_start_line": window_start,
                "body_start_line": window_start,
                "window_start_line": window_start,
                "window_end_line": window_end,
                "window_start_char": line_starts[window_start],
                "window_end_char": line_starts[window_end],
                "ranking_text": "".join(lines[window_start:window_end]),
            }
        )
        if window_end >= len(lines):
            break
    return candidates


def _query_slice_candidates(
    lines: list[str],
    line_starts: list[int],
    *,
    query: str,
    window_lines: int = QUERY_SLICE_WINDOW_LINES,
    stride_lines: int = QUERY_SLICE_WINDOW_STRIDE_LINES,
) -> tuple[list[dict[str, object]], int]:
    state_starts = [
        index for index, line in enumerate(lines) if re.match(r"^State\s+\d+\s*$", line.rstrip())
    ]
    if not state_starts:
        return [], 0
    candidates: list[dict[str, object]] = []
    focus_phrases = tuple(phrase.casefold() for phrase in _query_focus_phrases(query))
    roles = _query_ui_roles(query)
    state_ends = [*state_starts[1:], len(lines)]
    for state_start, state_end in zip(state_starts, state_ends, strict=True):
        body_start = next(
            (
                index + 1
                for index in range(state_start, state_end)
                if lines[index].rstrip().startswith("Accessibility tree:")
            ),
            min(state_start + 5, state_end),
        )
        if body_start >= state_end:
            continue
        window_starts = set(range(body_start, state_end, stride_lines))
        for line_index in range(body_start, state_end):
            line = lines[line_index].casefold()
            if not any(phrase in line for phrase in focus_phrases):
                continue
            anchored_start = max(
                body_start,
                min(
                    line_index - 1,
                    max(body_start, state_end - window_lines),
                ),
            )
            window_starts.add(anchored_start)
        final_start = max(body_start, state_end - window_lines)
        window_starts.add(final_start)
        for window_start in sorted(window_starts):
            window_end = min(state_end, window_start + window_lines)
            window_end = _query_slice_successor_end(
                lines,
                window_end=window_end,
                state_end=state_end,
            )
            candidates.append(
                {
                    "stable_id": f"{state_start}:{window_start}:{window_end}",
                    "state_start_line": state_start,
                    "body_start_line": body_start,
                    "window_start_line": window_start,
                    "window_end_line": window_end,
                    "window_start_char": line_starts[window_start],
                    "window_end_char": line_starts[window_end],
                    "ranking_text": "".join(lines[window_start:window_end]),
                }
            )
        candidates.extend(
            _query_structured_section_candidates(
                lines,
                line_starts,
                body_start=body_start,
                state_start=state_start,
                state_end=state_end,
                focus_phrases=focus_phrases,
                roles=roles,
            )
        )
    return candidates, state_starts[0]


def _query_structured_section_candidates(
    lines: list[str],
    line_starts: list[int],
    *,
    body_start: int,
    state_start: int,
    state_end: int,
    focus_phrases: tuple[str, ...],
    roles: tuple[str, ...],
) -> list[dict[str, object]]:
    if not focus_phrases or not roles:
        return []
    candidates: list[dict[str, object]] = []
    for focus_line in range(body_start, state_end):
        normalized_line = lines[focus_line].casefold()
        if not any(phrase in normalized_line for phrase in focus_phrases):
            continue
        section_marker = re.search(
            r"\b(?:button|group|heading|legend|list|menu|region|tablist)\b",
            normalized_line,
        )
        labeled_text = (
            "statictext" in normalized_line
            and focus_line > body_start
            and "labeltext" in lines[focus_line - 1].casefold()
        )
        if not section_marker and not labeled_text:
            continue
        if any(re.search(rf"\b{re.escape(role)}\b", normalized_line) for role in roles):
            continue
        role_lines: list[int] = []
        section_limit = min(
            state_end,
            focus_line + QUERY_SLICE_STRUCTURED_SECTION_LINES,
        )
        focus_indent = _line_indent(lines[focus_line])
        for line_index in range(focus_line + 1, section_limit):
            line = lines[line_index]
            normalized = line.casefold()
            if (
                role_lines
                and _line_indent(line) <= focus_indent
                and re.search(r"\b(?:heading|legend|region)\b", normalized)
            ):
                break
            if any(re.search(rf"\b{re.escape(role)}\b", normalized) for role in roles):
                role_lines.append(line_index)
        if not role_lines:
            continue
        window_start = max(body_start, focus_line - 1)
        window_end = min(
            state_end,
            role_lines[-1] + QUERY_SLICE_STRUCTURED_TRAILING_LINES + 1,
        )
        window_end = _query_slice_successor_end(
            lines,
            window_end=window_end,
            state_end=state_end,
        )
        candidates.append(
            {
                "stable_id": (f"structured:{state_start}:{window_start}:{window_end}"),
                "state_start_line": state_start,
                "body_start_line": body_start,
                "window_start_line": window_start,
                "window_end_line": window_end,
                "window_start_char": line_starts[window_start],
                "window_end_char": line_starts[window_end],
                "ranking_text": "".join(lines[window_start:window_end]),
                "structured_section": True,
            }
        )
    return candidates


def _query_slice_successor_end(
    lines: list[str],
    *,
    window_end: int,
    state_end: int,
) -> int:
    if state_end >= len(lines) or any(line.strip() for line in lines[window_end:state_end]):
        return window_end
    return min(len(lines), state_end + QUERY_SLICE_SUCCESSOR_LINES)


def _line_indent(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _query_slice_windows_overlap(
    left: dict[str, object],
    right: dict[str, object],
) -> bool:
    return bool(
        int(left["window_start_char"]) < int(right["window_end_char"])
        and int(right["window_start_char"]) < int(left["window_end_char"])
    )


def _render_query_slices(
    content: str,
    *,
    lines: list[str],
    line_starts: list[int],
    envelope_end_line: int,
    selected: list[dict[str, object]],
    max_chars: int,
) -> tuple[str, list[dict[str, object]], int] | None:
    source_order = sorted(selected, key=lambda item: int(item["window_start_line"]))
    static_parts: list[str] = []
    if envelope_end_line:
        static_parts.append("".join(lines[:envelope_end_line]).rstrip())
    cursor_line = envelope_end_line
    ranges: list[dict[str, object]] = []
    excerpt_capacities: list[int] = []
    excerpt_minimums: list[int] = []
    excerpt_slots: list[int] = []
    covered_intervals: list[tuple[int, int]] = [(0, line_starts[envelope_end_line])]
    for candidate in source_order:
        state_start = int(candidate["state_start_line"])
        body_start = int(candidate["body_start_line"])
        window_start = int(candidate["window_start_line"])
        window_end = int(candidate["window_end_line"])
        window_start_char = int(candidate["window_start_char"])
        window_end_char = int(candidate["window_end_char"])
        omission_start = cursor_line
        if state_start >= cursor_line:
            if state_start > cursor_line:
                static_parts.append(f"[Omitted source lines {cursor_line + 1}-{state_start}]")
            omission_start = body_start
        if window_start > omission_start:
            static_parts.append(f"[Omitted source lines {omission_start + 1}-{window_start}]")
        static_parts.append(
            "[Source slice: "
            f"lines {window_start + 1}-{window_end}, "
            f"chars {window_start_char}-{window_end_char}]"
        )
        identity = "".join(lines[state_start:body_start]).rstrip()
        if identity:
            static_parts.append(identity)
        excerpt_slots.append(len(static_parts))
        static_parts.append("")
        excerpt_capacity = window_end_char - window_start_char
        excerpt_capacities.append(excerpt_capacity)
        excerpt_minimums.append(
            excerpt_capacity
            if candidate.get("structured_signal")
            else min(excerpt_capacity, QUERY_SLICE_MIN_EXCERPT_CHARS)
        )
        ranges.append(
            {
                "start_line": window_start + 1,
                "end_line": window_end,
                "start_char": window_start_char,
                "end_char": window_end_char,
                "ranking_score": candidate.get("ranking_score"),
                "ranking_overlap": candidate.get("ranking_overlap"),
                "structured_signal": candidate.get("structured_signal"),
            }
        )
        covered_intervals.extend(
            (
                (line_starts[state_start], line_starts[body_start]),
                (window_start_char, window_end_char),
            )
        )
        cursor_line = window_end
    if cursor_line < len(lines):
        static_parts.append(f"[Omitted source lines {cursor_line + 1}-{len(lines)}]")

    fixed_chars = sum(len(part) for part in static_parts) + max(0, len(static_parts) - 1) * 2
    available_chars = max_chars - fixed_chars
    minimum_needed = sum(excerpt_minimums)
    if available_chars < minimum_needed:
        return None
    extra_allocations = _allocate_context_chars(
        [
            capacity - minimum
            for capacity, minimum in zip(
                excerpt_capacities,
                excerpt_minimums,
                strict=True,
            )
        ],
        budget=available_chars - minimum_needed,
    )
    allocations = [
        minimum + extra for minimum, extra in zip(excerpt_minimums, extra_allocations, strict=True)
    ]
    for slot, allocation, source_range in zip(
        excerpt_slots,
        allocations,
        ranges,
        strict=True,
    ):
        static_parts[slot] = content[
            int(source_range["start_char"]) : int(source_range["start_char"]) + allocation
        ]
        source_range["exposed_chars"] = allocation
    rendered = "\n\n".join(static_parts)
    if len(rendered) > max_chars:
        raise RuntimeError("query-aware source rendering exceeded its character budget")
    return rendered, ranges, _merged_interval_chars(covered_intervals)


def _merged_interval_chars(intervals: list[tuple[int, int]]) -> int:
    total = 0
    merged_end = 0
    for start, end in sorted(intervals):
        if end <= merged_end:
            continue
        total += end - max(start, merged_end)
        merged_end = end
    return total


def _memory_context_header(rank: int, result: dict[str, object]) -> str:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    trajectory_id = _stripped_str(metadata.get("longmemeval_v2_trajectory_id"))
    chunk_index = metadata.get("longmemeval_v2_chunk_index")
    selection_origin = _stripped_str(result.get("_selection_origin")) or "search"
    lines = [
        f"Retrieved evidence rank {rank}",
        f"Retrieval: {selection_origin}",
        f"Trajectory: {trajectory_id or 'unknown'}",
        f"Chunk: {chunk_index if isinstance(chunk_index, int) else 'unknown'}",
    ]
    inventory_count = metadata.get("ui_inventory_item_count")
    if (
        isinstance(inventory_count, int)
        and not isinstance(inventory_count, bool)
        and inventory_count > 0
    ):
        if metadata.get("ui_inventory_truncated"):
            lines.append(
                f"UI inventory: partial ({inventory_count} elements shown; "
                "absence of an element cannot be inferred)"
            )
        else:
            lines.append(
                f"UI inventory: complete ({inventory_count} elements; an element "
                "not listed was not present on this page)"
            )
    return "\n".join(lines)


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
    context_budget: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    budget_items = context_budget.get("items") if isinstance(context_budget, dict) else None
    exposed_chars_by_rank = (
        {
            item["rank"]: item["exposed_content_chars"]
            for item in budget_items
            if isinstance(item, dict)
            and isinstance(item.get("rank"), int)
            and isinstance(item.get("exposed_content_chars"), int)
        }
        if isinstance(budget_items, list)
        else {}
    )
    compaction_by_rank = (
        {
            item["rank"]: dict(item["compaction"])
            for item in budget_items
            if isinstance(item, dict)
            and isinstance(item.get("rank"), int)
            and isinstance(item.get("compaction"), dict)
            and item["compaction"].get("mode") == "query_slices"
        }
        if isinstance(budget_items, list)
        else {}
    )
    dropped_ranks = (
        {
            item["rank"]
            for item in budget_items
            if isinstance(item, dict)
            and item.get("dropped") is True
            and isinstance(item.get("rank"), int)
        }
        if isinstance(budget_items, list)
        else set()
    )
    trace: list[dict[str, object]] = []
    for rank, result in enumerate(results[:max_items], start=1):
        if rank in dropped_ranks:
            continue
        content = _stripped_str(result.get("content"))
        if not content:
            continue
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        metadata_state_indices = metadata.get("longmemeval_v2_state_indices")
        state_indices = (
            [int(value) for value in metadata_state_indices if isinstance(value, int)]
            if isinstance(metadata_state_indices, list)
            else [int(value) for value in re.findall(r"^State\s+(\d+)\b", content, re.MULTILINE)]
        )
        source_support_state_indices = metadata.get("source_support_state_indices")
        support_state_indices = (
            [
                int(value)
                for value in source_support_state_indices
                if isinstance(value, int) and not isinstance(value, bool)
            ]
            if isinstance(source_support_state_indices, list)
            else []
        )
        source_support_states = metadata.get("source_support_states")
        normalized_source_support_states = (
            [dict(value) for value in source_support_states if isinstance(value, dict)]
            if isinstance(source_support_states, list)
            else []
        )
        trace_item = {
            "rank": rank,
            "entity_id": _stripped_str(result.get("id")),
            "entity_type": _stripped_str(result.get("type")),
            "trajectory_id": _stripped_str(metadata.get("longmemeval_v2_trajectory_id")),
            "chunk_index": metadata.get("longmemeval_v2_chunk_index"),
            "chunk_count": metadata.get("longmemeval_v2_chunk_count"),
            "state_indices": state_indices,
            "source_support_entity_id": metadata.get("source_support_entity_id"),
            "source_support_operational_source_id": metadata.get(
                "source_support_operational_source_id"
            ),
            "source_support_state_indices": support_state_indices,
            "source_support_states": normalized_source_support_states,
            "score": result.get("score"),
            "selection_pool": result.get("_evidence_selection_pool"),
            "selection_pool_rank": result.get("_evidence_pool_rank"),
            "selection_score": result.get("_evidence_selection_score"),
            "selection_overlap": result.get("_evidence_selection_overlap"),
            "content_chars": len(content),
            "exposed_chars": exposed_chars_by_rank.get(
                rank,
                min(len(content), max_chars_per_item),
            ),
            "result_origin": _stripped_str(result.get("result_origin")),
            "selection_origin": _stripped_str(result.get("_selection_origin")) or "search",
            "search_rank": result.get("_search_rank"),
            "trajectory_refined_from_chunk": result.get("_trajectory_refined_from_chunk"),
            "state_part_of_search_rank": result.get("_state_part_of_search_rank"),
            "state_part_refined_from_chunk": result.get("_state_part_refined_from_chunk"),
            "neighbor_of_search_rank": result.get("_neighbor_of_search_rank"),
            "neighbor_distance": result.get("_neighbor_distance"),
        }
        if rank in compaction_by_rank:
            trace_item["content_compaction"] = compaction_by_rank[rank]
        trace.append(trace_item)
    return trace


def context_assembly_candidate_limit(
    *,
    max_items: int,
    neighbor_stitch_items: int,
    state_part_completion_items: int,
    has_chunk_catalog: bool,
) -> int:
    max_items = max(1, max_items)
    if not has_chunk_catalog:
        return max_items
    return max_items + max(0, neighbor_stitch_items) + max(0, state_part_completion_items)


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
    restored_search_result_count = 0
    transport_content_chars = 0
    restored_content_chars = 0
    for search_rank, result in enumerate(results, start=1):
        candidate = dict(result)
        candidate["_selection_origin"] = "search"
        candidate["_search_rank"] = search_rank
        candidate, restored = _restore_catalog_content(candidate, chunk_catalog)
        if restored:
            restored_search_result_count += 1
            transport_content_chars += int(candidate["_transport_content_chars"])
            restored_content_chars += int(candidate["_source_content_chars"])
        ranked.append(candidate)

    neighbor_budget = neighbor_stitch_items if chunk_catalog and neighbor_stitch_span else 0
    state_part_budget = state_part_completion_items if chunk_catalog else 0
    seed_limit = max_items - neighbor_budget - state_part_budget
    seeds = _select_diverse_results(
        ranked,
        limit=seed_limit,
        max_chunks_per_trajectory=max_chunks_per_trajectory,
    )
    trajectory_refined_seeds, trajectory_refinement_metadata = _refine_trajectory_chunks(
        query,
        seeds,
        chunk_catalog=chunk_catalog,
    )
    refined_seeds, state_part_refinement_metadata = _refine_state_parts(
        query,
        trajectory_refined_seeds,
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
        _stripped_str(result.get("_selection_origin")) == "state_part" for result in selected
    )
    retained_neighbors = sum(
        _stripped_str(result.get("_selection_origin")) == "neighbor" for result in selected
    )
    return selected, {
        "input_result_count": len(results),
        "restored_search_result_count": restored_search_result_count,
        "restored_transport_content_chars": transport_content_chars,
        "restored_source_content_chars": restored_content_chars,
        "selected_search_seed_count": len(seeds),
        "completed_state_part_count": retained_state_parts,
        "stitched_neighbor_count": retained_neighbors,
        "output_result_count": len(selected),
        "max_chunks_per_trajectory": max_chunks_per_trajectory,
        "neighbor_stitch_items": neighbor_stitch_items,
        "neighbor_stitch_span": neighbor_stitch_span,
        "state_part_completion": state_part_metadata,
        "trajectory_refinement": trajectory_refinement_metadata,
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


def _refine_trajectory_chunks(
    query: str,
    seeds: list[dict[str, object]],
    *,
    chunk_catalog: dict[str, dict[int, dict[str, object]]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    focus_phrases = _query_focus_phrases(query)
    ui_roles = _query_ui_roles(query)
    metadata: dict[str, object] = {
        "enabled": bool(query and chunk_catalog and focus_phrases and ui_roles),
        "query_focus_phrases": list(focus_phrases),
        "query_ui_roles": list(ui_roles),
        "inspected_trajectory_count": 0,
        "candidate_count": 0,
        "replacements": [],
    }
    if not metadata["enabled"]:
        return seeds, metadata

    refined: list[dict[str, object]] = []
    replacements: list[dict[str, object]] = []
    inspected_trajectories: set[str] = set()
    reserved_keys = {_result_chunk_key(seed) for seed in seeds}
    for seed in seeds:
        trajectory_id, seed_chunk_index = _result_chunk_key(seed)
        if trajectory_id in inspected_trajectories:
            refined.append(seed)
            continue
        inspected_trajectories.add(trajectory_id)
        trajectory_catalog = chunk_catalog.get(trajectory_id, {})
        metadata["candidate_count"] = int(metadata["candidate_count"]) + len(trajectory_catalog)
        if not trajectory_catalog:
            refined.append(seed)
            continue
        seed_signal = _query_structured_signal(query, _stripped_str(seed.get("content")))
        best_chunk_index, best = max(
            trajectory_catalog.items(),
            key=lambda item: (
                _query_structured_rank(
                    _query_structured_signal(
                        query,
                        _stripped_str(item[1].get("content")),
                    )
                ),
                -abs(item[0] - seed_chunk_index) if isinstance(seed_chunk_index, int) else 0,
                -item[0],
            ),
        )
        best_key = (trajectory_id, best_chunk_index)
        best_signal = _query_structured_signal(query, _stripped_str(best.get("content")))
        if (
            best_signal[0] <= 0
            or _query_structured_rank(best_signal) <= _query_structured_rank(seed_signal)
            or best_key in reserved_keys
        ):
            refined.append(seed)
            continue
        replacement = dict(best)
        replacement["score"] = seed.get("score")
        replacement["_selection_origin"] = "trajectory_refinement"
        replacement["_search_rank"] = seed.get("_search_rank")
        replacement["_trajectory_refined_from_chunk"] = seed_chunk_index
        refined.append(replacement)
        reserved_keys.add(best_key)
        replacements.append(
            {
                "search_rank": seed.get("_search_rank"),
                "trajectory_id": trajectory_id,
                "from_chunk_key": list(_result_chunk_key(seed)),
                "to_chunk_key": list(best_key),
                "from_signal": list(seed_signal),
                "to_signal": list(best_signal),
            }
        )
    metadata["inspected_trajectory_count"] = len(inspected_trajectories)
    metadata["replacements"] = replacements
    return refined, metadata


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
    metadata["admitted_chunk_keys"] = [list(_result_chunk_key(candidate)) for candidate in admitted]
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


def _restore_catalog_content(
    result: dict[str, object],
    chunk_catalog: dict[str, dict[int, dict[str, object]]],
) -> tuple[dict[str, object], bool]:
    trajectory_id, chunk_index = _result_chunk_key(result)
    if not isinstance(chunk_index, int):
        return result, False
    catalog_result = chunk_catalog.get(trajectory_id, {}).get(chunk_index)
    if catalog_result is None:
        return result, False
    source_content = catalog_result.get("content")
    transport_content = result.get("content")
    if not isinstance(source_content, str) or source_content == transport_content:
        return result, False
    restored = dict(result)
    restored["content"] = source_content
    restored["_source_content_restored"] = True
    restored["_transport_content_chars"] = (
        len(transport_content) if isinstance(transport_content, str) else 0
    )
    restored["_source_content_chars"] = len(source_content)
    return restored, True


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


def _catalog_entity_payloads(
    payloads: list[dict[str, object]],
) -> dict[str, dict[int, dict[str, object]]]:
    catalog: dict[str, dict[int, dict[str, object]]] = {}
    for payload in payloads:
        metadata = _flatten_operational_metadata(payload.get("metadata"))
        trajectory_id = _stripped_str(metadata.get("longmemeval_v2_trajectory_id"))
        chunk_index = metadata.get("longmemeval_v2_chunk_index")
        if not trajectory_id or not isinstance(chunk_index, int):
            continue
        catalog.setdefault(trajectory_id, {})[chunk_index] = dict(payload)
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


def load_api_credentials_file(path: Path) -> dict[str, str]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"API credentials file is empty: {path}")
    try:
        payload = json_module.loads(raw)
    except json_module.JSONDecodeError:
        return {"api_token": raw}
    if not isinstance(payload, dict):
        raise ValueError(f"API credentials JSON must be an object: {path}")
    access_token = _stripped_str(payload.get("access_token"))
    if not access_token:
        raise ValueError(f"API credentials JSON is missing access_token: {path}")
    credentials = {
        "api_token": access_token,
        "api_credentials_path": str(path),
    }
    refresh_token = _stripped_str(payload.get("refresh_token"))
    if refresh_token:
        credentials["refresh_token"] = refresh_token
    return credentials


@register_memory
class SibylLiveApiMemory(Memory):
    memory_type = "sibyl_live_api"

    @classmethod
    def attach_existing(
        cls,
        memory_params: dict[str, object],
        *,
        expected_trajectory_ids: Iterable[str],
        trajectories: Iterable[dict[str, object]],
    ) -> SibylLiveApiMemory:
        memory = cls.prepare_existing(
            memory_params,
            expected_trajectory_ids=expected_trajectory_ids,
            trajectories=trajectories,
        )
        memory.finalize_ingest()
        return memory

    @classmethod
    def prepare_existing(
        cls,
        memory_params: dict[str, object],
        *,
        expected_trajectory_ids: Iterable[str],
        trajectories: Iterable[dict[str, object]],
    ) -> SibylLiveApiMemory:
        project_id = _param_str(memory_params, "project_id", "")
        if not project_id:
            raise ValueError("prepare_existing requires project_id")
        effective_params = dict(memory_params)
        effective_params["reuse_existing_project"] = True
        memory = cls(effective_params)
        memory._attached_expected_trajectory_ids.update(
            str(trajectory_id).strip()
            for trajectory_id in expected_trajectory_ids
            if str(trajectory_id).strip()
        )
        for trajectory in trajectories:
            memory.insert(trajectory)
        return memory

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
        self.project_id = _param_str(memory_params, "project_id", "")
        self.reuse_existing_project = _param_bool(
            memory_params,
            "reuse_existing_project",
            False,
        )
        if self.reuse_existing_project and not self.project_id:
            raise ValueError("reuse_existing_project requires project_id")
        self.content_max_chars = _param_int(
            memory_params,
            "content_max_chars",
            DEFAULT_CONTENT_MAX_CHARS,
        )
        checkpoint_dir = _param_str(memory_params, "checkpoint_dir", "")
        self.checkpoint_dir = (
            Path(checkpoint_dir).expanduser().resolve() if checkpoint_dir else None
        )
        if self.reuse_existing_project and self.checkpoint_dir is not None:
            raise ValueError("reuse_existing_project cannot be combined with checkpoint_dir")
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
        if self.chunking_mode != DEFAULT_CHUNKING_MODE and not self.reuse_existing_project:
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
        self.evidence_composition_mode = _param_str(
            memory_params,
            "evidence_composition_mode",
            DEFAULT_EVIDENCE_COMPOSITION_MODE,
        )
        if self.evidence_composition_mode not in EVIDENCE_COMPOSITION_MODES:
            msg = (
                f"Unknown evidence_composition_mode {self.evidence_composition_mode!r}; "
                f"expected one of {sorted(EVIDENCE_COMPOSITION_MODES)}"
            )
            raise ValueError(msg)
        self.source_evidence_bundling = _param_bool(
            memory_params,
            "source_evidence_bundling",
            DEFAULT_SOURCE_EVIDENCE_BUNDLING,
        )
        self.retrieval_mode = _param_str(
            memory_params,
            "retrieval_mode",
            DEFAULT_RETRIEVAL_MODE,
        )
        if self.retrieval_mode not in RETRIEVAL_MODES:
            msg = (
                f"Unknown retrieval_mode {self.retrieval_mode!r}; "
                f"expected one of {sorted(RETRIEVAL_MODES)}"
            )
            raise ValueError(msg)
        self.retrieval_max_planned_queries = _param_int(
            memory_params,
            "retrieval_max_planned_queries",
            DEFAULT_RETRIEVAL_MAX_PLANNED_QUERIES,
            minimum=1,
        )
        if self.retrieval_max_planned_queries > MAX_REFINEMENT_QUERIES:
            raise ValueError(
                f"retrieval_max_planned_queries must be at most {MAX_REFINEMENT_QUERIES}"
            )
        self.typed_stream_retrieval = _param_bool(
            memory_params,
            "typed_stream_retrieval",
            DEFAULT_TYPED_STREAM_RETRIEVAL,
        )
        self.typed_stream_limit = _param_int(
            memory_params,
            "typed_stream_limit",
            DEFAULT_TYPED_STREAM_LIMIT,
            minimum=1,
        )
        self.max_context_total_chars = max(
            1,
            _param_int(
                memory_params,
                "max_context_total_chars",
                DEFAULT_CONTEXT_TOTAL_CHARS,
            ),
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
        self.inserted_trajectories = 0
        self.created_entities = 0
        self.last_experience_write_receipt: dict[str, object] = {}
        self.attached_project_receipt: dict[str, object] = {}
        self._attached_expected_trajectory_ids: set[str] = set()
        self._pending_embedding_job_ids: set[str] = set()
        self._pending_projection_job_ids: set[str] = set()
        self._pending_job_entity_ids: dict[str, list[str]] = {}
        self._pending_job_manifest_ids: dict[str, str] = {}
        self.ingest_embedding_usage: dict[str, object] = {}
        self._finalize_lock = threading.Lock()
        self._ingest_finalized = False
        self._query_local = threading.local()
        self._chunk_catalog: dict[str, dict[int, dict[str, object]]] = {}
        self._chunk_payload_catalog: dict[str, dict[int, dict[str, object]]] = {}
        self._operational_chunk_catalog: dict[str, dict[int, dict[str, object]]] = {}
        self._completed_trajectory_ids: set[str] = set()
        self._operational_trajectory_ids: set[str] = set()
        self._client = _new_http_client(
            self.api_url,
            timeout_seconds=self.api_timeout_seconds,
        )
        self._closed = False
        self._refresh_token = ""
        self._api_credentials_path: Path | None = None
        self._cli_auth: dict[str, str] = {}
        self._authenticate(memory_params)
        self.api_runtime = self._request_json("GET", "/health")
        self.ingest_api_runtime = dict(self.api_runtime)
        if not self.project_id:
            self.project_id = self._create_project()
        else:
            self._verify_project_visibility()
        self.memory_params["project_id"] = self.project_id
        if (
            self.checkpoint_dir is not None
            and (self.checkpoint_dir / CHECKPOINT_MANIFEST_FILENAME).is_file()
        ):
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
        if getattr(self, "reuse_existing_project", False):
            if not trajectory_id:
                raise ValueError("attached existing project requires trajectory ids")
            payloads = build_entity_payloads_for_trajectory(
                trajectory,
                project_id=self.project_id,
                run_id=self.run_id,
                content_max_chars=self.content_max_chars,
                chunking_mode=self.chunking_mode,
                include_screenshot_refs=self.include_screenshot_refs,
            )
            self._chunk_catalog.update(_catalog_results(payloads))
            operational_payloads = build_operational_session_payloads_for_trajectory(
                trajectory,
                project_id=self.project_id,
                run_id=self.run_id,
                content_max_chars=self.content_max_chars,
                include_screenshot_refs=self.include_screenshot_refs,
            )
            self._operational_chunk_catalog.update(_catalog_entity_payloads(operational_payloads))
            for payload in payloads:
                metadata = payload.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                payload_trajectory_id = _stripped_str(metadata.get("longmemeval_v2_trajectory_id"))
                chunk_index = metadata.get("longmemeval_v2_chunk_index")
                if payload_trajectory_id and isinstance(chunk_index, int):
                    self._chunk_payload_catalog.setdefault(payload_trajectory_id, {})[
                        chunk_index
                    ] = dict(payload)
            self._attached_expected_trajectory_ids.add(trajectory_id)
            self.inserted_trajectories = len(self._attached_expected_trajectory_ids)
            self.created_entities = sum(len(chunks) for chunks in self._chunk_catalog.values())
            self._ingest_finalized = False
            return
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
            if getattr(self, "reuse_existing_project", False):
                self.attached_project_receipt = self._verify_attached_project()
                self._ingest_finalized = True
                return
            self._drain_embedding_backfills()
            self._drain_memory_projections()
            self._ingest_finalized = True
            if getattr(self, "checkpoint_dir", None) is not None:
                self._write_checkpoint_manifest(finalized=True)

    def _verify_attached_project(self) -> dict[str, object]:
        inventory = self._attached_project_inventory()
        self._assert_attached_project_identity(inventory)
        missing_chunk_keys = inventory["missing_chunk_keys"]
        unexpected_chunk_keys = inventory["unexpected_chunk_keys"]
        duplicate_chunk_keys = inventory["duplicate_chunk_keys"]
        catalog_mismatches = inventory["catalog_mismatches"]
        source_metadata_mismatches = inventory["source_metadata_mismatches"]
        if (
            missing_chunk_keys
            or unexpected_chunk_keys
            or duplicate_chunk_keys
            or catalog_mismatches
            or source_metadata_mismatches
        ):
            receipt = inventory["receipt"]
            raise RuntimeError(
                "attached project catalog does not match stored session inventory: "
                f"stored={receipt['expected_session_entity_count']}, "
                f"catalog={receipt['catalog_entity_count']}, "
                f"missing={len(missing_chunk_keys)}, "
                f"unexpected={len(unexpected_chunk_keys)}, "
                f"duplicates={len(duplicate_chunk_keys)}, "
                f"mismatches={list(catalog_mismatches)[:5]}, "
                f"source_metadata_mismatches={len(source_metadata_mismatches)}"
            )
        receipt = dict(inventory["receipt"])
        receipt["content_audit"] = self._verify_attached_project_contents(inventory)
        return receipt

    def audit_attached_project(self) -> dict[str, object]:
        inventory = self._attached_project_inventory()
        self._assert_attached_project_identity(inventory)
        return {
            **dict(inventory["receipt"]),
            "missing_chunks": [
                f"{trajectory_id}:{chunk_index}"
                for trajectory_id, chunk_index in inventory["missing_chunk_keys"]
            ],
            "unexpected_chunks": [
                f"{trajectory_id}:{chunk_index}"
                for trajectory_id, chunk_index in inventory["unexpected_chunk_keys"]
            ],
            "duplicate_chunks": [
                f"{trajectory_id}:{chunk_index}"
                for trajectory_id, chunk_index in inventory["duplicate_chunk_keys"]
            ],
            "catalog_mismatches": list(inventory["catalog_mismatches"]),
            **_source_metadata_mismatch_receipt(inventory["source_metadata_mismatches"]),
        }

    def repair_attached_project(self, *, apply: bool) -> dict[str, object]:
        inventory = self._attached_project_inventory()
        self._assert_attached_project_identity(inventory)
        unexpected = inventory["unexpected_chunk_keys"]
        duplicates = inventory["duplicate_chunk_keys"]
        mismatches = inventory["catalog_mismatches"]
        missing = list(inventory["missing_chunk_keys"])
        source_metadata_mismatches = list(inventory["source_metadata_mismatches"])
        storage_shapes = set(inventory["receipt"]["storage_shapes"])
        non_repairable_reasons: list[str] = []
        if unexpected:
            non_repairable_reasons.append("unexpected_chunks")
        if duplicates:
            non_repairable_reasons.append("duplicate_chunks")
        if mismatches:
            non_repairable_reasons.append("catalog_mismatches")
        if len(storage_shapes) > 1:
            non_repairable_reasons.append("mixed_storage_shapes")
        if missing and storage_shapes != {"legacy"}:
            non_repairable_reasons.append("missing_chunks_require_legacy_storage")
        before = {
            **dict(inventory["receipt"]),
            "missing_chunks": [
                f"{trajectory_id}:{chunk_index}" for trajectory_id, chunk_index in missing
            ],
            **_source_metadata_mismatch_receipt(source_metadata_mismatches),
        }
        content_audit_blockers = unexpected or duplicates or mismatches or missing
        if content_audit_blockers:
            before["content_audit"] = {"status": "blocked_by_inventory_damage"}
        else:
            try:
                before["content_audit"] = self._verify_attached_project_contents(inventory)
            except RuntimeError as exc:
                before["content_audit"] = {"status": "mismatch", "error": str(exc)}
                non_repairable_reasons.append("content_mismatch")
        repairable = not non_repairable_reasons
        if not apply:
            return {
                "applied": False,
                "repairable": repairable,
                "non_repairable_reasons": non_repairable_reasons,
                "created_entity_count": 0,
                "updated_entity_count": 0,
                "before": before,
                "after": (
                    dict(inventory["receipt"])
                    if repairable and not missing and not source_metadata_mismatches
                    else None
                ),
            }
        if non_repairable_reasons:
            raise RuntimeError(
                "attached project damage is not safely repairable: "
                + ", ".join(non_repairable_reasons)
            )
        if not missing and not source_metadata_mismatches:
            return {
                "applied": False,
                "repairable": True,
                "non_repairable_reasons": [],
                "created_entity_count": 0,
                "updated_entity_count": 0,
                "before": before,
                "after": dict(inventory["receipt"]),
            }

        payloads: list[dict[str, object]] = []
        for trajectory_id, chunk_index in missing:
            payload = self._chunk_payload_catalog.get(trajectory_id, {}).get(chunk_index)
            if not isinstance(payload, dict):
                raise RuntimeError(
                    "attached project repair is missing reconstructed payload "
                    f"{trajectory_id}:{chunk_index}"
                )
            payloads.append(dict(payload))

        created: list[tuple[str, dict[str, object]]] = []
        for batch in _payload_batches(
            payloads,
            max_entities=self.bulk_max_entities,
            max_content_chars=self.bulk_max_content_chars,
        ):
            response = self._request_json(
                "POST",
                "/entities/bulk",
                json={"entities": batch, "defer_embeddings": self.defer_embeddings},
            )
            created_ids = _response_entity_ids(response)
            if _created_count(response) != len(batch) or len(created_ids) != len(batch):
                raise RuntimeError(
                    "attached project repair received a partial bulk-create receipt: "
                    f"requested={len(batch)}, created={_created_count(response)}, "
                    f"entity_ids={len(created_ids)}"
                )
            created.extend(zip(created_ids, batch, strict=True))
            self.created_entities += len(created_ids)
            self._remember_embedding_backfill_jobs(response)
            self._remember_memory_projection_jobs(response)
            if len(self._pending_embedding_job_ids) >= self.embedding_backfill_max_pending_jobs:
                self._drain_embedding_backfills()

        self._drain_embedding_backfills()
        self._drain_memory_projections()
        updated_entity_count = 0
        metadata_receipt = hashlib.sha256()

        def synchronize_source_metadata(item: tuple[str, str]) -> tuple[str, str]:
            entity_id, source_id = item
            updated = self._request_json(
                "PATCH",
                f"/entities/{entity_id}",
                json={"metadata": {"source_id": source_id}},
            )
            updated_metadata = updated.get("metadata")
            if (
                not isinstance(updated_metadata, dict)
                or updated_metadata.get("source_id") != source_id
            ):
                raise RuntimeError(f"repaired entity {entity_id} did not preserve source metadata")
            return entity_id, source_id

        metadata_repair_worker_count = min(16, len(source_metadata_mismatches))
        with ThreadPoolExecutor(max_workers=max(metadata_repair_worker_count, 1)) as executor:
            synchronized = executor.map(synchronize_source_metadata, source_metadata_mismatches)
            for entity_id, source_id in synchronized:
                metadata_receipt.update(entity_id.encode())
                metadata_receipt.update(b"\0")
                metadata_receipt.update(source_id.encode())
                metadata_receipt.update(b"\0")
                updated_entity_count += 1

        content_receipt = hashlib.sha256()
        for entity_id, payload in created:
            stored = self._request_json("GET", f"/entities/{entity_id}")
            self._verify_repaired_entity(stored, payload, entity_id=entity_id)
            content_receipt.update(entity_id.encode())
            content_receipt.update(b"\0")
            content_receipt.update(str(stored.get("content") or "").encode())
            content_receipt.update(b"\0")

        after_inventory = self._attached_project_inventory()
        self._assert_attached_project_identity(after_inventory)
        self.attached_project_receipt = self._complete_attached_project_receipt(after_inventory)
        self.attached_project_receipt["content_audit"] = self._verify_attached_project_contents(
            after_inventory
        )
        self._ingest_finalized = True
        return {
            "applied": True,
            "repairable": True,
            "non_repairable_reasons": [],
            "created_entity_count": len(created),
            "verified_entity_count": len(created),
            "verified_content_sha256": content_receipt.hexdigest(),
            "updated_entity_count": updated_entity_count,
            "metadata_repair_worker_count": metadata_repair_worker_count,
            "verified_source_metadata_sha256": metadata_receipt.hexdigest(),
            "before": before,
            "after": dict(self.attached_project_receipt),
        }

    def _attached_project_inventory(self) -> dict[str, object]:
        expected = set(self._attached_expected_trajectory_ids)
        if not expected:
            raise RuntimeError("attached existing project has no expected trajectories")
        catalog_trajectories = set(self._chunk_catalog)
        missing_catalog = sorted(expected - catalog_trajectories)
        if missing_catalog:
            raise RuntimeError(
                "attached project catalog is missing "
                f"{len(missing_catalog)} expected trajectories: {missing_catalog[:5]}"
            )
        observed: set[str] = set()
        run_ids: set[str] = set()
        observed_chunk_counts: Counter[tuple[str, int]] = Counter()
        entity_count = 0
        expected_entity_count = 0
        catalog_mismatches: list[str] = []
        source_metadata_mismatches: list[tuple[str, str]] = []
        entity_chunk_keys: list[tuple[str, str, int, str]] = []
        storage_shapes: set[str] = set()
        page = 1
        while True:
            response = self._request_json(
                "GET",
                "/entities",
                params={
                    "entity_type": "session",
                    "project_ids": self.project_id,
                    "page": page,
                    "page_size": 200,
                },
            )
            entities = response.get("entities")
            if not isinstance(entities, list):
                raise RuntimeError("attached project inventory returned invalid entities")
            for entity in entities:
                raw_metadata = entity.get("metadata") if isinstance(entity, dict) else None
                metadata = _flatten_operational_metadata(raw_metadata)
                trajectory_id = _stripped_str(metadata.get("longmemeval_v2_trajectory_id"))
                run_id = _stripped_str(metadata.get("longmemeval_v2_run_id"))
                if not trajectory_id or not run_id:
                    raise RuntimeError("attached project contains unbound session entities")
                observed.add(trajectory_id)
                run_ids.add(run_id)
                entity_count += 1
                if trajectory_id in expected:
                    expected_entity_count += 1
                    chunk_index = metadata.get("longmemeval_v2_chunk_index")
                    projection_kind = _stripped_str(metadata.get("projection_kind"))
                    storage_shape = (
                        "operational"
                        if projection_kind == "raw_observation"
                        else "legacy"
                        if not projection_kind
                        else "unknown"
                    )
                    storage_shapes.add(storage_shape)
                    if isinstance(chunk_index, int):
                        observed_chunk_counts[(trajectory_id, chunk_index)] += 1
                        entity_id = _stripped_str(entity.get("id"))
                        if entity_id:
                            entity_chunk_keys.append(
                                (entity_id, trajectory_id, chunk_index, storage_shape)
                            )
                    expected_catalog = (
                        self._operational_chunk_catalog
                        if storage_shape == "operational"
                        else self._chunk_catalog
                    )
                    catalog_entry = (
                        expected_catalog.get(trajectory_id, {}).get(chunk_index)
                        if isinstance(chunk_index, int)
                        else None
                    )
                    catalog_metadata = _flatten_operational_metadata(
                        catalog_entry.get("metadata") if isinstance(catalog_entry, dict) else None
                    )
                    required_metadata_keys = (
                        (
                            "project_id",
                            "longmemeval_v2_run_id",
                            "longmemeval_v2_trajectory_id",
                            "longmemeval_v2_chunk_index",
                            "longmemeval_v2_state_index",
                            "longmemeval_v2_state_part_index",
                            "longmemeval_v2_state_part_count",
                            "operational_source_id",
                            "projection_kind",
                            "observation_ordinal",
                            "evidence_part_index",
                            "evidence_part_count",
                        )
                        if storage_shape == "operational"
                        else (
                            "project_id",
                            "longmemeval_v2_run_id",
                            "longmemeval_v2_trajectory_id",
                            "longmemeval_v2_chunk_index",
                            "longmemeval_v2_chunk_count",
                            "entity_content_projection_policy",
                        )
                    )
                    if (
                        storage_shape == "unknown"
                        or not isinstance(catalog_entry, dict)
                        or entity.get("name") != catalog_entry.get("name")
                        or any(
                            metadata.get(key) != catalog_metadata.get(key)
                            for key in required_metadata_keys
                        )
                    ):
                        catalog_mismatches.append(f"{trajectory_id}:{chunk_index}")
                    elif storage_shape == "legacy" and metadata.get(
                        "source_id"
                    ) != catalog_metadata.get("source_id"):
                        entity_id = _stripped_str(entity.get("id"))
                        expected_source_id = _stripped_str(catalog_metadata.get("source_id"))
                        if not entity_id or not expected_source_id:
                            catalog_mismatches.append(f"{trajectory_id}:{chunk_index}")
                        else:
                            source_metadata_mismatches.append((entity_id, expected_source_id))
            if not response.get("has_more"):
                break
            if not entities or page >= 1_000:
                raise RuntimeError("attached project inventory pagination did not converge")
            page += 1
        catalog_keys = {
            (trajectory_id, chunk_index)
            for trajectory_id in expected
            for chunk_index in self._chunk_catalog[trajectory_id]
        }
        observed_keys = set(observed_chunk_counts)
        missing_chunk_keys = tuple(sorted(catalog_keys - observed_keys))
        unexpected_chunk_keys = tuple(sorted(observed_keys - catalog_keys))
        duplicate_chunk_keys = tuple(
            sorted(key for key, count in observed_chunk_counts.items() if count > 1)
        )
        receipt = {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "session_entity_count": entity_count,
            "expected_session_entity_count": expected_entity_count,
            "expected_trajectory_count": len(expected),
            "observed_trajectory_count": len(observed),
            "extra_trajectory_count": len(observed - expected),
            "catalog_trajectory_count": len(catalog_trajectories),
            "catalog_entity_count": len(catalog_keys),
            "missing_chunk_count": len(missing_chunk_keys),
            "unexpected_chunk_count": len(unexpected_chunk_keys),
            "duplicate_chunk_count": len(duplicate_chunk_keys),
            "catalog_mismatch_count": len(catalog_mismatches),
            "source_metadata_mismatch_count": len(source_metadata_mismatches),
            "storage_shapes": sorted(storage_shapes),
            "pages": page,
        }
        return {
            "receipt": receipt,
            "expected": expected,
            "observed": observed,
            "run_ids": run_ids,
            "missing_chunk_keys": missing_chunk_keys,
            "unexpected_chunk_keys": unexpected_chunk_keys,
            "duplicate_chunk_keys": duplicate_chunk_keys,
            "catalog_mismatches": tuple(catalog_mismatches),
            "source_metadata_mismatches": tuple(source_metadata_mismatches),
            "entity_chunk_keys": tuple(entity_chunk_keys),
        }

    def _verify_attached_project_contents(
        self,
        inventory: dict[str, object],
    ) -> dict[str, object]:
        entity_chunk_keys = sorted(inventory["entity_chunk_keys"])
        expected_count = int(inventory["receipt"]["expected_session_entity_count"])
        if len(entity_chunk_keys) != expected_count:
            raise RuntimeError(
                "attached project content audit cannot bind every session entity: "
                f"expected={expected_count}, bound={len(entity_chunk_keys)}"
            )

        def read_and_verify(item: tuple[str, str, int, str]) -> tuple[str, str]:
            entity_id, trajectory_id, chunk_index, storage_shape = item
            stored = self._request_json("GET", f"/entities/{entity_id}")
            expected_catalog = (
                self._operational_chunk_catalog
                if storage_shape == "operational"
                else self._chunk_catalog
            )
            expected = expected_catalog[trajectory_id][chunk_index]
            stored_content = stored.get("content")
            expected_content = expected.get("content")
            if stored_content != expected_content:
                raise RuntimeError(
                    "attached project content does not match reconstructed source: "
                    f"{trajectory_id}:{chunk_index}"
                )
            return entity_id, str(stored_content or "")

        worker_count = min(16, len(entity_chunk_keys))
        digest = hashlib.sha256()
        with ThreadPoolExecutor(max_workers=max(worker_count, 1)) as executor:
            verified = executor.map(read_and_verify, entity_chunk_keys)
            verified_count = 0
            for entity_id, content in verified:
                digest.update(entity_id.encode())
                digest.update(b"\0")
                digest.update(content.encode())
                digest.update(b"\0")
                verified_count += 1
        return {
            "status": "verified",
            "entity_count": verified_count,
            "sha256": digest.hexdigest(),
            "worker_count": worker_count,
        }

    def _assert_attached_project_identity(self, inventory: dict[str, object]) -> None:
        expected = set(inventory["expected"])
        observed = set(inventory["observed"])
        missing = sorted(expected - observed)
        if missing:
            raise RuntimeError(
                f"attached project is missing {len(missing)} expected trajectories: {missing[:5]}"
            )
        extra = sorted(observed - expected)
        if extra:
            raise RuntimeError(
                f"attached project contains {len(extra)} uncatalogued trajectories: {extra[:5]}"
            )
        run_ids = set(inventory["run_ids"])
        if run_ids != {self.run_id}:
            raise RuntimeError(
                f"attached project run identity mismatch: expected {self.run_id!r}, "
                f"found {sorted(run_ids)}"
            )

    def _complete_attached_project_receipt(
        self,
        inventory: dict[str, object],
    ) -> dict[str, object]:
        missing = inventory["missing_chunk_keys"]
        unexpected = inventory["unexpected_chunk_keys"]
        duplicates = inventory["duplicate_chunk_keys"]
        mismatches = inventory["catalog_mismatches"]
        source_metadata_mismatches = inventory["source_metadata_mismatches"]
        if missing or unexpected or duplicates or mismatches or source_metadata_mismatches:
            receipt = inventory["receipt"]
            raise RuntimeError(
                "attached project catalog does not match stored session inventory: "
                f"stored={receipt['expected_session_entity_count']}, "
                f"catalog={receipt['catalog_entity_count']}, "
                f"missing={len(missing)}, unexpected={len(unexpected)}, "
                f"duplicates={len(duplicates)}, mismatches={list(mismatches)[:5]}, "
                f"source_metadata_mismatches={len(source_metadata_mismatches)}"
            )
        return dict(inventory["receipt"])

    @staticmethod
    def _verify_repaired_entity(
        stored: dict[str, object],
        expected: dict[str, object],
        *,
        entity_id: str,
    ) -> None:
        if stored.get("id") != entity_id:
            raise RuntimeError(f"repaired entity {entity_id} read back with a different id")
        for field in ("name", "description", "content", "entity_type"):
            if stored.get(field) != expected.get(field):
                raise RuntimeError(
                    f"repaired entity {entity_id} does not match source payload field {field}"
                )
        stored_metadata = stored.get("metadata")
        expected_metadata = expected.get("metadata")
        if not isinstance(stored_metadata, dict) or not isinstance(expected_metadata, dict):
            raise RuntimeError(f"repaired entity {entity_id} returned invalid metadata")
        for key, value in expected_metadata.items():
            if stored_metadata.get(key) != value:
                raise RuntimeError(
                    f"repaired entity {entity_id} does not match source metadata field {key}"
                )

    def _typed_stream_results(
        self,
        query: str,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        response = self._request_json(
            "POST",
            "/context/pack",
            json={
                "goal": query,
                "intent": "learn",
                "layer": "deep_search",
                "project": self.project_id,
                "limit": self.typed_stream_limit,
                "include_related": True,
                "related_limit": 3,
                "audit": True,
                "record_exposure": False,
                "evidence": {
                    "types": list(TYPED_STREAM_TYPES),
                    "limit": self.typed_stream_limit,
                    "content_max_chars": self.max_context_chars_per_item,
                    "include_retrieval_diagnostics": False,
                    "retrieval_mode": "fast",
                    "max_planned_queries": 1,
                },
            },
        )
        results, filters = _required_context_evidence(response)
        stream_results: list[dict[str, object]] = []
        for item in results:
            if _stripped_str(item.get("type")) not in TYPED_STREAM_TYPES:
                continue
            candidate = _string_key_dict(item)
            candidate["_selection_origin"] = "context_pack:typed_stream"
            stream_results.append(candidate)
        return stream_results, {
            "requested_types": list(TYPED_STREAM_TYPES),
            "limit": self.typed_stream_limit,
            "result_count": len(stream_results),
            "filters": filters,
        }

    def query(self, query: str, query_image: str | None = None) -> list[MemoryContextItem]:
        pending_jobs = len(self._pending_embedding_job_ids) + len(self._pending_projection_job_ids)
        if pending_jobs or not self._ingest_finalized:
            msg = f"memory ingestion has {pending_jobs} pending jobs; call finalize_ingest first"
            raise RuntimeError(msg)
        stream_future = None
        stream_executor: ThreadPoolExecutor | None = None
        if getattr(self, "typed_stream_retrieval", DEFAULT_TYPED_STREAM_RETRIEVAL):
            stream_executor = ThreadPoolExecutor(max_workers=1)
            stream_future = stream_executor.submit(self._typed_stream_results, query)
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
                "evidence": {
                    "types": ["session"],
                    "limit": min(max(self.search_limit, self.max_context_items), 50),
                    "max_results_per_source": getattr(
                        self,
                        "max_chunks_per_trajectory",
                        DEFAULT_MAX_CHUNKS_PER_TRAJECTORY,
                    ),
                    "content_max_chars": self.max_context_chars_per_item,
                    "include_retrieval_diagnostics": True,
                    "retrieval_mode": getattr(
                        self,
                        "retrieval_mode",
                        DEFAULT_RETRIEVAL_MODE,
                    ),
                    "max_planned_queries": getattr(
                        self,
                        "retrieval_max_planned_queries",
                        DEFAULT_RETRIEVAL_MAX_PLANNED_QUERIES,
                    ),
                },
            },
        )
        typed_results = context_pack_to_search_results(
            context_response,
            query=query,
            include_source_support=getattr(
                self,
                "source_evidence_bundling",
                DEFAULT_SOURCE_EVIDENCE_BUNDLING,
            ),
        )
        results, search_metadata = _required_context_evidence(context_response)
        if stream_future is not None and stream_executor is not None:
            try:
                stream_results, stream_metadata = stream_future.result()
            finally:
                stream_executor.shutdown(wait=False)
            typed_results = merge_typed_stream_results(typed_results, stream_results)
            search_metadata["typed_stream"] = stream_metadata
        if (
            getattr(self, "retrieval_mode", DEFAULT_RETRIEVAL_MODE) == "accurate"
            and search_metadata.get("planner_status") != "success"
        ):
            planner_status = search_metadata.get("planner_status", "missing")
            raise RuntimeError(
                "accurate retrieval requires a successful query planner; "
                f"received planner_status={planner_status!r}"
            )
        self._query_local.search_metadata = search_metadata
        results = _flatten_operational_result_metadata(results)
        chunk_catalog = getattr(self, "_chunk_catalog", {})
        neighbor_stitch_items = getattr(
            self,
            "neighbor_stitch_items",
            DEFAULT_NEIGHBOR_STITCH_ITEMS,
        )
        state_part_completion_items = getattr(
            self,
            "state_part_completion_items",
            DEFAULT_STATE_PART_COMPLETION_ITEMS,
        )
        assembled_results, assembly_metadata = assemble_context_results(
            results,
            chunk_catalog=chunk_catalog,
            max_items=context_assembly_candidate_limit(
                max_items=self.max_context_items,
                neighbor_stitch_items=neighbor_stitch_items,
                state_part_completion_items=state_part_completion_items,
                has_chunk_catalog=bool(chunk_catalog),
            ),
            max_chunks_per_trajectory=getattr(
                self,
                "max_chunks_per_trajectory",
                DEFAULT_MAX_CHUNKS_PER_TRAJECTORY,
            ),
            neighbor_stitch_items=neighbor_stitch_items,
            neighbor_stitch_span=getattr(
                self,
                "neighbor_stitch_span",
                DEFAULT_NEIGHBOR_STITCH_SPAN,
            ),
            query=query,
            state_part_completion_items=state_part_completion_items,
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
            context_token_counter=lambda selected: self._count_context_result_tokens(
                selected,
                query=query,
            ),
        )
        evidence_set, evidence_composition = compile_operational_evidence_set(
            query=query,
            typed_results=typed_results,
            raw_results=assembled_results,
            max_items=self.max_context_items,
            mode=getattr(
                self,
                "evidence_composition_mode",
                DEFAULT_EVIDENCE_COMPOSITION_MODE,
            ),
        )
        assembly_metadata["typed_context_candidate_count"] = len(typed_results)
        assembly_metadata["typed_context_selected_count"] = sum(
            _stripped_str(item.get("_selection_origin")).startswith("context_pack:")
            for item in evidence_set
        )
        assembly_metadata["evidence_composition"] = evidence_composition
        self._query_local.search_metadata["adapter_assembly"] = assembly_metadata
        memory_context, context_budget = render_memory_context(
            evidence_set,
            query=query,
            max_items=self.max_context_items,
            max_chars_per_item=self.max_context_chars_per_item,
            max_total_chars=getattr(
                self,
                "max_context_total_chars",
                DEFAULT_CONTEXT_TOTAL_CHARS,
            ),
        )
        assembly_metadata["context_budget"] = context_budget
        self._query_local.retrieval_trace = build_retrieval_trace(
            evidence_set,
            max_items=self.max_context_items,
            max_chars_per_item=self.max_context_chars_per_item,
            context_budget=context_budget,
        )
        return memory_context

    def _count_context_result_tokens(
        self,
        results: list[dict[str, object]],
        *,
        query: str = "",
    ) -> int:
        return count_memory_context_tokens(
            search_results_to_memory_context(
                results,
                query=query,
                max_items=self.max_context_items,
                max_chars_per_item=self.max_context_chars_per_item,
                max_total_chars=getattr(
                    self,
                    "max_context_total_chars",
                    DEFAULT_CONTEXT_TOTAL_CHARS,
                ),
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
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != MEMORY_MANIFEST_SCHEMA_VERSION
        ):
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
                    loaded.get("metadata") if isinstance(loaded.get("metadata"), dict) else {}
                )
                trajectory_id = _stripped_str(metadata.get("longmemeval_v2_trajectory_id"))
                chunk_index = metadata.get("longmemeval_v2_chunk_index")
                if trajectory_id and isinstance(chunk_index, int):
                    catalog.setdefault(trajectory_id, {})[chunk_index] = loaded
        self._chunk_catalog = catalog
        self.created_entities = sum(len(chunks) for chunks in catalog.values())
        self.inserted_trajectories = len(catalog)
        self._completed_trajectory_ids = set(catalog)
        operational_ids = manifest.get("operational_trajectory_ids")
        self._operational_trajectory_ids = (
            {str(value) for value in operational_ids if isinstance(value, str) and value in catalog}
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
                if job_id in self._pending_embedding_job_ids | self._pending_projection_job_ids
            },
            "pending_job_manifest_ids": {
                job_id: manifest_id
                for job_id, manifest_id in sorted(
                    getattr(self, "_pending_job_manifest_ids", {}).items()
                )
                if job_id in self._pending_embedding_job_ids | self._pending_projection_job_ids
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
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        ):
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
        pending_job_manifest_ids = manifest.get("pending_job_manifest_ids")
        self._pending_job_manifest_ids = (
            {
                str(job_id): str(manifest_id)
                for job_id, manifest_id in pending_job_manifest_ids.items()
                if isinstance(job_id, str) and isinstance(manifest_id, str) and manifest_id
            }
            if isinstance(pending_job_manifest_ids, dict)
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
            "reuse_existing_project": getattr(self, "reuse_existing_project", False),
            "attached_project_receipt": dict(getattr(self, "attached_project_receipt", {})),
            "defer_embeddings": self.defer_embeddings,
            "pending_embedding_backfill_jobs": len(self._pending_embedding_job_ids),
            "pending_memory_projection_jobs": len(self._pending_projection_job_ids),
            "ingest_embedding_usage": dict(self.ingest_embedding_usage),
            "returned_context_items": len(memory_context),
            "search_content_max_chars": self.max_context_chars_per_item,
            "retrieval_mode": getattr(
                self,
                "retrieval_mode",
                DEFAULT_RETRIEVAL_MODE,
            ),
            "retrieval_max_planned_queries": getattr(
                self,
                "retrieval_max_planned_queries",
                DEFAULT_RETRIEVAL_MAX_PLANNED_QUERIES,
            ),
            "search_metadata": dict(getattr(self._query_local, "search_metadata", {})),
            "retrieval_trace": list(getattr(self._query_local, "retrieval_trace", [])),
        }

    def _remember_embedding_backfill_jobs(self, response: dict[str, object]) -> None:
        if not self.defer_embeddings:
            return
        background_jobs = response.get("background_jobs")
        embedding_job = (
            background_jobs.get("embedding_backfill") if isinstance(background_jobs, dict) else None
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
        manifest_id = _stripped_str(response.get("manifest_id"))
        if manifest_id:
            pending_job_manifest_ids = getattr(self, "_pending_job_manifest_ids", None)
            if pending_job_manifest_ids is None:
                pending_job_manifest_ids = {}
                self._pending_job_manifest_ids = pending_job_manifest_ids
            for job_id in job_ids:
                pending_job_manifest_ids[job_id] = manifest_id

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
        if getattr(self, "_pending_job_manifest_ids", None) is None:
            self._pending_job_manifest_ids = {}

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
                    self._pending_job_manifest_ids.pop(job_id, None)
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
                    f"{job_id}={last_statuses.get(job_id, 'unknown')}" for job_id in sorted(pending)
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
        pending_job_manifest_ids = getattr(self, "_pending_job_manifest_ids", {})
        manifest_id = (
            pending_job_manifest_ids.get(job_id) if job_kind == "embedding_backfill" else None
        )
        if (
            not manifest_id
            and job_kind == "embedding_backfill"
            and len(entity_ids) > MAX_BULK_CREATE
        ):
            # Legacy checkpoints stored operational inventories in manifest-last order.
            manifest_id = entity_ids[-1]
        if not entity_ids and not manifest_id:
            msg = f"cannot recover lost {job_kind} job {job_id}: checkpoint has no entity ids"
            raise RuntimeError(msg)
        recovery_target = (
            {"manifest_id": manifest_id} if manifest_id else {"entity_ids": entity_ids}
        )
        response = self._request_json(
            "POST",
            "/entities/bulk/requeue-background-jobs",
            json={**recovery_target, "jobs": [job_kind]},
        )
        replacements = set(_background_job_ids(response, job_kind))
        background_jobs = response.get("background_jobs")
        job_info = background_jobs.get(job_kind) if isinstance(background_jobs, dict) else None
        recovery_status = (
            _stripped_str(job_info.get("status")) if isinstance(job_info, dict) else ""
        )
        if not replacements and recovery_status != "skipped":
            msg = f"requeue returned no replacement {job_kind} job for {job_id}"
            raise RuntimeError(msg)
        self._pending_embedding_job_ids.discard(job_id)
        self._pending_projection_job_ids.discard(job_id)
        self._pending_job_entity_ids.pop(job_id, None)
        pending_job_manifest_ids.pop(job_id, None)
        self._remember_embedding_backfill_jobs(response)
        self._remember_memory_projection_jobs(response)
        self._write_checkpoint_manifest(finalized=False)
        return replacements

    def _authenticate(self, memory_params: dict[str, object]) -> None:
        if _is_loopback_url(self.api_url) and not self.allow_localhost:
            msg = "Refusing to mutate localhost without allow_localhost=true"
            raise RuntimeError(msg)
        credentials_path = _param_str(
            memory_params,
            "api_credentials_path",
            os.environ.get("SIBYL_API_CREDENTIALS_FILE", ""),
        )
        self._api_credentials_path = (
            Path(credentials_path).expanduser().resolve() if credentials_path else None
        )
        file_credentials = (
            load_api_credentials_file(self._api_credentials_path)
            if self._api_credentials_path is not None
            else {}
        )
        token = (
            _param_str(memory_params, "api_token", "")
            or file_credentials.get("api_token", "")
            or os.environ.get("SIBYL_API_TOKEN", "")
        )
        if token:
            self._refresh_token = _param_str(
                memory_params, "refresh_token", ""
            ) or file_credentials.get("refresh_token", "")
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
        if self._api_credentials_path is not None:
            _store_api_credentials_file(
                self._api_credentials_path,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=body.get("expires_in"),
            )
        _store_cli_auth(self._cli_auth, access_token, refresh_token, body.get("expires_in"))
        return True


def _store_api_credentials_file(
    path: Path,
    *,
    access_token: str,
    refresh_token: str,
    expires_in: object,
) -> None:
    try:
        payload = json_module.loads(path.read_text(encoding="utf-8"))
    except (OSError, json_module.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    payload["access_token"] = access_token
    payload["refresh_token"] = refresh_token
    if isinstance(expires_in, int | float) and not isinstance(expires_in, bool):
        payload["expires_in"] = expires_in
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json_module.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _trajectory_text_chunks(
    trajectory: LongMemEvalV2Trajectory,
    *,
    max_chars: int,
    include_screenshot_refs: bool,
    chunking_mode: str = DEFAULT_CHUNKING_MODE,
) -> list[str]:
    chunk_builder = (
        _grouped_trajectory_chunks if chunking_mode == "trajectory" else _trajectory_chunks
    )
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
                line[index : index + max_chars] for index in range(0, len(line), max_chars)
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
            if isinstance(entity, dict) and (entity_id := _stripped_str(entity.get("id")))
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
        f"{label}: {total - len(pending)}/{total} complete; pending {pending_summary or 'none'}.",
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
    expires = (
        expires_in if isinstance(expires_in, int) and not isinstance(expires_in, bool) else None
    )
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


def _param_context_expansion_ratio(params: dict[str, object], key: str, default: float) -> float:
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
