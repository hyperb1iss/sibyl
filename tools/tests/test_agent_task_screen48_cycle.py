"""Unit cover for the screen48 consolidation-cycle driver.

Every product call is monkeypatched: no database, no broker, no network. What
is under test is the driver's own policy, which is the part that can waste a
real run's money or hang it forever.
"""

# Expected token counts, HTTP codes and exit codes are the assertions here.
# ruff: noqa: PLR2004

from __future__ import annotations

import json
import os
import re
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from benchmarks.agent_tasks.screen48 import cycle
from benchmarks.agent_tasks.screen48.devbox import owned_db, run_phase

PRICES = {
    "price_input_per_million": Decimal("5"),
    "price_output_per_million": Decimal("25"),
}

#: A live-credential verdict, the shape ``preflight_provider`` returns.
PREFLIGHT_OK = {
    "provider": "anthropic",
    "model": "claude-opus-5",
    "status": "ok",
    "status_code": None,
    "error_type": None,
    "error_message": None,
    "checked_at": "2026-09-15T00:00:00+00:00",
}

#: Stands in for a real key so the receipt can be searched for key material.
FAKE_API_KEY = "sk-ant-api03-SCREEN48-FAKE-KEY-DO-NOT-LOG"

#: Captured before any fixture swaps it, so a test can ask for the real probe.
REAL_PREFLIGHT = cycle.preflight_provider


def make_config(**overrides: Any) -> cycle.CycleConfig:
    base: dict[str, Any] = {
        "cost_ceiling_usd": Decimal("100"),
        **PRICES,
    }
    base.update(overrides)
    return cycle.CycleConfig(**base)


class FakeProduct:
    """A dream job that consumes sources, drains candidates, and bills tokens."""

    def __init__(
        self,
        *,
        sources: list[str],
        pending: list[str] | None = None,
        drains: bool = True,
        input_tokens: int = 1_000,
        output_tokens: int = 200,
    ) -> None:
        self.source_ids = list(sources)
        self.index = 0
        self.revision = 0
        self.pending = list(pending or [])
        self.drains = drains
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: list[dict[str, int]] = []
        self.usage: list[dict[str, Any]] = []
        self.returned: set[str] = set()
        self.repairs = 0

    # -- the product job -------------------------------------------------
    async def invoke(
        self, *, group_id: str, source_limit: int, candidate_limit: int
    ) -> dict[str, Any]:
        assert group_id
        self.calls.append({"source_limit": source_limit, "candidate_limit": candidate_limit})
        consumed = self.source_ids[self.index : self.index + source_limit]
        self.index += len(consumed)
        self.revision += len(consumed)
        self.returned.update(consumed)
        drained: list[str] = []
        if candidate_limit > 0 and self.drains:
            drained, self.pending = self.pending, []
        self.usage.append(
            {
                "state": "returned",
                "created_at": "2026-09-15T00:00:00Z",
                "usage_json": json.dumps(
                    {
                        "requests": 1,
                        "input_tokens": self.input_tokens,
                        "output_tokens": self.output_tokens,
                        "total_tokens": self.input_tokens + self.output_tokens,
                        "cost_complete": False,
                    }
                ),
            }
        )
        return {
            "run_id": f"run-{len(self.calls)}",
            "sources_scanned": len(consumed),
            "sources_reflected": len(consumed),
            "candidates_scanned": len(drained),
            "promoted": len(drained),
            "archived": 0,
            "sources": [{"source_ids": consumed, "outcome": "reflected"}],
            "candidates": [
                {"candidate_id": identifier, "applied": True, "archived": False}
                for identifier in drained
            ],
        }

    # -- the driver's read seams ------------------------------------------
    async def load_cursor(self, group_id: str) -> tuple[str, int]:
        assert group_id
        cursor = self.source_ids[self.index - 1] if self.index else ""
        return cursor, self.revision

    async def count_eligible(self, group_id: str, cursor: str) -> int:
        assert group_id
        assert isinstance(cursor, str)
        return len(self.source_ids) - self.index

    async def list_sources(self, group_id: str) -> list[str]:
        assert group_id
        return list(self.source_ids)

    async def pending_ids(self, group_id: str, *, limit: int = 500) -> list[str]:
        assert group_id
        assert limit
        return list(self.pending)

    async def usage_rows(self, group_id: str, since: Any) -> list[dict[str, Any]]:
        assert group_id
        assert since is not None
        return list(self.usage)

    async def returned_sources(self, group_id: str) -> set[str]:
        assert group_id
        return set(self.returned)

    async def repair(self, group_id: str) -> dict[str, int]:
        assert group_id
        self.repairs += 1
        return {"checked": 0, "recovered": 0, "pending": 0, "failed": 0}


