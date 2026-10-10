"""Amazon S3 storage for encrypted validation receipts shared across nodes.

Receipts reach this store already Fernet-encrypted with a per-execution key
held in the content database, so the bucket only ever holds ciphertext and
its own encryption settings carry no confidentiality claim. Every write is
create-only: PutObject carries ``If-None-Match: *``, and S3's 412 answer means
the receipt already exists, the same outcome a hard link onto an existing
file reports to the directory store.

boto3 is imported lazily. The server ships it through ``sibyl-core[s3]``; the
client CLI never opens this store and does not carry it.
"""

from __future__ import annotations

import contextlib
import re
import secrets
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

# S3 general purpose bucket names: 3-63 lowercase letters, digits, dots, hyphens.
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_REGION = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")
# S3 documents that a PutObject refused with 409 (a concurrent delete won the
# race) may be retried; any later attempt resolves to created or 412.
_CREATE_ATTEMPTS = 3
# Receipt calls run on asyncio's default thread pool, which holds up to 32
# workers; botocore's default of 10 pooled connections would queue them.
_MAX_POOL_CONNECTIONS = 32


class ReceiptStoreUnavailable(OSError):
    """The receipt bucket failed or refused a request, like an unavailable disk."""


@dataclass(frozen=True)
class S3ReceiptLocation:
    bucket: str
    prefix: str
    region: str | None

    def key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name


def parse_s3_receipt_url(url: str) -> S3ReceiptLocation:
    """Parse ``s3://bucket[/prefix][?region=name]``, refusing anything else."""
    parts = urlsplit(url)
    if parts.scheme != "s3":
        raise ValueError("Validation receipt URL must use the s3:// scheme")
    if not _BUCKET.fullmatch(parts.netloc) or ".." in parts.netloc:
        raise ValueError("Validation receipt URL must name a valid S3 bucket")
    if parts.fragment:
        raise ValueError("Validation receipt URL may not carry a fragment")
    prefix = parts.path.strip("/")
    if prefix and any(segment in {"", ".", ".."} for segment in prefix.split("/")):
        raise ValueError("Validation receipt URL prefix may not contain empty or dot segments")
    region = None
    if parts.query:
        try:
            query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise ValueError("Validation receipt URL query is malformed") from None
        if set(query) != {"region"} or len(query["region"]) != 1:
            raise ValueError("Validation receipt URL accepts only one region query parameter")
        region = query["region"][0]
        if not _REGION.fullmatch(region):
            raise ValueError("Validation receipt URL region is malformed")
    return S3ReceiptLocation(bucket=parts.netloc, prefix=prefix, region=region)


_CLIENTS: dict[str | None, tuple[Any, Any]] = {}
_CLIENTS_LOCK = threading.Lock()


def s3_clients(region: str | None) -> tuple[Any, Any]:
    """Signed and anonymous S3 clients for a region, built once per process.

    Credentials come from the default AWS chain (IRSA web identity on EKS,
    environment, profile, instance role). Without a region in the URL the
    standard AWS_REGION / AWS_DEFAULT_REGION resolution applies, and
    AWS_ENDPOINT_URL_S3 points both clients at an S3-compatible endpoint.
    """
    with _CLIENTS_LOCK:
        clients = _CLIENTS.get(region)
        if clients is None:
            try:
                import boto3
                from botocore import UNSIGNED
                from botocore.config import Config
            except ImportError as error:
                raise ReceiptStoreUnavailable(
                    "S3 validation receipts need boto3; install sibyl-core[s3] (sibyld ships it)"
                ) from error
            session = boto3.session.Session()
            # Conditional writes require Signature Version 4.
            signed = session.client(
                "s3",
                region_name=region,
                config=Config(
                    signature_version="s3v4",
                    retries={"mode": "standard"},
                    max_pool_connections=_MAX_POOL_CONNECTIONS,
                ),
            )
            # Only asks whether a probe is publicly readable; never retried.
            anonymous = session.client(
                "s3",
                region_name=region,
                config=Config(
                    signature_version=UNSIGNED,
                    connect_timeout=2,
                    read_timeout=2,
                    retries={"total_max_attempts": 1},
                ),
            )
            clients = _CLIENTS[region] = (signed, anonymous)
        return clients


def _answer(error: BaseException) -> tuple[int | None, str]:
    response = getattr(error, "response", None) or {}
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status, str(response.get("Error", {}).get("Code", ""))


class S3ReceiptStore:
    """Create-only receipt objects named ``<prefix>/<review digest>.receipt``."""

    def __init__(self, location: S3ReceiptLocation) -> None:
        self.location = location
        self._client, self._anonymous = s3_clients(location.region)

    def _unavailable(self, action: str, error: Exception) -> ReceiptStoreUnavailable:
        status, code = _answer(error)
        detail = f"{code or type(error).__name__}" + (f" ({status})" if status else "")
        return ReceiptStoreUnavailable(
            f"Validation receipt bucket {self.location.bucket} refused {action}: {detail}"
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
            if _answer(error)[1] == "NoSuchKey":
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
            with ThreadPoolExecutor(max_workers=min(_MAX_POOL_CONNECTIONS, len(present))) as pool:
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
                status, code = _answer(error)
                if status == 412 or code == "PreconditionFailed":
                    return False
                conflict = status == 409 or code == "ConditionalRequestConflict"
                if conflict and attempt < _CREATE_ATTEMPTS:
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

    def ready(self) -> None:
        """Prove create-only writes, reads, privacy and deletion before dispatch.

        A probe object is created with If-None-Match, refused a second create,
        read back, refused to an unsigned client, deleted, and then must read
        as absent. A key that was never written must also read as absent
        rather than denied, which on AWS needs s3:ListBucket. A deleted key
        alone cannot prove that: on a versioned bucket its delete marker
        answers 404 even without the permission.
        """
        probe = f".probe-{secrets.token_hex(16)}"
        token = secrets.token_bytes(32)
        try:
            # A retried create that already landed answers 412 with our own bytes.
            if not self.put_new(probe, token) and self.get(probe) != token:
                raise ReceiptStoreUnavailable("Validation receipt probe collided with other data")
            if self.put_new(probe, b"overwrite"):
                raise ReceiptStoreUnavailable(
                    f"Validation receipt bucket {self.location.bucket} ignored If-None-Match; "
                    "it cannot keep receipts immutable"
                )
            if self.get(probe) != token:
                raise ReceiptStoreUnavailable("Validation receipt probe read back different bytes")
            self._refuse_public_read(probe)
        except BaseException:
            with contextlib.suppress(Exception):
                self.delete(probe)
            raise
        self.delete(probe)
        for name in (probe, f"{probe}.absent"):
            try:
                found = self.get(name)
            except ReceiptStoreUnavailable as error:
                if _answer(error.__cause__ or error)[0] != 403:
                    raise
                raise ReceiptStoreUnavailable(
                    f"{error}; a missing receipt must read as absent, so grant s3:ListBucket "
                    "on the receipt bucket"
                ) from error
            if found is not None:
                raise ReceiptStoreUnavailable(f"Validation receipt probe {name} reads as present")
