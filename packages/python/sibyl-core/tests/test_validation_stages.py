"""A completed automatic stage resumes finalization without repeating extraction."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from sibyl_core.services.validation_execution import ValidationExecution
from sibyl_core.services.validation_stages import run_validation_stage
from sibyl_core.tasks.memory_validation import run_memory_validation
from sibyl_core.tasks.procedure_review import review_digest
from tests.test_eval_publication import content_store as content_store
from tests.test_memory_validation import candidate as candidate
from tests.test_memory_validation import citations as citations
from tests.test_memory_validation import extractor
from tests.test_memory_validation import prepared as prepared
from tests.test_memory_validation import sources as sources


@pytest.mark.parametrize("failure", ["cancel", "source"])
async def test_generic_stage_retains_completed_usage(content_store, prepared, failure):
    request = {
        "org": "org",
        "principal": "owner",
        "parent": "parent",
        "policy": "{}",
        "source_bindings": [],
    }
    execution = ValidationExecution(review_digest(request), "org", "owner")
    error = asyncio.CancelledError() if failure == "cancel" else ValueError("source changed")
    check = AsyncMock(side_effect=[None, error])

    async def extract():
        return await run_memory_validation(prepared, extractor({"findings": []}))

    run = AsyncMock(side_effect=extract)
    kwargs = dict(
        execution=execution,
        parent_id="parent",
        source_ids=[],
        request=request,
        policy="{}",
        check_current=check,
        run=run,
    )
    with pytest.raises(type(error)):
        await run_validation_stage(**kwargs)
    row = await execution.load()
    assert row is not None
    assert row["state"] == ("recorded" if failure == "cancel" else "fenced")
    assert '"requests":1' in row["usage_json"]
    if failure == "cancel":
        kwargs["check_current"] = AsyncMock()
        kwargs["run"] = AsyncMock(side_effect=AssertionError("must not extract again"))
        resumed = await run_validation_stage(**kwargs)
        assert resumed["usage"]["requests"] == 1
        assert resumed["status"] == "no_findings"
        kwargs["run"].assert_not_awaited()
    run.assert_awaited_once()
