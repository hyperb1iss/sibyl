"""Sibyl live-API memory backend for the official LongMemEval-V2 harness."""

from __future__ import annotations

import itertools
import os
import re
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any
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
JOB_STATUS_BATCH_SIZE = 64
MAX_BULK_CREATE = 128
RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 409, 425, 429})

_AUTH_CACHE: dict[tuple[str, str, str], dict[str, str]] = {}
_AUTH_LOCK = threading.Lock()
_INSTANCE_COUNTER = itertools.count(1)


def build_entity_payloads_for_trajectory(
    trajectory_raw: dict[str, object],
    *,
    project_id: str,
    run_id: str,
    content_max_chars: int = DEFAULT_CONTENT_MAX_CHARS,
    include_screenshot_refs: bool = False,
) -> list[dict[str, object]]:
    trajectory = LongMemEvalV2Trajectory.from_mapping(trajectory_raw)
    chunks = _trajectory_text_chunks(
        trajectory,
        max_chars=content_max_chars,
        include_screenshot_refs=include_screenshot_refs,
    )
    payloads: list[dict[str, object]] = []
    for chunk_index, content in enumerate(chunks):
        payloads.append(
            {
                "name": _entity_name(trajectory.id, chunk_index, len(chunks)),
                "description": f"{trajectory.goal} ({trajectory.outcome})",
                "content": content,
                "entity_type": "session",
                "skip_conflicts": True,
                "metadata": {
                    "project_id": project_id,
                    "longmemeval_v2_run_id": run_id,
                    "longmemeval_v2_trajectory_id": trajectory.id,
                    "longmemeval_v2_chunk_index": chunk_index,
                    "longmemeval_v2_chunk_count": len(chunks),
                    "longmemeval_v2_domain": trajectory.domain,
                    "longmemeval_v2_environment": trajectory.environment,
                    "longmemeval_v2_goal": trajectory.goal,
                    "longmemeval_v2_outcome": trajectory.outcome,
                    "capture_surface": "longmemeval-v2-official",
                    "entity_content_projection_policy": "v2-trajectory-state-chunks-v1",
                },
                "tags": ["longmemeval-v2", trajectory.domain, trajectory.environment],
            }
        )
    return payloads


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
        header = [
            f"Retrieved evidence rank {rank}",
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


@register_memory
class SibylLiveApiMemory(Memory):
    memory_type = "sibyl_live_api"

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
        self._pending_embedding_job_ids: set[str] = set()
        self._pending_projection_job_ids: set[str] = set()
        self._finalize_lock = threading.Lock()
        self._ingest_finalized = False
        self._query_local = threading.local()
        self._client = _new_http_client(
            self.api_url,
            timeout_seconds=self.api_timeout_seconds,
        )
        self._closed = False
        self._refresh_token = ""
        self._cli_auth: dict[str, str] = {}
        self._authenticate(memory_params)
        if not self.project_id:
            self.project_id = self._create_project()

    def set_query_context(self, **kwargs: object) -> None:
        question_item = kwargs.get("question_item")
        safe_context = {
            "question": question_item.get("question") if isinstance(question_item, dict) else None,
            "image": question_item.get("image") if isinstance(question_item, dict) else None,
        }
        super().set_query_context(**safe_context)

    def insert(self, trajectory: dict[str, object]) -> None:
        self._ingest_finalized = False
        payloads = build_entity_payloads_for_trajectory(
            trajectory,
            project_id=self.project_id,
            run_id=self.run_id,
            content_max_chars=self.content_max_chars,
            include_screenshot_refs=self.include_screenshot_refs,
        )
        for batch in _payload_batches(
            payloads,
            max_entities=self.bulk_max_entities,
            max_content_chars=self.bulk_max_content_chars,
        ):
            created = self._request_json(
                "POST",
                "/entities/bulk",
                json={"entities": batch, "defer_embeddings": self.defer_embeddings},
            )
            self.created_entities += _created_count(created)
            self._remember_embedding_backfill_jobs(created)
            self._remember_memory_projection_jobs(created)
            if len(self._pending_embedding_job_ids) >= self.embedding_backfill_max_pending_jobs:
                self._drain_embedding_backfills()
            if len(self._pending_projection_job_ids) >= self.embedding_backfill_max_pending_jobs:
                self._drain_memory_projections()
        self.inserted_trajectories += 1

    def finalize_ingest(self) -> None:
        with self._finalize_lock:
            if self._ingest_finalized:
                return
            self._drain_embedding_backfills()
            self._drain_memory_projections()
            self._ingest_finalized = True

    def query(self, query: str, query_image: str | None = None) -> list[MemoryContextItem]:
        pending_jobs = len(self._pending_embedding_job_ids) + len(
            self._pending_projection_job_ids
        )
        if pending_jobs or not self._ingest_finalized:
            msg = f"memory ingestion has {pending_jobs} pending jobs; call finalize_ingest first"
            raise RuntimeError(msg)
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
            "limit": min(max(self.search_limit, self.max_context_items), 50),
        }
        response = self._request_json("POST", "/search", json=payload)
        filters = response.get("filters")
        self._query_local.search_metadata = (
            dict(filters) if isinstance(filters, dict) else {}
        )
        raw_results = response.get("results")
        results = [item for item in raw_results if isinstance(item, dict)] if isinstance(raw_results, list) else []
        return search_results_to_memory_context(
            results,
            max_items=self.max_context_items,
            max_chars_per_item=self.max_context_chars_per_item,
        )

    def _save_backend(self, output_dir: Path) -> None:
        del output_dir
        self.finalize_ingest()

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
            "project_id": self.project_id,
            "run_id": self.run_id,
            "inserted_trajectories": self.inserted_trajectories,
            "created_entities": self.created_entities,
            "defer_embeddings": self.defer_embeddings,
            "pending_embedding_backfill_jobs": len(self._pending_embedding_job_ids),
            "pending_memory_projection_jobs": len(self._pending_projection_job_ids),
            "returned_context_items": len(memory_context),
            "search_content_max_chars": self.max_context_chars_per_item,
            "search_metadata": dict(
                getattr(self._query_local, "search_metadata", {})
            ),
        }

    def _remember_embedding_backfill_jobs(self, response: dict[str, object]) -> None:
        if not self.defer_embeddings:
            return
        job_ids = _background_job_ids(response, "embedding_backfill")
        if not job_ids and _created_count(response) > 0:
            msg = "/entities/bulk deferred embeddings but returned no backfill job ids"
            raise RuntimeError(msg)
        self._pending_embedding_job_ids.update(job_ids)

    def _remember_memory_projection_jobs(self, response: dict[str, object]) -> None:
        self._pending_projection_job_ids.update(
            _background_job_ids(response, "memory_projection")
        )

    def _drain_embedding_backfills(self) -> None:
        self._drain_background_jobs(
            self._pending_embedding_job_ids,
            job_name="embedding backfill",
            progress_label="Embedding backfills",
        )

    def _drain_memory_projections(self) -> None:
        self._drain_background_jobs(
            self._pending_projection_job_ids,
            job_name="memory projection",
            progress_label="Memory projections",
        )

    def _drain_background_jobs(
        self,
        job_ids: set[str],
        *,
        job_name: str,
        progress_label: str,
    ) -> None:
        if not job_ids:
            return

        pending = set(job_ids)
        total = len(pending)
        stall_deadline = time.monotonic() + self.embedding_job_wait_timeout_seconds
        last_statuses: dict[str, str] = {}
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
                if status_value == "complete":
                    if status.get("error"):
                        msg = f"{job_name} job {job_id} failed: {status['error']}"
                        raise RuntimeError(msg)
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
                    pending.remove(job_id)
                    made_progress = True
                elif status_value in {"cancelled", "not_found"}:
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
        return project_id

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
) -> list[str]:
    header = "\n".join(
        [
            f"Trajectory: {trajectory.id}",
            f"Domain: {trajectory.domain}",
            f"Environment: {trajectory.environment}",
            f"Outcome: {trajectory.outcome}",
            f"Goal: {trajectory.goal}",
            f"Start URL: {trajectory.start_url}",
        ]
    )
    chunks: list[str] = []
    current = header
    for state in trajectory.states:
        block = _state_text(state, include_screenshot_refs=include_screenshot_refs)
        candidate = f"{current}\n\n{block}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current != header:
            chunks.append(current)
            current = f"{header}\n\n{block}"
            if len(current) <= max_chars:
                continue
        chunks.extend(_split_oversized_block(header, block, max_chars=max_chars))
        current = header
    if current != header or not chunks:
        chunks.append(current)
    return chunks


def _state_text(state: LongMemEvalV2State, *, include_screenshot_refs: bool) -> str:
    parts = [
        f"State {state.state_index}",
        f"URL: {state.url}",
    ]
    if state.action:
        parts.append(f"Action: {state.action}")
    if state.thought:
        parts.append(f"Thought: {state.thought}")
    if include_screenshot_refs and state.screenshot:
        parts.append(f"Screenshot: {state.screenshot}")
    parts.append(f"Accessibility tree:\n{state.accessibility_tree}")
    return "\n".join(parts)


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
    value = response.get("created")
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


def _param_int(params: dict[str, object], key: str, default: int) -> int:
    value = params.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(1, value)
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return max(1, int(value))
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


def _stripped_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
