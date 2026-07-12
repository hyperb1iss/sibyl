#!/usr/bin/env python3
"""Build frozen, retrieval-only diagnostics from LongMemEval-V2 artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

DIAGNOSTIC_SCHEMA_VERSION = "sibyl-longmemeval-v2-diagnostics-v1"
SLICE_SCHEMA_VERSION = "sibyl-longmemeval-v2-diagnostic-slice-v1"
DEFAULT_MAX_RANK = 10
DEFAULT_SLICE_SIZE_PER_DOMAIN = 16
MIN_EXACT_ANSWER_LENGTH = 8
MULTI_STATE_MIN_EVIDENCE_STATES = 2
_GENERIC_ANSWERS = frozenset({"true", "false", "yes", "no", "none", "null", "unknown", "n/a"})
_TRAJECTORY_PATTERN = re.compile(r"^Trajectory:\s*(\S+)", re.MULTILINE)
_CHUNK_PATTERN = re.compile(r"^Chunk:\s*(\d+)", re.MULTILINE)
_STATE_PATTERN = re.compile(r"^State\s+(\d+)\b", re.MULTILINE)
_RANK_PATTERN = re.compile(r"^Retrieved evidence rank\s+(\d+)", re.MULTILINE)
_SCORE_PATTERN = re.compile(r"^Score:\s*([^\n]+)", re.MULTILINE)
_JSONL_ID_PREFIX_PATTERN = re.compile(r'^\s*\{\s*"id"\s*:\s*"([^"]+)"')
_FAILURE_PRIORITY = (
    "trajectory_selection_miss",
    "state_selection_miss",
    "evidence_exposure_miss",
    "reader_or_scoring_miss",
    "unlabeled_exact_evidence",
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_root = Path(args.data_root).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    runs = parse_run_specs(args.run)
    questions = load_jsonl_by_id(data_root / "questions.jsonl")
    trajectories = load_jsonl_by_id(
        data_root / "trajectories.jsonl",
        required_ids=required_trajectory_ids(runs),
    )
    trace_rows, source_artifacts = build_trace_rows(
        runs=runs,
        questions=questions,
        trajectories=trajectories,
        max_rank=args.max_rank,
    )

    if args.slice:
        slice_record = load_json(Path(args.slice).expanduser().resolve())
        validate_slice_sources(slice_record, source_artifacts)
        selected_ids = {
            str(case["question_id"])
            for case in slice_record.get("cases", [])
            if isinstance(case, dict) and case.get("question_id")
        }
        trace_rows = [row for row in trace_rows if row["question_id"] in selected_ids]
        found_ids = {str(row["question_id"]) for row in trace_rows}
        if missing_ids := selected_ids - found_ids:
            msg = f"Diagnostic slice questions missing from run artifacts: {sorted(missing_ids)}"
            raise ValueError(msg)
    else:
        slice_record = build_diagnostic_slice(
            trace_rows,
            source_artifacts=source_artifacts,
            size_per_domain=args.slice_size_per_domain,
            max_rank=args.max_rank,
        )

    report = build_diagnostic_report(
        trace_rows,
        source_artifacts=source_artifacts,
        max_rank=args.max_rank,
        slice_record=slice_record,
    )
    write_jsonl(output_dir / "retrieval_trace.jsonl", trace_rows)
    write_json(output_dir / "diagnostic_report.json", report)
    write_json(Path(args.slice_output).expanduser().resolve(), slice_record)
    print(json.dumps(report, indent=2, sort_keys=True))  # noqa: T201
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument(
        "--run",
        action="append",
        required=True,
        metavar="DOMAIN=OUTPUT_DIR",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--slice", default=None)
    parser.add_argument("--slice-output", required=True)
    parser.add_argument("--slice-size-per-domain", type=int, default=DEFAULT_SLICE_SIZE_PER_DOMAIN)
    parser.add_argument("--max-rank", type=int, default=DEFAULT_MAX_RANK)
    args = parser.parse_args(argv)
    if args.slice_size_per_domain <= 0:
        parser.error("--slice-size-per-domain must be positive")
    if args.max_rank <= 0:
        parser.error("--max-rank must be positive")
    return args


def parse_run_specs(raw_specs: list[str]) -> dict[str, Path]:
    runs: dict[str, Path] = {}
    for raw_spec in raw_specs:
        domain, separator, raw_path = raw_spec.partition("=")
        domain = domain.strip()
        if not separator or not domain or not raw_path.strip():
            msg = f"Invalid --run value {raw_spec!r}; expected DOMAIN=OUTPUT_DIR"
            raise ValueError(msg)
        if domain in runs:
            msg = f"Duplicate --run domain {domain!r}"
            raise ValueError(msg)
        runs[domain] = Path(raw_path).expanduser().resolve()
    return runs


def required_trajectory_ids(runs: dict[str, Path]) -> set[str]:
    return {
        str(trajectory_id)
        for run_dir in runs.values()
        for trajectory_ids in load_json(run_dir / "runtime_inputs" / "haystack.json").values()
        if isinstance(trajectory_ids, list)
        for trajectory_id in trajectory_ids
    }


def build_trace_rows(
    *,
    runs: dict[str, Path],
    questions: dict[str, dict[str, Any]],
    trajectories: dict[str, dict[str, Any]],
    max_rank: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    state_text_index = build_state_text_index(trajectories)
    for domain, run_dir in sorted(runs.items()):
        per_question_path = run_dir / "per_question.jsonl"
        haystack_path = run_dir / "runtime_inputs" / "haystack.json"
        haystack = load_json(haystack_path)
        sources[domain] = {
            "per_question_sha256": sha256_file(per_question_path),
            "haystack_sha256": sha256_file(haystack_path),
            "question_count": 0,
        }
        for result in load_jsonl(per_question_path):
            question_id = str(result.get("question_id") or "")
            question = questions.get(question_id)
            if question is None:
                msg = f"Missing question {question_id!r} for {domain} run"
                raise TypeError(msg)
            trajectory_ids = haystack.get(question_id)
            if not isinstance(trajectory_ids, list):
                msg = f"Missing haystack for question {question_id!r}"
                raise TypeError(msg)
            rows.append(
                build_question_trace(
                    domain=domain,
                    result=result,
                    question=question,
                    haystack_ids=[str(item) for item in trajectory_ids],
                    state_text_index=state_text_index,
                    max_rank=max_rank,
                )
            )
            sources[domain]["question_count"] += 1
    return rows, sources


def build_question_trace(
    *,
    domain: str,
    result: dict[str, Any],
    question: dict[str, Any],
    haystack_ids: list[str],
    state_text_index: dict[str, list[tuple[int, str]]],
    max_rank: int,
) -> dict[str, Any]:
    answer = str(question.get("answer") or "")
    normalized_answer = normalize_text(answer)
    evidence_eligible = is_exact_evidence_eligible(normalized_answer)
    evidence_states = (
        find_exact_evidence_states(
            normalized_answer,
            haystack_ids=haystack_ids,
            state_text_index=state_text_index,
        )
        if evidence_eligible
        else []
    )
    evidence_trajectories = sorted({item["trajectory_id"] for item in evidence_states})
    retrieved = parse_retrieved_items(result)
    ranked = [item for item in retrieved if int(item["rank"]) <= max_rank]
    retrieved_trajectory_ids = {
        str(item["trajectory_id"]) for item in ranked if item.get("trajectory_id")
    }
    retrieved_state_pairs = {
        (str(item["trajectory_id"]), int(state_index))
        for item in ranked
        if item.get("trajectory_id")
        for state_index in item["state_indices"]
    }
    gold_state_pairs = {
        (str(item["trajectory_id"]), int(item["state_index"])) for item in evidence_states
    }
    trajectory_hit = bool(retrieved_trajectory_ids & set(evidence_trajectories))
    matched_state_count = len(retrieved_state_pairs & gold_state_pairs)
    state_hit = matched_state_count > 0
    visible_hit = any(
        normalized_answer and normalized_answer in normalize_text(str(item["state_content"]))
        for item in ranked
    )
    score_bool = result.get("score_bool")
    failure_class = classify_failure(
        evidence_eligible=evidence_eligible,
        evidence_states=evidence_states,
        trajectory_hit=trajectory_hit,
        state_hit=state_hit,
        visible_hit=visible_hit,
        score_bool=score_bool,
    )
    return {
        "question_id": str(question["id"]),
        "domain": domain,
        "question_type": str(question.get("question_type") or "unknown"),
        "baseline_score_bool": score_bool if isinstance(score_bool, bool) else None,
        "answer_sha256": sha256_text(answer),
        "answer_length": len(answer),
        "exact_evidence_eligible": evidence_eligible,
        "exact_evidence_trajectory_ids": evidence_trajectories,
        "exact_evidence_states": evidence_states,
        "retrieved": ranked,
        "metrics": {
            "trajectory_recall_at_k": trajectory_hit,
            "state_recall_at_k": state_hit,
            "exact_context_recall_at_k": visible_hit,
            "evidence_state_count": len(gold_state_pairs),
            "matched_evidence_state_count": matched_state_count,
            "multi_state_evidence_coverage_at_k": (
                matched_state_count / len(gold_state_pairs) if gold_state_pairs else None
            ),
        },
        "failure_class": failure_class,
        "memory_query_duration_seconds": finite_number(result.get("memory_query_duration_seconds")),
        "stage_timings_ms": nested_dict(
            result,
            "memory_post_query_metadata",
            "search_metadata",
            "stage_timings_ms",
        ),
    }


def parse_retrieved_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    contexts = parse_context_items(result.get("memory_context"))
    structured = nested_value(
        result,
        "memory_post_query_metadata",
        "retrieval_trace",
    )
    if isinstance(structured, list) and structured:
        context_by_rank = {int(item["rank"]): item for item in contexts}
        normalized = [
            normalize_structured_retrieval(item) for item in structured if isinstance(item, dict)
        ]
        for item in normalized:
            context = context_by_rank.get(int(item["rank"]), {})
            item["state_content"] = context.get("state_content", "")
            if not item["state_indices"]:
                item["state_indices"] = context.get("state_indices", [])
        return normalized
    return contexts


def parse_context_items(contexts: Any) -> list[dict[str, Any]]:
    if not isinstance(contexts, list):
        return []
    parsed: list[dict[str, Any]] = []
    for fallback_rank, context in enumerate(contexts, start=1):
        if not isinstance(context, dict):
            continue
        value = str(context.get("value") or "")
        parsed.append(
            {
                "rank": regex_int(_RANK_PATTERN, value) or fallback_rank,
                "entity_id": None,
                "trajectory_id": regex_string(_TRAJECTORY_PATTERN, value),
                "chunk_index": regex_int(_CHUNK_PATTERN, value),
                "state_indices": [int(item) for item in _STATE_PATTERN.findall(value)],
                "score": regex_float(_SCORE_PATTERN, value),
                "content_chars": len(value),
                "state_content": context_state_content(value),
            }
        )
    return parsed


def normalize_structured_retrieval(item: dict[str, Any]) -> dict[str, Any]:
    state_indices = item.get("state_indices")
    if not isinstance(state_indices, list):
        state_indices = []
    return {
        "rank": int(finite_number(item.get("rank")) or 0),
        "entity_id": item.get("entity_id"),
        "trajectory_id": item.get("trajectory_id"),
        "chunk_index": item.get("chunk_index"),
        "state_indices": [
            int(value) for value in state_indices if finite_number(value) is not None
        ],
        "score": finite_number(item.get("score")),
        "content_chars": int(finite_number(item.get("content_chars")) or 0),
        "state_content": str(item.get("state_content") or item.get("content") or ""),
    }


def find_exact_evidence_states(
    normalized_answer: str,
    *,
    haystack_ids: list[str],
    state_text_index: dict[str, list[tuple[int, str]]],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for trajectory_id in haystack_ids:
        for state_index, normalized_state in state_text_index.get(trajectory_id, []):
            if normalized_answer not in normalized_state:
                continue
            evidence.append(
                {
                    "trajectory_id": trajectory_id,
                    "state_index": state_index,
                }
            )
    return evidence


def build_state_text_index(
    trajectories: dict[str, dict[str, Any]],
) -> dict[str, list[tuple[int, str]]]:
    index: dict[str, list[tuple[int, str]]] = {}
    for trajectory_id, trajectory in trajectories.items():
        states = trajectory.get("states")
        if not isinstance(states, list):
            continue
        normalized_states: list[tuple[int, str]] = []
        for fallback_index, state in enumerate(states):
            if not isinstance(state, dict):
                continue
            state_index = state.get("state_index")
            normalized_states.append(
                (
                    int(state_index) if isinstance(state_index, int) else fallback_index,
                    normalize_text(state_evidence_text(state)),
                )
            )
        index[trajectory_id] = normalized_states
    return index


def state_evidence_text(state: dict[str, Any]) -> str:
    fields = ("url", "action", "thought", "accessibility_tree")
    return "\n".join(str(state.get(field) or "") for field in fields)


def context_state_content(value: str) -> str:
    match = _STATE_PATTERN.search(value)
    return value[match.start() :] if match else value


def classify_failure(
    *,
    evidence_eligible: bool,
    evidence_states: list[dict[str, Any]],
    trajectory_hit: bool,
    state_hit: bool,
    visible_hit: bool,
    score_bool: Any,
) -> str:
    if not evidence_eligible or not evidence_states:
        return "unlabeled_exact_evidence"
    if not trajectory_hit:
        return "trajectory_selection_miss"
    if not state_hit:
        return "state_selection_miss"
    if not visible_hit:
        return "evidence_exposure_miss"
    if score_bool is not True:
        return "reader_or_scoring_miss"
    return "retrieval_supported_success"


def build_diagnostic_slice(
    trace_rows: list[dict[str, Any]],
    *,
    source_artifacts: dict[str, dict[str, Any]],
    size_per_domain: int,
    max_rank: int,
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    for domain in sorted({str(row["domain"]) for row in trace_rows}):
        domain_rows = [row for row in trace_rows if row["domain"] == domain]
        control_count = min(max(2, size_per_domain // 4), size_per_domain)
        failure_count = max(0, size_per_domain - control_count)
        failures = [row for row in domain_rows if row["baseline_score_bool"] is not True]
        controls = [row for row in domain_rows if row["baseline_score_bool"] is True]
        labeled_failures = [
            row for row in failures if row["failure_class"] != "unlabeled_exact_evidence"
        ]
        chosen = round_robin_strata(labeled_failures, failure_count)
        chosen_ids = {str(row["question_id"]) for row in chosen}
        if len(chosen) < failure_count:
            remaining_failures = [row for row in failures if row["question_id"] not in chosen_ids]
            chosen.extend(
                round_robin_strata(
                    remaining_failures,
                    failure_count - len(chosen),
                )
            )
        supported_controls = [
            row for row in controls if row["failure_class"] == "retrieval_supported_success"
        ]
        chosen_controls = round_robin_strata(supported_controls, control_count)
        control_ids = {str(row["question_id"]) for row in chosen_controls}
        if len(chosen_controls) < control_count:
            remaining_controls = [row for row in controls if row["question_id"] not in control_ids]
            chosen_controls.extend(
                round_robin_strata(
                    remaining_controls,
                    control_count - len(chosen_controls),
                )
            )
        chosen.extend(chosen_controls)
        chosen_ids = {str(row["question_id"]) for row in chosen}
        if len(chosen) < size_per_domain:
            remaining = [row for row in domain_rows if row["question_id"] not in chosen_ids]
            chosen.extend(stable_rows(remaining)[: size_per_domain - len(chosen)])
        selected.extend(slice_case(row) for row in chosen[:size_per_domain])
    return {
        "schema_version": SLICE_SCHEMA_VERSION,
        "selection_policy": "deterministic round-robin by failure class and question type",
        "source_artifacts": source_artifacts,
        "max_rank": max_rank,
        "cases": selected,
        "decision_thresholds": {
            "go": {
                "exact_context_recall_at_10_absolute_gain": 0.15,
                "multi_state_evidence_coverage_at_10_absolute_gain": 0.10,
            },
            "no_go": {"all_arms_exact_context_recall_gain_below": 0.08},
            "research_more": {
                "exact_context_recall_gain_min": 0.08,
                "exact_context_recall_gain_max": 0.15,
            },
        },
    }


def validate_slice_sources(
    slice_record: dict[str, Any],
    source_artifacts: dict[str, dict[str, Any]],
) -> None:
    if slice_record.get("schema_version") != SLICE_SCHEMA_VERSION:
        msg = f"Diagnostic slice schema must be {SLICE_SCHEMA_VERSION!r}"
        raise ValueError(msg)
    expected_sources = slice_record.get("source_artifacts")
    if not isinstance(expected_sources, dict):
        msg = "Diagnostic slice source_artifacts must be an object"
        raise TypeError(msg)
    for domain, expected in expected_sources.items():
        current = source_artifacts.get(str(domain))
        if not isinstance(expected, dict) or current is None:
            msg = f"Diagnostic slice source domain {domain!r} is unavailable"
            raise ValueError(msg)
        if expected.get("haystack_sha256") != current.get("haystack_sha256"):
            msg = f"Diagnostic slice haystack hash mismatch for {domain}"
            raise ValueError(msg)


def round_robin_strata(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    strata: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    priority = {name: index for index, name in enumerate(_FAILURE_PRIORITY)}
    for row in stable_rows(rows):
        key = (
            priority.get(str(row["failure_class"]), len(priority)),
            str(row["failure_class"]),
            str(row["question_type"]),
        )
        strata[key].append(row)
    selected: list[dict[str, Any]] = []
    ordered_keys = sorted(strata)
    while len(selected) < limit and ordered_keys:
        next_keys: list[tuple[int, str, str]] = []
        for key in ordered_keys:
            if strata[key] and len(selected) < limit:
                selected.append(strata[key].pop(0))
            if strata[key]:
                next_keys.append(key)
        ordered_keys = next_keys
    return selected


def stable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: sha256_text(f"diagnostic-v1:{row['question_id']}"))


def slice_case(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "question_id": row["question_id"],
        "domain": row["domain"],
        "question_type": row["question_type"],
        "baseline_failure_class": row["failure_class"],
        "baseline_score_bool": row["baseline_score_bool"],
    }


def build_diagnostic_report(
    trace_rows: list[dict[str, Any]],
    *,
    source_artifacts: dict[str, dict[str, Any]],
    max_rank: int,
    slice_record: dict[str, Any],
) -> dict[str, Any]:
    eligible = [
        row for row in trace_rows if row["exact_evidence_eligible"] and row["exact_evidence_states"]
    ]
    multi_state = [
        row
        for row in eligible
        if row["metrics"]["evidence_state_count"] >= MULTI_STATE_MIN_EVIDENCE_STATES
    ]
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "max_rank": max_rank,
        "source_artifacts": source_artifacts,
        "slice_sha256": sha256_json(slice_record),
        "question_count": len(trace_rows),
        "exact_evidence_eligible_count": len(eligible),
        "multi_state_evidence_count": len(multi_state),
        "metrics": summarize_metrics(eligible, multi_state=multi_state),
        "by_domain": {
            domain: summarize_metrics(
                [row for row in eligible if row["domain"] == domain],
                multi_state=[row for row in multi_state if row["domain"] == domain],
            )
            for domain in sorted({str(row["domain"]) for row in trace_rows})
        },
        "failure_classes": count_values(trace_rows, "failure_class"),
        "question_types": count_values(trace_rows, "question_type"),
    }


def summarize_metrics(
    rows: list[dict[str, Any]],
    *,
    multi_state: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "eligible_count": len(rows),
        "trajectory_recall_at_10": mean_bool(rows, "trajectory_recall_at_k"),
        "state_recall_at_10": mean_bool(rows, "state_recall_at_k"),
        "exact_context_recall_at_10": mean_bool(rows, "exact_context_recall_at_k"),
        "multi_state_eligible_count": len(multi_state),
        "multi_state_evidence_coverage_at_10": mean_number(
            multi_state,
            "multi_state_evidence_coverage_at_k",
        ),
    }


def mean_bool(rows: list[dict[str, Any]], metric: str) -> float | None:
    if not rows:
        return None
    return sum(bool(row["metrics"][metric]) for row in rows) / len(rows)


def mean_number(rows: list[dict[str, Any]], metric: str) -> float | None:
    values = [
        number for row in rows if (number := finite_number(row["metrics"].get(metric))) is not None
    ]
    return sum(values) / len(values) if values else None


def count_values(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get(field) or "unknown")] += 1
    return dict(sorted(counts.items()))


def is_exact_evidence_eligible(normalized_answer: str) -> bool:
    return (
        len(normalized_answer) >= MIN_EXACT_ANSWER_LENGTH
        and normalized_answer not in _GENERIC_ANSWERS
    )


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized)
    return " ".join(normalized.split())


def regex_string(pattern: re.Pattern[str], value: str) -> str | None:
    match = pattern.search(value)
    return match.group(1) if match else None


def regex_int(pattern: re.Pattern[str], value: str) -> int | None:
    matched = regex_string(pattern, value)
    return int(matched) if matched is not None else None


def regex_float(pattern: re.Pattern[str], value: str) -> float | None:
    matched = regex_string(pattern, value)
    try:
        return finite_number(float(matched)) if matched is not None else None
    except ValueError:
        return None


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def nested_value(mapping: Any, *path: str) -> Any:
    current = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def nested_dict(mapping: Any, *path: str) -> dict[str, Any]:
    value = nested_value(mapping, *path)
    return dict(value) if isinstance(value, dict) else {}


def load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        msg = f"Expected JSON object in {path}"
        raise TypeError(msg)
    return loaded


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        loaded = json.loads(line)
        if isinstance(loaded, dict):
            rows.append(loaded)
    return rows


def load_jsonl_by_id(
    path: Path,
    *,
    required_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            if required_ids is not None:
                prefix_match = _JSONL_ID_PREFIX_PATTERN.match(line)
                if prefix_match and prefix_match.group(1) not in required_ids:
                    continue
            row = json.loads(line)
            if not isinstance(row, dict) or "id" not in row:
                continue
            row_id = str(row["id"])
            if required_ids is None or row_id in required_ids:
                rows[row_id] = row
            if required_ids is not None and rows.keys() >= required_ids:
                break
    if required_ids is not None and (missing := required_ids - rows.keys()):
        msg = f"Missing required ids in {path}: {sorted(missing)}"
        raise ValueError(msg)
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return sha256_text(encoded)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
