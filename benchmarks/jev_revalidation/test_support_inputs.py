"""Exercise the actual product preparation path with synthetic evidence."""

import json
from copy import deepcopy

import pytest

from sibyl_core.tasks.source_support import INSTRUCTIONS, OPTIONS, SOURCE_SUPPORT_VERSION

from .support_inputs import load_cases, make_request, program_hashes


@pytest.fixture
def support_case():
    return {
        "id": "example",
        "category": "scope",
        "content": "Two checks passed.",
        "claims": ["All checks passed."],
        "sources": [
            {"id": "raw", "text": "Two checks passed; a third failed.", "provenance": "reported"}
        ],
        "expected": {"/content": "supported", "/claim_records/0/content": "contradicted"},
        "rationale": "PRIVATE-GOLD-RATIONALE",
    }


def test_request_uses_product_questions_and_original_sources(support_case):
    request = make_request(support_case, "run")
    assert request.question_set_version == SOURCE_SUPPORT_VERSION
    assert {q.question_id for q in request.questions} == set(support_case["expected"])
    for question in request.questions:
        assert question.instructions == INSTRUCTIONS + question.question_id
        assert question.options == OPTIONS
    state = json.loads(request.state)
    assert state["sources"]["raw"]["text"] == support_case["sources"][0]["text"]
    assert request.org_id == request.source_refs[0].source.organization_id


def test_gold_and_categories_never_enter_provider_input(support_case):
    changed = deepcopy(support_case)
    changed.update(category="SECRET", rationale="SECRET", expected={"/content": "ambiguous"})
    request = make_request(support_case, "run")
    assert request == make_request(changed, "run")
    assert "PRIVATE-GOLD" not in request.model_dump_json()


def test_request_is_deterministic_and_binds_changed_evidence(support_case):
    original = make_request(support_case, "run")
    assert make_request(support_case, "run") == original
    repeated = make_request(support_case, "repeat")
    assert repeated.semantic_input_sha256 == original.semantic_input_sha256
    assert repeated.request_digest != original.request_digest
    changed = deepcopy(support_case)
    changed["sources"][0]["text"] = "Every check failed."
    assert make_request(changed, "run").semantic_input_sha256 != original.semantic_input_sha256
    changed = deepcopy(support_case)
    changed["claims"][0] = "No checks passed."
    assert make_request(changed, "run").semantic_input_sha256 != original.semantic_input_sha256


@pytest.mark.parametrize(
    "damage",
    ["duplicate_id", "missing_label", "unknown_label", "duplicate_source", "invalid_provenance"],
)
def test_fixture_validation_rejects_broken_inputs(support_case, tmp_path, damage):
    case = deepcopy(support_case)
    cases = [case]
    if damage == "duplicate_id":
        cases.append(case)
    elif damage == "missing_label":
        case["expected"].pop("/content")
    elif damage == "unknown_label":
        case["expected"]["/content"] = "retire"
    elif damage == "duplicate_source":
        case["sources"].append(case["sources"][0])
    else:
        case["sources"][0]["provenance"] = "trusted"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    with pytest.raises(ValueError, match="support"):
        load_cases(path)


def test_adapter_and_product_program_are_bound():
    assert set(program_hashes()) == {
        "support_inputs.py",
        "memory_validation.py",
        "source_support.py",
    }
