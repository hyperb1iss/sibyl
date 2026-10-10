"""Amazon S3 plumbing shared by Sibyl's bucket-backed stores.

Validation receipts and backup archives both name a location as
``s3://bucket[/prefix][?region=name]`` and talk to it through one signed client
per region, so they share URL parsing, timeouts, retries and the reading of
S3's error answers.

boto3 is imported lazily. The server ships it through ``sibyl-core[s3]``; the
client CLI never opens a bucket and does not carry it.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

# S3 general purpose bucket names: 3-63 lowercase letters, digits, dots, hyphens.
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
# AWS region names, or "auto" for endpoints such as Cloudflare R2.
_REGION = re.compile(r"auto|[a-z0-9]+(?:-[a-z0-9]+)+")
# Store calls run on asyncio's default thread pool, which holds up to 32
# workers; botocore's default of 10 pooled connections would queue them.
MAX_POOL_CONNECTIONS = 32
# botocore defaults to 60 s connect and read timeouts, so an endpoint that
# accepts TCP and never answers held a pool thread for minutes. These bound
# one request; standard retries make at most ATTEMPTS of them, with jittered
# backoff of a few seconds between, and do not throttle healthy traffic.
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 10.0
ATTEMPTS = 3


class S3StoreUnavailable(OSError):
    """A bucket failed or refused a request, like an unavailable disk."""


@dataclass(frozen=True)
class S3Location:
    bucket: str
    prefix: str
    region: str | None

    def key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    @property
    def base(self) -> str:
        """The key prefix every name in this location starts with."""
        return f"{self.prefix}/" if self.prefix else ""

    def overlaps(self, other: S3Location) -> bool:
        """Whether either location's keys can fall inside the other's."""
        if self.bucket != other.bucket:
            return False
        return self.base.startswith(other.base) or other.base.startswith(self.base)


def parse_s3_url(url: str, *, subject: str) -> S3Location:
    """Parse ``s3://bucket[/prefix][?region=name]``, refusing anything else.

    ``subject`` names the store in error messages, e.g. "Validation receipt".
    """
    parts = urlsplit(url)
    if parts.scheme != "s3":
        raise ValueError(f"{subject} URL must use the s3:// scheme")
    if not _BUCKET.fullmatch(parts.netloc) or ".." in parts.netloc:
        raise ValueError(f"{subject} URL must name a valid S3 bucket")
    if parts.fragment:
        raise ValueError(f"{subject} URL may not carry a fragment")
    prefix = parts.path.strip("/")
    if prefix and any(segment in {"", ".", ".."} for segment in prefix.split("/")):
        raise ValueError(f"{subject} URL prefix may not contain empty or dot segments")
    region = None
    if parts.query:
        try:
            query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise ValueError(f"{subject} URL query is malformed") from None
        if set(query) != {"region"} or len(query["region"]) != 1:
            raise ValueError(f"{subject} URL accepts only one region query parameter")
        region = query["region"][0]
        if not _REGION.fullmatch(region):
            raise ValueError(f"{subject} URL region is malformed")
    return S3Location(bucket=parts.netloc, prefix=prefix, region=region)


CLIENTS: dict[str | None, tuple[Any, Any]] = {}
_CLIENTS_LOCK = threading.Lock()


def s3_clients(region: str | None) -> tuple[Any, Any]:
    """Signed and anonymous S3 clients for a region, built once per process.

    Credentials come from the default AWS chain (IRSA web identity on EKS,
    environment, profile, instance role). Without a region in the URL the
    standard AWS_REGION / AWS_DEFAULT_REGION resolution applies, and
    AWS_ENDPOINT_URL_S3 points both clients at an S3-compatible endpoint.
    """
    with _CLIENTS_LOCK:
        clients = CLIENTS.get(region)
        if clients is None:
            try:
                import boto3
                from botocore import UNSIGNED
                from botocore.config import Config
            except ImportError as error:
                raise S3StoreUnavailable(
                    "S3 storage needs boto3; install sibyl-core[s3] (sibyld ships it)"
                ) from error
            session = boto3.session.Session()
            # Conditional writes require Signature Version 4.
            signed = session.client(
                "s3",
                region_name=region,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=CONNECT_TIMEOUT,
                    read_timeout=READ_TIMEOUT,
                    retries={"mode": "standard", "total_max_attempts": ATTEMPTS},
                    max_pool_connections=MAX_POOL_CONNECTIONS,
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
            clients = CLIENTS[region] = (signed, anonymous)
        return clients


def transport_failure(error: BaseException) -> bool:
    """Whether the endpoint never answered, as opposed to answering or a local error."""
    from botocore.exceptions import ClientError, HTTPClientError
    from botocore.exceptions import ConnectionError as EndpointUnreachable

    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ClientError):
            return False
        if isinstance(cause, EndpointUnreachable | HTTPClientError):
            return True
        cause = cause.__cause__
    return False


def answer(error: BaseException) -> tuple[int | None, str]:
    """The HTTP status and S3 error code a failed request carried, if any."""
    response = getattr(error, "response", None) or {}
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status, str(response.get("Error", {}).get("Code", ""))


def describe(error: Exception) -> str:
    """Code and status only: a botocore message can echo request parameters."""
    status, code = answer(error)
    return f"{code or type(error).__name__}" + (f" ({status})" if status else "")


def precondition_failed(error: Exception) -> bool:
    status, code = answer(error)
    return status == 412 or code == "PreconditionFailed"


def conditional_conflict(error: Exception) -> bool:
    status, code = answer(error)
    return status == 409 or code == "ConditionalRequestConflict"
