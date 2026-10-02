"""Complete inert history contracts, independent of native restore execution."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from uuid import uuid4

import pytest

from sibyl_core.migrate.archive_native_values import prepare_archive_native_value
from sibyl_core.migrate.archive_operator_history import (
    ArchiveHistoryScope,
    ArchiveHistoryScopeMapping,
    prepare_archive_history_prefix,
    validate_archive_operator_history,
)
from sibyl_core.migrate.archive_operator_native_root import PROFILE, prepare_archive_operator_root
from sibyl_core.migrate.archive_phase_receipts import (
    ArchivePhaseCounts,
    ArchivePhaseKey,
    ArchivePhaseReceipt,
    ArchiveRetirementEvidence,
    ArchiveRunBinding,
    phase_binding_json,
)
from sibyl_core.migrate.personal_archive_artifact import (
    ArchiveArtifactIntegrityError,
    validate_saved_archive_pair,
)
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveCredentialCeiling,
    CheckedArchivePlan,
    archive_digest,
    canonical_json,
    checked_plan_bytes,
    checked_plan_digest,
)
from sibyl_core.services.archive_operator_native_capture import _TABLES, _VERSIONS
from tests.test_archive_phase_receipts import introduced
from tests.test_personal_archive_prepared import checked_fixture

CLOCK = "2026-10-02T01:02:03.123456789Z"


@dataclass(frozen=True)
class Native:
    kind: str
    value: object


@dataclass(frozen=True)
class Record:
    table: str
    identifier: object


def seal(value):
    """Encode explicit fixture types; a supplied native SHA does not certify origin."""
    descriptors = []

    def encode(item, path, types):
        if isinstance(item, Record):
            identifiers = []
            identifier = encode(item.identifier, [], identifiers)
            types.append(
                {
                    "path": path,
                    "kind": "record",
                    "table": item.table,
                    "identifier": {"value": identifier, "native_types": identifiers},
                }
            )
            return {"table": item.table, "id": identifier}
        if isinstance(item, Native):
            types.append({"path": path, "kind": item.kind})
            return item.value
        if item is None:
            types.append({"path": path, "kind": "none"})
            return None
        if isinstance(item, dict):
            return {key: encode(child, [*path, key], types) for key, child in item.items()}
        if isinstance(item, list | tuple):
            return [encode(child, [*path, i], types) for i, child in enumerate(item)]
        return item

    encoded = encode(value, [], descriptors)
    return prepare_archive_operator_root(
        prepare_archive_native_value(
            value=encoded, native_types=descriptors, native_sha256="a" * 64
        )
    )


def table(data, store, name):
    scope = next(s for s in data["scopes"] if s["store"] == store)
    return next(t for t in scope["tables"] if t["name"] == name)


def metadata(parsed, plan, run_id, artifact_id):
    run = {
        "id": Record("archive_import_runs", ["run", Native("uuid", run_id), {"part": "x\0y"}]),
        "uuid": run_id,
        "organization_id": plan.organization_id,
        "actor_id": plan.actor_id,
        "intake_identity": "server-owned-check",
        "request_sha256": "b" * 64,
        "contract_version": 1,
        "archive_sha256": plan.archive_sha256,
        "artifact_id": artifact_id,
        "artifact_sha256": plan.artifact_sha256,
        "origin_json": canonical_json(plan.origin),
        "mappings_json": canonical_json(plan.mappings),
        "mappings_sha256": archive_digest("sibyl-archive-mappings-v1", plan.mappings),
        "conflict_policy": plan.conflict_policy,
        "credential_kind": plan.credential.credential_kind,
        "original_api_key_id": plan.credential.api_key_id,
        "original_ceiling_json": canonical_json(plan.credential),
        "checked_plan_json": checked_plan_bytes(plan),
        "checked_plan_sha256": checked_plan_digest(plan),
        "preview_counts_json": canonical_json(
            {k: v.model_dump(mode="json") for k, v in plan.counts.items()}
        ),
        "created_at": Native("datetime", CLOCK),
        "updated_at": Native("datetime", CLOCK),
        "status": "checked",
        "revision": 0,
    }
    artifact = {
        "id": Record("archive_import_artifacts", "artifact"),
        "uuid": artifact_id,
        "organization_id": plan.organization_id,
        "actor_id": plan.actor_id,
        "run_id": run_id,
        "archive_sha256": parsed.archive_sha256,
        "artifact_sha256": parsed.artifact_sha256,
        "contract_version": 1,
        "member_inventory_json": parsed.member_inventory_json,
        "staged_payload_json": parsed.staged_payload_json,
        "measured_sizes_json": parsed.measured_sizes_json,
        "created_at": Native("datetime", CLOCK),
    }
    return run, artifact


@pytest.fixture
def dataset(tmp_path):
    parsed, old = checked_fixture(tmp_path)
    ceiling = ArchiveCredentialCeiling(
        credential_kind="api_key",
        api_key_id=str(uuid4()),
        project_restricted=True,
        memory_restricted=True,
        project_ids=(),
        memory_scope_keys=(),
        memory_space_ids=(),
    )
    plan = CheckedArchivePlan.model_validate(
        {**old.model_dump(mode="python"), "credential": ceiling}
    )
    run_id, artifact_id = str(uuid4()), str(uuid4())
    run, artifact = metadata(parsed, plan, run_id, artifact_id)
    binding = ArchiveRunBinding(
        organization_id=plan.organization_id,
        actor_id=plan.actor_id,
        run_id=run_id,
        artifact_id=artifact_id,
        archive_sha256=plan.archive_sha256,
        artifact_sha256=plan.artifact_sha256,
        mappings_sha256=archive_digest("sibyl-archive-mappings-v1", plan.mappings),
        checked_plan_sha256=checked_plan_digest(plan),
        credential=ceiling.model_dump(mode="python"),
    )
    scopes = (
        ArchiveHistoryScope(
            "graph",
            "history_" + plan.organization_id.replace("-", ""),
            "graph",
            plan.organization_id,
        ),
        ArchiveHistoryScope("content", "history_content", "content"),
        ArchiveHistoryScope("auth", "history_auth", "auth"),
    )
    data = {
        "profile": PROFILE,
        "endpoint": "ws://127.0.0.1:23108/rpc",
        "operator_id": "inert-assertion",
        "decision_id": "inert-decision",
        "server_version": "surrealdb-3.2.4",
        "graph_namespace_prefix": "history_",
        "namespace_catalog": {"namespaces": {s.namespace: "namespace-definition" for s in scopes}},
        "scopes": [],
    }
    for scope in scopes:
        data["scopes"].append(
            {
                "store": scope.store,
                "namespace": scope.namespace,
                "database": scope.database,
                "organization_id": scope.organization_id,
                "namespace_catalog": {"databases": {scope.database: "database-definition"}},
                "database_catalog": {
                    "tables": {
                        name: f"DEFINE TABLE {name} SCHEMAFULL" for name in _TABLES[scope.store]
                    }
                },
                "absent_diagnostics": [],
                "tables": [
                    {"name": name, "catalog": {"fields": {}, "events": {}}, "rows": []}
                    for name in sorted(_TABLES[scope.store])
                ],
            }
        )
        table(data, scope.store, "schema_version")["rows"] = [
            {
                "id": Record("schema_version", scope.store),
                "name": scope.store,
                "version": _VERSIONS[scope.store],
            }
        ]
    table(data, "auth", "organizations")["rows"] = [
        {"id": Record("organizations", plan.organization_id), "uuid": plan.organization_id}
    ]
    table(data, "content", "archive_import_runs")["rows"] = [run]
    table(data, "content", "archive_import_artifacts")["rows"] = [artifact]
    return data, scopes, binding, plan


def flat(binding, store):
    return {
        "organization_id": binding.organization_id,
        "actor_id": binding.actor_id,
        "run_id": binding.run_id,
        "store": store,
        "binding_json": phase_binding_json(binding),
        "binding_sha256": binding.sha256,
        "checked_plan_sha256": binding.checked_plan_sha256,
    }


def chain(binding, store="content"):
    initial, rollback = str(uuid4()), str(uuid4())
    item = introduced()
    if store == "graph":
        item = type(item).model_validate(
            {
                **item.model_dump(mode="python"),
                "kind": "graph_entity",
                "physical_id": "entity:historical",
            }
        )
    first = ArchivePhaseReceipt(
        key=ArchivePhaseKey(binding=binding, store=store, action="apply", batch_sequence=17),
        token=initial,
        previous_token=initial,
        previous_revision=0,
        committed_revision=1,
        counts=(ArchivePhaseCounts(kind=item.kind, created=1),),
        introduced=(item,),
    )
    retirement = ArchiveRetirementEvidence(
        introduced=item,
        absent=True,
        row_sha256=None,
        source_incarnation=item.source_incarnation,
        source_generation=99,
        source_state_sha256="2" * 64,
    )
    second = ArchivePhaseReceipt(
        key=ArchivePhaseKey(binding=binding, store=store, action="rollback", batch_sequence=7),
        token=rollback,
        previous_token=initial,
        previous_revision=1,
        committed_revision=2,
        counts=(ArchivePhaseCounts(kind=item.kind, retired=1),),
        retired=(retirement,),
    )
    third = ArchivePhaseReceipt(
        key=ArchivePhaseKey(binding=binding, store=store, action="rollback", batch_sequence=90),
        token=rollback,
        previous_token=rollback,
        previous_revision=2,
        committed_revision=3,
        counts=(),
        terminal=True,
    )
    return (first, second, third)


def install(data, binding, proofs, store="content", *, initial=None):
    token = proofs[-1].token if proofs else initial or str(uuid4())
    state = (
        (
            "rolled_back"
            if proofs[-1].terminal
            else ("open" if proofs[-1].key.action == "apply" else "rolling_back")
        )
        if proofs
        else "open"
    )
    table(data, store, "archive_phase_controls")["rows"] = [
        {
            **flat(binding, store),
            "id": Record("archive_phase_controls", "control"),
            "revision": len(proofs),
            "token": token,
            "state": state,
            "future": {
                "clock": Native("datetime", CLOCK),
                "null": Native("null", None),
                "none": None,
            },
        }
    ]
    rows = []
    for proof in proofs:
        rows.append(
            {
                **flat(binding, store),
                "id": Record("archive_phase_receipts", "receipt-" + str(proof.committed_revision)),
                "action": proof.key.action,
                "phase": proof.key.phase,
                "batch_sequence": proof.key.batch_sequence,
                "previous_revision": proof.previous_revision,
                "committed_revision": proof.committed_revision,
                "token": proof.token,
                "previous_token": proof.previous_token,
                "terminal": proof.terminal,
                "counts": [c.model_dump(mode="json") for c in proof.counts],
                "introduced": [r.model_dump(mode="json") for r in proof.introduced],
                "retired": [r.model_dump(mode="json") for r in proof.retired],
            }
        )
    table(data, store, "archive_phase_receipts")["rows"] = rows


def validate(data, scopes):
    return validate_archive_operator_history(seal(data), expected_scopes=scopes)


def prefix(left, right):
    return prepare_archive_history_prefix(
        left,
        right,
        scope_mapping=tuple(
            ArchiveHistoryScopeMapping(
                s,
                next(
                    t
                    for t in right.expected_scopes
                    if (t.store, t.organization_id) == (s.store, s.organization_id)
                ),
            )
            for s in left.expected_scopes
        ),
    )


@pytest.mark.parametrize("scheme", [None, "graph-association-authority-v2"])
def test_history_saved_pair_keeps_exact_old_bytes_budget_ceiling_and_scheme(dataset, scheme):
    data, _, binding, old = dataset
    run = table(data, "content", "archive_import_runs")["rows"][0]
    item = table(data, "content", "archive_import_artifacts")["rows"][0]
    plan = CheckedArchivePlan.model_validate(
        {**old.model_dump(mode="python"), "witness_scheme": scheme}
    )
    run["checked_plan_json"], run["checked_plan_sha256"] = (
        checked_plan_bytes(plan),
        checked_plan_digest(plan),
    )
    original = run["checked_plan_json"]
    pair = validate_saved_archive_pair(
        run,
        item,
        run_id=binding.run_id,
        organization_id=binding.organization_id,
        actor_id=binding.actor_id,
    )
    assert pair.checked_plan_json == original
    assert pair.plan.effective_witness_scheme == (scheme or "native-full-v1")
    assert pair.plan.credential.project_restricted and pair.plan.credential.project_ids == ()
    assert pair.plan.credential.memory_restricted and pair.plan.credential.memory_scope_keys == ()
    detached = pair.plan
    detached.counts.clear()
    assert pair.plan.counts
    assert pair.checked_plan_json == original
    assert pair.archive.original_budget.encoded_plan_bytes > len(original)


@pytest.mark.parametrize("fault", ["actor", "ceiling", "staged", "measurements", "plan", "version"])
def test_history_saved_pair_rejects_original_binding_corruption(dataset, fault):
    data, _, binding, _ = dataset
    run = table(data, "content", "archive_import_runs")["rows"][0]
    item = table(data, "content", "archive_import_artifacts")["rows"][0]
    if fault == "actor":
        item["actor_id"] = str(uuid4())
    elif fault == "ceiling":
        run["original_ceiling_json"] = "{}"
    elif fault == "staged":
        item["staged_payload_json"] += " "
    elif fault == "measurements":
        item["measured_sizes_json"] = "{}"
    elif fault == "plan":
        run["checked_plan_json"] += " "
    else:
        item["contract_version"] = True
    with pytest.raises(ArchiveArtifactIntegrityError):
        validate_saved_archive_pair(
            run,
            item,
            run_id=binding.run_id,
            organization_id=binding.organization_id,
            actor_id=binding.actor_id,
        )


def test_history_complete_two_store_run_and_historical_deleted_rows_are_valid(dataset):
    data, scopes, binding, _ = dataset
    for store in ("content", "graph"):
        install(data, binding, chain(binding, store), store)
    # Same physical IDs coexist in different databases, and live introduced rows
    # are absent. Historical SHA evidence does not demand their present image.
    result = validate(data, scopes)
    assert len(result.payload["chains"]) == 2
    assert all(
        c["control"]["image"]["value"]["state"] == "rolled_back" for c in result.payload["chains"]
    )
    detached = result.payload
    detached["chains"].clear()
    assert len(result.payload["chains"]) == 2
    assert not hasattr(result, "authority") and not hasattr(result, "executor")


def test_history_unknown_flexible_native_fields_remain_full_image_evidence(dataset):
    data, scopes, binding, _ = dataset
    install(data, binding, chain(binding)[:1])
    proof = table(data, "content", "archive_phase_receipts")["rows"][0]
    proof["counts"][0]["future"] = {
        "native": Record("external", ["x", Native("uuid", str(uuid4()))])
    }
    proof["introduced"][0]["future"] = Native("datetime", CLOCK)
    proof["future"] = {"null": Native("null", None), "none": None}
    run = table(data, "content", "archive_import_runs")["rows"][0]
    run["unknown"] = Native("datetime", CLOCK)
    result = validate(data, scopes)
    image = result.payload["chains"][0]["receipts"][0]["image"]
    assert image["value"]["introduced"][0]["future"] == CLOCK
    assert any(
        d["kind"] == "record" and d["path"] == ["counts", 0, "future", "native"]
        for d in image["native_types"]
    )
    assert "unknown" in result.payload["pairs"][0]["run"]["image"]["value"]
    assert result.payload["pairs"][0]["run"]["identity"]["identifier"]["value"][2]["part"] == "x\0y"


@pytest.mark.parametrize(
    "fault",
    [
        "gap",
        "fork",
        "unrotated",
        "rerotated",
        "after_terminal",
        "no_parent",
        "parent_changed",
        "tip_revision",
        "tip_token",
        "tip_state",
        "advanced_empty",
        "no_control",
        "flat_actor",
        "flat_store",
        "binding_json",
        "binding_sha",
        "count_bool",
        "count_missing",
        "count_created",
        "duplicate_kind",
        "wrong_store",
        "duplicate_control",
        "duplicate_batch",
        "duplicate_physical",
        "repeat_intro",
    ],
)
def test_history_rejects_global_event_cas_count_and_parent_drift(dataset, fault):
    data, scopes, binding, _ = dataset
    install(data, binding, chain(binding))
    controls = table(data, "content", "archive_phase_controls")["rows"]
    proofs = table(data, "content", "archive_phase_receipts")["rows"]
    if fault == "gap":
        proofs.pop(1)
    elif fault == "fork":
        proofs[1].update(previous_revision=0, committed_revision=1)
    elif fault == "unrotated":
        proofs[1]["token"] = proofs[1]["previous_token"]
    elif fault == "rerotated":
        proofs[2]["token"] = str(uuid4())
    elif fault == "after_terminal":
        proofs[1]["terminal"] = True
    elif fault == "no_parent":
        proofs[0]["counts"], proofs[0]["introduced"] = [], []
    elif fault == "parent_changed":
        proofs[1]["retired"][0]["introduced"]["body_sha256"] = "a" * 64
    elif fault == "tip_revision":
        controls[0]["revision"] = 4
    elif fault == "tip_token":
        controls[0]["token"] = str(uuid4())
    elif fault == "tip_state":
        controls[0]["state"] = "open"
    elif fault == "advanced_empty":
        proofs.clear()
    elif fault == "no_control":
        controls.clear()
    elif fault == "flat_actor":
        proofs[0]["actor_id"] = str(uuid4())
    elif fault == "flat_store":
        proofs[0]["store"] = "graph"
    elif fault == "binding_json":
        proofs[0]["binding_json"] += " "
    elif fault == "binding_sha":
        proofs[0]["binding_sha256"] = "b" * 64
    elif fault == "count_bool":
        proofs[0]["counts"][0]["created"] = True
    elif fault == "count_missing":
        del proofs[0]["counts"][0]["preserved"]
    elif fault == "count_created":
        proofs[0]["counts"][0]["created"] = 2
    elif fault == "duplicate_kind":
        proofs[0]["counts"].append(deepcopy(proofs[0]["counts"][0]))
    elif fault == "wrong_store":
        proofs[0]["introduced"][0].update(kind="graph_entity", physical_id="entity:historical")
        proofs[0]["counts"][0]["kind"] = "graph_entity"
    elif fault == "duplicate_control":
        controls.append({**deepcopy(controls[0]), "id": Record("archive_phase_controls", "other")})
    elif fault == "duplicate_batch":
        proofs[2]["batch_sequence"] = proofs[1]["batch_sequence"]
    elif fault == "duplicate_physical":
        proofs[2]["id"] = proofs[1]["id"]
    else:
        proofs[1].update(
            action="apply",
            phase="content_apply",
            retired=[],
            introduced=deepcopy(proofs[0]["introduced"]),
            counts=deepcopy(proofs[0]["counts"]),
            previous_token=proofs[0]["token"],
            token=proofs[0]["token"],
        )
    with pytest.raises((ValueError, KeyError)):
        validate(data, scopes)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_table",
        "unknown_table",
        "version",
        "schema",
        "row_table",
        "semantic_uuid",
        "missing_native_date",
        "pair_missing",
        "intake_duplicate",
        "expected_scope",
        "foreign_graph",
    ],
)
def test_history_rejects_membership_native_type_pair_and_affinity_drift(dataset, fault):
    data, scopes, binding, _ = dataset
    install(data, binding, chain(binding)[:1])
    scope = next(s for s in data["scopes"] if s["store"] == "content")
    if fault == "missing_table":
        scope["tables"] = [t for t in scope["tables"] if t["name"] != "source_states"]
        del scope["database_catalog"]["tables"]["source_states"]
    elif fault == "unknown_table":
        scope["tables"].append({"name": "foreign_authority", "catalog": {}, "rows": []})
        scope["database_catalog"]["tables"]["foreign_authority"] = (
            "DEFINE TABLE foreign_authority SCHEMAFULL"
        )
    elif fault == "version":
        table(data, "content", "schema_version")["rows"][0]["version"] += 1
    elif fault == "schema":
        scope["database_catalog"]["tables"]["source_states"] = (
            "DEFINE TABLE source_states SCHEMALESS"
        )
    elif fault == "row_table":
        table(data, "content", "archive_phase_controls")["rows"][0]["id"] = Record(
            "foreign", "control"
        )
    elif fault == "semantic_uuid":
        table(data, "content", "archive_phase_receipts")["rows"][0]["token"] = Native(
            "uuid", str(uuid4())
        )
    elif fault == "missing_native_date":
        table(data, "content", "archive_import_artifacts")["rows"][0]["created_at"] = CLOCK
    elif fault == "pair_missing":
        table(data, "content", "archive_import_artifacts")["rows"].clear()
    elif fault == "intake_duplicate":
        other = deepcopy(table(data, "content", "archive_import_runs")["rows"][0])
        other.update(uuid=str(uuid4()), id=Record("archive_import_runs", "another"))
        table(data, "content", "archive_import_runs")["rows"].append(other)
    elif fault == "expected_scope":
        scopes = tuple(replace(s, database="wrong") if s.store == "content" else s for s in scopes)
    else:
        table(data, "graph", "entity")["rows"].append(
            {"id": Record("entity", "foreign"), "group_id": str(uuid4())}
        )
    with pytest.raises(ValueError):
        validate(data, scopes)


def test_history_registered_empty_and_optional_diagnostic_absence_are_bound(dataset):
    data, scopes, _, _ = dataset
    for scope in data["scopes"]:
        scope["tables"] = [t for t in scope["tables"] if t["name"] != "schema_lease"]
        del scope["database_catalog"]["tables"]["schema_lease"]
        scope["absent_diagnostics"] = ["schema_lease"]
    result = validate(data, scopes)
    assert result.payload["chains"] == []
    assert prefix(result, result).payload["stores"][0]["outcome"] == "unclaimed"


@pytest.mark.parametrize(
    "source_len,target_len,outcome,missing",
    [
        (0, 0, "retain", 0),
        (3, 0, "append", 3),
        (1, 3, "retain", 0),
        (3, 1, "append", 2),
        (3, 3, "retain", 0),
    ],
)
def test_history_prefix_keeps_normal_seed_exact_history_and_later_closure(
    dataset, source_len, target_len, outcome, missing
):
    data, scopes, binding, _ = dataset
    proofs = chain(binding)
    source, target = deepcopy(data), deepcopy(data)
    install(source, binding, proofs[:source_len], initial=proofs[0].previous_token)
    install(target, binding, proofs[:target_len], initial=proofs[0].previous_token)
    table(target, "content", "source_states")["rows"] = [
        {
            "id": Record("source_states", "later"),
            "organization_id": binding.organization_id,
            "generation": 999,
            "incarnation": "retained-newer",
            "deleted": True,
        }
    ]
    result = prefix(validate(source, scopes), validate(target, scopes))
    plan = next(s for s in result.payload["stores"] if s["store"] == "content")
    assert plan["outcome"] == outcome and len(plan["missing_receipts"]) == missing
    assert plan["seed"] is None
    assert result.retained.root.native.value["scopes"][1]["tables"] != []
    assert table(target, "content", "source_states")["rows"][0]["generation"] == 999
    if target_len == 3:
        assert plan["retained"]["control"]["image"]["value"]["state"] == "rolled_back"
    assert not hasattr(result, "permit") and not hasattr(result, "execute")


def test_history_fresh_target_only_derives_normal_open0_without_creating_advanced_control(dataset):
    data, scopes, binding, _ = dataset
    source, target = deepcopy(data), deepcopy(data)
    proofs = chain(binding)
    install(source, binding, proofs)
    result = prefix(validate(source, scopes), validate(target, scopes))
    plan = next(p for p in result.payload["stores"] if p["store"] == "content")
    assert plan["outcome"] == "missing"
    assert plan["seed"]["value"]["revision"] == 0
    assert plan["seed"]["value"]["token"] == proofs[0].previous_token
    assert plan["seed"]["value"]["state"] == "open"
    original = plan["archived"]["control"]["image"]
    for name, value in original["value"].items():
        if name not in {"revision", "token", "state"}:
            assert plan["seed"]["value"][name] == value
    assert len(plan["missing_receipts"]) == 3
    assert plan["seed"]["native_types"] == original["native_types"]


@pytest.mark.parametrize(
    "fault", ["unknown", "null_none", "nanos", "physical", "seed", "pair", "control_physical"]
)
def test_history_prefix_rejects_full_native_image_or_identity_drift(dataset, fault):
    data, scopes, binding, _ = dataset
    proofs = chain(binding)[:1]
    source, target = deepcopy(data), deepcopy(data)
    install(source, binding, proofs)
    install(target, binding, proofs)
    left = table(source, "content", "archive_phase_receipts")["rows"][0]
    right = table(target, "content", "archive_phase_receipts")["rows"][0]
    if fault == "unknown":
        left["unknown"], right["unknown"] = 1, 2
    elif fault == "null_none":
        left["unknown"], right["unknown"] = Native("null", None), None
    elif fault == "nanos":
        left["unknown"], right["unknown"] = (
            Native("datetime", CLOCK),
            Native("datetime", CLOCK.replace("789", "790")),
        )
    elif fault == "physical":
        right["id"] = Record("archive_phase_receipts", "different")
    elif fault == "seed":
        table(target, "content", "archive_phase_controls")["rows"][0]["future"]["clock"] = Native(
            "datetime", CLOCK.replace("789", "790")
        )
    elif fault == "pair":
        table(target, "content", "archive_import_artifacts")["rows"][0]["unknown"] = Native(
            "null", None
        )
    else:
        table(target, "content", "archive_phase_controls")["rows"][0]["id"] = Record(
            "archive_phase_controls", "different"
        )
    with pytest.raises(ValueError):
        prefix(validate(source, scopes), validate(target, scopes))


def test_history_physical_mapping_cannot_alias_stores_or_rewrite_organization(dataset):
    data, scopes, _, _ = dataset
    result = validate(data, scopes)
    alias = replace(scopes[1], namespace=scopes[0].namespace, database=scopes[0].database)
    with pytest.raises(ValueError, match="alias"):
        validate_archive_operator_history(seal(data), expected_scopes=(scopes[0], alias, scopes[2]))
    with pytest.raises(ValueError, match="rewrite"):
        ArchiveHistoryScopeMapping(scopes[0], replace(scopes[0], organization_id=str(uuid4())))
    mapping = tuple(ArchiveHistoryScopeMapping(s, s) for s in scopes)
    with pytest.raises(ValueError, match="alias"):
        prepare_archive_history_prefix(result, result, scope_mapping=(*mapping, mapping[0]))


def test_history_unapproved_graph_organization_stays_explicitly_uncovered_and_inert(dataset):
    data, scopes, _, _ = dataset
    org = str(uuid4())
    table(data, "auth", "organizations")["rows"].append(
        {"id": Record("organizations", org), "uuid": org}
    )
    result = validate(data, scopes)
    compared = prefix(result, result)
    assert result.payload["uncovered_organization_ids"] == [org]
    assert compared.payload["uncovered_organization_ids"] == [org]
    assert all(p["organization_id"] != org for p in compared.payload["stores"])


@pytest.mark.parametrize("unicode", [False, True])
def test_history_saved_pair_keeps_character_and_utf8_original_admission(dataset, unicode):
    data, _, binding, _ = dataset
    run = table(data, "content", "archive_import_runs")["rows"][0]
    item = table(data, "content", "archive_import_artifacts")["rows"][0]
    sizes = json.loads(item["measured_sizes_json"])
    sizes["resource_budget"]["encoded_plan_bytes"] = 1024
    item["measured_sizes_json"] = canonical_json(sizes)
    run["checked_plan_json"] = ("💜" * 300) if unicode else ("x" * 1025)
    with pytest.raises(ArchiveArtifactIntegrityError, match="exceeds original admission"):
        validate_saved_archive_pair(
            run,
            item,
            run_id=binding.run_id,
            organization_id=binding.organization_id,
            actor_id=binding.actor_id,
        )


def second_organization(data, scopes, binding, plan):
    org, run_id, artifact_id = str(uuid4()), str(uuid4()), str(uuid4())
    other_plan = CheckedArchivePlan.model_validate(
        {**plan.model_dump(mode="python"), "organization_id": org}
    )
    run = deepcopy(table(data, "content", "archive_import_runs")["rows"][0])
    artifact = deepcopy(table(data, "content", "archive_import_artifacts")["rows"][0])
    run.update(
        id=Record("archive_import_runs", run_id),
        uuid=run_id,
        organization_id=org,
        artifact_id=artifact_id,
        checked_plan_json=checked_plan_bytes(other_plan),
        checked_plan_sha256=checked_plan_digest(other_plan),
    )
    artifact.update(
        id=Record("archive_import_artifacts", artifact_id),
        uuid=artifact_id,
        organization_id=org,
        run_id=run_id,
    )
    table(data, "content", "archive_import_runs")["rows"].append(run)
    table(data, "content", "archive_import_artifacts")["rows"].append(artifact)
    graph = deepcopy(next(scope for scope in data["scopes"] if scope["store"] == "graph"))
    graph.update(namespace="history_" + org.replace("-", ""), organization_id=org)
    other_binding = ArchiveRunBinding.model_validate(
        {
            **binding.model_dump(mode="python"),
            "organization_id": org,
            "run_id": run_id,
            "artifact_id": artifact_id,
            "checked_plan_sha256": checked_plan_digest(other_plan),
        }
    )
    for t in graph["tables"]:
        if t["name"] in ("archive_phase_controls", "archive_phase_receipts"):
            for row in t["rows"]:
                row.update(flat(other_binding, "graph"))
    data["scopes"].append(graph)
    data["namespace_catalog"]["namespaces"][graph["namespace"]] = "namespace-definition"
    table(data, "auth", "organizations")["rows"].append(
        {"id": Record("organizations", org), "uuid": org}
    )
    return (*scopes, ArchiveHistoryScope("graph", graph["namespace"], "graph", org))


def test_history_same_native_ids_in_distinct_graph_databases_do_not_collide(dataset):
    data, scopes, binding, plan = dataset
    install(data, binding, chain(binding, "graph"), "graph")
    scopes = second_organization(data, scopes, binding, plan)
    source, target = deepcopy(data), deepcopy(data)
    first = next(s for s in target["scopes"] if s["store"] == "graph")
    for t in first["tables"]:
        if t["name"] in ("archive_phase_controls", "archive_phase_receipts"):
            t["rows"].clear()
    result = prefix(validate(source, scopes), validate(target, scopes))
    graph_plans = [p for p in result.payload["stores"] if p["store"] == "graph"]
    assert sorted(p["outcome"] for p in graph_plans) == ["missing", "retain"]
    full = validate(source, scopes).payload["chains"]
    assert full[0]["control"]["identity"] == full[1]["control"]["identity"]
    assert (
        full[0]["control"]["encoded_subtree_sha256"] != full[1]["control"]["encoded_subtree_sha256"]
    )


def test_history_qualified_subtree_hash_uses_nul_delimited_canonical_frame(dataset):
    data, scopes, binding, _ = dataset
    install(data, binding, chain(binding)[:1])
    qualified = validate(data, scopes).payload["chains"][0]["control"]
    frame = {key: value for key, value in qualified.items() if key != "encoded_subtree_sha256"}
    encoded = json.dumps(
        frame, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()
    domain = b"sibyl-archive-qualified-typed-subtree-v1"
    assert (
        qualified["encoded_subtree_sha256"]
        == hashlib.sha256(domain + bytes([0]) + encoded).hexdigest()
    )
    assert (
        qualified["encoded_subtree_sha256"]
        != hashlib.sha256(domain + bytes([92, 48]) + encoded).hexdigest()
    )


def test_history_exact_mapping_can_relocate_namespace_without_rewriting_native_rows(dataset):
    data, scopes, binding, _ = dataset
    install(data, binding, chain(binding)[:1])
    target = deepcopy(data)
    target["graph_namespace_prefix"] = "relocated_"
    names = {}
    mapped = []
    for scope in target["scopes"]:
        scope["namespace"] = scope["namespace"].replace("history_", "relocated_", 1)
        names[scope["namespace"]] = "namespace-definition"
        mapped.append(
            ArchiveHistoryScope(
                scope["store"], scope["namespace"], scope["database"], scope["organization_id"]
            )
        )
    target["namespace_catalog"]["namespaces"] = names
    result = prefix(validate(data, scopes), validate(target, tuple(mapped)))
    content = next(s for s in result.payload["stores"] if s["store"] == "content")
    assert content["outcome"] == "retain"
    assert content["archived"]["control"]["image"] == content["retained"]["control"]["image"]
    assert (
        result.payload["scope_mapping"][0]["source"]["namespace"]
        != result.payload["scope_mapping"][0]["target"]["namespace"]
    )


def test_history_missing_saved_artifact_cannot_collide_with_retained_logical_uuid(dataset):
    data, scopes, _, _ = dataset
    target = deepcopy(data)
    run = table(target, "content", "archive_import_runs")["rows"][0]
    item = table(target, "content", "archive_import_artifacts")["rows"][0]
    run_id = str(uuid4())
    run.update(uuid=run_id, id=Record("archive_import_runs", "other-run"))
    item.update(run_id=run_id, id=Record("archive_import_artifacts", "other-physical"))
    # Both roots are locally valid and the artifact native IDs differ, but the
    # retained table's UUID unique index still owns the incoming logical UUID.
    with pytest.raises(ValueError, match="logical identity"):
        prefix(validate(data, scopes), validate(target, scopes))


def retained_intake_pair(data):
    target = deepcopy(data)
    run = table(target, "content", "archive_import_runs")["rows"][0]
    artifact = table(target, "content", "archive_import_artifacts")["rows"][0]
    run_id, artifact_id = str(uuid4()), str(uuid4())
    run.update(
        uuid=run_id,
        artifact_id=artifact_id,
        id=Record("archive_import_runs", ["retained", run_id]),
    )
    artifact.update(
        uuid=artifact_id,
        run_id=run_id,
        id=Record("archive_import_artifacts", ["retained", artifact_id]),
    )
    return target, run, artifact


@pytest.mark.parametrize("same_request", [False, True])
@pytest.mark.parametrize("relocated_content", [False, True])
def test_history_missing_pair_rejects_retained_intake_owner(
    dataset, same_request, relocated_content
):
    data, scopes, _, _ = dataset
    target, run, _ = retained_intake_pair(data)
    incoming = table(data, "content", "archive_import_runs")["rows"][0]
    run["request_sha256"] = incoming["request_sha256"] if same_request else "c" * 64
    target_scopes = scopes
    if relocated_content:
        content = next(s for s in target["scopes"] if s["store"] == "content")
        old_namespace = content["namespace"]
        content.update(namespace="relocated_content", database="recovery")
        content["namespace_catalog"]["databases"] = {"recovery": "database-definition"}
        del target["namespace_catalog"]["namespaces"][old_namespace]
        target["namespace_catalog"]["namespaces"]["relocated_content"] = "namespace-definition"
        target_scopes = tuple(
            replace(s, namespace="relocated_content", database="recovery")
            if s.store == "content"
            else s
            for s in scopes
        )
    archived, retained = validate(data, scopes), validate(target, target_scopes)
    assert incoming["uuid"] != run["uuid"]
    assert incoming["artifact_id"] != run["artifact_id"]
    assert all(
        incoming[name] == run[name] for name in ("organization_id", "actor_id", "intake_identity")
    )
    with pytest.raises(ValueError, match="intake identity"):
        prefix(archived, retained)


@pytest.mark.parametrize("distinct", ["intake", "actor", "organization"])
def test_history_missing_pair_allows_distinct_native_intake_key(dataset, distinct):
    data, scopes, _, plan = dataset
    target, run, artifact = retained_intake_pair(data)
    if distinct == "intake":
        run["intake_identity"] = "another-server-owned-check"
    else:
        field = "actor_id" if distinct == "actor" else "organization_id"
        value = str(uuid4())
        other_plan = CheckedArchivePlan.model_validate(
            {**plan.model_dump(mode="python"), field: value}
        )
        run.update(
            **{
                field: value,
                "checked_plan_json": checked_plan_bytes(other_plan),
                "checked_plan_sha256": checked_plan_digest(other_plan),
            }
        )
        artifact[field] = value
        if distinct == "organization":
            table(target, "auth", "organizations")["rows"].append(
                {"id": Record("organizations", value), "uuid": value}
            )
    archived, retained = validate(data, scopes), validate(target, scopes)
    result = prefix(archived, retained)
    incoming_id = archived.payload["pairs"][0]["run_id"]
    missing = next(p for p in result.payload["pairs"] if p["run_id"] == incoming_id)
    assert missing["outcome"] == "missing"
    assert missing["archived"] == archived.payload["pairs"][0]
    assert retained.payload["pairs"][0]["run"]["image"]["value"]["uuid"] == run["uuid"]
    if distinct == "organization":
        assert result.payload["uncovered_organization_ids"] == [run["organization_id"]]
    else:
        assert sorted(p["outcome"] for p in result.payload["pairs"]) == ["missing", "retain"]
