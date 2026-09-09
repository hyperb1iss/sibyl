"""Every terminal consolidation preserves the evidence of its own outcome."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.services import content_client
from sibyl_core.services import eval_publication as p
from sibyl_core.tasks import consolidation as c
from tests.test_eval_publication import (
    admitted_pair as admitted_pair,
)
from tests.test_eval_publication import (
    content_store as content_store,
)
from tests.test_eval_publication import (
    evidence as evidence,
)
from tests.test_eval_publication import (
    proposal as proposal,
)
from tests.test_eval_publication import (
    rows,
)


def negative(result, status):
    reason = "Evidence is too narrow" if status == "abstained" else "Missing failure support"
    receipt = deepcopy(result.receipt)
    receipt.update(
        status=status,
        reason=reason,
        usage={
            "requests": 2,
            "input_tokens": 81,
            "output_tokens": 12,
            "cost_usd": None,
            "cost_complete": False,
            "transport_attempts": [
                {"status_code": 200, "usage_known": True},
                {"status_code": 502, "usage_known": False},
            ],
            "transport_usage_complete": False,
        },
    )
    return replace(
        result,
        candidate=None,
        receipt=receipt,
        proposal=c.ProcedureProposal(abstention_reason=reason)
        if status == "abstained"
        else result.proposal,
    )


@pytest.mark.parametrize("status", ["abstained", "rejected"])
async def test_negative_receipt_roundtrip_is_exact_and_terminal(proposal, status):
    op, result = proposal
    result = negative(result, status)
    original = deepcopy(result.receipt)
    stored = await p.store_consolidation(op, result)
    assert stored.status == status and stored.memory is None
    assert stored.build_receipt == original
    ledger = (await rows("eval_consolidations"))[0]
    assert json.loads(ledger["build_receipt_json"]) == original
    assert len(ledger["admission_bindings"]) == len(result.group.episodes) == 2
    assert len(await rows("raw_captures")) == 2
    result.receipt["reason"] = "caller changed after storage"
    # Immutable audit survives later deletion; it is not publication authority.
    async with content_client.surreal_content_client() as client:
        await client.execute_query("DELETE raw_captures;")
    assert await p.store_consolidation(op, result) == stored
    assert await p.get_stored_consolidation(op) == stored
    assert stored.build_receipt["usage"]["cost_usd"] is None


async def test_candidate_receipt_survives_candidate_purge(proposal):
    op, result = proposal
    stored = await p.store_consolidation(op, result)
    assert stored.build_receipt == result.receipt
    async with content_client.surreal_content_client() as client:
        await client.execute_query("DELETE raw_captures WHERE uuid=$id;", id=stored.memory.id)
    gone = await p.get_stored_consolidation(op)
    assert gone.status == "gone" and gone.build_receipt == result.receipt


@pytest.mark.parametrize("status", ["abstained", "rejected"])
async def test_negative_receipt_source_race_rolls_back(proposal, monkeypatch, status):
    op, result = proposal
    result = negative(result, status)
    original = content_client.select_many

    async def changing(client, query, **kwargs):
        records = await original(client, query, **kwargs)
        if "FROM eval_attempts" in query:
            await client.execute_query("UPDATE raw_captures SET revision += 1;")
        return records

    monkeypatch.setattr(content_client, "select_many", changing)
    with pytest.raises(p.ConsolidationConflict, match="source changed"):
        await p.store_consolidation(op, result)
    assert await rows("eval_consolidations") == []


async def test_missing_legacy_receipt_stays_unknown_after_upgrade(proposal):
    op, result = proposal
    await p.store_consolidation(op, negative(result, "abstained"))
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE eval_consolidations UNSET build_receipt_json; "
            "REMOVE FIELD build_receipt_json ON eval_consolidations; "
            "UPDATE schema_version SET version=34 WHERE name='content';"
        )
        before = await rows("eval_consolidations")
        await bootstrap_content_schema(client)
        after = await rows("eval_consolidations")
        assert before == after
        await bootstrap_content_schema(client)
        assert await rows("eval_consolidations") == after
    legacy = await p.get_stored_consolidation(op)
    assert legacy.status == "abstained" and legacy.build_receipt is None
    assert (await p.store_consolidation(op, result)).build_receipt is None


@pytest.mark.parametrize("field,value", [("principal_id", "other"), ("organization_id", "other")])
async def test_negative_receipt_cannot_cross_authority(proposal, field, value):
    op, result = proposal
    await p.store_consolidation(op, negative(result, "abstained"))
    changed = replace(op, **{field: value})
    assert await p.get_stored_consolidation(changed) is None


@pytest.mark.parametrize("encoded", ["not-json", "null", "[]", "{}", '{"status":"rejected"}'])
async def test_corrupt_receipt_is_not_legacy_unknown(proposal, encoded):
    op, result = proposal
    await p.store_consolidation(op, negative(result, "abstained"))
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE eval_consolidations SET build_receipt_json=$encoded;", encoded=encoded
        )
    with pytest.raises(p.ConsolidationConflict, match="receipt is invalid"):
        await p.get_stored_consolidation(op)


async def test_invalid_receipt_cannot_create_terminal_record(proposal):
    op, result = proposal
    invalid = negative(result, "abstained")
    invalid.receipt["schema_version"] = "unknown"
    with pytest.raises(p.ConsolidationConflict, match="receipt is invalid"):
        await p.store_consolidation(op, invalid)
    assert await rows("eval_consolidations") == []
