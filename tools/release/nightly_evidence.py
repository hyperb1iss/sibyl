"""Find, dispatch, or verify the Nightly Regression run a release cites.

A release may cite a Nightly Regression run only when that run finished on the
exact candidate commit and every one of its jobs succeeded. Two rules make this
stricter than the run's own conclusion:

* A skipped job is a gate that never ran, not a pass. The daily schedule skips
  Restore To Scratch (it runs only on dispatch and on the Monday schedule), yet
  GitHub still reports the whole run as ``success``.
* A run on any other commit proves nothing about the candidate, however recent
  or green it is.

When no run qualifies, ``resolve`` dispatches Nightly Regression on the release
ref and waits for it. A run that fails on the candidate is reported, never
retried: re-dispatching until green would turn a real regression into a flake.

The module is stdlib only, so the evidence job runs on the runner's system
Python without installing the workspace toolchain.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

WORKFLOW_FILE = "nightly-regression.yml"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_FILE}"
WORKFLOW_NAME = "Nightly Regression"

DEFAULT_TIMEOUT_MINUTES = 50
DEFAULT_POLL_SECONDS = 20
# A dispatched run takes a few seconds to show up in the run list. Inside this
# window an empty list means "not listed yet", not "nothing is running".
DEFAULT_APPEAR_GRACE_SECONDS = 120
# Nightly Regression cancels an in-progress run when a newer one starts on the
# same ref, so the daily schedule can cancel a run this module dispatched. One
# more dispatch recovers from that. Only a cancelled or incomplete run earns
# the second dispatch; a failed one stops the release.
MAX_DISPATCHES = 2

_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
_SKIP_HINT = " (a skipped job is a gate that did not run on this commit)"


class GitHubError(RuntimeError):
    """A GitHub API call failed."""


class EvidenceError(RuntimeError):
    """No Nightly Regression run can vouch for the candidate."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True, slots=True)
class NightlyJob:
    name: str
    status: str
    conclusion: str | None

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> NightlyJob:
        return cls(
            name=str(payload.get("name") or ""),
            status=str(payload.get("status") or ""),
            conclusion=payload.get("conclusion"),
        )


@dataclass(frozen=True, slots=True)
class NightlyRun:
    run_id: int
    name: str
    path: str
    event: str
    status: str
    conclusion: str | None
    head_sha: str
    url: str
    created_at: str
    attempt: int = 1
    started_at: str = ""

    @property
    def verdict_key(self) -> tuple[int, int]:
        """A re-run keeps its run ID, so a verdict belongs to one attempt."""
        return (self.run_id, self.attempt)

    @property
    def recency(self) -> tuple[str, int]:
        """Order verdicts by when the latest attempt started.

        A re-run keeps ``created_at`` but restarts ``run_started_at``, so a
        passing re-run of an older run is newer than a failure it follows.
        """
        return (self.started_at or self.created_at, self.run_id)

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> NightlyRun:
        return cls(
            run_id=int(payload["id"]),
            name=str(payload.get("name") or ""),
            path=str(payload.get("path") or ""),
            event=str(payload.get("event") or ""),
            status=str(payload.get("status") or ""),
            conclusion=payload.get("conclusion"),
            head_sha=str(payload.get("head_sha") or ""),
            url=str(payload.get("html_url") or ""),
            created_at=str(payload.get("created_at") or ""),
            attempt=int(payload.get("run_attempt") or 1),
            started_at=str(payload.get("run_started_at") or ""),
        )


@dataclass(frozen=True, slots=True)
class Resolution:
    run: NightlyRun
    jobs: tuple[NightlyJob, ...]
    # "override" (nightly_run_id input), "existing" (found without
    # dispatching), or "dispatched" (found after this module dispatched).
    source: str
    dispatches: int = 0


class GitHub(Protocol):
    def list_runs(self, head_sha: str) -> list[NightlyRun]: ...

    def get_run(self, run_id: int) -> NightlyRun: ...

    def list_jobs(self, run_id: int) -> list[NightlyJob]: ...

    def ref_head(self, ref: str) -> str: ...

    def dispatch(self, branch: str) -> None: ...


Runner = Callable[[Sequence[str]], str]


