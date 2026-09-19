"""Controlled claim-kind context atop the frozen original-source prompt."""

from __future__ import annotations

import json

from sibyl_core.ai.decisions import DecisionRequest

from . import prompts_v3

VERSION = "jev-revalidation-kind-v4"
KINDS = frozenset({"current_state", "standing_rule", "historical", "unknown"})
_KIND_CONTEXT = (
    "The pair's claim_kind describes how to interpret its memory. "
    "current_state means the actual state being tracked, not a default or standing rule: "
    "an explicit temporary replacement is counterevidence to that state during the change. "
    "standing_rule means a general rule or default: a temporary exception can coexist with it. "
    "historical means an assertion about a past occurrence. "
    "unknown supplies no additional interpretation; use only the evidence text. "
    "The kind alone is not evidence of a change. Do not infer authority, date order, "
    "expiry, or a lifecycle action from it. "
)


def make_request(cases: list[dict], arm: str, run_id: str) -> DecisionRequest:
    """Expose only the explicit experimental kind alongside original evidence."""
    if any(case.get("model_claim_kind") not in KINDS for case in cases):
        raise ValueError("source v4 requires an explicit valid model_claim_kind")
    original = prompts_v3.make_request(cases, arm, run_id)
    state = json.loads(original.state)
    for pair, case in zip(state["pairs"], cases, strict=True):
        pair["claim_kind"] = case["model_claim_kind"]
    values = dict(original)
    values.update(
        question_set_version=VERSION + "/" + arm,
        state=json.dumps(state, sort_keys=True, ensure_ascii=False),
        questions=tuple(
            question.model_copy(update={"instructions": question.instructions + _KIND_CONTEXT})
            for question in original.questions
        ),
    )
    return DecisionRequest.model_validate(values)


def predict(answers: dict[str, str], case_index: int, arm: str) -> str:
    """Keep the frozen source relation and witness composition unchanged."""
    return prompts_v3.predict(answers, case_index, arm)
