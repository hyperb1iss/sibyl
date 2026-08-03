#!/usr/bin/env python3
"""Run the focused release gate for team-scope memory isolation.

The receipt this writes used to be hand-maintained, which made its
``leak_count: 0`` an assertion typed by a human rather than an observation. Every
number here is now produced by writing scoped memories through the real capture
path and reading them back through the real read surfaces as two principals whose
memberships were resolved from real ``teams``/``team_members`` rows.

Two failure modes shaped the design. The original scope leak shipped because the
only test of the filter hand-injected the very field whose absence was the bug,
so no fixture here supplies scope metadata directly: the write path stamps it.
The 1.1.3 patch then regressed in the opposite direction by hiding rows a member
was entitled to, so every probe declares an expected direction and the receipt
counts allow failures beside leaks.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from sibyl.persistence.surreal.auth_runtime import projects as projects_runtime
from sibyl_core.auth.context import AuthContext
from sibyl_core.auth.memory_policy import (
    MEMORY_OWNER_METADATA_KEYS,
    authorize_memory_read,
    memory_metadata_read_allowed,
    memory_scope_policy_key,
    private_scope_granted_for,
)
from sibyl_core.auth.models import AuthOrganization, AuthUser, OrganizationRole
from sibyl_core.backends.surreal.auth_client import SurrealAuthClient
from sibyl_core.backends.surreal.auth_schema import bootstrap_auth_schema
from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.config import settings
from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest, MemoryCaptureService
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.retrieval.candidates import RetrievalCandidate
from sibyl_core.retrieval.search import _candidate_scope_allowed, build_context_retrieval_plan
from sibyl_core.services import surreal_content as content_service
from sibyl_core.tools.context import ContextFacet

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_SCHEMA_VERSION = "sibyl-team-scope-trust-receipt-v1"
DEFAULT_RECEIPT_PATH = (
    REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "team-scope-trust-receipt.json"
)
FIXTURE_NAME = "team-scope-isolation-v1"
RELEASE_SCOPE = "v1.2 Track C team-scope trust receipt"

Runner = Callable[[tuple[str, ...]], int]
Echo = Callable[[str], None]

TEAM_SCOPE_BUDGETS: dict[str, float] = {
    "leak_count": 0,
    "allow_failure_count": 0,
    "promotion_attribution_coverage": 1,
    "promotion_preview_coverage": 1,
    # A gate that probes nothing reports zero leaks. These floors are what stop a
    # vacuous receipt from reading as a clean one, so they are budgets rather
    # than commentary: dropping a probe class fails the gate.
    "deny_probe_count": 31,
    "allow_probe_count": 13,
    "surface_count": 6,
    # Five forged owner fields, and the two backfill-provenance keys matter most:
    # nothing downstream rewrites them, so they are the pair that proves the
    # write path's drop filter is load bearing rather than belt and braces.
    "owner_forgery_offered_count": 5,
    "owner_forgery_surviving_count": 0,
    # A read surface that stops answering agrees with every denial, so its
    # silence has to be counted rather than inferred from a direction.
    "surface_disagreement_count": 0,
    # Probe expectations and probe observations both read the resolver, so an
    # over-broad resolver could move them together and stay green. The rows this
    # gate provisions are the independent ground truth that check sits against.
    "membership_resolution_mismatch_count": 0,
}
LOWER_IS_BETTER_METRICS = frozenset(
    (
        "leak_count",
        "allow_failure_count",
        "graph_team_membership_forwarded",
        "owner_forgery_surviving_count",
        "surface_disagreement_count",
        "membership_resolution_mismatch_count",
    )
)


EXPECTED_PROBE_SURFACES: frozenset[str] = frozenset(
    (
        "raw_targeted_read",
        "raw_own_scope_read",
        "scope_authorization",
        "graph_metadata_read",
        "graph_metadata_read_narrowed",
        "retrieval_candidate_filter",
    )
)

PROMOTION_ATTRIBUTION_SURFACES: tuple[str, ...] = (
    "promotion attribution",
    "audit receipt",
    "team target promotion",
)
PROMOTION_PREVIEW_SURFACES: tuple[str, ...] = (
    "promotion preview",
    "team target redaction",
)

# Deterministic identity: the receipt is a function of the code under test, not
# of the clock or a random generator, so a rerun that changes bytes means the
# behaviour changed.
_ID_NAMESPACE = uuid5(NAMESPACE_URL, "https://sibyl.ist/gates/team-scope")


def _fixture_id(name: str) -> str:
    return str(uuid5(_ID_NAMESPACE, name))


ORGANIZATION_ID = _fixture_id("organization")
TEAM_ID = _fixture_id("team-alpha")
OTHER_TEAM_ID = _fixture_id("team-beta")
PROJECT_ID = _fixture_id("project-lumen")
OTHER_PROJECT_ID = _fixture_id("project-mercury")
DELEGATION_ID = _fixture_id("delegation-oncall")
PRINCIPAL_IDS: Mapping[str, str] = {
    "member": _fixture_id("principal-member"),
    "outsider": _fixture_id("principal-outsider"),
}

# Ground truth for the membership resolvers: exactly the rows _provision_principals
# writes. Comparing the resolvers against this is not the same as handing the
# probes their memberships, which still come from the resolvers; it is the one
# check that fails when a resolver stops agreeing with the store.
PROVISIONED_TEAMS: Mapping[str, frozenset[str]] = {
    "member": frozenset({TEAM_ID}),
    "outsider": frozenset({OTHER_TEAM_ID}),
}
PROVISIONED_PROJECTS: Mapping[str, frozenset[str]] = {
    "member": frozenset({PROJECT_ID}),
    "outsider": frozenset({OTHER_PROJECT_ID}),
}

ALLOW = "allow"
DENY = "deny"


@dataclass(frozen=True)
class GateCheck:
    name: str
    description: str
    surfaces: tuple[str, ...]
    command: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    check: GateCheck
    exit_code: int
    elapsed_seconds: float
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class Principal:
    """A reader whose memberships came out of the auth store, not a literal.

    ``teams`` and ``projects`` are resolved by the same functions the REST layer
    calls. Declaring them inline would reproduce the defect this gate exists to
    catch: a fixture that supplies the authorization input under test.
    """

    label: str
    user_id: str
    teams: frozenset[str]
    projects: frozenset[str]
    delegations: frozenset[str]

    @property
    def granted_memory_scope_keys(self) -> frozenset[str]:
        """The memory spaces an API key narrowed to this reader's projects holds.

        Derived from resolved project membership rather than declared, so the
        narrowing under test is the same key format the API mints.
        """
        return frozenset(
            memory_scope_policy_key(MemoryScope.PROJECT, project) for project in self.projects
        )


@dataclass(frozen=True)
class SeededMemory:
    label: str
    memory_scope: str
    scope_key: str | None
    owner_label: str
    title: str
    content: str
    raw_memory_id: str
    graph_metadata: Mapping[str, Any]
    requested_metadata: Mapping[str, Any]

    @property
    def offered_owner_forgeries(self) -> tuple[str, ...]:
        """Owner fields the payload tried to name for itself."""
        return tuple(sorted(set(self.requested_metadata) & MEMORY_OWNER_METADATA_KEYS))

    @property
    def surviving_owner_forgeries(self) -> tuple[str, ...]:
        """Forged owner fields the write path failed to overwrite.

        The read filter reads a row's audience out of these fields, so one
        surviving forgery lets a payload nominate its own readers and every
        downstream isolation probe becomes meaningless.
        """
        return tuple(
            field
            for field in self.offered_owner_forgeries
            if self.graph_metadata.get(field) == self.requested_metadata.get(field)
        )


@dataclass(frozen=True)
class SurfaceReading:
    """What one read surface answered, and whether it answered coherently."""

    observed: str
    detail: str
    disagreement: bool = False


@dataclass(frozen=True)
class ScopeProbe:
    """One authorization question with a declared direction.

    ``requested_scope_key`` is what the reader asked for, which is not always the
    row's own key: a reader querying the scope key they legitimately hold is how
    the row-selection clause gets tested, while a reader naming someone else's
    key is how the membership check gets tested.

    ``boundary`` names a structural limit of the surface when the expectation is
    a denial the scope model would otherwise allow, so a receipt reader can tell
    "isolation held" apart from "this surface cannot serve this scope at all".
    """

    surface: str
    reader_label: str
    memory_label: str
    expectation: str
    requested_scope_key: str | None = None
    boundary: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.surface,
            self.memory_label,
            self.reader_label,
            self.requested_scope_key or "",
        )


@dataclass(frozen=True)
class ProbeObservation:
    probe: ScopeProbe
    observed: str
    detail: str
    disagreement: bool = False

    @property
    def leaked(self) -> bool:
        return self.probe.expectation == DENY and self.observed == ALLOW

    @property
    def allow_failed(self) -> bool:
        return self.probe.expectation == ALLOW and self.observed == DENY

    @property
    def passed(self) -> bool:
        return self.observed == self.probe.expectation and not self.disagreement

    def as_receipt_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "surface": self.probe.surface,
            "memory": self.probe.memory_label,
            "reader": self.probe.reader_label,
            "requested_scope_key": self.probe.requested_scope_key,
            "expected": self.probe.expectation,
            "observed": self.observed,
            "status": "PASS" if self.passed else "FAIL",
            "detail": self.detail,
            "surface_disagreement": self.disagreement,
        }
        if self.probe.boundary is not None:
            entry["boundary"] = self.probe.boundary
        return entry


GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="team-target-preview-redaction",
        description="share preview redacts a team target's private and delegated sources",
        surfaces=("team target redaction", "private source isolation"),
        command=(
            "moon",
            "run",
            "core:test",
            "--",
            "tests/test_memory.py",
            "-k",
            "share_memory or share_preview",
        ),
    ),
    GateCheck(
        name="team-scope-rest-policy",
        description="REST recall serves a verified team and denies an unverified one",
        surfaces=("team scope REST recall", "unverified team denial"),
        command=("moon", "run", "api:memory-trust-rest-test"),
    ),
    GateCheck(
        name="share-promotion-apply",
        description="promotion to a team target records attribution and an audit receipt",
        surfaces=(
            "promotion attribution",
            "promotion preview",
            "audit receipt",
            "team target promotion",
        ),
        command=("moon", "run", "api:memory-trust-rest-test"),
    ),
    GateCheck(
        name="team-control-plane-auth",
        description="team membership lookup resolves the canonical team memory space",
        surfaces=("team membership lookup", "canonical team memory space"),
        command=("moon", "run", "api:trust-control-auth-test"),
    ),
    GateCheck(
        name="ai-memory-contracts",
        description="committed AI-memory manifest carries the team-scope receipt contract",
        surfaces=("manifest", "release contract"),
        command=("moon", "run", "bench-gate"),
    ),
)

OBSERVED_CHECK_NAME = "team-scope-read-isolation"
OBSERVED_CHECK_SURFACES: tuple[str, ...] = (
    "private source isolation",
    "delegated source isolation",
    "project source isolation",
    "unverified team denial",
    "team membership lookup",
    "canonical team memory space",
    "team scope service recall",
    "graph metadata read filter",
    "retrieval candidate filter",
)
CONTRACT_CHECK_NAMES = frozenset(("ai-memory-contracts",))

REQUIRED_SURFACES: tuple[str, ...] = (
    "team target redaction",
    "private source isolation",
    "delegated source isolation",
    "project source isolation",
    "team scope REST recall",
    "unverified team denial",
    "promotion attribution",
    "promotion preview",
    "audit receipt",
    "team target promotion",
    "team membership lookup",
    "canonical team memory space",
    "manifest",
    "release contract",
)


def covered_surfaces(checks: Iterable[GateCheck] = GATE_CHECKS) -> set[str]:
    covered = {surface for check in checks for surface in check.surfaces}
    covered.update(OBSERVED_CHECK_SURFACES)
    return covered


def missing_required_surfaces(checks: Sequence[GateCheck] = GATE_CHECKS) -> list[str]:
    covered = covered_surfaces(checks)
    return [surface for surface in REQUIRED_SURFACES if surface not in covered]


def graph_team_membership_forwarded() -> bool:
    """Whether the shared graph read helper can be told about team membership.

    ``memory_metadata_read_allowed`` accepts no team or delegation membership
    today, so a team-scoped graph row is denied to members and non-members
    alike, and the scope backfill deliberately refuses to stamp one. That is a
    real boundary rather than a passing isolation claim, so the receipt records
    it and this probe fails the gate the moment the signature grows the
    parameter: the boundary then needs retiring, not carrying forward.
    """
    parameters = inspect.signature(memory_metadata_read_allowed).parameters
    return "accessible_teams" in parameters or "accessible_delegations" in parameters


def _auth_context(user_id: str) -> AuthContext:
    return AuthContext(
        user=AuthUser(id=user_id, email=None, name="team-scope-gate"),
        organization=AuthOrganization(
            id=ORGANIZATION_ID,
            name="team-scope-gate",
            slug="team-scope-gate",
        ),
        # A plain member, so team access has to come from a membership row rather
        # than from an org-admin shortcut that returns every team in the org.
        org_role=OrganizationRole.MEMBER,
    )


@asynccontextmanager
async def _embedded_surreal_url():
    """Pin scope queries to the embedded SQL dialect for this run.

    The content service picks embedded or server function names off the resolved
    URL, and the fixture store is always embedded. A developer whose environment
    points at the shared dev stack would otherwise render server-side names
    against an in-process engine.
    """
    previous = settings.surreal_url
    settings.surreal_url = "memory://"
    try:
        yield
    finally:
        settings.surreal_url = previous


async def _provision_principals(client: SurrealAuthClient) -> dict[str, Principal]:
    await bootstrap_auth_schema(client)
    await client.execute_query(
        """
        CREATE teams CONTENT {
            uuid: $team, organization_id: $organization, name: 'Alpha', slug: 'alpha'
        };
        CREATE teams CONTENT {
            uuid: $other_team, organization_id: $organization, name: 'Beta', slug: 'beta'
        };
        CREATE team_members CONTENT {
            uuid: $membership, team_id: $team, user_id: $member
        };
        CREATE team_members CONTENT {
            uuid: $other_membership, team_id: $other_team, user_id: $outsider
        };
        CREATE projects CONTENT {
            uuid: $project, organization_id: $organization, name: 'Lumen', slug: 'lumen',
            graph_project_id: $project, visibility: 'private'
        };
        CREATE projects CONTENT {
            uuid: $other_project, organization_id: $organization, name: 'Mercury',
            slug: 'mercury', graph_project_id: $other_project, visibility: 'private'
        };
        CREATE project_members CONTENT {
            uuid: $project_membership, organization_id: $organization,
            project_id: $project, user_id: $member
        };
        CREATE project_members CONTENT {
            uuid: $other_project_membership, organization_id: $organization,
            project_id: $other_project, user_id: $outsider
        };
        """,
        organization=ORGANIZATION_ID,
        team=TEAM_ID,
        other_team=OTHER_TEAM_ID,
        project=PROJECT_ID,
        other_project=OTHER_PROJECT_ID,
        member=PRINCIPAL_IDS["member"],
        outsider=PRINCIPAL_IDS["outsider"],
        membership=_fixture_id("membership-member-alpha"),
        other_membership=_fixture_id("membership-outsider-beta"),
        project_membership=_fixture_id("membership-member-lumen"),
        other_project_membership=_fixture_id("membership-outsider-mercury"),
    )

    @asynccontextmanager
    async def fixture_auth_scope():
        yield client

    previous_scope = projects_runtime._auth_client_scope
    projects_runtime._auth_client_scope = cast(Any, fixture_auth_scope)
    try:
        principals: dict[str, Principal] = {}
        for label, user_id in PRINCIPAL_IDS.items():
            context = _auth_context(user_id)
            teams = await projects_runtime.list_accessible_team_scope_keys(context)
            projects = await projects_runtime.list_accessible_project_graph_ids(context)
            principals[label] = Principal(
                label=label,
                user_id=user_id,
                teams=frozenset(str(value) for value in teams),
                projects=frozenset(str(value) for value in projects),
                # No delegation is granted to anyone, so the delegated row's
                # isolation is observed rather than assumed away.
                delegations=frozenset(),
            )
    finally:
        projects_runtime._auth_client_scope = previous_scope
    return principals


def _seed_requests() -> tuple[MemoryCaptureRequest, ...]:
    member = PRINCIPAL_IDS["member"]
    return (
        MemoryCaptureRequest(
            title="private rotation notes",
            content="rotate the staging signing key before the lumen cutover",
            entity_type="episode",
            memory_scope=MemoryScope.PRIVATE.value,
            scope_key=None,
            principal_id=member,
            source_id="team-scope-gate:private",
            # A payload that names its own owner is the exact forgery the write
            # path is supposed to drop. The scope and owner keys are also
            # rewritten from authorized values further down that function, so
            # they alone cannot prove the drop filter runs; the two backfill
            # provenance keys are never rewritten, which makes them the pair
            # that fails loudly if the filter is removed. Forging them nominates
            # the row for a rollback that strips its scope to the fail-open.
            metadata={
                "principal_id": PRINCIPAL_IDS["outsider"],
                "memory_scope": "organization",
                "scope_backfill_source": "raw_capture",
                "scope_backfill_prior": {"touched": ["memory_scope"], "prior": {}},
            },
        ),
        MemoryCaptureRequest(
            title="team deploy runbook",
            content="alpha owns the lumen deploy runbook and the rollback switch",
            entity_type="episode",
            memory_scope=MemoryScope.TEAM.value,
            scope_key=TEAM_ID,
            principal_id=member,
            source_id="team-scope-gate:team",
            metadata={"scope_key": OTHER_TEAM_ID},  # claims another team's audience
        ),
        MemoryCaptureRequest(
            title="project retrieval decision",
            content="lumen retrieval uses surreal native rrf for graph fusion",
            entity_type="episode",
            memory_scope=MemoryScope.PROJECT.value,
            scope_key=PROJECT_ID,
            principal_id=member,
            source_id="team-scope-gate:project",
            project_id=PROJECT_ID,
            metadata={"project_id": PROJECT_ID},
        ),
        MemoryCaptureRequest(
            title="delegated oncall handoff",
            content="oncall delegation covers the lumen pager rotation this week",
            entity_type="episode",
            memory_scope=MemoryScope.DELEGATED.value,
            scope_key=DELEGATION_ID,
            principal_id=member,
            source_id="team-scope-gate:delegated",
            metadata={},
        ),
    )


_SEED_LABELS: Mapping[str, str] = {
    "team-scope-gate:private": "private-member",
    "team-scope-gate:team": "team-alpha",
    "team-scope-gate:project": "project-lumen",
    "team-scope-gate:delegated": "delegated-oncall",
}


async def _seed_memories(requests: Sequence[MemoryCaptureRequest]) -> list[SeededMemory]:
    """Write each scope through the real capture service and keep what it stamped."""
    stamped: dict[str, Mapping[str, Any]] = {}

    async def write_raw(request: MemoryCaptureRequest) -> Mapping[str, Any]:
        memory = await content_service.remember_raw_memory(
            organization_id=ORGANIZATION_ID,
            principal_id=request.principal_id or "",
            source_id=request.source_id or request.title,
            raw_content=request.content,
            title=request.title,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            metadata=dict(request.metadata),
            capture_surface=request.capture_surface,
            embedding_provider=None,
        )
        return {"id": memory.id, "source_id": memory.source_id}

    async def write_graph(
        request: MemoryCaptureRequest,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        stamped[str(request.source_id)] = dict(metadata)
        return {"id": _fixture_id(f"graph:{request.source_id}")}

    service = MemoryCaptureService(
        remember_raw_memory=write_raw,
        create_graph_entity=write_graph,
    )
    seeded: list[SeededMemory] = []
    for request in requests:
        result = await service.capture(request)
        source_id = str(request.source_id)
        seeded.append(
            SeededMemory(
                label=_SEED_LABELS[source_id],
                memory_scope=request.memory_scope,
                scope_key=request.scope_key,
                owner_label="member",
                title=request.title,
                content=request.content,
                raw_memory_id=str(result.raw_memory_id),
                graph_metadata=stamped[source_id],
                requested_metadata=dict(request.metadata),
            )
        )
    return sorted(seeded, key=lambda memory: memory.label)


def _reader_keys(principal: Principal, memory_scope: str) -> frozenset[str]:
    """The scope keys this reader's resolved memberships let them address."""
    if memory_scope == MemoryScope.TEAM.value:
        return principal.teams
    if memory_scope == MemoryScope.PROJECT.value:
        return principal.projects
    if memory_scope == MemoryScope.DELEGATED.value:
        return principal.delegations
    return frozenset()