@pytest.fixture
def product(monkeypatch: pytest.MonkeyPatch):
    """Install a fake product behind every driver seam, return a factory."""

    def install(fake: FakeProduct) -> FakeProduct:
        async def noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        async def preflight_ok(_config: Any) -> dict[str, Any]:
            return dict(PREFLIGHT_OK)

        monkeypatch.setattr(cycle, "bootstrap_runtime", noop)
        monkeypatch.setattr(cycle, "shutdown_runtime", noop)
        monkeypatch.setattr(cycle, "preflight_provider", preflight_ok)
        monkeypatch.setattr(cycle, "invoke_dream_cycle", fake.invoke)
        monkeypatch.setattr(cycle, "load_cursor", fake.load_cursor)
        monkeypatch.setattr(cycle, "count_eligible_sources", fake.count_eligible)
        monkeypatch.setattr(cycle, "list_eligible_source_ids", fake.list_sources)
        monkeypatch.setattr(cycle, "pending_candidate_ids", fake.pending_ids)
        monkeypatch.setattr(cycle, "usage_rows", fake.usage_rows)
        monkeypatch.setattr(cycle, "returned_execution_source_ids", fake.returned_sources)
        monkeypatch.setattr(cycle, "repair_embeddings", fake.repair)

        async def surfaces(_group_id: str) -> dict[str, int]:
            return {"agent_conversation": len(fake.source_ids)}

        async def states(_group_id: str) -> dict[str, int]:
            return {"None": len(fake.source_ids)}

        async def count(_group_id: str) -> int:
            return 0

        async def empty_set(_group_id: str) -> set[str]:
            return set()

        async def empty_list(_group_id: str) -> list[str]:
            return []

        async def health() -> dict[str, Any]:
            return {"status": "healthy", "queue_depth": 0}

        monkeypatch.setattr(cycle, "capture_surface_counts", surfaces)
        monkeypatch.setattr(cycle, "review_state_counts", states)
        monkeypatch.setattr(cycle, "entity_count", count)
        monkeypatch.setattr(cycle, "live_execution_count", count)
        monkeypatch.setattr(cycle, "completed_checkpoint_source_ids", empty_set)
        monkeypatch.setattr(cycle, "missing_embedding_candidate_ids", empty_list)
        monkeypatch.setattr(cycle, "broker_health", health)
        return fake

    return install


# ---------------------------------------------------------------------------
# Page planning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remaining", "page", "expected"),
    [
        (233, 100, 100),
        (133, 100, 100),
        (33, 100, 33),
        (0, 100, 0),
        (500, 250, 100),
        (-4, 100, 0),
    ],
)
def test_plan_source_page_never_exceeds_remaining(remaining: int, page: int, expected: int) -> None:
    assert cycle.plan_source_page(remaining, page) == expected


async def test_proposal_passes_page_233_without_wrapping(tmp_path: Path, product: Any) -> None:
    fake = product(FakeProduct(sources=[f"s{index:03d}" for index in range(233)]))

    receipt = await cycle.run_cycle(make_config(), tmp_path)

    assert [call["source_limit"] for call in fake.calls] == [100, 100, 33]
    assert fake.index == 233
    assert receipt["proposal"]["remaining_above_cursor"] == 0
    assert receipt["status"] == cycle.STATUS_COMPLETE
    assert receipt["reasons"] == []
    assert receipt["predicates"]["d_every_source_terminal"]["satisfied"] is True


class RingProduct:
    """A dream pager that keeps its budget by wrapping onto consolidated sources.

    Closer to the product than ``FakeProduct``: a source the owner rejects is
    never consolidated and never advances the cursor, and a pass asked for more
    than remains above the cursor fills the deficit from the start of the ring,
    which is exactly the waste the driver exists to prevent.
    """

    def __init__(
        self,
        *,
        sources: list[str],
        rejected: set[str] | None = None,
        over_count: bool = False,
        advances: bool = True,
    ) -> None:
        self.source_ids = list(sources)
        self.rejected = set(rejected or set())
        self.over_count = over_count
        self.advances = advances
        self.cursor = ""
        self.revision = 0
        self.calls: list[dict[str, int]] = []
        self.consolidated: list[str] = []
        self.wrapped: list[str] = []
        self.returned: set[str] = set()
        self.usage: list[dict[str, Any]] = []
        self.repairs = 0

    def _acceptable_above(self, cursor: str) -> list[str]:
        return [
            identifier
            for identifier in self.source_ids
            if identifier > cursor and identifier not in self.rejected
        ]

    async def invoke(
        self, *, group_id: str, source_limit: int, candidate_limit: int
    ) -> dict[str, Any]:
        assert group_id
        self.calls.append({"source_limit": source_limit, "candidate_limit": candidate_limit})
        picked = self._acceptable_above(self.cursor)[:source_limit]
        wrapped: list[str] = []
        if len(picked) < source_limit:
            deficit = source_limit - len(picked)
            wrapped = [
                identifier
                for identifier in self.source_ids
                if identifier <= self.cursor and identifier not in self.rejected
            ][:deficit]
        consolidated = [*picked, *wrapped]
        self.consolidated.extend(consolidated)
        self.wrapped.extend(wrapped)
        self.returned.update(consolidated)
        if consolidated and self.advances:
            self.cursor = consolidated[-1]
            self.revision += len(consolidated)
        self.usage.append(
            {
                "state": "returned",
                "created_at": "2026-09-15T00:00:00Z",
                "usage_json": json.dumps(
                    {"requests": 1, "input_tokens": 1_000, "output_tokens": 200}
                ),
            }
        )
        return {
            "run_id": f"run-{len(self.calls)}",
            "sources_scanned": len(consolidated),
            "sources_reflected": len(consolidated),
            "candidates_scanned": 0,
            "promoted": 0,
            "archived": 0,
            "sources": [{"source_ids": consolidated, "outcome": "reflected"}],
            "candidates": [],
        }

    async def load_cursor(self, group_id: str) -> tuple[str, int]:
        assert group_id
        return self.cursor, self.revision

    async def count_eligible(self, group_id: str, cursor: str) -> int:
        assert group_id
        if self.over_count:
            return len([identifier for identifier in self.source_ids if identifier > cursor])
        return len(self._acceptable_above(cursor))

    async def list_sources(self, group_id: str) -> list[str]:
        assert group_id
        return list(self.source_ids)

    async def pending_ids(self, group_id: str, *, limit: int = 500) -> list[str]:
        assert group_id
        assert limit
        return []

    async def usage_rows(self, group_id: str, since: Any) -> list[dict[str, Any]]:
        assert group_id
        assert since is not None
        return list(self.usage)

    async def returned_sources(self, group_id: str) -> set[str]:
        assert group_id
        return set(self.returned)

    async def repair(self, group_id: str) -> dict[str, int]:
        assert group_id
        self.repairs += 1
        return {"checked": 0, "recovered": 0, "pending": 0, "failed": 0}


