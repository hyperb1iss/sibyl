"""Async job queue using arq + Redis (via FalkorDB).

Provides background job processing for:
- Documentation crawling
- Embedding generation
- Graph synchronization
"""

from sibyl.jobs.queue import (
    JobStatus,
    enqueue_crawl,
    get_job_status,
    get_redis_settings,
)
from sibyl.jobs.worker import WorkerSettings, run_worker_async

__all__ = [
    "WorkerSettings",
    "enqueue_crawl",
    "get_job_status",
    "get_redis_settings",
    "run_worker_async",
    "JobStatus",
]
