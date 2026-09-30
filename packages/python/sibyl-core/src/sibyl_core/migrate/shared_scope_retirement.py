"""Retire legacy shared captures without guessing their audience."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from uuid import UUID

from sibyl_core.errors import RevisionConflictError
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services import content_client, content_models
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import save_raw_memory

RETIREMENT_METADATA_KEY = "shared_scope_retirement"
_PAGE_SIZE = 250


@dataclass(frozen=True, slots=True)
class SharedScopeAuthority:
    """Persisted same-organization team and principal grants, resolved together."""

    organization_id: str
    team_ids: frozenset[str]
    organization_members: frozenset[str]
    organization_admins: frozenset[str]
    team_memberships: frozenset[tuple[str, str]]

    def rejection(self, memory: RawMemory) -> str | None:
        if memory.organization_id != self.organization_id:
            raise ValueError("migration authority organization differs")
        try:
            canonical_key = str(UUID(memory.scope_key or ""))
        except ValueError:
            return "noncanonical_team_key"
        if canonical_key != memory.scope_key or canonical_key not in self.team_ids:
            return "team_not_in_organization"
        if memory.principal_id not in self.organization_members:
            return "principal_not_in_organization"
        if memory.principal_id in self.organization_admins:
            return None
        if (canonical_key, memory.principal_id) not in self.team_memberships:
            return "principal_not_in_team"
        return None


@dataclass(frozen=True, slots=True)
class SharedScopeRetirementEntry:
    capture_id: str
    prior_revision: int
    scope_key: str | None
    disposition: str
    reason: str
    applied_revision: int | None = None


@dataclass(slots=True)
class SharedScopeRetirementReceipt:
    organization_id: str
    dry_run: bool
    entries: list[SharedScopeRetirementEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.errors and all(entry.disposition != "conflict" for entry in self.entries)


AuthorityProvider = Callable[[], Awaitable[SharedScopeAuthority]]


async def retire_shared_captures(
    *,
    organization_id: str,
    authority_provider: AuthorityProvider,
    dry_run: bool = True,
) -> SharedScopeRetirementReceipt:
    """Map verified teams; tombstone exceptions while retaining source identity.

    The operator resolves persisted authority before each page. Query failures
    stop the run instead of being mistaken for absent memberships. Every save
    carries the observed revision fence; conflict rows remain untouched and
    appear in the receipt. Reruns select only remaining live shared captures.
    """
    if not organization_id:
        raise ValueError("organization_id is required")
    receipt = SharedScopeRetirementReceipt(organization_id=organization_id, dry_run=dry_run)
    after = ""
    async with content_client.surreal_content_client() as client:
        upper_rows = await content_client.select_many(
            client,
            "SELECT uuid FROM raw_captures WHERE organization_id = $organization_id "
            "AND memory_scope = 'shared' AND deleted_at = NONE ORDER BY uuid DESC LIMIT 1;",
            organization_id=organization_id,
        )
        if not upper_rows:
            return receipt
        upper = str(upper_rows[0]["uuid"])
        while True:
            try:
                authority = await authority_provider()
                if authority.organization_id != organization_id:
                    raise ValueError("migration authority organization differs")
                rows = await content_client.select_many(
                    client,
                    "SELECT * FROM raw_captures WHERE organization_id = $organization_id "
                    "AND memory_scope = 'shared' AND deleted_at = NONE "
                    "AND uuid > $after AND uuid <= $upper ORDER BY uuid LIMIT $limit;",
                    organization_id=organization_id,
                    after=after,
                    upper=upper,
                    limit=_PAGE_SIZE,
                )
                for row in rows:
                    memory = content_models.raw_memory_from_record(row)
                    reason = authority.rejection(memory)
                    disposition = "tombstone" if reason else "team"
                    entry = SharedScopeRetirementEntry(
                        capture_id=memory.id,
                        prior_revision=memory.revision,
                        scope_key=memory.scope_key,
                        disposition=disposition,
                        reason=reason or "canonical_team_and_principal_verified",
                    )
                    if dry_run:
                        receipt.entries.append(entry)
                        continue
                    metadata = {
                        **memory.metadata,
                        RETIREMENT_METADATA_KEY: {
                            "prior_scope": "shared",
                            "prior_scope_key": memory.scope_key,
                            "prior_revision": memory.revision,
                            "disposition": disposition,
                            "reason": entry.reason,
                            "at": content_models.utcnow().isoformat(),
                        },
                    }
                    retired = replace(
                        memory,
                        memory_scope=MemoryScope.SHARED if reason else MemoryScope.TEAM,
                        deleted_at=content_models.utcnow() if reason else memory.deleted_at,
                        metadata=metadata,
                    )
                    try:
                        saved = await save_raw_memory(
                            retired,
                            expected_revision=memory.revision,
                            embedding_provider=None,
                        )
                    except RevisionConflictError:
                        receipt.entries.append(
                            replace(entry, disposition="conflict", reason="revision_changed")
                        )
                        continue
                    receipt.entries.append(replace(entry, applied_revision=saved.revision))
                if not rows or len(rows) < _PAGE_SIZE:
                    return receipt
                next_after = str(rows[-1]["uuid"])
                if next_after <= after:
                    raise RuntimeError("shared capture migration cursor did not advance")
                after = next_after
            except Exception as exc:
                receipt.errors.append(f"{type(exc).__name__}: {exc}")
                return receipt
