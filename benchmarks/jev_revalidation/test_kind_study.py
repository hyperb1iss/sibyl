"""Toy-only checks for claim-kind isolation and complete paired measurements."""

# Numeric outcomes express explicit toy experiment expectations.
# ruff: noqa: PLR2004

import json

import pytest

from .kind_study import analyze, kind_projections, prepare
from .runner import prompt_dependencies, select_prompts
from .test_source_study import _fixture_run
from .test_source_study import toy as toy  # noqa: PLC0414 - explicitly re-export a pytest fixture


def _write(path, value):
    path.write_text(json.dumps(value))


def _runs(tmp_path, cases, *, unknown_status="completed"):
    master = tmp_path / "master.json"
    _write(master, cases)
    prepared = tmp_path / "prepared"
    prepare(master, prepared)
    paths = {}
    for name, version in (("v3", "v3"), ("unknown", "v4"), ("typed", "v4")):
        path = tmp_path / name
        _fixture_run(
            path,
            prepared / f"{name}.json",
            version=version,
            arm="direct",
            relation="supported" if name == "v3" else "superseded",
            status=unknown_status if name == "unknown" else "completed",
            repeats=2,
        )
        manifest_path = path / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["prompt_dependencies_sha256"] = prompt_dependencies(version)
        manifest["concurrency"] = 1
        _write(manifest_path, manifest)
        paths[name] = path
    return master, paths


def test_projections_change_only_model_kind(toy):
    original = dict(toy)
    projected = kind_projections([toy])
    base = projected["v3"][0]
    assert "model_claim_kind" not in base
    assert base["event"] == toy["source_text"]
    assert projected["unknown"][0] == {**base, "model_claim_kind": "unknown"}
    assert projected["typed"][0] == {**base, "model_claim_kind": "current_state"}
    assert projected["unknown"][0]["claim_kind"] == "current_state"
    assert toy == original


@pytest.mark.parametrize("change", [{"claim_kind": "invented"}, {"model_claim_kind": "historical"}])
def test_projection_rejects_uncontrolled_kind_input(toy, change):
    with pytest.raises(ValueError, match="claim_kind"):
        kind_projections([{**toy, **change}])


def test_v4_inputs_isolate_kind_and_never_include_policy_or_gold(toy):
    program = select_prompts("v4")
    projected = kind_projections([toy])
    case = projected["typed"][0]
    request = program.make_request([case], "direct", "toy")
    changed = {
        **case,
        "claim_kind": "historical",
        "expected_relation": "uncertain",
        "expected_action": "review",
        "expected_disposition": "review",
        "rationale": "GOLD-ONLY",
        "as_of": None,
        "source_authority": "reported",
    }
    assert request == program.make_request([changed], "direct", "toy")
    unknown = program.make_request(projected["unknown"], "direct", "toy")
    assert request.semantic_input_sha256 != unknown.semantic_input_sha256
    assert "GOLD-ONLY" not in request.state


def test_analyze_retains_failures_and_original_cost(toy, tmp_path):
    master, paths = _runs(tmp_path, [toy], unknown_status="unavailable")
    result = analyze(master, paths["v3"], paths["unknown"], paths["typed"], tmp_path / "analysis")
    unknown = result["methods"]["unknown"]["pooled"]
    assert unknown["cases"] == 2
    assert unknown["measurement_failures"] == 2
    assert unknown["accuracy"] == 0.0
    assert unknown["review_rate"] == 0.0
    assert result["methods"]["typed"]["pooled"]["accuracy"] == 1.0
    assert result["pairwise"]["unknown_to_typed"]["improvement_count"] == 2
    assert result["pairwise"]["unknown_to_typed"]["both_complete"] == 0
    assert result["accounting"]["unknown"]["calls"] == 2
    assert result["accounting"]["unknown"]["observed_cost_usd"] == 0.002
    assert len(result["errors"]["unknown"]) == 2


def test_primary_subsets_and_joint_pair_outcomes(toy, tmp_path):
    temporary = {
        **toy,
        "event_extent": "temporary",
        "event_valid_until": "2026-02-03T00:00:00Z",
        "expected_disposition": "overlay",
        "pair_group": "shared-scenario",
    }
    standing = {
        **temporary,
        "id": "standing",
        "claim_kind": "standing_rule",
        "expected_disposition": "retain",
    }
    master, paths = _runs(tmp_path, [temporary, standing])
    result = analyze(master, paths["v3"], paths["unknown"], paths["typed"], tmp_path / "analysis")
    scored = result["methods"]["typed"]
    assert (
        scored["subsets"]["current_state_temporary_gold_overlay"]["pooled"]["overlay_recall"] == 1.0
    )
    assert scored["subsets"]["standing_rule_temporary"]["pooled"]["retention_rate"] == 1.0
    assert scored["joint_group_correctness"]["0"]["groups"] == 1
    assert scored["joint_group_correctness"]["0"]["joint_accuracy"] == 1.0
    assert scored["joint_group_correctness"]["0"]["outcomes"]["shared-scenario"]["cases"] == 2
    assert result["methods"]["v3"]["joint_group_correctness"]["0"]["joint_accuracy"] == 0.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cases_sha256", "f" * 64),
        ("prompts_sha256", "f" * 64),
        ("prompt_dependencies_sha256", {}),
        ("prompt_version", "v3"),
        ("route_policy_sha256", "f" * 64),
        ("repeats", 1),
        ("concurrency", 2),
        ("batch_size", 2),
    ],
)
def test_analysis_rejects_unmatched_programs_or_controls(toy, tmp_path, field, value):
    master, paths = _runs(tmp_path, [toy])
    path = paths["typed"] / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    _write(path, manifest)
    with pytest.raises(ValueError, match=r"corpus|prompt|control|comparison"):
        analyze(master, paths["v3"], paths["unknown"], paths["typed"], tmp_path / "analysis")
    assert not (tmp_path / "analysis").exists()


@pytest.mark.parametrize(
    "corruption", ["missing_prediction", "duplicate_prediction", "missing_call", "duplicate_call"]
)
def test_analysis_rejects_incomplete_measurements(toy, tmp_path, corruption):
    master, paths = _runs(tmp_path, [toy])
    filename = "predictions.json" if "prediction" in corruption else "calls.json"
    path = paths["typed"] / filename
    rows = json.loads(path.read_text())
    if corruption.startswith("missing"):
        rows.pop()
    else:
        rows.append(dict(rows[-1]))
    _write(path, rows)
    with pytest.raises(ValueError, match=r"comparison|original call"):
        analyze(master, paths["v3"], paths["unknown"], paths["typed"], tmp_path / "analysis")


def test_prepare_refuses_reuse(toy, tmp_path):
    master = tmp_path / "master.json"
    _write(master, [toy])
    prepare(master, tmp_path / "prepared")
    with pytest.raises(FileExistsError):
        prepare(master, tmp_path / "prepared")
