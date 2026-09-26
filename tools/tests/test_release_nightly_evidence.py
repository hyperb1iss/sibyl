from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from tools.release.nightly_evidence import (
    DEFAULT_TIMEOUT_MINUTES,
    MAX_DISPATCHES,
    WORKFLOW_NAME,
    WORKFLOW_PATH,
    EvidenceError,
    GhCli,
    NightlyJob,
    evidence_failures,
    main,
    resolve_nightly,
)
from tools.tests.conftest import REPO_ROOT
from tools.tests.release_evidence_support import OTHER_SHA, SHA, FakeClock, FakeGitHub, make_run

REF = "refs/heads/main"
NIGHTLY_JOBS = ("Baseline Parity", "Live Graph Regression", "Restore To Scratch")
ALL_GREEN = tuple(NightlyJob(name, "completed", "success") for name in NIGHTLY_JOBS)
# The daily schedule: GitHub reports the run as success with this job skipped.
DAILY = (*ALL_GREEN[:2], NightlyJob("Restore To Scratch", "completed", "skipped"))


_run = make_run


def _resolve(github: FakeGitHub, clock: FakeClock, **kwargs: Any):
    return resolve_nightly(
        github,
        candidate_sha=SHA,
        ref=kwargs.pop("ref", REF),
        clock=clock,
        sleep=clock.sleep,
        log=lambda _message: None,
        **kwargs,
    )


def test_dispatched_run_with_every_job_green_is_evidence() -> None:
    assert evidence_failures(_run(1), ALL_GREEN, SHA) == []


def test_daily_schedule_with_a_skipped_job_is_not_evidence() -> None:
    failures = evidence_failures(_run(1, event="schedule"), DAILY, SHA)

    assert len(failures) == 1
    assert "'Restore To Scratch' concluded skipped" in failures[0]
    assert "did not run on this commit" in failures[0]


def test_monday_schedule_with_every_job_green_counts() -> None:
    assert evidence_failures(_run(1, event="schedule"), ALL_GREEN, SHA) == []


def test_run_on_another_commit_is_not_evidence() -> None:
    failures = evidence_failures(_run(1, sha=OTHER_SHA), ALL_GREEN, SHA)

    assert failures == [
        f"run 1 (workflow_dispatch) ran on {OTHER_SHA}, not the candidate {SHA}",
    ]


def test_run_of_another_workflow_is_not_evidence() -> None:
    impostor = replace(_run(1), name="CI", path=".github/workflows/ci.yml")

    failures = evidence_failures(impostor, ALL_GREEN, SHA)

    assert any("is 'CI', not 'Nightly Regression'" in failure for failure in failures)
    assert any("not .github/workflows/nightly-regression.yml" in failure for failure in failures)


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("in_progress", None, "is in_progress, not completed"),
        ("completed", "failure", "concluded failure, not success"),
        ("completed", "cancelled", "concluded cancelled, not success"),
    ],
)
def test_unfinished_or_red_run_is_not_evidence(
    status: str, conclusion: str | None, expected: str
) -> None:
    failures = evidence_failures(_run(1, status=status, conclusion=conclusion), ALL_GREEN, SHA)

    assert any(expected in failure for failure in failures)


def test_run_without_jobs_is_not_evidence() -> None:
    assert evidence_failures(_run(1), (), SHA) == ["run 1 (workflow_dispatch) reports no jobs"]


def test_explicit_run_id_is_validated_without_search_or_dispatch() -> None:
    named = _run(7)
    github = FakeGitHub([named], {named.run_id: ALL_GREEN})

    resolution = _resolve(github, FakeClock(), run_id=named.run_id)

    assert resolution.run == named
    assert resolution.source == "override"
    assert github.dispatched == []


def test_invalid_explicit_run_id_fails_without_falling_back() -> None:
    # A passing run exists, but the operator named the daily one. Citing the
    # other run would cite something nobody asked for.
    github = FakeGitHub(
        [_run(7, event="schedule"), _run(8)],
        {7: DAILY, 8: ALL_GREEN},
    )

    with pytest.raises(EvidenceError) as raised:
        _resolve(github, FakeClock(), run_id=7)

    assert raised.value.reasons[0] == "Nightly Regression run 7 is not release evidence"
    assert github.dispatched == []


