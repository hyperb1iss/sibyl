"""Keep the kind ablation limited to one explicit state value."""

import json

import pytest

from . import prompts_v3, prompts_v4
from .runner import prompt_dependencies


@pytest.fixture
def case():
    return {
        "memory": "The indicator is blue.",
        "event": "The indicator is amber during inspection.",
        "source_text": "The indicator is amber during inspection.",
        "claim_kind": "current_state",
        "model_claim_kind": "unknown",
        "source_authority": "authoritative",
        "expected_disposition": "overlay",
        "event_extent": "temporary",
        "as_of": "2026-02-01T00:00:00Z",
    }


def test_kind_is_the_only_request_difference(case):
    unknown = prompts_v4.make_request([case], "direct", "toy")
    typed = prompts_v4.make_request(
        [{**case, "model_claim_kind": "current_state"}], "direct", "toy"
    )
    a, b = unknown.model_dump(), typed.model_dump()
    state_a, state_b = json.loads(a.pop("state")), json.loads(b.pop("state"))
    assert a == b
    assert state_a["pairs"][0].pop("claim_kind") == "unknown"
    assert state_b["pairs"][0].pop("claim_kind") == "current_state"
    assert state_a == state_b


def test_offline_policy_metadata_and_labels_do_not_enter_request(case):
    changed = dict(
        case,
        claim_kind="historical",
        source_authority="untrusted",
        expected_disposition="review",
        event_extent="permanent",
        as_of=None,
        rationale="SECRET",
        expected_source_relation="unrelated",
    )
    request = prompts_v4.make_request([case], "direct", "toy")
    assert request == prompts_v4.make_request([changed], "direct", "toy")
    assert set(json.loads(request.state)["pairs"][0]) == {"memory", "event", "claim_kind"}


@pytest.mark.parametrize("kind", [None, "", "retire", "CURRENT_STATE"])
def test_missing_or_invalid_experimental_kind_rejected(case, kind):
    with pytest.raises(ValueError, match="model_claim_kind"):
        prompts_v4.make_request([{**case, "model_claim_kind": kind}], "direct", "toy")


def test_source_projection_guard_and_frozen_composition(case):
    with pytest.raises(ValueError, match="original-source projected"):
        prompts_v4.make_request([{**case, "event": "A lossy summary."}], "direct", "toy")
    for relation in ("contradicted", "superseded", "compatible", "uncertain"):
        for witness in ("explicit", "absent", "partial", "unclear"):
            answers = {"c0.relation": relation, "c0.witness": witness}
            assert prompts_v4.predict(answers, 0, "direct") == prompts_v3.predict(
                answers, 0, "direct"
            )


def test_transitive_source_prompt_dependencies_are_bound():
    assert set(prompt_dependencies("v4")) == {"prompts.py", "prompts_v3.py"}
    assert set(prompt_dependencies("v3")) == {"prompts.py"}
    assert prompt_dependencies("v1") == {}
