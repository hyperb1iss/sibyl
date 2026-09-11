"""Corrections preserve unedited claims and resolve edits against one parent."""

import copy
import json

import pytest

from sibyl_core.tasks import consolidation as c
from sibyl_core.tasks.episode_evidence import EvidenceCitation
from sibyl_core.tasks.procedure_edits import (
    VERSION,
    ProcedureEdits,
    apply_procedure_edits,
    correction_record,
)
from sibyl_core.tasks.procedure_review import ReviewSubmission, assertion_index, review_digest
from tests.test_tasks_consolidation import group as group
from tests.test_tasks_consolidation import procedure as procedure


def edit(parent, path, statement="Use the scoped project"):
    return {
        "claim_path": path,
        "claim_sha256": review_digest(assertion_index(parent)[path]),
        "replacement": None
        if statement is None
        else {
            "statement": statement,
            "label": "inferred",
            "support": [{"evidence_id": "original"}],
        },
    }


def output(edits):
    return ProcedureEdits.model_validate(
        {"assessments": [], "outcome": {"kind": "edits", "edits": edits}}
    )


@pytest.fixture
def citations():
    return {"original": EvidenceCitation(episode_id="success", ranges=((0, 4),))}


def test_procedure_edits_preserve_every_unlisted_assertion(procedure, citations):
    before = procedure.model_dump()
    result = apply_procedure_edits(procedure, output([edit(procedure, "/goal")]), citations)
    expected = copy.deepcopy(before)
    expected["goal"] = {
        "statement": "Use the scoped project",
        "label": "inferred",
        "support": [{"episode_id": "success", "start_byte": 0, "end_byte": 4}],
    }
    assert result.procedure.model_dump() == expected
    assert procedure.model_dump() == before


@pytest.mark.parametrize("change", ["hash", "foreign_path", "alias", "duplicate", "source"])
def test_procedure_edits_reject_stale_or_ambiguous_targets(procedure, citations, change):
    patch = edit(procedure, "/goal")
    if change == "hash":
        patch["claim_sha256"] = "f" * 64
    if change == "foreign_path":
        patch["claim_path"] = "/metadata"
    if change == "alias":
        patch["claim_path"] = "/environment/00"
    if change == "source":
        patch["replacement"]["support"][0]["evidence_id"] = "foreign"
    with pytest.raises(ValueError):
        apply_procedure_edits(
            procedure, output([patch, patch] if change == "duplicate" else [patch]), citations
        )


def test_procedure_edits_delete_after_replacements_use_original_indices(procedure, citations):
    procedure.environment.append(
        procedure.environment[0].model_copy(update={"statement": "Second"})
    )
    patches = [
        edit(procedure, "/environment/0", None),
        edit(procedure, "/environment/1", "Retained second"),
    ]
    result = apply_procedure_edits(procedure, output(patches), citations)
    assert [a.statement for a in result.procedure.environment] == ["Retained second"]
    assert len(procedure.environment) == 2


def test_procedure_edits_remove_whole_step_and_derive_order(procedure, citations):
    procedure.actions.append(procedure.actions[0].model_copy(update={"order": 2}))
    second = procedure.actions[1].model_dump()
    result = apply_procedure_edits(
        procedure, output([edit(procedure, "/actions/0/action", None)]), citations
    )
    assert result.procedure.actions[0].model_dump() == {**second, "order": 1}
    assert len(procedure.actions) == 2


@pytest.mark.parametrize(
    "path",
    [
        "/goal",
        "/expected_result",
        "/actions/0/success_criteria",
        "/actions/0/action",
        "/environment/0",
    ],
)
def test_procedure_edits_cannot_remove_required_remaining_content(procedure, citations, path):
    with pytest.raises(ValueError):
        apply_procedure_edits(procedure, output([edit(procedure, path, None)]), citations)


def test_procedure_edits_reject_check_edit_on_removed_step(procedure, citations):
    procedure.actions.append(procedure.actions[0].model_copy(update={"order": 2}))
    with pytest.raises(ValueError, match="removed action"):
        apply_procedure_edits(
            procedure,
            output(
                [
                    edit(procedure, "/actions/0/action", None),
                    edit(procedure, "/actions/0/success_criteria"),
                ]
            ),
            citations,
        )


def test_procedure_edits_allow_no_change_and_explicit_abstention(procedure, citations):
    assert apply_procedure_edits(procedure, output([]), citations).procedure == procedure
    abstain = ProcedureEdits.model_validate(
        {"assessments": [], "outcome": {"kind": "abstention", "reason": "Unsupported"}}
    )
    assert apply_procedure_edits(procedure, abstain, citations).abstention_reason == "Unsupported"


def test_procedure_edits_prompt_contains_complete_parent_without_changing_legacy(group, procedure):
    review = ReviewSubmission.model_validate(
        {
            "parent_operation_id": "a" * 64,
            "parent_candidate_sha256": "b" * 64,
            "findings": [
                {
                    "claim_path": "/goal",
                    "claim_sha256": review_digest(procedure.goal.model_dump(mode="json")),
                    "evidence_refs": [{"evidence_id": "episode:0"}],
                    "basis": "missing_condition",
                    "disposition": "qualify",
                    "critique": "Qualify scope",
                }
            ],
        }
    )
    legacy_record = {
        "submission": review.model_dump(mode="json"),
        "parent_procedure": procedure.model_dump(mode="json"),
    }
    evidence = c._extraction_input(group)
    legacy = c._review_input(group, evidence, legacy_record)
    record = correction_record(review, procedure.model_dump(mode="json"))
    current = c._review_input(group, evidence, record)
    section = json.loads(current.prompt.rsplit("\n", 1)[1])
    assert section["parent_procedure"] == procedure.model_dump(mode="json")
    assert section["claim_sha256_by_path"] == {
        path: review_digest(value) for path, value in assertion_index(procedure).items()
    }
    assert current.output_type is ProcedureEdits
    assert "must use evidence_id values" in current.system
    assert "Do not generate a complete replacement procedure" in current.system
    assert "Every assertion must cite exact UTF-8 byte ranges" not in current.system
    assert c.RETROSPECTIVE_REQUEST not in current.prompt
    assert legacy.system == c.SYSTEM_PROMPT
    assert c.RETROSPECTIVE_REQUEST in legacy.prompt
    assert current.reconsideration["correction_version"] == VERSION
    assert legacy.output_type is c.ReconsideredProcedureProposal
    assert "parent_procedure" not in json.loads(legacy.prompt.rsplit("\n", 1)[1])
    assert c._review_input(group, evidence, legacy_record) == legacy
    assert current.prompt != legacy.prompt
    with pytest.raises(ValueError, match="unknown correction"):
        c._review_input(group, evidence, {**record, "correction_version": "unknown"})