def test_resolve_cites_an_existing_green_run_past_a_newer_daily_one() -> None:
    github = FakeGitHub(
        [
            _run(1, created="2026-09-26T09:20:50Z"),
            _run(2, event="schedule", created="2026-09-27T09:09:27Z"),
        ],
        {1: ALL_GREEN, 2: DAILY},
    )

    resolution = _resolve(github, FakeClock())

    assert resolution.run.run_id == 1
    assert resolution.source == "existing"
    assert github.dispatched == []


def test_resolve_dispatches_when_only_incomplete_runs_exist_and_waits() -> None:
    github = FakeGitHub([_run(1, event="schedule")], {1: DAILY})
    clock = FakeClock()
    finished = _run(9, created="2026-09-26T10:00:00Z")
    github.on_dispatch = lambda gh: gh.add(replace(finished, status="queued", conclusion=None))
    clock.after_sleep[1] = lambda: github.add(
        replace(finished, status="in_progress", conclusion=None)
    )
    clock.after_sleep[3] = lambda: github.add(finished, ALL_GREEN)

    resolution = _resolve(github, clock)

    assert github.dispatched == ["main"]
    assert resolution.run == finished
    assert resolution.source == "dispatched"
    assert resolution.dispatches == 1


def test_resolve_waits_for_an_active_run_instead_of_cancelling_it() -> None:
    # Nightly cancels an in-progress run when a newer one starts on its ref,
    # so dispatching here would throw away a run that is nearly done.
    finished = _run(4)
    github = FakeGitHub([replace(finished, status="in_progress", conclusion=None)])
    clock = FakeClock()
    clock.after_sleep[2] = lambda: github.add(finished, ALL_GREEN)

    resolution = _resolve(github, clock)

    assert resolution.run == finished
    assert github.dispatched == []


def test_resolve_refuses_a_failed_run_even_when_an_older_one_passed() -> None:
    github = FakeGitHub(
        [
            _run(1, created="2026-09-26T09:00:00Z"),
            _run(2, conclusion="failure", created="2026-09-26T10:00:00Z"),
        ],
        {
            1: ALL_GREEN,
            2: (*ALL_GREEN[:2], NightlyJob("Restore To Scratch", "completed", "failure")),
        },
    )

    with pytest.raises(EvidenceError) as raised:
        _resolve(github, FakeClock())

    assert raised.value.reasons[0].startswith("Nightly Regression failed on the candidate")
    assert "actions/runs/2" in raised.value.reasons[0]
    assert github.dispatched == []


def test_resolve_never_redispatches_after_its_own_run_fails() -> None:
    github = FakeGitHub()
    clock = FakeClock()
    github.on_dispatch = lambda gh: gh.add(_run(5, status="in_progress"))
    clock.after_sleep[1] = lambda: github.add(
        _run(5, conclusion="failure"),
        (NightlyJob("Baseline Parity", "completed", "failure"), *ALL_GREEN[1:]),
    )

    with pytest.raises(EvidenceError, match="failed on the candidate"):
        _resolve(github, clock)

    assert github.dispatched == ["main"]


def test_resolve_refuses_to_dispatch_once_the_branch_moved() -> None:
    github = FakeGitHub(head=OTHER_SHA)

    with pytest.raises(EvidenceError) as raised:
        _resolve(github, FakeClock())

    assert f"main moved to {OTHER_SHA}" in raised.value.reasons[0]
    assert github.dispatched == []


def test_resolve_redispatches_once_after_a_cancelled_run() -> None:
    github = FakeGitHub()
    clock = FakeClock()
    cancelled = _run(11, conclusion="cancelled", created="2026-09-26T10:11:00Z")
    green = _run(12, created="2026-09-26T10:12:00Z")
    outcomes = iter(
        (
            (cancelled, (NightlyJob("Baseline Parity", "completed", "cancelled"),)),
            (green, ALL_GREEN),
        )
    )

    def start_run(gh: FakeGitHub) -> None:
        finished, jobs = next(outcomes)
        gh.add(replace(finished, status="in_progress", conclusion=None))
        clock.after_sleep[clock.sleeps + 1] = lambda: gh.add(finished, jobs)

    github.on_dispatch = start_run

    resolution = _resolve(github, clock)

    assert github.dispatched == ["main", "main"]
    assert resolution.run == green
    assert resolution.dispatches == MAX_DISPATCHES


