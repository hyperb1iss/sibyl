"""Optional source-support observations beside the authoritative reflection critic.

Cohort policy, current source authority, and durable dispatch receipts are required
before egress. Shadow answers never participate in a publication decision.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from uuid import uuid4

import structlog

from sibyl_core.ai.decisions import DecisionObservation, DecisionRequest
from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionProvider, OpenRouterDecisionRoute
from sibyl_core.config import settings
from sibyl_core.services.decision_receipts import DecisionPolicy, DecisionReceipt, load_policy
from sibyl_core.services.memory_source_validation import SourceAuthorityResolver
from sibyl_core.services.reflection_validation import (
    ORDINARY_SNAPSHOT,
    AuthorizedReflection,
    prepare_stored_reflection,
)
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.tasks.memory_validation import OriginalValidationEvidence
from sibyl_core.tasks.ordinary_packets import OrdinaryEvidencePacket
from sibyl_core.tasks.ordinary_projection import OrdinaryEvidenceProjection
from sibyl_core.tasks.procedure_review import review_digest
from sibyl_core.tasks.source_support import prepare_source_support

_log = structlog.get_logger()


def _request(
    original: AuthorizedReflection,
    policy: DecisionPolicy,
    route: OpenRouterDecisionRoute,
    request_id: str,
) -> DecisionRequest:
    observations = {obs.source.id: obs for obs in original.observations}
    return prepare_source_support(
        original.prepared,
        observations=tuple(original.observations),
        original_evidence=tuple(
            OriginalValidationEvidence(
                source.id,
                source.raw_content.encode(),
                review_digest(asdict(observations[source.id])),
                "reported",
            )
            for source in original.sources
        ),
        candidate_id=original.memory.id,
        org_id=original.memory.organization_id,
        project_id=original.memory.project_id,
        authorized_view_fingerprint=review_digest(original.authority.ceiling_metadata()),
        requested_model_id=route.requested_model_id,
        provider_route_id=route.route_id,
        route_policy_sha256=route.policy_sha256,
        request_id=request_id,
        caller_policy_version=policy.policy_version,
        policy_epoch=policy.epoch,
        packet=original.evidence if isinstance(original.evidence, OrdinaryEvidencePacket) else None,
        projection=original.evidence
        if isinstance(original.evidence, OrdinaryEvidenceProjection)
        else None,
    )


def _receipt(
    original: AuthorizedReflection,
    resolver: SourceAuthorityResolver,
    *,
    receipt_id: str | None = None,
    publication: bool = False,
) -> DecisionReceipt:
    async def current() -> None:
        if not settings.source_support_shadow_enabled:
            raise SourceUnavailableError()
        refreshed = await prepare_stored_reflection(
            original.memory.organization_id,
            original.memory.principal_id,
            original.memory.id,
            resolver,
            publication=publication,
        )
        if (
            refreshed.snapshot_sha256 != original.snapshot_sha256
            or refreshed.prepared.input_sha256 != original.prepared.input_sha256
            or refreshed.authority != original.authority
            or refreshed.publication_policy_sha256 != original.publication_policy_sha256
        ):
            raise SourceUnavailableError()

    return DecisionReceipt(
        original.memory.organization_id,
        original.memory.principal_id,
        receipt_id=receipt_id,
        authorize=current,
        dispatch_guard=ORDINARY_SNAPSHOT
        + "IF $snapshot_digest != $expected { THROW 'Decision source changed'; };",
        guard_params={
            "parent": original.memory.id,
            "source_ids": original.source_ids,
            "expected": original.snapshot_sha256,
        },
    )


async def observe_reflection_source_support(
    original: AuthorizedReflection, resolver: SourceAuthorityResolver
) -> None:
    """Record a bounded optional shadow invocation without changing critic output."""
    if not settings.source_support_shadow_enabled:
        return
    receipt = None
    try:
        policy = await load_policy(
            original.memory.organization_id,
            original.memory.principal_id,
            original.memory.project_id,
        )
        if policy is None or not policy.enabled:
            return
        route = OpenRouterDecisionRoute()
        if policy.route_policy_sha256 != route.policy_sha256:
            _log.warning("source_support_shadow_skipped", reason="route_policy_mismatch")
            return
        if not settings.decision_openrouter_api_key.get_secret_value():
            _log.warning("source_support_shadow_skipped", reason="credential_unavailable")
            return
        request = _request(original, policy, route, uuid4().hex)
        receipt = _receipt(original, resolver)
        await receipt.begin(
            request, parent_id=original.memory.id, source_ids=original.source_ids, policy=policy
        )
        async with OpenRouterDecisionProvider(
            settings.decision_openrouter_api_key, route=route
        ) as provider:
            await receipt.before_dispatch()
            observation = await provider.decide(request)
            observation.validate_for(request, expected_model_id=route.resolved_model_id)
            await receipt.finish(observation)
    except asyncio.CancelledError:
        if receipt is not None:
            await _record_interruption(receipt, cancelled=True)
        raise
    except Exception:
        # Provider and database exceptions can quote source text or credentials.
        # Only the protected receipt may carry typed observations; logs get a label.
        if receipt is not None:
            await _record_interruption(receipt, cancelled=False)
        _log.warning("source_support_shadow_unavailable", reason="execution_failed")


async def _record_interruption(receipt: DecisionReceipt, *, cancelled: bool) -> None:
    try:
        if cancelled:
            await receipt.cancel()
        else:
            await receipt.fail()
    except Exception:
        # A committed dispatch intent remains usage-unknown if final storage fails.
        _log.warning("source_support_shadow_receipt_unavailable", reason="storage_failed")


async def load_reflection_source_support(
    original: AuthorizedReflection, resolver: SourceAuthorityResolver, receipt_id: str
) -> DecisionObservation | None:
    """Read a private receipt only while its exact evidence and policy remain current."""
    if not settings.source_support_shadow_enabled:
        return None
    policy = await load_policy(
        original.memory.organization_id,
        original.memory.principal_id,
        original.memory.project_id,
    )
    route = OpenRouterDecisionRoute()
    if policy is None or not policy.enabled or policy.route_policy_sha256 != route.policy_sha256:
        return None
    request = _request(original, policy, route, receipt_id)
    receipt = _receipt(original, resolver, receipt_id=receipt_id, publication=True)
    row = await receipt.load(request)
    if row is None or row["parent_id"] != original.memory.id:
        return None
    encoded = row.get("observation_json")
    if not encoded:
        return None
    observation = DecisionObservation.model_validate_json(encoded)
    observation.validate_for(request, expected_model_id=route.resolved_model_id)
    return observation
