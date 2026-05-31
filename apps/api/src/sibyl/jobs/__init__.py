"""Async job queue using arq + Redis/Valkey.

Provides background job processing for:
- Documentation crawling (crawl.py)
- Entity operations (entities.py)
- Backup operations (backup.py)
- Pending entity registry (pending.py)

Job queue client is in queue.py, worker settings in worker.py.
"""

from sibyl.jobs.backup import (
    cleanup_old_backups,
    delete_backup,
    get_backup,
    list_backups,
    run_backup,
)
from sibyl.jobs.crawl import crawl_source, sync_all_sources, sync_source
from sibyl.jobs.entities import (
    create_entity,
    create_learning_episode,
    create_learning_procedure,
    project_memory_batch,
    update_entity,
    update_task,
)
from sibyl.jobs.memory_extraction import (
    enqueue_memory_extraction_batches,
    extract_memory_entities,
)
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
    enqueue_backup,
    enqueue_backup_cleanup,
    enqueue_consolidation,
    enqueue_crawl,
    enqueue_create_entity,
    enqueue_create_learning_episode,
    enqueue_create_learning_procedure,
    enqueue_memory_extraction,
    enqueue_memory_projection,
    enqueue_priority_decay,
    enqueue_raw_capture_changefeed_poll,
    enqueue_raw_promotion,
    enqueue_reflection_dream_cycle,
    enqueue_source_import_drain,
    enqueue_sync,
    enqueue_update_entity,
    enqueue_update_task,
    get_job_status,
    get_redis_settings,
)
from sibyl.jobs.raw_changefeed import (
    poll_all_raw_capture_changefeeds,
    poll_raw_capture_changefeed,
)
from sibyl.jobs.raw_promotion import promote_raw_captures
from sibyl.jobs.reflection import run_reflection_dream_cycle, run_reflection_dream_cycle_all_orgs
from sibyl.jobs.source_imports import drain_source_import, import_source_archive
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
    "enqueue_create_learning_procedure",
    "enqueue_memory_extraction",
    "enqueue_memory_extraction_batches",
    "enqueue_memory_projection",
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
    # Backup queue
    "enqueue_backup",
    "enqueue_backup_cleanup",
    "enqueue_consolidation",
    "enqueue_priority_decay",
    "enqueue_raw_capture_changefeed_poll",
    "enqueue_raw_promotion",
    "enqueue_reflection_dream_cycle",
    "enqueue_source_import_drain",
    # Job functions (for direct testing)
    "crawl_source",
    "sync_source",
    "sync_all_sources",
    "drain_source_import",
    "import_source_archive",
    "promote_raw_captures",
    "poll_all_raw_capture_changefeeds",
    "poll_raw_capture_changefeed",
    "create_entity",
    "create_learning_episode",
    "create_learning_procedure",
    "extract_memory_entities",
    "project_memory_batch",
    "update_entity",
    "update_task",
    "run_reflection_dream_cycle",
    "run_reflection_dream_cycle_all_orgs",
    # Backup
    "run_backup",
    "cleanup_old_backups",
    "list_backups",
    "get_backup",
    "delete_backup",
]
