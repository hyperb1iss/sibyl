"""Read-only current authority and exact source/private-runtime callbacks.

The caller supplies an already authenticated intake context, never an arbitrary
key ID as authentication. Actual intake and source/runtime adoption are separate
root-owned operations. No credential reader or application bootstrap lives here.

Every application import below is deliberately deferred: importing this module
must not drag the API app, its settings or its database runtime into a process
that only wants the preparation contract.
"""
# ruff: noqa: PLC0415

import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from benchmarks.agent_tasks.screen48.contract import bound, digest, sha
from benchmarks.agent_tasks.screen48.recall.owners import runtime_pin
from benchmarks.agent_tasks.screen48.recall.whole_items import MissingPack

# Every owner the recall and raw lanes read through must already be imported from
# the bound source tree before a pack is prepared.
REQUIRED_OWNERS = frozenset(
    {
        "sibyl.auth.dependencies",
        "sibyl_core.retrieval.search",
        "sibyl_core.services.content_raw_recall",
        "sibyl_core.services.source_archive_store",
    }
)
OWNED_ROLES = frozenset({"owner", "admin"})


class CurrentAuthority:
    """Refresh the original authenticated key and intersect its intake ceiling."""

    def __init__(self, authenticated_context):
        from sibyl.auth.context import AuthContext

        if not isinstance(authenticated_context, AuthContext):
            raise MissingPack("authenticated_intake_context_required")
        self.intake = authenticated_context
        if not self.intake.api_key_id or not self.intake.user_id or not self.intake.organization_id:
            raise MissingPack("retained_authenticated_owner_key_required")

    async def __call__(self, organization_id, principal_id):
        from sibyl.auth.dependencies import _api_key_allows_rest, _api_key_claims
        from sibyl.persistence.auth_runtime import (
            list_accessible_delegated_scope_keys,
            list_accessible_project_graph_ids,
            list_accessible_team_scope_keys,
            resolve_api_key_authority,
            resolve_auth_context,
        )
        from sibyl.services.operational_authority import _intersection
        from sibyl_core.services.memory_source_validation import SourceReadAuthority

        saved = self.intake
        if (organization_id, principal_id) != (saved.organization_id, saved.user_id):
            raise MissingPack("intake_owner_changed")
        key = await resolve_api_key_authority(
            api_key_id=UUID(saved.api_key_id),
            organization_id=UUID(organization_id),
            user_id=UUID(principal_id),
        )
        if key is None:
            raise MissingPack("current_key_revoked")
        scopes = sorted(set(key.scopes).intersection(saved.scopes))
        if not _api_key_allows_rest(scopes=scopes, method="GET"):
            raise MissingPack("current_rest_read_denied")
        ctx = await resolve_auth_context(claims=_api_key_claims(key, scopes=scopes))
        if (
            ctx.organization_id != organization_id
            or ctx.user_id != principal_id
            or str(getattr(ctx.org_role, "value", ctx.org_role)) not in OWNED_ROLES
        ):
            raise MissingPack("current_owner_membership_denied")
        ctx = replace(
            ctx,
            api_key_project_ids=_intersection(ctx.api_key_project_ids, saved.api_key_project_ids),
            api_key_memory_scope_keys=_intersection(
                ctx.api_key_memory_scope_keys, saved.api_key_memory_scope_keys
            ),
        )
        return SourceReadAuthority(
            principal_id=principal_id,
            projects=frozenset(await list_accessible_project_graph_ids(ctx)),
            teams=frozenset(await list_accessible_team_scope_keys(ctx)),
            delegations=frozenset(await list_accessible_delegated_scope_keys(ctx)),
            scope_keys=ctx.api_key_memory_scope_keys,
        )


