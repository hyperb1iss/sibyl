"""Explicit verdicts cannot omit assertions or hide incomplete assessments in projection."""
# Concrete fixture counts and costs make the accounting oracle explicit.
# ruff: noqa: PLR2004

import json
from copy import deepcopy

import pytest

from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

from . import assertion_critic, critic_pair, fast_critic, verdict_critic
from .test_fast_critic import body_for, encode_body, entry_for
from .test_support_inputs import support_case as support_case  # noqa: PLC0414


def verdict_entry(case, contract="verdict"):
    entry = entry_for(case, hinted=True)
    prepared = PreparedMemoryValidation(entry["prepared_payload"])
    wire = verdict_critic.critic_request(prepared, entry["hints"], contract=contract)
    return entry | {
        "contract": contract,
        "request": wire,
        "request_sha256": critic_pair.digest(canonical(wire)),
    }


def supported_output(entry):
    payload = json.loads(entry["prepared_payload"])
    return {
        "verdicts": [
            {
                "claim_path": path,
                "claim_sha256": digest,
                "verdict": "supported",
                "rationale": "The cited evidence establishes this assertion.",
                "evidence_refs": [{"evidence_id": "s0"}],
            }
            for path, digest in payload["assertion_hashes"].items()
        ]
    }


def concern(verdict):
    return {
        "claim_path": verdict["claim_path"],
        "claim_sha256": verdict["claim_sha256"],
        "verdict": "concern",
        "findings": [
            {
                "claim_path": verdict["claim_path"],
                "claim_sha256": verdict["claim_sha256"],
                "evidence_refs": [{"evidence_id": "s0"}],
                "basis": "unsupported_generalization",
                "disposition": "qualify",
                "critique": "The universal assertion exceeds the observed cases.",
            }
        ],
    }


def raw_for(entry, output):
    body = body_for(output)
    body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "VerdictOutput"
    return encode_body(entry, body)


def test_baseline_exact_and_intervention_only_schema_instructions(support_case):
    baseline = verdict_entry(support_case, "baseline")
    changed = verdict_entry(support_case)
    prepared = PreparedMemoryValidation(baseline["prepared_payload"])
    assert baseline["request"] == assertion_critic.critic_request(
        prepared, baseline["hints"], contract="assertion"
    )
    assert (
        changed["request"]["messages"][0]["content"]
        == verdict_critic.INSTRUCTIONS + "\n\n" + baseline["request"]["messages"][0]["content"]
    )
    assert (
        changed["request"]["tools"][0]["function"]["parameters"]
        == verdict_critic.VerdictOutput.model_json_schema()
    )
    assert changed["request"]["tool_choice"]["function"]["name"] == "VerdictOutput"
    for key in ("model", "provider", "max_tokens"):
        assert changed["request"][key] == baseline["request"][key]
    assert "PRIVATE-GOLD" not in canonical(changed["request"])
    for wire in (baseline["request"], changed["request"]):
        assert (
            json.loads(wire["messages"][0]["content"].split(fast_critic.HINT_SLOT)[1])
            == baseline["hints"]
        )


@pytest.mark.asyncio
async def test_baseline_interpretation_exact(support_case):
    entry = verdict_entry(support_case, "baseline")
    raw = encode_body(entry, body_for())
    old_entry = {**entry, "contract": "assertion"}
    assert await verdict_critic.interpret(entry, raw) == await assertion_critic.interpret(
        old_entry, raw
    )


@pytest.mark.asyncio
async def test_all_supported_retains_raw_rationales_and_exact_projection(support_case):
    entry = verdict_entry(support_case)
    output = supported_output(entry)
    # Mechanically valid does not mean semantically true: the raw text must survive review.
    output["verdicts"][0]["rationale"] = "A FALSE ACCUSATION MUST REMAIN VISIBLE TO THE REVIEWER."
    result = await verdict_critic.interpret(entry, raw_for(entry, output))
    assert result["execution_status"] == "completed"
    assert result["result"]["status"] == "no_findings"
    assert result["verdict_output"] == output
    assert result["projected_output"] == {"findings": [], "abstention_reason": None}
    assert result["usage"]["observed_cost_usd"] == 0.003
    assert result["result"]["input_sha256"] == critic_pair.digest(
        entry["request"]["messages"][0]["content"]
    )


