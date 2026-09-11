"""Progress receipts round-trip without losing assessments or legacy identities."""

import copy
import json

import pytest
from pydantic import TypeAdapter

from sibyl_core.services.validation_result_codec import (
    LegacyValidationStageResult,
    decode_validation_result,
    encode_validation_result,
)
from sibyl_core.tasks.memory_progress import ProgressMemoryValidationResult
from tests.test_eval_publication import admitted_pair as admitted_pair
from tests.test_eval_publication import content_store as content_store
from tests.test_eval_publication import evidence as evidence
from tests.test_eval_publication import proposal as proposal
from tests.test_memory_progress import (
    citations as citations,
)
from tests.test_memory_progress import (
    execute,
    output,
)
from tests.test_memory_progress import (
    prepared as prepared,
)
from tests.test_memory_progress import (
    progress as progress,
)
from tests.test_memory_progress import (
    sources as sources,
)
from tests.test_memory_validation import candidate as candidate
from tests.test_validation_receipts import private_journal as private_journal


async def test_progress_codec_roundtrip(progress):
    result = await execute(progress, output(progress))
    encoded = encode_validation_result(result)
    decoded = decode_validation_result(json.loads(json.dumps(encoded)))
    assert isinstance(decoded, ProgressMemoryValidationResult)
    assert decoded == result
    assert encode_validation_result(decoded) == encoded
    assert encoded["prior_assessments"] and encoded["progress"] == "advance"


@pytest.mark.parametrize("mutation", ["missing", "downgrade", "unknown", "extra"])
async def test_progress_codec_refuses_lossy_downgrade(progress, mutation):
    encoded = encode_validation_result(await execute(progress, output(progress)))
    changed = copy.deepcopy(encoded)
    if mutation == "missing":
        changed.pop("prior_assessments")
    elif mutation == "downgrade":
        changed.pop("version")
    elif mutation == "unknown":
        changed["version"] = "future-progress"
    else:
        changed["unexpected"] = True
    with pytest.raises(ValueError):
        decode_validation_result(changed)


async def test_progress_codec_preserves_legacy(prepared):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from sibyl_core.ai.llm.extractor import Extractor
    from sibyl_core.tasks.memory_validation import CriticOutput, run_memory_validation

    result = await run_memory_validation(
        prepared,
        Extractor(
            CriticOutput,
            agent=Agent(TestModel(custom_output_args={"findings": []}), output_type=CriticOutput),
        ),
    )
    expected = TypeAdapter(LegacyValidationStageResult).dump_python(result, mode="json")
    assert encode_validation_result(result) == expected
    assert encode_validation_result(decode_validation_result(expected)) == expected


@pytest.fixture
async def historical(progress):
    from sibyl_core.services.validation_progress_history import bind_progress_history
    from sibyl_core.tasks._evidence_json import canonical
    from sibyl_core.tasks.memory_validation import MemoryValidationResult
    from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest

    context = json.loads(progress.payload_json)["prior_progress"]
    current = await execute(progress, output(progress))
    prior = MemoryValidationResult(
        status="reconsider",
        submission=ReviewSubmission.model_validate(context["review"]),
        reason=None,
        input_sha256=context["previous_input_sha256"],
        schema_sha256="a" * 64,
        usage=current.usage,
        configured_policy_json="{}",
    )
    request = {
        "org": "org",
        "principal": "owner",
        "parent": "parent",
        "policy": "{}",
        "input": prior.input_sha256,
        "source_bindings": [{"source_id": "parent", "incarnation": "initial", "generation": 1}],
    }
    row = {
        "uuid": review_digest(request),
        "request_sha256": review_digest(request),
        "organization_id": "org",
        "principal_id": "owner",
        "parent_id": "parent",
        "policy_json": "{}",
        "request_json": canonical(request),
        "result_json": canonical(encode_validation_result(prior)),
        "state": "returned",
        "purged": False,
    }
    from sibyl_core.tasks.memory_validation import VALIDATION_VERSION, PreparedMemoryValidation

    before = json.loads(progress.payload_json)
    before.pop("prior_progress")
    before.update(
        version=VALIDATION_VERSION,
        candidate=context["previous_candidate"],
        assertions=context["previous_assertions"],
        candidate_view_sha256=context["previous_candidate_view_sha256"],
        parent_operation_id=context["previous_parent_operation_id"],
        parent_candidate_sha256=context["previous_parent_candidate_sha256"],
        assertion_hashes={k: review_digest(v) for k, v in context["previous_assertions"].items()},
    )
    previous = PreparedMemoryValidation(canonical(before))
    return (
        row,
        bind_progress_history(progress, row, "org", "owner", previous=previous),
        current,
        previous,
    )