def test_resolve_stops_after_the_dispatch_budget() -> None:
    github = FakeGitHub()
    clock = FakeClock()
    counter = iter(range(20, 30))

    def cancelled_run(gh: FakeGitHub) -> None:
        gh.add(
            _run(next(counter), conclusion="cancelled"),
            (NightlyJob("Baseline Parity", "completed", "cancelled"),),
        )

    github.on_dispatch = cancelled_run

    with pytest.raises(EvidenceError) as raised:
        _resolve(github, clock)

    assert raised.value.reasons[0].endswith(f"after {MAX_DISPATCHES} dispatches")
    assert len(github.dispatched) == MAX_DISPATCHES


def test_resolve_waits_for_a_dispatched_run_to_appear_before_dispatching_again() -> None:
    github = FakeGitHub()
    clock = FakeClock()
    # Listed only after three polls (60 seconds), inside the grace window.
    listed = _run(30)
    clock.after_sleep[3] = lambda: github.add(listed, ALL_GREEN)

    resolution = _resolve(github, clock)

    assert github.dispatched == ["main"]
    assert resolution.run == listed


def test_resolve_times_out_with_a_clear_message() -> None:
    github = FakeGitHub([_run(40, status="in_progress")])

    with pytest.raises(EvidenceError) as raised:
        _resolve(github, FakeClock(), timeout_seconds=60)

    assert raised.value.reasons[0].startswith("timed out after 1 minutes")
    assert github.dispatched == []


def test_resolve_polls_through_a_transient_listing_error() -> None:
    github = FakeGitHub([_run(1)], {1: ALL_GREEN})
    github.list_failures = 1

    resolution = _resolve(github, FakeClock())

    assert resolution.run.run_id == 1


def test_resolve_judges_a_rerun_attempt_again() -> None:
    github = FakeGitHub(
        [_run(50, conclusion="cancelled")],
        {50: (NightlyJob("Baseline Parity", "completed", "cancelled"),)},
    )
    clock = FakeClock()
    github.on_dispatch = lambda gh: gh.add(_run(50, status="in_progress", attempt=2))
    clock.after_sleep[1] = lambda: github.add(_run(50, attempt=2), ALL_GREEN)

    resolution = _resolve(github, clock)

    assert resolution.run.attempt == 2  # noqa: PLR2004


def test_resolve_refuses_to_dispatch_for_a_tag_ref() -> None:
    with pytest.raises(EvidenceError, match="only branch refs can be dispatched"):
        _resolve(FakeGitHub(), FakeClock(), ref="refs/tags/v1.4.2")


def test_resolve_cites_an_existing_green_run_for_a_tag_ref() -> None:
    green = _run(1)
    github = FakeGitHub([green], {1: ALL_GREEN})

    resolution = _resolve(github, FakeClock(), ref="refs/tags/v1.4.2")

    assert resolution.run == green
    assert github.dispatched == []


def test_a_passing_rerun_outranks_the_failure_it_follows() -> None:
    # Run 1 was created first and re-run after run 2 failed. A re-run keeps
    # created_at, so recency has to come from when the attempt started.
    rerun = _run(1, created="2026-09-26T09:00:00Z", started="2026-09-26T11:00:00Z", attempt=2)
    failed = _run(2, conclusion="failure", created="2026-09-26T10:00:00Z")
    github = FakeGitHub(
        [rerun, failed],
        {1: ALL_GREEN, 2: (NightlyJob("Baseline Parity", "completed", "failure"),)},
    )

    assert _resolve(github, FakeClock()).run == rerun


def test_verify_refuses_a_run_that_a_newer_failure_outranks() -> None:
    cited = _run(1, created="2026-09-26T09:00:00Z")
    newer = _run(2, conclusion="failure", created="2026-09-26T10:00:00Z")
    github = FakeGitHub(
        [cited, newer],
        {1: ALL_GREEN, 2: (NightlyJob("Live Graph Regression", "completed", "failure"),)},
    )

    with pytest.raises(EvidenceError) as raised:
        _resolve(github, FakeClock(), run_id=cited.run_id)

    assert "but a newer run" in raised.value.reasons[0]
    assert "actions/runs/2" in raised.value.reasons[0]


def test_verify_ignores_a_newer_incomplete_run() -> None:
    cited = _run(1, created="2026-09-26T09:00:00Z")
    daily = _run(2, event="schedule", created="2026-09-27T09:00:00Z")
    github = FakeGitHub([cited, daily], {1: ALL_GREEN, 2: DAILY})

    assert _resolve(github, FakeClock(), run_id=cited.run_id).run == cited


