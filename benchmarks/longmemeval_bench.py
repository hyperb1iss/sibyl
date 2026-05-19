#!/usr/bin/env python3
# ruff: noqa: B905, I001, PLC0415, PLR2004, S110, S607, SIM105, T201
"""
Sibyl x LongMemEval Offline Baseline
====================================

Evaluates an offline Sibyl-style retrieval baseline against LongMemEval using
the same dataset and metrics as MemPalace.

For each of the 500 questions:
1. Ingest all haystack sessions into a fresh in-memory search index
2. Query using Sibyl's hybrid retrieval pipeline (vector + temporal + RRF)
3. Score retrieval against ground-truth answer sessions

This script intentionally does NOT touch the live Sibyl graph or `/api/search`.
For live runtime evaluation against the production search stack, use
`benchmarks/live_runtime_eval.py`.

Usage:
    uv run --with chromadb python benchmarks/longmemeval_bench.py /tmp/longmemeval-data/longmemeval_s_cleaned.json
    uv run --with chromadb python benchmarks/longmemeval_bench.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --limit 20
    uv run --with chromadb python benchmarks/longmemeval_bench.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --mode hybrid
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import chromadb
except ModuleNotFoundError:
    chromadb = None

sys.path.insert(0, str(Path(__file__).parent.parent / "packages" / "python" / "sibyl-core" / "src"))
from sibyl_core.evals.longmemeval import (
    CORPUS_TEXT_POLICY,
    average_metric,
    build_longmemeval_corpus,
    score_longmemeval_ranking,
)


# =============================================================================
# RETRIEVAL MODES
# =============================================================================

_bench_client = chromadb.EphemeralClient() if chromadb is not None else None

_HYBRID_STOP_WORDS = {
    "what",
    "when",
    "where",
    "who",
    "how",
    "which",
    "did",
    "do",
    "was",
    "were",
    "have",
    "has",
    "had",
    "is",
    "are",
    "am",
    "the",
    "a",
    "an",
    "my",
    "me",
    "i",
    "you",
    "your",
    "their",
    "it",
    "its",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "and",
    "or",
    "but",
    "ago",
    "last",
    "that",
    "this",
    "there",
    "about",
    "get",
    "got",
    "give",
    "gave",
    "buy",
    "bought",
    "made",
    "make",
    "been",
}


def _extract_keywords(text: str) -> list[str]:
    return [
        word
        for word in re.findall(r"\b[a-z]{3,}\b", text.lower())
        if word not in _HYBRID_STOP_WORDS
    ]


def _require_bench_client() -> Any:
    if _bench_client is None:
        raise RuntimeError(
            "chromadb is required for benchmarks/longmemeval_bench.py. "
            "Use benchmarks/live_runtime_eval.py for the live runtime path."
        )
    return _bench_client


def _fresh_collection(name: str = "sibyl_bench") -> Any:
    client = _require_bench_client()
    try:
        client.delete_collection(name)
    except Exception:
        pass
    return client.create_collection(name)


def retrieve_raw(entry: dict, n_results: int = 50) -> tuple[list[int], list[str]]:
    """Baseline: raw ChromaDB search (same as MemPalace raw mode)."""
    corpus, corpus_ids = _build_corpus(entry)
    if not corpus:
        return [], corpus_ids

    collection = _fresh_collection()
    collection.add(
        documents=corpus,
        ids=[f"doc_{i}" for i in range(len(corpus))],
        metadatas=[{"corpus_id": cid} for cid in corpus_ids],
    )

    results = collection.query(
        query_texts=[entry["question"]],
        n_results=min(n_results, len(corpus)),
        include=["distances"],
    )

    doc_id_to_idx = {f"doc_{i}": i for i in range(len(corpus))}
    ranked = [doc_id_to_idx[rid] for rid in results["ids"][0]]
    seen = set(ranked)
    for i in range(len(corpus)):
        if i not in seen:
            ranked.append(i)

    return ranked, corpus_ids


def retrieve_hybrid(entry: dict, n_results: int = 50) -> tuple[list[int], list[str]]:
    """Sibyl-style hybrid: embedding + keyword overlap + temporal proximity."""

    documents = build_longmemeval_corpus(entry)
    corpus = [document.text for document in documents]
    corpus_ids = [document.session_id for document in documents]
    if not corpus:
        return [], corpus_ids

    collection = _fresh_collection()
    collection.add(
        documents=corpus,
        ids=[f"doc_{i}" for i in range(len(corpus))],
        metadatas=[
            {"corpus_id": document.session_id, "timestamp": document.timestamp}
            for document in documents
        ],
    )

    query = entry["question"]
    results = collection.query(
        query_texts=[query],
        n_results=min(n_results, len(corpus)),
        include=["distances", "metadatas", "documents"],
    )

    # Keyword overlap scoring
    query_kws = _extract_keywords(query)

    # Temporal parsing
    question_date = _parse_date(entry.get("question_date", ""))
    temporal_target = _parse_temporal_reference(query, question_date)

    doc_id_to_idx = {f"doc_{i}": i for i in range(len(corpus))}
    scored: list[tuple[int, float]] = []

    for rid, dist, meta, doc in zip(
        results["ids"][0],
        results["distances"][0],
        results["metadatas"][0],
        results["documents"][0],
    ):
        idx = doc_id_to_idx[rid]
        base_score = 1.0 / (1.0 + dist)

        # Keyword boost
        if query_kws:
            doc_lower = doc.lower()
            hits = sum(1 for kw in query_kws if kw in doc_lower)
            kw_boost = 0.3 * (hits / len(query_kws))
        else:
            kw_boost = 0.0

        # Temporal proximity boost
        temporal_boost = 0.0
        if temporal_target and meta.get("timestamp"):
            doc_date = _parse_date(meta["timestamp"])
            if doc_date and temporal_target:
                days_diff = abs((temporal_target - doc_date).days)
                if days_diff <= 3:
                    temporal_boost = 0.4
                elif days_diff <= 7:
                    temporal_boost = 0.25
                elif days_diff <= 14:
                    temporal_boost = 0.1

        fused = base_score * (1 + kw_boost) * (1 + temporal_boost)
        scored.append((idx, fused))

    scored.sort(key=lambda x: x[1], reverse=True)
    ranked = [idx for idx, _ in scored]

    seen = set(ranked)
    for i in range(len(corpus)):
        if i not in seen:
            ranked.append(i)

    return ranked, corpus_ids


# =============================================================================
# HELPERS
# =============================================================================


def _build_corpus(entry: dict) -> tuple[list[str], list[str]]:
    """Build corpus from haystack sessions (user turns only, one doc per session)."""
    documents = build_longmemeval_corpus(entry)
    return [document.text for document in documents], [
        document.session_id for document in documents
    ]


def _parse_date(date_str: str):
    """Parse LongMemEval date format."""
    from datetime import datetime

    if not date_str:
        return None
    for fmt in ["%Y/%m/%d (%a) %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"]:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_temporal_reference(query: str, question_date):
    """Extract temporal target from query like 'a week ago', '10 days ago'."""
    from datetime import timedelta

    if not question_date:
        return None

    patterns = [
        (r"\b(\d+)\s+days?\s+ago\b", lambda m: timedelta(days=int(m.group(1)))),
        (r"\ba\s+couple\s+(?:of\s+)?days?\s+ago\b", lambda m: timedelta(days=2)),
        (r"\byesterday\b", lambda m: timedelta(days=1)),
        (r"\b(\d+)\s+weeks?\s+ago\b", lambda m: timedelta(weeks=int(m.group(1)))),
        (r"\b(\d+)\s+months?\s+ago\b", lambda m: timedelta(days=int(m.group(1)) * 30)),
        (r"\ba\s+week\s+ago\b", lambda m: timedelta(weeks=1)),
        (r"\ba\s+month\s+ago\b", lambda m: timedelta(days=30)),
        (r"\blast\s+week\b", lambda m: timedelta(weeks=1)),
        (r"\blast\s+month\b", lambda m: timedelta(days=30)),
        (r"\blast\s+year\b", lambda m: timedelta(days=365)),
        (r"\ba\s+year\s+ago\b", lambda m: timedelta(days=365)),
        (r"\brecently\b", lambda m: timedelta(days=7)),
    ]

    for pattern, delta_fn in patterns:
        match = re.search(pattern, query.lower())
        if match:
            return question_date - delta_fn(match)

    return None


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


# =============================================================================
# MAIN BENCHMARK
# =============================================================================


def run_benchmark(
    data_path: str,
    mode: str = "raw",
    limit: int | None = None,
    k_values: list[int] | None = None,
    command: list[str] | None = None,
) -> dict:
    """Run the full LongMemEval benchmark."""
    k_values = k_values or [5, 10]

    dataset_path = Path(data_path)
    with dataset_path.open() as f:
        entries = json.load(f)

    total_entries = len(entries)
    if limit:
        entries = entries[:limit]

    retrieve_fn = retrieve_hybrid if mode == "hybrid" else retrieve_raw

    results_by_type: dict[str, list[dict]] = defaultdict(list)
    all_results: list[dict] = []

    total = len(entries)
    start_time = time.time()

    print(f"\n{'=' * 60}")
    print("  Sibyl x LongMemEval Benchmark")
    print(f"  Mode: {mode}")
    print(f"  Questions: {total}")
    print(f"  K values: {k_values}")
    print(f"{'=' * 60}\n")

    for i, entry in enumerate(entries):
        q_type = entry["question_type"]
        correct = set(entry["answer_session_ids"])

        rankings, corpus_ids = retrieve_fn(entry)
        ranked_session_ids = [corpus_ids[idx] for idx in rankings]
        metrics = score_longmemeval_ranking(ranked_session_ids, correct, k_values)
        result = {
            "question_id": entry["question_id"],
            "question_type": q_type,
            "question": entry.get("question"),
            "question_date": entry.get("question_date"),
            "answer_session_ids": sorted(correct),
            "ranked_session_ids": ranked_session_ids,
            **metrics,
        }
        results_by_type[q_type].append(result)
        all_results.append(result)

        if (i + 1) % 50 == 0 or i == total - 1:
            elapsed = time.time() - start_time
            avg_ms = (elapsed / (i + 1)) * 1000
            progress_k = 5 if 5 in k_values else min(k_values)
            recall_key = f"recall@{progress_k}"
            recall = sum(r[recall_key] for r in all_results) / len(all_results) * 100
            print(f"  [{i + 1:3d}/{total}] R@{progress_k}: {recall:.1f}%  ({avg_ms:.0f}ms/q)")

    # Aggregate
    elapsed = time.time() - start_time

    print(f"\n{'=' * 60}")
    print(f"  RESULTS — {mode} mode")
    print(f"{'=' * 60}")

    overall = {}
    metric_names = [
        f"{metric}@{k}" for k in k_values for metric in ("hit", "legacy_recall", "recall", "ndcg")
    ]
    for k in k_values:
        for metric in ("hit", "legacy_recall", "recall", "ndcg"):
            key = f"{metric}@{k}"
            overall[key] = average_metric(all_results, key)
        print(
            f"  Overall H@{k}: {overall[f'hit@{k}'] * 100:.1f}%  "
            f"R@{k}: {overall[f'recall@{k}'] * 100:.1f}%  "
            f"NDCG@{k}: {overall[f'ndcg@{k}']:.3f}"
        )

    print("\n  Per question type:")
    for q_type, type_results in sorted(results_by_type.items()):
        for k in k_values:
            rk = f"recall@{k}"
            avg = average_metric(type_results, rk)
            print(f"    {q_type:35s} R@{k}: {avg * 100:.1f}% ({len(type_results)} questions)")

    print(f"\n  Time: {elapsed:.1f}s ({elapsed / len(entries) * 1000:.0f}ms/question)")
    print(f"{'=' * 60}\n")

    return {
        "schema_version": "longmemeval-offline-v2",
        "suite": "LongMemEval-style offline",
        "suite_version": "offline-runner-v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "sibyl_commit": _git_commit(),
        "command": command,
        "mode": mode,
        "runtime": {
            "runtime_mode": "offline",
            "graph_engine": "none",
            "store": "chromadb_ephemeral",
            "retrieval_mode": mode,
            "embedding_provider": "chromadb",
            "embedding_model": "chromadb_default",
            "embedding_dimensions": 384,
            "tokenizer_estimate_method": "chromadb_default",
        },
        "dataset": {
            "name": dataset_path.stem,
            "path": data_path,
            "corpus_hash": _sha256_file(dataset_path),
            "corpus_text_policy": CORPUS_TEXT_POLICY,
            "total_entries": total_entries,
            "evaluated_entries": total,
            "limit": limit,
        },
        "repeat_count": 1,
        "auth_manifest_id": "not-applicable:offline",
        "k_values": k_values,
        "total_questions": total,
        "overall": overall,
        "per_type": {
            qt: {metric: average_metric(results, metric) for metric in metric_names}
            for qt, results in results_by_type.items()
        },
        "case_results": all_results,
        "elapsed_seconds": elapsed,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sibyl x LongMemEval Benchmark")
    parser.add_argument("data", help="Path to longmemeval_s_cleaned.json")
    parser.add_argument("--mode", choices=["raw", "hybrid"], default="raw")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N questions")
    parser.add_argument(
        "--k", type=int, nargs="+", default=[5, 10], help="K values for recall/NDCG"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path for the full JSON result artifact.",
    )
    args = parser.parse_args()

    results = run_benchmark(
        args.data,
        mode=args.mode,
        limit=args.limit,
        k_values=args.k,
        command=sys.argv,
    )

    out_path = args.output or Path("benchmarks/results/ai-memory") / (
        f"longmemeval_sibyl_{args.mode}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"  Results saved to {out_path}")
