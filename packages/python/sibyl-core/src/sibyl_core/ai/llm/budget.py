"""LLM budget context, per-run spend ledger, and reservation hooks."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Protocol

from sibyl_core.ai.errors import LLMBudgetExceededError, LLMRunBudgetExceededError

RUN_TOKEN_CEILING = "run_token_ceiling"


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
    ) -> str | None:
        """Reserve tokens and return the budget period they landed in, if any."""
        ...

    async def settle(
        self,
        context: LLMBudgetContext,
        *,
        surface: str,
        reserved_tokens: int,
        actual_tokens: int,
        period: str | None = None,
    ) -> None:
        """Replace a reservation with what the call consumed, never below zero.

        ``period`` is what ``reserve`` returned, so a call reserved in one
        budget period settles in that period even if it finishes in the next.
        """
        ...


@dataclass(frozen=True, slots=True)
class LLMReservation:
    """Tokens one reservation took, and the budget period they belong to."""

    tokens: int
    period: str | None = None


@dataclass(slots=True)
class LLMRunSpendLedger:
    """Tokens one job run has committed against its own ceiling.

    A reservation counts in full until its call settles to actual usage, so a
    run in flight never looks cheaper than its worst case. A reservation that
    would cross the cap is refused before the shared monthly buckets see it,
    and the ledger marks itself exhausted so the run can stop admitting work
    instead of failing every remaining call one by one.
    """

    cap_tokens: int | None
    reserved_tokens: int = 0
    refunded_tokens: int = 0
    charged_tokens: int = 0
    refusals: int = 0
    exhausted: bool = False
    stopped_reason: str | None = None

    @property
    def committed_tokens(self) -> int:
        return self.reserved_tokens - self.refunded_tokens + self.charged_tokens

    @property
    def remaining_tokens(self) -> int | None:
        if self.cap_tokens is None:
            return None
        return max(0, self.cap_tokens - self.committed_tokens)

    def admit(self, *, surface: str, estimated_tokens: int) -> None:
        """Refuse a reservation the cap cannot hold, and remember that it happened."""
        if self.cap_tokens is None:
            return
        if self.committed_tokens + estimated_tokens > self.cap_tokens:
            self.refusals += 1
            self.exhausted = True
            self.stopped_reason = self.stopped_reason or RUN_TOKEN_CEILING
            raise LLMRunBudgetExceededError(
                "LLM run token ceiling reached",
                surface=surface,
                details={
                    "kind": RUN_TOKEN_CEILING,
                    "run_cap_tokens": self.cap_tokens,
                    "committed_tokens": self.committed_tokens,
                    "requested_tokens": estimated_tokens,
                },
            )

    def record_reservation(self, estimated_tokens: int) -> None:
        self.reserved_tokens += estimated_tokens

    def settle(self, *, reserved_tokens: int, actual_tokens: int) -> None:
        delta = actual_tokens - reserved_tokens
        if delta < 0:
            self.refunded_tokens += -delta
        elif delta > 0:
            self.charged_tokens += delta

    def snapshot(self) -> dict[str, int | str | bool | None]:
        return {
            "cap_tokens": self.cap_tokens,
            "reserved_tokens": self.reserved_tokens,
            "refunded_tokens": self.refunded_tokens,
            "charged_tokens": self.charged_tokens,
            "committed_tokens": self.committed_tokens,
            "remaining_tokens": self.remaining_tokens,
            "refusals": self.refusals,
            "exhausted": self.exhausted,
            "stopped_reason": self.stopped_reason,
        }


_budget_context: ContextVar[LLMBudgetContext | None] = ContextVar(
    "sibyl_llm_budget_context",
    default=None,
)
_run_ledger: ContextVar[LLMRunSpendLedger | None] = ContextVar(
    "sibyl_llm_run_spend_ledger",
    default=None,
)
_reservation_log: ContextVar[list[LLMReservation] | None] = ContextVar(
    "sibyl_llm_reservation_log",
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


def get_llm_run_spend_ledger() -> LLMRunSpendLedger | None:
    return _run_ledger.get()


def run_ceiling_reached() -> bool:
    """True once the current run's spend ledger has refused a reservation."""
    ledger = _run_ledger.get()
    return ledger is not None and ledger.exhausted


