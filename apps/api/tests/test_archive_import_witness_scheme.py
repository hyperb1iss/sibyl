from __future__ import annotations

import json
from uuid import uuid4

import pytest

from sibyl.api.routes import archive_import_preview as preview, archive_imports as routes
from sibyl.persistence.surreal.archive_import_artifacts import (
    SurrealArchiveImportArtifactRepository,
)
from sibyl.persistence.surreal.archive_import_runs import (
    CheckedArchiveArtifact,
    SurrealArchiveImportRunRepository,
)
from sibyl_core.migrate.personal_archive_plan import (
    CURRENT_ARCHIVE_WITNESS_SCHEME,
    CheckedArchivePlan,
    checked_plan_bytes,
    checked_plan_digest,
)
from sibyl_core.migrate.personal_archive_prepared import prepare_archive_records
from sibyl_core.services.operational_relationships import _ENDPOINT_WRITE_WITNESS, _SNAPSHOT
from tests import (
    test_archive_import_artifacts as artifact_fixtures,
    test_archive_import_preview as preview_fixtures,
    test_archive_imports as http_fixtures,
)

archive_auth = http_fixtures.archive_auth
archive_http = http_fixtures.archive_http
destination = preview_fixtures.destination
staged = artifact_fixtures.staged


@pytest.fixture
async def owned_destination(destination):
    _, content, graph, _ = destination
    try:
        yield destination
    finally:
        await content.execute_query(f"REMOVE NAMESPACE `{content.namespace}`;")
        await graph.execute_query(f"REMOVE NAMESPACE `{graph.namespace}`;")


async def _source(destination, kind):
    context, content, graph, _ = destination
    identity = str(uuid4())
    client = graph if kind == "graph_entity" else content
    if kind == "graph_entity":
        await client.execute_query(
            "CREATE entity CONTENT $record;",
            record={
                "uuid": identity,
                "group_id": context.organization_id,
                "entity_type": "topic",
                "name": "Private witness fixture",
                "content": "Stable source body",
                "attributes": {"memory_scope": "private", "principal_id": context.user_id},
            },
        )
    else:
        await client.execute_query(
            "CREATE raw_captures CONTENT $record;",
            record={
                "uuid": identity,
                "organization_id": context.organization_id,
                "source_id": identity,
                "principal_id": context.user_id,
                "memory_scope": "private",
                "scope_key": context.user_id,
                "raw_content": "Stable raw body",
                "title": "Private raw fixture",
                "metadata": {},
            },
        )
    await client.execute_query(
        "CREATE memory_derivations CONTENT $record;",
        record={
            "organization_id": context.organization_id,
            "target_kind": kind,
            "target_id": identity,
            "body_sha256": "a" * 64,
            "principal_id": context.user_id,
            "authority_ceiling": {},
            "observations": [],
            "active": True,
        },
    )
    return identity, client


async def _witnesses(destination, identity, kind="graph_entity"):
    context, _, _, _ = destination
    values = []
    for scheme in ("native-full-v1", CURRENT_ARCHIVE_WITNESS_SCHEME):
        content, graph = await preview._read_cuts(
            organization_id=context.organization_id,
            raw_ids=[identity] if kind == "raw_capture" else [],
            node_ids=[identity] if kind == "graph_entity" else [],
            edge_ids=[],
            witness_scheme=scheme,
        )
        values.append((content if kind == "raw_capture" else graph).witness(identity))
    return tuple(values)