@pytest.mark.asyncio
async def test_concern_and_unable_preserve_findings_and_abstention(support_case):
    entry = verdict_entry(support_case)
    output = supported_output(entry)
    output["verdicts"][0] = concern(output["verdicts"][0])
    other = output["verdicts"][1]
    output["verdicts"][1] = {
        "claim_path": other["claim_path"],
        "claim_sha256": other["claim_sha256"],
        "verdict": "unable",
        "reason": "Evidence does not resolve this assertion.",
    }
    result = await verdict_critic.interpret(entry, raw_for(entry, output))
    assert result["result"]["status"] == "abstain"
    assert result["result"]["submission"]["findings"] == output["verdicts"][0]["findings"]
    assert result["projected_output"]["findings"] == output["verdicts"][0]["findings"]
    assert result["projected_output"]["abstention_reason"]
    assert result["verdict_output"] == output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "unknown",
        "hash",
        "empty",
        "foreign_citation",
        "duplicate_citation",
        "hidden_finding",
        "empty_concern",
        "finding_target",
        "finding_hash",
        "finding_citation",
        "basis",
        "blank_reason",
        "extra",
    ],
)
async def test_invalid_verdict_rejects_whole_response_and_keeps_cost(support_case, damage):
    entry = verdict_entry(support_case)
    output = supported_output(entry)
    first = output["verdicts"][0]
    if damage == "missing":
        output["verdicts"].pop()
    elif damage == "duplicate":
        output["verdicts"].append(deepcopy(first))
    elif damage == "unknown":
        first["claim_path"] = "/unknown"
    elif damage == "hash":
        first["claim_sha256"] = "a" * 64
    elif damage == "empty":
        output["verdicts"] = []
    elif damage == "foreign_citation":
        first["evidence_refs"] = [{"evidence_id": "raw"}]
    elif damage == "duplicate_citation":
        first["evidence_refs"] *= 2
    elif damage == "hidden_finding":
        first["findings"] = concern(first)["findings"]
    elif damage == "blank_reason":
        output["verdicts"][0] = {
            "claim_path": first["claim_path"],
            "claim_sha256": first["claim_sha256"],
            "verdict": "unable",
            "reason": "   ",
        }
    elif damage == "extra":
        output["gold"] = "not allowed"
    else:
        _damage_concern(output, damage)
    raw = raw_for(entry, output)
    result = await verdict_critic.interpret(entry, raw)
    assert result["execution_status"] == "failed"
    assert result["result"] is None
    assert "projected_output" not in result
    assert result["response_body_base64"] == raw["response_body_base64"]
    assert result["usage"]["observed_cost_usd"] == 0.003
    assert result["usage"]["attempt_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["contract", "wire", "route", "tool", "truncated", "duplicate_json"]
)
async def test_wire_schema_route_and_arguments_are_bound(support_case, damage):
    entry = verdict_entry(support_case)
    output = supported_output(entry)
    raw = raw_for(entry, output)
    if damage == "contract":
        entry["contract"] = "baseline"
    elif damage == "wire":
        raw["request"]["messages"][0]["content"] += " changed"
    else:
        body = json.loads(raw["response_body"])
        fn = body["choices"][0]["message"]["tool_calls"][0]["function"]
        if damage == "route":
            body["provider"] = "Other"
        elif damage == "tool":
            fn["name"] = "CriticOutput"
        elif damage == "truncated":
            body["choices"][0]["finish_reason"] = "length"
        else:
            fn["arguments"] = '{"verdicts": [], "verdicts": []}'
        raw = encode_body(entry, body)
    result = await verdict_critic.interpret(entry, raw)
    assert result["execution_status"] == "failed"
    assert result["usage"]["observed_cost_usd"] == 0.003


def _damage_concern(output, damage):
    first = output["verdicts"][0] = concern(output["verdicts"][0])
    if damage == "empty_concern":
        first["findings"] = []
    elif damage == "finding_target":
        first["findings"][0]["claim_path"] = output["verdicts"][1]["claim_path"]
    elif damage == "finding_hash":
        first["findings"][0]["claim_sha256"] = "b" * 64
    elif damage == "finding_citation":
        first["findings"][0]["evidence_refs"] = [{"evidence_id": "absent"}]
    elif damage == "basis":
        first["findings"][0]["basis"] = "unsupported_certainty"


def test_supported_verdict_checks_actual_assertion_digest(support_case):
    entry = verdict_entry(support_case)
    output = verdict_critic.VerdictOutput.model_validate(supported_output(entry))
    payload = json.loads(entry["prepared_payload"])
    payload["assertions"]["/content"] = "Changed assertion"
    with pytest.raises(ValueError, match="hash mismatch"):
        verdict_critic.project(output, PreparedMemoryValidation(canonical(payload)))
