from __future__ import annotations

import pytest

from sibyl_core.ai.errors import LLMBudgetExceededError, LLMRunBudgetExceededError
from sibyl_core.ai.llm.budget import (
    RUN_TOKEN_CEILING,
    LLMBudgetContext,
    LLMReservation,
    collect_llm_reservations,
    estimate_llm_tokens,
    get_llm_budget_context,
    get_llm_run_spend_ledger,
    llm_budget_context,
    llm_run_spend_ledger,
    reserve_llm_budget,
    set_budget_enforcer,
    settle_llm_budget,
    settle_llm_reservations,
)


class RecordingEnforcer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[LLMBudgetContext, str, int]] = []
        self.settlements: list[tuple[LLMBudgetContext, str, int, int]] = []
        self.periods: list[str | None] = []
        self.period: str | None = None

    async def reserve(
        self,
        context: LLMBudgetContext,
        *,
        surface: str,
        estimated_tokens: int,
    ) -> str | None:
        self.calls.append((context, surface, estimated_tokens))
        if self.fail:
            raise LLMBudgetExceededError("budget exceeded", surface=surface)
        return self.period

    async def settle(
        self,
        context: LLMBudgetContext,
        *,
        surface: str,
        reserved_tokens: int,
        actual_tokens: int,
        period: str | None = None,
    ) -> None:
        self.settlements.append((context, surface, reserved_tokens, actual_tokens))
        self.periods.append(period)


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


@pytest.mark.asyncio
async def test_settle_llm_budget_delegates_with_context_and_skips_a_no_op() -> None:
    enforcer = RecordingEnforcer()
    set_budget_enforcer(enforcer)

    with llm_budget_context(user_id="user-1", organization_id="org-1"):
        await settle_llm_budget(surface="memory", reserved_tokens=90, actual_tokens=40)
        await settle_llm_budget(surface="memory", reserved_tokens=40, actual_tokens=40)

    assert len(enforcer.settlements) == 1
    context, surface, reserved, actual = enforcer.settlements[0]
    assert (context.user_id, context.organization_id) == ("user-1", "org-1")
    assert (surface, reserved, actual) == ("memory", 90, 40)


@pytest.mark.asyncio
async def test_settle_llm_budget_skips_without_context() -> None:
    enforcer = RecordingEnforcer()
    set_budget_enforcer(enforcer)

    await settle_llm_budget(surface="memory", reserved_tokens=9, actual_tokens=1)

    assert enforcer.settlements == []


@pytest.mark.asyncio
async def test_run_ledger_counts_reservations_and_settles_to_actual_usage() -> None:
    enforcer = RecordingEnforcer()
    set_budget_enforcer(enforcer)

    with (
        llm_run_spend_ledger(1_000) as ledger,
        llm_budget_context(user_id="u", organization_id="o"),
    ):
        assert get_llm_run_spend_ledger() is ledger
        reserved = await reserve_llm_budget(surface="memory", prompt="abcd" * 100)
        await settle_llm_budget(surface="memory", reserved_tokens=reserved, actual_tokens=40)
        await reserve_llm_budget(surface="memory", prompt="abcd" * 10)
        await settle_llm_budget(surface="memory", reserved_tokens=10, actual_tokens=25)

    assert reserved == 100
    assert ledger.reserved_tokens == 110
    assert ledger.refunded_tokens == 60
    assert ledger.charged_tokens == 15
    assert ledger.committed_tokens == 65
    assert ledger.remaining_tokens == 935
    assert ledger.exhausted is False and ledger.stopped_reason is None
    assert get_llm_run_spend_ledger() is None
    assert [call[2] for call in enforcer.calls] == [100, 10]