async def test_witness_scheme_graph_canonical_validation_changes_only_legacy_hash(
    owned_destination,
):
    context, _, graph, _ = owned_destination
    identity, _ = await _source(owned_destination, "graph_entity")
    before = await _witnesses(owned_destination, identity)
    await graph.execute_query(
        "RETURN {" + _SNAPSHOT + _ENDPOINT_WRITE_WITNESS + "RETURN true;};",
        org=context.organization_id,
        ids=[identity],
        relationship_ids=[],
    )
    after = await _witnesses(owned_destination, identity)
    assert before[0].associations_sha256 != after[0].associations_sha256
    assert before[1].associations_sha256 == after[1].associations_sha256
    for original, current in zip(before, after, strict=True):
        assert original.row_sha256 == current.row_sha256
        assert original.state_sha256 == current.state_sha256
    # Saved authorization selects the validated plan's scheme, including old
    # saved winners after a server upgrade. Neither hash is translated.
    legacy = CheckedArchivePlan.model_validate(
        {
            "organization_id": context.organization_id,
            "actor_id": context.user_id,
            "archive_sha256": "a" * 64,
            "artifact_sha256": "b" * 64,
            "origin": {"organization_id": str(uuid4()), "source_store": "surreal"},
            "mappings": {
                "source_private_owner_id": "foreign-owner",
                "quarantine": {
                    "memory_scope": "private",
                    "scope_key": context.user_id,
                },
            },
            "credential": {"credential_kind": "session"},
            "rows": [
                {
                    "kind": "graph_entity",
                    "original_id": "foreign-source",
                    "destination_id": identity,
                    "disposition": "conflicted",
                    "protection": "ordinary",
                    "reason": "protected_destination",
                    "semantic_sha256": "c" * 64,
                    "audience": {"memory_scope": "private", "scope_key": context.user_id},
                    "witnesses": [before[0].model_dump(mode="python")],
                }
            ],
            "counts": {"graph_entity": {"conflicted": 1}},
        }
    )
    _, _, _, calls = owned_destination
    for marker, query in (
        (None, preview._GRAPH_CUT),
        (CURRENT_ARCHIVE_WITNESS_SCHEME, preview._GRAPH_AUTHORITY_CUT),
    ):
        plan = CheckedArchivePlan.model_validate(
            {**legacy.model_dump(mode="python"), "witness_scheme": marker}
        )
        await preview.authorize_archive_plan(
            plan=plan,
            context=context,
            request=preview_fixtures._request(),
        )
        assert calls[-1][0] == "graph"
        assert calls[-1][1] == query
    assert checked_plan_bytes(legacy).find('"witness_scheme"') == -1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active", False),
        ("body_sha256", "b" * 64),
        ("principal_id", "changed-principal"),
        ("authority_ceiling", {"memory_scope": "project"}),
        ("observations", [{"source_kind": "raw_capture", "source_id": "different-source"}]),
    ],
)
async def test_witness_scheme_preserves_every_graph_association_authority_field(
    owned_destination, field, value
):
    identity, graph = await _source(owned_destination, "graph_entity")
    before = await _witnesses(owned_destination, identity)
    await graph.execute_query(
        f"UPDATE memory_derivations SET {field}=$value WHERE target_id=$identity;",  # noqa: S608
        value=value,
        identity=identity,
    )
    after = await _witnesses(owned_destination, identity)
    assert all(
        a.associations_sha256 != b.associations_sha256 for a, b in zip(before, after, strict=True)
    )
    assert all(a.row_sha256 == b.row_sha256 for a, b in zip(before, after, strict=True))


async def test_witness_scheme_preserves_native_graph_association_physical_identity(
    owned_destination,
):
    identity, graph = await _source(owned_destination, "graph_entity")
    before = await _witnesses(owned_destination, identity)
    await graph.execute_query(
        "LET $original = SELECT * OMIT id FROM memory_derivations WHERE target_id=$identity;"
        "DELETE memory_derivations WHERE target_id=$identity;"
        "CREATE memory_derivations CONTENT $original[0];",
        identity=identity,
    )
    after = await _witnesses(owned_destination, identity)
    assert all(
        a.associations_sha256 != b.associations_sha256 for a, b in zip(before, after, strict=True)
    )
    assert all(a.state_sha256 == b.state_sha256 for a, b in zip(before, after, strict=True))


@pytest.mark.parametrize(
    ("field", "value"),
    [("incarnation", "changed-incarnation"), ("generation", 2), ("revision", 2), ("deleted", True)],
)
async def test_witness_scheme_preserves_source_state_authority(owned_destination, field, value):
    identity, graph = await _source(owned_destination, "graph_entity")
    before = await _witnesses(owned_destination, identity)
    await graph.execute_query(
        f"UPDATE source_states SET {field}=$value WHERE source_id=$identity;",  # noqa: S608
        value=value,
        identity=identity,
    )
    after = await _witnesses(owned_destination, identity)
    assert all(a.state_sha256 != b.state_sha256 for a, b in zip(before, after, strict=True))
    assert all(a.row_sha256 == b.row_sha256 for a, b in zip(before, after, strict=True))


async def test_witness_scheme_keeps_full_raw_association_semantics(owned_destination):
    identity, content = await _source(owned_destination, "raw_capture")
    before = await _witnesses(owned_destination, identity, "raw_capture")
    assert before[0] == before[1]
    await content.execute_query(
        "UPDATE memory_derivations SET body_sha256=$digest WHERE target_id=$identity;",
        digest="b" * 64,
        identity=identity,
    )
    after = await _witnesses(owned_destination, identity, "raw_capture")
    assert after[0] == after[1]
    assert before[0].associations_sha256 != after[0].associations_sha256


