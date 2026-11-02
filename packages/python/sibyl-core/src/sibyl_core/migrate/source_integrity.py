"""Complete, typed source provenance sections for privileged application archives."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, SupportsIndex

from sibyl_core.memory_pipeline.observations import SourceKind

INTEGRITY_ARCHIVE_VERSION = 1


class ArchiveDatetime(datetime):
    """Keep native archive precision without changing evidence hash date formatting."""

    native_text: str

    @classmethod
    def parse(cls, text: str) -> ArchiveDatetime:
        parsed = datetime.fromisoformat(text)
        value = cls(
            parsed.year,
            parsed.month,
            parsed.day,
            parsed.hour,
            parsed.minute,
            parsed.second,
            parsed.microsecond,
            tzinfo=parsed.tzinfo,
            fold=parsed.fold,
        )
        value.native_text = text
        return value

    def __reduce_ex__(self, protocol: SupportsIndex, /):
        return self.parse, (self.native_text,)


def native_archive_parameters(value: Any) -> Any:
    """Pass archive dates through the SDK's lossless native datetime encoder."""
    from surrealdb.data.types.datetime import Datetime

    if isinstance(value, ArchiveDatetime):
        # The native CBOR datetime string requires UTC Z notation. Keep the
        # fractional remainder that Python's microsecond datetime cannot hold.
        fraction = re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,](\d+)", value.native_text)
        digits = fraction.group(1) if fraction else ""
        if len(digits) > 9:
            raise ValueError("archive datetime exceeds native nanosecond precision")
        parsed = datetime.fromisoformat(value.native_text)
        utc = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        nanoseconds = utc.microsecond * 1000 + int((digits[6:] + "000")[:3])
        seconds = utc.replace(microsecond=0).isoformat(timespec="seconds").removesuffix("+00:00")
        return Datetime(f"{seconds}.{nanoseconds:09d}Z")
    if isinstance(value, dict):
        return {key: native_archive_parameters(item) for key, item in value.items()}
    if isinstance(value, list):
        return [native_archive_parameters(item) for item in value]
    return value