def budget_failure_fields(exc: BaseException) -> dict[str, object]:
    """Receipt fields that name a budget refusal, so a run report can count them."""
    if not isinstance(exc, LLMBudgetExceededError):
        return {}
    fields: dict[str, object] = {"budget": dict(exc.details)}
    if isinstance(exc, LLMRunBudgetExceededError):
        fields["reason"] = RUN_TOKEN_CEILING
    return fields


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


@contextmanager
def llm_run_spend_ledger(cap_tokens: int | None) -> Iterator[LLMRunSpendLedger]:
    """Count every reservation and settlement made inside against one run's ceiling."""
    if cap_tokens is not None and cap_tokens <= 0:
        raise ValueError("run token ceiling must be positive")
    ledger = LLMRunSpendLedger(cap_tokens=cap_tokens)
    token = _run_ledger.set(ledger)
    try:
        yield ledger
    finally:
        _run_ledger.reset(token)


@contextmanager
def collect_llm_reservations() -> Iterator[list[LLMReservation]]:
    """Record every reservation made inside, so one call can settle exactly what it took."""
    reservations: list[LLMReservation] = []
    token = _reservation_log.set(reservations)
    try:
        yield reservations
    finally:
        _reservation_log.reset(token)


async def reserve_llm_budget(
    *,
    surface: str,
    prompt: str,
    output_token_limit: int | None = None,
    attempt_envelope: int = 1,
) -> int:
    """Reserve a character-based estimate for ``attempt_envelope`` attempts."""
    if attempt_envelope < 1:
        raise ValueError("attempt envelope must be positive")
    estimated_tokens = (
        estimate_llm_tokens(prompt, output_token_limit=output_token_limit) * attempt_envelope
    )
    ledger = _run_ledger.get()
    if ledger is not None:
        ledger.admit(surface=surface, estimated_tokens=estimated_tokens)
    enforcer = _budget_enforcer
    context = _budget_context.get()
    period = None
    if enforcer is not None and context is not None:
        period = await enforcer.reserve(
            context,
            surface=surface,
            estimated_tokens=estimated_tokens,
        )
    # The ledger's admit check and this record sit on either side of the
    # enforcer's await. That is safe while a run makes its calls one at a time;
    # concurrent calls under one ledger could each pass admit before either
    # records, and would need the two steps joined under a lock.
    if ledger is not None:
        ledger.record_reservation(estimated_tokens)
    log = _reservation_log.get()
    if log is not None:
        log.append(LLMReservation(estimated_tokens, period if isinstance(period, str) else None))
    return estimated_tokens


async def settle_llm_budget(
    *,
    surface: str,
    reserved_tokens: int,
    actual_tokens: int,
    period: str | None = None,
) -> None:
    """Replace what a call reserved with what it used, in the buckets and the run ledger."""
    if reserved_tokens < 0 or actual_tokens < 0:
        raise ValueError("token counts must not be negative")
    enforcer = _budget_enforcer
    context = _budget_context.get()
    if enforcer is not None and context is not None and reserved_tokens != actual_tokens:
        await enforcer.settle(
            context,
            surface=surface,
            reserved_tokens=reserved_tokens,
            actual_tokens=actual_tokens,
            period=period,
        )
    ledger = _run_ledger.get()
    if ledger is not None:
        ledger.settle(reserved_tokens=reserved_tokens, actual_tokens=actual_tokens)


async def settle_llm_reservations(
    *,
    surface: str,
    reservations: Sequence[LLMReservation],
    actual_tokens: int,
) -> None:
    """Settle one call's reservations against its actual usage, period by period.

    A call's hops can land in different budget periods when it runs across a
    month boundary. Usage is allotted to periods in the order they were
    reserved, each taking at most what it reserved, and the last period takes
    any overage, so no period is refunded tokens it never held.
    """
    if actual_tokens < 0:
        raise ValueError("token counts must not be negative")
    periods: dict[str | None, int] = {}
    for reservation in reservations:
        periods[reservation.period] = periods.get(reservation.period, 0) + reservation.tokens
    remaining = actual_tokens
    items = list(periods.items())
    for index, (period, reserved) in enumerate(items):
        used = remaining if index == len(items) - 1 else min(reserved, remaining)
        remaining -= used
        await settle_llm_budget(
            surface=surface, reserved_tokens=reserved, actual_tokens=used, period=period
        )


def estimate_llm_tokens(text: str, *, output_token_limit: int | None = None) -> int:
    input_tokens = max(1, len(text) // 4)
    output_tokens = max(0, int(output_token_limit or 0))
    return input_tokens + output_tokens
