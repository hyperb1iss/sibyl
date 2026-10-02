from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sibyl_core.migrate.personal_archive_plan import (
    CURRENT_ARCHIVE_WITNESS_SCHEME,
    CheckedArchivePlan,
    canonical_json,
    checked_plan_bytes,
    checked_plan_digest,
    verify_checked_plan,
)
from sibyl_core.migrate.personal_archive_prepared import PreparedArchiveRecords, PreparedArchiveRow

# Captured from the preceding plan implementation, including nullable witnesses
# and the original empty restricted ceiling. These are stored bytes, not a
# reconstruction through the current model's defaults.
_LEGACY_JSON = (
    '{"actor_id":"22222222-2222-4222-8222-222222222222","archive_sha256":"aaaaaaaaaaaaaaaaaaaaa'
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","artifact_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbb'
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","conflict_policy":"additive","contract_version":1,'
    '"counts":{"graph_entity":{"coalesced":0,"conflicted":1,"created":0,"quarantined":0,"skippe'
    'd":0}},"credential":{"api_key_id":"44444444-4444-4444-8444-444444444444","credential_kind"'
    ':"api_key","memory_restricted":true,"memory_scope_keys":[],"memory_space_ids":[],"project_'
    'ids":[],"project_restricted":true,"rest_scopes":["api:write"]},"mappings":{"projects":{},"'
    'quarantine":{"memory_scope":"private","scope_key":"22222222-2222-4222-8222-222222222222"},'
    '"source_private_owner_id":"foreign-owner","teams":{}},"organization_id":"11111111-1111-411'
    '1-8111-111111111111","origin":{"organization_id":"33333333-3333-4333-8333-333333333333","s'
    'ource_store":"surreal"},"rows":[{"audience":{"memory_scope":"private","scope_key":"2222222'
    '2-2222-4222-8222-222222222222"},"declarations":1,"destination_id":"55555555-5555-4555-8555'
    '-555555555555","disposition":"conflicted","endpoint_ids":[],"kind":"graph_entity","origina'
    'l_id":"foreign-node","protection":"ordinary","reason":"protected_destination","semantic_sh'
    'a256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","witnesses":[{"as'
    'sociations_sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","ide'
    'ntity":"entity:55555555-5555-4555-8555-555555555555","row_sha256":"ddddddddddddddddddddddd'
    'ddddddddddddddddddddddddddddddddddddddddd","state_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
    'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","store":"graph"}]}]}'
)
_LEGACY_SHA256 = "9d44f720303480147dbb8398b41364e2b12c6cab1fb70931f3bf469a30ebe1da"


def test_witness_scheme_preserves_historical_checked_bytes_and_prepared_binding():
    plan = verify_checked_plan(_LEGACY_JSON, _LEGACY_SHA256)
    assert plan.effective_witness_scheme == "native-full-v1"
    for mode in ("json", "python"):
        snapshot = plan.model_dump(mode=mode)
        assert "witness_scheme" not in snapshot
        assert "effective_witness_scheme" not in snapshot
        assert CheckedArchivePlan.model_validate(snapshot) == plan
    assert checked_plan_bytes(plan) == _LEGACY_JSON
    assert checked_plan_digest(plan) == _LEGACY_SHA256
    prepared = PreparedArchiveRecords(
        str(uuid4()),
        str(uuid4()),
        _LEGACY_JSON,
        _LEGACY_SHA256,
        tuple(PreparedArchiveRow(canonical_json(row), None) for row in plan.rows),
    )
    assert prepared.plan == plan
    assert prepared.binding.checked_plan_sha256 == _LEGACY_SHA256
    assert prepared.plan.credential.project_restricted
    assert prepared.plan.credential.project_ids == ()
    assert prepared.plan.credential.memory_restricted
    assert prepared.plan.rows[0].witnesses[0].associations_sha256 == "f" * 64


def test_witness_scheme_is_explicit_and_bound_without_rewriting_old_plan():
    old = verify_checked_plan(_LEGACY_JSON, _LEGACY_SHA256)
    new = CheckedArchivePlan.model_validate(
        {**old.model_dump(mode="python"), "witness_scheme": CURRENT_ARCHIVE_WITNESS_SCHEME}
    )
    encoded, digest = checked_plan_bytes(new), checked_plan_digest(new)
    assert new.contract_version == old.contract_version == 1
    assert new.effective_witness_scheme == CURRENT_ARCHIVE_WITNESS_SCHEME
    assert json.loads(encoded)["witness_scheme"] == CURRENT_ARCHIVE_WITNESS_SCHEME
    assert digest != _LEGACY_SHA256
    assert verify_checked_plan(encoded, digest) == new
    assert old.rows == new.rows and old.credential == new.credential
    assert checked_plan_bytes(old) == _LEGACY_JSON
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_checked_plan(encoded, _LEGACY_SHA256)
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_checked_plan(_LEGACY_JSON, digest)


