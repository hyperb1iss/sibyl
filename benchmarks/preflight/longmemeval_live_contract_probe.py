#!/usr/bin/env python3
"""Probe the live LongMemEval harness contracts before harness implementation."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DATASET_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
DEFAULT_DATASET = ROOT / ".moon/cache/benchmarks/longmemeval_s_cleaned.json"
DEFAULT_OUTPUT = ROOT / "benchmarks/preflight/longmemeval_live_preflight_output.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _git_commit() -> str:
    git_dir = ROOT / ".git"
    if git_dir.is_file():
        prefix = "gitdir: "
        content = git_dir.read_text(encoding="utf-8").strip()
        if content.startswith(prefix):
            git_dir = (ROOT / content.removeprefix(prefix)).resolve()
    head = git_dir / "HEAD"
    if not head.exists():
        return "unknown"
    value = head.read_text(encoding="utf-8").strip()
    if not value.startswith("ref: "):
        return value or "unknown"
    ref = git_dir / value.removeprefix("ref: ").strip()
    if ref.exists():
        return ref.read_text(encoding="utf-8").strip() or "unknown"
    return "unknown"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _post_json(
    client: httpx.Client,
    path: str,
    *,
    payload: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.post(path, json=payload, params=params)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        msg = f"{path} returned non-object JSON"
        raise TypeError(msg)
    return data


def _run_live_roundtrip(api_url: str) -> dict[str, Any]:
    suffix = uuid4().hex[:12]
    email = f"longmemeval-preflight-{suffix}@example.invalid"
    password = f"SibylPreflight-{suffix}-pw"
    session_id = f"lme-preflight-session-{suffix}"
    question_id = f"lme-preflight-question-{suffix}"
    needle = f"cobalt quartz orchard {suffix}"

    with httpx.Client(base_url=api_url, timeout=30.0) as client:
        health = client.get("/health")
        health.raise_for_status()

        signup = _post_json(
            client,
            "/auth/local/signup",
            payload={"email": email, "password": password, "name": "LongMemEval Preflight"},
        )
        token = str(signup["access_token"])
        org = signup.get("organization") if isinstance(signup.get("organization"), dict) else {}
        client.headers.update({"Authorization": f"Bearer {token}"})

        entity_payload = {
            "name": f"LongMemEval preflight session {suffix}",
            "description": "Contract probe session for live LongMemEval.",
            "content": (
                "User: I need this remembered for the retrieval probe.\n"
                f"User: The exact answer phrase is {needle}."
            ),
            "entity_type": "session",
            "skip_conflicts": True,
            "metadata": {
                "longmemeval_session_id": session_id,
                "longmemeval_question_id": question_id,
                "valid_at": "2026-05-19T12:00:00+00:00",
                "corpus_text_policy": "user-turns-only-v1",
                "capture_surface": "longmemeval-preflight",
            },
        }
        created = _post_json(
            client,
            "/entities",
            params={"sync": "true"},
            payload=entity_payload,
        )
        search = _post_json(
            client,
            "/search",
            payload={
                "query": needle,
                "types": ["session"],
                "include_documents": False,
                "include_graph": True,
                "include_content": True,
                "use_enhanced": True,
                "boost_recent": False,
                "limit": 10,
            },
        )

    results = search.get("results") if isinstance(search.get("results"), list) else []
    matched = [
        result
        for result in results
        if isinstance(result, dict)
        and isinstance(result.get("metadata"), dict)
        and result["metadata"].get("longmemeval_session_id") == session_id
    ]
    first_match = matched[0] if matched else {}
    metadata = first_match.get("metadata") if isinstance(first_match.get("metadata"), dict) else {}
    embedding_metadata = (
        metadata.get("embedding_metadata")
        if isinstance(metadata.get("embedding_metadata"), dict)
        else None
    )

    return {
        "health": health.json(),
        "tenant": {
            "organization_id": org.get("id"),
            "organization_slug": org.get("slug"),
            "user_email": email,
        },
        "ingestion_endpoint": "POST /api/entities?sync=true",
        "retrieval_endpoint": "POST /api/search",
        "created_entity": {
            "id": created.get("id"),
            "entity_type": created.get("entity_type"),
            "metadata_roundtrip": (
                isinstance(created.get("metadata"), dict)
                and created["metadata"].get("longmemeval_session_id") == session_id
            ),
        },
        "search": {
            "query": needle,
            "result_count": len(results),
            "matched_expected_session": bool(matched),
            "rank": results.index(first_match) + 1 if matched else None,
            "metadata_roundtrip": metadata.get("longmemeval_session_id") == session_id,
            "embedding_metadata_present": embedding_metadata is not None,
            "embedding_dimensions": (
                embedding_metadata.get("dimensions") if embedding_metadata is not None else None
            ),
            "top_result_ids": [
                result.get("metadata", {}).get("longmemeval_session_id") or result.get("id")
                for result in results[:5]
                if isinstance(result, dict)
            ],
        },
    }


def _extract_indented_block(path: Path, marker: str) -> tuple[int, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if marker in line)
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent and line.lstrip().startswith(("async def ", "def ", "class ")):
            end = index
            break
    return start + 1, "\n".join(lines[start:end])


def _source_semantics() -> dict[str, Any]:
    native_graph = ROOT / "packages/python/sibyl-core/src/sibyl_core/services/native_graph.py"
    context_tool = ROOT / "packages/python/sibyl-core/src/sibyl_core/tools/context.py"
    native_start, native_search = _extract_indented_block(native_graph, "    async def search(")
    context_start, selected_search = _extract_indented_block(
        context_tool,
        "    async def selected_search_fn",
    )
    return {
        "api_search_graph_function": {
            "path": str(native_graph.relative_to(ROOT)),
            "line": native_start,
            "uses_fulltext_scores": "search::score" in native_search and "@0@" in native_search,
            "uses_knn_vector": "name_embedding <|" in native_search,
            "uses_embedding_provider": "embed_texts" in native_search,
        },
        "context_pack_native_function": {
            "path": str(context_tool.relative_to(ROOT)),
            "line": context_start,
            "passes_embedding_provider": "embedding_provider=" in selected_search,
        },
        "verified_result_signal_label": "node_fulltext",
    }


def _artifact_contract() -> dict[str, Any]:
    eval_gate_path = ROOT / "tools/bench/eval_gate.py"
    spec = importlib.util.spec_from_file_location("eval_gate", eval_gate_path)
    if spec is None or spec.loader is None:
        msg = f"Cannot load eval gate from {eval_gate_path}"
        raise RuntimeError(msg)
    eval_gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = eval_gate
    spec.loader.exec_module(eval_gate)

    sample_report = {
        "schema_version": "longmemeval-live-v1",
        "suite": "LongMemEval-S live preflight",
        "suite_version": "preflight-v1",
        "generated_at": _now(),
        "sibyl_commit": _git_commit(),
        "command": ["benchmarks/preflight/longmemeval_live_contract_probe.py"],
        "mode": "native",
        "runtime": {
            "runtime_mode": "live-api",
            "graph_engine": "surreal",
            "store": "surreal",
            "retrieval_mode": "native",
            "embedding_provider": "preflight",
            "embedding_model": "preflight",
            "embedding_dimensions": 1,
            "tokenizer_estimate_method": "preflight",
        },
        "dataset": {
            "name": "longmemeval_s_cleaned",
            "corpus_hash": f"sha256:{DATASET_SHA256}",
            "total_entries": 1,
            "evaluated_entries": 1,
            "limit": 1,
            "corpus_text_policy": "user-turns-only-v1",
        },
        "repeat_count": 1,
        "auth_manifest_id": "preflight",
        "overall": {
            "recall@5": 1.0,
            "recall@10": 1.0,
            "ndcg@5": 1.0,
            "ndcg@10": 1.0,
            "hit@5": 1.0,
            "hit@10": 1.0,
        },
        "per_type": {
            "preflight": {
                "recall@5": 1.0,
                "recall@10": 1.0,
                "ndcg@5": 1.0,
                "ndcg@10": 1.0,
                "hit@5": 1.0,
                "hit@10": 1.0,
            }
        },
        "case_results": [
            {
                "question_id": "preflight",
                "answer_session_ids": ["session-a"],
                "ranked_session_ids": ["session-a"],
                "retrieval_signals": ["node_fulltext"],
                "recall@5": 1.0,
                "ndcg@5": 1.0,
                "hit@5": 1.0,
            }
        ],
        "claim_boundary": "Preflight artifact contract only.",
    }
    valid_failures = eval_gate.evaluate_report(sample_report, profile="ai-memory")
    invalid_report = {
        **sample_report,
        "mode": "live",
        "runtime": {**sample_report["runtime"], "retrieval_mode": "live"},
    }
    invalid_failures = eval_gate.evaluate_report(invalid_report, profile="ai-memory")
    return {
        "valid_sample_failures": valid_failures,
        "mode_live_failures": invalid_failures,
        "required_fields": {
            "header": ["schema_version", "suite", "generated_at", "sibyl_commit", "command"],
            "runtime": [
                "runtime_mode",
                "graph_engine",
                "store",
                "retrieval_mode",
                "embedding_provider",
                "embedding_model",
                "embedding_dimensions",
                "tokenizer_estimate_method",
            ],
            "dataset": ["name", "corpus_hash"],
            "release": ["repeat_count", "auth_manifest_id", "mode"],
            "summary": ["overall", "per_type"],
            "cases": ["case identifier", "answer IDs", "ranked result IDs", "numeric metric"],
        },
        "allowed_ai_memory_modes": [
            "compare",
            "hybrid",
            "native",
            "post-graphiti",
            "pre-graphiti",
            "raw",
        ],
    }


def _abstention_contract(dataset: Path | None) -> dict[str, Any]:
    if dataset is None or not dataset.exists():
        return {
            "dataset_path": str(dataset) if dataset is not None else None,
            "dataset_available": False,
            "resolved": False,
        }

    entries = json.loads(dataset.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        msg = f"Expected LongMemEval dataset list at {dataset}"
        raise TypeError(msg)
    dataset_sha256 = _sha256_file(dataset)
    empty_answer_ids = [
        entry.get("question_id")
        for entry in entries
        if isinstance(entry, dict) and not entry.get("answer_session_ids")
    ]
    return {
        "dataset_path": str(dataset.relative_to(ROOT)),
        "dataset_available": True,
        "dataset_sha256": dataset_sha256,
        "expected_sha256": DATASET_SHA256,
        "sha256_matches_expected": dataset_sha256 == DATASET_SHA256,
        "total_entries": len(entries),
        "empty_answer_session_id_count": len(empty_answer_ids),
        "abstention_present": bool(empty_answer_ids),
        "resolved": True,
    }


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    live = None if args.skip_live else _run_live_roundtrip(args.api_url)
    semantics = _source_semantics()
    artifact = _artifact_contract()
    abstention = _abstention_contract(args.dataset)

    blockers: list[str] = []
    if live is None:
        blockers.append("live ingestion/retrieval roundtrip was skipped")
    else:
        if not live["created_entity"]["metadata_roundtrip"]:
            blockers.append("created session entity did not round-trip LongMemEval metadata")
        if not live["search"]["matched_expected_session"]:
            blockers.append("search did not return the expected LongMemEval session")
        if not live["search"]["embedding_metadata_present"]:
            blockers.append("search result did not carry embedding metadata or dimensions")
    if not semantics["api_search_graph_function"]["uses_knn_vector"]:
        blockers.append("/api/search session path is fulltext/lexical, not KNN vector search")
    if not semantics["context_pack_native_function"]["passes_embedding_provider"]:
        blockers.append("context-pack native retrieval is not passed an embedding provider")
    if artifact["valid_sample_failures"]:
        blockers.append("ai-memory sample artifact failed eval_gate")
    if not abstention.get("resolved"):
        blockers.append("LongMemEval abstention contract was not resolved")

    return {
        "schema_version": "sibyl-longmemeval-live-preflight-v1",
        "generated_at": _now(),
        "sibyl_commit": _git_commit(),
        "api_url": args.api_url,
        "status": "blocked" if blockers else "passed",
        "blocking_findings": blockers,
        "contracts": {
            "ingestion_retrieval": live,
            "retrieval_path_semantics": semantics,
            "artifact_contract": artifact,
            "abstention": abstention,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:3334/api")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    report = run_probe(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    sys.stdout.write(json.dumps(report, indent=2))
    sys.stdout.write("\n")
    if args.strict and report["blocking_findings"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