def encode_record(record: dict[str, Any]) -> dict[str, Any]:
    """Preserve database datetimes without interpreting user strings as dates."""
    datetimes: list[list[str | int]] = []

    def encode(value: Any, path: list[str | int]) -> Any:
        if isinstance(value, datetime):
            datetimes.append(path)
            return value.native_text if isinstance(value, ArchiveDatetime) else value.isoformat()
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("archive record keys must be strings")
            return {key: encode(item, [*path, key]) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [encode(item, [*path, index]) for index, item in enumerate(value)]
        if value is None or isinstance(value, str | int | float | bool):
            return value
        raise ValueError(f"unsupported archive value type: {type(value).__name__}")

    return {"record": encode(record, []), "datetimes": datetimes}


def decode_record(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"record", "datetimes"}:
        raise ValueError("archive source row requires record and datetimes")
    if not isinstance(payload["record"], dict) or not isinstance(payload["datetimes"], list):
        raise ValueError("invalid typed archive record")
    record = deepcopy(payload["record"])
    for path in payload["datetimes"]:
        if not isinstance(path, list) or not path:
            raise ValueError("invalid archive datetime path")
        parent: Any = record
        for index, key in enumerate(path):
            if isinstance(parent, dict):
                valid = type(key) is str and key in parent
            elif isinstance(parent, list):
                valid = type(key) is int and 0 <= key < len(parent)
            else:
                valid = False
            if not valid:
                raise ValueError("invalid archive datetime path component")
            if index == len(path) - 1:
                if not isinstance(parent[key], str):
                    raise ValueError("archive datetime path must identify a string")
                parent[key] = ArchiveDatetime.parse(parent[key])
            else:
                parent = parent[key]
    return record


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


class SectionDigest:
    """A section's digest, fed one encoded source row at a time.

    It equals ``_digest`` of the assembled section. Canonical JSON sorts the
    keys, so derivations, kind and organizations precede the rows, and the
    states and version follow them.
    """

    def __init__(
        self, *, kind: SourceKind, organizations: list[str], derivations: list[dict[str, Any]]
    ) -> None:
        self._hash = hashlib.sha256()
        self._rows = 0
        self._update(
            '{"derivations":'
            + _canonical(derivations)
            + ',"kind":'
            + _canonical(kind.value)
            + ',"organizations":'
            + _canonical(sorted(organizations))
            + ',"source_rows":['
        )

    def _update(self, text: str) -> None:
        self._hash.update(text.encode())

    def add_row(self, encoded_row: dict[str, Any]) -> None:
        self._update(("," if self._rows else "") + _canonical(encoded_row))
        self._rows += 1

    def finish(self, states: list[dict[str, Any]]) -> str:
        self._update(
            '],"source_states":'
            + _canonical(states)
            + ',"version":'
            + _canonical(INTEGRITY_ARCHIVE_VERSION)
            + "}"
        )
        return self._hash.hexdigest()


def build_integrity_archive(
    *,
    kind: SourceKind,
    organizations: list[str],
    source_rows: list[dict[str, Any]],
    source_states: list[dict[str, Any]],
    derivations: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "version": INTEGRITY_ARCHIVE_VERSION,
        "kind": kind.value,
        "organizations": sorted(organizations),
        "source_rows": [encode_record(row) for row in source_rows],
        "source_states": source_states,
        "derivations": derivations,
    }
    payload["sha256"] = _digest(payload)
    validate_integrity_archive(payload, kind=kind, organizations=organizations)
    return payload


def validate_integrity_archive(
    payload: object, *, kind: SourceKind, organizations: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Reject incomplete or foreign provenance before any destination mutation."""
    required = {
        "version",
        "kind",
        "organizations",
        "source_rows",
        "source_states",
        "derivations",
        "sha256",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("archive integrity section is incomplete")
    if (
        type(payload["version"]) is not int
        or payload["version"] != INTEGRITY_ARCHIVE_VERSION
        or payload["kind"] != kind.value
    ):
        raise ValueError("unsupported archive integrity contract")
    _check_scope(organizations)
    if payload["organizations"] != sorted(set(organizations)):
        raise ValueError("archive integrity organization mismatch")
    if payload["sha256"] != _digest({k: v for k, v in payload.items() if k != "sha256"}):
        raise ValueError("archive integrity digest mismatch")
    if any(
        not isinstance(payload[key], list)
        for key in ("source_rows", "source_states", "derivations")
    ):
        raise ValueError("archive integrity collections must be arrays")
    rows = [decode_record(row) for row in payload["source_rows"]]
    states = deepcopy(payload["source_states"])
    derivations = deepcopy(payload["derivations"])
    orgs = set(organizations)
    row_keys: set[tuple[str, str]] = set()
    state_keys: set[tuple[str, str]] = set()
    for row in rows:
        _check_source_row(row, kind=kind, orgs=orgs, row_keys=row_keys)
    for state in states:
        _check_source_state(state, kind=kind, orgs=orgs, state_keys=state_keys)
    if not row_keys <= state_keys:
        raise ValueError("archive source has no retained ledger")
    ledger_by_key = {(state["organization_id"], state["source_id"]): state for state in states}
    for row in rows:
        _check_row_revision(row, kind=kind, ledger_by_key=ledger_by_key)
    _check_live_ledgers(states, row_keys)
    _check_derivations(derivations, kind=kind, state_keys=state_keys)
    return rows, states, derivations


class IntegrityRowValidator:
    """The section checks of ``validate_integrity_archive``, one row at a time.

    The ledger and derivations are checked up front, each decoded row as it
    arrives, and ``finish`` confirms every live ledger entry had a row, so a
    section can be validated without holding its rows.
    """

    def __init__(
        self,
        *,
        kind: SourceKind,
        organizations: list[str],
        states: list[dict[str, Any]],
        derivations: list[dict[str, Any]],
    ) -> None:
        _check_scope(organizations)
        self._kind = kind
        self._orgs = set(organizations)
        state_keys: set[tuple[str, str]] = set()
        for state in states:
            _check_source_state(state, kind=kind, orgs=self._orgs, state_keys=state_keys)
        _check_derivations(derivations, kind=kind, state_keys=state_keys)
        self._states = states
        self._ledger_by_key = {
            (state["organization_id"], state["source_id"]): state for state in states
        }
        self._row_keys: set[tuple[str, str]] = set()

    def check(self, row: dict[str, Any]) -> None:
        _check_source_row(row, kind=self._kind, orgs=self._orgs, row_keys=self._row_keys)
        if _row_key(row, self._kind) not in self._ledger_by_key:
            raise ValueError("archive source has no retained ledger")
        _check_row_revision(row, kind=self._kind, ledger_by_key=self._ledger_by_key)

    def finish(self) -> None:
        _check_live_ledgers(self._states, self._row_keys)


def _check_scope(organizations: list[str]) -> None:
    if any(not isinstance(org, str) or not org.strip() for org in organizations):
        raise ValueError("archive organization scope must be explicit")


def _row_key(row: dict[str, Any], kind: SourceKind) -> tuple[Any, Any]:
    org_field = "group_id" if kind is SourceKind.GRAPH_ENTITY else "organization_id"
    return row.get(org_field), row.get("uuid")


def _check_source_row(
    row: dict[str, Any],
    *,
    kind: SourceKind,
    orgs: set[str],
    row_keys: set[tuple[str, str]],
) -> None:
    key = _row_key(row, kind)
    org, identity = key
    if org not in orgs or not isinstance(identity, str) or not identity:
        raise ValueError("archive source identity mismatch")
    if kind is SourceKind.RAW_CAPTURE and not isinstance(row.get("raw_content"), str):
        raise ValueError("archive raw source content must be a string")
    if key in row_keys:
        raise ValueError("duplicate archive source identity")
    row_keys.add(key)
    if row.get("derivation_required") is not None and type(row["derivation_required"]) is not bool:
        raise ValueError("invalid archive source marker")


def _check_source_state(
    state: object,
    *,
    kind: SourceKind,
    orgs: set[str],
    state_keys: set[tuple[str, str]],
) -> None:
    if not isinstance(state, dict):
        raise ValueError("invalid archive source state")
    org, identity = state.get("organization_id"), state.get("source_id")
    if org not in orgs or not isinstance(identity, str) or not identity:
        raise ValueError("archive ledger identity mismatch")
    if state.get("source_kind") != kind.value:
        raise ValueError("archive ledger source kind mismatch")
    key = (org, identity)
    if key in state_keys:
        raise ValueError("duplicate archive source ledger")
    state_keys.add(key)
    if (
        type(state.get("generation")) is not int
        or state["generation"] <= 0
        or type(state.get("revision")) is not int
        or state["revision"] < 0
        or type(state.get("deleted")) is not bool
        or not isinstance(state.get("incarnation"), str)
        or not state["incarnation"].strip()
    ):
        raise ValueError("invalid archive source incarnation or high-water")


def _check_row_revision(
    row: dict[str, Any],
    *,
    kind: SourceKind,
    ledger_by_key: dict[tuple[str, str], dict[str, Any]],
) -> None:
    state = ledger_by_key[_row_key(row, kind)]
    if type(row.get("revision")) is not int or row["revision"] != state["revision"]:
        raise ValueError("archive source revision does not match retained ledger")


def _check_live_ledgers(states: list[dict[str, Any]], row_keys: set[tuple[str, str]]) -> None:
    if any(
        not state["deleted"] and (state["organization_id"], state["source_id"]) not in row_keys
        for state in states
    ):
        raise ValueError("archive live ledger has no source row")


def _check_derivations(
    derivations: list[dict[str, Any]],
    *,
    kind: SourceKind,
    state_keys: set[tuple[str, str]],
) -> None:
    association_keys: set[tuple[object, object]] = set()
    for association in derivations:
        if not isinstance(association, dict):
            raise ValueError("invalid archive derivation")
        key = (association.get("organization_id"), association.get("target_id"))
        if key not in state_keys or association.get("target_kind") != kind.value:
            raise ValueError("archive derivation target has no scoped ledger")
        if key in association_keys:
            raise ValueError("duplicate archive target association")
        association_keys.add(key)
        if type(association.get("active")) is not bool:
            raise ValueError("archive derivation requires explicit retirement state")
        if not isinstance(association.get("observations"), list) or not association["observations"]:
            raise ValueError("archive derivation has no captured observations")
        if (
            not isinstance(association.get("body_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", association["body_sha256"]) is None
            or not isinstance(association.get("principal_id"), str)
            or not association["principal_id"].strip()
            or not isinstance(association.get("authority_ceiling"), dict)
        ):
            raise ValueError("archive derivation body or authority is malformed")
        if association.get("operational_omission") is not None:
            from sibyl_core.services.operational_omission import validate_omission_receipt

            validate_omission_receipt(association)
        if association.get("validation_binding_json") is not None:
            from sibyl_core.services.validation_promotion import ValidationBinding

            ValidationBinding.model_validate_json(association["validation_binding_json"])
        if association.get("origin_execution_id") is not None:
            from uuid import NAMESPACE_URL, uuid5

            origin = association["origin_execution_id"]
            if (
                kind is not SourceKind.RAW_CAPTURE
                or not isinstance(origin, str)
                or re.fullmatch(r"[0-9a-f]{64}", origin) is None
                or str(uuid5(NAMESPACE_URL, "sibyl:validation-correction:" + origin)) != key[1]
            ):
                raise ValueError("Archive derivation origin identity differs")
        from sibyl_core.services.memory_derivations import observation_from_record

        for value in association["observations"]:
            observation = observation_from_record(value)
            if not observation.durable or observation.source.organization_id != key[0]:
                raise ValueError("archive derivation source is not scoped durable evidence")