def _run_gh(args: Sequence[str]) -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise GitHubError("the gh CLI is not installed")
    result = subprocess.run(  # noqa: S603
        [gh, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitHubError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


@dataclass(slots=True)
class GhCli:
    """GitHub access through ``gh api``, authenticated by ``GH_TOKEN``."""

    repo: str
    runner: Runner = field(default=_run_gh)
    workflow_file: str = WORKFLOW_FILE

    def _get(self, endpoint: str, **params: str | int) -> Any:
        args = ["api", "-X", "GET", endpoint]
        for key, value in params.items():
            args.extend(("-f", f"{key}={value}"))
        return json.loads(self.runner(args))

    def list_runs(self, head_sha: str) -> list[NightlyRun]:
        payload = self._get(
            f"repos/{self.repo}/actions/workflows/{self.workflow_file}/runs",
            head_sha=head_sha,
            per_page=100,
        )
        return [NightlyRun.from_api(item) for item in payload.get("workflow_runs", [])]

    def get_run(self, run_id: int) -> NightlyRun:
        return NightlyRun.from_api(self._get(f"repos/{self.repo}/actions/runs/{run_id}"))

    def list_jobs(self, run_id: int) -> list[NightlyJob]:
        payload = self._get(
            f"repos/{self.repo}/actions/runs/{run_id}/jobs",
            filter="latest",
            per_page=100,
        )
        return [NightlyJob.from_api(item) for item in payload.get("jobs", [])]

    def ref_head(self, ref: str) -> str:
        payload = self._get(f"repos/{self.repo}/git/ref/{ref.removeprefix('refs/')}")
        return str(payload["object"]["sha"])

    def dispatch(self, branch: str) -> None:
        self.runner(
            [
                "api",
                "-X",
                "POST",
                f"repos/{self.repo}/actions/workflows/{self.workflow_file}/dispatches",
                "-f",
                f"ref={branch}",
            ]
        )


def evidence_failures(
    run: NightlyRun,
    jobs: Sequence[NightlyJob],
    candidate_sha: str,
) -> list[str]:
    """Every reason ``run`` cannot vouch for ``candidate_sha``; empty means it can."""
    label = f"run {run.run_id} ({run.event or 'unknown event'})"
    failures: list[str] = []
    if run.name != WORKFLOW_NAME:
        failures.append(f"{label} is {run.name!r}, not {WORKFLOW_NAME!r}")
    if run.path.split("@", 1)[0] != WORKFLOW_PATH:
        failures.append(f"{label} comes from {run.path!r}, not {WORKFLOW_PATH}")
    if run.head_sha != candidate_sha:
        failures.append(f"{label} ran on {run.head_sha}, not the candidate {candidate_sha}")
    if run.status != "completed":
        failures.append(f"{label} is {run.status}, not completed")
        return failures
    if run.conclusion != "success":
        failures.append(f"{label} concluded {run.conclusion}, not success")
    if not jobs:
        failures.append(f"{label} reports no jobs")
    for job in jobs:
        if job.conclusion != "success":
            outcome = job.conclusion or job.status
            hint = _SKIP_HINT if job.conclusion == "skipped" else ""
            failures.append(f"{label} job {job.name!r} concluded {outcome}{hint}")
    return failures


def run_failed(run: NightlyRun, jobs: Sequence[NightlyJob]) -> bool:
    """True when the run tested the code and the code lost, not merely incomplete."""
    if run.conclusion in _FAILED_CONCLUSIONS:
        return True
    return any(job.conclusion in _FAILED_CONCLUSIONS for job in jobs)


def _branch_name(ref: str) -> str:
    if not ref.startswith("refs/heads/"):
        raise EvidenceError(
            [
                f"cannot dispatch Nightly Regression for {ref!r}: only branch refs can "
                "be dispatched, so pass nightly_run_id for a run on this commit"
            ]
        )
    return ref.removeprefix("refs/heads/")


def _log(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def verify_nightly(github: GitHub, *, run_id: int, candidate_sha: str) -> Resolution:
    """Validate one named run, with no search and no fallback.

    The run must also still be the latest verdict on the commit: a newer run
    on the same SHA that failed outranks it, as it does in ``resolve``.
    """
    run = github.get_run(run_id)
    jobs = tuple(github.list_jobs(run_id))
    failures = evidence_failures(run, jobs, candidate_sha)
    if failures:
        raise EvidenceError([f"Nightly Regression run {run_id} is not release evidence", *failures])
    for newer in github.list_runs(candidate_sha):
        if newer.head_sha != candidate_sha or newer.status != "completed":
            continue
        if newer.recency <= run.recency:
            continue
        newer_jobs = tuple(github.list_jobs(newer.run_id))
        if run_failed(newer, newer_jobs):
            raise EvidenceError(
                [
                    f"Nightly Regression run {run_id} passed, but a newer run on "
                    f"{candidate_sha} failed: {newer.url or newer.run_id}",
                    *evidence_failures(newer, newer_jobs, candidate_sha),
                ]
            )
    return Resolution(run=run, jobs=jobs, source="override")


@dataclass(slots=True)
class _Search:
    """State for one find-or-dispatch pass over a candidate's nightly runs."""

    github: GitHub
    candidate_sha: str
    ref: str
    max_dispatches: int
    log: Callable[[str], None]
    rejected: dict[tuple[int, int], list[str]] = field(default_factory=dict)
    announced: set[int] = field(default_factory=set)
    dispatches: int = 0
    last_dispatch: float | None = None

    def rejected_reasons(self) -> list[str]:
        return [reason for reasons in self.rejected.values() for reason in reasons]

    def read(self) -> tuple[list[NightlyRun], list[tuple[NightlyRun, tuple[NightlyJob, ...]]]]:
        """Every run on the candidate, plus the jobs of each unjudged completed one."""
        runs = [
            run
            for run in self.github.list_runs(self.candidate_sha)
            if run.head_sha == self.candidate_sha
        ]
        unjudged = sorted(
            (
                run
                for run in runs
                if run.status == "completed" and run.verdict_key not in self.rejected
            ),
            key=lambda run: run.recency,
            reverse=True,
        )
        return runs, [(run, tuple(self.github.list_jobs(run.run_id))) for run in unjudged]

    def judge(
        self, verdicts: Sequence[tuple[NightlyRun, tuple[NightlyJob, ...]]]
    ) -> Resolution | None:
        for run, jobs in verdicts:
            failures = evidence_failures(run, jobs, self.candidate_sha)
            if not failures:
                source = "dispatched" if self.dispatches else "existing"
                return Resolution(run=run, jobs=jobs, source=source, dispatches=self.dispatches)
            self.rejected[run.verdict_key] = failures
            self.log(f"Not citing {run.url or run.run_id}: {'; '.join(failures)}")
            if run_failed(run, jobs):
                raise EvidenceError(
                    [
                        f"Nightly Regression failed on the candidate {self.candidate_sha}: "
                        f"{run.url or run.run_id}",
                        *failures,
                        "Fix the regression, or re-run that nightly deliberately if it "
                        "was infrastructure, then dispatch the release again.",
                    ]
                )
        return None

    def wait_or_dispatch(self, runs: Sequence[NightlyRun], now: float, grace: float) -> None:
        active = [run for run in runs if run.status != "completed"]
        for run in active:
            if run.run_id not in self.announced:
                self.announced.add(run.run_id)
                self.log(f"Waiting for {run.url or run.run_id} ({run.event}, {run.status})")
        # Dispatching while a run is active would cancel it through the
        # nightly's own concurrency group, so an active run is waited on.
        if active or (self.last_dispatch is not None and now - self.last_dispatch < grace):
            return
        if self.dispatches >= self.max_dispatches:
            raise EvidenceError(
                [
                    f"no Nightly Regression run on {self.candidate_sha} finished with every "
                    f"job green after {self.dispatches} dispatches",
                    *self.rejected_reasons(),
                ]
            )
        branch = _branch_name(self.ref)
        head = self.github.ref_head(self.ref)
        if head != self.candidate_sha:
            raise EvidenceError(
                [
                    f"{branch} moved to {head} after this release started on "
                    f"{self.candidate_sha}. A nightly dispatched now would test {head}, "
                    "so dispatch the release again on the new head."
                ]
            )
        self.github.dispatch(branch)
        self.dispatches += 1
        self.last_dispatch = now
        self.log(
            f"Dispatched Nightly Regression on {branch} at {self.candidate_sha} "
            f"({self.dispatches} of {self.max_dispatches})"
        )


def resolve_nightly(
    github: GitHub,
    *,
    candidate_sha: str,
    ref: str,
    run_id: int | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_MINUTES * 60,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    appear_grace_seconds: float = DEFAULT_APPEAR_GRACE_SECONDS,
    max_dispatches: int = MAX_DISPATCHES,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = _log,
) -> Resolution:
    """Return a passing same-SHA run, dispatching one when none exists.

    Completed runs are read newest first and the latest verdict on the commit
    wins. A passing run is cited. A failed run stops the release even when an
    older run passed, because citing the older pass would pick the green one
    out of two verdicts on the same code. A cancelled run, or one with a
    skipped job, is incomplete rather than failed, so the search moves past it.

    An explicit ``run_id`` is validated and nothing else: a bad override fails
    rather than falling back to a search, so the cited run is the one the
    operator named.
    """
    if run_id is not None:
        return verify_nightly(github, run_id=run_id, candidate_sha=candidate_sha)

    search = _Search(
        github=github,
        candidate_sha=candidate_sha,
        ref=ref,
        max_dispatches=max_dispatches,
        log=log,
    )
    deadline = clock() + timeout_seconds
    while True:
        try:
            runs, verdicts = search.read()
        except GitHubError as exc:
            log(f"::warning::could not read Nightly Regression runs, polling again: {exc}")
        else:
            resolution = search.judge(verdicts)
            if resolution is not None:
                return resolution
            search.wait_or_dispatch(runs, clock(), appear_grace_seconds)

        if clock() >= deadline:
            raise EvidenceError(
                [
                    f"timed out after {round(timeout_seconds / 60)} minutes waiting for a "
                    f"Nightly Regression run on {candidate_sha} with every job green",
                    *search.rejected_reasons(),
                ]
            )
        sleep(poll_seconds)


def _write_outputs(path: Path, resolution: Resolution) -> None:
    run = resolution.run
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"run_id={run.run_id}\n")
        stream.write(f"url={run.url}\n")
        stream.write(f"event={run.event}\n")
        stream.write(f"source={resolution.source}\n")
        stream.write(f"dispatches={resolution.dispatches}\n")


def _write_summary(path: Path, resolution: Resolution, candidate_sha: str) -> None:
    run = resolution.run
    with path.open("a", encoding="utf-8") as stream:
        stream.write("### Nightly Regression evidence\n\n")
        stream.write(f"- Candidate: `{candidate_sha}`\n")
        stream.write(f"- Run: [{run.run_id}]({run.url}) ({run.event}, {resolution.source})\n\n")
        stream.write("| Job | Conclusion |\n| --- | --- |\n")
        for job in resolution.jobs:
            stream.write(f"| {job.name} | {job.conclusion} |\n")
        stream.write("\n")


def _optional_run_id(raw: str | None) -> int | None:
    value = (raw or "").strip()
    if not value:
        return None
    if not value.isdigit():
        raise EvidenceError([f"nightly_run_id must be a numeric run ID, got {value!r}"])
    return int(value)


def _required(run_id: int | None) -> int:
    if run_id is None:
        raise EvidenceError(["verify needs a Nightly Regression run ID"])
    return run_id


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
    parser = argparse.ArgumentParser(
        description="Find, dispatch, or verify the Nightly Regression run a release cites."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve", help="find or dispatch same-SHA evidence")
    resolve.add_argument("--ref", required=True, help="full ref the release runs on")
    resolve.add_argument("--run-id", default="", help="explicit run to validate instead")
    resolve.add_argument("--timeout-minutes", type=float, default=DEFAULT_TIMEOUT_MINUTES)
    resolve.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)

    verify = commands.add_parser("verify", help="validate one run against the candidate")
    verify.add_argument("--run-id", required=True)

    for command in (resolve, verify):
        command.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
        command.add_argument("--sha", required=True, help="candidate commit")
        command.add_argument("--github-output", type=Path, default=_env_path("GITHUB_OUTPUT"))
        command.add_argument("--summary", type=Path, default=_env_path("GITHUB_STEP_SUMMARY"))

    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo is required when GITHUB_REPOSITORY is unset")
    client = github if github is not None else GhCli(args.repo)

    try:
        run_id = _optional_run_id(args.run_id)
        if args.command == "verify":
            resolution = verify_nightly(client, run_id=_required(run_id), candidate_sha=args.sha)
        else:
            resolution = resolve_nightly(
                client,
                candidate_sha=args.sha,
                ref=args.ref,
                run_id=run_id,
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

    _log(
        f"Citing Nightly Regression {resolution.run.url} "
        f"({resolution.run.event}, {resolution.source}) for {args.sha}"
    )
    if args.github_output is not None:
        _write_outputs(args.github_output, resolution)
    if args.summary is not None:
        _write_summary(args.summary, resolution, args.sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