@pytest.mark.parametrize("change", ["input", "request", "result", "owner", "purged", "state"])
async def test_progress_history_exact_prior_binding(historical, progress, change):
    from sibyl_core.services.validation_progress_history import bind_progress_history

    row, binding, _, previous = historical
    row = copy.deepcopy(row)
    if change == "input":
        payload = json.loads(progress.payload_json)
        payload["prior_progress"]["previous_input_sha256"] = "1" * 64
        from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

        progress = PreparedMemoryValidation(json.dumps(payload))
    elif change in ("request", "result"):
        row[change + "_json"] += " "
    elif change == "owner":
        row["principal_id"] = "other"
    elif change == "purged":
        row["purged"] = True
    else:
        row["state"] = "recorded"
    with pytest.raises(ValueError):
        if change == "input":
            bind_progress_history(progress, row, "org", "owner", previous=previous)
        else:
            from sibyl_core.services.validation_progress_history import validate_history_row

            validate_history_row(row, binding, "org", "owner")


@pytest.mark.parametrize("mode", ["recorded", "journal"])
async def test_progress_durable_recorded_resume(
    historical, content_store, monkeypatch, private_journal, mode
):
    from sibyl_core.services import validation_execution as owner
    from sibyl_core.services.validation_execution import ValidationExecution
    from sibyl_core.services.validation_stages import run_validation_stage
    from sibyl_core.tasks._evidence_json import canonical
    from sibyl_core.tasks.procedure_review import review_digest

    prior, binding, result, _ = historical
    await owner._query(
        "CREATE memory_validation_executions CONTENT $row;",
        row={
            **prior,
            "source_ids": ["parent"],
            "claim_id": "prior",
            "usage_json": canonical(json.loads(prior["result_json"])["usage"]),
        },
    )
    request = {
        "org": "org",
        "principal": "owner",
        "parent": "child",
        "policy": "{}",
        "input": result.input_sha256,
        "source_bindings": [{"source_id": "child", "incarnation": "initial", "generation": 1}],
        "progress_history": binding.model_dump(mode="json"),
    }
    execution = ValidationExecution(review_digest(request), "org", "owner")
    assert await execution.begin(
        parent_id="child", source_ids=["child"], policy="{}", request=request
    )
    if mode == "journal":

        async def unavailable(*args, **kwargs):
            raise OSError("simulated database outage")

        monkeypatch.setattr(execution, "_finish", unavailable)
        with pytest.raises(OSError):
            await execution.record_result(result)
        assert list(private_journal.glob("*.receipt"))
        execution = ValidationExecution(review_digest(request), "org", "owner")
    else:
        await execution.record_result(result)

    async def current():
        pass

    async def forbidden():
        raise AssertionError("completed stage must not dispatch")

    resumed = await run_validation_stage(
        execution=execution,
        parent_id="child",
        source_ids=["child"],
        request=request,
        policy="{}",
        check_current=current,
        run=forbidden,
    )
    assert resumed["prior_assessments"] == encode_validation_result(result)["prior_assessments"]
    assert resumed["progress"] == "advance"
    assert not list(private_journal.glob("*.receipt"))


