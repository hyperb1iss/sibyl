from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from tools.release.ci_evidence import (
    ALWAYS_RUN_JOBS,
    CI_WORKFLOW_FILE,
    CI_WORKFLOW_NAME,
    CI_WORKFLOW_PATH,
    ci_failures,
    main,
    resolve_ci,
)
from tools.release.nightly_evidence import EvidenceError, NightlyJob
from tools.tests.conftest import REPO_ROOT
from tools.tests.release_evidence_support import OTHER_SHA, SHA, FakeClock, FakeGitHub, make_run

# A docs-only push: the always-run jobs pass and the path-scoped ones skip.
DOCS_ONLY = (
    NightlyJob("Detect Changes", "completed", "success"),
    NightlyJob("Dependency Audit", "completed", "success"),
    NightlyJob("E2E", "completed", "skipped"),
    NightlyJob("Helm (${{ matrix.profile }})", "completed", "skipped"),
)
RED_CORE = (
    *DOCS_ONLY[:2],
    NightlyJob("Test Suite (core)", "completed", "failure"),
    NightlyJob("Package Tests", "completed", "failure"),
)


def _ci_run(run_id: int, **kwargs: Any):
    return make_run(
        run_id,
        event=kwargs.pop("event", "push"),
        name=CI_WORKFLOW_NAME,
        path=CI_WORKFLOW_PATH,
        **kwargs,
    )


def _resolve(github: FakeGitHub, clock: FakeClock, **kwargs: Any):
    return resolve_ci(
        github,
        candidate_sha=SHA,
        clock=clock,
        sleep=clock.sleep,
        log=lambda _message: None,
        **kwargs,
    )


def test_a_green_run_with_path_skipped_jobs_is_evidence() -> None:
    # The release proves the skipped gates on the candidate itself.
    assert ci_failures(_ci_run(1), DOCS_ONLY, SHA) == []


def test_a_skipped_always_run_job_is_not_evidence() -> None:
    jobs = (DOCS_ONLY[0], NightlyJob("Dependency Audit", "completed", "skipped"))

    failures = ci_failures(_ci_run(1), jobs, SHA)

    assert failures == ["CI run 1 (push) job 'Dependency Audit' concluded skipped"]


def test_a_run_missing_an_always_run_job_is_not_evidence() -> None:
    assert ci_failures(_ci_run(1), DOCS_ONLY[:1], SHA) == [
        "CI run 1 (push) has no 'Dependency Audit' job"
    ]


def test_a_red_or_foreign_run_is_not_evidence() -> None:
    failures = ci_failures(_ci_run(1, conclusion="failure", sha=OTHER_SHA), RED_CORE, SHA)

    assert any("not the candidate" in failure for failure in failures)
    assert any("concluded failure, not success" in failure for failure in failures)
    assert any("'Test Suite (core)' concluded failure" in failure for failure in failures)


def test_a_run_of_another_workflow_is_not_evidence() -> None:
    nightly = make_run(1)

    assert any("not .github/workflows/ci.yml" in f for f in ci_failures(nightly, DOCS_ONLY, SHA))


def test_resolve_cites_the_green_run_on_the_candidate() -> None:
    green = _ci_run(1)
    github = FakeGitHub([green, _ci_run(2, sha=OTHER_SHA)], {1: DOCS_ONLY})

    resolution = _resolve(github, FakeClock())

    assert resolution.run == green
    assert github.dispatched == []


def test_resolve_waits_for_ci_that_is_still_running() -> None:
    finished = _ci_run(3)
    github = FakeGitHub([replace(finished, status="in_progress", conclusion=None)])
    clock = FakeClock()
    clock.after_sleep[4] = lambda: github.add(finished, DOCS_ONLY)

    assert _resolve(github, clock).run == finished


def test_resolve_refuses_red_ci_on_the_candidate() -> None:
    github = FakeGitHub([_ci_run(4, conclusion="failure")], {4: RED_CORE})

    with pytest.raises(EvidenceError) as raised:
        _resolve(github, FakeClock())

    assert raised.value.reasons[0].startswith(f"CI failed on the candidate {SHA}")


def test_resolve_passes_over_a_cancelled_run_to_an_older_green_one() -> None:
    green = _ci_run(5, created="2026-09-26T08:00:00Z")
    cancelled = _ci_run(6, conclusion="cancelled", created="2026-09-26T09:00:00Z")
    github = FakeGitHub(
        [green, cancelled],
        {5: DOCS_ONLY, 6: (*DOCS_ONLY[:2], NightlyJob("E2E", "completed", "cancelled"))},
    )

    assert _resolve(github, FakeClock()).run == green


def test_resolve_refuses_when_ci_never_ran_on_the_candidate() -> None:
    github = FakeGitHub([_ci_run(7, sha=OTHER_SHA)], {7: DOCS_ONLY})

    with pytest.raises(EvidenceError) as raised:
        _resolve(github, FakeClock())

    assert raised.value.reasons[0] == f"no CI run on {SHA} passed, and none is running"


def test_resolve_times_out_while_ci_keeps_running() -> None:
    github = FakeGitHub([_ci_run(8, status="in_progress")])

    with pytest.raises(EvidenceError, match="timed out after 1 minutes"):
        _resolve(github, FakeClock(), timeout_seconds=60)


def test_cli_writes_outputs_and_summary(tmp_path: Path) -> None:
    github = FakeGitHub([_ci_run(9)], {9: DOCS_ONLY})
    output = tmp_path / "output"
    summary = tmp_path / "summary"

    status = main(
        [
            "--repo",
            "o/r",
            "--sha",
            SHA,
            "--github-output",
            str(output),
            "--summary",
            str(summary),
        ],
        github=github,
    )

    assert status == 0
    assert output.read_text(encoding="utf-8").splitlines() == [
        "run_id=9",
        "url=https://github.com/o/r/actions/runs/9",
        "event=push",
    ]
    assert "| Dependency Audit | success |" in summary.read_text(encoding="utf-8")


def test_always_run_jobs_really_run_on_every_ci_event() -> None:
    workflow = yaml.safe_load((REPO_ROOT / CI_WORKFLOW_PATH).read_text(encoding="utf-8"))
    jobs_by_name = {job["name"]: job for job in workflow["jobs"].values()}

    assert workflow["name"] == CI_WORKFLOW_NAME
    assert CI_WORKFLOW_PATH.endswith(CI_WORKFLOW_FILE)
    for name in ALWAYS_RUN_JOBS:
        assert "if" not in jobs_by_name[name], name
        assert "needs" not in jobs_by_name[name], name
