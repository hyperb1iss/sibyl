#!/usr/bin/env python3
"""Replay frozen LongMemEval-V2 prompts through the official reader and judge."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.git_provenance import git_provenance  # noqa: E402
from benchmarks.longmemeval_v2_official import (  # noqa: E402
    ensure_fresh_provider_usage_output,
    ensure_official_harness,
    git_commit,
    install_evaluator_retry,
    install_provider_usage_tracking,
    install_reader_retry,
    sha256_question_ids,
)

SCHEMA_VERSION = "sibyl-longmemeval-v2-reader-replay-v1"
DATASET_MANIFEST_SHA256_BY_QUESTIONS_SHA256 = {
    "sha256:0a3ae5ebea938c24d7800e1e0b0828e08ae1646f939a53853b2b8cdc08e292b7": (
        "sha256:b17a18daa52873f915808502217c3c5fab39d20638544f986401155c9e8d67a6"
    ),
}
SOURCE_FILES = (
    "prompt_rows.jsonl",
    "per_question.jsonl",
    "aggregated_metrics.json",
    "run_args.json",
    "longmemeval_v2_official_receipt.json",
)
SOURCE_RECEIPT_BINDINGS = {
    "per_question": "per_question.jsonl",
    "aggregated_metrics": "aggregated_metrics.json",
    "run_args": "run_args.json",
}
REQUIRED_PROMPT_FIELDS = frozenset(
    {
        "answer_gold",
        "category",
        "eval_function",
        "eval_name",
        "is_abstention_problem",
        "messages",
        "question_id",
        "question_item",
        "question_type",
    }
)
READER_INPUT_FIELDS = frozenset({"question_id", "messages"})
CHECKSUM_FIELD_COUNT = 2
SHA256_HEX_LENGTH = 64


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay hash-bound LongMemEval-V2 prompt artifacts without retrieval."
    )
    parser.add_argument("--source-run-dir", required=True)
    parser.add_argument("--question-set-from-run", default=None)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--official-repo", required=True)
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--reader-api-key-env", default=None)
    parser.add_argument("--reader-api-key-file", default=None)
    parser.add_argument("--evaluator-api-key-env", default=None)
    parser.add_argument("--evaluator-api-key-file", default=None)
    parser.add_argument("--reader-max-concurrent-requests", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--evaluator-timeout-seconds", type=float, default=None)
    parser.add_argument("--question-order-seed", type=int, default=None)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--provider-usage-run-id", default=None)
    parser.add_argument("--reader-retry-attempts", type=int, default=4)
    parser.add_argument("--reader-retry-base-delay-seconds", type=float, default=2.0)
    parser.add_argument("--reader-retry-max-delay-seconds", type=float, default=30.0)
    parser.add_argument("--evaluator-retry-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    for name in (
        "reader_max_concurrent_requests",
        "reader_retry_attempts",
        "evaluator_retry_attempts",
    ):
        value = getattr(args, name)
        if value is not None and value < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for name in (
        "timeout_seconds",
        "evaluator_timeout_seconds",
        "reader_retry_base_delay_seconds",
        "reader_retry_max_delay_seconds",
    ):
        value = getattr(args, name)
        if value is not None and value < 0:
            parser.error(f"--{name.replace('_', '-')} must be non-negative")
    if args.provider_usage_run_id is None:
        args.provider_usage_run_id = f"lme-v2-reader-replay-{uuid4().hex[:12]}"
    return args


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"Expected JSON object at {path}:{line_number}")
            rows.append(value)
    return rows


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def source_artifacts(source_dir: Path) -> dict[str, dict[str, Any]]:
    artifacts = {}
    for name in SOURCE_FILES:
        path = source_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"Missing source artifact: {path}")
        artifacts[name] = {
            "path": str(path),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    return artifacts


def load_source_run(source_dir: Path) -> dict[str, Any]:
    artifacts = source_artifacts(source_dir)
    receipt = load_json(source_dir / "longmemeval_v2_official_receipt.json")
    validate_source_artifact_bindings(artifacts, receipt)
    prompt_rows = load_jsonl(source_dir / "prompt_rows.jsonl")
    score_rows = load_jsonl(source_dir / "per_question.jsonl")
    reader_rows = validate_prompt_rows(
        prompt_rows,
        source_per_question_rows=score_rows,
        source_receipt=receipt,
    )
    return {
        "run_dir": source_dir,
        "artifacts": artifacts,
        "receipt": receipt,
        "run_args": load_json(source_dir / "run_args.json"),
        "prompt_rows": prompt_rows,
        "score_rows": score_rows,
        "reader_rows": reader_rows,
    }


def validate_source_artifact_bindings(
    artifacts: dict[str, dict[str, Any]],
    source_receipt: dict[str, Any],
) -> None:
    receipt_artifacts = source_receipt.get("artifacts")
    if not isinstance(receipt_artifacts, dict):
        raise TypeError("Source receipt does not bind official run artifacts")
    for receipt_key, filename in SOURCE_RECEIPT_BINDINGS.items():
        expected = receipt_artifacts.get(receipt_key, {}).get("sha256")
        actual = artifacts[filename]["sha256"]
        if not isinstance(expected, str) or expected != actual:
            raise ValueError(f"Source artifact does not match its receipt: {filename}")


def validate_prompt_rows(
    prompt_rows: list[dict[str, Any]],
    *,
    source_per_question_rows: list[dict[str, Any]],
    source_receipt: dict[str, Any],
) -> list[dict[str, Any]]:
    if not prompt_rows:
        raise ValueError("Source prompt rows are empty")
    question_ids = []
    reader_rows = []
    for index, row in enumerate(prompt_rows):
        missing = REQUIRED_PROMPT_FIELDS - row.keys()
        if missing:
            raise ValueError(f"Prompt row {index} is missing fields: {sorted(missing)}")
        question_id = row["question_id"]
        messages = row["messages"]
        if not isinstance(question_id, str) or not question_id:
            raise ValueError(f"Prompt row {index} has an invalid question_id")
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"Prompt row {index} has invalid messages")
        question_ids.append(question_id)
        reader_row = {"question_id": question_id, "messages": messages}
        if frozenset(reader_row) != READER_INPUT_FIELDS:
            raise AssertionError("Reader projection changed its exact field set")
        reader_rows.append(reader_row)
    if len(question_ids) != len(set(question_ids)):
        raise ValueError("Source prompt rows contain duplicate question IDs")

    source_score_ids = [row.get("question_id") for row in source_per_question_rows]
    if len(source_score_ids) != len(set(source_score_ids)):
        raise ValueError("Source per-question rows contain duplicate question IDs")
    if set(question_ids) != set(source_score_ids):
        raise ValueError("Prompt rows do not match source per-question question IDs")
    validate_prompt_score_bindings(prompt_rows, source_per_question_rows)

    expected_identity = source_receipt.get("dataset", {}).get("selected_question_ids_sha256")
    actual_identity = sha256_question_ids(question_ids)
    if not isinstance(expected_identity, str) or actual_identity != expected_identity:
        raise ValueError("Prompt question identity does not match the source receipt")
    return reader_rows


def validate_prompt_score_bindings(
    prompt_rows: list[dict[str, Any]],
    source_per_question_rows: list[dict[str, Any]],
) -> None:
    scores_by_question_id = {row["question_id"]: row for row in source_per_question_rows}
    frozen_fields = (
        "answer_gold",
        "category",
        "eval_function",
        "is_abstention_problem",
        "prompt_messages",
        "question_image",
        "question_text",
        "question_type",
    )
    for row in prompt_rows:
        if normalize_loaded_images(row.get("messages")) != normalize_loaded_images(
            row.get("prompt_messages")
        ):
            raise ValueError(f"Prompt messages changed shape for {row['question_id']}")
        source_score = scores_by_question_id[row["question_id"]]
        if any(row.get(field) != source_score.get(field) for field in frozen_fields):
            raise ValueError(f"Prompt row does not match source scoring row: {row['question_id']}")


def normalize_loaded_images(value: Any) -> Any:
    if isinstance(value, list):
        return [normalize_loaded_images(item) for item in value]
    if not isinstance(value, dict):
        return value
    item_type = value.get("type")
    if item_type == "image_path":
        if not isinstance(value.get("image_path"), str) or not value["image_path"]:
            raise ValueError("Frozen prompt contains an invalid image path")
        return {"type": "frozen_image"}
    if item_type == "image_url":
        image_url = value.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else None
        if not isinstance(url, str) or not url.startswith("data:image/") or ";base64," not in url:
            raise ValueError("Reader prompt contains an invalid embedded image")
        base64.b64decode(url.split(",", 1)[1], validate=True)
        return {"type": "frozen_image"}
    return {key: normalize_loaded_images(item) for key, item in value.items()}


def parse_checksum_manifest(path: Path) -> dict[str, str]:
    checksums = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != CHECKSUM_FIELD_COUNT or len(parts[0]) != SHA256_HEX_LENGTH:
            raise ValueError(f"Invalid dataset checksum at {path}:{line_number}")
        digest, relative_path = parts
        checksums[relative_path.strip()] = f"sha256:{digest}"
    return checksums


def collect_message_items(value: Any, item_type: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for nested in value for item in collect_message_items(nested, item_type)]
    if not isinstance(value, dict):
        return []
    items = [value] if value.get("type") == item_type else []
    for nested in value.values():
        items.extend(collect_message_items(nested, item_type))
    return items


def load_pinned_image_dataset(
    data_root: Path,
    source_receipt: dict[str, Any],
) -> dict[str, Any]:
    questions_path = data_root / "questions.jsonl"
    manifest_path = data_root / "checksums.sha256"
    if not questions_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("Dataset root must contain questions.jsonl and checksums.sha256")

    expected_questions_sha = source_receipt.get("dataset", {}).get("questions_sha256")
    if (
        not isinstance(expected_questions_sha, str)
        or sha256_file(questions_path) != expected_questions_sha
    ):
        raise ValueError("Dataset questions do not match the source receipt")
    expected_manifest_sha = DATASET_MANIFEST_SHA256_BY_QUESTIONS_SHA256.get(expected_questions_sha)
    if expected_manifest_sha is None or sha256_file(manifest_path) != expected_manifest_sha:
        raise ValueError("Dataset checksum manifest is not the pinned official manifest")
    return {
        "root": data_root.resolve(),
        "questions_sha256": expected_questions_sha,
        "manifest_sha256": expected_manifest_sha,
        "checksums": parse_checksum_manifest(manifest_path),
        "questions": {row["id"]: row for row in load_jsonl(questions_path)},
    }


def validate_prompt_image(row: dict[str, Any], dataset: dict[str, Any]) -> dict[str, Any]:
    question_id = row["question_id"]
    question = dataset["questions"].get(question_id)
    relative_path = question.get("image") if isinstance(question, dict) else None
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError(f"Dataset question does not bind an image: {question_id}")
    if not Path(str(row["question_image"])).as_posix().endswith(f"/{relative_path}"):
        raise ValueError(f"Prompt image path does not match the dataset: {question_id}")

    root = dataset["root"]
    image_path = (root / relative_path).resolve()
    if root not in image_path.parents or not image_path.is_file():
        raise ValueError(f"Dataset image path is invalid: {relative_path}")
    expected_image_sha = dataset["checksums"].get(relative_path)
    if expected_image_sha is None or sha256_file(image_path) != expected_image_sha:
        raise ValueError(f"Dataset image does not match its checksum: {question_id}")

    frozen_items = collect_message_items(row.get("prompt_messages"), "image_path")
    reader_items = collect_message_items(row.get("messages"), "image_url")
    if len(frozen_items) != 1 or len(reader_items) != 1:
        raise ValueError(f"Prompt must contain exactly one bound image: {question_id}")
    frozen_path = frozen_items[0].get("image_path")
    if not isinstance(frozen_path, str) or not Path(frozen_path).as_posix().endswith(
        f"/{relative_path}"
    ):
        raise ValueError(f"Frozen image path does not match the dataset: {question_id}")
    image_url = reader_items[0].get("image_url")
    url = image_url.get("url") if isinstance(image_url, dict) else None
    if not isinstance(url, str) or ";base64," not in url:
        raise ValueError(f"Reader image is not embedded as base64: {question_id}")
    embedded_bytes = base64.b64decode(url.split(",", 1)[1], validate=True)
    embedded_sha = f"sha256:{hashlib.sha256(embedded_bytes).hexdigest()}"
    if embedded_sha != expected_image_sha:
        raise ValueError(f"Embedded image bytes do not match the dataset: {question_id}")
    return {
        "question_id": question_id,
        "dataset_path": relative_path,
        "sha256": embedded_sha,
        "size_bytes": len(embedded_bytes),
    }


def validate_prompt_image_bytes(
    prompt_rows: list[dict[str, Any]],
    *,
    data_root: Path | None,
    source_receipt: dict[str, Any],
) -> dict[str, Any]:
    image_rows = [
        row
        for row in prompt_rows
        if row.get("question_image")
        or collect_message_items(row.get("prompt_messages"), "image_path")
        or collect_message_items(row.get("messages"), "image_url")
    ]
    if not image_rows:
        return {
            "embedded_image_count": 0,
            "verified_image_count": 0,
            "dataset_root": None,
            "records": [],
        }
    if data_root is None:
        raise ValueError("--data-root is required for prompts with embedded images")
    dataset = load_pinned_image_dataset(data_root, source_receipt)
    records = [validate_prompt_image(row, dataset) for row in image_rows]
    return {
        "embedded_image_count": len(image_rows),
        "verified_image_count": len(records),
        "dataset_root": str(dataset["root"]),
        "questions_sha256": dataset["questions_sha256"],
        "checksum_manifest_sha256": dataset["manifest_sha256"],
        "records": records,
    }


def build_harness_args(
    source_args: dict[str, Any],
    replay_args: argparse.Namespace,
) -> argparse.Namespace:
    frozen = {key: value for key, value in source_args.items() if key != "started_at_utc"}
    runtime_overrides = {
        "api_key_env": replay_args.reader_api_key_env,
        "api_key_file": replay_args.reader_api_key_file,
        "evaluator_api_key_env": replay_args.evaluator_api_key_env,
        "evaluator_api_key_file": replay_args.evaluator_api_key_file,
        "reader_max_concurrent_requests": replay_args.reader_max_concurrent_requests,
        "timeout_seconds": replay_args.timeout_seconds,
        "evaluator_timeout_seconds": replay_args.evaluator_timeout_seconds,
    }
    for key, value in runtime_overrides.items():
        if value is not None:
            frozen[key] = value
    frozen.update(
        {
            "provider_usage_run_id": replay_args.provider_usage_run_id,
            "reader_retry_attempts": replay_args.reader_retry_attempts,
            "reader_retry_base_delay_seconds": replay_args.reader_retry_base_delay_seconds,
            "reader_retry_max_delay_seconds": replay_args.reader_retry_max_delay_seconds,
            "evaluator_retry_attempts": replay_args.evaluator_retry_attempts,
        }
    )
    return argparse.Namespace(**frozen)


def verify_official_repo(official_repo: Path, source_receipt: dict[str, Any]) -> str:
    ensure_official_harness(official_repo)
    expected = source_receipt.get("official_repo", {}).get("commit")
    actual = git_commit(official_repo)
    if not isinstance(expected, str) or not expected:
        raise ValueError("Source receipt does not bind an official harness commit")
    if actual != expected:
        raise ValueError(
            f"Official harness commit mismatch: expected {expected}, found {actual or 'unknown'}"
        )
    return expected


def prepare_output_dir(output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"Replay output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    ensure_fresh_provider_usage_output(output_dir)


def reader_input_records(reader_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "question_id": row["question_id"],
            "sha256": canonical_sha256(row),
        }
        for row in reader_rows
    ]


def select_question_set(
    source: dict[str, Any],
    selection: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selection_ids = [row["question_id"] for row in selection["prompt_rows"]]
    prompt_rows_by_id = {row["question_id"]: row for row in source["prompt_rows"]}
    score_rows_by_id = {row["question_id"]: row for row in source["score_rows"]}
    missing = [
        question_id
        for question_id in selection_ids
        if question_id not in prompt_rows_by_id or question_id not in score_rows_by_id
    ]
    if missing:
        raise ValueError(f"Selected question IDs are missing from the source run: {missing[:5]}")
    prompt_rows = [prompt_rows_by_id[question_id] for question_id in selection_ids]
    reader_rows = [
        {"question_id": row["question_id"], "messages": row["messages"]} for row in prompt_rows
    ]
    return prompt_rows, reader_rows


def merge_and_score_rows(
    *,
    prompt_rows: list[dict[str, Any]],
    outputs_by_question_id: dict[str, dict[str, Any]],
    official_harness: Any,
    eval_config: dict[str, Any],
    output_path: Path,
) -> list[dict[str, Any]]:
    expected_ids = {row["question_id"] for row in prompt_rows}
    if set(outputs_by_question_id) != expected_ids:
        raise ValueError("Reader outputs do not cover the frozen prompt rows exactly")
    records = []
    with output_path.open("w", encoding="utf-8") as handle:
        for row in prompt_rows:
            output = outputs_by_question_id[row["question_id"]]
            response_raw = output.get("response_raw")
            if not isinstance(response_raw, str):
                raise TypeError(f"Reader output is invalid for {row['question_id']}")
            scored_row = {**row, **output}
            score_bool, _, _ = official_harness.score_prediction(scored_row, eval_config)
            record = {
                key: value
                for key, value in row.items()
                if key not in {"messages", "question_item", "eval_name"}
            }
            record.update(
                {
                    "response_raw": response_raw,
                    "response_parsed_boxed": output["response_parsed_boxed"],
                    "is_unknown": output["is_unknown"],
                    "score": 1.0 if score_bool else 0.0,
                    "score_bool": score_bool,
                    "usage": output["usage"],
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                }
            )
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
            handle.flush()
            records.append(record)
    return records


def load_usage_events(path: Path, *, run_id: str) -> tuple[list[dict[str, Any]], int]:
    events = []
    invalid_lines = 0
    if not path.is_file():
        return events, invalid_lines
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            if not isinstance(event, dict) or event.get("run_id") != run_id:
                invalid_lines += 1
                continue
            events.append(event)
    return events, invalid_lines


def summarize_usage(path: Path, *, run_id: str) -> dict[str, Any]:
    events, invalid_lines = load_usage_events(path, run_id=run_id)
    costs = [
        float(cost)
        for event in events
        if isinstance(cost := event.get("usage", {}).get("cost_usd"), int | float)
    ]
    return {
        "requests": len(events),
        "priced_requests": len(costs),
        "provider_reported_cost_usd": sum(costs),
        "cost_coverage_complete": bool(events) and len(costs) == len(events),
        "tracking_complete": invalid_lines == 0 and bool(events),
        "invalid_or_foreign_lines": invalid_lines,
        "requested_models": sorted(
            {
                model
                for event in events
                if isinstance(model := event.get("requested_model"), str) and model
            }
        ),
        "provider_models": sorted(
            {
                model
                for event in events
                if isinstance(model := event.get("provider_model"), str) and model
            }
        ),
    }


def usage_tracking_complete(
    reader_usage: dict[str, Any],
    judge_usage: dict[str, Any],
    *,
    reader_request_count: int,
    expected_judge_requests: int,
) -> tuple[bool, bool]:
    reader_complete = bool(
        reader_usage["tracking_complete"] and reader_usage["requests"] >= reader_request_count
    )
    judge_complete = bool(
        (expected_judge_requests == 0 and judge_usage["invalid_or_foreign_lines"] == 0)
        or (judge_usage["tracking_complete"] and judge_usage["requests"] >= expected_judge_requests)
    )
    return reader_complete, judge_complete


def replay(
    replay_args: argparse.Namespace,
    *,
    official_harness: Any,
    official_metrics: Any,
) -> dict[str, Any]:
    source_dir = Path(replay_args.source_run_dir).expanduser().resolve()
    output_dir = Path(replay_args.output_dir).expanduser().resolve()
    official_repo = Path(replay_args.official_repo).expanduser().resolve()
    source = load_source_run(source_dir)
    source_receipt = source["receipt"]
    source_args = source["run_args"]
    prompt_rows = source["prompt_rows"]
    reader_rows = source["reader_rows"]
    selection_record = None
    if replay_args.question_set_from_run:
        selection_dir = Path(replay_args.question_set_from_run).expanduser().resolve()
        selection = load_source_run(selection_dir)
        prompt_rows, reader_rows = select_question_set(source, selection)
        selection_record = {
            "run_dir": str(selection_dir),
            "prompt_rows_sha256": selection["artifacts"]["prompt_rows.jsonl"]["sha256"],
            "question_ids_sha256": sha256_question_ids(
                [row["question_id"] for row in selection["prompt_rows"]]
            ),
        }
    data_root = (
        Path(replay_args.data_root).expanduser().resolve() if replay_args.data_root else None
    )
    image_integrity = validate_prompt_image_bytes(
        prompt_rows,
        data_root=data_root,
        source_receipt=source_receipt,
    )
    official_commit = verify_official_repo(official_repo, source_receipt)
    harness_args = build_harness_args(source_args, replay_args)
    preflight = {
        "schema_version": SCHEMA_VERSION,
        "mode": "reader-replay-preflight",
        "status": "PASS",
        "source": {
            "run_dir": str(source_dir),
            "artifacts": source["artifacts"],
            "official_commit": official_commit,
        },
        "question_set_selection": selection_record,
        "reader_inputs": {
            "question_count": len(reader_rows),
            "question_ids_sha256": sha256_question_ids([row["question_id"] for row in reader_rows]),
            "image_integrity": image_integrity,
        },
        "claim_boundary": {
            "provider_calls_executed": False,
            "retrieval_executed": False,
        },
    }
    if replay_args.preflight_only:
        return preflight
    prepare_output_dir(output_dir)

    if replay_args.question_order_seed is not None:
        random.Random(replay_args.question_order_seed).shuffle(reader_rows)  # noqa: S311
    input_records = reader_input_records(reader_rows)
    write_json(
        output_dir / "run_args.json",
        {
            "schema_version": SCHEMA_VERSION,
            "mode": "reader-replay",
            "started_at_utc": datetime.now(UTC).isoformat(),
            "source_run_dir": str(source_dir),
            "question_set_from_run": replay_args.question_set_from_run,
            "official_repo": str(official_repo),
            "official_commit": official_commit,
            "provider_usage_run_id": replay_args.provider_usage_run_id,
            "question_order_seed": replay_args.question_order_seed,
            "frozen_harness_args": {
                key: value for key, value in vars(harness_args).items() if "key" not in key
            },
        },
    )

    install_provider_usage_tracking(
        official_harness,
        official_metrics,
        args=harness_args,
        output_dir=output_dir,
    )
    install_reader_retry(official_harness, args=harness_args)
    install_evaluator_retry(official_metrics, args=harness_args)
    outputs = asyncio.run(official_harness.generate_all_reader_outputs(harness_args, reader_rows))
    eval_config = official_harness.make_eval_config(harness_args)
    records = merge_and_score_rows(
        prompt_rows=prompt_rows,
        outputs_by_question_id=outputs,
        official_harness=official_harness,
        eval_config=eval_config,
        output_path=output_dir / "per_question.jsonl",
    )

    empty_response_ids = [row["question_id"] for row in records if not row["response_raw"].strip()]
    tokens = {
        "prompt_tokens": sum(int(row["usage"].get("prompt_tokens", 0)) for row in records),
        "completion_tokens": sum(int(row["usage"].get("completion_tokens", 0)) for row in records),
    }
    tokens["total_tokens"] = tokens["prompt_tokens"] + tokens["completion_tokens"]
    aggregated = official_harness.aggregate_metrics(records)
    aggregated["tokens"] = tokens
    aggregated["retrieval"] = {
        "executed": False,
        "frozen_prompt_source": str(source_dir / "prompt_rows.jsonl"),
    }
    write_json(output_dir / "aggregated_metrics.json", aggregated)

    reader_usage = summarize_usage(
        output_dir / "provider_usage" / "reader.jsonl",
        run_id=replay_args.provider_usage_run_id,
    )
    judge_usage = summarize_usage(
        output_dir / "provider_usage" / "judge.jsonl",
        run_id=replay_args.provider_usage_run_id,
    )
    expected_judge_requests = sum(
        row["eval_name"] in official_harness.LLM_EVAL_FUNCTIONS for row in prompt_rows
    )
    reader_tracking_complete, judge_tracking_complete = usage_tracking_complete(
        reader_usage,
        judge_usage,
        reader_request_count=len(records),
        expected_judge_requests=expected_judge_requests,
    )
    valid = not empty_response_ids and reader_tracking_complete and judge_tracking_complete
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "mode": "reader-replay",
        "status": "PASS" if valid else "INVALID",
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "source": {
            "run_dir": str(source_dir),
            "artifacts": source["artifacts"],
            "official_commit": official_commit,
        },
        "question_set_selection": selection_record,
        "reader_inputs": {
            "hash_algorithm": (
                "sha256(canonical-json({question_id,messages}); sort_keys=true; "
                "ensure_ascii=true; separators=(',',':'))"
            ),
            "question_count": len(reader_rows),
            "question_ids_sha256": sha256_question_ids([row["question_id"] for row in reader_rows]),
            "records": input_records,
            "image_integrity": image_integrity,
        },
        "outputs": {
            "per_question": {
                "path": str(output_dir / "per_question.jsonl"),
                "sha256": sha256_file(output_dir / "per_question.jsonl"),
            },
            "aggregated_metrics": {
                "path": str(output_dir / "aggregated_metrics.json"),
                "sha256": sha256_file(output_dir / "aggregated_metrics.json"),
            },
            "empty_response_question_ids": empty_response_ids,
        },
        "accounting": {
            "reader": reader_usage,
            "reader_tracking_complete": reader_tracking_complete,
            "judge": judge_usage,
            "expected_judge_requests": expected_judge_requests,
            "judge_tracking_complete": judge_tracking_complete,
            "provider_reported_cost_usd": (
                reader_usage["provider_reported_cost_usd"]
                + judge_usage["provider_reported_cost_usd"]
            ),
            "cost_coverage_complete": (
                reader_usage["cost_coverage_complete"]
                and (expected_judge_requests == 0 or judge_usage["cost_coverage_complete"])
            ),
        },
        "runner_provenance": git_provenance(ROOT),
        "claim_boundary": {
            "retrieval_executed": False,
            "memory_rebuilt": False,
            "reader_received_exact_fields": sorted(READER_INPUT_FIELDS),
            "text_inputs_bound_to_scored_artifact": True,
            "embedded_image_bytes_bound_to_pinned_dataset": (
                image_integrity["verified_image_count"] == image_integrity["embedded_image_count"]
            ),
            "replay_receipt_hashes_exact_reader_inputs": True,
            "outputs_reproducible_from_question_order_seed": False,
            "variance_includes_reader_and_llm_judge_sampling": True,
        },
    }
    write_json(output_dir / "longmemeval_v2_reader_replay_receipt.json", receipt)
    if not valid:
        raise RuntimeError("Reader replay is invalid; inspect its replay receipt")
    return receipt


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    official_repo = Path(args.official_repo).expanduser().resolve()
    source_dir = Path(args.source_run_dir).expanduser().resolve()
    source_artifact_records = source_artifacts(source_dir)
    source_receipt = load_json(source_dir / "longmemeval_v2_official_receipt.json")
    validate_source_artifact_bindings(source_artifact_records, source_receipt)
    verify_official_repo(official_repo, source_receipt)
    if args.preflight_only:
        receipt = replay(
            args,
            official_harness=None,
            official_metrics=None,
        )
        print(json.dumps(receipt, indent=2, sort_keys=True))  # noqa: T201
        return 0
    sys.path.insert(0, str(official_repo))
    import evaluation.harness as official_harness  # noqa: PLC0415
    import evaluation.qa_eval_metrics as official_metrics  # noqa: PLC0415

    receipt = replay(
        args,
        official_harness=official_harness,
        official_metrics=official_metrics,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
