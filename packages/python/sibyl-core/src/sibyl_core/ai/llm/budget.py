"""LLM budget context and reservation hooks."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LLMBudgetContext:
    user_id: str | None = None
    organization_id: str | None = None


class LLMBudgetEnforcer(Protocol):
    async def reserve(
        self,
        context: LLMBudgetContext,
        *,
        surface: str,
        estimated_tokens: int,
    ) -> None: ...


_budget_context: ContextVar[LLMBudgetContext | None] = ContextVar(
    "sibyl_llm_budget_context",
    default=None,
)
_budget_enforcer: LLMBudgetEnforcer | None = None


def set_budget_enforcer(enforcer: LLMBudgetEnforcer | None) -> None:
    global _budget_enforcer
    _budget_enforcer = enforcer


def get_budget_enforcer() -> LLMBudgetEnforcer | None:
    return _budget_enforcer


def get_llm_budget_context() -> LLMBudgetContext | None:
    return _budget_context.get()


def set_llm_budget_context(
    *,
    user_id: str | None = None,
    organization_id: str | None = None,
) -> Token[LLMBudgetContext | None]:
    return _budget_context.set(
        LLMBudgetContext(
            user_id=str(user_id) if user_id else None,
            organization_id=str(organization_id) if organization_id else None,
        )
    )


@contextmanager
def llm_budget_context(
    *,
    user_id: str | None = None,
    organization_id: str | None = None,
) -> Iterator[LLMBudgetContext]:
    token = set_llm_budget_context(
        user_id=user_id,
        organization_id=organization_id,
    )
    context = _budget_context.get()
    try:
        yield context or LLMBudgetContext()
    finally:
        _budget_context.reset(token)


async def reserve_llm_budget(
    *,
    surface: str,
    prompt: str,
    output_token_limit: int | None = None,
) -> int:
    enforcer = _budget_enforcer
    context = _budget_context.get()
    estimated_tokens = estimate_llm_tokens(prompt, output_token_limit=output_token_limit)
    if enforcer is None or context is None:
        return estimated_tokens
    await enforcer.reserve(
        context,
        surface=surface,
        estimated_tokens=estimated_tokens,
    )
    return estimated_tokens


def estimate_llm_tokens(text: str, *, output_token_limit: int | None = None) -> int:
    input_tokens = max(1, len(text) // 4)
    output_tokens = max(0, int(output_token_limit or 0))
    return input_tokens + output_tokens