async def test_three_owner_rejected_sources_do_not_wrap_the_last_page(
    tmp_path: Path, product: Any
) -> None:
    """233 admitted captures, three the dream owner will not accept.

    The naive SQL count calls the last page 33 and the pager fills the missing
    three from the head of the ring. An exact count asks for 30 and the run
    ends on 230 consolidations, each one paid for once.
    """
    sources = [f"s{index:03d}" for index in range(233)]
    rejected = {"s101", "s150", "s232"}
    fake = product(RingProduct(sources=sources, rejected=rejected))

    receipt = await cycle.run_cycle(make_config(), tmp_path)

    assert [call["source_limit"] for call in fake.calls] == [100, 100, 30]
    assert fake.wrapped == []
    assert len(fake.consolidated) == len(set(fake.consolidated)) == 230
    assert receipt["proposal"]["ring_wrap"] is None
    assert receipt["proposal"]["remaining_above_cursor"] == 0
    assert all(item["ring_wrap"] is None for item in receipt["proposal"]["passes"])


async def test_an_over_counted_page_that_wraps_stops_the_run(tmp_path: Path, product: Any) -> None:
    """The residual over-count is caught after the fact, before it repeats."""
    sources = [f"s{index:03d}" for index in range(233)]
    fake = product(RingProduct(sources=sources, rejected={"s101", "s150", "s232"}, over_count=True))

    receipt = await cycle.run_cycle(make_config(), tmp_path)

    assert [call["source_limit"] for call in fake.calls] == [100, 100, 31]
    assert fake.wrapped == ["s000"]
    wrap = receipt["proposal"]["ring_wrap"]
    assert wrap["reason"] == "consolidated_source_at_or_below_cursor"
    assert wrap["wrapped_source_ids"] == ["s000"]
    assert receipt["status"] == cycle.STATUS_RING_WRAP
    assert receipt["reasons"][0] == "ring_wrap_detected"
    assert "drain" not in receipt, "a wrapped run must not keep spending"
    assert fake.repairs == 0


def test_detect_ring_wrap_reads_both_signatures() -> None:
    repeated = cycle.detect_ring_wrap(
        cursor_before="s100", cursor_after="s130", source_ids=["s099", "s130"]
    )
    assert repeated is not None
    assert repeated["wrapped_source_ids"] == ["s099"]
    backwards = cycle.detect_ring_wrap(
        cursor_before="s100", cursor_after="s004", source_ids=["s101"]
    )
    assert backwards is not None
    assert backwards["reason"] == "cursor_moved_backwards"
    assert (
        cycle.detect_ring_wrap(cursor_before="s100", cursor_after="s130", source_ids=["s101"])
        is None
    )
    # A fresh run starts below every id, so nothing it consolidates is a repeat.
    assert (
        cycle.detect_ring_wrap(cursor_before="", cursor_after="s100", source_ids=["s001"]) is None
    )


def test_receipt_source_ids_reads_cohort_and_single_source_rows() -> None:
    receipt = {
        "sources": [
            {"source_ids": ["s001", "s002"]},
            {"source_id": "s003"},
            {"source_id": None},
            "not-a-row",
        ]
    }

    assert cycle.receipt_source_ids(receipt) == ["s001", "s002", "s003"]


async def test_count_eligible_sources_drops_what_the_product_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count mirrors the pager's Python filters, not just its SQL predicate."""
    rows = [
        {"uuid": "s001", "organization_id": "org", "principal_id": "p", "raw_content": "body"},
        # Excluded by the SQL predicate on the column itself.
        {
            "uuid": "s002",
            "organization_id": "org",
            "principal_id": "p",
            "raw_content": "body",
            "capture_surface": "reflection_candidate",
        },
        # Not currently recallable: superseded by a later source.
        {
            "uuid": "s003",
            "organization_id": "org",
            "principal_id": "p",
            "raw_content": "body",
            "metadata": {"superseded_by_source_id": "s999"},
        },
        # Excluded surface carried only in metadata, which SQL cannot see.
        {
            "uuid": "s004",
            "organization_id": "org",
            "principal_id": "p",
            "raw_content": "body",
            "metadata": {"capture_surface": "reflection_candidate"},
        },
        # The dream owner refuses a source with no principal or no content.
        {"uuid": "s005", "organization_id": "org", "principal_id": "", "raw_content": "body"},
        {"uuid": "s006", "organization_id": "org", "principal_id": "p", "raw_content": "   "},
        {"uuid": "s007", "organization_id": "org", "principal_id": "p", "raw_content": "body"},
    ]

    async def content_rows(_query: str, **params: Any) -> list[dict[str, Any]]:
        excluded = set(params["excluded"])
        return [
            row
            for row in rows
            if row["uuid"] > params["cursor"] and row.get("capture_surface") not in excluded
        ]

    monkeypatch.setattr(cycle, "_content_rows", content_rows)

    assert await cycle.eligible_source_ids_above("org", "") == ["s001", "s007"]
    assert await cycle.count_eligible_sources("org", "") == 2
    assert await cycle.count_eligible_sources("org", "s001") == 1


