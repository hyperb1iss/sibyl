from __future__ import annotations

import json
import re
from pathlib import Path

from tools.baselines.common import (
    read_jsonl,
    resolve_placeholders,
    resolve_pointer,
    validate_expectations,
    write_jsonl,
    write_manifest,
)

EXPECTED_ERROR_COUNT = 3
REPO_ROOT = Path(__file__).parents[2]


def _workflow_job(workflow: str, job_name: str) -> str:
    header = f"  {job_name}:"
    start = workflow.index(header)
    tail = workflow[start:]
    next_job = re.search(r"\n  [a-zA-Z0-9_-]+:\n", tail[len(header) :])
    if next_job is None:
        return tail
    return tail[: len(header) + next_job.start()]


def test_resolve_pointer_handles_nested_maps_and_lists() -> None:
    payload = {
        "body": {
            "results": [
                {"name": "Baseline Corpus Episode"},
            ]
        }
    }

    assert resolve_pointer(payload, "/body/results/0/name") == "Baseline Corpus Episode"


def test_validate_expectations_supports_required_equals_minimums_and_list_contains() -> None:
    payload = {
        "status_code": 200,
        "body": {"total": 2, "results": [{"name": "Baseline Corpus Episode"}]},
    }
    expect = {
        "required": ["/body/results"],
        "equals": {"/status_code": 200},
        "minimums": {"/body/total": 1},
        "list_contains": [
            {"pointer": "/body/results", "match": {"name": "Baseline Corpus Episode"}},
        ],
        "serialized_contains": ["Baseline Corpus Episode"],
    }

    assert validate_expectations(payload, expect) == []


def test_validate_expectations_reports_mismatches() -> None:
    payload = {"status_code": 500, "body": {"results": []}}
    expect = {
        "equals": {"/status_code": 200},
        "minimums": {"/body/total": 1},
        "list_contains": [{"pointer": "/body/results", "match": {"name": "Missing"}}],
    }

    errors = validate_expectations(payload, expect)

    assert len(errors) == EXPECTED_ERROR_COUNT
    assert "expected 200" in errors[0]
    assert "missing pointer for minimum check" in errors[1]
    assert "did not contain match" in errors[2]


def test_resolve_placeholders_handles_embedded_and_exact_tokens() -> None:
    manifest = {
        "graph_fixture": {
            "task_a": {"id": "task_alpha", "name": "Obsidian Spire"},
        }
    }
    payload = {
        "path": "/entities/{{graph_fixture.task_a.id}}",
        "expect": {
            "equals": {
                "/body/id": "{{graph_fixture.task_a.id}}",
                "/body/name": "{{graph_fixture.task_a.name}}",
            }
        },
    }

    resolved = resolve_placeholders(payload, manifest)

    assert resolved == {
        "path": "/entities/task_alpha",
        "expect": {
            "equals": {
                "/body/id": "task_alpha",
                "/body/name": "Obsidian Spire",
            }
        },
    }


def test_jsonl_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    rows = [{"id": "case-a"}, {"id": "case-b"}]

    write_jsonl(path, rows)

    assert read_jsonl(path) == rows


def test_write_manifest_can_carry_runtime_auth_and_raw_fixtures(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"

    write_manifest(
        path,
        base_url="http://localhost:3334",
        email="baseline-corpus@sibyl.dev",
        rest_seed={
            "id": "episode_1",
            "title": "Baseline Corpus Episode",
            "entity_type": "episode",
        },
        graph_fixture={
            "task_a": {
                "id": "task_a",
                "name": "Obsidian Spire",
                "entity_type": "task",
            }
        },
        raw_memory_fixture={
            "personal": {
                "id": "raw_1",
                "title": "Personal Baseline Memory",
                "source_id": "baseline:personal-memory",
            }
        },
        access_token="token-123",  # noqa: S106
    )

    manifest = json.loads(path.read_text(encoding="utf-8"))

    assert manifest["auth"] == {"access_token": "token-123"}
    assert manifest["raw_memory_fixture"]["personal"] == {
        "id": "raw_1",
        "title": "Personal Baseline Memory",
        "source_id": "baseline:personal-memory",
    }


def test_write_manifest_omits_auth_without_runtime_token(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"

    write_manifest(
        path,
        base_url="http://localhost:3334",
        email="baseline-corpus@sibyl.dev",
        rest_seed={"id": "episode_1", "title": "Baseline Corpus Episode"},
        graph_fixture={
            "task_a": {
                "id": "task_a",
                "name": "Obsidian Spire",
                "entity_type": "task",
            }
        },
    )

    manifest = json.loads(path.read_text(encoding="utf-8"))

    assert "auth" not in manifest


def test_runtime_baseline_workflows_enable_local_auth_under_production_env() -> None:
    jobs = {
        ".github/workflows/ci.yml": ("e2e",),
        ".github/workflows/eval.yml": ("live-eval",),
        ".github/workflows/nightly-regression.yml": ("baseline-parity",),
    }

    for workflow_path, job_names in jobs.items():
        workflow = (REPO_ROOT / workflow_path).read_text(encoding="utf-8")
        for job_name in job_names:
            job = _workflow_job(workflow, job_name)
            assert "SIBYL_ENVIRONMENT: production" in job
            assert 'SIBYL_LOCAL_AUTH_ENABLED: "true"' in job
