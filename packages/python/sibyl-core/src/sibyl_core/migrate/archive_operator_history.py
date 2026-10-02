"""Pure, qualified history validation for inert privileged backup captures.

The result proves consistency, not origin, current authority, native row hashes
or permission to replay. Full typed images remain separate from semantic models.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from sibyl_core.backends.surreal.auth_schema import AUTH_SCHEMA_CURRENT_VERSION, AUTH_TABLES
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_CURRENT_VERSION,
    CONTENT_TABLES,
)
from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES
from sibyl_core.backends.surreal.schema_version import GRAPH_SCHEMA_CURRENT_VERSION
from sibyl_core.migrate.archive_operator_native_root import PreparedArchiveOperatorRoot
from sibyl_core.migrate.archive_phase_receipts import (
    ArchivePhaseControl,
    ArchivePhaseCounts,
    ArchivePhaseKey,
    ArchivePhaseReceipt,
    ArchiveRetirementEvidence,
    ArchiveRunBinding,
    IntroducedArchiveRow,
    phase_binding_json,
)
from sibyl_core.migrate.personal_archive_artifact import validate_saved_archive_pair
from sibyl_core.migrate.personal_archive_plan import archive_digest

_TABLES = {
    "graph": {
        *GRAPH_TABLES,
        *GRAPH_EDGES,
        "source_states",
        "memory_derivations",
        "embedding_states",
        "schema_version",
        "schema_lease",
    },
    "content": {
        *CONTENT_TABLES,
        "source_states",
        "memory_derivations",
        "schema_version",
        "schema_lease",
    },
    "auth": {*AUTH_TABLES, "schema_version", "schema_lease"},
}
_VERSIONS = {
    "graph": GRAPH_SCHEMA_CURRENT_VERSION,
    "content": CONTENT_SCHEMA_CURRENT_VERSION,
    "auth": AUTH_SCHEMA_CURRENT_VERSION,
}
_COMMON = (
    "organization_id",
    "actor_id",
    "run_id",
    "store",
    "binding_json",
    "binding_sha256",
    "checked_plan_sha256",
)
_NUMBERS = ("created", "skipped", "conflicted", "quarantined", "retired", "preserved")
_TRANSITIONS = frozenset({"revision", "token", "state"})
_GRAPH_ORG_FIELDS = {
    "entity": "group_id",
    "episode": "group_id",
    "relates_to": "group_id",
    "mentions": "group_id",
    "source_states": "organization_id",
    "memory_derivations": "organization_id",
    "embedding_states": "organization_id",
    "archive_phase_controls": "organization_id",
    "archive_phase_receipts": "organization_id",
}
_FRAME_DOMAIN = b"sibyl-archive-qualified-typed-subtree-v1\\0"


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _uuid(value: object) -> str:
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError("history identity must be a canonical UUID")
    return value


@dataclass(frozen=True, slots=True)
class ArchiveHistoryScope:
    """Expected physical layout supplied by the server, without an auth grant."""

    store: Literal["content", "graph", "auth"]
    namespace: str
    database: str
    organization_id: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.store) is not str
            or self.store not in _TABLES
            or any(type(value) is not str or not value for value in (self.namespace, self.database))
        ):
            raise ValueError("invalid history physical scope")
        if self.store == "graph":
            _uuid(self.organization_id)
        elif self.organization_id is not None:
            raise ValueError("shared history scopes must remain full operator scopes")

    @property
    def locator(self) -> tuple[str, str]:
        return self.namespace, self.database


@dataclass(frozen=True, slots=True)
class ArchiveHistoryScopeMapping:
    """An inert, exact mapping; parsed source labels cannot expand target scope."""

    source: ArchiveHistoryScope
    target: ArchiveHistoryScope

    def __post_init__(self) -> None:
        if (
            type(self.source) is not ArchiveHistoryScope
            or type(self.target) is not ArchiveHistoryScope
        ):
            raise TypeError("history mapping requires exact prepared scopes")
        if (self.source.store, self.source.organization_id) != (
            self.target.store,
            self.target.organization_id,
        ):
            raise ValueError("history mapping cannot rewrite organization or store")


@dataclass(frozen=True, slots=True)
class _Row:
    scope: ArchiveHistoryScope
    table: str
    image_json: str
    identity_json: str

    @property
    def image(self) -> dict[str, Any]:
        return json.loads(self.image_json)

    @property
    def value(self) -> dict[str, Any]:
        return self.image["value"]

    @property
    def types(self) -> list[dict[str, Any]]:
        return self.image["native_types"]

    def qualified(self) -> dict[str, Any]:
        value = {
            "scope": _scope_dict(self.scope),
            "table": self.table,
            "identity": json.loads(self.identity_json),
            "image": self.image,
        }
        return {
            **value,
            "encoded_subtree_sha256": hashlib.sha256(
                _FRAME_DOMAIN + _json(value).encode()
            ).hexdigest(),
        }


def _scopes(value: tuple[ArchiveHistoryScope, ...]) -> tuple[ArchiveHistoryScope, ...]:
    if (
        type(value) is not tuple
        or not value
        or any(type(s) is not ArchiveHistoryScope for s in value)
    ):
        raise TypeError("history requires an immutable exact scope inventory")
    if len({s.locator for s in value}) != len(value):
        raise ValueError("history scope labels alias one physical locator")
    if (
        {s.store for s in value} != set(_TABLES)
        or sum(s.store == "content" for s in value) != 1
        or sum(s.store == "auth" for s in value) != 1
    ):
        raise ValueError("history requires graph and one shared content/auth scope")
    if len({s.organization_id for s in value if s.store == "graph"}) != sum(
        s.store == "graph" for s in value
    ):
        raise ValueError("history repeats a graph organization")
    return value


def _rows(
    root: PreparedArchiveOperatorRoot, expected: tuple[ArchiveHistoryScope, ...]
) -> dict[ArchiveHistoryScope, dict[str, tuple[_Row, ...]]]:
    native = root.native.payload
    value = native["value"]
    if value["server_version"].split("+", 1)[0] != "surrealdb-3.2.4":
        raise ValueError("unsupported history backend")
    actual = tuple(
        ArchiveHistoryScope(s["store"], s["namespace"], s["database"], s["organization_id"])
        for s in value["scopes"]
    )
    if set(actual) != set(expected) or len(actual) != len(expected):
        raise ValueError("history scopes differ from expected physical inventory")
    descriptors: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for descriptor in native["native_types"]:
        path = descriptor["path"]
        if len(path) >= 6 and path[0] == "scopes" and path[2] == "tables" and path[4] == "rows":
            descriptors[(path[1], path[3], path[5])].append({**descriptor, "path": path[6:]})
    result = {}
    for i, (scope, data) in enumerate(zip(actual, value["scopes"], strict=True)):
        names = {table["name"] for table in data["tables"]}
        if names | set(data["absent_diagnostics"]) != _TABLES[scope.store]:
            raise ValueError("missing or unregistered history table inventory")
        if any(
            " SCHEMAFULL" not in definition
            for definition in data["database_catalog"]["tables"].values()
        ):
            raise ValueError("unsupported history table schema")
        tables = {}
        physical = set()
        for j, table in enumerate(data["tables"]):
            rows = []
            for k, row in enumerate(table["rows"]):
                types = sorted(descriptors[(i, j, k)], key=lambda d: _json(d["path"]))
                ids = [d for d in types if d["path"] == ["id"]]
                if (
                    type(row) is not dict
                    or len(ids) != 1
                    or ids[0]["kind"] != "record"
                    or ids[0]["table"] != table["name"]
                ):
                    raise ValueError("history row requires its exact native table identity")
                identity = _json({key: val for key, val in ids[0].items() if key != "path"})
                if identity in physical:
                    raise ValueError("duplicate native physical identity in history scope")
                physical.add(identity)
                image = _json({"value": row, "native_types": types})
                captured = _Row(scope, table["name"], image, identity)
                if scope.store == "graph" and table["name"] in _GRAPH_ORG_FIELDS:
                    field_name = _GRAPH_ORG_FIELDS[table["name"]]
                    if row.get(field_name) != scope.organization_id:
                        raise ValueError("history graph row belongs to another organization")
                    _plain(captured, (field_name,))
                rows.append(captured)
            tables[table["name"]] = tuple(rows)
        versions = tables["schema_version"]
        if (
            len(versions) != 1
            or versions[0].value.get("name") != scope.store
            or type(versions[0].value.get("version")) is not int
            or versions[0].value["version"] != _VERSIONS[scope.store]
        ):
            raise ValueError("unsupported history store schema version")
        _plain(versions[0], ("name", "version"))
        result[scope] = tables
    return result


def _plain(row: _Row, fields: tuple[str, ...]) -> None:
    """Known semantics cannot hide a UUID/RecordID/date behind JSON strings."""
    for descriptor in row.types:
        path = descriptor["path"]
        if len(path) == 1 and path[0] in fields and descriptor["kind"] not in ("none", "null"):
            raise ValueError("history semantic fields have unsupported native types")


def _text(row: dict[str, Any], fields: tuple[str, ...]) -> None:
    if any(type(row.get(name)) is not str for name in fields):
        raise ValueError("history binding requires native strings")


def _project(value: object, model: type[BaseModel]) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("history evidence requires objects")
    return {name: value[name] for name in model.model_fields if name in value}


def _pairs(tables: dict[str, tuple[_Row, ...]]) -> dict[str, dict[str, Any]]:
    runs = {}
    artifacts = {}
    intakes = set()
    for row in tables["archive_import_runs"]:
        value = row.value
        _text(
            value,
            (
                "uuid",
                "organization_id",
                "actor_id",
                "intake_identity",
                "request_sha256",
                "archive_sha256",
                "artifact_id",
                "artifact_sha256",
                "origin_json",
                "mappings_json",
                "mappings_sha256",
                "conflict_policy",
                "credential_kind",
                "original_ceiling_json",
                "checked_plan_json",
                "checked_plan_sha256",
                "preview_counts_json",
                "status",
            ),
        )
        _plain(
            row,
            (
                "uuid",
                "organization_id",
                "actor_id",
                "intake_identity",
                "request_sha256",
                "contract_version",
                "archive_sha256",
                "artifact_id",
                "artifact_sha256",
                "origin_json",
                "mappings_json",
                "mappings_sha256",
                "conflict_policy",
                "credential_kind",
                "original_api_key_id",
                "original_ceiling_json",
                "checked_plan_json",
                "checked_plan_sha256",
                "preview_counts_json",
                "status",
                "revision",
            ),
        )
        for name in ("uuid", "organization_id", "actor_id", "artifact_id"):
            _uuid(value[name])
        if (
            value["status"] != "checked"
            or type(value.get("revision")) is not int
            or value["revision"] < 0
        ):
            raise ValueError("invalid saved run lifecycle")
        if len(value["request_sha256"]) != 64 or any(
            c not in "0123456789abcdef" for c in value["request_sha256"]
        ):
            raise ValueError("invalid saved request digest")
        for name in ("created_at", "updated_at"):
            _date_field(row, name)
        key = (value["organization_id"], value["actor_id"], value["intake_identity"])
        if value["uuid"] in runs or key in intakes:
            raise ValueError("duplicate saved run or intake identity")
        intakes.add(key)
        runs[value["uuid"]] = row
    owners = set()
    for row in tables["archive_import_artifacts"]:
        value = row.value
        _text(
            value,
            (
                "uuid",
                "organization_id",
                "actor_id",
                "run_id",
                "archive_sha256",
                "artifact_sha256",
                "member_inventory_json",
                "staged_payload_json",
                "measured_sizes_json",
            ),
        )
        _plain(
            row,
            (
                "uuid",
                "organization_id",
                "actor_id",
                "run_id",
                "archive_sha256",
                "artifact_sha256",
                "contract_version",
                "member_inventory_json",
                "staged_payload_json",
                "measured_sizes_json",
            ),
        )
        for name in ("uuid", "organization_id", "actor_id", "run_id"):
            _uuid(value[name])
        _date_field(row, "created_at")
        owner = (value["organization_id"], value["actor_id"], value["run_id"])
        if value["uuid"] in artifacts or owner in owners:
            raise ValueError("duplicate saved artifact identity or owner")
        owners.add(owner)
        artifacts[value["uuid"]] = row
    result = {}
    referenced = set()
    for run_id, run in runs.items():
        item = artifacts.get(run.value["artifact_id"])
        if item is None or item.value["run_id"] != run_id:
            raise ValueError("saved run lacks its exact artifact")
        pair = validate_saved_archive_pair(
            run.value,
            item.value,
            run_id=run_id,
            organization_id=run.value["organization_id"],
            actor_id=run.value["actor_id"],
        )
        plan = pair.plan
        binding = ArchiveRunBinding(
            organization_id=plan.organization_id,
            actor_id=plan.actor_id,
            run_id=pair.run_id,
            artifact_id=pair.artifact_id,
            archive_sha256=plan.archive_sha256,
            artifact_sha256=plan.artifact_sha256,
            mappings_sha256=archive_digest("sibyl-archive-mappings-v1", plan.mappings),
            checked_plan_sha256=pair.checked_plan_sha256,
            credential=plan.credential.model_dump(mode="python"),
        )
        result[run_id] = {"binding": binding, "run": run, "artifact": item}
        referenced.add(pair.artifact_id)
    if referenced != set(artifacts):
        raise ValueError("saved artifact lacks its exact run")
    return result


def _date_field(row: _Row, name: str) -> None:
    if not any(d["path"] == [name] and d["kind"] == "datetime" for d in row.types):
        raise ValueError("saved metadata requires its native datetime")


def _binding(row: _Row, pair: dict[str, Any]) -> ArchiveRunBinding:
    value = row.value
    _text(value, _COMMON)
    _plain(
        row,
        (
            *_COMMON,
            "revision",
            "token",
            "state",
            "action",
            "phase",
            "batch_sequence",
            "previous_revision",
            "committed_revision",
            "previous_token",
            "terminal",
        ),
    )
    if value["store"] != row.scope.store:
        raise ValueError("phase row store differs from owning physical scope")
    binding = ArchiveRunBinding.model_validate_json(value["binding_json"])
    if (
        phase_binding_json(binding) != value["binding_json"]
        or binding.sha256 != value["binding_sha256"]
    ):
        raise ValueError("phase binding bytes or digest changed")
    if binding != pair["binding"] or any(
        value[name] != getattr(binding, name)
        for name in ("organization_id", "actor_id", "run_id", "checked_plan_sha256")
    ):
        raise ValueError("phase flat columns differ from exact saved binding")
    return binding


def _receipt(row: _Row, binding: ArchiveRunBinding) -> ArchivePhaseReceipt:
    v = row.value
    store = row.scope.store
    if store == "auth":
        raise ValueError("auth cannot own phase receipts")
    for descriptor in row.types:
        path = descriptor["path"]
        relevant = False
        if len(path) >= 3 and path[0] == "counts":
            relevant = path[2] in ArchivePhaseCounts.model_fields
        elif len(path) >= 3 and path[0] == "introduced":
            relevant = path[2] in IntroducedArchiveRow.model_fields
        elif len(path) >= 3 and path[0] == "retired":
            if path[2] == "introduced":
                relevant = len(path) >= 4 and path[3] in IntroducedArchiveRow.model_fields
            else:
                relevant = path[2] in ArchiveRetirementEvidence.model_fields
        if relevant and descriptor["kind"] not in ("none", "null"):
            raise ValueError("phase evidence semantic fields have unsupported native types")
    _text(v, ("action", "phase", "token", "previous_token"))
    if any(
        name not in v
        for name in ("batch_sequence", "previous_revision", "committed_revision", "terminal")
    ):
        raise ValueError("phase receipt fields are missing")
    key = ArchivePhaseKey(
        binding=binding,
        store=store,
        action=v["action"],
        batch_sequence=v["batch_sequence"],
    )
    if key.phase != v["phase"]:
        raise ValueError("phase receipt phase label changed")
    if any(type(v.get(name)) is not list for name in ("counts", "introduced", "retired")):
        raise ValueError("phase evidence arrays are missing")
    counts = []
    for count in v["counts"]:
        if type(count) is not dict or any(name not in count for name in ("kind", *_NUMBERS)):
            raise ValueError("phase count fields are missing")
        counts.append(ArchivePhaseCounts.model_validate(_project(count, ArchivePhaseCounts)))
    introduced = tuple(
        IntroducedArchiveRow.model_validate_json(_json(_project(item, IntroducedArchiveRow)))
        for item in v["introduced"]
    )
    retired = []
    for item in v["retired"]:
        projection = _project(item, ArchiveRetirementEvidence)
        projection["introduced"] = _project(projection.get("introduced"), IntroducedArchiveRow)
        retired.append(ArchiveRetirementEvidence.model_validate_json(_json(projection)))
    return ArchivePhaseReceipt(
        key=key,
        token=v["token"],
        previous_token=v["previous_token"],
        previous_revision=v["previous_revision"],
        committed_revision=v["committed_revision"],
        counts=tuple(counts),
        introduced=introduced,
        retired=tuple(retired),
        terminal=v["terminal"],
    )


def _chains(
    inventory: dict[ArchiveHistoryScope, dict[str, tuple[_Row, ...]]],
    pairs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for scope, tables in inventory.items():
        if scope.store == "auth":
            continue
        controls = {}
        receipts: dict[tuple[str, str], list[tuple[_Row, ArchivePhaseReceipt]]] = defaultdict(list)
        batches = set()
        revisions = set()
        for row in tables["archive_phase_controls"]:
            v = row.value
            pair = pairs.get(v.get("run_id"))
            if pair is None:
                raise ValueError("phase control lacks its saved pair")
            binding = _binding(row, pair)
            key = (binding.organization_id, binding.run_id)
            if key in controls:
                raise ValueError("duplicate physical org/run control")
            if any(name not in v for name in ("revision", "token", "state")):
                raise ValueError("phase control fields are missing")
            control = ArchivePhaseControl(
                binding=binding,
                store=scope.store,
                revision=v["revision"],
                token=v["token"],
                state=v["state"],
            )
            controls[key] = (row, control)
        for row in tables["archive_phase_receipts"]:
            v = row.value
            pair = pairs.get(v.get("run_id"))
            if pair is None:
                raise ValueError("phase receipt lacks its saved pair")
            binding = _binding(row, pair)
            proof = _receipt(row, binding)
            key = (binding.organization_id, binding.run_id)
            batch = (*key, binding.checked_plan_sha256, proof.key.phase, proof.key.batch_sequence)
            revision = (*key, binding.checked_plan_sha256, scope.store, proof.committed_revision)
            if batch in batches or revision in revisions:
                raise ValueError("duplicate receipt batch or committed revision")
            batches.add(batch)
            revisions.add(revision)
            receipts[key].append((row, proof))
        if receipts.keys() - controls.keys():
            raise ValueError("receipts lack their owning control")
        for key, (control_row, control) in controls.items():
            chain = sorted(receipts[key], key=lambda entry: entry[1].committed_revision)
            token = chain[0][1].previous_token if chain else control.token
            initial_token = token
            state = "open"
            introductions = {}
            introduced_physical = set()
            for revision, (_, proof) in enumerate(chain, 1):
                if proof.committed_revision != revision or proof.previous_token != token:
                    raise ValueError("phase history has a gap, fork or previous-token drift")
                if state == "rolled_back":
                    raise ValueError("phase history continues after terminal closure")
                if proof.key.action == "apply":
                    if state != "open":
                        raise ValueError("apply follows closed apply token")
                elif (state == "open" and proof.token == token) or (
                    state == "rolling_back" and proof.token != token
                ):
                    raise ValueError("rollback token rotation differs from native CAS")
                for item in proof.introduced:
                    identity = (item.kind, item.destination_id)
                    if identity in introductions or item.physical_id in introduced_physical:
                        raise ValueError("introduction aliases earlier native evidence")
                    introductions[identity] = item
                    introduced_physical.add(item.physical_id)
                for retirement in proof.retired:
                    parent = retirement.introduced
                    if introductions.get((parent.kind, parent.destination_id)) != parent:
                        raise ValueError("retirement has no exact retained earlier apply parent")
                token = proof.token
                state = (
                    "open"
                    if proof.key.action == "apply"
                    else ("rolled_back" if proof.terminal else "rolling_back")
                )
            if (control.revision, control.token, control.state) != (len(chain), token, state):
                raise ValueError("control differs from derived complete receipt tip")
            seed = control_row.image
            seed["value"].update(revision=0, token=initial_token, state="open")
            seed["native_types"] = [
                d for d in seed["native_types"] if not d["path"] or d["path"][0] not in _TRANSITIONS
            ]
            result.append(
                {
                    "scope": _scope_dict(scope),
                    "organization_id": key[0],
                    "run_id": key[1],
                    "binding_json": phase_binding_json(control.binding),
                    "control": control_row.qualified(),
                    "seed": seed,
                    "receipts": [row.qualified() for row, _ in chain],
                }
            )
    return sorted(
        result,
        key=lambda c: (
            c["scope"]["namespace"],
            c["scope"]["database"],
            c["organization_id"],
            c["run_id"],
        ),
    )


def _scope_dict(scope: ArchiveHistoryScope) -> dict[str, Any]:
    return {
        "store": scope.store,
        "namespace": scope.namespace,
        "database": scope.database,
        "organization_id": scope.organization_id,
    }


def _history(
    root: PreparedArchiveOperatorRoot, scopes: tuple[ArchiveHistoryScope, ...]
) -> dict[str, Any]:
    if type(root) is not PreparedArchiveOperatorRoot:
        raise TypeError("history requires an inert prepared operator root")
    scopes = _scopes(scopes)
    inventory = _rows(root, scopes)
    content = next(t for s, t in inventory.items() if s.store == "content")
    auth = next(t for s, t in inventory.items() if s.store == "auth")
    pairs = _pairs(content)
    organizations = set()
    for row in auth["organizations"]:
        org = _uuid(row.value.get("uuid"))
        _plain(row, ("uuid",))
        if org in organizations:
            raise ValueError("duplicate native organization identity")
        organizations.add(org)
    covered = {s.organization_id for s in scopes if s.store == "graph"}
    if not covered.issubset(organizations):
        raise ValueError("history graph scope lacks its native auth organization")
    chain = _chains(inventory, pairs)
    known = organizations | {p["binding"].organization_id for p in pairs.values()}
    return {
        "scopes": [_scope_dict(s) for s in scopes],
        "root_sha256": root.payload["sha256"],
        "chains": chain,
        "pairs": [
            {
                "run_id": run_id,
                "organization_id": pair["binding"].organization_id,
                "run": pair["run"].qualified(),
                "artifact": pair["artifact"].qualified(),
            }
            for run_id, pair in sorted(pairs.items())
        ],
        "uncovered_organization_ids": sorted(known - covered),
    }


@dataclass(frozen=True, slots=True)
class PreparedArchiveOperatorHistory:
    """Inert consistency evidence with detached accessors, not replay authority."""

    root: PreparedArchiveOperatorRoot
    expected_scopes: tuple[ArchiveHistoryScope, ...]
    _payload_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_payload_json", _json(_history(self.root, self.expected_scopes)))

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


def validate_archive_operator_history(
    root: PreparedArchiveOperatorRoot, *, expected_scopes: tuple[ArchiveHistoryScope, ...]
) -> PreparedArchiveOperatorHistory:
    """Validate complete native-event-equivalent history, without current-row guards."""
    return PreparedArchiveOperatorHistory(root, expected_scopes)


def _same_image(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return _json(a["identity"]) == _json(b["identity"]) and _json(a["image"]) == _json(b["image"])


def _prefix(
    archived: PreparedArchiveOperatorHistory,
    retained: PreparedArchiveOperatorHistory,
    mappings: tuple[ArchiveHistoryScopeMapping, ...],
) -> dict[str, Any]:
    if (
        type(archived) is not PreparedArchiveOperatorHistory
        or type(retained) is not PreparedArchiveOperatorHistory
    ):
        raise TypeError("prefix requires complete validated histories")
    if type(mappings) is not tuple or any(
        type(m) is not ArchiveHistoryScopeMapping for m in mappings
    ):
        raise TypeError("prefix requires an immutable exact scope mapping")
    if len({m.source.locator for m in mappings}) != len(mappings) or len(
        {m.target.locator for m in mappings}
    ) != len(mappings):
        raise ValueError("prefix mapping aliases a physical locator")
    if set(m.source for m in mappings) != set(archived.expected_scopes) or set(
        m.target for m in mappings
    ) != set(retained.expected_scopes):
        raise ValueError("prefix mapping must cover both exact approved scope inventories")
    a, b = archived.payload, retained.payload
    covered = {s.organization_id for s in archived.expected_scopes if s.store == "graph"}
    pair_a = {p["run_id"]: p for p in a["pairs"]}
    pair_b = {p["run_id"]: p for p in b["pairs"]}
    pair_plans = []
    artifact_owners = {p["artifact"]["image"]["value"]["uuid"]: p["run_id"] for p in b["pairs"]}
    target_ids = {
        (p[name]["table"], _json(p[name]["identity"])): p["run_id"]
        for p in b["pairs"]
        for name in ("run", "artifact")
    }
    for run_id, pair in pair_a.items():
        if pair["organization_id"] not in covered:
            continue
        other = pair_b.get(run_id)
        artifact_id = pair["artifact"]["image"]["value"]["uuid"]
        if artifact_id in artifact_owners and artifact_owners[artifact_id] != run_id:
            raise ValueError("saved artifact logical identity collides with retained pair")
        if other is not None and any(
            not _same_image(pair[name], other[name]) for name in ("run", "artifact")
        ):
            raise ValueError("retained saved pair differs from exact captured native images")
        if other is None and any(
            (pair[name]["table"], _json(pair[name]["identity"])) in target_ids
            for name in ("run", "artifact")
        ):
            raise ValueError("missing saved pair collides with retained native physical identity")
        pair_plans.append(
            {
                "run_id": run_id,
                "outcome": "retain" if other else "missing",
                "archived": pair,
                "retained": other,
            }
        )
    pair_plans.extend(
        {"run_id": run_id, "outcome": "retain", "archived": None, "retained": pair}
        for run_id, pair in pair_b.items()
        if run_id not in pair_a and pair["organization_id"] in covered
    )

    def index(payload: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
        return {
            (c["scope"]["store"], c["organization_id"], c["run_id"]): c
            for c in payload["chains"]
            if c["organization_id"] in covered
        }

    source, target = index(a), index(b)
    target_scope = {m.source: m.target for m in mappings}
    target_ids = {
        (c["scope"]["namespace"], c["scope"]["database"], row["table"], _json(row["identity"])): (
            c["organization_id"],
            c["run_id"],
        )
        for c in b["chains"]
        for row in (c["control"], *c["receipts"])
    }
    plans = []
    keys = (
        source.keys()
        | target.keys()
        | {
            (store, p["organization_id"], p["run_id"])
            for p in (*a["pairs"], *b["pairs"])
            if p["organization_id"] in covered
            for store in ("content", "graph")
        }
    )
    for key in sorted(keys):
        left, right = source.get(key), target.get(key)
        if left is None:
            plans.append(
                {
                    "store": key[0],
                    "organization_id": key[1],
                    "run_id": key[2],
                    "outcome": "retain" if right else "unclaimed",
                    "archived": None,
                    "retained": right,
                    "seed": None,
                    "missing_receipts": [],
                }
            )
            continue
        if right is not None:
            if _json(left["control"]["identity"]) != _json(right["control"]["identity"]) or _json(
                left["seed"]
            ) != _json(right["seed"]):
                raise ValueError("retained control differs from exact original native seed")
            for old, current in zip(left["receipts"], right["receipts"], strict=False):
                if not _same_image(old, current):
                    raise ValueError(
                        "history prefix diverges in native identity or full typed image"
                    )
            missing = left["receipts"][len(right["receipts"]) :]
        else:
            missing = left["receipts"]
        for row in ([left["control"]] if right is None else []) + missing:
            locator = target_scope[ArchiveHistoryScope(**left["scope"])].locator
            collision = target_ids.get((*locator, row["table"], _json(row["identity"])))
            if collision is not None:
                raise ValueError(
                    "missing history row collides with retained native physical identity"
                )
        plans.append(
            {
                "store": key[0],
                "organization_id": key[1],
                "run_id": key[2],
                "outcome": "missing" if right is None else ("append" if missing else "retain"),
                "archived": left,
                "retained": right,
                "seed": left["seed"] if right is None else None,
                "missing_receipts": missing,
            }
        )
    return {
        "version": 1,
        "archived_root_sha256": a["root_sha256"],
        "retained_root_sha256": b["root_sha256"],
        "scope_mapping": [
            {"source": _scope_dict(m.source), "target": _scope_dict(m.target)} for m in mappings
        ],
        "pairs": pair_plans,
        "stores": plans,
        "uncovered_organization_ids": sorted(
            set(a["uncovered_organization_ids"]) | set(b["uncovered_organization_ids"])
        ),
    }


@dataclass(frozen=True, slots=True)
class PreparedArchiveHistoryPrefix:
    """A monotonic inert comparison; no statement, permit or source-state write."""

    archived: PreparedArchiveOperatorHistory
    retained: PreparedArchiveOperatorHistory
    scope_mapping: tuple[ArchiveHistoryScopeMapping, ...]
    _payload_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_payload_json", _json(_prefix(self.archived, self.retained, self.scope_mapping))
        )

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


def prepare_archive_history_prefix(
    archived: PreparedArchiveOperatorHistory,
    retained: PreparedArchiveOperatorHistory,
    *,
    scope_mapping: tuple[ArchiveHistoryScopeMapping, ...],
) -> PreparedArchiveHistoryPrefix:
    """Retain exact prefixes and later closure; only describe missing inert images."""
    return PreparedArchiveHistoryPrefix(archived, retained, scope_mapping)
