from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[4]


def _load_script(relative_path: str) -> ModuleType:
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_longmemeval_report_uses_graph_embedding_runtime(monkeypatch) -> None:
    module = _load_script("benchmarks/longmemeval_live.py")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", "1024")
    monkeypatch.setenv("SIBYL_OPENAI_API_KEY", "test-key")

    metadata = module._graph_embedding_runtime_metadata()

    assert metadata["embedding_provider"] == "openai"
    assert metadata["embedding_model"] == "text-embedding-3-small"
    assert metadata["embedding_dimensions"] == 1024
    assert metadata["embedding_provider_status"] == "enabled"
    assert "native vector" in metadata["retrieval_semantics"]
    assert metadata["vector_search_surface"] == (
        "entity.name_embedding KNN via NativeEntityManager.search"
    )


def test_longmemeval_preflight_detects_vector_search_surface() -> None:
    module = _load_script("benchmarks/preflight/longmemeval_live_contract_probe.py")

    semantics = module._source_semantics()

    graph_function = semantics["api_search_graph_function"]
    assert graph_function["uses_fulltext_scores"] is True
    assert graph_function["uses_knn_vector"] is True
    assert graph_function["uses_embedding_provider"] is True
