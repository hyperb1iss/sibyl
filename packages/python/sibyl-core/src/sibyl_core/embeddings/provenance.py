"""Which model produced a stored vector, and how an unknown origin is recorded.

Every vector Sibyl stores sits beside an ``embedding_metadata`` object. A vector
counts as usable only while that object equals what the configured provider
would write today. Rows whose origin cannot be proven carry an explicit
unverified marker instead of no marker at all: no configured provider ever
produces it, so the embedding sweep replaces those vectors and the vector
lanes ignore them until it does.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

UNVERIFIED_EMBEDDING_PROVIDER = "unverified"
UNVERIFIED_ORIGIN_ARCHIVE = "archive_import"
UNVERIFIED_ORIGIN_LEGACY = "legacy_unrecorded"
UNVERIFIED_ORIGIN_OPERATOR = "operator_reembed"
UNVERIFIED_ORIGIN_REBUILD = "dimension_rebuild"

DOCUMENT_CHUNK_EMBEDDING_TEXT_VERSION = "document-chunk-v1"

# Provider errors that say "not now" rather than "not this input": throttling,
# quota, and the provider or its model being briefly unavailable. The sweep backs
# off from these; any other error means the request itself was refused.
_TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_ERROR_CODES = frozenset(
    {
        "throttlingexception",
        "toomanyrequestsexception",
        "rate_limit_exceeded",
        "ratelimitexceeded",
        "resource_exhausted",
        "servicequotaexceededexception",
        "serviceunavailableexception",
        "modelnotreadyexception",
        "internalserverexception",
        "internalfailure",
        "unavailable",
    }
)
_TRANSIENT_NAME_FRAGMENTS = (
    "ratelimit",
    "throttl",
    "toomanyrequests",
    "resourceexhausted",
    "serviceunavailable",
    "internalserver",
    "modelnotready",
    "apiconnection",
    "apitimeout",
    "connecterror",
    "connectionerror",
    "connectionreset",
    "readerror",
    "remoteprotocolerror",
    "timeout",
)


def unverified_embedding_metadata(origin: str) -> dict[str, str | int]:
    """Provenance for a vector whose producing model is not known.

    It never equals a provider's metadata, so a row carrying it is always
    work for the sweep and never a vector-lane candidate.
    """
    return {
        "provider": UNVERIFIED_EMBEDDING_PROVIDER,
        "model": UNVERIFIED_EMBEDDING_PROVIDER,
        "dimensions": 0,
        "origin": origin,
    }


def is_unverified_embedding_metadata(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("provider") == UNVERIFIED_EMBEDDING_PROVIDER


def document_chunk_embedding_metadata(
    *, provider: str, model: str, dimensions: int
) -> dict[str, str | int]:
    """Provenance stamped on a document chunk vector.

    Chunk writers and chunk queries resolve provider, model and dimensions
    from the same content embedding configuration, so both sides derive the
    stamp from those three values and the chunk text contract version.
    """
    return {
        "provider": provider,
        "model": model,
        "dimensions": int(dimensions),
        "text_version": DOCUMENT_CHUNK_EMBEDDING_TEXT_VERSION,
    }


def same_embedding_model(stamp: object, *, provider: str, model: str, dimensions: int) -> bool:
    """Whether a stamp from another plane names the same model and size.

    Planes stamp different text contracts, so cross-plane evidence compares
    only the fields that decide the vector space.
    """
    if not isinstance(stamp, Mapping):
        return False
    return (
        stamp.get("provider") == provider
        and stamp.get("model") == model
        and stamp.get("dimensions") == dimensions
    )


def mark_unverified_vector(
    container: MutableMapping[str, Any],
    *,
    has_vector: bool,
    origin: str = UNVERIFIED_ORIGIN_ARCHIVE,
    key: str = "embedding_metadata",
) -> bool:
    """Stamp a vector that arrived without provenance as unverified.

    Imports call this on the mapping that stores the row's provenance. A
    vector that already names its model keeps that stamp: the sweep compares
    it against the configured provider like any other row. Returns whether
    the mapping changed.
    """
    if not has_vector or isinstance(container.get(key), Mapping):
        return False
    container[key] = unverified_embedding_metadata(origin)
    return True


def is_transient_provider_error(exc: BaseException) -> bool:
    """Recognize throttling and brief provider unavailability across SDKs.

    OpenAI and httpx raise with ``status_code``, google-genai with ``code``,
    botocore with ``response['Error']['Code']``, all without importing any
    of them. Wrapped errors are unwrapped through their cause chain.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _looks_transient(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def _looks_transient(exc: BaseException) -> bool:
    # Refused, reset and aborted connections and any timeout are all "not now".
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    for attribute in ("status_code", "status", "code", "http_status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and value in _TRANSIENT_STATUS_CODES:
            return True
        if isinstance(value, str) and value.strip().lower() in _TRANSIENT_ERROR_CODES:
            return True
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        code = error.get("Code") if isinstance(error, Mapping) else None
        if isinstance(code, str) and code.strip().lower() in _TRANSIENT_ERROR_CODES:
            return True
    else:
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and status in _TRANSIENT_STATUS_CODES:
            return True
    name = type(exc).__name__.lower()
    return any(fragment in name for fragment in _TRANSIENT_NAME_FRAGMENTS)


__all__ = [
    "DOCUMENT_CHUNK_EMBEDDING_TEXT_VERSION",
    "UNVERIFIED_EMBEDDING_PROVIDER",
    "UNVERIFIED_ORIGIN_ARCHIVE",
    "UNVERIFIED_ORIGIN_LEGACY",
    "UNVERIFIED_ORIGIN_OPERATOR",
    "UNVERIFIED_ORIGIN_REBUILD",
    "document_chunk_embedding_metadata",
    "is_transient_provider_error",
    "is_unverified_embedding_metadata",
    "mark_unverified_vector",
    "same_embedding_model",
    "unverified_embedding_metadata",
]
