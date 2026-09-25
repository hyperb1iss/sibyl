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
    def __init__(self, responses: list[object], events: list[str] | None = None) -> None:
        self.responses = responses
        self.events = events
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **kwargs: object) -> object:
        if self.events is not None:
            self.events.append("query")
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
async def test_db_llm_budget_enforcer_serializes_budget_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    client = SequenceClient(
        [
            [{"bucket_key": "user:user-z:2026-05", "used_tokens": 0}],
            [{"bucket_key": "org:org-a:2026-05", "used_tokens": 0}],
            [{"bucket_key": "user:user-z:2026-05", "used_tokens": 4}],
            [{"bucket_key": "org:org-a:2026-05", "used_tokens": 4}],
        ],
        events=events,
    )

    @asynccontextmanager
    async def client_scope():
        yield client

    @asynccontextmanager
    async def fake_entity_lock(scope: str, key: str):
        events.append(f"lock:{scope}:{key}")
        try:
            yield "token"
        finally:
            events.append(f"unlock:{scope}:{key}")

    monkeypatch.setattr(llm_budget, "surreal_auth_client_scope", client_scope)
    monkeypatch.setattr(llm_budget, "entity_lock", fake_entity_lock)
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
        LLMBudgetContext(user_id="user-z", organization_id="org-a"),
        surface="memory",
        estimated_tokens=4,
    )

    assert events[:2] == [
        "lock:auth:llm-budget:org:org-a:2026-05",
        "lock:auth:llm-budget:user:user-z:2026-05",
    ]
    assert events[2:6] == ["query", "query", "query", "query"]
    assert events[-2:] == [
        "unlock:auth:llm-budget:user:user-z:2026-05",
        "unlock:auth:llm-budget:org:org-a:2026-05",
    ]


@pytest.mark.asyncio
async def test_db_llm_budget_enforcer_rejects_exceeded_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SequenceClient([[{"bucket_key": "user:user-1:2026-05", "used_tokens": 95}]])

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


def _settle_enforcer(monkeypatch: pytest.MonkeyPatch, client: SequenceClient, events=None):
    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(llm_budget, "surreal_auth_client_scope", client_scope)
    monkeypatch.setattr(llm_budget, "_utcnow", lambda: llm_budget.datetime(2026, 5, 22, 12, 0))
    if events is not None:

        @asynccontextmanager
        async def fake_entity_lock(scope: str, key: str):
            events.append(f"lock:{scope}:{key}")
            try:
                yield "token"
            finally:
                events.append(f"unlock:{scope}:{key}")

        monkeypatch.setattr(llm_budget, "entity_lock", fake_entity_lock)
    return llm_budget.DBLLMBudgetEnforcer(
        FakeSettingsService(
            {llm_budget.USER_BUDGET_SETTING: "1000", llm_budget.ORG_BUDGET_SETTING: "2000"}
        )
    )


@pytest.mark.asyncio
async def test_db_llm_budget_enforcer_settles_each_bucket_down_to_actual_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SequenceClient(
        [
            [{"bucket_key": "user:user-1:2026-05", "used_tokens": 500}],
            [],
            [{"bucket_key": "org:org-1:2026-05", "used_tokens": 700}],
            [],
        ]
    )
    enforcer = _settle_enforcer(monkeypatch, client)

    await enforcer.settle(
        LLMBudgetContext(user_id="user-1", organization_id="org-1"),
        surface="memory",
        reserved_tokens=400,
        actual_tokens=100,
    )

    updates = [params for query, params in client.calls if "UPDATE llm_usage_buckets" in query]
    assert [update["used_tokens"] for update in updates] == [200, 400]
    assert [update["bucket_key"] for update in updates] == [
        "user:user-1:2026-05",
        "org:org-1:2026-05",
    ]


@pytest.mark.asyncio
async def test_db_llm_budget_enforcer_settle_charges_overage_and_floors_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SequenceClient(
        [
            [{"bucket_key": "user:user-1:2026-05", "used_tokens": 100}],
            [],
            [{"bucket_key": "org:org-1:2026-05", "used_tokens": 100}],
            [],
            [{"bucket_key": "user:user-1:2026-05", "used_tokens": 130}],
            [],
            [{"bucket_key": "org:org-1:2026-05", "used_tokens": 130}],
            [],
        ]
    )
    enforcer = _settle_enforcer(monkeypatch, client)
    context = LLMBudgetContext(user_id="user-1", organization_id="org-1")

    await enforcer.settle(context, surface="memory", reserved_tokens=50, actual_tokens=80)
    await enforcer.settle(context, surface="memory", reserved_tokens=400, actual_tokens=0)

    updates = [params for query, params in client.calls if "UPDATE llm_usage_buckets" in query]
    assert [update["used_tokens"] for update in updates] == [130, 130, 0, 0]