async def test_a_cursor_that_never_advances_stops_the_proposal_loop(
    tmp_path: Path, product: Any
) -> None:
    """A cursor-revision conflict must not re-dispatch the same page 233 times."""
    fake = product(RingProduct(sources=[f"s{index:03d}" for index in range(233)], advances=False))

    receipt = await cycle.run_cycle(make_config(), tmp_path)

    assert len(fake.calls) == 1, "the same page must not be bought twice"
    assert receipt["proposal"]["stopped_reason"] == cycle.STOP_CURSOR_STALLED
    assert receipt["status"] == cycle.STATUS_INCOMPLETE
    assert cycle.STOP_CURSOR_STALLED in receipt["reasons"]


async def test_no_eligible_sources_dispatches_nothing(tmp_path: Path, product: Any) -> None:
    fake = product(FakeProduct(sources=[]))

    receipt = await cycle.run_cycle(make_config(expected_sources=0), tmp_path)

    assert fake.calls == []
    assert receipt["status"] == cycle.STATUS_COMPLETE


# ---------------------------------------------------------------------------
# Stuck-pending guard
# ---------------------------------------------------------------------------


async def test_stuck_pending_stops_after_a_pass_with_no_transition(
    tmp_path: Path, product: Any
) -> None:
    fake = product(FakeProduct(sources=[], pending=["cand-b", "cand-a"], drains=False))

    receipt = await cycle.run_cycle(make_config(expected_sources=0, max_drain_passes=6), tmp_path)

    assert len(fake.calls) == 1, "a second drain pass would re-pay for the same candidates"
    assert receipt["drain"]["stuck_pending"] == ["cand-a", "cand-b"]
    assert receipt["status"] == cycle.STATUS_INCOMPLETE
    assert "stuck_pending_candidates" in receipt["reasons"]
    assert "predicate_a_no_pending_candidates_unsatisfied" in receipt["reasons"]


async def test_draining_candidates_clears_the_pending_set(tmp_path: Path, product: Any) -> None:
    fake = product(FakeProduct(sources=[], pending=["cand-a"], drains=True))

    receipt = await cycle.run_cycle(make_config(expected_sources=0), tmp_path)

    assert len(fake.calls) == 1
    assert receipt["drain"]["stuck_pending"] == []
    assert receipt["drain"]["passes"][0]["pending_after"] == []
    assert receipt["status"] == cycle.STATUS_COMPLETE


# ---------------------------------------------------------------------------
# Cost ceiling
# ---------------------------------------------------------------------------


async def test_cost_ceiling_stops_dispatch_and_seals_the_receipt(
    tmp_path: Path, product: Any
) -> None:
    # 10M input tokens in one request sits in the long-context tier, so it bills
    # at 10/M for 100 USD per invocation and the first pass alone clears a
    # 10 USD ceiling.
    fake = product(
        FakeProduct(
            sources=[f"s{index:03d}" for index in range(233)],
            input_tokens=10_000_000,
            output_tokens=0,
        )
    )

    receipt = await cycle.run_cycle(make_config(cost_ceiling_usd=Decimal("10")), tmp_path)

    assert len(fake.calls) == 1, "dispatch must stop at the ceiling, not finish the ring"
    assert receipt["status"] == cycle.STATUS_COST_CEILING
    assert receipt["reasons"][0] == "cost_ceiling_exceeded"
    assert Decimal(receipt["usage"]["cost_usd_exact"]) == Decimal("100")
    assert "drain" not in receipt


async def test_a_tripped_ceiling_skips_the_embedding_repair(tmp_path: Path, product: Any) -> None:
    """Repair calls the embedding provider and writes, so the guard owns it too."""
    fake = product(
        FakeProduct(
            sources=[f"s{index:03d}" for index in range(233)],
            input_tokens=10_000_000,
            output_tokens=0,
        )
    )

    receipt = await cycle.run_cycle(make_config(cost_ceiling_usd=Decimal("10")), tmp_path)

    assert fake.repairs == 0, "no provider calls once the ceiling has tripped"
    assert "embedding_repair" not in receipt
    assert receipt["broker_health"]["status"] == "healthy"


async def test_a_run_under_the_ceiling_still_repairs_embeddings(
    tmp_path: Path, product: Any
) -> None:
    fake = product(FakeProduct(sources=["s000"]))

    receipt = await cycle.run_cycle(make_config(expected_sources=1), tmp_path)

    assert fake.repairs == 1
    assert receipt["embedding_repair"] == {
        "checked": 0,
        "recovered": 0,
        "pending": 0,
        "failed": 0,
    }


