from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from sibyl_core.migrate.personal_archive_plan import (
    ArchiveAudience,
    ArchiveCredentialCeiling,
    ArchiveDisposition,
    ArchiveKind,
    ArchiveMappings,
    ArchiveSourceOrigin,
    CheckedArchivePlan,
    PlannedArchiveRow,
    canonical_json,
    checked_plan_bytes,
    checked_plan_digest,
    destination_identity,
    preview_counts,
    verify_checked_plan,
)


def _plan() -> CheckedArchivePlan:
    org, actor = str(uuid4()), str(uuid4())
    audience = ArchiveAudience(memory_scope="private", scope_key=actor)
    row = PlannedArchiveRow(
        kind=ArchiveKind.RAW_CAPTURE,
        original_id=str(uuid4()),
        destination_id=str(uuid4()),
        audience=audience,
        disposition=ArchiveDisposition.CREATED,
        reason="missing",
        semantic_sha256="a" * 64,
        protection="ordinary",
        declarations=2,
    )
    return CheckedArchivePlan(
        organization_id=org,
        actor_id=actor,
        archive_sha256="b" * 64,
        artifact_sha256="c" * 64,
        origin=ArchiveSourceOrigin(organization_id=str(uuid4()), source_store="surreal"),
        mappings=ArchiveMappings(source_private_owner_id="declared-owner", quarantine=audience),
        credential=ArchiveCredentialCeiling(credential_kind="session"),
        rows=(row,),
        counts=preview_counts((row,)),
    )


def test_checked_plan_detects_tampered_body_or_digest() -> None:
    plan = _plan()
    encoded, digest = checked_plan_bytes(plan), checked_plan_digest(plan)
    assert verify_checked_plan(encoded, digest) == plan
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_checked_plan(encoded.replace('"reason":"missing"', '"reason":"different"'), digest)
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_checked_plan(encoded, "0" * 64)


def test_checked_counts_reconcile_coalesced_declarations() -> None:
    plan = _plan()
    counts = plan.counts["raw_capture"]
    assert counts.created == 1
    assert counts.coalesced == 1
    assert counts.normalized_rows + counts.coalesced == 2
    with pytest.raises(ValidationError, match="do not reconcile"):
        CheckedArchivePlan.model_validate({**plan.model_dump(), "counts": {}})


def test_retired_and_foreign_protected_inputs_cannot_plan_a_live_create() -> None:
    row = _plan().rows[0]
    for category in ("retired", "protected", "inert"):
        with pytest.raises(ValidationError, match="cannot be created"):
            PlannedArchiveRow.model_validate({**row.model_dump(), "protection": category})


def test_destination_identity_is_stable_but_not_cross_actor_or_changed_origin_dedup() -> None:
    plan = _plan()
    arguments = {
        "organization_id": plan.organization_id,
        "actor_id": plan.actor_id,
        "origin": plan.origin,
        "kind": plan.rows[0].kind,
        "original_id": plan.rows[0].original_id,
        "audience": plan.rows[0].audience,
    }
    identity = destination_identity(**arguments)
    assert destination_identity(**arguments) == identity
    assert destination_identity(**{**arguments, "actor_id": str(uuid4())}) != identity
    changed_origin = ArchiveSourceOrigin(organization_id=str(uuid4()), source_store="surreal")
    assert destination_identity(**{**arguments, "origin": changed_origin}) != identity


def test_restricted_empty_ceiling_is_not_an_unrestricted_ceiling() -> None:
    key = str(uuid4())
    unrestricted = ArchiveCredentialCeiling(credential_kind="api_key", api_key_id=key)
    empty = ArchiveCredentialCeiling(
        credential_kind="api_key",
        api_key_id=key,
        project_restricted=True,
        memory_restricted=True,
    )
    assert canonical_json(unrestricted) != canonical_json(empty)
    assert empty.project_ids == empty.memory_scope_keys == ()
    with pytest.raises(ValidationError, match="restricted ceiling"):
        ArchiveCredentialCeiling(
            credential_kind="api_key", api_key_id=key, project_ids=("project-1",)
        )


def test_digest_encoder_rejects_nonfinite_and_implicit_objects() -> None:
    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            canonical_json({"value": value})
    with pytest.raises(TypeError):
        canonical_json({"value": uuid4()})