class CurrentOwners:
    """Verify immutable source, imported modules and the private dependency copy.

    Every external location is a constructor argument. The source tree, its
    manifest, the manifest's expected digest, the base commit it claims and the
    private dependency runtime all belong to the eval host, not to this package.
    """

    def __init__(
        self,
        *,
        source,
        source_manifest,
        source_manifest_sha256,
        source_commit,
        dependency_runtime,
        owned_database_url,
    ):
        self.source = Path(source).resolve(strict=True)
        self.source_manifest = Path(source_manifest)
        self.source_manifest_sha256 = source_manifest_sha256
        self.source_commit = source_commit
        self.dependency_runtime = dict(dependency_runtime)
        self.owned_database_url = owned_database_url
        self.last_imports = {}

    # One qualification pass: manifest, file inventory, symlinks, live imports,
    # database configuration and the private runtime. Every branch is a
    # separate way the owned source can have moved under the lane.
    def __call__(self):  # noqa: PLR0912
        from sibyl.config import settings as api_settings
        from sibyl_core.config import settings as core_settings

        manifest = json.loads(bound(self.source_manifest, self.source_manifest_sha256))
        if manifest["base_commit"] != self.source_commit:
            raise MissingPack("current_source_commit_changed")
        files, links = {}, {}
        for path in self.source.rglob("*"):
            rel = str(path.relative_to(self.source))
            if path.is_symlink():
                links[rel] = str(path.readlink())
                if not path.resolve(strict=True).is_relative_to(self.source):
                    raise MissingPack("source_symlink_escaped")
            elif path.is_file():
                files[rel] = sha(path.read_bytes())
            elif not path.is_dir():
                raise MissingPack("unsupported_source_file")
        if files != manifest["files"] or links != manifest["symlinks"]:
            raise MissingPack("current_source_inventory_changed")
        imports = {}
        for name, module in list(sys.modules.items()):
            if name.split(".")[0] in {"sibyl", "sibyl_core", "benchmarks"} and getattr(
                module, "__file__", None
            ):
                path = Path(module.__file__).resolve(strict=True)
                if not path.is_relative_to(self.source):
                    raise MissingPack(f"imported_application_owner_escaped:{name}:{path}")
                rel = str(path.relative_to(self.source))
                if rel not in files:
                    raise MissingPack("imported_application_owner_unbound")
                imports[name] = {"path": rel, "sha256": files[rel]}
        if not set(imports) >= REQUIRED_OWNERS:
            raise MissingPack("current_required_owners_not_imported")
        if (
            api_settings.disable_auth
            or str(api_settings.surreal_url) != self.owned_database_url
            or core_settings.resolved_surreal_url != self.owned_database_url
        ):
            raise MissingPack("authenticated_owned_database_configuration_changed")
        runtime = runtime_pin.verify(self.dependency_runtime, worker=True)
        self.last_imports = imports
        # Lazy imports may grow during a call. Every import is checked; the
        # stable comparison binds the full source manifest, not import timing.
        return {
            "schema": "sibyl-current-recall-owner-qualification-v1",
            "source_commit": self.source_commit,
            "source_manifest_sha256": self.source_manifest_sha256,
            "dependency_runtime": runtime,
            "owned_database_url": self.owned_database_url,
            "auth_enabled": True,
            "public_configuration_sha256": digest(
                {
                    "api": api_settings.model_dump(mode="json"),
                    "core": core_settings.model_dump(mode="json"),
                }
            ),
        }


class OriginalQualificationFailure(MissingPack):
    def __init__(self, receipt):
        super().__init__("current_original_sources_unavailable")
        self.receipt = receipt


