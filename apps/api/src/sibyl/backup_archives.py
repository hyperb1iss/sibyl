"""The configured backup archive store.

Every process that writes or serves organization backup archives (API
replicas and workers) opens the same store: the S3 prefix named by
``SIBYL_BACKUP_ARCHIVE_URL`` when set, otherwise ``SIBYL_BACKUP_DIR``, which
must then be a volume they all mount.
"""

from __future__ import annotations

from sibyl.config import settings
from sibyl_core.backends.archive_store import ArchiveStore, DirectoryArchiveStore


def backup_archive_store() -> ArchiveStore:
    if settings.backup_archive_url:
        from sibyl_core.backends.s3_archive_store import S3ArchiveStore, parse_s3_archive_url

        return S3ArchiveStore(parse_s3_archive_url(settings.backup_archive_url))
    return DirectoryArchiveStore(settings.backup_dir)


__all__ = ["backup_archive_store"]
