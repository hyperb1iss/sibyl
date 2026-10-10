"""Amazon S3 storage for organization backup archives shared by every replica.

Selected by ``SIBYL_BACKUP_ARCHIVE_URL=s3://bucket[/prefix][?region=name]``.
The backup job (API or worker) publishes ``<prefix>/sibyl_<backup id>.tar.gz``
once, and any API replica lists, streams and deletes it.

Writes are create-only and atomic. An archive up to one part is a single
PutObject; a larger one is a multipart upload, which S3 never lists or serves
until CompleteMultipartUpload succeeds. Both carry ``If-None-Match: *``, and
each object records its SHA-256 in user metadata, so a 412 answer is resolved
by comparing digests: the same digest is a retried write that already landed,
a different one is an :class:`ArchiveConflict`. A failed multipart upload is
aborted; one stranded by a dead endpoint expires under the bucket's
AbortIncompleteMultipartUpload lifecycle rule.

Archives are plaintext tarballs holding secrets, unlike receipts, so the
readiness check also refuses a bucket that serves objects to unsigned
requests. Client configuration (timeouts, retries, region, pool) is shared
with the receipt store through :mod:`sibyl_core.backends.s3_client`.
"""

from __future__ import annotations

import contextlib
import math
import secrets
from collections.abc import Iterator
from datetime import UTC
from pathlib import Path
from typing import Any

from sibyl_core.backends.archive_store import (
    CHUNK_SIZE,
    ArchiveConflict,
    ArchiveObject,
    ArchiveReader,
    ArchiveStoreUnavailable,
    check_name,
    retained_name,
)
from sibyl_core.backends.s3_client import (
    S3Location,
    answer,
    conditional_conflict,
    describe,
    parse_s3_url,
    precondition_failed,
    s3_clients,
    transport_failure,
)

# Parts are read into memory one at a time, so this bounds upload memory.
# S3 allows 10,000 parts; larger archives grow the part size to fit.
PART_SIZE = 16 * 1024 * 1024
_MAX_PARTS = 10_000
# A 409 from a conditional write means a concurrent operation on the key won
# the race; S3 documents retrying the whole upload.
_CREATE_ATTEMPTS = 3
_SHA256_METADATA = "sha256"
_CONTENT_TYPE = "application/gzip"


def parse_s3_archive_url(url: str) -> S3Location:
    """Parse ``s3://bucket[/prefix][?region=name]``, refusing anything else."""
    return parse_s3_url(url, subject="Backup archive")


class _ObjectReader:
    def __init__(self, body: Any, size: int) -> None:
        self._body = body
        self.size = size

    def chunks(self) -> Iterator[bytes]:
        try:
            yield from self._body.iter_chunks(CHUNK_SIZE)
        finally:
            self.close()

    def close(self) -> None:
        self._body.close()


