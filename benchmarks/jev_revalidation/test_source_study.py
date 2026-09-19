"""Toy evidence verifies typed dispositions and complete study joins."""

# Explicit numeric outcomes make the toy experiment assertions readable.
# ruff: noqa: PLR2004

import hashlib
import json
from pathlib import Path

import pytest

from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionRoute

from .runner import select_prompts
from .source_study import analyze, metrics, prepare, typed_disposition


@pytest.fixture
def toy():
    return {
        "id": "toy",
        "category": "toy",
        "memory": "The service uses port 1234.",
        "event": "The service now uses port 5678.",
        "source_text": "The service now uses port 5678.",
        "memory_effective_at": "2026-01-01T00:00:00Z",
        "event_effective_at": "2026-02-01T00:00:00Z",
        "source_authority": "authoritative",
        "expected_relation": "superseded",
        "expected_action": "retire",
        "expected_source_relation": "superseded",
        "expected_disposition": "retire",
        "rationale": "Toy permanent configuration change.",
        "claim_kind": "current_state",
        "event_extent": "permanent",
        "as_of": "2026-02-02T00:00:00Z",
        "event_valid_until": None,
    }


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "retire"),
        ({"claim_kind": "historical"}, "review"),
        ({"claim_kind": "standing_rule"}, "retire"),
        ({"claim_kind": "unknown"}, "review"),
        ({"as_of": None}, "review"),
        ({"as_of": "2026-02-02"}, "review"),
        ({"as_of": "2026-01-15T00:00:00Z"}, "retain"),
        ({"event_extent": "unknown"}, "review"),
        ({"event_extent": "temporary"}, "review"),
        ({"event_extent": "temporary", "event_valid_until": "2026-02-01T00:00:00Z"}, "review"),
        ({"event_extent": "temporary", "event_valid_until": "2026-01-01T00:00:00Z"}, "review"),
        ({"event_extent": "temporary", "event_valid_until": "2026-02-02T00:00:00Z"}, "retain"),
        ({"event_extent": "temporary", "event_valid_until": "2026-02-03T00:00:00Z"}, "overlay"),
        (
            {
                "event_extent": "temporary",
                "event_valid_until": "2026-02-03T00:00:00Z",
                "claim_kind": "standing_rule",
            },
            "retain",
        ),
        ({"source_authority": "reported"}, "review"),
        ({"event_effective_at": "2025-01-01T00:00:00Z"}, "retain"),
    ],
)
def test_typed_disposition_boundaries(toy, changes, expected):
    assert typed_disposition("superseded", {**toy, **changes}) == expected


def test_non_conflicts_do_not_gain_destructive_authority(toy):
    assert typed_disposition("supported", toy) == "retain"
    assert typed_disposition("non_evidence", toy) == "retain"
    assert typed_disposition("uncertain", toy) == "review"


def _write(path, value):
    path.write_text(json.dumps(value))


def _fixture_run(path, projection_path, *, version, arm, relation, status="completed", repeats=1):
    cases = json.loads(projection_path.read_text())
    path.mkdir()
    manifest = {
        "cases_sha256": hashlib.sha256(projection_path.read_bytes()).hexdigest(),
        "prompt_version": version,
        "prompts_sha256": hashlib.sha256(
            Path(str(select_prompts(version).__file__)).read_bytes()
        ).hexdigest(),
        "prompt_dependencies_sha256": {
            "prompts.py": hashlib.sha256(
                Path(str(select_prompts("v1").__file__)).read_bytes()
            ).hexdigest()
        },
        "route_policy_sha256": OpenRouterDecisionRoute().policy_sha256,
        "arms": [arm],
        "repeats": repeats,
        "case_count": len(cases),
        "batch_size": 1,
    }
    _write(path / "manifest.json", manifest)
    rows = []
    for case in cases:
        base = {
            "case_id": case["id"],
            "category": case["category"],
            "expected_relation": case["expected_relation"],
            "expected_action": case["expected_action"],
        }
        rows.append(
            {
                **base,
                "arm": "baseline",
                "repeat": 0,
                "status": "completed",
                "relation": "uncertain",
                "action": "retain",
                "proposal": "no_signal",
            }
        )
        for repeat in range(repeats):
            rows.append(
                {
                    **base,
                    "arm": arm,
                    "repeat": repeat,
                    "status": status,
                    "relation": relation if status == "completed" else None,
                    "action": "retire" if status == "completed" else None,
                }
            )
    _write(path / "predictions.json", rows)
    _write(
        path / "calls.json",
        [
            {
                "arm": arm,
                "repeat": repeat,
                "batch": f"{arm}-{repeat}-{offset}",
                "attempt_count": 1,
                "observed_cost_usd": 0.001,
                "input_tokens": 10,
                "output_tokens": 2,
            }
            for repeat in range(repeats)
            for offset in range(len(cases))
        ],
    )


def _prepare_pair(tmp_path, toy, *, source_relation="superseded", source_status="completed"):
    master = tmp_path / "master.json"
    _write(master, [toy])
    prepared = tmp_path / "prepared"
    prepare(master, prepared)
    summary = tmp_path / "summary-run"
    source = tmp_path / "source-run"
    _fixture_run(
        summary, prepared / "summary.json", version="v2", arm="decomposed", relation="superseded"
    )
    _fixture_run(
        source,
        prepared / "source.json",
        version="v3",
        arm="direct",
        relation=source_relation,
        status=source_status,
    )
    return master, summary, source


