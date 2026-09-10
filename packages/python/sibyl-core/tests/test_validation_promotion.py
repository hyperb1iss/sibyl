"""A durable critic stage must survive every promotion and recall boundary."""

import json
from unittest.mock import AsyncMock

import pytest

from sibyl_core.services import content_client, memory_reflection
from sibyl_core.services.content_raw_recall import recall_raw_memory
from sibyl_core.services.procedure_validation import validate_stored_procedure
from sibyl_core.services.validation_promotion import promote_validated_procedure
from tests.test_eval_publication import admitted_pair as admitted_pair
from tests.test_eval_publication import content_store as content_store
from tests.test_eval_publication import evidence as evidence
from tests.test_eval_publication import proposal as proposal
from tests.test_eval_publication import rows
from tests.test_eval_publication_promotion import runtime as runtime
from tests.test_validation_execution import candidate as candidate


async def mutate(query, **params):
    async with content_client.surreal_content_client() as client:
        return await client.execute_query(query, **params)


@pytest.fixture
async def approved(candidate):
    result = await validate_stored_procedure(
        organization_id="org", principal_id="owner", parent_id=candidate.id, authorize=AsyncMock()
    )
    return dict(
        organization_id="org",
        principal_id="owner",
        candidate_id=candidate.id,
        execution_id=result["execution_id"],
        authorize=AsyncMock(),
    )


async def test_validated_promotion_replays_and_recalls(approved, candidate, runtime):
    first = await promote_validated_procedure(**approved)
    assert first.success, first
    associations = await rows("memory_derivations")
    binding = next(x for x in associations if x["target_id"] == candidate.id)[
        "validation_binding_json"
    ]
    assert json.loads(binding)["execution_id"] == approved["execution_id"]
    assert candidate.id in {
        m.id
        for m in await recall_raw_memory(
            organization_id="org", principal_id="owner", query="Check the actual output"
        )
    }
    replay = await promote_validated_procedure(**approved)
    assert replay.success and replay.promoted_id == first.promoted_id
    assert len(await rows("memory_validation_executions")) == 1


@pytest.mark.parametrize("mutation", ["purged", "missing", "result", "owner"])
async def test_invalid_stored_validation_cannot_promote(approved, runtime, mutation):
    if mutation == "missing":
        await mutate("DELETE memory_validation_executions;")
    elif mutation == "purged":
        await mutate("UPDATE memory_validation_executions SET purged=true;")
    elif mutation == "result":
        await mutate("UPDATE memory_validation_executions SET result_json='{}';")
    else:
        approved["principal_id"] = "other"
    with pytest.raises(ValueError):
        await promote_validated_procedure(**approved)
    assert not await rows("memory_derivations")


@pytest.mark.parametrize("mutation", ["purged", "missing", "result"])
async def test_promotion_validation_loss_denies_recall(approved, candidate, runtime, mutation):
    result = await promote_validated_procedure(**approved)
    assert result.success, result
    if mutation == "missing":
        await mutate("DELETE memory_validation_executions;")
    elif mutation == "purged":
        await mutate("UPDATE memory_validation_executions SET purged=true;")
    else:
        await mutate("UPDATE memory_validation_executions SET result_json='{}';")
    assert candidate.id not in {
        m.id
        for m in await recall_raw_memory(
            organization_id="org", principal_id="owner", query="Check the actual output"
        )
    }
    from sibyl_core.services.eval_publication_guards import unavailable_publication_ids

    entity = await runtime.entity_manager.get(result.promoted_id)
    assert result.promoted_id in await unavailable_publication_ids(
        "org", {result.promoted_id: entity.metadata}
    )


async def test_stage_change_at_final_cas_cannot_publish(approved, candidate, runtime, monkeypatch):
    original = memory_reflection.save_raw_memory

    async def changed(memory, **kwargs):
        if memory.review_state == "promoted":
            await mutate("UPDATE memory_validation_executions SET result_json='{}';")
        return await original(memory, **kwargs)

    monkeypatch.setattr(memory_reflection, "save_raw_memory", changed)
    try:
        result = await promote_validated_procedure(**approved)
    except ValueError:
        pass
    else:
        assert not result.success
    assert candidate.id not in {
        m.id
        for m in await recall_raw_memory(
            organization_id="org", principal_id="owner", query="Check the actual output"
        )
    }


