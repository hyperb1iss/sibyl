"""Experimental assertion-level instructions with unchanged evidence and output mechanics."""

from __future__ import annotations

from functools import partial
from typing import Any

from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

from . import fast_critic

VERSION = "assertion-first-critic-v1"
CONTRACTS = ("baseline", "assertion")
ASSERTION_INSTRUCTIONS = """Apply the review to each assertion's actual proposition, preserving its
subject, time, environment, quantifier, and reported-versus-observed scope. Before proposing a
finding, check that its objection applies to words the assertion actually states, not a stronger
claim a reader could imagine. Reporting that two events co-occurred does not itself assert cause.
A supported neighboring assertion must not inherit another assertion's defect.

Still inspect every assertion for real overreach: invented completion, incorrect counts,
omitted necessary conditions, unsupported causal attribution, excessive certainty, and claims
beyond observed cases. A report that someone made a claim is distinct from proving that claim.
Treat unresolved conflicting accounts as conflicting accounts, not proof of either physical
outcome. Missing evidence may justify uncertainty; it does not establish that an event never
happened or that the opposite is true.

Every finding must identify a material defect in its exact target and cite original evidence
that warrants the criticism at the strength stated. Use factual_contradiction only for an
incompatible same-scope fact established by the evidence. Use unsupported_causality for causal
overreach, unsupported_generalization for scope expansion, missing_condition for an omitted
necessary condition, or misleading_certainty for unsupported certainty. These are the existing
schema values; do not invent new basis labels. A correct concern does not license an unsupported
extra clause or correction fact. Keep each critique concise and limited to the evidenced defect.

Advisory labels cannot justify or require a finding. If a label conflicts with original evidence,
judge the assertion from the evidence. Do not emit a finding whose own explanation says its target
is accurate and identifies no actual defect. Return findings=[] and abstention_reason=null when
all assertions are supported within their stated scope. Return every supported material concern
when concerns exist. Abstain only when the evidence prevents a useful assessment. Preserve the
full original review, exact assertion hashes, citation keys, and CriticOutput schema below."""


def critic_request(
    prepared: PreparedMemoryValidation,
    hints: list[dict[str, str]] | None = None,
    *,
    contract: str = "baseline",
) -> dict[str, Any]:
    """Vary instructions only; routes, original evidence, hints and schema stay identical."""
    if contract not in CONTRACTS:
        raise ValueError("unknown critic contract")
    request = fast_critic.critic_request(prepared, hints)
    if contract == "assertion":
        request["messages"][0]["content"] = (
            ASSERTION_INSTRUCTIONS + "\n\n" + request["messages"][0]["content"]
        )
    return request


async def interpret(entry: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the declared contract before applying the unchanged strict parser."""
    contract = entry["contract"]
    if contract not in CONTRACTS:
        raise ValueError("unknown critic contract")
    return await fast_critic.interpret(
        entry, raw, request_builder=partial(critic_request, contract=contract)
    )