def _reader_grants(principal: Principal, memory: SeededMemory) -> bool:
    """Whether this reader's resolved memberships satisfy the row's audience."""
    if memory.memory_scope == MemoryScope.PRIVATE.value:
        return principal.label == memory.owner_label
    return memory.scope_key is not None and memory.scope_key in _reader_keys(
        principal,
        memory.memory_scope,
    )


_GRAPH_SERVABLE_SCOPES = frozenset(
    (MemoryScope.PRIVATE.value, MemoryScope.PROJECT.value),
)
GRAPH_MEMBERSHIP_BOUNDARY = "graph read helper forwards no team or delegation membership"
DELEGATION_RESOLVER_BOUNDARY = "no resolver grants a delegation, so no principal can hold one"


def _expected_probes(
    principals: Mapping[str, Principal],
    memories: Sequence[SeededMemory],
) -> list[ScopeProbe]:
    """Enumerate one probe per read decision the surfaces can actually make.

    Every expectation is derived from resolved memberships plus what the surface
    is structurally able to serve, never from a per-case judgement, so a probe
    cannot be quietly tuned to match whatever the code happens to do.
    """
    graph_boundary = None if graph_team_membership_forwarded() else GRAPH_MEMBERSHIP_BOUNDARY
    probes: list[ScopeProbe] = []
    for memory in memories:
        for label in sorted(principals):
            principal = principals[label]
            entitled = _reader_grants(principal, memory)
            delegated_unreachable = (
                memory.memory_scope == MemoryScope.DELEGATED.value and not principal.delegations
            )

            # Reaching for the row's own audience: the membership check answers.
            probes.append(
                ScopeProbe(
                    surface="raw_targeted_read",
                    reader_label=label,
                    memory_label=memory.label,
                    expectation=ALLOW if entitled else DENY,
                    requested_scope_key=memory.scope_key,
                    boundary=DELEGATION_RESOLVER_BOUNDARY if delegated_unreachable else None,
                )
            )
            if memory.memory_scope != MemoryScope.PRIVATE.value:
                probes.append(
                    ScopeProbe(
                        surface="scope_authorization",
                        reader_label=label,
                        memory_label=memory.label,
                        expectation=ALLOW if entitled else DENY,
                        requested_scope_key=memory.scope_key,
                        boundary=DELEGATION_RESOLVER_BOUNDARY if delegated_unreachable else None,
                    )
                )

            # Reading the reader's own audience: the row-selection clause answers,
            # which is the only way a dropped scope_key filter shows up.
            own_keys: tuple[str | None, ...]
            if memory.memory_scope == MemoryScope.PRIVATE.value:
                own_keys = (None,)
            else:
                own_keys = tuple(sorted(_reader_keys(principal, memory.memory_scope)))
            for own_key in own_keys:
                probes.append(
                    ScopeProbe(
                        surface="raw_own_scope_read",
                        reader_label=label,
                        memory_label=memory.label,
                        expectation=ALLOW if (entitled and own_key == memory.scope_key) else DENY,
                        requested_scope_key=own_key,
                    )
                )

            graph_servable = memory.memory_scope in _GRAPH_SERVABLE_SCOPES
            graph_expectation = ALLOW if (entitled and graph_servable) else DENY
            boundary = graph_boundary if entitled and not graph_servable else None
            for surface in ("graph_metadata_read", "retrieval_candidate_filter"):
                probes.append(
                    ScopeProbe(
                        surface=surface,
                        reader_label=label,
                        memory_label=memory.label,
                        expectation=graph_expectation,
                        requested_scope_key=memory.scope_key,
                        boundary=boundary,
                    )
                )

            # Same row, same reader, but through a credential narrowed to the
            # reader's project memory spaces. A row whose canonical space the key
            # does not hold must be refused even when the reader owns it, so the
            # private row is denied to its own author here.
            probes.append(
                ScopeProbe(
                    surface="graph_metadata_read_narrowed",
                    reader_label=label,
                    memory_label=memory.label,
                    expectation=ALLOW
                    if (
                        graph_expectation == ALLOW
                        and _memory_space_key(memory) in principal.granted_memory_scope_keys
                    )
                    else DENY,
                    requested_scope_key=memory.scope_key,
                    boundary=boundary,
                )
            )
    return sorted(probes, key=lambda probe: probe.key)


