from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_context_pack_eval_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "context_pack_eval.py"
    spec = importlib.util.spec_from_file_location("context_pack_eval", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_context_pack_eval_metadata_includes_git_provenance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_context_pack_eval_module()
    cases_file = tmp_path / "context_cases.json"
    cases_file.write_text("[]", encoding="utf-8")
    args = SimpleNamespace(
        auth_manifest=None,
        metadata=["sibyl_commit=wrong", "git_dirty=true", "git_status=dirty"],
    )
    monkeypatch.setattr(
        module,
        "git_provenance_metadata",
        lambda _root: {
            "sibyl_commit": "abc123",
            "git_dirty": "false",
            "git_status": "clean",
        },
    )

    metadata = module._benchmark_metadata(args=args, cases_file=cases_file)

    assert metadata["sibyl_commit"] == "abc123"
    assert metadata["git_dirty"] == "false"
    assert metadata["git_status"] == "clean"
