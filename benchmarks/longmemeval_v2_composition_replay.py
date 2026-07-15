#!/usr/bin/env python3
"""Replay LongMemEval-V2 evidence composition from sealed run artifacts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import string
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.longmemeval_v2_memory.sibyl_memory import (
    assemble_context_results,
    compile_operational_evidence_set,
)

SCHEMA_VERSION = "sibyl-longmemeval-v2-composition-replay-v1"
_PHRASE_SET_EVALUATOR = "norm_phrase_set_match"
_LEGACY_CONTEXT_HEADER = re.compile(
    r"^Retrieved evidence rank (?P<rank>\d+)\n"
    r"Trajectory: (?P<trajectory>[^\n]+)\n"
    r"Chunk: (?P<chunk>\d+)\n"
    r"Score: (?P<score>[^\n]+)$"
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runs = parse_specs(args.run)
    catalogs = parse_specs(args.chunk_catalog)
    manifests = parse_specs(args.memory_manifest)
    if runs.keys() != catalogs.keys() or runs.keys() != manifests.keys():
        raise ValueError("--run, --chunk-catalog, and --memory-manifest domains must match")
    report = replay_composition(
        runs=runs,
        catalogs=catalogs,
        manifests=manifests,
        max_items=args.max_items,
        max_chunks_per_trajectory=args.max_chunks_per_trajectory,
        neighbor_stitch_items=args.neighbor_stitch_items,
        neighbor_stitch_span=args.neighbor_stitch_span,
        state_part_completion_items=args.state_part_completion_items,
        state_part_refinement=args.state_part_refinement,
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(  # noqa: T201
        json.dumps(
            {
                "schema_version": report["schema_version"],
                "sources": report["sources"],
                "configuration": report["configuration"],
                "metrics": report["metrics"],
                "gate": report["gate"],
                "gained_question_ids": report["gained_question_ids"],
                "lost_question_ids": report["lost_question_ids"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, metavar="DOMAIN=PER_QUESTION")
    parser.add_argument(
        "--chunk-catalog",
        action="append",
        required=True,
        metavar="DOMAIN=CHUNK_CATALOG",
    )
    parser.add_argument(
        "--memory-manifest",
        action="append",
        required=True,
        metavar="DOMAIN=MEMORY_MANIFEST",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-items", type=int, default=8)
    parser.add_argument("--max-chunks-per-trajectory", type=int, default=2)
    parser.add_argument("--neighbor-stitch-items", type=int, default=2)
    parser.add_argument("--neighbor-stitch-span", type=int, default=1)
    parser.add_argument("--state-part-completion-items", type=int, default=0)
    parser.add_argument("--state-part-refinement", action="store_true")
    args = parser.parse_args(argv)
    for name in (
        "max_items",
        "max_chunks_per_trajectory",
        "neighbor_stitch_items",
        "neighbor_stitch_span",
        "state_part_completion_items",
    ):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} cannot be negative")
    if args.max_items < 1 or args.max_chunks_per_trajectory < 1:
        parser.error("item and per-trajectory limits must be positive")
    return args


def parse_specs(raw_specs: list[str]) -> dict[str, Path]:
    specs: dict[str, Path] = {}
    for raw_spec in raw_specs:
        domain, separator, raw_path = raw_spec.partition("=")
        domain = domain.strip()
        if not separator or not domain or not raw_path.strip():
            raise ValueError(f"Invalid domain path specification: {raw_spec!r}")
        if domain in specs:
            raise ValueError(f"Duplicate domain specification: {domain!r}")
        specs[domain] = Path(raw_path).expanduser().resolve()
    return specs


def replay_composition(
    *,
    runs: dict[str, Path],
    catalogs: dict[str, Path],
    manifests: dict[str, Path],
    max_items: int,
    max_chunks_per_trajectory: int,
    neighbor_stitch_items: int,
    neighbor_stitch_span: int,
    state_part_completion_items: int = 0,
    state_part_refinement: bool = False,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    for domain in sorted(runs):
        run_path = runs[domain]
        catalog_path = catalogs[domain]
        manifest_path = manifests[domain]
        catalog = load_chunk_catalog(catalog_path)
        domain_rows = load_jsonl(run_path)
        manifest = load_memory_manifest(manifest_path)
        validate_memory_binding(
            manifest=manifest,
            catalog_path=catalog_path,
            rows=domain_rows,
        )
        sources[domain] = {
            "per_question_sha256": sha256_file(run_path),
            "chunk_catalog_sha256": sha256_file(catalog_path),
            "memory_manifest_sha256": sha256_file(manifest_path),
            "question_count": len(domain_rows),
        }
        for row in domain_rows:
            rows.append(
                replay_question(
                    domain=domain,
                    row=row,
                    chunk_catalog=catalog,
                    max_items=max_items,
                    max_chunks_per_trajectory=max_chunks_per_trajectory,
                    neighbor_stitch_items=neighbor_stitch_items,
                    neighbor_stitch_span=neighbor_stitch_span,
                    state_part_completion_items=state_part_completion_items,
                    state_part_refinement=state_part_refinement,
                )
            )

    phrase_rows = [row for row in rows if row["phrase_set_eligible"]]
    baseline_exposed = sum(bool(row["baseline_full_phrase_exposure"]) for row in phrase_rows)
    candidate_exposed = sum(bool(row["candidate_full_phrase_exposure"]) for row in phrase_rows)
    phrase_count = len(phrase_rows)
    typed_evidence_available = any(int(row["baseline_typed_count"]) for row in rows)
    baseline_rate = baseline_exposed / phrase_count if phrase_count else 0.0
    candidate_rate = candidate_exposed / phrase_count if phrase_count else 0.0
    lost = [
        row["question_id"]
        for row in phrase_rows
        if row["baseline_full_phrase_exposure"]
        and not row["candidate_full_phrase_exposure"]
    ]
    gained = [
        row["question_id"]
        for row in phrase_rows
        if row["candidate_full_phrase_exposure"]
        and not row["baseline_full_phrase_exposure"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "sources": sources,
        "configuration": {
            "max_items": max_items,
            "max_chunks_per_trajectory": max_chunks_per_trajectory,
            "neighbor_stitch_items": neighbor_stitch_items,
            "neighbor_stitch_span": neighbor_stitch_span,
            "state_part_completion_items": state_part_completion_items,
            "state_part_refinement": state_part_refinement,
        },
        "metrics": {
            "question_count": len(rows),
            "phrase_set_question_count": phrase_count,
            "baseline_full_phrase_exposure_count": baseline_exposed,
            "baseline_full_phrase_exposure_rate": baseline_rate,
            "candidate_full_phrase_exposure_count": candidate_exposed,
            "candidate_full_phrase_exposure_rate": candidate_rate,
            "full_phrase_exposure_gain_pp": (candidate_rate - baseline_rate) * 100.0,
            "questions_gaining_full_phrase_exposure": len(gained),
            "questions_losing_full_phrase_exposure": len(lost),
            "changed_context_count": sum(bool(row["context_changed"]) for row in rows),
            "raw_assembly_parity_count": sum(
                bool(row["raw_assembly_matches_baseline"]) for row in rows
            ),
            "raw_assembly_parity_rate": (
                sum(bool(row["raw_assembly_matches_baseline"]) for row in rows) / len(rows)
                if rows
                else 0.0
            ),
            "baseline_typed_item_count": sum(int(row["baseline_typed_count"]) for row in rows),
            "candidate_typed_item_count": sum(int(row["candidate_typed_count"]) for row in rows),
            "candidate_neighbor_item_count": sum(
                int(row["candidate_neighbor_count"]) for row in rows
            ),
            "candidate_state_part_item_count": sum(
                int(row["candidate_state_part_count"]) for row in rows
            ),
            "candidate_state_refinement_count": sum(
                int(row["candidate_state_refinement_count"]) for row in rows
            ),
            "typed_evidence_available": typed_evidence_available,
            "legacy_context_header_question_count": sum(
                row["artifact_trace_mode"] == "legacy_context_headers" for row in rows
            ),
            "typed_entity_type_fallback_count": sum(
                int(row["typed_entity_type_fallback_count"]) for row in rows
            ),
        },
        "gate": {
            "minimum_exposure_gain_pp": 3.0,
            "requires_zero_exposure_losses": True,
            "requires_raw_assembly_parity": True,
            "requires_typed_evidence": True,
            "pass": (
                candidate_rate - baseline_rate >= 0.03
                and not lost
                and all(row["raw_assembly_matches_baseline"] for row in rows)
                and typed_evidence_available
            ),
        },
        "gained_question_ids": gained,
        "lost_question_ids": lost,
        "questions": rows,
    }


def replay_question(
    *,
    domain: str,
    row: dict[str, Any],
    chunk_catalog: dict[str, dict[int, dict[str, object]]],
    max_items: int,
    max_chunks_per_trajectory: int,
    neighbor_stitch_items: int,
    neighbor_stitch_span: int,
    state_part_completion_items: int,
    state_part_refinement: bool,
) -> dict[str, Any]:
    query = str(row.get("question_text") or "")
    baseline = result_candidates(row)
    validate_seed_catalog_content(baseline, chunk_catalog=chunk_catalog)
    typed = [
        candidate
        for candidate in baseline
        if str(candidate.get("_selection_origin") or "").startswith("context_pack:")
    ]
    search_seeds = [
        candidate
        for candidate in baseline
        if candidate.get("_selection_origin") in {"search", "state_part_refinement"}
    ]
    search_seeds.sort(key=lambda item: int(item.get("_search_rank") or max_items + 1))
    baseline_assembled, _baseline_assembly = assemble_context_results(
        search_seeds,
        chunk_catalog=chunk_catalog,
        max_items=max_items,
        max_chunks_per_trajectory=max_chunks_per_trajectory,
        neighbor_stitch_items=neighbor_stitch_items,
        neighbor_stitch_span=neighbor_stitch_span,
        query=query,
    )
    candidate_assembled, candidate_assembly = assemble_context_results(
        search_seeds,
        chunk_catalog=chunk_catalog,
        max_items=max_items,
        max_chunks_per_trajectory=max_chunks_per_trajectory,
        neighbor_stitch_items=neighbor_stitch_items,
        neighbor_stitch_span=neighbor_stitch_span,
        query=query,
        state_part_completion_items=state_part_completion_items,
        state_part_refinement=state_part_refinement,
    )
    candidate, composition = compile_operational_evidence_set(
        query=query,
        typed_results=typed,
        raw_results=candidate_assembled,
        max_items=max_items,
        mode="shared_relevance",
    )
    phrases = answer_phrases(row)
    baseline_exposed = full_phrase_exposure(phrases, baseline)
    candidate_exposed = full_phrase_exposure(phrases, candidate)
    baseline_keys = [candidate_key(item) for item in baseline]
    candidate_keys = [candidate_key(item) for item in candidate]
    baseline_raw_keys = [
        candidate_key(item)
        for item in baseline
        if not str(item.get("_selection_origin") or "").startswith("context_pack:")
    ]
    assembled_keys = [candidate_key(item) for item in baseline_assembled]
    return {
        "domain": domain,
        "question_id": str(row.get("question_id") or ""),
        "artifact_trace_mode": artifact_trace_mode(row),
        "phrase_set_eligible": bool(phrases),
        "baseline_full_phrase_exposure": baseline_exposed,
        "candidate_full_phrase_exposure": candidate_exposed,
        "context_changed": baseline_keys != candidate_keys,
        "raw_assembly_matches_baseline": (
            assembled_keys[: len(baseline_raw_keys)] == baseline_raw_keys
        ),
        "baseline_typed_count": len(typed),
        "typed_entity_type_fallback_count": sum(
            bool(item.get("_entity_type_fallback")) for item in typed
        ),
        "candidate_typed_count": composition["selected_typed_count"],
        "candidate_neighbor_count": sum(
            item.get("_selection_origin") == "neighbor" for item in candidate
        ),
        "candidate_state_part_count": sum(
            item.get("_selection_origin") == "state_part" for item in candidate
        ),
        "candidate_state_refinement_count": len(
            candidate_assembly["state_part_refinement"]["replacements"]
        ),
        "baseline_keys": baseline_keys,
        "candidate_keys": candidate_keys,
        "assembled_raw_keys": assembled_keys,
        "composition": composition,
    }


def result_candidates(row: dict[str, Any]) -> list[dict[str, object]]:
    contexts = row.get("memory_context")
    metadata = row.get("memory_post_query_metadata")
    if not isinstance(contexts, list) or not isinstance(metadata, dict):
        raise TypeError("Run row is missing memory context metadata")
    trace = metadata.get("retrieval_trace")
    if trace is None or trace == []:
        return legacy_result_candidates(contexts)
    if not isinstance(trace, list) or len(trace) != len(contexts):
        raise ValueError("Retrieval trace and memory context lengths disagree")
    candidates: list[dict[str, object]] = []
    for trace_item, context_item in zip(trace, contexts, strict=True):
        if not isinstance(trace_item, dict) or not isinstance(context_item, dict):
            raise TypeError("Invalid retrieval trace item")
        value = context_item.get("value")
        if not isinstance(value, str):
            raise TypeError("Memory context item is missing text")
        _header, separator, content = value.partition("\n\n")
        state_indices = trace_item.get("state_indices") or []
        candidates.append(
            {
                "id": str(trace_item.get("entity_id") or ""),
                "type": _trace_entity_type(trace_item),
                "content": content if separator else value,
                "score": trace_item.get("score"),
                "metadata": {
                    "longmemeval_v2_trajectory_id": trace_item.get("trajectory_id"),
                    "longmemeval_v2_chunk_index": trace_item.get("chunk_index"),
                    "longmemeval_v2_state_index": (
                        state_indices[0]
                        if isinstance(state_indices, list) and len(state_indices) == 1
                        else None
                    ),
                    "longmemeval_v2_state_indices": state_indices,
                },
                "_selection_origin": trace_item.get("selection_origin"),
                "_search_rank": trace_item.get("search_rank"),
                "_state_part_of_search_rank": trace_item.get("state_part_of_search_rank"),
                "_state_part_refined_from_chunk": trace_item.get(
                    "state_part_refined_from_chunk"
                ),
                "_neighbor_of_search_rank": trace_item.get("neighbor_of_search_rank"),
                "_neighbor_distance": trace_item.get("neighbor_distance"),
                "_entity_type_fallback": (
                    str(trace_item.get("selection_origin") or "").startswith("context_pack:")
                    and not str(trace_item.get("entity_type") or "").strip()
                ),
            }
        )
    return candidates


def _trace_entity_type(trace_item: dict[str, object]) -> str:
    if entity_type := str(trace_item.get("entity_type") or "").strip():
        return entity_type
    if str(trace_item.get("selection_origin") or "").startswith("context_pack:"):
        return "procedure"
    return "session"


def artifact_trace_mode(row: dict[str, Any]) -> str:
    metadata = row.get("memory_post_query_metadata")
    trace = metadata.get("retrieval_trace") if isinstance(metadata, dict) else None
    return "retrieval_trace" if isinstance(trace, list) and trace else "legacy_context_headers"


def legacy_result_candidates(contexts: list[object]) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for context_item in contexts:
        if not isinstance(context_item, dict):
            raise TypeError("Invalid legacy memory context item")
        value = context_item.get("value")
        if not isinstance(value, str):
            raise TypeError("Legacy memory context item is missing text")
        header, separator, content = value.partition("\n\n")
        match = _LEGACY_CONTEXT_HEADER.fullmatch(header)
        if match is None or not separator:
            raise ValueError("Legacy memory context header is not replayable")
        rank = int(match.group("rank"))
        chunk_index = int(match.group("chunk"))
        trajectory_id = match.group("trajectory").strip()
        candidates.append(
            {
                "id": f"legacy:{trajectory_id}:{chunk_index}",
                "type": "session",
                "content": content,
                "score": float(match.group("score")),
                "metadata": {
                    "longmemeval_v2_trajectory_id": trajectory_id,
                    "longmemeval_v2_chunk_index": chunk_index,
                    "longmemeval_v2_state_index": chunk_index,
                    "longmemeval_v2_state_indices": [chunk_index],
                },
                "_selection_origin": "search",
                "_search_rank": rank,
            }
        )
    return candidates


def load_chunk_catalog(path: Path) -> dict[str, dict[int, dict[str, object]]]:
    catalog: dict[str, dict[int, dict[str, object]]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            metadata = item.get("metadata")
            if not isinstance(metadata, dict):
                continue
            trajectory_id = metadata.get("longmemeval_v2_trajectory_id")
            chunk_index = metadata.get("longmemeval_v2_chunk_index")
            if isinstance(trajectory_id, str) and isinstance(chunk_index, int):
                catalog.setdefault(trajectory_id, {})[chunk_index] = item
    return catalog


def load_memory_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"Memory manifest is not an object: {path}")
    return payload


def validate_memory_binding(
    *,
    manifest: dict[str, object],
    catalog_path: Path,
    rows: list[dict[str, Any]],
) -> None:
    catalog_sha256 = sha256_file(catalog_path)
    if manifest.get("chunk_catalog_sha256") != catalog_sha256:
        raise ValueError("Memory manifest does not bind the supplied chunk catalog")
    expected_run_id = str(manifest.get("run_id") or "")
    expected_project_id = str(manifest.get("project_id") or "")
    if not expected_run_id or not expected_project_id:
        raise ValueError("Memory manifest is missing run or project identity")
    for row in rows:
        metadata = row.get("memory_post_query_metadata")
        if not isinstance(metadata, dict):
            raise TypeError("Run row is missing memory post-query metadata")
        if str(metadata.get("run_id") or "") != expected_run_id:
            raise ValueError("Run row does not match the memory manifest run identity")
        if str(metadata.get("project_id") or "") != expected_project_id:
            raise ValueError("Run row does not match the memory manifest project identity")


def validate_seed_catalog_content(
    candidates: list[dict[str, object]],
    *,
    chunk_catalog: dict[str, dict[int, dict[str, object]]],
) -> None:
    for candidate in candidates:
        if str(candidate.get("_selection_origin") or "") != "search":
            continue
        metadata = candidate.get("metadata")
        if not isinstance(metadata, dict):
            raise TypeError("Search seed is missing replay metadata")
        trajectory_id = str(metadata.get("longmemeval_v2_trajectory_id") or "")
        chunk_index = metadata.get("longmemeval_v2_chunk_index")
        catalog_item = (
            chunk_catalog.get(trajectory_id, {}).get(chunk_index)
            if isinstance(chunk_index, int)
            else None
        )
        if catalog_item is None:
            raise ValueError("Search seed is absent from the bound chunk catalog")
        sealed_content = " ".join(str(candidate.get("content") or "").split())
        catalog_content = " ".join(str(catalog_item.get("content") or "").split())
        if sealed_content != catalog_content:
            raise ValueError("Search seed content disagrees with the bound chunk catalog")


def answer_phrases(row: dict[str, Any]) -> list[str]:
    evaluator = str(row.get("eval_function") or "")
    if not evaluator.startswith(_PHRASE_SET_EVALUATOR):
        return []
    answer = str(row.get("answer_gold") or "")
    return [phrase for part in re.split(r"[;,]", answer) if (phrase := normalize_text(part))]


def full_phrase_exposure(
    phrases: list[str],
    candidates: list[dict[str, object]],
) -> bool:
    if not phrases:
        return False
    context = normalize_text("\n".join(str(item.get("content") or "") for item in candidates))
    return all(phrase in context for phrase in phrases)


def normalize_text(value: str) -> str:
    translation = str.maketrans({character: " " for character in string.punctuation})
    return " ".join(value.lower().replace("-", " ").translate(translation).split())


def candidate_key(candidate: dict[str, object]) -> str:
    origin = str(candidate.get("_selection_origin") or "")
    metadata = candidate.get("metadata")
    if origin.startswith("context_pack:") or not isinstance(metadata, dict):
        return str(candidate.get("id") or "")
    return ":".join(
        (
            str(metadata.get("longmemeval_v2_trajectory_id") or ""),
            str(
                metadata.get("longmemeval_v2_chunk_index")
                if metadata.get("longmemeval_v2_chunk_index") is not None
                else ""
            ),
        )
    )


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


if __name__ == "__main__":
    raise SystemExit(main())