def membership_resolution_mismatches(
    principals: Mapping[str, Principal],
) -> list[str]:
    """Where a resolver disagrees with the membership rows this gate wrote."""
    mismatches: list[str] = []
    for label in sorted(principals):
        principal = principals[label]
        for kind, resolved, provisioned in (
            ("teams", principal.teams, PROVISIONED_TEAMS.get(label, frozenset())),
            ("projects", principal.projects, PROVISIONED_PROJECTS.get(label, frozenset())),
        ):
            if resolved != provisioned:
                mismatches.append(
                    f"{label} resolved {kind} {sorted(resolved)} "
                    f"but was provisioned {sorted(provisioned)}"
                )
    return mismatches


def _memory_space_key(memory: SeededMemory) -> str:
    return memory_scope_policy_key(memory.memory_scope, memory.scope_key)


def _authorize_raw_read(
    principal: Principal,
    memory: SeededMemory,
    scope_key: str | None,
) -> Any:
    return authorize_memory_read(
        principal_id=principal.user_id,
        memory_scope=memory.memory_scope,
        scope_key=scope_key,
        accessible_projects=principal.projects,
        accessible_teams=principal.teams,
        accessible_delegations=principal.delegations,
    )


async def _observe_raw_read(
    principal: Principal,
    memory: SeededMemory,
    scope_key: str | None,
) -> SurfaceReading:
    """Run a raw read the way a route runs one: authorize, then query.

    The content query builder trusts an already-authorized scope key, so probing
    it alone would report a membership check that lives a layer up. Composing the
    two is the only shape that measures what a caller actually gets.
    """
    decision = _authorize_raw_read(principal, memory, scope_key)
    if not decision.allowed:
        return SurfaceReading(DENY, f"denied_at=authorization reason={decision.reason}")
    listed = await content_service.list_raw_memories_for_scope(
        organization_id=ORGANIZATION_ID,
        principal_id=principal.user_id,
        memory_scope=memory.memory_scope,
        scope_key=scope_key,
        limit=25,
    )
    recalled = await content_service.recall_raw_memory(
        organization_id=ORGANIZATION_ID,
        principal_id=principal.user_id,
        query=memory.content,
        memory_scope=memory.memory_scope,
        scope_key=scope_key,
        limit=25,
    )
    in_listing = any(row.id == memory.raw_memory_id for row in listed)
    in_recall = any(row.id == memory.raw_memory_id for row in recalled)
    detail = (
        f"reason={decision.reason} listed={in_listing} recalled={in_recall} "
        f"rows_listed={len(listed)} rows_recalled={len(recalled)}"
    )
    # Two reads of one row that disagree mean one of them stopped applying the
    # scope clause, and the direction does not make it benign: a recall surface
    # that returns nothing at all agrees with every denial and would otherwise
    # pass its allow probes on the listing's answer alone. So the disagreement
    # is carried as its own counted fact rather than folded into allow or deny,
    # where an expectation could absorb it.
    return SurfaceReading(
        ALLOW if in_listing else DENY,
        detail,
        disagreement=in_listing != in_recall,
    )


