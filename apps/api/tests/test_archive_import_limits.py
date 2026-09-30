from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sibyl.api.routes import archive_import_limits as limits
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveAudience,
    ArchiveCredentialCeiling,
    ArchiveDisposition,
    ArchiveKind,
    ArchiveMappings,
    ArchiveSourceOrigin,
    CheckedArchivePlan,
    PlannedArchiveRow,
    checked_plan_bytes,
    preview_counts,
)


@pytest.fixture(autouse=True)
def isolated_archive_settings(monkeypatch):
    # Copy the current settings without mutating unrelated shared test fields.
    monkeypatch.setattr(limits, "settings", limits.settings.model_copy(deep=True))
    limits.settings.archive_import_metadata_transaction_bytes = None
    limits.settings.archive_import_request_bytes = None


@pytest.mark.parametrize("scheme", ["ws", "wss", "http", "https", "memory", "surrealkv"])
def test_archive_limits_follow_native_transport_resource(scheme):
    limits.settings.surreal_url = scheme + "://archive.test"
    limits.settings.surreal_data_dir = ""
    intake, _ = limits.archive_import_budgets()
    expected = (4 if scheme in {"http", "https"} else 64) * 1024**2
    assert intake.metadata_transaction_bytes == expected
    # Observed HTTP acceptance is above a 3 MiB payload once the full envelope
    # is included; admission preserves that positive under the 4 MiB resource.
    assert intake.metadata_transaction_bytes >= 3_148_653
    if scheme in {"http", "https"}:
        assert intake.metadata_transaction_bytes < 5_245_805
    else:
        assert intake.metadata_transaction_bytes >= 39_155_167
        assert intake.metadata_transaction_bytes >= 30_678_844


@pytest.mark.parametrize("scheme", ["http", "ws"])
def test_archive_limits_expanded_transport_override_does_not_nerf_other_resources(scheme):
    limits.settings.surreal_url = scheme + "://archive.test"
    limits.settings.archive_import_metadata_transaction_bytes = 128 * 1024**2
    intake, _ = limits.archive_import_budgets()
    assert intake.metadata_transaction_bytes == 128 * 1024**2
    assert intake.inflated_bytes >= 29_362_325
    assert intake.encoded_artifact_bytes >= 39_150_362
    assert intake.encoded_plan_bytes >= 8_691_011


def test_archive_limits_derived_request_envelope_tracks_configured_parts():
    limits.settings.archive_import_compressed_bytes = 1234
    limits.settings.archive_import_options_bytes = 456
    limits.settings.archive_import_header_bytes = 100
    intake, upload = limits.archive_import_budgets()
    assert upload.request_bytes == 1234 + 456 + 200 + 1024
    assert intake.compressed_bytes == 1234
    limits.settings.archive_import_request_bytes = 9876
    assert limits.archive_import_budgets()[1].request_bytes == 9876


def _plan():
    actor = str(uuid4())
    audience = ArchiveAudience(memory_scope="private", scope_key=actor)
    row = PlannedArchiveRow(
        kind=ArchiveKind.RAW_CAPTURE,
        original_id="foreign-identity-界",
        destination_id=str(uuid4()),
        audience=audience,
        disposition=ArchiveDisposition.CREATED,
        reason="ordinary source candidate",
        semantic_sha256="c" * 64,
        protection="ordinary",
    )
    return CheckedArchivePlan(
        organization_id=str(uuid4()),
        actor_id=actor,
        archive_sha256="a" * 64,
        artifact_sha256="b" * 64,
        origin=ArchiveSourceOrigin(organization_id=str(uuid4()), source_store="surreal"),
        mappings=ArchiveMappings(source_private_owner_id="untrusted-owner", quarantine=audience),
        credential=ArchiveCredentialCeiling(credential_kind="session"),
        rows=(row,),
        counts=preview_counts((row,)),
    )


def test_archive_limits_count_encoded_utf8_plan_bytes_at_exact_boundary():
    plan = _plan()
    encoded = checked_plan_bytes(plan)
    size = len(encoded.encode("utf-8"))
    assert size > len(encoded)
    budget, _ = limits.archive_import_budgets()
    limits.validate_archive_plan_capacity(plan, replace(budget, encoded_plan_bytes=size))
    with pytest.raises(limits.ArchiveIntakeCapacityError, match="encoded-plan"):
        limits.validate_archive_plan_capacity(plan, replace(budget, encoded_plan_bytes=size - 1))
    # An unrelated healthy plan remains acceptable after the rejected boundary.
    limits.validate_archive_plan_capacity(_plan(), budget)


def test_archive_limits_revalidate_mutable_nested_plan_contract():
    plan = _plan()
    budget, _ = limits.archive_import_budgets()
    plan.counts.clear()
    with pytest.raises(ValidationError, match="reconcile"):
        limits.validate_archive_plan_capacity(plan, budget)
    limits.validate_archive_plan_capacity(_plan(), budget)


@pytest.mark.parametrize(
    "field",
    [
        "archive_import_compressed_bytes",
        "archive_import_inflated_bytes",
        "archive_import_member_bytes",
        "archive_import_members",
        "archive_import_json_depth",
        "archive_import_json_scalar_bytes",
        "archive_import_json_nodes",
        "archive_import_parsed_rows",
        "archive_import_encoded_artifact_bytes",
        "archive_import_encoded_plan_bytes",
        "archive_import_metadata_transaction_bytes",
        "archive_import_options_bytes",
        "archive_import_header_bytes",
        "archive_import_request_bytes",
    ],
)
def test_archive_limits_invalid_configuration_cannot_disable_resource_boundary(field):
    setattr(limits.settings, field, 0)
    with pytest.raises(ValueError, match="positive integers"):
        limits.archive_import_budgets()
