"""Qualify authored fixtures without pretending they are agent outcomes."""

import json
import subprocess
import sys
from collections import Counter

import pytest
from benchmarks.agent_tasks.corpus import freeze, lineage_audit
from benchmarks.agent_tasks.corpus_families import families
from benchmarks.agent_tasks.corpus_manifest import compose
from benchmarks.agent_tasks.json_oracle import validate_oracle_inputs
from benchmarks.agent_tasks.manifest import (
    ControllerBudget,
    JsonOracleChecker,
    ManifestError,
    Task,
    load_manifest,
    read_artifact,
)


@pytest.mark.parametrize("seed", [0, 7])
@pytest.mark.parametrize("family_index", range(6))
def test_repair_discrimination(tmp_path, seed, family_index):
    family = families(seed)[family_index]

    def results(repairs, cases):
        for path, text in (family.workspace | repairs).items():
            (tmp_path / path).write_text(text)
        outcomes = []
        for case in cases:
            result = subprocess.run(
                [sys.executable, "-B", "app.py"],
                cwd=tmp_path,
                input=json.dumps(case["input"]),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            outcomes.append(
                result.returncode == 0 and json.loads(result.stdout) == case["expected"]
            )
        return outcomes

    assert not all(results({}, family.public_cases + family.private_cases)), family.id
    assert all(results(family.reference, family.public_cases + family.private_cases)), family.id
    assert all(results(family.partial, family.public_cases)), family.id
    assert not all(results(family.partial, family.private_cases)), family.id


def test_freeze_binds_existing_oracle_and_excludes_repairs(tmp_path):
    root = tmp_path / "frozen"
    catalog = json.loads(
        freeze(root, seed=0, image="sha256:" + "a" * 64, docker="/usr/bin/docker").read_bytes()
    )
    tasks = [Task.model_validate(item) for item in catalog["tasks"]]
    assert Counter(task.split for task in tasks) == {"learning": 4, "development": 2}
    assert catalog["experiences"] == []
    for task in tasks:
        assert isinstance(task.checker, JsonOracleChecker)
        checker = task.checker
        inputs = {
            artifact.path: read_artifact(root, artifact)
            for artifact in [checker.oracle, checker.runtime, checker.evaluator]
        }
        assert validate_oracle_inputs(checker, inputs).cases
        assert all(not item.artifact.path.startswith("private/") for item in task.workspace)
        assert all(
            "reference" not in item.destination and "partial" not in item.destination
            for item in task.workspace
        )
    with pytest.raises(FileExistsError):
        freeze(root, seed=0, image="sha256:" + "a" * 64, docker="/usr/bin/docker")


def test_cross_split_lineage_report():
    audit = lineage_audit(families(0))
    assert len(audit["pairs"]) == 4 * 2
    assert not any(pair["shared_lineage"] for pair in audit["pairs"])
    assert all(pair["files"] for pair in audit["pairs"])
    assert audit["experience_count"] == 0


def test_mechanism_dependency_survives_distinct_authored_lineages():
    audit = lineage_audit(families(0))
    shared = [pair for pair in audit["pairs"] if pair["shared_mechanism"]]
    assert len(shared) == 1
    assert not shared[0]["shared_lineage"]
    assert audit["independent_statistical_families"] is False
    assert audit["mechanism_cluster_count"] < audit["family_count"]


def test_runtime_composition_requires_transfer_and_binds_inputs(tmp_path):
    catalog = freeze(
        tmp_path / "catalog", seed=7, image="sha256:" + "a" * 64, docker="/usr/bin/docker"
    )
    lock = tmp_path / "uv.lock"
    lock.write_text("fixture dependency lock")
    kwargs = {
        "experiment_id": "corpus-test",
        "model": "qwen/qwen3-coder-next",
        "budget": ControllerBudget(
            input_tokens=10000, output_tokens=1000, tool_calls=10, cost_usd=0.1
        ),
        "dependency_lock": lock,
    }
    output = tmp_path / "run"
    with pytest.raises(ManifestError, match="shared mechanism"):
        compose(catalog, output, **kwargs)
    assert not output.exists()
    manifest_path = compose(catalog, output, allow_mechanism_transfer=True, **kwargs)
    manifest, inputs = load_manifest(manifest_path)
    assert manifest.purpose == "trusted_development"
    assert manifest.experiences == []
    assert inputs[manifest.arms[0].memory_pack.path] == b""
    assert manifest.controller_api_key_env == "OPENROUTER_API_KEY"
    assert len(manifest.tasks) == len(families(7))
    receipt = json.loads((output / "composition.json").read_bytes())
    assert receipt["shared_mechanism_clusters"] == ["newest-revision-active-projection"]
    assert receipt["sealed"] is False
    with pytest.raises(FileExistsError):
        compose(catalog, output, allow_mechanism_transfer=True, **kwargs)
    altered = json.loads(catalog.read_bytes())
    altered["tasks"][0]["split"] = "sealed"
    catalog.write_text(json.dumps(altered))
    with pytest.raises(ManifestError, match="learning/development"):
        compose(catalog, tmp_path / "sealed", allow_mechanism_transfer=True, **kwargs)
