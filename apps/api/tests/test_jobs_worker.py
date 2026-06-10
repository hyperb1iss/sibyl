from __future__ import annotations

from sibyl.config import settings
from sibyl.jobs.worker import WorkerSettings


def test_worker_settings_uses_resolved_max_jobs() -> None:
    assert WorkerSettings.max_jobs == settings.resolved_worker_max_jobs