def _observe_scope_authorization(
    principal: Principal,
    memory: SeededMemory,
    scope_key: str | None,
) -> SurfaceReading:
    decision = _authorize_raw_read(principal, memory, scope_key)
    return SurfaceReading(ALLOW if decision.allowed else DENY, f"reason={decision.reason}")


def _observe_graph_metadata_read(
    principal: Principal,
    memory: SeededMemory,
) -> SurfaceReading:
    allowed = memory_metadata_read_allowed(
        memory.graph_metadata,
        principal_id=principal.user_id,
        private_scope_granted=True,
        accessible_projects=principal.projects,
        project_id=None,
    )
    stamped_scope = memory.graph_metadata.get("memory_scope")
    stamped_owner = memory.graph_metadata.get("principal_id")
    return SurfaceReading(
        ALLOW if allowed else DENY,
        f"stamped_scope={stamped_scope} owner_is_reader={stamped_owner == principal.user_id}",
    )


def _observe_graph_metadata_read_narrowed(
    principal: Principal,
    memory: SeededMemory,
) -> SurfaceReading:
    """Read as an API key narrowed to this reader's project memory space.

    A credential narrowed to a project must not reach the principal's own private
    rows just because the principal owns them, so this is the surface where the
    canonical memory-space key is the authorization input rather than a label.
    """
    granted = principal.granted_memory_scope_keys
    allowed = memory_metadata_read_allowed(
        memory.graph_metadata,
        principal_id=principal.user_id,
        private_scope_granted=private_scope_granted_for(
            granted,
            principal_id=principal.user_id,
        ),
        accessible_projects=principal.projects,
        project_id=None,
        allowed_memory_scope_keys=granted,
    )
    return SurfaceReading(
        ALLOW if allowed else DENY,
        f"granted_spaces={len(granted)} row_space={_memory_space_key(memory)}",
    )