def test_cli_resolve_writes_outputs_and_summary(tmp_path: Path) -> None:
    github = FakeGitHub([_run(1)], {1: ALL_GREEN})
    output = tmp_path / "output"
    summary = tmp_path / "summary"

    status = main(
        [
            "resolve",
            "--repo",
            "o/r",
            "--sha",
            SHA,
            "--ref",
            REF,
            "--run-id",
            "",
            "--github-output",
            str(output),
            "--summary",
            str(summary),
        ],
        github=github,
    )

    assert status == 0
    assert output.read_text(encoding="utf-8").splitlines() == [
        "run_id=1",
        "url=https://github.com/o/r/actions/runs/1",
        "event=workflow_dispatch",
        "source=existing",
        "dispatches=0",
    ]
    text = summary.read_text(encoding="utf-8")
    assert "| Restore To Scratch | success |" in text
    assert f"`{SHA}`" in text


def test_cli_verify_reports_every_reason_as_an_annotation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    github = FakeGitHub([_run(2, event="schedule", sha=OTHER_SHA)], {2: DAILY})

    status = main(["verify", "--repo", "o/r", "--sha", SHA, "--run-id", "2"], github=github)

    errors = capsys.readouterr().err.splitlines()
    assert status == 1
    assert errors[0] == "::error::Nightly Regression run 2 is not release evidence"
    assert any("not the candidate" in line for line in errors)
    assert any("'Restore To Scratch' concluded skipped" in line for line in errors)


def test_cli_rejects_a_non_numeric_run_id(capsys: pytest.CaptureFixture[str]) -> None:
    status = main(
        ["verify", "--repo", "o/r", "--sha", SHA, "--run-id", "latest"],
        github=FakeGitHub(),
    )

    assert status == 1
    assert "must be a numeric run ID" in capsys.readouterr().err


def test_gh_cli_asks_for_the_candidate_runs_and_dispatches_the_branch() -> None:
    calls: list[list[str]] = []
    responses = {
        "runs": {"workflow_runs": [{"id": 3, "head_sha": SHA, "run_attempt": 2}]},
        "jobs": {"jobs": [{"name": "Baseline Parity", "status": "completed"}]},
        "ref": {"object": {"sha": SHA, "type": "commit"}},
    }

    def runner(args: Sequence[str]) -> str:
        calls.append(list(args))
        endpoint = args[3]
        if endpoint.endswith("/runs"):
            return json.dumps(responses["runs"])
        if endpoint.endswith("/jobs"):
            return json.dumps(responses["jobs"])
        if "/git/ref/" in endpoint:
            return json.dumps(responses["ref"])
        return ""

    client = GhCli("o/r", runner=runner)
    assert GhCli("o/r", workflow_file="ci.yml").workflow_file == "ci.yml"

    runs = client.list_runs(SHA)
    client.list_jobs(3)
    assert client.ref_head("refs/heads/nova/release") == SHA
    client.dispatch("main")

    assert runs[0].attempt == 2  # noqa: PLR2004
    assert calls == [
        [
            "api",
            "-X",
            "GET",
            "repos/o/r/actions/workflows/nightly-regression.yml/runs",
            "-f",
            f"head_sha={SHA}",
            "-f",
            "per_page=100",
        ],
        [
            "api",
            "-X",
            "GET",
            "repos/o/r/actions/runs/3/jobs",
            "-f",
            "filter=latest",
            "-f",
            "per_page=100",
        ],
        ["api", "-X", "GET", "repos/o/r/git/ref/heads/nova/release"],
        [
            "api",
            "-X",
            "POST",
            "repos/o/r/actions/workflows/nightly-regression.yml/dispatches",
            "-f",
            "ref=main",
        ],
    ]


def test_evidence_rules_match_the_nightly_workflow() -> None:
    workflow_path = REPO_ROOT / WORKFLOW_PATH
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert workflow["name"] == WORKFLOW_NAME
    # This condition is why a daily scheduled run is incomplete evidence: the
    # job is skipped, and GitHub still calls the run a success.
    assert "workflow_dispatch" in jobs["restore-to-scratch"]["if"]
    assert "0 10 * * 1" in jobs["restore-to-scratch"]["if"]
    # The wait outlasts every nightly job's own timeout.
    assert all(int(job["timeout-minutes"]) < DEFAULT_TIMEOUT_MINUTES for job in jobs.values())
