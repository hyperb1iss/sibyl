#!/usr/bin/env python3
"""
Sibyl × LongMemEval Offline Baseline
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
    uv run python benchmarks/longmemeval_bench.py /tmp/longmemeval-data/longmemeval_s_cleaned.json
    uv run python benchmarks/longmemeval_bench.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --limit 20
    uv run python benchmarks/longmemeval_bench.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --mode hybrid
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import chromadb
except ModuleNotFoundError:
    chromadb = None

sys.path.insert(0, str(Path(__file__).parent.parent / "packages" / "python" / "sibyl-core" / "src"))


# =============================================================================
# METRICS (same as MemPalace for apple-to-apple comparison)
# =============================================================================


def dcg(relevances: list[float], k: int) -> float:
    score = 0.0
    for i, rel in enumerate(relevances[:k]):
        score += rel / math.log2(i + 2)
    return score


def ndcg_score(rankings: list[int], correct_ids: set[str], corpus_ids: list[str], k: int) -> float:
    relevances = [1.0 if corpus_ids[idx] in correct_ids else 0.0 for idx in rankings[:k]]
    ideal = sorted(relevances, reverse=True)
    idcg = dcg(ideal, k)
    if idcg == 0:
        return 0.0
    return dcg(relevances, k) / idcg


def recall_at_k(rankings: list[int], correct_ids: set[str], corpus_ids: list[str], k: int) -> float:
    top_k = {corpus_ids[idx] for idx in rankings[:k]}
    return float(any(cid in top_k for cid in correct_ids))


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
    return [word for word in re.findall(r"\b[a-z]{3,}\b", text.lower()) if word not in _HYBRID_STOP_WORDS]


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

    corpus, corpus_ids = _build_corpus(entry)
    timestamps = entry.get("haystack_dates", [])
    if not corpus:
        return [], corpus_ids

    collection = _fresh_collection()
    collection.add(
        documents=corpus,
        ids=[f"doc_{i}" for i in range(len(corpus))],
        metadatas=[
            {"corpus_id": cid, "timestamp": ts if i < len(timestamps) else ""}
            for i, (cid, ts) in enumerate(
                zip(corpus_ids, timestamps + [""] * max(0, len(corpus_ids) - len(timestamps)))
            )
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
    corpus = []
    corpus_ids = []
    for session, sess_id in zip(entry["haystack_sessions"], entry["haystack_session_ids"]):
        user_turns = [t["content"] for t in session if t["role"] == "user"]
        if user_turns:
            corpus.append("\n".join(user_turns))
            corpus_ids.append(sess_id)
    return corpus, corpus_ids


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


# =============================================================================
# MAIN BENCHMARK
# =============================================================================


def run_benchmark(
    data_path: str,
    mode: str = "raw",
    limit: int | None = None,
    k_values: list[int] | None = None,
) -> dict:
    """Run the full LongMemEval benchmark."""
    k_values = k_values or [5, 10]

    with open(data_path) as f:
        entries = json.load(f)

    if limit:
        entries = entries[:limit]

    retrieve_fn = retrieve_hybrid if mode == "hybrid" else retrieve_raw

    results_by_type: dict[str, list[dict]] = defaultdict(list)
    all_results: list[dict] = []

    total = len(entries)
    start_time = time.time()

    print(f"\n{'='*60}")
    print(f"  Sibyl × LongMemEval Benchmark")
    print(f"  Mode: {mode}")
    print(f"  Questions: {total}")
    print(f"  K values: {k_values}")
    print(f"{'='*60}\n")

    for i, entry in enumerate(entries):
        q_type = entry["question_type"]
        correct = set(entry["answer_session_ids"])

        rankings, corpus_ids = retrieve_fn(entry)

        metrics = {}
        for k in k_values:
            r = recall_at_k(rankings, correct, corpus_ids, k)
            n = ndcg_score(rankings, correct, corpus_ids, k)
            metrics[f"recall@{k}"] = r
            metrics[f"ndcg@{k}"] = n

        result = {
            "question_id": entry["question_id"],
            "question_type": q_type,
            **metrics,
        }
        results_by_type[q_type].append(result)
        all_results.append(result)

        if (i + 1) % 50 == 0 or i == total - 1:
            elapsed = time.time() - start_time
            avg_ms = (elapsed / (i + 1)) * 1000
            r5 = sum(r["recall@5"] for r in all_results) / len(all_results) * 100
            print(f"  [{i+1:3d}/{total}] R@5: {r5:.1f}%  ({avg_ms:.0f}ms/q)")

    # Aggregate
    elapsed = time.time() - start_time

    print(f"\n{'='*60}")
    print(f"  RESULTS — {mode} mode")
    print(f"{'='*60}")

    overall = {}
    for k in k_values:
        rk = f"recall@{k}"
        nk = f"ndcg@{k}"
        overall[rk] = sum(r[rk] for r in all_results) / len(all_results)
        overall[nk] = sum(r[nk] for r in all_results) / len(all_results)
        print(f"  Overall R@{k}: {overall[rk]*100:.1f}%  NDCG@{k}: {overall[nk]:.3f}")

    print(f"\n  Per question type:")
    for q_type, type_results in sorted(results_by_type.items()):
        for k in k_values:
            rk = f"recall@{k}"
            avg = sum(r[rk] for r in type_results) / len(type_results)
            print(f"    {q_type:35s} R@{k}: {avg*100:.1f}% ({len(type_results)} questions)")

    print(f"\n  Time: {elapsed:.1f}s ({elapsed/len(entries)*1000:.0f}ms/question)")
    print(f"{'='*60}\n")

    return {
        "mode": mode,
        "total_questions": total,
        "overall": overall,
        "per_type": {
            qt: {
                metric: sum(r[metric] for r in results) / len(results)
                for metric in results[0] if metric.startswith(("recall", "ndcg"))
            }
            for qt, results in results_by_type.items()
        },
        "elapsed_seconds": elapsed,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sibyl × LongMemEval Benchmark")
    parser.add_argument("data", help="Path to longmemeval_s_cleaned.json")
    parser.add_argument("--mode", choices=["raw", "hybrid"], default="raw")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N questions")
    parser.add_argument("--k", type=int, nargs="+", default=[5, 10], help="K values for recall/NDCG")
    args = parser.parse_args()

    results = run_benchmark(args.data, mode=args.mode, limit=args.limit, k_values=args.k)

    out_path = f"benchmarks/results_sibyl_{args.mode}.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  Results saved to {out_path}")