async def test_witness_scheme_saved_pair_load_preserves_both_versions_and_original_ceiling(staged):
    client, saved, old, parsed = staged
    loader = SurrealArchiveImportArtifactRepository(client)
    loaded = await loader.load(
        str(saved.record["uuid"]), organization_id=old.organization_id, actor_id=old.actor_id
    )
    assert loaded.plan == old
    assert loaded.checked_plan_json == checked_plan_bytes(old)
    assert loaded.checked_plan_sha256 == checked_plan_digest(old)
    assert "witness_scheme" not in json.loads(loaded.checked_plan_json)
    new = CheckedArchivePlan.model_validate(
        {**old.model_dump(mode="python"), "witness_scheme": CURRENT_ARCHIVE_WITNESS_SCHEME}
    )
    marked = await SurrealArchiveImportRunRepository(client).create_checked(
        plan=new,
        artifact=CheckedArchiveArtifact(
            parsed.archive_sha256,
            parsed.artifact_sha256,
            parsed.member_inventory_json,
            parsed.staged_payload_json,
            parsed.measured_sizes_json,
        ),
        intake_identity="marked-loader-control",
        request_sha256="d" * 64,
    )
    marked_loaded = await loader.load(
        str(marked.record["uuid"]), organization_id=new.organization_id, actor_id=new.actor_id
    )
    assert marked_loaded.plan == new
    assert marked_loaded.plan.effective_witness_scheme == CURRENT_ARCHIVE_WITNESS_SCHEME
    assert marked_loaded.plan.contract_version == loaded.plan.contract_version == 1
    assert marked_loaded.plan.credential == loaded.plan.credential
    assert marked_loaded.plan.credential.project_restricted
    assert marked_loaded.plan.credential.project_ids == ()
    repeated = await loader.load(
        loaded.run_id, organization_id=old.organization_id, actor_id=old.actor_id
    )
    assert repeated.checked_plan_json == loaded.checked_plan_json
    assert repeated.checked_plan_sha256 == loaded.checked_plan_sha256


async def test_witness_scheme_fresh_http_check_and_prepared_pair_are_server_owned(
    archive_http, tmp_path
):
    fixture = archive_http
    payload, options = http_fixtures._personal_archive(tmp_path, fixture.context.user_id)
    checked = await fixture.post(payload, options, operation="marked-check")
    assert checked.status_code == 200, checked.text
    loader = SurrealArchiveImportArtifactRepository(fixture.content)
    loaded = await loader.load(
        checked.json()["run_id"],
        organization_id=fixture.context.organization_id,
        actor_id=fixture.context.user_id,
    )
    assert loaded.plan.effective_witness_scheme == CURRENT_ARCHIVE_WITNESS_SCHEME
    prepared = prepare_archive_records(
        loaded.archive.materialize(),
        loaded.plan,
        run_id=loaded.run_id,
        artifact_id=loaded.artifact_id,
    )
    assert prepared.checked_plan_json == loaded.checked_plan_json
    assert prepared.plan.effective_witness_scheme == CURRENT_ARCHIVE_WITNESS_SCHEME
    before = await fixture.counts()
    supplied = {**json.loads(options), "witness_scheme": "native-full-v1"}
    rejected = await fixture.post(payload, json.dumps(supplied), operation="public-marker")
    assert rejected.status_code in {400, 422}, rejected.text
    assert await fixture.counts() == before


async def test_witness_scheme_same_operation_retains_legacy_saved_winner(
    archive_http, tmp_path, monkeypatch
):
    fixture = archive_http
    payload, options = http_fixtures._personal_archive(tmp_path, fixture.context.user_id)
    original_preview, original_plan = routes.build_archive_preview, routes._build_checked_plan

    async def legacy_preview(**kwargs):
        return await original_preview(**{**kwargs, "witness_scheme": "native-full-v1"})

    def legacy_plan(*args):
        plan = original_plan(*args)
        return CheckedArchivePlan.model_validate(
            {
                key: value
                for key, value in plan.model_dump(mode="python").items()
                if key != "witness_scheme"
            }
        )

    monkeypatch.setattr(routes, "build_archive_preview", legacy_preview)
    monkeypatch.setattr(routes, "_build_checked_plan", legacy_plan)
    original = await fixture.post(payload, options, operation="legacy-saved-winner")
    assert original.status_code == 200, original.text
    loader = SurrealArchiveImportArtifactRepository(fixture.content)
    saved = await loader.load(
        original.json()["run_id"],
        organization_id=fixture.context.organization_id,
        actor_id=fixture.context.user_id,
    )
    assert saved.plan.effective_witness_scheme == "native-full-v1"
    monkeypatch.setattr(routes, "build_archive_preview", original_preview)
    monkeypatch.setattr(routes, "_build_checked_plan", original_plan)
    repeated = await fixture.post(payload, options, operation="legacy-saved-winner")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {**original.json(), "replayed": True}
    replayed = await loader.load(
        saved.run_id,
        organization_id=fixture.context.organization_id,
        actor_id=fixture.context.user_id,
    )
    assert replayed.checked_plan_json == saved.checked_plan_json
    assert replayed.checked_plan_sha256 == saved.checked_plan_sha256
    assert replayed.plan.credential == saved.plan.credential
    fresh = await fixture.post(payload, options, operation="fresh-marked-winner")
    assert fresh.status_code == 200, fresh.text
    loaded = await loader.load(
        fresh.json()["run_id"],
        organization_id=fixture.context.organization_id,
        actor_id=fixture.context.user_id,
    )
    assert loaded.plan.effective_witness_scheme == CURRENT_ARCHIVE_WITNESS_SCHEME
    assert await fixture.counts() == {
        "archive_import_runs": 2,
        "archive_import_artifacts": 2,
        "raw_captures": 0,
    }
