"""Require a green CI run on the release candidate.

The Release workflow re-proves the heavy CI gates itself (the forced RC
bundle, the E2E fixture, the image scans), because CI's path classifier skips
them for changes outside their paths. CI still owns checks the release does
not repeat, such as the shipped-dependency audit and the Storybook build, so
the candidate's own CI run has to be green.

A path-skipped job is accepted here. The release proves nearly all of those
gates on the candidate itself, and for the rest (the Storybook build) it
relies on CI's own path rules, as every merge does. The jobs in
``ALWAYS_RUN_JOBS`` run on every push and pull request, so a skip there means
CI never really ran on the commit. The newest completed CI verdict on the
commit wins: a failure stops the release, and a cancelled run is passed over.
A cancelled run is never read as a failure, even though CI's ``if: always()``
aggregator (Package Tests) fails whenever a suite is cancelled. CI cannot be
dispatched, so when no run passes the release refuses instead of waiting
forever.

Like ``nightly_evidence``, this module is stdlib only.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.release.nightly_evidence import (
    DEFAULT_APPEAR_GRACE_SECONDS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_TIMEOUT_MINUTES,
    EvidenceError,
    GhCli,
    GitHub,
    GitHubError,
    NightlyJob,
    NightlyRun,
    Resolution,
    run_failed,
)

CI_WORKFLOW_FILE = "ci.yml"
CI_WORKFLOW_PATH = f".github/workflows/{CI_WORKFLOW_FILE}"
CI_WORKFLOW_NAME = "CI"
ALWAYS_RUN_JOBS = ("Detect Changes", "Dependency Audit")
_FAILED_JOB_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "cancelled"})


def ci_failures(run: NightlyRun, jobs: Sequence[NightlyJob], candidate_sha: str) -> list[str]:
    """Every reason ``run`` cannot vouch for ``candidate_sha``; empty means it can."""
    label = f"CI run {run.run_id} ({run.event or 'unknown event'})"
    failures: list[str] = []
    if run.name != CI_WORKFLOW_NAME or run.path.split("@", 1)[0] != CI_WORKFLOW_PATH:
        failures.append(f"{label} is {run.name!r} from {run.path!r}, not {CI_WORKFLOW_PATH}")
    if run.head_sha != candidate_sha:
        failures.append(f"{label} ran on {run.head_sha}, not the candidate {candidate_sha}")
    if run.status != "completed":
        failures.append(f"{label} is {run.status}, not completed")
        return failures
    if run.conclusion != "success":
        failures.append(f"{label} concluded {run.conclusion}, not success")
    by_name = {job.name: job for job in jobs}
    for name in ALWAYS_RUN_JOBS:
        job = by_name.get(name)
        if job is None:
            failures.append(f"{label} has no {name!r} job")
        elif job.conclusion != "success":
            failures.append(f"{label} job {name!r} concluded {job.conclusion or job.status}")
    failures.extend(
        f"{label} job {job.name!r} concluded {job.conclusion}"
        for job in jobs
        if job.name not in ALWAYS_RUN_JOBS and job.conclusion in _FAILED_JOB_CONCLUSIONS
    )
    return failures


def _log(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def resolve_ci(
    github: GitHub,
    *,
    candidate_sha: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_MINUTES * 60,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    appear_grace_seconds: float = DEFAULT_APPEAR_GRACE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = None,
) -> Resolution:
    """Return the newest passing CI run on the candidate, waiting for one in progress."""
    log = log or _log
    started = clock()
    deadline = started + timeout_seconds
    judged: dict[tuple[int, int], list[str]] = {}
    announced: set[int] = set()
    while True:
        try:
            runs = [run for run in github.list_runs(candidate_sha) if run.head_sha == candidate_sha]
            completed = sorted(
                (
                    run
                    for run in runs
                    if run.status == "completed" and run.verdict_key not in judged
                ),
                key=lambda run: run.recency,
                reverse=True,
            )
            verdicts = [(run, tuple(github.list_jobs(run.run_id))) for run in completed]
        except GitHubError as exc:
            log(f"::warning::could not read CI runs, polling again: {exc}")
            runs = None
            verdicts = []

        for run, jobs in verdicts:
            failures = ci_failures(run, jobs, candidate_sha)
            if not failures:
                return Resolution(run=run, jobs=jobs, source="existing")
            judged[run.verdict_key] = failures
            log(f"Not citing {run.url or run.run_id}: {'; '.join(failures)}")
            if run.conclusion != "cancelled" and run_failed(run, jobs):
                raise EvidenceError(
                    [
                        f"CI failed on the candidate {candidate_sha}: {run.url or run.run_id}",
                        *failures,
                        "Fix the failure on the branch, or re-run the failed jobs if they "
                        "were infrastructure, then dispatch the release again.",
                    ]
                )

        if runs is not None:
            active = [run for run in runs if run.status != "completed"]
            for run in active:
                if run.run_id not in announced:
                    announced.add(run.run_id)
                    log(f"Waiting for {run.url or run.run_id} ({run.event}, {run.status})")
            # A push that just landed takes a few seconds to show a run.
            if not active and clock() - started >= appear_grace_seconds:
                raise EvidenceError(
                    [
                        f"no CI run on {candidate_sha} passed, and none is running. A "
                        "cancelled run usually means a newer push superseded this commit; "
                        "dispatch the release on the new head.",
                        *(reason for reasons in judged.values() for reason in reasons),
                    ]
                )

        if clock() >= deadline:
            raise EvidenceError(
                [
                    f"timed out after {round(timeout_seconds / 60)} minutes waiting for CI "
                    f"on {candidate_sha}",
                    *(reason for reasons in judged.values() for reason in reasons),
                ]
            )
        sleep(poll_seconds)


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "")
    return Path(value) if value else None


def main(
    argv: Sequence[str] | None = None,
    *,
    github: GitHub | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(description="Require a green CI run on the candidate.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--sha", required=True, help="candidate commit")
    parser.add_argument("--timeout-minutes", type=float, default=DEFAULT_TIMEOUT_MINUTES)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--github-output", type=Path, default=_env_path("GITHUB_OUTPUT"))
    parser.add_argument("--summary", type=Path, default=_env_path("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo is required when GITHUB_REPOSITORY is unset")
    client = github if github is not None else GhCli(args.repo, workflow_file=CI_WORKFLOW_FILE)

    try:
        resolution = resolve_ci(
            client,
            candidate_sha=args.sha,
            timeout_seconds=args.timeout_minutes * 60,
            poll_seconds=args.poll_seconds,
            clock=clock,
            sleep=sleep,
        )
    except (EvidenceError, GitHubError) as exc:
        reasons = exc.reasons if isinstance(exc, EvidenceError) else (str(exc),)
        for reason in reasons:
            sys.stderr.write(f"::error::{reason}\n")
        return 1

    run = resolution.run
    sys.stdout.write(f"Citing CI {run.url} ({run.event}) for {args.sha}\n")
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"run_id={run.run_id}\nurl={run.url}\nevent={run.event}\n")
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as stream:
            stream.write("### CI evidence\n\n")
            stream.write(f"- Candidate: `{args.sha}`\n")
            stream.write(f"- Run: [{run.run_id}]({run.url}) ({run.event})\n\n")
            stream.write("| Job | Conclusion |\n| --- | --- |\n")
            for job in resolution.jobs:
                stream.write(f"| {job.name} | {job.conclusion} |\n")
            stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
