from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

EXPECTED_CONTENT_MAX_CHARS = 50_000


def _load_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_repair_project.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_repair_project", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_trajectories_rejects_duplicate_ids(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "trajectories.jsonl"
    path.write_text('{"id":"t1"}\n{"id":"t1"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate trajectory id"):
        module.load_trajectories(path, expected_ids={"t1"})


def test_repair_main_writes_bound_apply_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    trajectories = tmp_path / "trajectories.jsonl"
    trajectories.write_text('{"id":"t1"}\n', encoding="utf-8")
    trajectory_ids = tmp_path / "trajectory-ids.json"
    trajectory_ids.write_text('["t1"]\n', encoding="utf-8")
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    captured: dict[str, object] = {}

    class FakeMemory:
        def __init__(self) -> None:
            self.api_runtime = {"status": "healthy"}
            self._client = SimpleNamespace(close=lambda: None)

        def repair_attached_project(self, *, apply: bool) -> dict[str, object]:
            captured["apply"] = apply
            return {"applied": apply, "created_entity_count": 1}

    def prepare_existing(
        memory_params: dict[str, object],
        *,
        expected_trajectory_ids: set[str],
        trajectories: list[dict[str, object]],
    ) -> FakeMemory:
        captured["memory_params"] = memory_params
        captured["expected_trajectory_ids"] = expected_trajectory_ids
        captured["trajectories"] = trajectories
        return FakeMemory()

    monkeypatch.setattr(module.SibylLiveApiMemory, "prepare_existing", prepare_existing)

    exit_code = module.main(
        [
            "--api-token-file",
            str(token),
            "--project-id",
            "project_test",
            "--run-id",
            "run_test",
            "--trajectories",
            str(trajectories),
            "--trajectory-ids-file",
            str(trajectory_ids),
            "--content-max-chars",
            str(EXPECTED_CONTENT_MAX_CHARS),
            "--chunking-mode",
            "trajectory",
            "--receipt",
            str(receipt),
            "--allow-localhost",
            "--apply",
        ]
    )

    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert captured["apply"] is True
    assert captured["expected_trajectory_ids"] == {"t1"}
    assert payload["apply_requested"] is True
    assert payload["content_max_chars"] == EXPECTED_CONTENT_MAX_CHARS
    assert payload["result"]["created_entity_count"] == 1
