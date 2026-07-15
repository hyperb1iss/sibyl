from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _load_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_projection_audit.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_projection_audit", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_projection_audit_proves_source_support_and_replay(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "trajectories.jsonl"
    source.write_text(json.dumps(_trajectory()) + "\n", encoding="utf-8")

    report = module.audit_trajectories(source, content_max_chars=220)

    assert report["passed"] is True, report["issues"]
    assert report["counts"]["trajectories"] == 1
    assert report["counts"]["evidence_parts"] > 2
    assert report["counts"]["actions"] == 1
    assert report["relationship_types"]["DERIVED_FROM"] > 0
    assert report["bounds"]["max_evidence_part_chars"] <= 220
    assert report["bounds"]["max_total_write_amplification"] <= 2.0


def _trajectory() -> dict[str, object]:
    return {
        "id": "trajectory-1",
        "domain": "web",
        "environment": "test",
        "goal": "Update the deployment",
        "outcome": "success",
        "start_url": "https://example.test/start",
        "states": [
            {
                "state_index": 0,
                "step": 0,
                "url": "https://example.test/start",
                "action": None,
                "thought": "Open the deployment.",
                "accessibility_tree": "Root\n" + "Initial deployment state\n" * 20,
                "screenshot": None,
            },
            {
                "state_index": 1,
                "step": 1,
                "url": "https://example.test/done",
                "action": "click('Deploy')",
                "thought": "The deployment completed.",
                "accessibility_tree": "Root\n" + "Deployment complete\n" * 20,
                "screenshot": None,
            },
        ],
    }
