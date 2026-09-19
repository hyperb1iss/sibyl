"""Prospective scope/state clarification; the original questions stay immutable."""

from __future__ import annotations

from sibyl_core.ai.decisions import ChoiceOption, ChoiceQuestion, DecisionRequest

from . import prompts

VERSION = "jev-revalidation-choice-v2"
_CLARIFICATION = (
    "A changed value for the same subject and property is not a different condition. "
    "Different conditions mean distinct actors, environments, versions, or explicit "
    "preconditions under which both assertions can hold; a replacement value alone "
    "does not establish different conditions. "
    "Distinguish a durable rule from a current state: a temporary event may contradict "
    "a claim about current status, even when it does not revoke a standing policy. "
)
_OPTION_CHANGES = {
    ("scope", "same"): (
        "Same subject, property, and applicable conditions, including when its value changes."
    ),
    ("scope", "different_conditions"): (
        "Distinct actors, environments, versions, or explicit preconditions explain why both "
        "assertions can hold. A changed value alone is not a different condition."
    ),
    ("effect", "supports"): (
        "The event supports the memory. A temporary exception may preserve a standing rule, "
        "but cannot support an incompatible claim about current status."
    ),
}


def make_request(cases: list[dict], arm: str, run_id: str) -> DecisionRequest:
    """Change only question wording/version, preserving v1 data isolation and identities."""
    original = prompts.make_request(cases, arm, run_id)
    questions = []
    for question in original.questions:
        component = question.question_id.split(".", 1)[1]
        questions.append(
            ChoiceQuestion(
                question_id=question.question_id,
                instructions=question.instructions + " " + _CLARIFICATION,
                options=tuple(
                    ChoiceOption(
                        label=option.label,
                        description=_OPTION_CHANGES.get(
                            (component, option.label), option.description
                        ),
                    )
                    for option in question.options
                ),
            )
        )
    values = dict(original)
    values.update(question_set_version=VERSION + "/" + arm, questions=tuple(questions))
    return DecisionRequest.model_validate(values)


predict = prompts.predict