# ---------------------------------------------------------------------------
# Usage summation
# ---------------------------------------------------------------------------


def test_usage_summation_mixes_complete_costs_tokens_and_failures() -> None:
    rows = [
        {
            "state": "returned",
            "usage_json": json.dumps(
                {
                    "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                    "cost_usd": 3.5,
                    "cost_complete": True,
                }
            ),
        },
        {
            "state": "returned",
            "usage_json": json.dumps(
                {
                    "input_tokens": 2_000_000,
                    "output_tokens": 100_000,
                    "cost_usd": None,
                    "cost_complete": False,
                }
            ),
        },
        {
            "state": "failed",
            "usage_json": json.dumps(
                {"transport_attempts": [], "usage_complete": False, "cost_complete": False}
            ),
        },
        {"state": "running", "usage_json": None},
    ]

    summary = cycle.summarize_usage(rows, **PRICES)

    # 3.50 for the priced row, then a 2M-input single request in the
    # long-context tier: 2M at 10/M plus 100K output at 50/M.
    assert Decimal(summary["cost_usd_exact"]) == Decimal("3.5") + Decimal("20") + Decimal("5")
    assert summary["rows"] == 4
    assert summary["rows_with_usage"] == 3
    assert summary["rows_cost_complete"] == 1
    assert summary["rows_priced_from_tokens"] == 2
    assert summary["input_tokens"] == 3_000_000
    assert summary["output_tokens"] == 1_100_000
    assert summary["states"] == {"returned": 2, "failed": 1, "running": 1}


def test_usage_summation_ignores_cost_usd_when_cost_is_incomplete() -> None:
    rows = [
        {
            "state": "returned",
            "usage_json": json.dumps(
                {"input_tokens": 0, "output_tokens": 0, "cost_usd": 999.0, "cost_complete": False}
            ),
        }
    ]

    assert Decimal(cycle.summarize_usage(rows, **PRICES)["cost_usd_exact"]) == Decimal(0)


def test_usage_summation_counts_unparsable_rows() -> None:
    summary = cycle.summarize_usage([{"state": "returned", "usage_json": "{"}], **PRICES)

    assert summary["rows_unparsable"] == 1
    assert summary["rows_with_usage"] == 0


def test_archived_exception_reasons_are_counted_by_reason() -> None:
    receipts = [
        {
            "candidates": [
                {"archived": True, "exception_reasons": ["sensitive_material", "low_support"]},
                {"archived": True, "exception_reasons": [], "reason": "policy_block"},
                {"archived": False, "exception_reasons": ["ignored"]},
            ]
        },
        {"candidates": [{"archived": True, "exception_reasons": ["sensitive_material"]}]},
    ]

    assert cycle._archived_exception_reasons(receipts) == {
        "sensitive_material": 2,
        "low_support": 1,
        "policy_block": 1,
    }


# ---------------------------------------------------------------------------
# Evidence durability
# ---------------------------------------------------------------------------


async def test_receipt_is_created_exclusively(tmp_path: Path, product: Any) -> None:
    product(FakeProduct(sources=[]))
    await cycle.run_cycle(make_config(expected_sources=0), tmp_path)

    with pytest.raises(cycle.CycleError, match="already exists"):
        await cycle.run_cycle(make_config(expected_sources=0), tmp_path)


