#!/usr/bin/env python3
# ruff: noqa: E402, PLR2004, S607, T201
"""Run LongMemEval-S against the existing live `/api/search` surface."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "python" / "sibyl-core" / "src"))

from sibyl_core.config import settings
from sibyl_core.evals.longmemeval import (
    CORPUS_TEXT_POLICIES,
    CORPUS_TEXT_POLICY,
    average_metric,
    build_longmemeval_corpus,
    score_longmemeval_ranking,
)

DATASET_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
DEFAULT_K_VALUES = [5, 10]
LIVE_RETRIEVAL_MODE = "hybrid"
AUTH_MANIFEST_ID = "ephemeral-local-signup-v1"
ENTITY_CONTENT_MAX_CHARS = 50_000
ENTITY_CONTENT_PROJECTION_POLICY = "api-entity-content-chunked-v1"
DEFAULT_SAMPLE_STRATEGY = "prefix"
SAMPLE_STRATEGIES = ("prefix", "stratified")
DIAGNOSTIC_CASE_LIMIT = 25
DIAGNOSTIC_SNIPPET_CHARS = 360
DEFAULT_DIAGNOSTIC_SEARCH_LIMIT = 50
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0
DEFAULT_STALL_TIMEOUT_SECONDS = 300.0
DEFAULT_MEMORY_EXTRACTION_TIMEOUT_SECONDS = 180.0


class LongMemEvalLiveError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _format_ms(value: Any) -> str:
    if not isinstance(value, int | float):
        return "n/a"
    return f"{value:.0f}ms"


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _corpus_hash(path: Path) -> str:
    return f"sha256:{_sha256_file(path)}"


def _load_dataset(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _select_entries(
    entries: list[dict[str, Any]],
    *,
    limit: int | None,
    sample_strategy: str,
) -> tuple[list[dict[str, Any]], list[int]]:
    if sample_strategy not in SAMPLE_STRATEGIES:
        msg = f"Unsupported LongMemEval sample strategy: {sample_strategy}"
        raise LongMemEvalLiveError(msg)
    if limit is None:
        return entries, list(range(len(entries)))
    if sample_strategy == "prefix":
        return entries[:limit], list(range(min(limit, len(entries))))

    by_type: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, entry in enumerate(entries):
        by_type[str(entry.get("question_type") or "unknown")].append((index, entry))

    selected: list[dict[str, Any]] = []
    selected_indices: list[int] = []
    offsets = dict.fromkeys(by_type, 0)
    question_types = sorted(by_type)
    while len(selected) < limit:
        added = False
        for question_type in question_types:
            offset = offsets[question_type]
            type_entries = by_type[question_type]
            if offset >= len(type_entries):
                continue
            original_index, entry = type_entries[offset]
            selected.append(entry)
            selected_indices.append(original_index)
            offsets[question_type] += 1
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break
    return selected, selected_indices


def _is_loopback_url(api_url: str) -> bool:
    host = urlparse(api_url).hostname
    if host is None:
        return False
    return host in {"localhost", "::1"} or host.startswith("127.")


def validate_target(api_url: str, *, allow_localhost: bool) -> None:
    if _is_loopback_url(api_url) and not allow_localhost:
        msg = (
            "Refusing to run LongMemEval live against localhost without "
            "--allow-localhost. This harness mutates its target by signing up "
            "throwaway users and ingesting sessions; only point it at a "
            "disposable stack."
        )
        raise LongMemEvalLiveError(msg)


def _parse_metadata(values: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            msg = f"Invalid metadata entry: {item!r}. Expected key=value."
            raise argparse.ArgumentTypeError(msg)
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            msg = f"Invalid metadata entry: {item!r}. Key cannot be empty."
            raise argparse.ArgumentTypeError(msg)
        metadata[key] = value
    return metadata


async def _post_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    payload: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.post(path, json=payload, params=params)
    if response.status_code >= 400:
        raise LongMemEvalLiveError(
            f"{path} failed with HTTP {response.status_code}: {response.text[:500]}"
        )
    data = response.json()
    if not isinstance(data, dict):
        raise LongMemEvalLiveError(f"{path} returned non-object JSON")
    return data


async def _get_json(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    response = await client.get(path)
    if response.status_code >= 400:
        raise LongMemEvalLiveError(
            f"{path} failed with HTTP {response.status_code}: {response.text[:500]}"
        )
    data = response.json()
    if not isinstance(data, dict):
        raise LongMemEvalLiveError(f"{path} returned non-object JSON")
    return data


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _graph_embedding_runtime_metadata() -> dict[str, Any]:
    provider = (
        os.environ.get("SIBYL_GRAPH_EMBEDDING_PROVIDER") or settings.graph_embedding_provider
    ).strip()
    model = os.environ.get("SIBYL_GRAPH_EMBEDDING_MODEL", "").strip()
    if not model:
        if provider == "gemini" and settings.graph_embedding_model == "text-embedding-3-small":
            model = "gemini-embedding-2"
        else:
            model = settings.graph_embedding_model

    raw_dimensions = os.environ.get("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", "").strip()
    dimensions = int(raw_dimensions) if raw_dimensions else settings.graph_embedding_dimensions
    if provider == "gemini":
        api_key_present = bool(
            os.environ.get("SIBYL_GEMINI_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or settings.gemini_api_key.get_secret_value()
        )
    else:
        api_key_present = bool(
            os.environ.get("SIBYL_OPENAI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or settings.openai_api_key.get_secret_value()
        )

    return {
        "retrieval_semantics": (
            "API graph hybrid retrieval with native vector/fulltext seed search "
            "plus graph traversal"
        ),
        "embedding_provider": provider if api_key_present else "disabled",
        "embedding_model": model if api_key_present else "not-applicable",
        "embedding_dimensions": dimensions if api_key_present else 0,
        "embedding_provider_configured": provider,
        "embedding_provider_status": "enabled" if api_key_present else "missing_key",
        "tokenizer_estimate_method": "provider-default" if api_key_present else "not-applicable",
        "vector_search_surface": "entity.name_embedding KNN via NativeEntityManager.search",
    }


def _background_job_info(response: dict[str, Any], key: str) -> dict[str, Any]:
    background_jobs = response.get("background_jobs")
    if not isinstance(background_jobs, dict):
        return {}
    job_info = background_jobs.get(key)
    if not isinstance(job_info, dict):
        return {}
    return dict(job_info)


def _new_memory_extraction_stats() -> dict[str, Any]:
    return {
        "batches": 0,
        "job_count": 0,
        "job_result_count": 0,
        "queued_sources": 0,
        "skipped_sources": 0,
        "queue_depth_max": None,
        "estimated_input_tokens": 0,
        "sources": 0,
        "extracted_entities": 0,
        "projected_entities": 0,
        "relationships": 0,
        "errors": 0,
        "projection_errors": 0,
        "statuses": {},
        "reasons": {},
    }


def _new_memory_projection_stats() -> dict[str, Any]:
    return {
        "batches": 0,
        "job_count": 0,
        "job_result_count": 0,
        "queued_sources": 0,
        "skipped_sources": 0,
        "sources": 0,
        "extracted": 0,
        "projected_entities": 0,
        "relationships": 0,
        "skipped": 0,
        "errors": 0,
        "statuses": {},
    }


def _int_metric(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_int_metric(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _increment_counter(counter: dict[str, int], key: str) -> None:
    counter[key] = int(counter.get(key, 0)) + 1


def _record_memory_extraction_enqueue(
    stats: dict[str, Any],
    job_info: dict[str, Any],
) -> None:
    if not job_info:
        return
    stats["batches"] += 1
    stats["job_count"] += len(_job_ids_from_info(job_info))
    stats["queued_sources"] += _int_metric(job_info.get("queued_sources"))
    stats["skipped_sources"] += _int_metric(job_info.get("skipped_sources"))
    queue_depth = _optional_int_metric(job_info.get("queue_depth"))
    if queue_depth is not None:
        current = stats.get("queue_depth_max")
        stats["queue_depth_max"] = queue_depth if current is None else max(current, queue_depth)
    status = str(job_info.get("status") or "unknown")
    _increment_counter(stats["statuses"], status)
    reason = str(job_info.get("reason") or "").strip()
    if reason:
        _increment_counter(stats["reasons"], reason)


def _record_memory_projection_enqueue(
    stats: dict[str, Any],
    job_info: dict[str, Any],
) -> None:
    if not job_info:
        return
    stats["batches"] += 1
    stats["job_count"] += len(_job_ids_from_info(job_info))
    stats["queued_sources"] += _int_metric(job_info.get("queued_sources"))
    stats["skipped_sources"] += _int_metric(job_info.get("skipped_sources"))
    status = str(job_info.get("status") or "unknown")
    _increment_counter(stats["statuses"], status)


def _record_memory_extraction_job_results(
    stats: dict[str, Any],
    completed_jobs: list[dict[str, Any]],
) -> None:
    for job in completed_jobs:
        result = job.get("result")
        if not isinstance(result, dict):
            continue
        stats["job_result_count"] += 1
        for key in (
            "estimated_input_tokens",
            "sources",
            "extracted_entities",
            "projected_entities",
            "relationships",
        ):
            stats[key] += _int_metric(result.get(key))
        errors = result.get("errors")
        if isinstance(errors, list):
            stats["errors"] += len(errors)
        projection_errors = result.get("projection_errors")
        if isinstance(projection_errors, list):
            stats["projection_errors"] += len(projection_errors)


def _record_memory_projection_job_results(
    stats: dict[str, Any],
    completed_jobs: list[dict[str, Any]],
) -> None:
    for job in completed_jobs:
        result = job.get("result")
        if not isinstance(result, dict):
            continue
        stats["job_result_count"] += 1
        for key in (
            "sources",
            "extracted",
            "projected_entities",
            "relationships",
            "skipped",
        ):
            stats[key] += _int_metric(result.get(key))
        errors = result.get("errors")
        if isinstance(errors, list):
            stats["errors"] += len(errors)


def _job_ids_from_info(job_info: dict[str, Any]) -> list[str]:
    job_ids = job_info.get("job_ids")
    if not isinstance(job_ids, list):
        return []
    return [str(job_id) for job_id in job_ids if str(job_id)]


async def _wait_for_jobs(
    client: httpx.AsyncClient,
    job_ids: list[str],
    *,
    job_kind: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    if not job_ids:
        return []

    deadline = time.monotonic() + timeout_seconds
    pending = set(job_ids)
    completed: list[dict[str, Any]] = []
    last_statuses: dict[str, str] = {}
    while pending:
        for job_id in list(pending):
            status = await _get_json(client, f"/jobs/{job_id}")
            status_value = str(status.get("status") or "unknown")
            last_statuses[job_id] = status_value
            if status_value == "complete":
                if status.get("error"):
                    raise LongMemEvalLiveError(
                        f"job {job_id} failed during {job_kind}: {status['error']}"
                    )
                pending.remove(job_id)
                completed.append(status)
            elif status_value == "not_found":
                raise LongMemEvalLiveError(f"job {job_id} disappeared during wait")

        if not pending:
            break
        if time.monotonic() >= deadline:
            raise LongMemEvalLiveError(
                f"timed out waiting for {job_kind} jobs: "
                + ", ".join(f"{job_id}={last_statuses.get(job_id)}" for job_id in sorted(pending))
            )
        await asyncio.sleep(0.5)

    return completed


async def _signup_throwaway_tenant(
    client: httpx.AsyncClient,
    *,
    run_id: str,
    case_index: int,
) -> dict[str, Any]:
    suffix = uuid4().hex[:12]
    signup = await _post_json(
        client,
        "/auth/local/signup",
        payload={
            "email": f"longmemeval-{run_id}-{case_index}-{suffix}@example.invalid",
            "password": f"SibylLongMemEval-{suffix}-password",
            "name": "LongMemEval Runner",
        },
    )
    token = str(signup.get("access_token") or "")
    if not token:
        raise LongMemEvalLiveError("signup did not return an access_token")
    client.headers.update({"Authorization": f"Bearer {token}"})
    org = signup.get("organization") if isinstance(signup.get("organization"), dict) else {}
    return {
        "organization_id": org.get("id"),
        "organization_slug": org.get("slug"),
    }


def _entity_name(
    *,
    run_id: str,
    case_index: int,
    document_index: int,
    chunk_index: int,
    chunk_count: int,
) -> str:
    name = f"LongMemEval {run_id} case {case_index} session {document_index}"
    if chunk_count > 1:
        name = f"{name} chunk {chunk_index + 1} of {chunk_count}"
    return name[:200]


def _chunk_entity_content(text: str) -> list[str]:
    if len(text) <= ENTITY_CONTENT_MAX_CHARS:
        return [text]
    return [
        text[index : index + ENTITY_CONTENT_MAX_CHARS]
        for index in range(0, len(text), ENTITY_CONTENT_MAX_CHARS)
    ]


def _set_active_phase(
    active_case: dict[str, Any] | None,
    phase: str,
    **metadata: object,
) -> None:
    if active_case is None:
        return
    for key in (
        "document_index",
        "document_count",
        "chunk_index",
        "chunk_count",
        "path",
    ):
        active_case.pop(key, None)
    active_case["phase"] = phase
    active_case["phase_started_at"] = time.monotonic()
    active_case.update(metadata)


def _format_active_case_summary(
    case_index: int,
    metadata: dict[str, Any],
    now: float,
) -> str:
    parts = [
        f"case={case_index}",
        f"type={metadata.get('question_type')}",
        f"worker={metadata.get('worker')}",
        f"elapsed={now - float(metadata['started_at']):.1f}s",
    ]
    phase = metadata.get("phase")
    if phase:
        phase_started = float(metadata.get("phase_started_at") or metadata["started_at"])
        parts.append(f"phase={phase}")
        parts.append(f"phase_elapsed={now - phase_started:.1f}s")
    document_index = metadata.get("document_index")
    document_count = metadata.get("document_count")
    if document_index is not None and document_count is not None:
        parts.append(f"doc={document_index}/{document_count}")
    chunk_index = metadata.get("chunk_index")
    chunk_count = metadata.get("chunk_count")
    if chunk_index is not None and chunk_count is not None:
        parts.append(f"chunk={chunk_index}/{chunk_count}")
    path = metadata.get("path")
    if path:
        parts.append(f"path={path}")
    return " ".join(parts)


async def _ingest_haystack(
    client: httpx.AsyncClient,
    *,
    entry: dict[str, Any],
    run_id: str,
    case_index: int,
    corpus_text_policy: str,
    wait_for_memory_extraction: bool,
    memory_extraction_timeout_seconds: float,
    memory_projection_timeout_seconds: float,
    active_case: dict[str, Any] | None = None,
) -> tuple[list[str], float, int, int, int, float, dict[str, Any], int, float, dict[str, Any]]:
    start = time.perf_counter()
    question_id = str(entry["question_id"])
    created_ids: list[str] = []
    memory_projection_job_ids: list[str] = []
    memory_projection_stats = _new_memory_projection_stats()
    memory_extraction_job_ids: list[str] = []
    memory_extraction_stats = _new_memory_extraction_stats()
    chunked_session_count = 0
    documents = build_longmemeval_corpus(entry, text_policy=corpus_text_policy)
    entities: list[dict[str, Any]] = []
    for document_index, document in enumerate(documents):
        content_chunks = _chunk_entity_content(document.text)
        if len(content_chunks) > 1:
            chunked_session_count += 1
        for chunk_index, content_chunk in enumerate(content_chunks):
            _set_active_phase(
                active_case,
                "ingest",
                document_index=document_index + 1,
                document_count=len(documents),
                chunk_index=chunk_index + 1,
                chunk_count=len(content_chunks),
                path="/entities/bulk",
            )
            entities.append(
                {
                    "name": _entity_name(
                        run_id=run_id,
                        case_index=case_index,
                        document_index=document_index,
                        chunk_index=chunk_index,
                        chunk_count=len(content_chunks),
                    ),
                    "description": "",
                    "content": content_chunk,
                    "entity_type": "session",
                    "skip_conflicts": True,
                    "metadata": {
                        "longmemeval_run_id": run_id,
                        "longmemeval_case_index": case_index,
                        "longmemeval_question_id": question_id,
                        "longmemeval_session_id": document.session_id,
                        "longmemeval_haystack_index": document_index,
                        "longmemeval_chunk_index": chunk_index,
                        "longmemeval_chunk_count": len(content_chunks),
                        "longmemeval_original_content_chars": len(document.text),
                        "entity_content_max_chars": ENTITY_CONTENT_MAX_CHARS,
                        "entity_content_projection_policy": ENTITY_CONTENT_PROJECTION_POLICY,
                        "valid_at": document.timestamp,
                        "corpus_text_policy": corpus_text_policy,
                        "capture_surface": "longmemeval-live",
                    },
                },
            )
    for batch_start in range(0, len(entities), 128):
        batch = entities[batch_start : batch_start + 128]
        created = await _post_json(
            client,
            "/entities/bulk",
            payload={"entities": batch},
        )
        created_entities = (
            created.get("entities") if isinstance(created.get("entities"), list) else []
        )
        created_ids.extend(
            str(entity.get("id") or "") for entity in created_entities if isinstance(entity, dict)
        )
        memory_extraction_info = _background_job_info(created, "memory_extraction")
        memory_extraction_job_ids.extend(_job_ids_from_info(memory_extraction_info))
        _record_memory_extraction_enqueue(memory_extraction_stats, memory_extraction_info)
        memory_projection_info = _background_job_info(created, "memory_projection")
        memory_projection_job_ids.extend(_job_ids_from_info(memory_projection_info))
        _record_memory_projection_enqueue(memory_projection_stats, memory_projection_info)

    ingest_ms = (time.perf_counter() - start) * 1000
    memory_projection_wait_ms = 0.0
    if memory_projection_job_ids:
        _set_active_phase(
            active_case,
            "memory_projection",
            path="/jobs",
        )
        wait_start = time.perf_counter()
        completed_jobs = await _wait_for_jobs(
            client,
            memory_projection_job_ids,
            job_kind="memory projection",
            timeout_seconds=memory_projection_timeout_seconds,
        )
        _record_memory_projection_job_results(memory_projection_stats, completed_jobs)
        memory_projection_wait_ms = (time.perf_counter() - wait_start) * 1000

    memory_extraction_wait_ms = 0.0
    if wait_for_memory_extraction and memory_extraction_job_ids:
        _set_active_phase(
            active_case,
            "memory_extraction",
            path="/jobs",
        )
        wait_start = time.perf_counter()
        completed_jobs = await _wait_for_jobs(
            client,
            memory_extraction_job_ids,
            job_kind="memory extraction",
            timeout_seconds=memory_extraction_timeout_seconds,
        )
        _record_memory_extraction_job_results(memory_extraction_stats, completed_jobs)
        memory_extraction_wait_ms = (time.perf_counter() - wait_start) * 1000

    return (
        created_ids,
        ingest_ms,
        chunked_session_count,
        len(documents),
        len(memory_extraction_job_ids),
        memory_extraction_wait_ms,
        memory_extraction_stats,
        len(memory_projection_job_ids),
        memory_projection_wait_ms,
        memory_projection_stats,
    )


async def _verify_namespace_probe(
    client: httpx.AsyncClient,
    *,
    run_id: str,
    question_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    while True:
        attempts += 1
        search = await _post_json(
            client,
            "/search",
            payload={
                "query": "LongMemEval",
                "types": ["session"],
                "include_documents": False,
                "include_graph": True,
                "include_content": False,
                "use_enhanced": True,
                "boost_recent": False,
                "limit": 5,
            },
        )
        results = search.get("results") if isinstance(search.get("results"), list) else []
        matched = [
            result
            for result in results
            if isinstance(result, dict)
            and isinstance(result.get("metadata"), dict)
            and result["metadata"].get("longmemeval_run_id") == run_id
            and result["metadata"].get("longmemeval_question_id") == question_id
        ]
        if matched:
            return {"attempts": attempts, "result_count": len(results)}
        if time.monotonic() >= deadline:
            raise LongMemEvalLiveError(
                f"timed out waiting for searchable sessions for question {question_id}"
            )
        await asyncio.sleep(0.5)


async def _search_question(
    client: httpx.AsyncClient,
    *,
    query: str,
    limit: int,
    reference_time: str | None,
    diagnostic_search_limit: int,
) -> dict[str, Any]:
    search_limit = max(limit, max(1, min(diagnostic_search_limit, 50)))
    return await _post_json(
        client,
        "/search",
        payload={
            "query": query,
            "types": ["session"],
            "include_documents": False,
            "include_graph": True,
            "include_content": False,
            "use_enhanced": True,
            "boost_recent": True,
            "reference_time": reference_time,
            "limit": search_limit,
        },
    )


def _ranked_sessions(results: list[Any]) -> tuple[list[str], list[dict[str, Any]], int]:
    ranked_session_ids: list[str] = []
    ranked_results: list[dict[str, Any]] = []
    cross_question_count = 0
    seen: set[str] = set()
    current_question_id: str | None = None
    for result in results:
        if not isinstance(result, dict):
            continue
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        session_id = metadata.get("longmemeval_session_id")
        question_id = metadata.get("longmemeval_question_id")
        if current_question_id is None and isinstance(question_id, str):
            current_question_id = question_id
        if (
            isinstance(question_id, str)
            and current_question_id is not None
            and question_id != current_question_id
        ):
            cross_question_count += 1
        if isinstance(session_id, str) and session_id not in seen:
            ranked_session_ids.append(session_id)
            seen.add(session_id)
        ranked_results.append(
            {
                "id": result.get("id"),
                "longmemeval_session_id": session_id,
                "score": result.get("score"),
                "result_origin": result.get("result_origin"),
                "type": result.get("type"),
            }
        )
    return ranked_session_ids, ranked_results, cross_question_count


def _answer_ranks(
    ranked_session_ids: list[str],
    answer_session_ids: list[str],
) -> list[dict[str, int | str | None]]:
    ranks: list[dict[str, int | str | None]] = []
    for session_id in answer_session_ids:
        rank = (
            ranked_session_ids.index(session_id) + 1 if session_id in ranked_session_ids else None
        )
        ranks.append({"session_id": session_id, "rank": rank})
    return ranks


def _snippet(text: str, max_chars: int = DIAGNOSTIC_SNIPPET_CHARS) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 1].rstrip()}…"


def _session_text_lookup(
    entry: dict[str, Any],
    *,
    corpus_text_policy: str,
) -> dict[str, str]:
    return {
        document.session_id: document.text
        for document in build_longmemeval_corpus(entry, text_policy=corpus_text_policy)
    }


def _build_diagnostics(
    *,
    results: list[dict[str, Any]],
    entries_by_case_index: dict[int, dict[str, Any]],
    corpus_text_policy: str,
    k_values: list[int],
) -> dict[str, Any]:
    max_k = max(k_values)
    min_k = min(k_values)
    hit_key = f"hit@{max_k}"
    recall_key = f"recall@{max_k}"
    ndcg_key = f"ndcg@{max_k}"
    cases_with_gaps = [
        result
        for result in results
        if float(result.get(hit_key, 0.0)) < 1.0 or float(result.get(recall_key, 0.0)) < 1.0
    ]
    worst_cases = sorted(
        cases_with_gaps,
        key=lambda result: (
            float(result.get(recall_key, 0.0)),
            float(result.get(ndcg_key, 0.0)),
            float(result.get(f"recall@{min_k}", 0.0)),
        ),
    )[:DIAGNOSTIC_CASE_LIMIT]

    type_counts: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cases": 0.0, "hit_misses": 0.0, "missing_answer_slots": 0.0, "answer_slots": 0.0}
    )
    for result in results:
        question_type = str(result.get("question_type") or "unknown")
        answer_ids = {str(session_id) for session_id in result.get("answer_session_ids", [])}
        ranked_ids = [str(session_id) for session_id in result.get("ranked_session_ids", [])]
        stats = type_counts[question_type]
        stats["cases"] += 1.0
        if float(result.get(hit_key, 0.0)) < 1.0:
            stats["hit_misses"] += 1.0
        stats["missing_answer_slots"] += len(answer_ids - set(ranked_ids[:max_k]))
        stats["answer_slots"] += len(answer_ids)

    diagnostic_cases: list[dict[str, Any]] = []
    for result in worst_cases:
        case_index = int(result["case_index"])
        entry = entries_by_case_index[case_index]
        answer_ids = [str(session_id) for session_id in result["answer_session_ids"]]
        ranked_ids = [str(session_id) for session_id in result["ranked_session_ids"]]
        top_distractors = [session_id for session_id in ranked_ids if session_id not in answer_ids]
        text_by_session_id = _session_text_lookup(entry, corpus_text_policy=corpus_text_policy)
        diagnostic_cases.append(
            {
                "case_index": case_index,
                "question_id": result.get("question_id"),
                "question_type": result.get("question_type"),
                "question": result.get("question"),
                "answer_ranks": result.get("answer_ranks"),
                "metrics": {
                    key: result[key]
                    for key in sorted(result)
                    if key.startswith(("hit@", "recall@", "ndcg@"))
                },
                "answer_snippets": {
                    session_id: _snippet(text_by_session_id.get(session_id, ""))
                    for session_id in answer_ids
                },
                "top_distractor_snippets": {
                    session_id: _snippet(text_by_session_id.get(session_id, ""))
                    for session_id in top_distractors[:3]
                },
            }
        )

    return {
        "max_k": max_k,
        "case_gap_count": len(cases_with_gaps),
        "question_type_counts": dict(sorted(type_counts.items())),
        "worst_cases": diagnostic_cases,
    }


async def _run_case(
    entry: dict[str, Any],
    *,
    api_url: str,
    run_id: str,
    case_index: int,
    k_values: list[int],
    readiness_timeout_seconds: float,
    timeout_seconds: float,
    corpus_text_policy: str,
    diagnostic_search_limit: int,
    wait_for_memory_extraction: bool,
    memory_extraction_timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
    active_case: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    timings_ms: dict[str, float] = {}
    async with httpx.AsyncClient(
        base_url=api_url,
        timeout=timeout_seconds,
        headers={"Content-Type": "application/json"},
        transport=transport,
    ) as client:
        _set_active_phase(active_case, "health", path="/health")
        phase_started = time.perf_counter()
        await _get_json(client, "/health")
        timings_ms["health"] = (time.perf_counter() - phase_started) * 1000
        _set_active_phase(active_case, "signup", path="/auth/local/signup")
        phase_started = time.perf_counter()
        tenant = await _signup_throwaway_tenant(client, run_id=run_id, case_index=case_index)
        timings_ms["signup"] = (time.perf_counter() - phase_started) * 1000
        _set_active_phase(active_case, "ingest")
        (
            created_ids,
            ingest_ms,
            chunked_session_count,
            document_count,
            memory_extraction_job_count,
            memory_extraction_wait_ms,
            memory_extraction_stats,
            memory_projection_job_count,
            memory_projection_wait_ms,
            memory_projection_stats,
        ) = await _ingest_haystack(
            client,
            entry=entry,
            run_id=run_id,
            case_index=case_index,
            corpus_text_policy=corpus_text_policy,
            wait_for_memory_extraction=wait_for_memory_extraction,
            memory_extraction_timeout_seconds=memory_extraction_timeout_seconds,
            memory_projection_timeout_seconds=readiness_timeout_seconds,
            active_case=active_case,
        )
        timings_ms["ingest"] = ingest_ms
        if memory_projection_wait_ms:
            timings_ms["memory_projection"] = memory_projection_wait_ms
        if memory_extraction_wait_ms:
            timings_ms["memory_extraction"] = memory_extraction_wait_ms
        _set_active_phase(active_case, "readiness", path="/search")
        phase_started = time.perf_counter()
        readiness = await _verify_namespace_probe(
            client,
            run_id=run_id,
            question_id=str(entry["question_id"]),
            timeout_seconds=readiness_timeout_seconds,
        )
        timings_ms["readiness"] = (time.perf_counter() - phase_started) * 1000
        _set_active_phase(active_case, "search", path="/search")
        phase_started = time.perf_counter()
        search = await _search_question(
            client,
            query=str(entry["question"]),
            limit=max(k_values),
            reference_time=(
                str(entry["question_date"]) if entry.get("question_date") is not None else None
            ),
            diagnostic_search_limit=diagnostic_search_limit,
        )
        timings_ms["search"] = (time.perf_counter() - phase_started) * 1000

    results = search.get("results") if isinstance(search.get("results"), list) else []
    ranked_session_ids, ranked_results, cross_question_count = _ranked_sessions(results)
    answer_session_ids = sorted(str(value) for value in entry.get("answer_session_ids", []))
    metrics = score_longmemeval_ranking(ranked_session_ids, answer_session_ids, k_values)
    answer_ranks = _answer_ranks(ranked_session_ids, answer_session_ids)
    return {
        "case_index": case_index,
        "question_id": entry["question_id"],
        "question_type": entry["question_type"],
        "question": entry.get("question"),
        "question_date": entry.get("question_date"),
        "answer_session_ids": answer_session_ids,
        "answer_ranks": answer_ranks,
        "missed_answer_session_ids": [
            rank["session_id"] for rank in answer_ranks if rank["rank"] is None
        ],
        "ranked_session_ids": ranked_session_ids,
        "ranked_results": ranked_results,
        "tenant": tenant,
        "document_count": document_count,
        "created_entity_count": len(created_ids),
        "chunked_session_count": chunked_session_count,
        "memory_extraction_job_count": memory_extraction_job_count,
        "memory_extraction_wait_ms": memory_extraction_wait_ms,
        "memory_extraction": memory_extraction_stats,
        "memory_projection_job_count": memory_projection_job_count,
        "memory_projection_wait_ms": memory_projection_wait_ms,
        "memory_projection": memory_projection_stats,
        "readiness": readiness,
        "ingest_ms": ingest_ms,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "timings_ms": timings_ms,
        "cross_question_result_count": cross_question_count,
        **metrics,
    }


async def _run_cases(
    entries: list[tuple[int, dict[str, Any]]],
    *,
    api_url: str,
    run_id: str,
    concurrency: int,
    k_values: list[int],
    readiness_timeout_seconds: float,
    timeout_seconds: float,
    corpus_text_policy: str,
    transport: httpx.AsyncBaseTransport | None,
    heartbeat_interval_seconds: float,
    stall_timeout_seconds: float,
    diagnostic_search_limit: int,
    wait_for_memory_extraction: bool,
    memory_extraction_timeout_seconds: float,
    on_progress: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    worker_count = max(1, concurrency)
    queue: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue()
    for entry in entries:
        queue.put_nowait(entry)

    results: list[dict[str, Any]] = []
    active_cases: dict[int, dict[str, Any]] = {}
    lock = asyncio.Lock()
    done_event = asyncio.Event()
    last_progress_at = time.monotonic()
    completed_count = 0
    total_count = len(entries)

    async def worker(worker_index: int) -> None:
        nonlocal completed_count, last_progress_at
        while True:
            try:
                case_index, entry = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            async with lock:
                active_cases[case_index] = {
                    "started_at": time.monotonic(),
                    "question_type": entry.get("question_type"),
                    "worker": worker_index,
                }
            try:
                result = await _run_case(
                    entry,
                    api_url=api_url,
                    run_id=run_id,
                    case_index=case_index,
                    k_values=k_values,
                    readiness_timeout_seconds=readiness_timeout_seconds,
                    timeout_seconds=timeout_seconds,
                    corpus_text_policy=corpus_text_policy,
                    diagnostic_search_limit=diagnostic_search_limit,
                    wait_for_memory_extraction=wait_for_memory_extraction,
                    memory_extraction_timeout_seconds=memory_extraction_timeout_seconds,
                    transport=transport,
                    active_case=active_cases[case_index],
                )
            finally:
                async with lock:
                    active_cases.pop(case_index, None)
                queue.task_done()

            async with lock:
                results.append(result)
                completed_count += 1
                last_progress_at = time.monotonic()
                if on_progress is not None:
                    await on_progress(sorted(results, key=lambda record: int(record["case_index"])))
                progress_k = 5 if 5 in k_values else min(k_values)
                progress_key = f"recall@{progress_k}"
                recall = average_metric(results, progress_key) * 100
                timings = result.get("timings_ms")
                timings = timings if isinstance(timings, dict) else {}
                extraction = result.get("memory_extraction")
                extraction = extraction if isinstance(extraction, dict) else {}
                projection = result.get("memory_projection")
                projection = projection if isinstance(projection, dict) else {}
                readiness = result.get("readiness")
                readiness = readiness if isinstance(readiness, dict) else {}
                print(
                    f"  [{completed_count:3d}/{total_count}] "
                    f"case={case_index} type={result.get('question_type')} "
                    f"R@{progress_k}: {recall:.1f}% "
                    f"latency={_format_ms(result.get('latency_ms'))} "
                    f"signup={_format_ms(timings.get('signup'))} "
                    f"ingest={_format_ms(timings.get('ingest'))} "
                    f"ready={_format_ms(timings.get('readiness'))}/"
                    f"{readiness.get('attempts', 'n/a')} "
                    f"search={_format_ms(timings.get('search'))} "
                    f"docs={result.get('document_count')} "
                    f"entities={result.get('created_entity_count')} "
                    f"project_jobs={result.get('memory_projection_job_count')} "
                    f"projected={projection.get('projected_entities', 0)} "
                    f"extract_jobs={result.get('memory_extraction_job_count')} "
                    f"extract_skipped={extraction.get('skipped_sources', 0)} "
                    f"extract_tokens={extraction.get('estimated_input_tokens', 0)}",
                    flush=True,
                )

    async def monitor(workers: list[asyncio.Task[None]]) -> None:
        interval = heartbeat_interval_seconds
        if interval <= 0:
            interval = min(stall_timeout_seconds, 30.0) if stall_timeout_seconds > 0 else 30.0
        while any(not worker.done() for worker in workers):
            try:
                await asyncio.wait_for(done_event.wait(), timeout=interval)
            except TimeoutError:
                pass
            else:
                return

            now = time.monotonic()
            async with lock:
                idle_seconds = now - last_progress_at
                active_summary = ", ".join(
                    _format_active_case_summary(case_index, metadata, now)
                    for case_index, metadata in sorted(active_cases.items())
                )
                completed_snapshot = completed_count
                queued_snapshot = queue.qsize()
            if heartbeat_interval_seconds > 0:
                print(
                    "  heartbeat "
                    f"completed={completed_snapshot}/{total_count} "
                    f"queued={queued_snapshot} "
                    f"active=[{active_summary or 'none'}] "
                    f"no_case_completed_for={idle_seconds:.1f}s",
                    flush=True,
                )
            if stall_timeout_seconds > 0 and idle_seconds >= stall_timeout_seconds:
                raise LongMemEvalLiveError(
                    "LongMemEval live stalled: no case completed for "
                    f"{idle_seconds:.1f}s; active=[{active_summary or 'none'}]"
                )

    workers = [asyncio.create_task(worker(index)) for index in range(worker_count)]

    async def run_workers() -> None:
        try:
            await asyncio.gather(*workers)
        finally:
            done_event.set()

    worker_group = asyncio.create_task(run_workers())
    monitor_task = asyncio.create_task(monitor(workers))
    try:
        done, pending = await asyncio.wait(
            {worker_group, monitor_task},
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
        await asyncio.gather(*pending)
    except (Exception, asyncio.CancelledError):
        for task in [worker_group, monitor_task, *workers]:
            task.cancel()
        await asyncio.gather(worker_group, monitor_task, *workers, return_exceptions=True)
        raise

    return sorted(results, key=lambda record: int(record["case_index"]))


def _aggregate(results: list[dict[str, Any]], k_values: list[int]) -> tuple[dict[str, float], dict]:
    metric_names = [
        f"{metric}@{k}" for k in k_values for metric in ("hit", "legacy_recall", "recall", "ndcg")
    ]
    overall = {metric: average_metric(results, metric) for metric in metric_names}
    overall["cross_question_result_count"] = sum(
        float(result["cross_question_result_count"]) for result in results
    )
    overall["created_entity_count"] = sum(
        float(result["created_entity_count"]) for result in results
    )
    overall["chunked_session_count"] = sum(
        float(result["chunked_session_count"]) for result in results
    )
    overall["memory_extraction_job_count"] = sum(
        float(result.get("memory_extraction_job_count", 0)) for result in results
    )
    overall["memory_extraction_wait_ms"] = sum(
        float(result.get("memory_extraction_wait_ms", 0.0)) for result in results
    )
    overall["memory_projection_job_count"] = sum(
        float(result.get("memory_projection_job_count", 0)) for result in results
    )
    overall["memory_projection_wait_ms"] = sum(
        float(result.get("memory_projection_wait_ms", 0.0)) for result in results
    )
    projection_keys = (
        "queued_sources",
        "skipped_sources",
        "job_result_count",
        "sources",
        "extracted",
        "projected_entities",
        "relationships",
        "skipped",
        "errors",
    )
    for key in projection_keys:
        overall[f"memory_projection_{key}"] = sum(
            float(result.get("memory_projection", {}).get(key, 0.0)) for result in results
        )
    extraction_keys = (
        "queued_sources",
        "skipped_sources",
        "job_result_count",
        "estimated_input_tokens",
        "sources",
        "extracted_entities",
        "projected_entities",
        "relationships",
        "errors",
        "projection_errors",
    )
    for key in extraction_keys:
        overall[f"memory_extraction_{key}"] = sum(
            float(result.get("memory_extraction", {}).get(key, 0.0)) for result in results
        )
    queue_depths = [
        result.get("memory_extraction", {}).get("queue_depth_max")
        for result in results
        if result.get("memory_extraction", {}).get("queue_depth_max") is not None
    ]
    overall["memory_extraction_queue_depth_max"] = float(max(queue_depths)) if queue_depths else 0.0

    results_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        results_by_type[str(result["question_type"])].append(result)
    per_type = {
        question_type: {metric: average_metric(type_results, metric) for metric in metric_names}
        for question_type, type_results in sorted(results_by_type.items())
    }
    return overall, per_type


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _build_live_report(
    *,
    dataset_path: Path,
    total_entries: int,
    selected_entries_count: int,
    selected_indices: list[int],
    entries_by_case_index: dict[int, dict[str, Any]],
    k_values: list[int],
    case_results: list[dict[str, Any]],
    command: list[str] | None,
    metadata: dict[str, str] | None,
    sample_strategy: str,
    corpus_text_policy: str,
    limit: int | None,
    diagnostic_search_limit: int,
    heartbeat_interval_seconds: float,
    stall_timeout_seconds: float,
    wait_for_memory_extraction: bool,
    memory_extraction_timeout_seconds: float,
    elapsed_seconds: float,
    completion_status: str,
) -> dict[str, Any]:
    sorted_results = sorted(case_results, key=lambda record: int(record["case_index"]))
    overall, per_type = _aggregate(sorted_results, k_values)
    diagnostics = _build_diagnostics(
        results=sorted_results,
        entries_by_case_index=entries_by_case_index,
        corpus_text_policy=corpus_text_policy,
        k_values=k_values,
    )
    embedding_runtime = _graph_embedding_runtime_metadata()

    return {
        "schema_version": "longmemeval-live-v1",
        "suite": "LongMemEval-S live API",
        "suite_version": "live-api-search-v1",
        "generated_at": _now(),
        "sibyl_commit": _git_commit(),
        "command": command,
        "mode": LIVE_RETRIEVAL_MODE,
        "runtime": {
            "runtime_mode": "live-api-ephemeral",
            "graph_engine": "surreal",
            "store": "surreal",
            "retrieval_mode": LIVE_RETRIEVAL_MODE,
            "retrieval_surface": "POST /api/search",
            "retrieval_contract": "sync entity writes plus /api/search graph results",
            **embedding_runtime,
            "readiness_strategy": "sync_write_plus_search_probe",
            "per_question_isolation": "throwaway organization namespace per question",
            "entity_content_max_chars": ENTITY_CONTENT_MAX_CHARS,
            "entity_content_projection_policy": ENTITY_CONTENT_PROJECTION_POLICY,
            "sample_strategy": sample_strategy,
            "corpus_text_policy": corpus_text_policy,
            "diagnostic_search_limit": diagnostic_search_limit,
            "heartbeat_interval_seconds": heartbeat_interval_seconds,
            "stall_timeout_seconds": stall_timeout_seconds,
            "auto_extract_entities_env": _env_flag("SIBYL_AUTO_EXTRACT_ENTITIES"),
            "wait_for_memory_extraction": wait_for_memory_extraction,
            "memory_enrichment_consistency": ("strong" if wait_for_memory_extraction else "async"),
            "memory_extraction_timeout_seconds": memory_extraction_timeout_seconds,
            "graph_hnsw_efc_env": os.environ.get("SIBYL_GRAPH_HNSW_EFC", ""),
            "graph_hnsw_m_env": os.environ.get("SIBYL_GRAPH_HNSW_M", ""),
            "graph_knn_ef_env": os.environ.get("SIBYL_GRAPH_KNN_EF", ""),
            "native_fusion_backend_env": os.environ.get("SIBYL_NATIVE_FUSION_BACKEND", ""),
        },
        "dataset": {
            "name": dataset_path.stem,
            "path": str(dataset_path),
            "corpus_hash": _corpus_hash(dataset_path),
            "total_entries": total_entries,
            "evaluated_entries": selected_entries_count,
            "completed_entries": len(sorted_results),
            "limit": limit,
            "sample_strategy": sample_strategy,
            "selected_case_indices": selected_indices,
            "corpus_text_policy": corpus_text_policy,
            "diagnostic_search_limit": diagnostic_search_limit,
            "entity_content_projection_policy": ENTITY_CONTENT_PROJECTION_POLICY,
            "wait_for_memory_extraction": wait_for_memory_extraction,
            "memory_enrichment_consistency": ("strong" if wait_for_memory_extraction else "async"),
        },
        "metadata": metadata or {},
        "repeat_count": 1,
        "auth_manifest_id": AUTH_MANIFEST_ID,
        "k_values": k_values,
        "total_questions": selected_entries_count,
        "completed_questions": len(sorted_results),
        "completion_status": completion_status,
        "overall": overall,
        "per_type": per_type,
        "diagnostics": diagnostics,
        "case_results": sorted_results,
        "elapsed_seconds": elapsed_seconds,
        "claim_boundary": (
            "Live API runtime evidence for /api/search against an ephemeral "
            "Sibyl stack with per-question throwaway org namespaces. This "
            "artifact measures the production graph hybrid search path, "
            "including native graph vector search when embedding config is "
            "enabled."
        ),
    }


async def run_benchmark(
    data_path: str | Path,
    *,
    api_url: str,
    allow_localhost: bool = False,
    limit: int | None = None,
    concurrency: int = 2,
    k_values: list[int] | None = None,
    command: list[str] | None = None,
    metadata: dict[str, str] | None = None,
    readiness_timeout_seconds: float = 30.0,
    timeout_seconds: float = 60.0,
    sample_strategy: str = DEFAULT_SAMPLE_STRATEGY,
    corpus_text_policy: str = CORPUS_TEXT_POLICY,
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    stall_timeout_seconds: float = DEFAULT_STALL_TIMEOUT_SECONDS,
    diagnostic_search_limit: int = DEFAULT_DIAGNOSTIC_SEARCH_LIMIT,
    wait_for_memory_extraction: bool = False,
    memory_extraction_timeout_seconds: float = DEFAULT_MEMORY_EXTRACTION_TIMEOUT_SECONDS,
    verify_sha256: bool = True,
    output_path: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    validate_target(api_url, allow_localhost=allow_localhost)
    k_values = k_values or list(DEFAULT_K_VALUES)
    dataset_path = Path(data_path)
    dataset_sha = _sha256_file(dataset_path)
    if verify_sha256 and dataset_sha != DATASET_SHA256:
        raise LongMemEvalLiveError(
            f"dataset SHA-256 mismatch: expected {DATASET_SHA256}, got {dataset_sha}"
        )

    entries = _load_dataset(dataset_path)
    if not isinstance(entries, list):
        raise LongMemEvalLiveError("dataset must be a JSON list")
    total_entries = len(entries)
    selected_entries, selected_indices = _select_entries(
        entries,
        limit=limit,
        sample_strategy=sample_strategy,
    )
    if not selected_entries:
        raise LongMemEvalLiveError("no LongMemEval entries selected")
    if corpus_text_policy not in CORPUS_TEXT_POLICIES:
        msg = f"Unsupported LongMemEval corpus text policy: {corpus_text_policy}"
        raise LongMemEvalLiveError(msg)
    selected_cases = list(zip(selected_indices, selected_entries, strict=True))
    entries_by_case_index = dict(selected_cases)

    run_id = uuid4().hex[:12]
    started = time.perf_counter()
    print("\n============================================================", flush=True)
    print("  Sibyl x LongMemEval Live API Benchmark", flush=True)
    print(f"  API: {api_url}", flush=True)
    print(f"  Questions: {len(selected_entries)}", flush=True)
    print(f"  Sample strategy: {sample_strategy}", flush=True)
    print(f"  Corpus text policy: {corpus_text_policy}", flush=True)
    print(f"  Concurrency: {concurrency}", flush=True)
    print(f"  K values: {k_values}", flush=True)
    print(f"  Diagnostic search limit: {diagnostic_search_limit}", flush=True)
    print(f"  Wait for memory extraction: {wait_for_memory_extraction}", flush=True)
    print(
        f"  Memory enrichment consistency: {'strong' if wait_for_memory_extraction else 'async'}",
        flush=True,
    )
    print(
        "  Graph HNSW: "
        f"EFC={os.environ.get('SIBYL_GRAPH_HNSW_EFC', '150')} "
        f"M={os.environ.get('SIBYL_GRAPH_HNSW_M', '12')} "
        f"KNN_EF={os.environ.get('SIBYL_GRAPH_KNN_EF', '40')}",
        flush=True,
    )
    print(
        f"  Native fusion backend: {os.environ.get('SIBYL_NATIVE_FUSION_BACKEND', 'python_rrf')}",
        flush=True,
    )
    print(
        f"  Heartbeat: {heartbeat_interval_seconds:.1f}s; "
        f"stall timeout: {stall_timeout_seconds:.1f}s",
        flush=True,
    )
    print("============================================================\n", flush=True)

    async def checkpoint(partial_results: list[dict[str, Any]]) -> None:
        if output_path is None:
            return
        report = _build_live_report(
            dataset_path=dataset_path,
            total_entries=total_entries,
            selected_entries_count=len(selected_entries),
            selected_indices=selected_indices,
            entries_by_case_index=entries_by_case_index,
            k_values=k_values,
            case_results=partial_results,
            command=command,
            metadata=metadata,
            sample_strategy=sample_strategy,
            corpus_text_policy=corpus_text_policy,
            limit=limit,
            diagnostic_search_limit=diagnostic_search_limit,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            stall_timeout_seconds=stall_timeout_seconds,
            wait_for_memory_extraction=wait_for_memory_extraction,
            memory_extraction_timeout_seconds=memory_extraction_timeout_seconds,
            elapsed_seconds=time.perf_counter() - started,
            completion_status="partial",
        )
        _write_report(output_path, report)

    await checkpoint([])

    case_results = await _run_cases(
        selected_cases,
        api_url=api_url,
        run_id=run_id,
        concurrency=concurrency,
        k_values=k_values,
        readiness_timeout_seconds=readiness_timeout_seconds,
        timeout_seconds=timeout_seconds,
        corpus_text_policy=corpus_text_policy,
        transport=transport,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        stall_timeout_seconds=stall_timeout_seconds,
        diagnostic_search_limit=diagnostic_search_limit,
        wait_for_memory_extraction=wait_for_memory_extraction,
        memory_extraction_timeout_seconds=memory_extraction_timeout_seconds,
        on_progress=checkpoint,
    )
    elapsed = time.perf_counter() - started
    report = _build_live_report(
        dataset_path=dataset_path,
        total_entries=total_entries,
        selected_entries_count=len(selected_entries),
        selected_indices=selected_indices,
        entries_by_case_index=entries_by_case_index,
        k_values=k_values,
        case_results=case_results,
        command=command,
        metadata=metadata,
        sample_strategy=sample_strategy,
        corpus_text_policy=corpus_text_policy,
        limit=limit,
        diagnostic_search_limit=diagnostic_search_limit,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        stall_timeout_seconds=stall_timeout_seconds,
        wait_for_memory_extraction=wait_for_memory_extraction,
        memory_extraction_timeout_seconds=memory_extraction_timeout_seconds,
        elapsed_seconds=elapsed,
        completion_status="complete",
    )
    if output_path is not None:
        _write_report(output_path, report)
    overall = report["overall"]

    print("\n============================================================", flush=True)
    print("  RESULTS - live /api/search", flush=True)
    print("============================================================", flush=True)
    for k in k_values:
        print(
            f"  Overall H@{k}: {overall[f'hit@{k}'] * 100:.1f}%  "
            f"R@{k}: {overall[f'recall@{k}'] * 100:.1f}%  "
            f"NDCG@{k}: {overall[f'ndcg@{k}']:.3f}",
            flush=True,
        )
    print(f"  Cross-question results: {overall['cross_question_result_count']:.0f}", flush=True)
    print(f"  Time: {elapsed:.1f}s", flush=True)
    print("============================================================\n", flush=True)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run LongMemEval-S against live /api/search.")
    parser.add_argument("data", type=Path, help="Path to longmemeval_s_cleaned.json")
    parser.add_argument("--api-url", default="http://localhost:3334/api")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--k", type=int, nargs="+", default=DEFAULT_K_VALUES)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--label", default=None)
    parser.add_argument("--metadata", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument(
        "--allow-localhost",
        action="store_true",
        help="Allow mutation of a localhost API. Use only for disposable stacks.",
    )
    parser.add_argument("--readiness-timeout", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        help="Seconds between progress heartbeats while cases are in flight.",
    )
    parser.add_argument(
        "--stall-timeout",
        type=float,
        default=DEFAULT_STALL_TIMEOUT_SECONDS,
        help="Fail when no LongMemEval case completes for this many seconds.",
    )
    parser.add_argument(
        "--diagnostic-search-limit",
        type=int,
        default=DEFAULT_DIAGNOSTIC_SEARCH_LIMIT,
        help="Search result count to request while still scoring configured k values.",
    )
    parser.add_argument(
        "--wait-for-memory-extraction",
        action="store_true",
        help="Wait for queued memory extraction jobs before scoring each case.",
    )
    parser.add_argument(
        "--memory-extraction-timeout",
        type=float,
        default=DEFAULT_MEMORY_EXTRACTION_TIMEOUT_SECONDS,
        help="Seconds to wait for each case's memory extraction jobs.",
    )
    parser.add_argument(
        "--sample-strategy",
        choices=SAMPLE_STRATEGIES,
        default=DEFAULT_SAMPLE_STRATEGY,
        help="How to select cases when --limit is set.",
    )
    parser.add_argument(
        "--corpus-text-policy",
        choices=CORPUS_TEXT_POLICIES,
        default=CORPUS_TEXT_POLICY,
        help="Which conversation roles to project into session entities.",
    )
    parser.add_argument("--skip-sha256-check", action="store_true")
    args = parser.parse_args()

    metadata = _parse_metadata(args.metadata)
    if args.label:
        metadata["label"] = args.label

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.output or ROOT / "benchmarks" / "results" / "ai-memory" / (
        f"longmemeval_live_api_{timestamp}.json"
    )
    try:
        asyncio.run(
            run_benchmark(
                args.data,
                api_url=args.api_url,
                allow_localhost=args.allow_localhost,
                limit=args.limit,
                concurrency=args.concurrency,
                k_values=args.k,
                command=sys.argv,
                metadata=metadata,
                readiness_timeout_seconds=args.readiness_timeout,
                timeout_seconds=args.timeout,
                sample_strategy=args.sample_strategy,
                corpus_text_policy=args.corpus_text_policy,
                heartbeat_interval_seconds=args.heartbeat_interval,
                stall_timeout_seconds=args.stall_timeout,
                diagnostic_search_limit=args.diagnostic_search_limit,
                wait_for_memory_extraction=args.wait_for_memory_extraction,
                memory_extraction_timeout_seconds=args.memory_extraction_timeout,
                verify_sha256=not args.skip_sha256_check,
                output_path=out_path,
            )
        )
    except LongMemEvalLiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    print(f"  Results saved to {out_path}", flush=True)


if __name__ == "__main__":
    main()
