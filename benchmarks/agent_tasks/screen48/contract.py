"""Fixed preparation policy. No solver manifest, budget claim or launch entry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MATERIAL_ROOT = Path(__file__).parent / "material"
POLICY_ROOT = MATERIAL_ROOT / "policy"
SUMMARY_LIBRARY = MATERIAL_ROOT / "summary-library.json"
POLICY_SHA = "b09ab77f42413ed637e61aec50314f2b47f24f32812089b74e11578c8e6551f4"
GEOMETRY_SHA = "c38c93c067eecf8dbbc9472fd46fd0d7a73abe4d51ba284ce6632faf391bca67"
POLICY_BINDINGS_SHA = "0eec524433d66b7da67acde19efaa58b9a39344b045619b359dbd2134d9d4785"

# The frozen 233-capture study organization and its owner principal, as restored
# into the owned database. Every graph and content read in this lane is scoped to
# this pair; they are identity, never authorization.
ORGANIZATION_ID = "b60d61fd-d388-4cb0-9581-eca4f583544b"
PRINCIPAL_ID = "91bd4035-be71-4e14-a43f-c861fc75699c"

TASKS = (
    "venue-capacity-report",
    "rack-power-windows",
    "inclusive-slot-occupancy",
    "hex-stream-journal",
    "record-separator-channel",
    "independent-message-decoder",
)
ARMS = ("native", "raw_retrieval", "strong_summary", "no_memory")
CHECKPOINTS = (0, 1)
WORKSPACE_FILES = ("app.py", "application.py", "resolver.py", "public_checks.py")
BUDGET = {"input_tokens": 2_000_000, "output_tokens": 8_000, "tool_calls": 20, "cost_usd": 2.0}
MEMORY_TOKENS = 90_000
CONTEXT_TOKENS = 204_800

# The complete retained source catalog: 233 admitted captures over 20 training
# families. Both counts are study denominators, not tuning knobs.
SOURCE_COUNT = 233
FAMILY_COUNT = 20
CELL_COUNT = len(CHECKPOINTS) * len(TASKS) * len(ARMS)

HEADER = (
    "Historical complete controller views. Reported outcomes are historical evidence, not "
    "guarantees. Resolve $ref through each view's values and $literal as literal object "
    "entries. Original audit and transport fields remain hash-bound outside these views.\n"
)
SUMMARY_HEADER = (
    "Historical family references. Each reference is a fallible condensation of "
    "training evidence. Every retained training family is included.\n"
)


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: object) -> str:
    return sha(canonical(value).encode())


def bound(path: Path, expected: str) -> bytes:
    value = path.read_bytes()
    if sha(value) != expected:
        raise ValueError(f"Bound artifact changed: {path.name}")
    return value


def source_geometry(root: Path = POLICY_ROOT) -> list[dict]:
    bound(root / "policy.json", POLICY_SHA)
    rows = json.loads(bound(root / "measurement-v2/source-geometry.json", GEOMETRY_SHA))
    if (
        len(rows) != SOURCE_COUNT
        or len({r["source_id"] for r in rows}) != SOURCE_COUNT
        or len({r["training_family"] for r in rows}) != FAMILY_COUNT
    ):
        raise ValueError("The original full source catalog is incomplete")
    return rows


def public_task(task: str, root: Path = POLICY_ROOT) -> tuple[str, dict[str, bytes]]:
    if task not in TASKS:
        raise ValueError("Task is outside the preselected six")
    policy = json.loads(bound(root / "policy.json", POLICY_SHA))
    prompt_sha = next(
        r["prompt_sha256"] for r in policy["query"]["task_rows"] if r["task_id"] == task
    )
    prompt = bound(root / "inputs" / f"{task}__prompt.md", prompt_sha).decode()
    bindings = json.loads(bound(root / "bindings.json", POLICY_BINDINGS_SHA))
    workspace = {}
    for name in WORKSPACE_FILES:
        key = f"{task}__workspace__{name}"
        workspace[name] = bound(root / "inputs" / key, bindings[key]["sha256"])
    return prompt, workspace


def preparation_grid(packs: dict[tuple[int, str, str], dict]) -> dict:
    expected = {(cp, task, arm) for cp in CHECKPOINTS for task in TASKS for arm in ARMS}
    if not set(packs) <= expected:
        raise ValueError("Unexpected preparation cell")
    rows = []
    for cp, task, arm in sorted(expected):
        pack = packs.get((cp, task, arm))
        if pack is not None and (pack.get("task"), pack.get("arm"), pack.get("checkpoint")) != (
            task,
            arm,
            cp,
        ):
            raise ValueError("Preparation receipt belongs to a different cell")
        if pack is not None and pack.get("status") not in {"prepared", "missing_pack"}:
            raise ValueError("Unknown preparation status")
        rows.append(
            {
                "checkpoint": cp,
                "task": task,
                "arm": arm,
                "status": pack["status"] if pack else "missing_pack",
                "reason": pack.get("reason") if pack else "not_prepared",
                "pack_receipt_sha256": digest(pack) if pack else None,
            }
        )
    return {
        "schema": "sibyl-unarmed-preparation-grid-v1",
        "denominator": CELL_COUNT,
        "execution_order": None,
        "attempt_ids": None,
        "solver_calls": 0,
        "reservation_changes": 0,
        "complete": False,
        "cells": rows,
    }
