"""STAGE 1 - offline selection simulation: slices vs fat states.

Decision rule pre-registered in out/STAGE1_PREREGISTRATION.md BEFORE this ran.

Arms (per domain, over the official lme_v2_small 100-trajectory haystack):
  FAT         the chunk as indexed today (trajectory preamble + state + tree)
  SLICE       the A1 slice as designed (header + breadcrumb + content)
  SLICE_GOAL  ablation: trajectory preamble prefixed to the slice

Rankers: BM25, dense (local MiniLM, window mean-pooled), RRF fusion.
Metric:  recall@k = union of top-k units covers every gold phrase.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import CountVectorizer

sys.path.insert(0, str(Path(__file__).parent))

from corpus import OUT, build_units, eligible_questions, normalize_text, state_evidence_text

K_GRID = (1, 3, 5, 10, 20, 50, 100, 200)
CHAR_BUDGETS = (5_000, 10_000, 25_000, 50_000, 100_000, 135_000, 200_000)
WINDOW_CHARS = 900
MAX_WINDOWS = 24
RRF_K = 60
SCAN_LIMIT = 2000
BM25_K1 = 1.2
BM25_B = 0.75
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def tokens(text: str) -> list[str]:
    return normalize_text(text).split()


def build_bm25(documents: list[str]) -> tuple[csr_matrix, dict[str, int]]:
    vectorizer = CountVectorizer(analyzer=tokens, min_df=1, dtype=np.float32)
    counts = vectorizer.fit_transform(documents).tocsr()
    doc_count = counts.shape[0]
    document_frequency = np.asarray((counts > 0).sum(axis=0)).ravel()
    idf = np.log(1.0 + (doc_count - document_frequency + 0.5) / (document_frequency + 0.5))
    lengths = np.asarray(counts.sum(axis=1)).ravel()
    average_length = lengths.mean()
    weights = counts.copy()
    row_index = np.repeat(np.arange(doc_count), np.diff(weights.indptr))
    term_frequency = weights.data
    denominator = term_frequency + BM25_K1 * (
        1 - BM25_B + BM25_B * lengths[row_index] / average_length
    )
    weights.data = idf[weights.indices] * term_frequency * (BM25_K1 + 1) / denominator
    return weights.tocsc(), vectorizer.vocabulary_


def bm25_scores(weights: Any, vocabulary: dict[str, int], query: str) -> np.ndarray:
    ids = [vocabulary[t] for t in set(tokens(query)) if t in vocabulary]
    if not ids:
        return np.zeros(weights.shape[0], dtype=np.float32)
    return np.asarray(weights[:, ids].sum(axis=1)).ravel()


def windows(text: str) -> list[str]:
    if not text:
        return [""]
    pieces = [text[i : i + WINDOW_CHARS] for i in range(0, len(text), WINDOW_CHARS)]
    return pieces[:MAX_WINDOWS]


def encode_pooled(model: Any, texts: list[str], batch: int = 1024, label: str = "") -> tuple:
    """Sum 900-char window embeddings per text, streaming to bound memory."""
    counts = [len(windows(t)) for t in texts]
    total = sum(counts)
    dimension = model.get_sentence_embedding_dimension()
    pooled = np.zeros((len(texts), dimension), dtype=np.float32)
    buffer: list[str] = []
    owners: list[int] = []
    done = 0
    for index, text in enumerate(texts):
        for piece in windows(text):
            buffer.append(piece)
            owners.append(index)
        if len(buffer) >= 16384:
            vectors = model.encode(
                buffer,
                batch_size=batch,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            ).astype(np.float32)
            np.add.at(pooled, np.asarray(owners), vectors)
            done += len(buffer)
            print(f"    [{label}] {done}/{total} windows", flush=True)
            buffer, owners = [], []
    if buffer:
        vectors = model.encode(
            buffer,
            batch_size=batch,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).astype(np.float32)
        np.add.at(pooled, np.asarray(owners), vectors)
        done += len(buffer)
        print(f"    [{label}] {done}/{total} windows", flush=True)
    window_counts = np.asarray(counts, dtype=np.float32)
    normed = pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9)
    return normed, window_counts, pooled


def rrf(*rank_lists: np.ndarray) -> np.ndarray:
    fused = np.zeros(rank_lists[0].shape[0], dtype=np.float32)
    for ranks in rank_lists:
        fused += 1.0 / (RRF_K + ranks)
    return fused


def ranks_from_scores(scores: np.ndarray) -> np.ndarray:
    order = np.argsort(-scores, kind="stable")
    ranks = np.empty_like(order)
    ranks[order] = np.arange(1, len(order) + 1)
    return ranks


def recall_curve(
    order: np.ndarray,
    gold_masks: list[np.ndarray],
    unit_chars: np.ndarray,
) -> dict[str, Any]:
    phrase_count = len(gold_masks)
    covered = set()
    at_k: dict[int, int] = {}
    at_budget: dict[int, int] = {}
    first_gold_rank = None
    complete_rank = None
    cumulative = 0
    budget_pointer = 0
    k_pointer = 0
    sorted_k = sorted(K_GRID)
    sorted_budgets = sorted(CHAR_BUDGETS)
    for position, unit in enumerate(order, start=1):
        for index, mask in enumerate(gold_masks):
            if index not in covered and mask[unit]:
                covered.add(index)
                if first_gold_rank is None:
                    first_gold_rank = position
        cumulative += int(unit_chars[unit])
        if complete_rank is None and len(covered) == phrase_count:
            complete_rank = position
        while k_pointer < len(sorted_k) and sorted_k[k_pointer] == position:
            at_k[sorted_k[k_pointer]] = len(covered)
            k_pointer += 1
        while budget_pointer < len(sorted_budgets) and cumulative >= sorted_budgets[budget_pointer]:
            at_budget[sorted_budgets[budget_pointer]] = len(covered)
            budget_pointer += 1
        if position >= SCAN_LIMIT:
            break
    for k in sorted_k:
        at_k.setdefault(k, len(covered))
    for budget in sorted_budgets:
        at_budget.setdefault(budget, len(covered))
    return {
        "hit_at_k": {str(k): at_k[k] == phrase_count for k in sorted_k},
        "cover_at_k": {str(k): at_k[k] / phrase_count for k in sorted_k},
        "hit_at_budget": {str(b): at_budget[b] == phrase_count for b in sorted_budgets},
        "first_gold_rank": first_gold_rank,
        "complete_rank": complete_rank,
    }


def run_domain(domain: str, model: Any) -> dict[str, Any]:
    units = build_units(domain)
    states = units["states"]
    norm_state = {k: normalize_text(state_evidence_text(e)) for k, e in states.items()}

    measurable = []
    for item in eligible_questions(domain):
        phrases = item["phrases"]
        if all(any(p in text for text in norm_state.values()) for p in phrases):
            measurable.append(item)
    print(f"[{domain}] measurable questions: {len(measurable)}", flush=True)

    arms: dict[str, list[dict[str, Any]]] = {
        "FAT": units["fat"],
        "SLICE": units["slices"],
    }

    results: dict[str, Any] = {"domain": domain, "measurable": len(measurable), "arms": {}}
    cached_slice_pooled = None

    for arm_name, rows in arms.items():
        index_texts = [r["index_text"] for r in rows]
        exposure_norm = [normalize_text(r["exposure_text"]) for r in rows]
        unit_chars = np.asarray([len(r["exposure_text"]) for r in rows], dtype=np.int64)
        print(
            f"[{domain}/{arm_name}] units={len(rows)} mean_chars={unit_chars.mean():.0f}",
            flush=True,
        )
        weights, vocabulary = build_bm25(index_texts)
        dense, _window_counts, pooled_sum = encode_pooled(
            model, index_texts, label=f"{domain}/{arm_name}"
        )
        if arm_name == "SLICE":
            cached_slice_pooled = pooled_sum
        results["arms"][arm_name] = score_arm(
            arm_name, rows, measurable, weights, vocabulary, dense, exposure_norm, unit_chars, model
        )

    # SLICE_GOAL ablation: mean-pool the trajectory preamble windows into the slice vector.
    rows = units["slices"]
    preambles = sorted({r["preamble"] for r in rows})
    preamble_index = {text: i for i, text in enumerate(preambles)}
    _, _preamble_windows, preamble_sum = encode_pooled(
        model, preambles, label=f"{domain}/preambles"
    )
    ids = np.asarray([preamble_index[r["preamble"]] for r in rows])
    combined = cached_slice_pooled + preamble_sum[ids]
    combined /= np.maximum(np.linalg.norm(combined, axis=1, keepdims=True), 1e-9)
    goal_texts = [r["preamble"] + r["index_text"] for r in rows]
    weights, vocabulary = build_bm25(goal_texts)
    exposure_norm = [normalize_text(r["exposure_text"]) for r in rows]
    unit_chars = np.asarray([len(r["exposure_text"]) for r in rows], dtype=np.int64)
    results["arms"]["SLICE_GOAL"] = score_arm(
        "SLICE_GOAL",
        rows,
        measurable,
        weights,
        vocabulary,
        combined,
        exposure_norm,
        unit_chars,
        model,
    )
    return results


def score_arm(
    arm_name: str,
    rows: list[dict[str, Any]],
    measurable: list[dict[str, Any]],
    weights: csr_matrix,
    vocabulary: dict[str, int],
    dense: np.ndarray,
    exposure_norm: list[str],
    unit_chars: np.ndarray,
    model: Any,
) -> dict[str, Any]:
    queries = [item["question"]["question"] for item in measurable]
    query_vectors = model.encode(
        queries, batch_size=64, convert_to_numpy=True, normalize_embeddings=True
    ).astype(np.float32)
    per_question: list[dict[str, Any]] = []
    for position, item in enumerate(measurable):
        phrases = item["phrases"]
        gold_masks = [
            np.asarray([phrase in text for text in exposure_norm], dtype=bool) for phrase in phrases
        ]
        lexical = bm25_scores(weights, vocabulary, item["question"]["question"])
        semantic = dense @ query_vectors[position]
        fused = rrf(ranks_from_scores(lexical), ranks_from_scores(semantic))
        entry: dict[str, Any] = {
            "question_id": item["question"]["id"],
            "question_type": item["question"].get("question_type"),
            "phrase_count": len(phrases),
            "gold_units": [int(mask.sum()) for mask in gold_masks],
        }
        for ranker_name, scores in (
            ("bm25", lexical),
            ("dense", semantic),
            ("rrf", fused),
        ):
            order = np.argsort(-scores, kind="stable")
            entry[ranker_name] = recall_curve(order, gold_masks, unit_chars)
        per_question.append(entry)

    summary: dict[str, Any] = {"units": len(rows), "mean_unit_chars": float(unit_chars.mean())}
    for ranker_name in ("bm25", "dense", "rrf"):
        summary[ranker_name] = {
            "recall_at_k": {
                str(k): round(
                    sum(1 for e in per_question if e[ranker_name]["hit_at_k"][str(k)])
                    / len(per_question),
                    4,
                )
                for k in K_GRID
            },
            "recall_at_char_budget": {
                str(b): round(
                    sum(1 for e in per_question if e[ranker_name]["hit_at_budget"][str(b)])
                    / len(per_question),
                    4,
                )
                for b in CHAR_BUDGETS
            },
            "median_complete_rank": float(
                np.median([e[ranker_name]["complete_rank"] or 10**9 for e in per_question])
            ),
            "median_first_gold_rank": float(
                np.median([e[ranker_name]["first_gold_rank"] or 10**9 for e in per_question])
            ),
        }
    summary["per_question"] = per_question
    return summary


def main() -> None:
    import torch
    from sentence_transformers import SentenceTransformer

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"device={device}", flush=True)
    model = SentenceTransformer(MODEL_NAME, device=device)
    model.max_seq_length = 256
    report = {}
    for domain in ("enterprise", "web"):
        report[domain] = run_domain(domain, model)
        (OUT / "stage1_report.json").write_text(json.dumps(report, indent=2) + "\n")
        for arm, data in report[domain]["arms"].items():
            print(
                f"[{domain}] {arm:11s} units={data['units']:7d} "
                f"rrf@10={data['rrf']['recall_at_k']['10']} "
                f"rrf@50={data['rrf']['recall_at_k']['50']} "
                f"rrf@135k_chars={data['rrf']['recall_at_char_budget']['135000']}",
                flush=True,
            )
    (OUT / "stage1_report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