class S3ArchiveStore:
    """Write-once archive objects named ``<prefix>/sibyl_<backup id>.tar.gz``."""

    def __init__(self, location: S3Location) -> None:
        self.s3_location = location
        self._client, self._anonymous = s3_clients(location.region)

    def _unavailable(self, action: str, error: Exception) -> ArchiveStoreUnavailable:
        return ArchiveStoreUnavailable(
            f"Backup archive bucket {self.s3_location.bucket} refused {action}: {describe(error)}"
        )

    def _key(self, name: str) -> str:
        return self.s3_location.key(check_name(name))

    def staging_dir(self) -> None:
        return None

    def location(self, name: str) -> str:
        return f"s3://{self.s3_location.bucket}/{self._key(name)}"

    # -- writes ---------------------------------------------------------------

    def _call(self, action: str, method: str, **params: Any) -> Any:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return getattr(self._client, method)(Bucket=self.s3_location.bucket, **params)
        except (BotoCoreError, ClientError) as error:
            raise self._unavailable(action, error) from error

    def _same_object(self, key: str, sha256: str, size: int) -> bool:
        """Resolve a 412: does the existing object hold exactly our bytes?"""
        head = self._call("HeadObject", "head_object", Key=key)
        metadata = {name.lower(): value for name, value in (head.get("Metadata") or {}).items()}
        return metadata.get(_SHA256_METADATA) == sha256 and head.get("ContentLength") == size

    def _resolve_existing(self, name: str, key: str, sha256: str, size: int) -> bool:
        if self._same_object(key, sha256, size):
            return False
        raise ArchiveConflict(f"{name} already holds a different archive")

    def _put_single(self, key: str, source: Path, sha256: str) -> None:
        self._client.put_object(
            Bucket=self.s3_location.bucket,
            Key=key,
            Body=source.read_bytes(),
            IfNoneMatch="*",
            ContentType=_CONTENT_TYPE,
            Metadata={_SHA256_METADATA: sha256},
        )

    def _abort(self, key: str, upload_id: str) -> None:
        with contextlib.suppress(Exception):
            self._client.abort_multipart_upload(
                Bucket=self.s3_location.bucket, Key=key, UploadId=upload_id
            )

    def _put_multipart(self, key: str, source: Path, sha256: str, size: int) -> None:
        part_size = max(PART_SIZE, math.ceil(size / _MAX_PARTS))
        upload_id = self._client.create_multipart_upload(
            Bucket=self.s3_location.bucket,
            Key=key,
            ContentType=_CONTENT_TYPE,
            Metadata={_SHA256_METADATA: sha256},
        )["UploadId"]
        try:
            parts = []
            with source.open("rb") as handle:
                number = 1
                while chunk := handle.read(part_size):
                    response = self._client.upload_part(
                        Bucket=self.s3_location.bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=number,
                        Body=chunk,
                    )
                    parts.append({"ETag": response["ETag"], "PartNumber": number})
                    number += 1
            self._client.complete_multipart_upload(
                Bucket=self.s3_location.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
                IfNoneMatch="*",
            )
        except BaseException as failure:
            # Best effort, and only while the endpoint still answers; an upload
            # stranded by a hung endpoint expires under the lifecycle rule.
            if not transport_failure(failure):
                self._abort(key, upload_id)
            raise

    def put_new(self, name: str, source: Path, *, sha256: str) -> bool:
        from botocore.exceptions import BotoCoreError, ClientError

        key = self._key(name)
        size = source.stat().st_size
        action = "PutObject" if size <= PART_SIZE else "CompleteMultipartUpload"
        attempt = 0
        while True:
            attempt += 1
            try:
                if size <= PART_SIZE:
                    self._put_single(key, source, sha256)
                else:
                    self._put_multipart(key, source, sha256, size)
                return True
            except ClientError as error:
                if precondition_failed(error):
                    return self._resolve_existing(name, key, sha256, size)
                if conditional_conflict(error) and attempt < _CREATE_ATTEMPTS:
                    continue
                raise self._unavailable(action, error) from error
            except BotoCoreError as error:
                raise self._unavailable(action, error) from error

    # -- reads ----------------------------------------------------------------

    def open(self, name: str) -> ArchiveReader | None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.get_object(Bucket=self.s3_location.bucket, Key=self._key(name))
        except ClientError as error:
            # Only a missing key is absence; NoSuchBucket or a bare 404 from a
            # wrong endpoint is an outage, not "no archive".
            if answer(error)[1] == "NoSuchKey":
                return None
            raise self._unavailable("GetObject", error) from error
        except BotoCoreError as error:
            raise self._unavailable("GetObject", error) from error
        return _ObjectReader(response["Body"], int(response["ContentLength"]))

    def archives(self) -> list[ArchiveObject]:
        base = self.s3_location.base
        found: list[ArchiveObject] = []
        params: dict[str, Any] = {"Prefix": base, "Delimiter": "/"}
        while True:
            page = self._call("ListObjectsV2", "list_objects_v2", **params)
            for item in page.get("Contents", ()):
                name = item["Key"][len(base) :]
                if not retained_name(name):
                    continue
                modified = item["LastModified"]
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=UTC)
                found.append(ArchiveObject(name=name, size=int(item["Size"]), modified=modified))
            if not page.get("IsTruncated"):
                return found
            params["ContinuationToken"] = page["NextContinuationToken"]

    def delete(self, name: str) -> None:
        self._call("DeleteObject", "delete_object", Key=self._key(name))

    # -- readiness ------------------------------------------------------------

    def _refuse_public_read(self, key: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._anonymous.get_object(Bucket=self.s3_location.bucket, Key=key)
        except (BotoCoreError, ClientError):
            return
        raise ArchiveStoreUnavailable(
            f"Backup archive bucket {self.s3_location.bucket} serves objects to unsigned requests; "
            'block public access and remove bucket policy statements that allow Principal "*" '
            "before writing backups, which hold secrets"
        )

    def ready(self) -> None:
        """Prove create-only writes and privacy before a backup does its work.

        Runs as each backup job begins. A probe object is created with
        If-None-Match, a second create must be refused, an unsigned read must
        fail, and the probe is deleted. An S3-compatible store that ignores
        If-None-Match, or a public bucket, stops the backup before any
        organization data is exported.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        key = self.s3_location.key(f".probe-{secrets.token_hex(16)}")
        token = secrets.token_bytes(32)

        def create(body: bytes) -> bool:
            try:
                self._client.put_object(
                    Bucket=self.s3_location.bucket, Key=key, Body=body, IfNoneMatch="*"
                )
            except ClientError as error:
                if precondition_failed(error):
                    return False
                raise self._unavailable("PutObject", error) from error
            except BotoCoreError as error:
                raise self._unavailable("PutObject", error) from error
            return True

        created = create(token)
        try:
            if not created:
                # A retried create that already landed answers 412 with our bytes.
                existing = self._call("GetObject", "get_object", Key=key)["Body"].read()
                if existing != token:
                    raise ArchiveStoreUnavailable("Backup archive probe collided with other data")
            if create(b"overwrite"):
                raise ArchiveStoreUnavailable(
                    f"Backup archive bucket {self.s3_location.bucket} ignored If-None-Match; "
                    "it cannot keep archives write-once"
                )
            self._refuse_public_read(key)
        except BaseException as failure:
            if not transport_failure(failure):
                with contextlib.suppress(Exception):
                    self._client.delete_object(Bucket=self.s3_location.bucket, Key=key)
            raise
        self._call("DeleteObject", "delete_object", Key=key)


__all__ = ["PART_SIZE", "S3ArchiveStore", "parse_s3_archive_url"]