async def test_progress_archive_history_and_exact_roundtrip(historical):
    from sibyl_core.migrate.validation_receipt_archive import capture, prepare
    from sibyl_core.tasks._evidence_json import canonical
    from sibyl_core.tasks.procedure_review import review_digest

    prior, binding, result, _ = historical
    prior = {
        **prior,
        "source_ids": ["parent"],
        "usage_json": canonical(json.loads(prior["result_json"])["usage"]),
    }
    request = {
        "org": "org",
        "principal": "owner",
        "parent": "child",
        "policy": "{}",
        "input": result.input_sha256,
        "source_bindings": [{"source_id": "child", "incarnation": "initial", "generation": 1}],
        "progress_history": binding.model_dump(mode="json"),
    }
    child = {
        "uuid": review_digest(request),
        "request_sha256": review_digest(request),
        "organization_id": "org",
        "principal_id": "owner",
        "parent_id": "child",
        "source_ids": ["child"],
        "policy_json": "{}",
        "request_json": canonical(request),
        "result_json": canonical(encode_validation_result(result)),
        "usage_json": canonical(result.usage.model_dump(mode="json")),
        "state": "returned",
        "purged": False,
    }
    # Input ordering must not require the prior row to precede the child.
    section = capture([child, prior])
    assert prepare(section, [child, prior]) == []
    for mutation in ("missing", "purged", "changed"):
        broken = copy.deepcopy(prior)
        if mutation == "purged":
            broken.update(purged=True, result_json=None)
        elif mutation == "changed":
            broken["result_json"] += " "
        with pytest.raises(ValueError):
            prepare(section, [child] if mutation == "missing" else [child, broken])


async def test_progress_history_native_conflict(
    historical, content_store, private_journal, monkeypatch
):
    import asyncio
    import os

    if not os.environ.get("SIBYL_OPERATIONAL_TEST_URL"):
        pytest.skip("Requires the isolated native SDK database")
    from sibyl_core.services import validation_execution as owner
    from sibyl_core.services.validation_execution import ValidationExecution
    from sibyl_core.tasks.procedure_review import review_digest

    prior, binding, result, _ = historical
    await owner._query(
        "CREATE memory_validation_executions CONTENT $row;",
        row={**prior, "source_ids": ["parent"], "claim_id": "prior", "usage_json": "{}"},
    )
    request = {
        "org": "org",
        "principal": "owner",
        "parent": "child",
        "policy": "{}",
        "input": result.input_sha256,
        "source_bindings": [],
        "progress_history": binding.model_dump(mode="json"),
    }
    execution = ValidationExecution(review_digest(request), "org", "owner")
    assert await execution.begin(
        parent_id="child", source_ids=["child"], policy="{}", request=request
    )
    original_query = owner._query
    entered = asyncio.Event()

    async def delayed(query, **params):
        if "CREATE memory_validation_attempts" in query:
            query = query.replace(
                "UPDATE memory_validation_executions",
                "SLEEP 1s; UPDATE memory_validation_executions",
                1,
            )
            entered.set()
        return await original_query(query, **params)

    monkeypatch.setattr(owner, "_query", delayed)
    dispatch = asyncio.create_task(execution.before_dispatch())
    await entered.wait()
    await asyncio.sleep(0.3)
    await original_query(
        "UPDATE memory_validation_executions SET purged=true WHERE uuid=$uuid;", uuid=prior["uuid"]
    )
    assert not dispatch.done()
    with pytest.raises(Exception) as failure:
        await dispatch
    assert (
        "conflict" in str(failure.value).lower()
        or "prior receipt changed" in str(failure.value).lower()
    )
    assert not await original_query("SELECT * FROM memory_validation_attempts;")
    assert (
        await original_query(
            "SELECT * FROM memory_validation_executions WHERE uuid=$uuid;", uuid=prior["uuid"]
        )
    )[0]["purged"] is True


async def test_progress_codec_legacy_signed_tuple_roundtrip(proposal):
    from sibyl_core.ai.llm.extractor import ExtractionUsage
    from sibyl_core.tasks.procedure_correction_result import ProcedureCorrectionResult

    _, consolidation = proposal
    result = ProcedureCorrectionResult(
        status="procedure_correction",
        parent_operation_id="a" * 64,
        parent_candidate_sha256="b" * 64,
        review_execution_id="c" * 64,
        result=consolidation,
        usage=ExtractionUsage(requests=0, input_tokens=0, output_tokens=0, total_tokens=0),
    )
    historical = TypeAdapter(LegacyValidationStageResult).dump_python(result, mode="json")
    assert isinstance(historical["result"]["group"]["episodes"], list)
    assert encode_validation_result(result) == historical
    decoded = decode_validation_result(historical)
    assert isinstance(decoded, ProcedureCorrectionResult)
    assert isinstance(decoded.result.group.episodes, tuple)
    assert encode_validation_result(decoded) == historical
