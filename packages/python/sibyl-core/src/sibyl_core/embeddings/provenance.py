"""Which model produced a stored vector, and how an unknown origin is recorded.

Every vector Sibyl stores sits beside an ``embedding_metadata`` object. Two
projections of it matter. The vector space (provider, model and dimensions)
decides whether a stored vector can be scored against a query vector. The
vector identity adds what else changes the vector for the same model (the
embedded text contract and whether the provider embeds documents and queries
differently); the embedding sweep replaces a vector whose identity differs
from what the configured provider writes today. Bookkeeping fields such as a
cache namespace or a token estimator never trigger either.

Rows whose origin cannot be proven carry an explicit unverified marker
instead of no marker at all: no configured provider ever produces it, so the
embedding sweep replaces those vectors and the vector lanes ignore them until
it does.
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

# Every stamp this release writes carries this marker, so code can tell a stamp
# it wrote from one the previous release left: only the latter is evidence of
# the model that preceded the upgrade, however early a new process wrote.
STAMP_VERSION_FIELD = "stamp_version"
EMBEDDING_STAMP_VERSION = 2

VECTOR_SPACE_FIELDS = ("provider", "model", "dimensions")
VECTOR_IDENTITY_FIELDS = (*VECTOR_SPACE_FIELDS, "text_version", "input_kind_sensitive")

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


EMBEDDING_STAMP_KEY = "embedding_metadata"


def without_client_embedding_stamp(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop an embedding stamp a caller supplied.

    Stamps are server-owned: only the code that produced a vector may say
    which model produced it. A client-supplied stamp on a row whose vector the
    server keeps would otherwise vouch for that vector, and the upgrade's
    evidence would believe it.
    """
    return {key: value for key, value in (metadata or {}).items() if key != EMBEDDING_STAMP_KEY}


def is_legacy_stamp(value: object) -> bool:
    """Whether a stamp was written before stamps carried a version."""
    return isinstance(value, Mapping) and value.get(STAMP_VERSION_FIELD) is None


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
    if provider == "bedrock":
        # The same model routed through us., global. or an inference-profile
        # ARN produces the same vectors, as the Bedrock provider records it.
        from sibyl_core.ai.bedrock import arn_model_id, remove_geo_prefix

        model = remove_geo_prefix(arn_model_id(model) or model)
    return {
        "provider": provider,
        "model": model,
        "dimensions": int(dimensions),
        "text_version": DOCUMENT_CHUNK_EMBEDDING_TEXT_VERSION,
        STAMP_VERSION_FIELD: EMBEDDING_STAMP_VERSION,
    }


def vector_space(stamp: object) -> dict[str, Any] | None:
    """The part of a stamp that decides which query vectors a stored vector can meet."""
    if not isinstance(stamp, Mapping):
        return None
    return {field: stamp.get(field) for field in VECTOR_SPACE_FIELDS}


def vector_identity(stamp: object) -> dict[str, Any] | None:
    """The part of a stamp that decides whether the sweep would replace the vector."""
    if not isinstance(stamp, Mapping):
        return None
    return {field: stamp.get(field) for field in VECTOR_IDENTITY_FIELDS}


def same_vector_space(left: object, right: object) -> bool:
    space = vector_space(left)
    return space is not None and space == vector_space(right)


def same_vector_identity(left: object, right: object) -> bool:
    """Whether two stamps describe vectors the sweep would not replace for each other."""
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    return all(left.get(field) == right.get(field) for field in VECTOR_IDENTITY_FIELDS)


def vector_space_predicate(path: str, param: str) -> str:
    """SurrealQL: the stamp at ``path`` is in the same vector space as ``$param``."""
    return "(" + " AND ".join(f"{path}.{f} = ${param}.{f}" for f in VECTOR_SPACE_FIELDS) + ")"


def vector_identity_differs_predicate(path: str, param: str) -> str:
    """SurrealQL: the stamp at ``path`` differs from ``$param`` in a field that shapes the vector.

    A field absent from both stamps compares as equal, so chunk stamps, which
    carry no provider input-kind flag, still compare cleanly.
    """
    return "(" + " OR ".join(f"{path}.{f} != ${param}.{f}" for f in VECTOR_IDENTITY_FIELDS) + ")"


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
    "EMBEDDING_STAMP_KEY",
    "EMBEDDING_STAMP_VERSION",
    "STAMP_VERSION_FIELD",
    "UNVERIFIED_EMBEDDING_PROVIDER",
    "UNVERIFIED_ORIGIN_ARCHIVE",
    "UNVERIFIED_ORIGIN_LEGACY",
    "UNVERIFIED_ORIGIN_OPERATOR",
    "UNVERIFIED_ORIGIN_REBUILD",
    "VECTOR_IDENTITY_FIELDS",
    "VECTOR_SPACE_FIELDS",
    "document_chunk_embedding_metadata",
    "is_legacy_stamp",
    "is_transient_provider_error",
    "is_unverified_embedding_metadata",
    "mark_unverified_vector",
    "same_vector_identity",
    "same_vector_space",
    "unverified_embedding_metadata",
    "vector_identity",
    "vector_identity_differs_predicate",
    "vector_space",
    "vector_space_predicate",
    "without_client_embedding_stamp",
]
