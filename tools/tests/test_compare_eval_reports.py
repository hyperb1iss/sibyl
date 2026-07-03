from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _load_compare_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "compare_eval_reports.py"
    spec = importlib.util.spec_from_file_location("compare_eval_reports", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compare_eval_reports_renders_accounting_columns(tmp_path: Path) -> None:
    module = _load_compare_module()
    report_path = tmp_path / "longmemeval.json"
    report_path.write_text(
        json.dumps(
            {
                "suite": "LongMemEval-S live API",
                "overall": {"recall@5": 0.95},
                "accounting": {
                    "schema_version": "sibyl-eval-accounting-v1",
                    "latency": {"p50_ms": 100.0, "p95_ms": 150.0},
                    "tokens": {"estimated_input_tokens": 1200.0},
                    "embedding": {"calls": 42},
                    "cost": {"estimated_total_usd": 0.000024},
                },
            }
        ),
        encoding="utf-8",
    )

    row = module.summarize_report(report_path)
    rendered = module.render_markdown([row])

    assert "accuracy" in rendered
    assert "p50 ms" in rendered
    assert "p95 ms" in rendered
    assert "token estimate" in rendered
    assert "embedding calls" in rendered
    assert "estimated cost" in rendered
    assert "recall@5=0.9500" in rendered
    assert "$0.000024" in rendered


def test_compare_eval_reports_fallback_uses_total_token_estimate(tmp_path: Path) -> None:
    module = _load_compare_module()
    expected_token_estimate = 750.0
    report_path = tmp_path / "context-pack.json"
    report_path.write_text(
        json.dumps(
            {
                "label": "retrieval-native",
                "metrics": {
                    "pass_rate": 1.0,
                    "cases": 3,
                    "avg_budgeted_estimated_tokens": 250.0,
                    "latency_p95_ms": 50.0,
                },
            }
        ),
        encoding="utf-8",
    )

    row = module.summarize_report(report_path)

    assert row["token_estimate"] == expected_token_estimate
