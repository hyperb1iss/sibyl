from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from sibyl.ai.llm import budget as llm_budget
from sibyl_core.ai.errors import LLMBudgetExceededError
from sibyl_core.ai.llm.budget import LLMBudgetContext


class FakeSettingsService:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


class SequenceClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        if not self.responses:
            raise AssertionError("unexpected query")
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_db_llm_budget_enforcer_reserves_user_and_org_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SequenceClient(
        [
            [],
            [{"bucket_key": "user:user-1:2026-05", "used_tokens": 0}],
            [],
            [{"bucket_key": "org:org-1:2026-05", "used_tokens": 10}],
            [{"bucket_key": "user:user-1:2026-05", "used_tokens": 8}],
            [{"bucket_key": "org:org-1:2026-05", "used_tokens": 18}],
        ]
    )

    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(llm_budget, "surreal_auth_client_scope", client_scope)
    monkeypatch.setattr(
        llm_budget,
        "_utcnow",
        lambda: llm_budget.datetime(2026, 5, 22, 12, 0),
    )
    enforcer = llm_budget.DBLLMBudgetEnforcer(
        FakeSettingsService(
            {
                llm_budget.USER_BUDGET_SETTING: "100",
                llm_budget.ORG_BUDGET_SETTING: "200",
            }
        )
    )

    await enforcer.reserve(
        LLMBudgetContext(user_id="user-1", organization_id="org-1"),
        surface="memory",
        estimated_tokens=8,
    )

    queries = [query for query, _params in client.calls]
    assert queries.count("CREATE llm_usage_buckets CONTENT $record;") == 2
    assert sum("UPDATE llm_usage_buckets" in query for query in queries) == 2
    assert client.calls[-2][1]["used_tokens"] == 8
    assert client.calls[-1][1]["used_tokens"] == 18


@pytest.mark.asyncio
async def test_db_llm_budget_enforcer_rejects_exceeded_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SequenceClient(
        [[{"bucket_key": "user:user-1:2026-05", "used_tokens": 95}]]
    )

    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(llm_budget, "surreal_auth_client_scope", client_scope)
    monkeypatch.setattr(
        llm_budget,
        "_utcnow",
        lambda: llm_budget.datetime(2026, 5, 22, 12, 0),
    )
    enforcer = llm_budget.DBLLMBudgetEnforcer(
        FakeSettingsService(
            {
                llm_budget.USER_BUDGET_SETTING: "100",
            }
        )
    )

    with pytest.raises(LLMBudgetExceededError) as exc_info:
        await enforcer.reserve(
            LLMBudgetContext(user_id="user-1"),
            surface="memory",
            estimated_tokens=8,
        )

    assert exc_info.value.details["monthly_limit"] == 100
    assert exc_info.value.details["used_tokens"] == 95
    assert len(client.calls) == 1
