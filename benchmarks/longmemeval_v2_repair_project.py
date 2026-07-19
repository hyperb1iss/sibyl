#!/usr/bin/env python3
"""Audit or repair source chunks in an existing LongMemEval-V2 project."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

BENCHMARKS_ROOT = Path(__file__).resolve().parent
if str(BENCHMARKS_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS_ROOT))

from longmemeval_v2_memory.sibyl_memory import (  # noqa: E402
    SibylLiveApiMemory,
    load_api_credentials_file,
)

RECEIPT_SCHEMA_VERSION = "sibyl-longmemeval-v2-project-repair-v2"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    trajectories_path = Path(args.trajectories).expanduser().resolve()
    trajectory_ids_path = Path(args.trajectory_ids_file).expanduser().resolve()
    token_path = Path(args.api_token_file).expanduser().resolve()
    receipt_path = Path(args.receipt).expanduser().resolve()
    trajectory_ids = load_trajectory_ids(trajectory_ids_path)
    trajectories = load_trajectories(trajectories_path, expected_ids=set(trajectory_ids))
    credentials = load_api_credentials_file(token_path)

    memory = SibylLiveApiMemory.prepare_existing(
        {
            "api_url": args.api_url,
            **credentials,
            "allow_localhost": args.allow_localhost,
            "project_id": args.project_id,
            "run_id": args.run_id,
            "content_max_chars": args.content_max_chars,
            "chunking_mode": args.chunking_mode,
            "api_timeout_seconds": args.timeout_seconds,
            "defer_embeddings": True,
        },
        expected_trajectory_ids=set(trajectory_ids),
        trajectories=trajectories,
    )
    try:
        result = memory.repair_attached_project(apply=args.apply)
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "api_url": args.api_url,
            "api_runtime": memory.api_runtime,
            "project_id": args.project_id,
            "run_id": args.run_id,
            "trajectories_sha256": sha256_file(trajectories_path),
            "trajectory_ids_sha256": sha256_file(trajectory_ids_path),
            "trajectory_count": len(trajectories),
            "content_max_chars": args.content_max_chars,
            "chunking_mode": args.chunking_mode,
            "apply_requested": args.apply,
            "result": result,
        }
        write_json_atomic(receipt_path, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))  # noqa: T201
        return 0
    finally:
        memory._client.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:3434/api")
    parser.add_argument("--api-token-file", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--trajectories", required=True)
    parser.add_argument("--trajectory-ids-file", required=True)
    parser.add_argument("--content-max-chars", type=int, required=True)
    parser.add_argument("--chunking-mode", choices=("state", "trajectory"), required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-localhost", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    args = parser.parse_args(argv)
    if args.content_max_chars <= 0:
        parser.error("--content-max-chars must be positive")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    return args


def load_trajectory_ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("trajectory ids file must contain a non-empty JSON list")
    trajectory_ids = [str(value).strip() for value in payload]
    if any(not trajectory_id for trajectory_id in trajectory_ids):
        raise ValueError("trajectory ids must be non-empty strings")
    if len(trajectory_ids) != len(set(trajectory_ids)):
        raise ValueError("trajectory ids must be unique")
    return trajectory_ids


def load_trajectories(path: Path, *, expected_ids: set[str]) -> list[dict[str, Any]]:
    trajectories: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                trajectory = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on {path}:{line_number}") from exc
            trajectory_id = str(trajectory.get("id") or "") if isinstance(trajectory, dict) else ""
            if not trajectory_id:
                raise ValueError(f"Missing trajectory id on {path}:{line_number}")
            if trajectory_id not in expected_ids:
                continue
            if trajectory_id in seen:
                raise ValueError(
                    f"Duplicate trajectory id {trajectory_id!r} on {path}:{line_number}"
                )
            seen.add(trajectory_id)
            trajectories.append(trajectory)
    missing = sorted(expected_ids - seen)
    if missing:
        raise ValueError(f"Trajectories not found: {missing[:5]}")
    return trajectories


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