@pytest.mark.asyncio
async def test_db_llm_budget_enforcer_settle_is_a_no_op_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SequenceClient([])
    enforcer = _settle_enforcer(monkeypatch, client)

    await enforcer.settle(
        LLMBudgetContext(user_id="user-1", organization_id="org-1"),
        surface="memory",
        reserved_tokens=64,
        actual_tokens=64,
    )

    assert client.calls == []


@pytest.mark.asyncio
async def test_db_llm_budget_enforcer_settle_holds_the_same_locks_as_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    client = SequenceClient(
        [
            [{"bucket_key": "user:user-z:2026-05", "used_tokens": 40}],
            [],
            [{"bucket_key": "org:org-a:2026-05", "used_tokens": 40}],
            [],
        ],
        events=events,
    )
    enforcer = _settle_enforcer(monkeypatch, client, events)

    await enforcer.settle(
        LLMBudgetContext(user_id="user-z", organization_id="org-a"),
        surface="memory",
        reserved_tokens=40,
        actual_tokens=10,
    )

    assert events[:2] == [
        "lock:auth:llm-budget:org:org-a:2026-05",
        "lock:auth:llm-budget:user:user-z:2026-05",
    ]
    assert events[2:6] == ["query", "query", "query", "query"]
    assert events[-2:] == [
        "unlock:auth:llm-budget:user:user-z:2026-05",
        "unlock:auth:llm-budget:org:org-a:2026-05",
    ]


@pytest.mark.asyncio
async def test_an_opus_sized_request_fits_a_default_user_budget_with_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1.6M-character consolidation prompt reserves one attempt, not the retry envelope.

    The envelope used to be three transport attempts times three output attempts,
    so the same request booked 3,894,912 tokens and the default 1,000,000 user
    budget refused any prompt above about 313K characters.
    """
    from sibyl_core.ai.llm.budget import estimate_llm_tokens

    one_attempt = estimate_llm_tokens("x" * 1_600_000, output_token_limit=32_768)
    assert one_attempt == 432_768
    assert one_attempt * 9 == 3_894_912 > llm_budget.DEFAULT_MONTHLY_USER_TOKENS
    assert one_attempt < llm_budget.DEFAULT_MONTHLY_USER_TOKENS

    client = SequenceClient(
        [
            # reserve: no bucket yet, create it, then write the reservation
            [],
            [{"bucket_key": "user:user-1:2026-05", "used_tokens": 0}],
            [],
            # settle: read the reserved bucket, write the actual usage
            [{"bucket_key": "user:user-1:2026-05", "used_tokens": one_attempt}],
            [],
            # the old nine-attempt envelope against an empty bucket is refused
            [{"bucket_key": "user:user-1:2026-05", "used_tokens": 0}],
        ]
    )

    @asynccontextmanager
    async def client_scope():
        yield client

    monkeypatch.setattr(llm_budget, "surreal_auth_client_scope", client_scope)
    monkeypatch.setattr(llm_budget, "_utcnow", lambda: llm_budget.datetime(2026, 5, 22, 12, 0))
    enforcer = llm_budget.DBLLMBudgetEnforcer(FakeSettingsService())
    context = LLMBudgetContext(user_id="user-1")

    await enforcer.reserve(context, surface="memory", estimated_tokens=one_attempt)
    await enforcer.settle(
        context, surface="memory", reserved_tokens=one_attempt, actual_tokens=411_939
    )

    updates = [params for query, params in client.calls if "UPDATE llm_usage_buckets" in query]
    assert [update["used_tokens"] for update in updates] == [one_attempt, 411_939]

    with pytest.raises(LLMBudgetExceededError):
        await llm_budget.DBLLMBudgetEnforcer(FakeSettingsService()).reserve(
            context, surface="memory", estimated_tokens=one_attempt * 9
        )