def _observe_retrieval_filter(principal: Principal, memory: SeededMemory) -> SurfaceReading:
    plan = build_context_retrieval_plan(
        query=memory.content,
        organization_id=ORGANIZATION_ID,
        facets=(ContextFacet.RECENT_MEMORY,),
        facet_types={ContextFacet.RECENT_MEMORY: ("episode",)},
        principal_id=principal.user_id,
        project=None,
        accessible_projects=principal.projects,
    )
    candidate = RetrievalCandidate(
        id=_fixture_id(f"candidate:{memory.label}"),
        type="episode",
        name=memory.title,
        content=memory.content,
        score=1.0,
        source="team-scope-gate",
        metadata=dict(memory.graph_metadata),
        project_id=memory.scope_key if memory.memory_scope == MemoryScope.PROJECT.value else None,
    )
    allowed = _candidate_scope_allowed(candidate, plan)
    return SurfaceReading(
        ALLOW if allowed else DENY,
        f"plan_scopes={','.join(sorted(scope.memory_scope.value for scope in plan.scopes))}",
    )


async def _observe_probe(
    probe: ScopeProbe,
    *,
    principals: Mapping[str, Principal],
    memories: Mapping[str, SeededMemory],
) -> ProbeObservation:
    principal = principals[probe.reader_label]
    memory = memories[probe.memory_label]
    if probe.surface in {"raw_targeted_read", "raw_own_scope_read"}:
        reading = await _observe_raw_read(principal, memory, probe.requested_scope_key)
    elif probe.surface == "scope_authorization":
        reading = _observe_scope_authorization(principal, memory, probe.requested_scope_key)
    elif probe.surface == "graph_metadata_read":
        reading = _observe_graph_metadata_read(principal, memory)
    elif probe.surface == "graph_metadata_read_narrowed":
        reading = _observe_graph_metadata_read_narrowed(principal, memory)
    elif probe.surface == "retrieval_candidate_filter":
        reading = _observe_retrieval_filter(principal, memory)
    else:  # pragma: no cover - guarded by _expected_probes
        msg = f"unknown probe surface {probe.surface!r}"
        raise AssertionError(msg)
    return ProbeObservation(
        probe=probe,
        observed=reading.observed,
        detail=reading.detail,
        disagreement=reading.disagreement,
    )


