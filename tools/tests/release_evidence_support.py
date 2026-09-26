"""Fakes shared by the release evidence tests: a scripted GitHub and a sleep-driven clock."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from tools.release.nightly_evidence import (
    WORKFLOW_NAME,
    WORKFLOW_PATH,
    GitHubError,
    NightlyJob,
    NightlyRun,
)

SHA = "a" * 40
OTHER_SHA = "b" * 40


def make_run(
    run_id: int,
    *,
    event: str = "workflow_dispatch",
    status: str = "completed",
    conclusion: str | None = "success",
    sha: str = SHA,
    created: str = "2026-09-26T09:00:00Z",
    attempt: int = 1,
    started: str = "",
    name: str = WORKFLOW_NAME,
    path: str = WORKFLOW_PATH,
) -> NightlyRun:
    return NightlyRun(
        run_id=run_id,
        name=name,
        path=path,
        event=event,
        status=status,
        conclusion=conclusion if status == "completed" else None,
        head_sha=sha,
        url=f"https://github.com/o/r/actions/runs/{run_id}",
        created_at=created,
        attempt=attempt,
        started_at=started,
    )


class FakeGitHub:
    def __init__(
        self,
        runs: Sequence[NightlyRun] = (),
        jobs: dict[int, Sequence[NightlyJob]] | None = None,
        head: str = SHA,
    ) -> None:
        self.runs = list(runs)
        self.jobs = {run_id: tuple(value) for run_id, value in (jobs or {}).items()}
        self.head = head
        self.dispatched: list[str] = []
        self.on_dispatch: Callable[[FakeGitHub], None] | None = None
        self.list_failures = 0

    def list_runs(self, head_sha: str) -> list[NightlyRun]:
        if self.list_failures:
            self.list_failures -= 1
            raise GitHubError("HTTP 502")
        return [run for run in self.runs if run.head_sha == head_sha]

    def get_run(self, run_id: int) -> NightlyRun:
        return next(run for run in self.runs if run.run_id == run_id)

    def list_jobs(self, run_id: int) -> list[NightlyJob]:
        return list(self.jobs.get(run_id, ()))

    def ref_head(self, ref: str) -> str:
        return self.head

    def dispatch(self, branch: str) -> None:
        self.dispatched.append(branch)
        if self.on_dispatch is not None:
            self.on_dispatch(self)

    def add(self, run: NightlyRun, jobs: Sequence[NightlyJob] = ()) -> None:
        self.runs = [existing for existing in self.runs if existing.run_id != run.run_id]
        self.runs.append(run)
        self.jobs[run.run_id] = tuple(jobs)


class FakeClock:
    """Monotonic time that advances only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0
        self.after_sleep: dict[int, Callable[[], None]] = {}

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        hook = self.after_sleep.pop(self.sleeps, None)
        if hook is not None:
            hook()
