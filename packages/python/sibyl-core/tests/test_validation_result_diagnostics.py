from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.services import validation_promotion
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.procedure_review import review_digest


@pytest.mark.parametrize("tamper", [None, "hash", "principal"])
async def test_newer_result_version_diagnosed_only_after_authority_checks(monkeypatch, tamper):
    request = {
        "org": "diagnostic-org",
        "principal": "owner",
        "parent": "candidate",
        "input": "a" * 64,
        "policy": "{}",
    }
    request_json = canonical(request)
    execution_id = review_digest(request)
    result_json = canonical({"version": "future-version", "secret": "capture-do-not-log"})
    binding = validation_promotion.ValidationBinding(
        execution_id=execution_id,
        request_sha256=execution_id,
        input_sha256="a" * 64,
        result_sha256=sha256(result_json.encode()).hexdigest(),
    )
    row = {
        "uuid": execution_id,
        "organization_id": request["org"],
        "principal_id": "owner",
        "parent_id": "candidate",
        "state": "returned",
        "purged": False,
        "request_json": request_json,
        "result_json": result_json,
        "request_sha256": execution_id,
        "policy_json": "{}",
    }
    if tamper == "hash":
        row["result_json"] = "{}"
    elif tamper == "principal":
        row["principal_id"] = "foreign-owner"
    execution = AsyncMock()
    execution.load.return_value = row
    monkeypatch.setattr(validation_promotion, "ValidationExecution", lambda *_args: execution)
    warnings = []
    validation_promotion._report_unreadable_result.cache_clear()
    monkeypatch.setattr(
        validation_promotion.log,
        "warning",
        lambda event, **fields: warnings.append((event, fields)),
    )
    memory = SimpleNamespace(organization_id="diagnostic-org", principal_id="owner", id="candidate")
    for _ in range(2):
        assert not await validation_promotion.validation_binding_current(
            memory, {"validation_binding_json": binding.model_dump_json()}
        )
    execution.result.assert_not_awaited()
    assert "capture-do-not-log" not in str(warnings)
    if tamper:
        assert warnings == []
    else:
        assert len(warnings) == 1
        assert warnings[0][0] == "validation_result_unreadable"