async def collect_team_scope_observations() -> dict[str, Any]:
    auth_client = SurrealAuthClient(url="memory://")
    content_client = SurrealContentClient(url="memory://")

    @asynccontextmanager
    async def fixture_content_scope():
        yield content_client

    previous_content_scope = content_service.surreal_content_client
    try:
        async with _embedded_surreal_url():
            principals = await _provision_principals(auth_client)
            await bootstrap_content_schema(content_client)
            content_service.surreal_content_client = cast(Any, fixture_content_scope)
            memories = await _seed_memories(_seed_requests())
            memories_by_label = {memory.label: memory for memory in memories}
            probes = _expected_probes(principals, memories)
            observations = [
                await _observe_probe(
                    probe,
                    principals=principals,
                    memories=memories_by_label,
                )
                for probe in probes
            ]
    finally:
        content_service.surreal_content_client = previous_content_scope
        await content_client.close()
        await auth_client.close()

    return build_team_scope_receipt(
        principals=principals,
        memories=memories,
        observations=observations,
    )


def build_observed_team_scope_receipt() -> dict[str, Any]:
    return asyncio.run(collect_team_scope_observations())


def _principal_entry(principal: Principal) -> dict[str, Any]:
    return {
        "label": principal.label,
        "resolved_teams": sorted(principal.teams),
        "resolved_projects": sorted(principal.projects),
        "resolved_delegations": sorted(principal.delegations),
        "team_memory_space": sorted(
            memory_scope_policy_key(MemoryScope.TEAM, team) for team in principal.teams
        ),
    }


def _memory_entry(memory: SeededMemory) -> dict[str, Any]:
    metadata = dict(memory.graph_metadata)
    return {
        "label": memory.label,
        "requested_scope": memory.memory_scope,
        "requested_scope_key": memory.scope_key,
        "owner": memory.owner_label,
        "stamped_memory_scope": metadata.get("memory_scope"),
        "stamped_scope_key": metadata.get("scope_key"),
        "stamped_principal_id": metadata.get("principal_id"),
        "stamped_metadata_keys": sorted(metadata),
        "offered_owner_forgeries": list(memory.offered_owner_forgeries),
        "surviving_owner_forgeries": list(memory.surviving_owner_forgeries),
    }


def build_team_scope_receipt(
    *,
    principals: Mapping[str, Principal],
    memories: Sequence[SeededMemory],
    observations: Sequence[ProbeObservation],
) -> dict[str, Any]:
    mismatches = membership_resolution_mismatches(principals)
    ordered = sorted(observations, key=lambda observation: observation.probe.key)
    leaks = [observation for observation in ordered if observation.leaked]
    allow_failures = [observation for observation in ordered if observation.allow_failed]
    deny_probes = [observation for observation in ordered if observation.probe.expectation == DENY]
    allow_probes = [
        observation for observation in ordered if observation.probe.expectation == ALLOW
    ]
    surfaces = sorted({observation.probe.surface for observation in ordered})
    boundaries = sorted(
        {
            observation.probe.boundary
            for observation in ordered
            if observation.probe.boundary is not None
        }
    )
    disagreements = [observation for observation in ordered if observation.disagreement]
    metrics: dict[str, Any] = {
        "leak_count": len(leaks),
        "allow_failure_count": len(allow_failures),
        "surface_disagreement_count": len(disagreements),
        "probe_count": len(ordered),
        "deny_probe_count": len(deny_probes),
        "allow_probe_count": len(allow_probes),
        "surface_count": len(surfaces),
        "graph_team_membership_forwarded": int(graph_team_membership_forwarded()),
        "membership_resolution_mismatch_count": len(mismatches),
        "owner_forgery_offered_count": sum(
            len(memory.offered_owner_forgeries) for memory in memories
        ),
        "owner_forgery_surviving_count": sum(
            len(memory.surviving_owner_forgeries) for memory in memories
        ),
        # Promotion coverage is observed from the evidence checks, so a receipt
        # built without them reports zero rather than an aspirational one.
        "promotion_attribution_coverage": 0,
        "promotion_preview_coverage": 0,
    }
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "fixture": FIXTURE_NAME,
        "release_scope": RELEASE_SCOPE,
        "claim_boundary": (
            "Machine-generated from an ephemeral embedded auth and content store. "
            "Memberships are resolved by the real team and project lookups, scope "
            "metadata is stamped by the real capture path, and every probe reads "
            "through a real read surface in both the deny and the allow direction."
        ),
        "budgets": dict(TEAM_SCOPE_BUDGETS),
        "metrics": metrics,
        "boundaries": boundaries,
        "membership_mismatches": mismatches,
        "principals": [_principal_entry(principals[label]) for label in sorted(principals)],
        "memories": [_memory_entry(memory) for memory in memories],
        "probes": [observation.as_receipt_entry() for observation in ordered],
        "surfaces": surfaces,
    }


OBSERVED_RECEIPT_FIELDS: tuple[str, ...] = (
    "schema_version",
    "fixture",
    "boundaries",
    "membership_mismatches",
    "principals",
    "memories",
    "probes",
    "surfaces",
)
DERIVED_METRICS: frozenset[str] = frozenset(
    ("promotion_attribution_coverage", "promotion_preview_coverage"),
)


