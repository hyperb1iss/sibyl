from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _load_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_live_smoke.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_live_smoke", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_trajectory_selects_exact_id(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "trajectories.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps({"id": "first", "states": []}),
                json.dumps({"id": "wanted", "states": [{"state_index": 0}]}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert module.load_trajectory(source, "wanted")["id"] == "wanted"


def test_load_trajectory_rejects_missing_id(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "trajectories.jsonl"
    source.write_text('{"id":"other"}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="not found"):
        module.load_trajectory(source, "missing")


def test_evaluate_smoke_report_requires_live_write_retrieval_and_replay() -> None:
    module = _load_module()
    report = {
        "api_runtime": {"status": "healthy"},
        "ingest": {
            "written_entities": 6,
            "pending_embedding_jobs": 0,
            "pending_projection_jobs": 0,
        },
        "query": {
            "context_items": 3,
            "selection_origins": ["context_pack:procedural", "search"],
        },
        "replay": {
            "written_entities": 0,
            "written_relationships": 0,
            "deleted_entities": 0,
            "deleted_relationships": 0,
            "background_jobs": {},
        },
    }

    assert all(module.evaluate_smoke_report(report).values())


def test_evaluate_smoke_report_rejects_missing_typed_evidence() -> None:
    module = _load_module()
    report = {
        "api_runtime": {"status": "healthy"},
        "ingest": {
            "written_entities": 6,
            "pending_embedding_jobs": 0,
            "pending_projection_jobs": 0,
        },
        "query": {"context_items": 1, "selection_origins": ["search"]},
        "replay": {
            "written_entities": 0,
            "written_relationships": 0,
            "deleted_entities": 0,
            "deleted_relationships": 0,
            "background_jobs": {},
        },
    }

    checks = module.evaluate_smoke_report(report)

    assert checks["typed_evidence_selected"] is False
    assert checks["raw_evidence_selected"] is True
