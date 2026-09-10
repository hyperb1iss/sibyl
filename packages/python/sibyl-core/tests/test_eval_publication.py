"""A permanent operation cannot resurrect a deleted consolidation candidate."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sibyl_core.services import content_client, eval_consolidation
from sibyl_core.services import eval_publication as p
from sibyl_core.tasks import consolidation as c
from tests.test_eval_consolidation import admitted_pair as admitted_pair
from tests.test_eval_receipts import evidence as evidence
from tests.test_reflection_identity import content_store as content_store


@pytest.fixture
async def proposal(admitted_pair, monkeypatch):
    params, _ = admitted_pair
    group = await eval_consolidation.load_admitted_consolidation_group(**params)

    def assertion(index):
        source = group.episodes[index]
        return c.ConditionalAssertion(
            statement="Check the actual output",
            label="inferred",
            support=[
                c.SupportRef(
                    episode_id=source.episode_id, start_byte=0, end_byte=len(source.artifact)
                )
            ],
        )

    draft = c.DraftConditionalProcedure(
        goal=assertion(0),
        environment=[assertion(0)],
        preconditions=[assertion(0)],
        actions=[c.ConditionalAction(order=1, action=assertion(0), success_criteria=assertion(0))],
        expected_result=assertion(0),
        failure_modes=[assertion(1)],
        abstain_when=[assertion(1)],
    )

    async def extract(_self, _prompt):
        return SimpleNamespace(
            output=c.ProcedureProposal(procedure=draft),
            usage=SimpleNamespace(model_dump=lambda **_: {}),
        )

    monkeypatch.setattr(c.Extractor, "extract_with_usage", extract)
    result = await c.propose_conditional_procedure(group)
    op = p.ConsolidationOperation(
        organization_id="org",
        principal_id="owner",
        experiment_id=params["experiment_id"],
        experiment_revision=params["experiment_revision"],
        arm_id="raw",
        checkpoint=0,
        group_id="contrast",
        attempt_ids=params["attempt_ids"],
        mechanism=params["mechanism"],
        controller_policy_sha256=params["expected_controller_policy_sha256"],
        extractor_revision="test-protocol-model-config-v1",
    )
    return op, result


async def rows(table):
    async with content_client.surreal_content_client() as client:
        return content_client.normalize_records(
            await client.execute_query(f"SELECT * FROM {table};")
        )


async def test_concurrent_retry_keeps_one_original_candidate(proposal):
    op, result = proposal
    assert await p.get_stored_consolidation(op) is None
    first, second = await asyncio.gather(
        p.store_consolidation(op, result), p.store_consolidation(op, result)
    )
    assert first == second
    assert first.memory.review_state == "pending"
    from sibyl_core.services.content_models import raw_memory_recallable

    assert not raw_memory_recallable(first.memory)
    assert first.memory.metadata["source_bindings"] == {
        e.stored_sources[0].source_id: e.stored_sources[0].observed_revision
        for e in result.group.episodes
    }
    assert len(await rows("eval_consolidations")) == 1
    assert len(await rows("raw_captures")) == 3
    assert await p.get_stored_consolidation(op) == first
    assert "raw_content" not in (await rows("eval_consolidations"))[0]


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE raw_captures SET deleted_at = time::now() WHERE uuid = $id;",
        "DELETE raw_captures WHERE uuid = $id;",
    ],
)
async def test_purged_candidate_is_terminal(proposal, mutation):
    op, result = proposal
    stored = await p.store_consolidation(op, result)
    async with content_client.surreal_content_client() as client:
        await client.execute_query(mutation, id=stored.memory.id)
    assert (await p.store_consolidation(op, result)).status == "gone"
    assert (await p.get_stored_consolidation(op)).status == "gone"
    assert len(await rows("eval_consolidations")) == 1


async def test_changed_input_conflicts_without_new_candidate(proposal):
    op, result = proposal
    await p.store_consolidation(op, result)
    changed = replace(op, extractor_revision="different-model")
    assert changed.key == op.key
    with pytest.raises(p.ConsolidationConflict):
        await p.get_stored_consolidation(changed)
    with pytest.raises(p.ConsolidationConflict):
        await p.store_consolidation(changed, result)


async def test_changed_source_aborts_before_writes(proposal):
    op, result = proposal
    async with content_client.surreal_content_client() as client:
        await client.execute_query("UPDATE raw_captures SET revision += 1;")
    with pytest.raises(p.ConsolidationConflict, match="source changed"):
        await p.store_consolidation(op, result)
    assert await rows("eval_consolidations") == []
    assert len(await rows("raw_captures")) == 2


async def test_late_failure_rolls_back_both_rows(proposal, monkeypatch):
    op, result = proposal
    monkeypatch.setattr(
        p,
        "_STORE",
        p._STORE.replace("RETURN { ledger: $stored", "THROW 'injected'; RETURN { ledger: $stored"),
    )
    with pytest.raises(Exception, match="injected"):
        await p.store_consolidation(op, result)
    assert await rows("eval_consolidations") == []
    assert len(await rows("raw_captures")) == 2


async def test_abstention_is_terminal_and_contains_no_source_text(proposal):
    op, result = proposal
    abstention = replace(
        result,
        candidate=None,
        receipt=result.receipt | {"status": "abstained", "reason": "insufficient support"},
        proposal=c.ProcedureProposal(abstention_reason="insufficient support"),
    )
    assert (await p.store_consolidation(op, abstention)).status == "abstained"
    assert (await p.store_consolidation(op, result)).status == "abstained"
    assert len(await rows("raw_captures")) == 2


async def test_caller_cannot_forge_consolidation_stamp(content_store):
    from sibyl_core.services.content_raw_persistence import remember_raw_memory

    memory = await remember_raw_memory(
        organization_id="org",
        principal_id="owner",
        source_id="manual",
        raw_content="Ordinary capture",
        metadata={p.CONSOLIDATION_METADATA_KEY: "forged"},
        embedding_provider=None,
    )
    assert p.CONSOLIDATION_METADATA_KEY not in memory.metadata


@pytest.mark.parametrize(
    "change", ["review_state = 'archived'", "metadata.source_validation_pending = true"]
)
async def test_unversioned_lifecycle_change_blocks_storage(proposal, change):
    op, result = proposal
    async with content_client.surreal_content_client() as client:
        await client.execute_query("UPDATE raw_captures SET " + change + ";")
    with pytest.raises(p.ConsolidationConflict, match="source changed"):
        await p.store_consolidation(op, result)
    assert await rows("eval_consolidations") == []


async def test_lifecycle_change_between_check_and_transaction_blocks(proposal, monkeypatch):
    op, result = proposal
    async with content_client.surreal_content_client() as client:
        original = client.execute_query

        async def changed(query, **params):
            if query == p._STORE:
                await original("UPDATE raw_captures SET review_state = 'archived';")
            return await original(query, **params)

        monkeypatch.setattr(client, "execute_query", changed)
        with pytest.raises(p.ConsolidationConflict, match="source changed"):
            await p.store_consolidation(op, result)
    assert await rows("eval_consolidations") == []


async def test_review_candidate_stays_excluded_after_source_validation(proposal):
    from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
    from sibyl_core.services.content_models import raw_memory_recallable

    op, result = proposal
    memory = (await p.store_consolidation(op, result)).memory
    checked = replace(memory, metadata={**memory.metadata, "source_validation_pending": False})
    assert not raw_memory_recallable(checked)
    assert not graph_metadata_recallable(
        {p.CONSOLIDATION_METADATA_KEY: op.key, "review_state": "pending"}
    )
    ordinary = replace(checked, metadata={})
    assert raw_memory_recallable(ordinary)


@pytest.mark.parametrize("field", ["receipt_sha256", "assignment_sha256", "admission_id"])
@pytest.mark.parametrize("during_transaction", [False, True])
async def test_original_admission_stamp_is_immutable(
    proposal, monkeypatch, field, during_transaction
):
    op, result = proposal
    query = "UPDATE raw_captures SET metadata.eval_admission." + field + " = 'changed';"
    async with content_client.surreal_content_client() as client:
        original = client.execute_query
        if during_transaction:

            async def changed(sql, **params):
                if sql == p._STORE:
                    await original(query)
                return await original(sql, **params)

            monkeypatch.setattr(client, "execute_query", changed)
        else:
            await original(query)
        with pytest.raises(p.ConsolidationConflict):
            await p.store_consolidation(op, result)
    assert await rows("eval_consolidations") == []


@pytest.mark.parametrize(
    "mutation",
    [
        "assignment_json = '{}'",
        "assignment_sha256 = 'changed'",
        "receipt_base64 = 'Y2hhbmdlZA=='",
        "receipt_sha256 = 'changed'",
        "outcome_sha256 = 'changed'",
        "transcript_sha256 = 'changed'",
        "episode_sha256 = 'changed'",
        "capture_id = 'changed'",
        "admitted_at = NONE",
    ],
)
@pytest.mark.parametrize("during_transaction", [False, True])
async def test_original_admission_ledger_is_immutable(
    proposal, monkeypatch, mutation, during_transaction
):
    op, result = proposal
    query = "UPDATE eval_attempts SET " + mutation + ";"
    async with content_client.surreal_content_client() as client:
        original = client.execute_query
        if during_transaction:

            async def changed(sql, **params):
                if sql == p._STORE:
                    await original(query)
                return await original(sql, **params)

            monkeypatch.setattr(client, "execute_query", changed)
        else:
            await original(query)
        with pytest.raises(p.ConsolidationConflict):
            await p.store_consolidation(op, result)
    assert await rows("eval_consolidations") == []
    assert len(await rows("raw_captures")) == 2


async def test_frozen_input_and_output_budgets_reach_actual_proposal(proposal, monkeypatch):
    from sibyl_core.ai.llm.config import EnvConfigSource

    op, result = proposal
    environment = {"SIBYL_LLM_MEMORY_MODEL": "test-model", "SIBYL_LLM_MEMORY_MAX_TOKENS": "3072"}
    monkeypatch.setattr(p, "resolve_llm_config", EnvConfigSource(environment).resolve)
    monkeypatch.setattr(p.core_config, "consolidation_max_input_chars", 120_000)
    model, revision = await p.consolidation_extractor_configuration()
    calls = []

    async def propose(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(proposal=result)

    monkeypatch.setattr(eval_consolidation, "propose_admitted_procedure", propose)
    stored = await p.consolidate_admitted_procedure(
        replace(op, extractor_revision=revision),
        trusted_issuer_id="oracle-1",
        trusted_public_key=None,
        model_override=model,
    )
    assert stored.memory is not None
    assert calls[0]["max_input_chars"] == 120_000
    assert calls[0]["max_tokens"] == 3072
    assert calls[0]["output_mode"] == p.core_config.consolidation_output_mode
    assert calls[0]["openrouter_provider"] == p.core_config.consolidation_openrouter_provider
    monkeypatch.setattr(p.core_config, "consolidation_max_input_chars", 120_001)
    assert (await p.consolidation_extractor_configuration())[1] != revision
    monkeypatch.setattr(p.core_config, "consolidation_max_input_chars", 120_000)
    environment["SIBYL_LLM_MEMORY_MAX_TOKENS"] = "3073"
    assert (await p.consolidation_extractor_configuration())[1] != revision


async def test_output_validation_policy_changes_extractor_revision(monkeypatch):
    from sibyl_core.ai.llm.config import EnvConfigSource

    monkeypatch.setattr(p, "resolve_llm_config", EnvConfigSource({}).resolve)
    assert p.OUTPUT_RETRIES == 2
    current = await p.consolidation_extractor_configuration()
    monkeypatch.setattr(p, "OUTPUT_RETRIES", 0)
    assert await p.consolidation_extractor_configuration() != current


async def test_native_mode_and_endpoint_change_extractor_revision(monkeypatch):
    from sibyl_core.ai.llm.config import EnvConfigSource

    monkeypatch.setattr(p, "resolve_llm_config", EnvConfigSource({}).resolve)
    original = await p.consolidation_extractor_configuration()
    monkeypatch.setattr(p.core_config, "consolidation_output_mode", "native_strict")
    native = await p.consolidation_extractor_configuration()
    assert native != original
    monkeypatch.setattr(p.core_config, "consolidation_openrouter_provider", "parasail/bf16")
    routed = await p.consolidation_extractor_configuration()
    assert routed not in (original, native)


async def test_evidence_semantic_validation_changes_extractor_revision(monkeypatch):
    from sibyl_core.ai.llm.config import EnvConfigSource

    monkeypatch.setattr(p, "resolve_llm_config", EnvConfigSource({}).resolve)
    current = await p.consolidation_extractor_configuration()
    monkeypatch.setattr(p, "EVIDENCE_PROPOSAL_VERSION", "old-validation")
    assert await p.consolidation_extractor_configuration() != current


async def test_retrospective_framing_changes_extractor_revision(monkeypatch):
    from sibyl_core.ai.llm.config import EnvConfigSource
    from sibyl_core.tasks.consolidation import RETROSPECTIVE_REQUEST

    monkeypatch.setattr(p, "resolve_llm_config", EnvConfigSource({}).resolve)
    current = await p.consolidation_extractor_configuration()
    assert RETROSPECTIVE_REQUEST in p.SYSTEM_PROMPT
    monkeypatch.setattr(p, "SYSTEM_PROMPT", p.SYSTEM_PROMPT.replace(RETROSPECTIVE_REQUEST, ""))
    assert await p.consolidation_extractor_configuration() != current
