"""Amazon S3 storage for encrypted validation receipts shared across nodes.

Receipts reach this store already Fernet-encrypted with a per-execution key
held in the content database, so the bucket only ever holds ciphertext and
its own encryption settings carry no confidentiality claim. Every write is
create-only: PutObject carries ``If-None-Match: *``, and S3's 412 answer means
the receipt already exists, the same outcome a hard link onto an existing
file reports to the directory store.

URL parsing, the client factory and error reading live in
:mod:`sibyl_core.backends.s3_client`, shared with the backup archive store.
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sibyl_core.backends.s3_client import (
    MAX_POOL_CONNECTIONS,
    S3Location,
    S3StoreUnavailable,
    answer,
    conditional_conflict,
    describe,
    parse_s3_url,
    precondition_failed,
    s3_clients,
    transport_failure,
)

# S3 documents that a PutObject refused with 409 (a concurrent delete won the
# race) may be retried; any later attempt resolves to created or 412.
_CREATE_ATTEMPTS = 3

# The receipt store's names for the shared location type.
S3ReceiptLocation = S3Location

__all__ = [
    "ReceiptStoreUnavailable",
    "S3ReceiptLocation",
    "S3ReceiptStore",
    "parse_s3_receipt_url",
    "s3_clients",
]


class ReceiptStoreUnavailable(S3StoreUnavailable):
    """The receipt bucket failed or refused a request, like an unavailable disk."""


def parse_s3_receipt_url(url: str) -> S3Location:
    """Parse ``s3://bucket[/prefix][?region=name]``, refusing anything else."""
    return parse_s3_url(url, subject="Validation receipt")


