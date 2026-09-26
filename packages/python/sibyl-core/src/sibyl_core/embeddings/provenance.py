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

# How a provider error bears on the rows in the request that failed:
#  - input_rejected: the provider judged this request's content unacceptable.
#    Only this kind may blame rows, and only once they stand alone.
#  - transient: "not now" (throttling, quota, the model or service briefly
#    failing). Back off or end the pass; the same rows can succeed later.
#  - provider_fault: credentials, access, a missing model, or an error that
#    says nothing about the input. End the pass; no row is to blame.
PROVIDER_ERROR_INPUT = "input_rejected"
PROVIDER_ERROR_TRANSIENT = "transient"
PROVIDER_ERROR_FAULT = "provider_fault"

# HTTP statuses that decide the kind when the error names no type. Bedrock's
# ModelErrorException is 424 and its ModelTimeoutException 408.
_TRANSIENT_STATUS_CODES = frozenset({408, 409, 424, 425, 429, 500, 502, 503, 504})
_FAULT_STATUS_CODES = frozenset({401, 403, 404, 405})
_INPUT_STATUS_CODES = frozenset({400, 413, 422})
# Error names and codes across SDKs, lowercased: botocore's
# ``Error.Code``, Bedrock's ``x-amzn-ErrorType``, OpenAI's ``code`` and
# google-genai's ``status``. A name decides before a status does: Bedrock
# answers both a malformed input and an exhausted quota with 400.
_INPUT_ERROR_CODES = frozenset(
    {
        "validationexception",
        "context_length_exceeded",
        "string_above_max_length",
        "invalid_argument",
    }
)
_TRANSIENT_ERROR_CODES = frozenset(
    {
        "throttlingexception",
        "toomanyrequestsexception",
        "rate_limit_exceeded",
        "ratelimitexceeded",
        "resource_exhausted",
        "insufficient_quota",
        "servicequotaexceededexception",
        "serviceunavailableexception",
        "modelnotreadyexception",
        "modelerrorexception",
        "modeltimeoutexception",
        "modelstreamerrorexception",
        "internalserverexception",
        "internalfailure",
        "unavailable",
        "deadline_exceeded",
        "internal",
    }
)
_MODEL_PROCESSING_ERROR_CODES = frozenset(
    {
        "modelerrorexception",
        "modeltimeoutexception",
        "modelstreamerrorexception",
        "internalserverexception",
        "internalfailure",
        "internal",
    }
)
_MODEL_PROCESSING_STATUS_CODES = frozenset({408, 424, 500})
_FAULT_ERROR_CODES = frozenset(
    {
        "accessdeniedexception",
        "unrecognizedclientexception",
        "expiredtokenexception",
        "invalidsignatureexception",
        "incompletesignatureexception",
        "missingauthenticationtokenexception",
        "resourcenotfoundexception",
        "unauthorizedexception",
        "permission_denied",
        "unauthenticated",
        "not_found",
        "invalid_api_key",
        "model_not_found",
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


def vector_space_predicate(path: str, param: str, *, admit_unstamped: bool = False) -> str:
    """SurrealQL: the stamp at ``path`` is in the same vector space as ``$param``.

    With ``admit_unstamped``, a row with no stamp matches too, for a plane
    whose unstamped vectors count as the configured model while adoption
    stamps them. A bare AND chain, never parenthesized and never OR: callers
    AND-join it, and the embedded engine silently drops a parenthesized
    group inside an HNSW bracket, which would empty every vector lane there.
    """
    if admit_unstamped:
        return " AND ".join(
            f"[${param}.{f}, NONE] CONTAINS {path}.{f}" for f in VECTOR_SPACE_FIELDS
        )
    return " AND ".join(f"{path}.{f} = ${param}.{f}" for f in VECTOR_SPACE_FIELDS)


def vector_identity_differs_predicate(path: str, param: str) -> str:
    """SurrealQL: the stamp at ``path`` differs from ``$param`` in a field that shapes the vector.

    A field absent from both stamps compares as equal, so chunk stamps, which
    carry no provider input-kind flag, still compare cleanly. A stored NULL
    counts as absent on either side: the previous release could keep a
    client's null, NULL never equals NONE, and a Python None arrives as NONE.
    """
    return (
        "("
        + " OR ".join(
            f"({path}.{f} ?? NONE) != (${param}.{f} ?? NONE)" for f in VECTOR_IDENTITY_FIELDS
        )
        + ")"
    )


# The fields a write fences on to tell that a stamp it read is still the one
# stored: what shapes the vector, and whether the stamp is in the previous
# release's format.
OBSERVED_STAMP_FIELDS = (*VECTOR_IDENTITY_FIELDS, STAMP_VERSION_FIELD)


def stamp_unchanged_predicate(path: str, observed: str) -> str:
    """SurrealQL: the stamp at ``path`` still matches ``observed`` (an expression) where it counts.

    Compared field by field rather than as whole objects: a stored NULL inside
    a stamp reads back as None and is sent as NONE, so whole-object equality
    never holds again and the write would be refused on every pass. A NULL or
    missing stamp matches a missing one. Another model's write changes one of
    these fields, so the write still loses that race.
    """
    return " AND ".join(
        f"({path}.{f} ?? NONE) = ({observed}.{f} ?? NONE)" for f in OBSERVED_STAMP_FIELDS
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


def provider_error_kind(exc: BaseException) -> str:
    """Classify an embedding provider error by what it says about the input.

    Returns ``PROVIDER_ERROR_INPUT``, ``PROVIDER_ERROR_TRANSIENT`` or
    ``PROVIDER_ERROR_FAULT``. Each error in the cause chain is read in turn,
    outermost first, and the first one that says anything decides: its error
    name or code, then its HTTP status, then its type (a timeout or a dropped
    connection). OpenAI and httpx carry ``status_code``, google-genai
    ``code`` and ``status``, botocore ``response['Error']['Code']`` and
    ``ResponseMetadata.HTTPStatusCode``, and Bedrock embedding errors
    ``error_type``, all read without importing any of them. An error that
    says nothing, such as missing credentials or a malformed response, is a
    provider fault: nothing in it blames the input.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        kind = _error_kind(current)
        if kind is not None:
            return kind
        current = current.__cause__ or current.__context__
    return PROVIDER_ERROR_FAULT


def is_transient_provider_error(exc: BaseException) -> bool:
    """Whether a provider error says "not now", so the caller should back off."""
    return provider_error_kind(exc) == PROVIDER_ERROR_TRANSIENT


def is_model_processing_error(exc: BaseException) -> bool:
    """Whether a transient error is the model failing on this request, not a capacity signal.

    Bedrock's ModelErrorException (424) and ModelTimeoutException (408) and a
    provider's internal error (500) can follow one input around, so a caller
    may split the request to find it. Throttling, quota and unavailability
    never can: splitting only sends more requests the provider is turning
    away. Neither kind blames the input.
    """
    if not is_transient_provider_error(exc):
        return False
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        codes = _error_codes(current)
        if any(code in _MODEL_PROCESSING_ERROR_CODES for code in codes):
            return True
        if any(code in _TRANSIENT_ERROR_CODES for code in codes):
            return False
        status = _error_status(current)
        if status is not None:
            return status in _MODEL_PROCESSING_STATUS_CODES
        current = current.__cause__ or current.__context__
    return False


def provider_error_status(exc: BaseException) -> int | None:
    """The HTTP status a provider error carries anywhere in its cause chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = _error_status(current)
        if status is not None:
            return status
        current = current.__cause__ or current.__context__
    return None


def _error_codes(exc: BaseException) -> list[str]:
    codes = [
        value
        for attribute in ("error_type", "code", "status")
        if isinstance(value := getattr(exc, attribute, None), str)
    ]
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        code = error.get("Code") if isinstance(error, Mapping) else None
        if isinstance(code, str):
            codes.append(code)
    return [code.strip().lower() for code in codes if code.strip()]


def _error_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    metadata = response.get("ResponseMetadata") if isinstance(response, Mapping) else None
    for value in (
        getattr(exc, "status_code", None),
        getattr(exc, "http_status", None),
        getattr(exc, "code", None),
        getattr(exc, "status", None),
        None if isinstance(response, Mapping) else getattr(response, "status_code", None),
        metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None,
    ):
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value < 600:
            return value
    return None


def _error_kind(exc: BaseException) -> str | None:
    for code in _error_codes(exc):
        if code in _INPUT_ERROR_CODES:
            return PROVIDER_ERROR_INPUT
        if code in _TRANSIENT_ERROR_CODES:
            return PROVIDER_ERROR_TRANSIENT
        if code in _FAULT_ERROR_CODES:
            return PROVIDER_ERROR_FAULT
    status = _error_status(exc)
    if status is not None:
        if status in _TRANSIENT_STATUS_CODES or status >= 500:
            return PROVIDER_ERROR_TRANSIENT
        if status in _INPUT_STATUS_CODES:
            return PROVIDER_ERROR_INPUT
        if status in _FAULT_STATUS_CODES or 400 <= status < 500:
            return PROVIDER_ERROR_FAULT
    # Refused, reset and aborted connections and any timeout are all "not now".
    if isinstance(exc, ConnectionError | TimeoutError):
        return PROVIDER_ERROR_TRANSIENT
    name = type(exc).__name__.lower()
    if any(fragment in name for fragment in _TRANSIENT_NAME_FRAGMENTS):
        return PROVIDER_ERROR_TRANSIENT
    return None


__all__ = [
    "DOCUMENT_CHUNK_EMBEDDING_TEXT_VERSION",
    "EMBEDDING_STAMP_KEY",
    "EMBEDDING_STAMP_VERSION",
    "OBSERVED_STAMP_FIELDS",
    "PROVIDER_ERROR_FAULT",
    "PROVIDER_ERROR_INPUT",
    "PROVIDER_ERROR_TRANSIENT",
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
    "is_model_processing_error",
    "is_transient_provider_error",
    "is_unverified_embedding_metadata",
    "mark_unverified_vector",
    "provider_error_kind",
    "provider_error_status",
    "same_vector_identity",
    "same_vector_space",
    "stamp_unchanged_predicate",
    "unverified_embedding_metadata",
    "vector_identity",
    "vector_identity_differs_predicate",
    "vector_space",
    "vector_space_predicate",
    "without_client_embedding_stamp",
]
