from __future__ import annotations

import pytest

from sibyl_core.ai.errors import LLMBudgetExceededError
from sibyl_core.ai.llm.budget import (
    LLMBudgetContext,
    estimate_llm_tokens,
    get_llm_budget_context,
    llm_budget_context,
    reserve_llm_budget,
    set_budget_enforcer,
)


class RecordingEnforcer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[LLMBudgetContext, str, int]] = []

    async def reserve(
        self,
        context: LLMBudgetContext,
        *,
        surface: str,
        estimated_tokens: int,
    ) -> None:
        self.calls.append((context, surface, estimated_tokens))
        if self.fail:
            raise LLMBudgetExceededError("budget exceeded", surface=surface)


@pytest.fixture(autouse=True)
def reset_budget_enforcer() -> None:
    set_budget_enforcer(None)


def test_estimate_llm_tokens_includes_output_limit() -> None:
    assert estimate_llm_tokens("abcd" * 10, output_token_limit=7) == 17


@pytest.mark.asyncio
async def test_reserve_llm_budget_delegates_with_context() -> None:
    enforcer = RecordingEnforcer()
    set_budget_enforcer(enforcer)

    with llm_budget_context(user_id="user-1", organization_id="org-1"):
        reserved = await reserve_llm_budget(
            surface="memory",
            prompt="abcd" * 4,
            output_token_limit=3,
        )

    assert reserved == 7
    assert len(enforcer.calls) == 1
    context, surface, tokens = enforcer.calls[0]
    assert context.user_id == "user-1"
    assert context.organization_id == "org-1"
    assert surface == "memory"
    assert tokens == 7
    assert get_llm_budget_context() is None


@pytest.mark.asyncio
async def test_reserve_llm_budget_skips_without_context() -> None:
    enforcer = RecordingEnforcer()
    set_budget_enforcer(enforcer)

    reserved = await reserve_llm_budget(surface="memory", prompt="abcd")

    assert reserved == 1
    assert enforcer.calls == []
