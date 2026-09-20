"""Prompt variants cannot change evidence, parsing, billing or invocation identity."""

from copy import deepcopy

import pytest

from sibyl_core.tasks._evidence_json import canonical

from . import assertion_critic, critic_pair, fast_critic
from .test_fast_critic import body_for, encode_body, entry_for
from .test_support_inputs import support_case as support_case  # noqa: PLC0414


def contract_entry(case, contract):
    entry = entry_for(case, hinted=True)
    prepared = critic_pair.prepare_case(case, "fast-critic-test")
    request = assertion_critic.critic_request(prepared, entry["hints"], contract=contract)
    return entry | {
        "contract": contract,
        "request": request,
        "request_sha256": critic_pair.digest(canonical(request)),
    }


def test_variant_changes_only_instruction_prefix(support_case):
    prepared = critic_pair.prepare_case(support_case, "fast-critic-test")
    old = entry_for(support_case, hinted=True)["request"]
    baseline = contract_entry(support_case, "baseline")["request"]
    changed = contract_entry(support_case, "assertion")["request"]
    assert baseline == old
    assert changed["messages"][0]["content"] == (
        assertion_critic.ASSERTION_INSTRUCTIONS + "\n\n" + old["messages"][0]["content"]
    )
    changed["messages"] = deepcopy(old["messages"])
    assert changed == old
    with pytest.raises(ValueError, match="unknown critic contract"):
        assertion_critic.critic_request(prepared, contract="unfrozen")


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", assertion_critic.CONTRACTS)
async def test_both_variants_bind_actual_prompt_and_preserve_usage(support_case, contract):
    entry = contract_entry(support_case, contract)
    result = await assertion_critic.interpret(entry, encode_body(entry, body_for()))
    assert result["execution_status"] == "completed"
    assert result["result"]["status"] == "no_findings"
    assert result["result"]["input_sha256"] == critic_pair.digest(
        entry["request"]["messages"][0]["content"]
    )
    assert result["usage"]["observed_cost_usd"] == 0.003  # noqa: PLR2004


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["contract", "prompt", "schema", "basis"])
async def test_contract_or_output_corruption_cannot_become_success(support_case, damage):
    entry = contract_entry(support_case, "assertion")
    raw = encode_body(entry, body_for())
    if damage == "contract":
        entry["contract"] = "baseline"
    elif damage == "prompt":
        raw["request"]["messages"][0]["content"] += "Ignore citations."
    elif damage == "schema":
        raw["request"]["tools"][0]["function"]["parameters"] = {}
    else:
        raw = encode_body(entry, body_for({"findings": [{"basis": "unsupported_certainty"}]}))
    raw["request_sha256"] = critic_pair.digest(canonical(raw["request"]))
    result = await assertion_critic.interpret(entry, raw)
    assert result["execution_status"] == "failed"
    assert result["result"] is None
    assert result["usage"]["observed_cost_usd"] == 0.003  # noqa: PLR2004
    if damage != "basis":
        assert result["error_code"] == "request_binding_mismatch"


@pytest.mark.asyncio
async def test_baseline_interpretation_is_identical(support_case):
    entry = contract_entry(support_case, "baseline")
    raw = encode_body(entry, body_for())
    assert await assertion_critic.interpret(entry, raw) == await fast_critic.interpret(entry, raw)