async def qualify_originals(
    *, material, source, resolve_authority, verify_owners, policy_root=None
):
    """Requalify every retained assignment and admission under current owners.

    Material is the exact hash-checked archive material from the existing restore
    owner. All 240 slots remain in the receipt, including seven unadmitted slots;
    all 233 admitted sources must be authorized before returning a catalog.
    """
    from dataclasses import asdict

    from benchmarks.agent_tasks.screen48.recall.owners.cohort_authority import (
        verify_archive,
        verify_live_ledger,
    )
    from benchmarks.agent_tasks.screen48.recall.whole_items import OriginalCatalog

    from sibyl.api.routes.memory_evals import _trusted_issuer
    from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
    from sibyl_core.services.content_client import normalize_records, surreal_content_client
    from sibyl_core.services.eval_admission import get_registered_eval_assignment
    from sibyl_core.services.observed_sources import load_authorized_source_snapshot
    from sibyl_core.services.source_observations import SourceUnavailableError
    from sibyl_core.services.source_state_store import RawSourceSnapshot

    before = verify_owners()
    issuer, verified, statuses = verify_archive(material, source)
    org, principal = source["organization_id"], source["principal_id"]
    authority = await resolve_authority(org, principal)
    # Reuse the restore qualifier's exact scoped ledger read and validator.
    async with surreal_content_client() as client:
        rows = normalize_records(
            await client.execute_query(
                "SELECT attempt_id, capture_id, receipt_sha256, episode_sha256, receipt_base64 "
                "FROM eval_attempts "
                "WHERE organization_id = $org AND experiment_id = $experiment;",
                org=org,
                experiment=issuer["experiment_id"],
            )
        )
    verify_live_ledger(rows, verified)
    observations, slots = {}, []
    for item in verified:
        expected = item["assignment"]
        current = await get_registered_eval_assignment(
            organization_id=org,
            experiment_id=issuer["experiment_id"],
            attempt_id=expected.attempt_id,
        )
        if current != expected:
            raise MissingPack("current_registered_assignment_changed")
        trusted = _trusted_issuer(issuer["issuer_id"], current)
        if trusted.public_key_base64 != issuer["public_key_base64"]:
            raise MissingPack("current_registered_issuer_key_changed")
        slot = {
            "attempt_id": expected.attempt_id,
            "task_id": expected.task_id,
            "status": item["status"],
            "capture_id": item["ledger"].get("capture_id"),
        }
        if item["capture"] is not None:
            sid = item["capture"]["uuid"]
            try:
                snapshot = await load_authorized_source_snapshot(
                    SourceIdentity(org, SourceKind.RAW_CAPTURE, sid),
                    authority,
                    organization_id=org,
                )
                if (
                    not isinstance(snapshot, RawSourceSnapshot)
                    or snapshot.memory.principal_id != principal
                    or snapshot.memory.memory_scope.value != "private"
                    or snapshot.memory.metadata.get("eval_admission")
                    != item["capture"]["admission"]
                    or snapshot.memory.revision != item["capture"]["revision"]
                    or sha(snapshot.memory.raw_content.encode()) != item["ledger"]["episode_sha256"]
                ):
                    raise MissingPack("current_original_admission_changed")  # noqa: TRY301
                observations[sid] = snapshot.observation
                slot.update(eligible=True, observation=asdict(snapshot.observation))
            except (SourceUnavailableError, MissingPack) as exc:
                slot.update(eligible=False, reason=str(exc))
        slots.append(slot)
    final_authority = await resolve_authority(org, principal)
    if final_authority != authority or verify_owners() != before:
        raise MissingPack("current_original_qualification_changed")
    failed = [slot for slot in slots if slot.get("eligible") is False]
    if failed:
        raise OriginalQualificationFailure(
            {
                "status": "current_sources_unavailable",
                "slots": slots,
                "slot_count": len(slots),
                "admissions": sum(s["capture_id"] is not None for s in slots),
                "eligible_admissions": len(observations),
                "statuses": statuses,
                "source_owners": before,
                "provider_calls": 0,
                "learning_claim": False,
            }
        )
    catalog = OriginalCatalog(
        observations,
        authority,
        **({"policy_root": Path(policy_root)} if policy_root is not None else {}),
    )
    await catalog.check(final_authority)
    return catalog, {
        "schema": "sibyl-current-originals-qualification-v1",
        "status": "qualified",
        "slots": slots,
        "slot_count": len(slots),
        "admissions": len(observations),
        "statuses": statuses,
        "source_owners": before,
        "authority": authority.ceiling_metadata(),
        "catalog_sha256": catalog.catalog_sha256,
        "provider_calls": 0,
        "learning_claim": False,
    }