async def test_validation_binding_cannot_be_removed(approved, candidate, runtime):
    assert (await promote_validated_procedure(**approved)).success
    with pytest.raises(Exception, match="validation binding is immutable"):
        await mutate(
            "UPDATE memory_derivations SET validation_binding_json=NONE WHERE target_id=$id;",
            id=candidate.id,
        )


async def test_validation_stage_changes_inside_final_query(
    approved, candidate, runtime, monkeypatch
):
    original = content_client.select_many_raw
    injected = []

    async def changed(client, query, **params):
        if params.get("record", {}).get("review_state") == "promoted" and params.get(
            "validation_binding"
        ):
            await client.execute_query("UPDATE memory_validation_executions SET result_json='{}';")
            injected.append(True)
        return await original(client, query, **params)

    monkeypatch.setattr(content_client, "select_many_raw", changed)
    result = await promote_validated_procedure(**approved)
    assert injected == [True]
    assert not result.success
    assert candidate.id not in {
        m.id
        for m in await recall_raw_memory(
            organization_id="org", principal_id="owner", query="Check the actual output"
        )
    }
    assert (await rows("memory_validation_executions"))[0]["usage_json"]


async def test_source_retired_after_graph_write_stays_unrecalled(
    approved, candidate, runtime, monkeypatch
):
    original = runtime.entity_manager.create_direct_if_absent

    async def changed(entity, **kwargs):
        result = await original(entity, **kwargs)
        await mutate(
            "UPDATE raw_captures SET deleted_at=time::now() WHERE uuid!=$candidate;",
            candidate=candidate.id,
        )
        return result

    monkeypatch.setattr(runtime.entity_manager, "create_direct_if_absent", changed)
    try:
        result = await promote_validated_procedure(**approved)
    except ValueError:
        pass
    else:
        assert not result.success
    assert candidate.id not in {
        m.id
        for m in await recall_raw_memory(
            organization_id="org", principal_id="owner", query="Check the actual output"
        )
    }


async def test_validation_binding_survives_source_archive_clean(approved, candidate, runtime):
    from sibyl_core.memory_pipeline.observations import SourceKind
    from sibyl_core.services.source_archive_store import (
        export_source_integrity,
        restore_source_integrity,
    )

    assert (await promote_validated_procedure(**approved)).success
    async with content_client.surreal_content_client() as client:
        payload = await export_source_integrity(
            client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=["org"]
        )
        before = next(
            x for x in await rows("memory_derivations") if x["target_id"] == candidate.id
        )["validation_binding_json"]
        await client.execute_query(
            "UPDATE memory_validation_executions SET purged=true,result_json=NONE;"
        )
        await restore_source_integrity(
            client.execute_query,
            payload,
            kind=SourceKind.RAW_CAPTURE,
            organizations=["org"],
            clean=True,
        )
        after = next(x for x in await rows("memory_derivations") if x["target_id"] == candidate.id)[
            "validation_binding_json"
        ]
    assert before == after
    assert candidate.id not in {
        m.id
        for m in await recall_raw_memory(
            organization_id="org", principal_id="owner", query="Check the actual output"
        )
    }


async def test_actual_automatic_result_promotes_without_reextracting(candidate, runtime):
    from sibyl_core.services.automatic_procedure import automatically_reconsider_procedure

    auth = AsyncMock()
    checked = await automatically_reconsider_procedure(
        organization_id="org", principal_id="owner", parent_id=candidate.id, authorize=auth
    )
    assert checked.status == "validated"
    before = await rows("memory_validation_executions")
    promoted = await promote_validated_procedure(
        organization_id="org",
        principal_id="owner",
        candidate_id=checked.candidate_id,
        execution_id=checked.executions[-1],
        authorize=auth,
    )
    assert promoted.success
    after = await rows("memory_validation_executions")
    assert len(after) == len(before) == 1
    assert after[0]["result_json"] == before[0]["result_json"]
    assert after[0]["usage_json"] == before[0]["usage_json"]
    assert candidate.id in {
        m.id
        for m in await recall_raw_memory(
            organization_id="org", principal_id="owner", query="Check the actual output"
        )
    }