def observed_receipt_fields(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a receipt that a rerun has to reproduce exactly.

    Promotion coverage and the check list come from subprocess evidence, so they
    are excluded. Everything else is a direct observation of the code under test,
    which is what makes a committed receipt checkable against a fresh run instead
    of being taken on trust.
    """
    fields = {field: receipt.get(field) for field in OBSERVED_RECEIPT_FIELDS}
    metrics = receipt.get("metrics")
    fields["metrics"] = (
        {metric: value for metric, value in metrics.items() if metric not in DERIVED_METRICS}
        if isinstance(metrics, Mapping)
        else metrics
    )
    return fields


def with_promotion_coverage(
    receipt: Mapping[str, Any],
    results: Sequence[GateResult],
) -> dict[str, Any]:
    """Derive the promotion coverage metrics from checks that actually ran."""
    passed_surfaces = {
        surface for result in results if result.passed for surface in result.check.surfaces
    }
    attempted_surfaces = {surface for result in results for surface in result.check.surfaces}

    def coverage(required: tuple[str, ...]) -> float:
        expected = [surface for surface in required if surface in attempted_surfaces]
        if len(expected) != len(required):
            return 0.0
        covered = sum(1 for surface in required if surface in passed_surfaces)
        return covered / len(required)

    metrics = {
        **dict(receipt["metrics"]),
        "promotion_attribution_coverage": coverage(PROMOTION_ATTRIBUTION_SURFACES),
        "promotion_preview_coverage": coverage(PROMOTION_PREVIEW_SURFACES),
    }
    return {**dict(receipt), "metrics": metrics}


def with_check_results(
    receipt: Mapping[str, Any],
    results: Sequence[GateResult],
) -> dict[str, Any]:
    observed_status = "PASS"
    metrics = receipt.get("metrics", {})
    if (
        metrics.get("leak_count")
        or metrics.get("allow_failure_count")
        or metrics.get("surface_disagreement_count")
    ):
        observed_status = "FAIL"
    checks: list[dict[str, Any]] = [
        {
            "name": OBSERVED_CHECK_NAME,
            "status": observed_status,
            "command": format_command(("moon", "run", "team-scope-gate")),
            "surfaces": list(OBSERVED_CHECK_SURFACES),
        }
    ]
    checks.extend(
        {
            "name": result.check.name,
            "status": "PASS" if result.passed else "FAIL",
            "exit_code": result.exit_code,
            "command": format_command(result.check.command),
            "surfaces": list(result.check.surfaces),
        }
        for result in results
    )
    return {**dict(receipt), "checks": checks}


def validate_team_scope_receipt(receipt: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        failures.append(f"receipt schema_version must be {RECEIPT_SCHEMA_VERSION}")
    metrics = receipt.get("metrics")
    if not isinstance(metrics, dict):
        return [*failures, "receipt metrics must be an object"]
    failures.extend(_validate_receipt_metrics(metrics))
    failures.extend(_validate_receipt_probes(receipt.get("probes")))
    failures.extend(_validate_receipt_surfaces(receipt.get("surfaces")))
    failures.extend(_validate_receipt_checks(receipt.get("checks")))
    return failures


def _validate_observed_metrics(metrics: Mapping[str, Any]) -> list[str]:
    """Budgets that hold without the subprocess evidence checks having run.

    Only the two promotion coverage metrics need those checks, so observe-only
    runs enforce everything else. Skipping the floors there would make
    ``--observe-only`` look like a gate while accepting an empty probe set.
    """
    return _validate_receipt_metrics(metrics, skip=DERIVED_METRICS)


def _validate_receipt_metrics(
    metrics: Mapping[str, Any],
    *,
    skip: frozenset[str] = frozenset(),
) -> list[str]:
    failures: list[str] = []
    for metric, budget in TEAM_SCOPE_BUDGETS.items():
        if metric in skip:
            continue
        value = metrics.get(metric)
        if not isinstance(value, int | float) or isinstance(value, bool):
            failures.append(f"metric {metric!r} must be numeric")
            continue
        if metric in LOWER_IS_BETTER_METRICS:
            if float(value) > float(budget):
                failures.append(f"metric {metric!r} exceeds budget {budget}: {value}")
        elif float(value) < float(budget):
            failures.append(f"metric {metric!r} below budget {budget}: {value}")
    mismatch = metrics.get("membership_resolution_mismatch_count")
    if isinstance(mismatch, int | float) and not isinstance(mismatch, bool) and mismatch:
        failures.append(
            "a membership resolver no longer agrees with the provisioned rows, so "
            "probe expectations and probe observations are no longer independent"
        )
    forwarded = metrics.get("graph_team_membership_forwarded")
    if isinstance(forwarded, int | float) and not isinstance(forwarded, bool) and forwarded:
        failures.append(
            "graph read helper now forwards team membership: retire the "
            "'graph read helper forwards no team or delegation membership' boundary "
            "and add team allow probes on the graph surfaces"
        )
    return failures


def _validate_receipt_surfaces(surfaces: Any) -> list[str]:
    """Every declared probe surface has to have actually run.

    A count floor catches a shrunken probe set but not a renamed or silently
    dropped surface, so the surface names are pinned too.
    """
    if not isinstance(surfaces, list):
        return ["receipt surfaces must be a list"]
    observed = {str(surface) for surface in surfaces}
    missing = sorted(EXPECTED_PROBE_SURFACES - observed)
    unexpected = sorted(observed - EXPECTED_PROBE_SURFACES)
    failures = [f"probe surface {surface!r} never ran" for surface in missing]
    failures.extend(f"receipt reports unknown probe surface {surface!r}" for surface in unexpected)
    return failures


def _validate_receipt_probes(probes: Any) -> list[str]:
    if not isinstance(probes, list) or not probes:
        return ["receipt probes must be a non-empty list"]
    failures: list[str] = []
    for index, probe in enumerate(probes):
        if not isinstance(probe, dict):
            failures.append(f"receipt probes[{index}] must be an object")
            continue
        if probe.get("surface_disagreement"):
            failures.append(
                f"receipt probes[{index}] {probe.get('surface')} "
                f"{probe.get('memory')} as {probe.get('reader')}: "
                "listing and recall disagreed, so one of them stopped filtering"
            )
        if probe.get("status") != "PASS" and probe.get("expected") != probe.get("observed"):
            failures.append(
                f"receipt probes[{index}] {probe.get('surface')} "
                f"{probe.get('memory')} as {probe.get('reader')} "
                f"expected {probe.get('expected')} but observed {probe.get('observed')}"
            )
    return failures


def _validate_receipt_checks(checks: Any) -> list[str]:
    if checks is None:
        return []
    if not isinstance(checks, list):
        return ["receipt checks must be a list"]
    failures: list[str] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            failures.append(f"receipt checks[{index}] must be an object")
            continue
        if check.get("status") != "PASS":
            failures.append(f"receipt checks[{index}] {check.get('name')} did not pass")
    return failures


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def write_receipt(receipt: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(receipt, indent=2, sort_keys=True)}\n", encoding="utf-8")


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _echo(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def _real_runner(command: tuple[str, ...]) -> int:
    executable = which(command[0])
    if executable is None:
        msg = f"Required executable not found on PATH: {command[0]}"
        raise RuntimeError(msg)
    env = dict(os.environ)
    env.setdefault("MOON_COLOR", "false")
    completed = subprocess.run(  # noqa: S603
        (executable, *command[1:]),
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    return completed.returncode


def _run_check(check: GateCheck, *, runner: Runner, echo: Echo) -> GateResult:
    echo("")
    echo(f"[{check.name}] {check.description}")
    echo(f"surfaces: {', '.join(check.surfaces)}")
    echo(f"command: {format_command(check.command)}")

    started = time.perf_counter()
    error: str | None = None
    try:
        exit_code = runner(check.command)
    except Exception as exc:
        exit_code = 1
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started

    status = "PASS" if exit_code == 0 else f"FAIL exit={exit_code}"
    if error is not None:
        status = f"{status} error={error}"
    echo(f"result: {status} in {elapsed:.2f}s")
    return GateResult(check=check, exit_code=exit_code, elapsed_seconds=elapsed, error=error)


def _print_observations(receipt: Mapping[str, Any], *, echo: Echo) -> None:
    metrics = receipt["metrics"]
    echo("")
    echo("Team Scope Read Isolation")
    echo(
        f"probes: {metrics['probe_count']} "
        f"({metrics['deny_probe_count']} deny, {metrics['allow_probe_count']} allow) "
        f"across {metrics['surface_count']} surfaces"
    )
    echo(f"leak_count: {metrics['leak_count']}")
    echo(f"allow_failure_count: {metrics['allow_failure_count']}")
    echo(f"surface_disagreement_count: {metrics['surface_disagreement_count']}")
    for mismatch in receipt.get("membership_mismatches", []):
        echo(f"membership mismatch: {mismatch}")
    for boundary in receipt.get("boundaries", []):
        echo(f"boundary: {boundary}")
    for probe in receipt["probes"]:
        if probe["status"] != "PASS":
            echo(
                f"- FAIL {probe['surface']} {probe['memory']} as {probe['reader']}: "
                f"expected {probe['expected']} observed {probe['observed']} ({probe['detail']})"
            )


def _print_receipt(
    receipt: Mapping[str, Any],
    results: Sequence[GateResult],
    *,
    echo: Echo,
) -> None:
    passed = [result for result in results if result.passed]
    failed = [result for result in results if not result.passed]
    metrics = receipt["metrics"]
    status = "PASS" if not failed and not validate_team_scope_receipt(receipt) else "FAIL"

    echo("")
    echo("Team Scope Trust Gate Receipt")
    echo(f"status: {status}")
    echo(f"checks: {len(passed)} passed, {len(failed)} failed")
    echo("metrics: " + ", ".join(f"{metric}={value}" for metric, value in metrics.items()))
    echo(f"surfaces: {', '.join(sorted(covered_surfaces()))}")
    for result in results:
        check_status = "PASS" if result.passed else f"FAIL exit={result.exit_code}"
        error = f"; error={result.error}" if result.error is not None else ""
        echo(f"- {check_status} {result.check.name} ({result.elapsed_seconds:.2f}s){error}")


def run_observations(
    *,
    echo: Echo = _echo,
    receipt_path: Path | None = None,
    receipt_builder: Callable[[], dict[str, Any]] = build_observed_team_scope_receipt,
) -> int:
    receipt = receipt_builder()
    _print_observations(receipt, echo=echo)
    if receipt_path is not None:
        write_receipt(receipt, receipt_path)
        echo(f"receipt: {display_path(receipt_path)}")
    failures = [
        *_validate_observed_metrics(receipt["metrics"]),
        *_validate_receipt_probes(receipt.get("probes")),
        *_validate_receipt_surfaces(receipt.get("surfaces")),
    ]
    for failure in failures:
        echo(f"- {failure}")
    return 1 if failures else 0


def run_gate(
    checks: Sequence[GateCheck] = GATE_CHECKS,
    *,
    runner: Runner | None = None,
    echo: Echo = _echo,
    receipt_path: Path | None = DEFAULT_RECEIPT_PATH,
    receipt_builder: Callable[[], dict[str, Any]] = build_observed_team_scope_receipt,
) -> int:
    missing = missing_required_surfaces(checks)
    if missing:
        echo("Team scope gate is missing required surfaces:")
        for surface in missing:
            echo(f"- {surface}")
        return 2

    echo("Team Scope Trust Gate")
    echo(f"checks: {len(checks)}")
    echo(f"receipt_schema: {RECEIPT_SCHEMA_VERSION}")
    if receipt_path is not None:
        echo(f"receipt: {display_path(receipt_path)}")

    try:
        receipt = receipt_builder()
    except Exception as exc:
        echo(f"Team scope observations failed: {type(exc).__name__}: {exc}")
        return 1
    _print_observations(receipt, echo=echo)

    active_runner = runner or _real_runner
    evidence_checks = [check for check in checks if check.name not in CONTRACT_CHECK_NAMES]
    contract_checks = [check for check in checks if check.name in CONTRACT_CHECK_NAMES]
    results = [_run_check(check, runner=active_runner, echo=echo) for check in evidence_checks]

    evidence_receipt = with_check_results(
        with_promotion_coverage(receipt, results),
        results,
    )
    failures = validate_team_scope_receipt(evidence_receipt)
    if receipt_path is not None:
        write_receipt(evidence_receipt, receipt_path)
    if failures:
        echo("Team scope receipt failed:")
        for failure in failures:
            echo(f"- {failure}")
        _print_receipt(evidence_receipt, results, echo=echo)
        return 1

    results.extend(_run_check(check, runner=active_runner, echo=echo) for check in contract_checks)
    final_receipt = with_check_results(
        with_promotion_coverage(receipt, results),
        results,
    )
    if receipt_path is not None:
        write_receipt(final_receipt, receipt_path)
    _print_receipt(final_receipt, results, echo=echo)
    if validate_team_scope_receipt(final_receipt):
        return 1
    return 0 if all(result.passed for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run focused team-scope isolation release-gate checks.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List checks and exit without running them.",
    )
    parser.add_argument(
        "--observe-only",
        action="store_true",
        help="Run the in-process scope probes without the moon evidence checks.",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        help="Receipt path. Defaults to the committed receipt, or nowhere with --observe-only.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for check in GATE_CHECKS:
            _echo(f"{check.name}: {format_command(check.command)}")
        return 0
    if args.observe_only:
        return run_observations(receipt_path=args.receipt)
    return run_gate(receipt_path=args.receipt or DEFAULT_RECEIPT_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