def test_prepare_preserves_master_and_source_projection(toy, tmp_path):
    master = tmp_path / "master.json"
    _write(master, [toy])
    out = tmp_path / "prepared"
    original = master.read_bytes()
    manifest = prepare(master, out)
    assert master.read_bytes() == original
    assert json.loads((out / "summary.json").read_text()) == [toy]
    source = json.loads((out / "source.json").read_text())[0]
    assert source["event"] == toy["source_text"]
    assert source["expected_relation"] == toy["expected_source_relation"]
    assert source["id"] == toy["id"]
    assert manifest["case_count"] == 1
    with pytest.raises(FileExistsError):
        prepare(master, out)


def test_source_witness_blocks_unsupported_retirement(toy, tmp_path):
    case = {**toy, "expected_source_relation": "supported", "expected_disposition": "retain"}
    master, summary, source = _prepare_pair(tmp_path, case, source_relation="supported")
    result = analyze(master, summary, source, tmp_path / "analysis")
    assert result["methods"]["summary_legacy"]["pooled"]["dangerous_retirement_count"] == 1
    assert result["methods"]["source_typed"]["pooled"]["accuracy"] == 1.0
    gated = result["methods"]["gated_typed"]["pooled"]
    assert gated["dangerous_retirement_count"] == 0
    assert gated["review_count"] == 1
    assert len(result["disagreements"]) == 1
    assert result["accounting"]["summary"]["observed_cost_usd"] == 0.001


def test_source_failure_remains_in_denominator(toy, tmp_path):
    master, summary, source = _prepare_pair(tmp_path, toy, source_status="unavailable")
    result = analyze(master, summary, source, tmp_path / "analysis")
    for method in ("source_typed", "gated_typed"):
        score = result["methods"][method]["pooled"]
        assert score["cases"] == 1
        assert score["measurement_failures"] == 1
        assert score["accuracy"] == 0.0
        assert score["review_count"] == 0
        assert score["retire_recall"] == 0.0


@pytest.mark.parametrize(
    "corruption",
    ["missing", "duplicate", "wrong_id", "wrong_repeat", "wrong_gold", "failed_with_action"],
)
def test_join_rejects_incomplete_or_ambiguous_predictions(toy, tmp_path, corruption):
    master, summary, source = _prepare_pair(tmp_path, toy)
    path = source / "predictions.json"
    rows = json.loads(path.read_text())
    if corruption == "missing":
        rows.pop()
    elif corruption == "duplicate":
        rows.append(dict(rows[-1]))
    elif corruption == "wrong_id":
        rows[-1]["case_id"] = "other"
    elif corruption == "wrong_repeat":
        rows[-1]["repeat"] = True
    elif corruption == "wrong_gold":
        rows[-1]["expected_action"] = "review"
    else:
        rows[-1]["status"] = "unavailable"
    _write(path, rows)
    with pytest.raises(ValueError, match=r"comparison|prediction"):
        analyze(master, summary, source, tmp_path / "analysis")
    assert not (tmp_path / "analysis").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("cases_sha256", "wrong"), ("prompt_version", "v2"), ("arms", ["decomposed"]), ("repeats", 2)],
)
def test_join_rejects_wrong_run_manifest(toy, tmp_path, field, value):
    master, summary, source = _prepare_pair(tmp_path, toy)
    path = source / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    _write(path, manifest)
    with pytest.raises(ValueError, match=r"corpus|prompt|comparison|repeat"):
        analyze(master, summary, source, tmp_path / "analysis")


def test_metrics_do_not_reward_failure_as_review():
    rows = [{"action": None, "expected_disposition": "review", "status": "measurement_failure"}]
    result = metrics(rows)
    assert result["accuracy"] == 0.0
    assert result["review_rate"] == 0.0
    assert result["measurement_failures"] == 1


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "negative_cost", "boolean_tokens"])
def test_accounting_rejects_incomplete_or_invalid_calls(toy, tmp_path, corruption):
    master, summary, source = _prepare_pair(tmp_path, toy)
    path = source / "calls.json"
    calls = json.loads(path.read_text())
    if corruption == "missing":
        calls.clear()
    elif corruption == "duplicate":
        calls.append(dict(calls[0]))
    elif corruption == "negative_cost":
        calls[0]["observed_cost_usd"] = -1.0
    else:
        calls[0]["input_tokens"] = True
    _write(path, calls)
    with pytest.raises(ValueError, match=r"accounting|original call"):
        analyze(master, summary, source, tmp_path / "analysis")


def test_gated_retention_does_not_require_source_inference(toy, tmp_path):
    case = {**toy, "expected_source_relation": "supported", "expected_disposition": "retain"}
    master, summary, source = _prepare_pair(tmp_path, case, source_status="unavailable")
    path = summary / "predictions.json"
    rows = json.loads(path.read_text())
    rows[-1].update(relation="supported", action="retain")
    _write(path, rows)
    result = analyze(master, summary, source, tmp_path / "analysis")
    score = result["methods"]["gated_typed"]["pooled"]
    assert score["accuracy"] == 1.0
    assert score["measurement_failures"] == 0
    assert result["methods"]["source_typed"]["pooled"]["measurement_failures"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompts_sha256", "f" * 64),
        ("prompt_dependencies_sha256", {"prompts.py": "f" * 64}),
        ("prompt_dependencies_sha256", {}),
        ("route_policy_sha256", "f" * 64),
        ("route_policy_sha256", None),
    ],
)
def test_analyze_rejects_changed_program_dependencies_or_route(toy, tmp_path, field, value):
    master, summary, source = _prepare_pair(tmp_path, toy)
    path = source / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    _write(path, manifest)
    with pytest.raises(ValueError, match=r"prompt|route policy"):
        analyze(master, summary, source, tmp_path / "analysis")
    assert not (tmp_path / "analysis").exists()