class S3ReceiptStore:
    """Create-only receipt objects named ``<prefix>/<review digest>.receipt``."""

    def __init__(self, location: S3Location) -> None:
        self.location = location
        self._client, self._anonymous = s3_clients(location.region)

    def _unavailable(self, action: str, error: Exception) -> ReceiptStoreUnavailable:
        return ReceiptStoreUnavailable(
            f"Validation receipt bucket {self.location.bucket} refused {action}: {describe(error)}"
        )

    def get(self, name: str) -> bytes | None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.get_object(
                Bucket=self.location.bucket, Key=self.location.key(name)
            )
            return response["Body"].read()
        except ClientError as error:
            # Only a missing key is absence. NoSuchBucket, or a bare 404 from a
            # wrong endpoint, must not read as "no receipt".
            if answer(error)[1] == "NoSuchKey":
                return None
            raise self._unavailable("GetObject", error) from error
        except BotoCoreError as error:
            raise self._unavailable("GetObject", error) from error

    def _present(self, wanted: set[str]) -> set[str]:
        """Names in ``wanted`` that exist under the prefix, from one paged listing."""
        from botocore.exceptions import BotoCoreError, ClientError

        base = f"{self.location.prefix}/" if self.location.prefix else ""
        present: set[str] = set()
        params: dict[str, Any] = {"Bucket": self.location.bucket, "Prefix": base}
        while True:
            try:
                page = self._client.list_objects_v2(**params)
            except (BotoCoreError, ClientError) as error:
                raise self._unavailable("ListObjectsV2", error) from error
            for item in page.get("Contents", ()):
                name = item["Key"][len(base) :]
                if name in wanted:
                    present.add(name)
            if not page.get("IsTruncated"):
                return present
            params["ContinuationToken"] = page["NextContinuationToken"]

    def get_many(self, names: Iterable[str]) -> dict[str, bytes | None]:
        """Fetch many receipts: list the prefix once, then read present keys concurrently.

        Most executions keep their key long after their receipt was discarded,
        so a backup asks about far more receipts than exist. Listing first turns
        a GetObject per execution into one request per thousand keys, and the
        remaining reads share the client's whole connection pool.
        """
        wanted = set(names)
        found: dict[str, bytes | None] = dict.fromkeys(wanted)
        present = sorted(self._present(wanted)) if wanted else []
        if present:
            with ThreadPoolExecutor(max_workers=min(MAX_POOL_CONNECTIONS, len(present))) as pool:
                found.update(zip(present, pool.map(self.get, present), strict=True))
        return found

    def put_new(self, name: str, ciphertext: bytes) -> bool:
        """Create the object unless the key exists; False means it already did."""
        from botocore.exceptions import BotoCoreError, ClientError

        attempt = 0
        while True:
            attempt += 1
            try:
                self._client.put_object(
                    Bucket=self.location.bucket,
                    Key=self.location.key(name),
                    Body=ciphertext,
                    IfNoneMatch="*",
                    ContentType="application/octet-stream",
                )
                return True
            except ClientError as error:
                if precondition_failed(error):
                    return False
                if conditional_conflict(error) and attempt < _CREATE_ATTEMPTS:
                    continue
                raise self._unavailable("PutObject", error) from error
            except BotoCoreError as error:
                raise self._unavailable("PutObject", error) from error

    def delete(self, name: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client.delete_object(Bucket=self.location.bucket, Key=self.location.key(name))
        except (BotoCoreError, ClientError) as error:
            raise self._unavailable("DeleteObject", error) from error

    def _refuse_public_read(self, name: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._anonymous.get_object(Bucket=self.location.bucket, Key=self.location.key(name))
        except (BotoCoreError, ClientError):
            return
        raise ReceiptStoreUnavailable(
            f"Validation receipt bucket {self.location.bucket} serves objects to unsigned "
            "requests; block public access and remove bucket policy statements that allow "
            'Principal "*" before running validation'
        )

    def _listed(self, name: str) -> bool:
        from botocore.exceptions import BotoCoreError, ClientError

        key = self.location.key(name)
        try:
            page = self._client.list_objects_v2(Bucket=self.location.bucket, Prefix=key)
        except (BotoCoreError, ClientError) as error:
            raise self._unavailable("ListObjectsV2", error) from error
        return any(item["Key"] == key for item in page.get("Contents", ()))

    def _require_absent(self, name: str) -> None:
        try:
            found = self.get(name)
        except ReceiptStoreUnavailable as error:
            if answer(error.__cause__ or error)[0] != 403:
                raise
            raise ReceiptStoreUnavailable(
                f"{error}; a missing receipt must read as absent, so grant s3:ListBucket "
                "on the receipt bucket"
            ) from error
        if found is not None:
            raise ReceiptStoreUnavailable(f"Validation receipt probe {name} reads as present")

    def ready(self) -> None:
        """Prove deletion, absence, create-only writes, reads and privacy.

        Runs once as each validation execution begins, before its first model
        call. Deleting a key that was never written proves s3:DeleteObject
        without creating anything, so a role that cannot delete never leaks a
        probe. A second never-written key must read as absent rather than
        denied, which on AWS needs s3:ListBucket; a deleted key cannot prove
        that, because a versioned bucket answers 404 for its delete marker
        even without the permission. Then a probe object is created with
        If-None-Match, refused a second create, read back, found by a listing,
        refused to an unsigned client, deleted, and must read as absent.
        """
        probe = f".probe-{secrets.token_hex(16)}"
        token = secrets.token_bytes(32)
        self.delete(f"{probe}.unwritten")
        self._require_absent(f"{probe}.absent")
        # When the create fails there is nothing of ours to remove, and a
        # timed-out endpoint must not be waited out again by a delete.
        created = self.put_new(probe, token)
        try:
            # A retried create that already landed answers 412 with our own bytes.
            if not created and self.get(probe) != token:
                raise ReceiptStoreUnavailable("Validation receipt probe collided with other data")
            if self.put_new(probe, b"overwrite"):
                raise ReceiptStoreUnavailable(
                    f"Validation receipt bucket {self.location.bucket} ignored If-None-Match; "
                    "it cannot keep receipts immutable"
                )
            if self.get(probe) != token:
                raise ReceiptStoreUnavailable("Validation receipt probe read back different bytes")
            # Backup export finds receipts by listing, so a store whose listing
            # lags its writes would record a pending receipt as missing.
            if not self._listed(probe):
                raise ReceiptStoreUnavailable(
                    f"Validation receipt bucket {self.location.bucket} does not list an object "
                    "it just stored; backups would miss pending receipts"
                )
            self._refuse_public_read(probe)
        except BaseException as failure:
            # Best effort, and only while the endpoint still answers: a probe
            # left behind by a hung endpoint expires under the lifecycle rule.
            if not transport_failure(failure):
                with contextlib.suppress(Exception):
                    self.delete(probe)
            raise
        self.delete(probe)
        self._require_absent(probe)
