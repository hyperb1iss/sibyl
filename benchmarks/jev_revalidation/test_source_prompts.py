"""Guard source isolation and counterevidence composition using toy inputs."""

import json

import pytest

from . import prompts_v3


def test_source_request_ignores_summary_labels_and_validity_metadata():
    case = {
        "memory": "All office lamps are blue.",
        "event": "One office lamp is red.",
        "source_text": "One office lamp is red.",
        "original_summary": "PRIVATE-SUMMARY-MUST-NOT-ENTER-STATE",
        "expected_source_relation": "contradicted",
        "expected_disposition": "retire",
        "claim_kind": "current_state",
        "event_extent": "permanent",
        "as_of": "2026-02-01T00:00:00Z",
        "event_valid_until": None,
    }
    request = prompts_v3.make_request([case], "direct", "toy")
    changed = dict(
        case,
        original_summary="DIFFERENT-SUMMARY",
        expected_source_relation="supported",
        expected_disposition="retain",
        claim_kind="historical",
        event_extent="temporary",
        as_of=None,
        event_valid_until="2026-03-01T00:00:00Z",
    )
    assert request == prompts_v3.make_request([changed], "direct", "toy")
    assert json.loads(request.state) == {
        "pairs": [{"memory": case["memory"], "event": case["source_text"]}]
    }
    assert "PRIVATE-SUMMARY" not in request.model_dump_json()


@pytest.mark.parametrize("source", [None, "", "A different original passage."])
def test_unprojected_source_is_rejected(source):
    with pytest.raises(ValueError, match="original-source projected"):
        prompts_v3.make_request(
            [
                {
                    "memory": "The light is green.",
                    "event": "The light is red.",
                    "source_text": source,
                }
            ],
            "direct",
            "toy",
        )


@pytest.mark.parametrize("relation", ["contradicted", "superseded"])
@pytest.mark.parametrize("witness", ["absent", "partial", "unclear"])
def test_conflict_without_explicit_witness_is_uncertain(relation, witness):
    answers = {"c0.relation": relation, "c0.witness": witness}
    assert prompts_v3.predict(answers, 0, "direct") == "uncertain"


def test_explicit_counterexample_can_refute_universal_claim():
    answers = {"c0.relation": "contradicted", "c0.witness": "explicit"}
    assert prompts_v3.predict(answers, 0, "direct") == "contradicted"


def test_witness_alone_cannot_create_a_conflict():
    answers = {"c0.relation": "uncertain", "c0.witness": "explicit"}
    assert prompts_v3.predict(answers, 0, "direct") == "uncertain"