async def test_invocations_jsonl_grows_once_per_product_invocation(
    tmp_path: Path, product: Any
) -> None:
    fake = product(FakeProduct(sources=[f"s{index:03d}" for index in range(233)]))

    receipt = await cycle.run_cycle(make_config(), tmp_path)

    lines = (tmp_path / "invocations.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    # One line for the provider probe, then one per product invocation.
    assert [record["index"] for record in records] == list(range(len(records)))
    assert records[0]["phase"] == "provider_preflight"
    invocations = records[1:]
    assert len(invocations) == len(fake.calls) == receipt["invocations"]
    assert [record["requested_source_limit"] for record in invocations] == [100, 100, 33]
    assert all(record["phase"] == "proposal" for record in invocations)
    assert all("receipt" in record for record in invocations)

    written = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert written["status"] == receipt["status"]
    assert written["config"]["pricing_source"] == cycle.PRICING_SOURCE


async def test_a_dispatch_failure_still_seals_a_receipt(
    tmp_path: Path, product: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    product(FakeProduct(sources=["s000"]))

    async def explode(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("provider refused")

    monkeypatch.setattr(cycle, "invoke_dream_cycle", explode)

    receipt = await cycle.run_cycle(make_config(), tmp_path)

    assert receipt["status"] == cycle.STATUS_INCOMPLETE
    assert any("provider refused" in reason for reason in receipt["reasons"])
    assert (tmp_path / "cycle.json").exists()


# ---------------------------------------------------------------------------
# Provider preflight
# ---------------------------------------------------------------------------


class FakeAPIStatusError(Exception):
    """Shaped like ``anthropic.APIStatusError``: a status code and a JSON body.

    Its own string carries the fake key, the way a transport-level error can,
    so a driver that falls back to ``str(exc)`` when a body is right there
    would leak it into the receipt.
    """

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        super().__init__(f"Error code: {status_code} - x-api-key {FAKE_API_KEY} - {body}")
        self.status_code = status_code
        self.body = body


CAPPED_ACCOUNT_BODY = {
    "error": {
        "type": "invalid_request_error",
        "message": "You have reached your specified API usage limits...",
    }
}


def install_probe(
    monkeypatch: pytest.MonkeyPatch, *, raises: BaseException | None = None
) -> list[tuple[str, str]]:
    """Resolve the MEMORY surface to Anthropic and record every probe.

    Restores the real ``preflight_provider`` over whatever the ``product``
    fixture installed, so the driver's own probe policy is what runs.
    """
    probes: list[tuple[str, str]] = []

    async def resolve() -> tuple[str, str]:
        return "anthropic", "claude-opus-5"

    def probe(provider: str, model: str) -> None:
        probes.append((provider, model))
        if raises is not None:
            raise raises

    monkeypatch.setattr(cycle, "preflight_provider", REAL_PREFLIGHT)
    monkeypatch.setattr(cycle, "_resolve_memory_llm", resolve)
    monkeypatch.setattr(cycle, "_probe_provider", probe)
    return probes


async def test_preflight_records_a_live_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    probes = install_probe(monkeypatch)

    record = await cycle.preflight_provider(make_config())

    assert probes == [("anthropic", "claude-opus-5")]
    assert record["status"] == "ok"
    assert record["provider"] == "anthropic"
    assert record["model"] == "claude-opus-5"
    assert record["status_code"] is None
    assert record["error_type"] is None
    assert record["error_message"] is None
    assert record["checked_at"]


async def test_preflight_reports_a_capped_account_from_the_provider_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_probe(monkeypatch, raises=FakeAPIStatusError(400, CAPPED_ACCOUNT_BODY))

    record = await cycle.preflight_provider(make_config())

    assert record["status"] == "refused"
    assert record["status_code"] == 400
    assert record["error_type"] == "invalid_request_error"
    assert record["error_message"] == "You have reached your specified API usage limits..."
    assert FAKE_API_KEY not in json.dumps(record)


async def test_preflight_calls_a_bodyless_failure_an_error_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection that never reached the provider is the driver failing to ask."""
    install_probe(monkeypatch, raises=ConnectionError("no route to host"))

    record = await cycle.preflight_provider(make_config())

    assert record["status"] == "error"
    assert record["status_code"] is None
    assert record["error_type"] == "ConnectionError"
    assert record["error_message"] == "no route to host"


async def test_a_refusal_with_no_usable_body_never_quotes_the_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exception string renders the request, so a key can ride along in it."""
    install_probe(monkeypatch, raises=FakeAPIStatusError(502, {"error": "rate limited"}))

    record = await cycle.preflight_provider(make_config())

    assert record["status"] == "refused"
    assert record["status_code"] == 502
    assert record["error_type"] == "FakeAPIStatusError"
    assert record["error_message"] == "provider answered with no error body"
    assert FAKE_API_KEY not in json.dumps(record)


async def test_an_unresolvable_memory_surface_seals_instead_of_raising(
    tmp_path: Path, product: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed SIBYL_LLM_MEMORY_* binding must not strand the receipt."""
    fake = product(FakeProduct(sources=["s000"]))

    async def unresolvable() -> tuple[str, str]:
        raise RuntimeError("Unsupported LLM provider: nonesuch")

    monkeypatch.setattr(cycle, "preflight_provider", REAL_PREFLIGHT)
    monkeypatch.setattr(cycle, "_resolve_memory_llm", unresolvable)

    receipt = await cycle.run_cycle(make_config(expected_sources=1), tmp_path)

    assert fake.calls == []
    assert receipt["status"] == cycle.STATUS_PROVIDER_UNAVAILABLE
    assert receipt["reasons"] == ["provider_preflight_failed: RuntimeError"]
    assert receipt["provider_preflight"]["error_message"] == "Unsupported LLM provider: nonesuch"
    written = json.loads((tmp_path / "cycle.json").read_text(encoding="utf-8"))
    assert written["status"] == cycle.STATUS_PROVIDER_UNAVAILABLE, "no receipt left at running"


def test_a_long_provider_message_is_clipped() -> None:
    body = {"error": {"type": "overloaded_error", "message": "x" * 5_000}}

    record = cycle._provider_failure(FakeAPIStatusError(529, body))

    assert len(record["error_message"]) == cycle.PROVIDER_ERROR_MESSAGE_LIMIT


async def test_a_live_credential_proceeds_to_the_proposal_passes(
    tmp_path: Path, product: Any
) -> None:
    fake = product(FakeProduct(sources=["s000"]))

    receipt = await cycle.run_cycle(make_config(expected_sources=1), tmp_path)

    assert receipt["provider_preflight"]["status"] == "ok"
    assert len(fake.calls) == 1
    assert receipt["status"] == cycle.STATUS_COMPLETE


async def test_a_capped_key_stops_the_run_before_a_single_cohort_is_packed(
    tmp_path: Path, product: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure the first live cycle paid ten minutes to discover."""
    fake = product(FakeProduct(sources=[f"s{index:03d}" for index in range(233)]))
    install_probe(monkeypatch, raises=FakeAPIStatusError(400, CAPPED_ACCOUNT_BODY))

    receipt = await cycle.run_cycle(make_config(), tmp_path)

    assert fake.calls == [], "not one proposal pass may be dispatched"
    assert fake.repairs == 0
    assert receipt["status"] == cycle.STATUS_PROVIDER_UNAVAILABLE
    assert receipt["reasons"] == ["provider_preflight_failed: invalid_request_error"]
    assert receipt["invocations"] == 0
    assert "proposal" not in receipt
    assert "drain" not in receipt
    assert "embedding_repair" not in receipt

    preflight = receipt["provider_preflight"]
    assert preflight["status"] == "refused"
    assert preflight["status_code"] == 400
    assert preflight["error_type"] == "invalid_request_error"

    lines = (tmp_path / "invocations.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    written = json.loads(lines[0])
    assert written["phase"] == "provider_preflight"
    assert written["provider_preflight"]["status_code"] == 400


def test_a_refused_preflight_writes_no_key_material_anywhere(
    tmp_path: Path, product: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    product(FakeProduct(sources=["s000"]))
    install_probe(monkeypatch, raises=FakeAPIStatusError(400, CAPPED_ACCOUNT_BODY))
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_API_KEY)

    cycle.main(["--output", str(tmp_path), "--expected-sources", "1"])

    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert written, "the run must leave evidence to search"
    for path in written:
        assert FAKE_API_KEY not in path.read_text(encoding="utf-8"), path


async def test_the_skip_flag_dispatches_without_probing_the_provider(
    tmp_path: Path, product: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = product(FakeProduct(sources=["s000"]))
    probes = install_probe(monkeypatch)

    receipt = await cycle.run_cycle(
        make_config(expected_sources=1, skip_provider_preflight=True), tmp_path
    )

    assert probes == [], "the offline escape hatch must reach no provider"
    assert receipt["provider_preflight"] == {
        "status": "ok",
        "skipped": True,
        "checked_at": receipt["provider_preflight"]["checked_at"],
    }
    assert len(fake.calls) == 1
    assert receipt["status"] == cycle.STATUS_COMPLETE
    lines = (tmp_path / "invocations.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["phase"] for line in lines] == ["proposal"]


def test_the_skip_flag_is_off_by_default_and_reaches_the_config(tmp_path: Path) -> None:
    parser = cycle.build_parser()

    default = parser.parse_args(["--output", str(tmp_path)])
    skipped = parser.parse_args(["--output", str(tmp_path), "--skip-provider-preflight"])

    assert cycle.config_from_args(default).skip_provider_preflight is False
    assert cycle.config_from_args(skipped).skip_provider_preflight is True


def test_a_refused_provider_exits_non_zero(
    tmp_path: Path, product: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    product(FakeProduct(sources=["s000"]))
    install_probe(monkeypatch, raises=FakeAPIStatusError(400, CAPPED_ACCOUNT_BODY))

    assert cycle.main(["--output", str(tmp_path), "--expected-sources", "1"]) == 1
    assert cycle.EXIT_CODES[cycle.STATUS_PROVIDER_UNAVAILABLE] == 1
    assert cycle.EXIT_CODES[cycle.STATUS_COMPLETE] == 0


# ---------------------------------------------------------------------------
# Owned database
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeConnection:
    """Stands in for the unix-socket HTTP connection to dockerd."""

    def __init__(self, responses: dict[str, tuple[int, bytes]]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str]] = []

    def __call__(self, *_args: Any, **_kwargs: Any) -> _FakeConnection:
        return self

    def request(self, method: str, path: str, headers: dict[str, str] | None = None) -> None:
        assert headers is not None
        self.requests.append((method, path))
        self._pending = self.responses[path]

    def getresponse(self) -> _FakeResponse:
        return _FakeResponse(*self._pending)

    def close(self) -> None:
        return None


def _install_docker(monkeypatch: pytest.MonkeyPatch, name: str) -> _FakeConnection:
    document = json.dumps({"Name": name, "Id": owned_db.DEFAULT_CONTAINER_ID}).encode()
    connection = _FakeConnection(
        {
            f"/containers/{owned_db.DEFAULT_CONTAINER_ID}/json": (200, document),
            f"/containers/{owned_db.DEFAULT_CONTAINER_ID}/start": (204, b""),
            f"/containers/{owned_db.DEFAULT_CONTAINER_ID}/stop?t=30": (204, b""),
        }
    )
    monkeypatch.setattr(owned_db, "_UnixSocketConnection", connection)
    return connection


def test_owned_db_refuses_a_foreign_container(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _install_docker(monkeypatch, "/some-other-persons-database")

    with pytest.raises(owned_db.ForeignContainerError, match="does not start with"):
        owned_db.start()

    assert [method for method, _ in connection.requests] == ["GET"], "no start was sent"


def test_owned_db_starts_and_stops_the_owned_container(monkeypatch: pytest.MonkeyPatch) -> None:
    name = f"{owned_db.OWNED_NAME_PREFIX}613f4c5caa8fd156cb56df26-restored"
    connection = _install_docker(monkeypatch, name)

    assert owned_db.start()["Name"] == name
    assert owned_db.stop()["Name"] == name

    assert [path for _, path in connection.requests] == [
        f"/containers/{owned_db.DEFAULT_CONTAINER_ID}/json",
        f"/containers/{owned_db.DEFAULT_CONTAINER_ID}/start",
        f"/containers/{owned_db.DEFAULT_CONTAINER_ID}/json",
        f"/containers/{owned_db.DEFAULT_CONTAINER_ID}/stop?t=30",
        f"/containers/{owned_db.DEFAULT_CONTAINER_ID}/json",
    ]


def test_owned_db_escapes_a_container_id_into_one_path_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id carrying path syntax must not address a different endpoint."""
    name = f"{owned_db.OWNED_NAME_PREFIX}613f4c5caa8fd156cb56df26-restored"
    escaped = "..%2Fimages%2Fjson%3Fall%3D1"
    connection = _FakeConnection(
        {f"/containers/{escaped}/json": (200, json.dumps({"Name": name}).encode())}
    )
    monkeypatch.setattr(owned_db, "_UnixSocketConnection", connection)

    assert owned_db.inspect("../images/json?all=1")["Name"] == name
    assert connection.requests == [("GET", f"/containers/{escaped}/json")]


def test_stage_refuses_a_container_id_that_is_not_hex(tmp_path: Path) -> None:
    """stage.sh interpolates the id into a Docker path, so it validates first."""
    assert re.fullmatch(r"[0-9a-f]{12,64}", owned_db.DEFAULT_CONTAINER_ID)
    script = Path(owned_db.__file__).parent / "stage.sh"
    root = tmp_path / "runtime"

    result = subprocess.run(  # noqa: S603 - a fixed script path, no shell
        ["bash", str(script), "deadbeefdead", str(root)],  # noqa: S607
        env={**os.environ, "SCREEN48_OWNED_CONTAINER": "../images/json"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "not a hex container id" in result.stderr
    assert not root.exists(), "the guard runs before anything is cloned"


def test_owned_db_reads_the_health_port_from_the_ws_url() -> None:
    assert owned_db.health_port("ws://127.0.0.1:21642/rpc") == ("127.0.0.1", 21642)


def test_wait_ready_gives_up_with_the_last_probe_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owned_db, "probe_health", lambda *_a, **_k: 503)
    monkeypatch.setattr(owned_db.time, "sleep", lambda _seconds: None)

    with pytest.raises(owned_db.OwnedDatabaseError, match="never returned 200"):
        owned_db.wait_ready("ws://127.0.0.1:21642/rpc", timeout=0.01)


def test_wait_ready_returns_once_health_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owned_db, "probe_health", lambda *_a, **_k: 200)

    assert owned_db.wait_ready("ws://127.0.0.1:21642/rpc", timeout=1.0)["status"] == 200


# ---------------------------------------------------------------------------
# Phase runner
# ---------------------------------------------------------------------------


def test_run_phase_refuses_when_redis_is_configured() -> None:
    with pytest.raises(run_phase.PhaseError, match="SIBYL_REDIS_HOST"):
        run_phase.assert_no_redis({"SIBYL_REDIS_HOST": "localhost"})


def test_run_phase_accepts_an_environment_without_redis() -> None:
    run_phase.assert_no_redis({"SIBYL_SURREAL_URL": "ws://127.0.0.1:21642/rpc"})


def test_run_phase_main_refuses_redis_and_writes_a_phase_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIBYL_REDIS_HOST", "localhost")

    assert run_phase.main(["cycle", "--output", str(tmp_path)]) == 2

    record = json.loads((tmp_path / "phase.json").read_text(encoding="utf-8"))
    assert record["status"] == "error"
    assert "SIBYL_REDIS_HOST" in record["error"]
    assert record["container_inspect_before"] is None


def test_apply_environment_stamps_the_study_allowance() -> None:
    environ: dict[str, str] = {"SIBYL_SURREAL_PASSWORD": "kept"}

    names = run_phase.apply_environment(environ)

    assert environ["SIBYL_CONSOLIDATION_MAX_INPUT_CHARS"] == "800000"
    assert environ["SIBYL_COORDINATION_BACKEND"] == "local"
    assert environ["SIBYL_SURREAL_URL"] == run_phase.SURREAL_URL
    assert environ["SIBYL_LLM_MEMORY_MODEL"] == "claude-opus-5"
    assert "SIBYL_SURREAL_PASSWORD" in names
    assert "SIBYL_SURREAL_USERNAME" not in names


def test_phase_registry_carries_the_cycle_both_checkpoints_and_preflight() -> None:
    assert sorted(run_phase.PHASES) == ["checkpoint0", "checkpoint1", "cycle", "preflight"]


def test_long_context_rows_bill_at_the_doubled_tier() -> None:
    short = json.dumps({"input_tokens": 100_000, "output_tokens": 1_000, "requests": 1})
    long = json.dumps({"input_tokens": 250_000, "output_tokens": 1_000, "requests": 1})
    split = json.dumps({"input_tokens": 250_000, "output_tokens": 1_000, "requests": 2})
    price = Decimal(10)
    result = cycle.summarize_usage(
        [{"state": "returned", "usage_json": row} for row in (short, long, split)],
        price_input_per_million=price,
        price_output_per_million=price,
    )
    # short: 101K tokens at 10/M = 1.01; long: 251K at 20/M = 5.02; split: 251K at 10/M = 2.51
    assert result["cost_usd_exact"] == str(Decimal("1.01") + Decimal("5.02") + Decimal("2.51"))
