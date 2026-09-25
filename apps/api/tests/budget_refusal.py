"""Refuse one reservation on the real extractor path, as a budget would."""

from sibyl_core.ai.errors import LLMBudgetExceededError
from sibyl_core.ai.llm import extractor as extraction
from sibyl_core.ai.llm.budget import get_llm_run_spend_ledger


def refuse_reservation(monkeypatch, *, at: int, kind: str) -> dict[str, int]:
    """Refuse the ``at``-th reservation once, then let every later one through.

    ``ceiling`` drives the run's own ledger past its cap, so the refusal is the
    real LLMRunBudgetExceededError and the ledger is left exhausted. ``monthly``
    raises what the database enforcer raises when a monthly bucket is full.
    """
    if kind not in {"ceiling", "monthly"}:
        raise ValueError(kind)
    real = extraction.reserve_llm_budget
    seen = {"count": 0}

    async def reserve(**kwargs):
        seen["count"] += 1
        if seen["count"] == at:
            if kind == "ceiling":
                ledger = get_llm_run_spend_ledger()
                assert ledger is not None
                assert ledger.cap_tokens is not None
                ledger.admit(surface=kwargs["surface"], estimated_tokens=ledger.cap_tokens + 1)
            raise LLMBudgetExceededError(
                "LLM user monthly budget exceeded",
                surface=kwargs["surface"],
                details={"subject_type": "user", "monthly_limit": 1_000_000},
            )
        return await real(**kwargs)

    monkeypatch.setattr(extraction, "reserve_llm_budget", reserve)
    return seen