@pytest.mark.asyncio
async def test_run_ledger_refuses_past_the_cap_before_the_monthly_buckets_see_it() -> None:
    enforcer = RecordingEnforcer()
    set_budget_enforcer(enforcer)

    with llm_run_spend_ledger(150) as ledger, llm_budget_context(user_id="u", organization_id="o"):
        await reserve_llm_budget(surface="memory", prompt="abcd" * 100)
        with pytest.raises(LLMRunBudgetExceededError) as caught:
            await reserve_llm_budget(surface="memory", prompt="abcd" * 100)
        assert ledger.exhausted is True
        assert ledger.stopped_reason == RUN_TOKEN_CEILING
        assert ledger.refusals == 1
        # A refused reservation must not be counted as committed.
        assert ledger.committed_tokens == 100

    assert isinstance(caught.value, LLMBudgetExceededError)
    assert caught.value.details == {
        "kind": RUN_TOKEN_CEILING,
        "run_cap_tokens": 150,
        "committed_tokens": 100,
        "requested_tokens": 100,
        "surface": "memory",
    }
    assert len(enforcer.calls) == 1


@pytest.mark.asyncio
async def test_run_ledger_without_a_cap_only_counts() -> None:
    with llm_run_spend_ledger(None) as ledger:
        for _ in range(3):
            await reserve_llm_budget(surface="memory", prompt="abcd" * 1_000)

    assert ledger.reserved_tokens == 3_000
    assert ledger.remaining_tokens is None
    assert ledger.exhausted is False


def test_run_ledger_rejects_a_non_positive_cap() -> None:
    with pytest.raises(ValueError, match="positive"), llm_run_spend_ledger(0):
        pass


@pytest.mark.asyncio
async def test_settle_rejects_negative_counts() -> None:
    with pytest.raises(ValueError):
        await settle_llm_budget(surface="memory", reserved_tokens=-1, actual_tokens=0)


@pytest.mark.asyncio
async def test_reservations_record_the_period_their_enforcer_returns() -> None:
    enforcer = RecordingEnforcer()
    enforcer.period = "2026-09"
    set_budget_enforcer(enforcer)

    with collect_llm_reservations() as log, llm_budget_context(user_id="u", organization_id="o"):
        await reserve_llm_budget(surface="memory", prompt="abcd" * 100)
        enforcer.period = "2026-10"
        await reserve_llm_budget(surface="memory", prompt="abcd" * 10, attempt_envelope=2)

    assert log == [LLMReservation(100, "2026-09"), LLMReservation(20, "2026-10")]


@pytest.mark.asyncio
async def test_a_refused_reservation_is_not_recorded() -> None:
    set_budget_enforcer(RecordingEnforcer(fail=True))

    with (
        collect_llm_reservations() as log,
        llm_budget_context(user_id="u", organization_id="o"),
        pytest.raises(LLMBudgetExceededError),
    ):
        await reserve_llm_budget(surface="memory", prompt="abcd")

    assert log == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        # Under the September reservation: September takes it all, October is freed.
        (20, [(400, 20, "2026-09"), (100, 0, "2026-10")]),
        # September keeps what it held and October takes the rest.
        (450, [(100, 50, "2026-10")]),
        # Overage lands in the last period, never refunded to an earlier one.
        (700, [(100, 300, "2026-10")]),
    ],
)
async def test_settle_llm_reservations_allots_usage_period_by_period(actual, expected) -> None:
    enforcer = RecordingEnforcer()
    set_budget_enforcer(enforcer)
    reservations = [LLMReservation(400, "2026-09"), LLMReservation(100, "2026-10")]

    with llm_budget_context(user_id="u", organization_id="o"):
        await settle_llm_reservations(
            surface="memory", reservations=reservations, actual_tokens=actual
        )

    settled = [
        (reserved, used, period)
        for (_context, _surface, reserved, used), period in zip(
            enforcer.settlements, enforcer.periods, strict=True
        )
    ]
    assert settled == expected


@pytest.mark.asyncio
async def test_settle_llm_budget_passes_the_period_through() -> None:
    enforcer = RecordingEnforcer()
    set_budget_enforcer(enforcer)

    with llm_budget_context(user_id="u", organization_id="o"):
        await settle_llm_budget(
            surface="memory", reserved_tokens=9, actual_tokens=1, period="2026-09"
        )

    assert enforcer.periods == ["2026-09"]