@pytest.mark.parametrize("marker", ["native-full-v1", "unknown-v3", True, 2, {}])
def test_witness_scheme_rejects_unknown_or_coerced_markers(marker):
    with pytest.raises(ValidationError):
        CheckedArchivePlan.model_validate({**json.loads(_LEGACY_JSON), "witness_scheme": marker})


def test_witness_scheme_rejects_explicit_null_as_noncanonical_stored_bytes():
    encoded = canonical_json({**json.loads(_LEGACY_JSON), "witness_scheme": None})
    supplied_digest = hashlib.sha256(b"sibyl-archive-plan-v1\0" + encoded.encode()).hexdigest()
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_checked_plan(encoded, supplied_digest)


_NULLABLE_LEGACY_JSON = (
    '{"actor_id":"22222222-2222-4222-8222-222222222222","archive_sha256":"aaaaaaaaaaaaaaaa'
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","artifact_sha256":"bbbbbbbbbbbbbbbb'
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","conflict_policy":"additive","contr'
    'act_version":1,"counts":{"graph_entity":{"coalesced":0,"conflicted":1,"created":0,"qu'
    'arantined":0,"skipped":0}},"credential":{"api_key_id":null,"credential_kind":"session'
    '","memory_restricted":true,"memory_scope_keys":[],"memory_space_ids":[],"project_ids"'
    ':[],"project_restricted":true,"rest_scopes":["api:write"]},"mappings":{"projects":{},'
    '"quarantine":{"memory_scope":"private","scope_key":"22222222-2222-4222-8222-222222222'
    '222"},"source_private_owner_id":"foreign-owner","teams":{}},"organization_id":"111111'
    '11-1111-4111-8111-111111111111","origin":{"organization_id":"33333333-3333-4333-8333-'
    '333333333333","source_store":"surreal"},"rows":[{"audience":{"memory_scope":"private"'
    ',"scope_key":"22222222-2222-4222-8222-222222222222"},"declarations":1,"destination_id'
    '":"55555555-5555-4555-8555-555555555555","disposition":"conflicted","endpoint_ids":[]'
    ',"kind":"graph_entity","original_id":"foreign-node","protection":"ordinary","reason":'
    '"protected_destination","semantic_sha256":"cccccccccccccccccccccccccccccccccccccccccc'
    'cccccccccccccccccccccc","witnesses":[{"associations_sha256":null,"identity":"entity:5'
    '5555555-5555-4555-8555-555555555555","row_sha256":null,"state_sha256":null,"store":"g'
    'raph"}]}]}'
)
_NULLABLE_LEGACY_SHA256 = "39e6806c98be74a683f0febcc3c000b0e09c2df5e130cb342cf85635392ad5ee"


def test_witness_scheme_preserves_historical_null_authority_fields():
    plan = verify_checked_plan(_NULLABLE_LEGACY_JSON, _NULLABLE_LEGACY_SHA256)
    snapshot = plan.model_dump(mode="json")
    assert "witness_scheme" not in snapshot
    assert "api_key_id" in snapshot["credential"]
    assert snapshot["credential"]["api_key_id"] is None
    for field in ("row_sha256", "state_sha256", "associations_sha256"):
        assert field in snapshot["rows"][0]["witnesses"][0]
        assert snapshot["rows"][0]["witnesses"][0][field] is None
    assert checked_plan_bytes(plan) == _NULLABLE_LEGACY_JSON
    assert checked_plan_digest(plan) == _NULLABLE_LEGACY_SHA256
    prepared = PreparedArchiveRecords(
        str(uuid4()),
        str(uuid4()),
        _NULLABLE_LEGACY_JSON,
        _NULLABLE_LEGACY_SHA256,
        tuple(PreparedArchiveRow(canonical_json(row), None) for row in plan.rows),
    )
    assert prepared.plan.credential.api_key_id is None
    assert prepared.checked_plan_json == _NULLABLE_LEGACY_JSON
