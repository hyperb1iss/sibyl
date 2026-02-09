"""Async job queue using arq + Redis (via FalkorDB).

Provides background job processing for:
- Documentation crawling (crawl.py)
- Entity operations (entities.py)
- Agent execution (agents.py)
- Backup operations (backup.py)
- Pending entity registry (pending.py)

Job queue client is in queue.py, worker settings in worker.py.
"""

from sibyl.jobs.agents import (
    generate_status_hint,
    resume_agent_execution,
    run_agent_execution,
)
from sibyl.jobs.backup import (
    cleanup_old_backups,
    delete_backup,
    get_backup,
    list_backups,
    run_backup,
)
from sibyl.jobs.crawl import crawl_source, sync_all_sources, sync_source
from sibyl.jobs.entities import create_entity, create_learning_episode, update_entity, update_task
from sibyl.jobs.pending import (
    clear_pending,
    clear_pending_operations,
    get_pending_operations,
    is_pending,
    mark_pending,
    process_pending_operations,
    queue_pending_operation,
)
from sibyl.jobs.queue import (
    JobStatus,
    enqueue_agent_execution,
    enqueue_agent_resume,
    enqueue_backup,
    enqueue_backup_cleanup,
    enqueue_crawl,
    enqueue_create_entity,
    enqueue_create_learning_episode,
    enqueue_sync,
    enqueue_update_entity,
    enqueue_update_task,
    get_job_status,
    get_redis_settings,
)
from sibyl.jobs.worker import WorkerSettings, run_worker_async

__all__ = [
    # Worker
    "WorkerSettings",
    "run_worker_async",
    # Queue client
    "JobStatus",
    "get_job_status",
    "get_redis_settings",
    # Crawl queue
    "enqueue_crawl",
    "enqueue_sync",
    # Entity queue
    "enqueue_create_entity",
    "enqueue_create_learning_episode",
    "enqueue_update_entity",
    "enqueue_update_task",
    # Pending entity registry
    "mark_pending",
    "is_pending",
    "clear_pending",
    "queue_pending_operation",
    "get_pending_operations",
    "clear_pending_operations",
    "process_pending_operations",
    # Agent queue
    "enqueue_agent_execution",
    "enqueue_agent_resume",
    # Backup queue
    "enqueue_backup",
    "enqueue_backup_cleanup",
    # Job functions (for direct testing)
    "crawl_source",
    "sync_source",
    "sync_all_sources",
    "create_entity",
    "create_learning_episode",
    "update_entity",
    "update_task",
    "run_agent_execution",
    "resume_agent_execution",
    "generate_status_hint",
    # Backup
    "run_backup",
    "cleanup_old_backups",
    "list_backups",
    "get_backup",
    "delete_backup",
]
