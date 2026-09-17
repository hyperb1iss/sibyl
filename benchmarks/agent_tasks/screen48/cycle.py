"""Drive one complete ordinary consolidation cycle over the 233 admitted captures.

The driver owns no memory logic. It calls the product job
``sibyl.jobs.reflection.run_reflection_dream_cycle`` in process, with the
product's own runtime bootstrap (schema, runtime settings, core runtime ports,
shared Surreal connectivity, local queue broker), and stops when every original
source is terminal. Every query the driver issues on its own account is read
only. The one deliberate write is the closing embedding repair, which calls the
product's ``repair_promoted_embeddings`` and so bills the embedding provider
and stamps ``entity.name_embedding``; it runs only when the cost guard is still
under its ceiling and the proposal loop never wrapped.

Two product behaviours shape the loop.

The dream source pager wraps. ``list_reflection_dream_source_memories`` keysets
on ``uuid > $cursor`` and, when a page is exhausted, resets the cursor to "" and
pages again from the start. On 233 sources a blind third pass of 100 would pull
33 fresh sources and then 67 already-consolidated ones, form new cohort digests
over them, and pay for the repeat. So before each proposal pass the driver reads
the dispatch cursor and counts eligible sources above it, then asks for exactly
``min(source_page, 100, remaining)`` and stops when nothing remains.

The count only protects the run if it matches what the product will accept. The
pager applies three filters in Python after its own SQL page, so the driver
hydrates the same rows and replays the same tests: lifecycle recallability, the
excluded capture surfaces (column, else ``metadata["capture_surface"]``), a
non-empty ``principal_id`` and non-blank ``raw_content``. The fourth product
test, the dream owner's ``pending`` callback, resolves project access and loads
an authorized snapshot per source, which is too many round trips to replay for
every remaining source before every pass. That leaves one residual over-count,
authority or visibility drift between the capture row and its snapshot, so
every proposal pass is also checked after the fact: the cursor must not move
backwards and no consolidated source may sit at or below the cursor the pass
started from. A pass that fails either check stops the run as
``stopped_ring_wrap``.

A candidate whose automatic correction returns ``pending`` /
``unresolved_progress`` keeps ``review_state='pending'`` forever and is
re-selected by every drain. The driver bounds drain passes and stops early when
a pass produces no state transition, reporting those ids as ``stuck_pending``.

A dead or spend-capped API key is invisible to the cost guard. The first live
cycle scanned 100 sources, spent ten minutes bin-packing cohorts, dispatched 17
proposals, and watched the provider refuse every one of them with an HTTP 400
spend cap; measured cost stayed at zero, so only the no-transition guard would
ever have stopped it. So the driver asks the provider whether it will answer
before it packs anything: one free Anthropic token count (or an OpenAI model
lookup) against the model the MEMORY surface resolves to. A refusal seals the
receipt as ``stopped_provider_unavailable`` in seconds and dispatches nothing.

Pricing defaults are the campaign's accepted Anthropic binding; see the module
constants below for the artifact path they are cited from.
"""

# Product imports are deliberately lazy. run_phase stamps SIBYL_* into the
# environment before the first sibyl import, and the unit tests import this
# module with no database or broker in reach.
# ruff: noqa: PLC0415

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

DEFAULT_GROUP_ID = "b60d61fd-d388-4cb0-9581-eca4f583544b"
DEFAULT_PRINCIPAL_ID = "91bd4035-be71-4e14-a43f-c861fc75699c"

EXPECTED_SOURCES = 233

#: The product clamps ``source_limit`` into ``[0, 100]``; asking for more is a lie.
PRODUCT_SOURCE_LIMIT_CEILING = 100

#: The product clamps ``candidate_limit`` into ``[0, 200]``.
PRODUCT_CANDIDATE_LIMIT_CEILING = 200

#: The campaign's accepted Anthropic pricing binding for ``claude-opus-5``, in
#: USD per million tokens, as enforced by the ordinary-count contract
#: (``prepared-bundle/entry/count_contract.py`` rejects any quote whose rate
#: strings are not exactly "5" and "25").
#:
#: Caveat carried from the witness: 5/25 is the base tier. The same hashed
#: document lists a long-context tier of 10/50 per million above a 200K-token
#: prompt, and consolidation requests in this campaign have run 235K-272K input
#: tokens. The campaign binds 5/25 unconditionally, so a token-priced row here
#: is a lower bound whenever its request crossed that threshold. Rows whose
#: provider usage arrives with ``cost_complete`` are unaffected.
PRICING_SOURCE: str | None = (
    "/Users/bliss/dev/eval-artifacts/sibyl/full-cohort-pricing-20260914/sources.json"
    " (sha256 22623e55aff2bd2ed5c7062148c774f05430b1b6e9a81503f459c839c9597327)"
)
DEFAULT_PRICE_INPUT_PER_MILLION: Decimal | None = Decimal("5")
DEFAULT_PRICE_OUTPUT_PER_MILLION: Decimal | None = Decimal("25")

#: The campaign's own recorded ceiling is 300 USD
#: (``ordinary-count-outcome-root-20260915-r4bxev1s/acceptance.json``); this
#: lower default is the screen48 diagnostic's own guard and is overridable.
DEFAULT_COST_CEILING_USD = Decimal("232.75")

_RECEIPT_NAME = "cycle.json"
_INVOCATIONS_NAME = "invocations.jsonl"

#: Keyset page size for the driver's own read-only scans.
_SCAN_PAGE = 512

STATUS_COMPLETE = "complete_original_cycle"
STATUS_INCOMPLETE = "incomplete_original_cycle"
STATUS_COST_CEILING = "stopped_cost_ceiling"
STATUS_RING_WRAP = "stopped_ring_wrap"
STATUS_PROVIDER_UNAVAILABLE = "stopped_provider_unavailable"

#: A provider error record carries only what the provider itself said, and the
#: message is clipped so a verbose body cannot bloat the receipt.
PROVIDER_ERROR_MESSAGE_LIMIT = 200

#: The proposal loop stops itself when a pass leaves the cursor and the
#: remaining count exactly where it found them.
STOP_CURSOR_STALLED = "cursor_did_not_advance"


class CycleError(RuntimeError):
    """The cycle could not be driven through product code paths."""


@dataclass(frozen=True, kw_only=True)
class CycleConfig:
    """Fixed policy for one consolidation-cycle run."""

    group_id: str = DEFAULT_GROUP_ID
    principal_id: str = DEFAULT_PRINCIPAL_ID
    source_page: int = PRODUCT_SOURCE_LIMIT_CEILING
    candidate_limit: int = PRODUCT_CANDIDATE_LIMIT_CEILING
    max_drain_passes: int = 6
    cost_ceiling_usd: Decimal
    price_input_per_million: Decimal
    price_output_per_million: Decimal
    expected_sources: int = EXPECTED_SOURCES
    skip_provider_preflight: bool = False


# --------------------------------------------------------------------------
# Pure policy
# --------------------------------------------------------------------------


def plan_source_page(remaining: int, source_page: int) -> int:
    """Return the source budget for one proposal pass, never past ``remaining``.

    Asking for more than remains is what makes the dream pager wrap onto
    already-consolidated sources and pay for them a second time.
    """
    return max(0, min(source_page, PRODUCT_SOURCE_LIMIT_CEILING, remaining))


def receipt_source_ids(receipt: dict[str, Any]) -> list[str]:
    """Collect every source id a product receipt says it consolidated.

    Mirrors the receipt's own ``sources_scanned`` accounting: cohort rows carry
    ``source_ids``, single-source rows carry ``source_id``.
    """
    identifiers: list[str] = []
    for item in receipt.get("sources") or []:
        if not isinstance(item, dict):
            continue
        values = item.get("source_ids")
        if not isinstance(values, list | tuple):
            values = [item.get("source_id")]
        identifiers.extend(str(value) for value in values if value is not None)
    return identifiers


def detect_ring_wrap(
    *, cursor_before: str, cursor_after: str, source_ids: Sequence[str]
) -> dict[str, Any] | None:
    """Prove after the fact that one proposal pass did not wrap the pager.

    Two observable signatures. A consolidated source at or below the cursor the
    pass started from is a source the run already paid for. A cursor that moved
    backwards is the pager having reset to "" and re-walked the ring, since
    ``advance_dream_cursor`` stamps whatever source id it is handed.
    """
    if not cursor_before:
        return None
    wrapped = sorted({identifier for identifier in source_ids if identifier <= cursor_before})
    if wrapped:
        return {
            "reason": "consolidated_source_at_or_below_cursor",
            "cursor_before": cursor_before,
            "cursor_after": cursor_after,
            "wrapped_source_ids": wrapped,
        }
    if cursor_after and cursor_after < cursor_before:
        return {
            "reason": "cursor_moved_backwards",
            "cursor_before": cursor_before,
            "cursor_after": cursor_after,
            "wrapped_source_ids": [],
        }
    return None


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


#: Requests whose input exceeds this many tokens bill at the long-context tier,
#: which the pricing witness lists at twice the base input and output rates.
LONG_CONTEXT_THRESHOLD_TOKENS = 200_000
LONG_CONTEXT_MULTIPLIER = Decimal(2)


def _token_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    price_input_per_million: Decimal,
    price_output_per_million: Decimal,
    requests: int = 1,
) -> Decimal:
    million = Decimal(1_000_000)
    per_request = input_tokens / max(requests, 1)
    tier = LONG_CONTEXT_MULTIPLIER if per_request > LONG_CONTEXT_THRESHOLD_TOKENS else Decimal(1)
    return (
        (
            Decimal(input_tokens) * price_input_per_million
            + Decimal(output_tokens) * price_output_per_million
        )
        * tier
        / million
    )


def summarize_usage(
    rows: Sequence[dict[str, Any]],
    *,
    price_input_per_million: Decimal,
    price_output_per_million: Decimal,
) -> dict[str, Any]:
    """Sum durable validation-execution usage into a priced total.

    ``cost_complete`` rows carry the provider's own ``cost_usd``. Everything
    else is priced from its token counts, and a ``FailedExtractionUsage`` row
    (no token fields at all) contributes zero of both.

    A transport attempt that timed out records no usage and so prices at zero,
    but the request was in flight when the client gave up and the provider
    almost certainly billed it. Those attempts are estimated separately in
    ``unrecorded_attempt_estimate_usd`` and never folded into the measured
    total.
    """
    totals = {
        "rows": 0,
        "rows_with_usage": 0,
        "rows_cost_complete": 0,
        "rows_priced_from_tokens": 0,
        "rows_unparsable": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    states: dict[str, int] = {}
    attempts: list[_UnrecordedAttempts] = []
    cost = Decimal(0)
    for row in rows:
        totals["rows"] += 1
        state = str(row.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
        raw = row.get("usage_json")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            totals["rows_unparsable"] += 1
            continue
        if not isinstance(payload, dict):
            totals["rows_unparsable"] += 1
            continue
        totals["rows_with_usage"] += 1
        input_tokens = _as_int(payload.get("input_tokens"))
        output_tokens = _as_int(payload.get("output_tokens"))
        requests = max(_as_int(payload.get("requests")), 1)
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        attempts.append(
            _UnrecordedAttempts(
                stage_kind=stage_kind(row),
                attempts=_unrecorded_attempts(payload),
                measured_input_tokens=input_tokens // requests if input_tokens else 0,
            )
        )
        cost_usd = payload.get("cost_usd")
        if payload.get("cost_complete") is True and isinstance(cost_usd, int | float | str):
            totals["rows_cost_complete"] += 1
            cost += Decimal(str(cost_usd))
            continue
        totals["rows_priced_from_tokens"] += 1
        cost += _token_cost(
            input_tokens,
            output_tokens,
            price_input_per_million=price_input_per_million,
            price_output_per_million=price_output_per_million,
            requests=requests,
        )
    unrecorded = estimate_unrecorded_attempts(
        attempts,
        price_input_per_million=price_input_per_million,
        price_output_per_million=price_output_per_million,
    )
    return {
        **totals,
        **unrecorded,
        "states": states,
        "cost_usd": float(cost),
        "cost_usd_exact": str(cost),
        "ceiling_cost_usd_exact": str(
            cost + Decimal(unrecorded["unrecorded_attempt_estimate_usd_exact"])
        ),
        "price_input_per_million": str(price_input_per_million),
        "price_output_per_million": str(price_output_per_million),
    }


@dataclass(frozen=True, kw_only=True)
class _UnrecordedAttempts:
    """One execution row's billable-but-unmeasured transport attempts."""

    stage_kind: str
    attempts: int
    measured_input_tokens: int


def stage_kind(row: dict[str, Any]) -> str:
    """Name the stage a row belongs to, from the request the product stored."""
    raw = row.get("request_json")
    if not isinstance(raw, str) or not raw.strip():
        return "unknown"
    try:
        request = json.loads(raw)
    except (TypeError, ValueError):
        return "unknown"
    kind = request.get("kind") if isinstance(request, dict) else None
    return kind if isinstance(kind, str) and kind else "unknown"


def _unrecorded_attempts(payload: dict[str, Any]) -> int:
    """Count HTTP attempts the extractor could not attribute token usage to."""
    attempts = payload.get("transport_attempts")
    if not isinstance(attempts, list):
        return 0
    return sum(
        1
        for attempt in attempts
        if not (isinstance(attempt, dict) and attempt.get("usage_known") is True)
    )


def estimate_unrecorded_attempts(
    rows: Sequence[_UnrecordedAttempts],
    *,
    price_input_per_million: Decimal,
    price_output_per_million: Decimal,
) -> dict[str, Any]:
    """Price attempts that were dispatched and billed but never measured.

    Input tokens are the only estimable side: a timed-out request produced no
    countable output. The row's own measured input tokens per request are the
    closest available proxy, because a row with a recorded success retried the
    same prompt. A row with no usage at all takes the mean input tokens per
    request of the rows of its own stage kind, then the run-wide mean. The
    stored ``request_json`` is not a proxy: it carries the evidence binding and
    digests, never the prompt. Nothing is estimated when the run measured no
    row at all, and the count of rows without a basis is reported.
    """
    stage_totals: dict[str, list[int]] = {}
    for row in rows:
        if row.measured_input_tokens:
            stage_totals.setdefault(row.stage_kind, []).append(row.measured_input_tokens)
    measured = [tokens for values in stage_totals.values() for tokens in values]
    run_mean = sum(measured) // len(measured) if measured else 0
    basis = {"measured_row": 0, "stage_kind_mean": 0, "run_mean": 0, "none": 0}
    estimate = Decimal(0)
    attempts = 0
    affected = 0
    for row in rows:
        if row.attempts <= 0:
            continue
        affected += 1
        attempts += row.attempts
        values = stage_totals.get(row.stage_kind) or []
        if row.measured_input_tokens:
            tokens, name = row.measured_input_tokens, "measured_row"
        elif values:
            tokens, name = sum(values) // len(values), "stage_kind_mean"
        elif run_mean:
            tokens, name = run_mean, "run_mean"
        else:
            tokens, name = 0, "none"
        basis[name] += 1
        estimate += row.attempts * _token_cost(
            tokens,
            0,
            price_input_per_million=price_input_per_million,
            price_output_per_million=price_output_per_million,
        )
    return {
        "rows_with_unrecorded_attempts": affected,
        "unrecorded_attempts": attempts,
        "unrecorded_attempt_estimate_usd": float(estimate),
        "unrecorded_attempt_estimate_usd_exact": str(estimate),
        "unrecorded_attempt_estimate_basis": basis,
    }


def terminal_status(
    predicates: dict[str, Any],
    *,
    stuck_pending: Sequence[str],
    cost_ceiling_hit: bool,
    ring_wrap: bool = False,
    proposal_stop: str | None = None,
) -> tuple[str, list[str]]:
    """Fold the four predicates plus the guards into one terminal status."""
    reasons: list[str] = [
        f"predicate_{name}_unsatisfied"
        for name in sorted(predicates)
        if predicates[name].get("satisfied") is not True
    ]
    if stuck_pending:
        reasons.append("stuck_pending_candidates")
    if proposal_stop:
        reasons.append(proposal_stop)
    if ring_wrap:
        return STATUS_RING_WRAP, ["ring_wrap_detected", *reasons]
    if cost_ceiling_hit:
        return STATUS_COST_CEILING, ["cost_ceiling_exceeded", *reasons]
    if reasons:
        return STATUS_INCOMPLETE, reasons
    return STATUS_COMPLETE, []


# --------------------------------------------------------------------------
# Product seams. Every database or product touch goes through one of these.
# --------------------------------------------------------------------------


def _excluded_capture_surfaces() -> list[str]:
    from sibyl_core.services.content_raw_recall import (
        _REFLECTION_DREAM_EXCLUDED_CAPTURE_SURFACES,
    )

    return sorted(_REFLECTION_DREAM_EXCLUDED_CAPTURE_SURFACES)


async def _content_rows(query: str, **params: object) -> list[dict[str, Any]]:
    from sibyl_core.services import content_client

    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(client, query, **params)
    return [dict(row) for row in rows]


async def bootstrap_runtime() -> None:
    """Install the product runtime the queued worker installs, in process."""
    from sibyl.config import settings as api_settings
    from sibyl.coordination.broker import get_broker, get_queue_backend
    from sibyl.runtime_services import (
        bootstrap_surreal_runtime_schemas,
        install_core_runtime_ports,
        install_llm_db_config_source,
        load_runtime_settings_from_db,
    )
    from sibyl.services.surreal_connectivity import initialize_shared_surreal_connectivity

    backend = api_settings.resolved_coordination_backend
    if backend != "local":
        raise CycleError(f"coordination backend must be local, resolved {backend!r}")
    await bootstrap_surreal_runtime_schemas()
    await load_runtime_settings_from_db()
    install_llm_db_config_source()
    install_core_runtime_ports()
    await initialize_shared_surreal_connectivity()
    if get_queue_backend() != "local":
        raise CycleError("queue backend must be local")
    # Embeddings enqueue during the candidate drain, not only in the repair
    # phase, so the broker has to be live before the first invocation or every
    # enqueue is swallowed into {"status": "failed"}.
    await get_broker().startup()


async def shutdown_runtime() -> None:
    """Drain the local queue, then release the shared Surreal clients."""
    from sibyl.coordination.broker import get_broker
    from sibyl.services.surreal_connectivity import stop_surreal_connectivity_monitor
    from sibyl_core.services.content_client import close_shared_surreal_content_client

    await get_broker().shutdown()
    await stop_surreal_connectivity_monitor()
    await close_shared_surreal_content_client()


async def _resolve_memory_llm() -> tuple[str, str]:
    """Resolve the MEMORY surface the product way and keep only its identity.

    The resolved config also carries the API key. Nothing here reads it: the
    preflight lets each SDK find its own credential in the environment, so no
    key value ever reaches a local variable, let alone the receipt.
    """
    from sibyl_core.ai.llm.config import LLMSurface, resolve_llm_config

    resolved = await resolve_llm_config(LLMSurface.MEMORY)
    return resolved.provider.value, resolved.model.value


def _probe_provider(provider: str, model: str) -> None:
    """Ask the provider whether it will answer, for free, and return nothing.

    Anthropic's token-count endpoint bills nothing and is refused by exactly
    the credentials that would refuse a real consolidation request, spend caps
    included. OpenAI has no free counter, so the cheapest live equivalent is a
    model lookup. Any other provider has no free probe the driver trusts, and
    refusing to guess is the point: ``--skip-provider-preflight`` is the named
    way past it.
    """
    if provider == "anthropic":
        import anthropic

        # The SDK finds its own credential in ANTHROPIC_API_KEY, so no key value
        # passes through this driver. The product resolver also accepts
        # SIBYL_ANTHROPIC_API_KEY, which the SDK does not read: an environment
        # that binds only the prefixed name must skip the preflight.
        anthropic.Anthropic().messages.count_tokens(
            model=model, messages=[{"role": "user", "content": "ping"}]
        )
        return
    if provider == "openai":
        import openai

        openai.OpenAI().models.retrieve(model)
        return
    raise CycleError(f"no free provider preflight for {provider!r}")


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _provider_failure(exc: BaseException) -> dict[str, Any]:
    """Describe a refused probe using only what the provider itself said.

    Two fields are read off the exception: the status code it answered with,
    and the ``error.type`` / ``error.message`` pair inside its body. The
    request, its headers and the credential are never in reach, and the
    exception's own string is the last resort for providers that raise before
    any body exists. A status code means the provider answered and said no;
    anything else is the driver failing to ask.
    """
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        status_code = None
    error_type: str | None = None
    message: str | None = None
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            error_type = _text(error.get("type"))
            message = _text(error.get("message"))
    if message is None:
        # A provider that answered has already said everything it is going to
        # say. Its transport's rendering of the exception adds only the request,
        # which is the one place a credential could ride along into the receipt.
        message = "provider answered with no error body" if status_code else str(exc)
    return {
        "status": "refused" if status_code is not None else "error",
        "status_code": status_code,
        "error_type": error_type or type(exc).__name__,
        "error_message": message[:PROVIDER_ERROR_MESSAGE_LIMIT],
    }


async def preflight_provider(config: CycleConfig) -> dict[str, Any]:
    """Prove the configured LLM credential is live before anything is packed.

    Returns the record the receipt carries: never the key, never the request,
    only the provider's own verdict. ``config`` is accepted so the probe reads
    like every other seam and can grow policy without a signature change.
    """
    import asyncio

    provider, model = await _resolve_memory_llm()
    outcome: dict[str, Any] = {
        "status": "ok",
        "status_code": None,
        "error_type": None,
        "error_message": None,
    }
    try:
        await asyncio.to_thread(_probe_provider, provider, model)
    except Exception as exc:  # every refusal shape is evidence, not a crash
        outcome = _provider_failure(exc)
    return {
        "provider": provider,
        "model": model,
        **outcome,
        "checked_at": datetime.now(UTC).isoformat(),
    }


async def broker_health() -> dict[str, Any]:
    from sibyl.coordination.broker import get_broker

    return dict(await get_broker().health())


async def invoke_dream_cycle(
    *, group_id: str, source_limit: int, candidate_limit: int
) -> dict[str, Any]:
    """Run the product job once. ``ctx`` is ignored by the job itself."""
    from sibyl.jobs.reflection import run_reflection_dream_cycle

    return await run_reflection_dream_cycle(
        {},
        group_id,
        dry_run=False,
        source_limit=source_limit,
        candidate_limit=candidate_limit,
    )


async def load_cursor(group_id: str) -> tuple[str, int]:
    from sibyl_core.services.dream_checkpoints import load_dream_cursor

    return await load_dream_cursor(group_id)


def product_accepts_source(memory: Any, *, excluded: Sequence[str]) -> bool:
    """Replay every cheap filter the product applies after its own SQL page.

    ``list_reflection_dream_source_memories`` hydrates each row and keeps it
    only if it is currently recallable and its resolved capture surface is not
    excluded; the dream owner's ``pending`` callback then drops any source with
    no principal or blank content before it ever loads a snapshot. Counting a
    row the product will drop is what makes a pass ask for more than remains
    and wrap the pager onto already-consolidated sources.
    """
    from sibyl_core.services.content_models import (
        raw_memory_capture_surface,
        raw_memory_currently_recallable,
    )

    return (
        raw_memory_currently_recallable(memory)
        and raw_memory_capture_surface(memory) not in set(excluded)
        and bool(memory.principal_id)
        and bool(memory.raw_content.strip())
    )


async def eligible_source_ids_above(group_id: str, cursor: str) -> list[str]:
    """List the captures above the cursor the product pager would accept.

    Same SQL predicate as the pager, keyset paged rather than limited, then
    hydrated through the product's own ``raw_memory_from_record`` so the
    Python-side filters see exactly what the pager sees.
    """
    from sibyl_core.services.content_models import raw_memory_from_record

    excluded = _excluded_capture_surfaces()
    accepted: list[str] = []
    scan = cursor
    while True:
        rows = await _content_rows(
            "SELECT * FROM raw_captures "
            "WHERE organization_id = $org AND uuid > $cursor "
            "AND (capture_surface NOT IN $excluded OR capture_surface = NONE) "
            "ORDER BY uuid ASC LIMIT $limit;",
            org=group_id,
            cursor=scan,
            excluded=excluded,
            limit=_SCAN_PAGE,
        )
        if not rows:
            return accepted
        scan = str(rows[-1]["uuid"])
        accepted.extend(
            memory.id
            for memory in (raw_memory_from_record(row) for row in rows)
            if product_accepts_source(memory, excluded=excluded)
        )
        if len(rows) < _SCAN_PAGE:
            return accepted


async def count_eligible_sources(group_id: str, cursor: str) -> int:
    """Count the dream-eligible captures above the dispatch cursor.

    Exact rather than optimistic: a SQL ``count()`` over the pager's predicate
    alone over-counts by every row the product drops in Python, and each
    over-count is a source budget the pager fills by wrapping.
    """
    return len(await eligible_source_ids_above(group_id, cursor))


async def list_eligible_source_ids(group_id: str) -> list[str]:
    """Enumerate every dream-eligible capture id, keyset paged like the product."""
    identifiers: list[str] = []
    cursor = ""
    while True:
        rows = await _content_rows(
            "SELECT uuid FROM raw_captures "
            "WHERE organization_id = $org AND uuid > $cursor "
            "AND (capture_surface NOT IN $excluded OR capture_surface = NONE) "
            "ORDER BY uuid ASC LIMIT $limit;",
            org=group_id,
            cursor=cursor,
            excluded=_excluded_capture_surfaces(),
            limit=_SCAN_PAGE,
        )
        if not rows:
            return identifiers
        identifiers.extend(str(row["uuid"]) for row in rows)
        cursor = str(rows[-1]["uuid"])
        if len(rows) < _SCAN_PAGE:
            return identifiers


async def pending_candidate_ids(group_id: str, *, limit: int = 500) -> list[str]:
    from sibyl_core.services.surreal_content import list_reflection_candidate_reviews

    candidates = await list_reflection_candidate_reviews(
        organization_id=group_id, review_state="pending", limit=limit
    )
    return [candidate.id for candidate in candidates]


async def live_execution_count(group_id: str) -> int:
    rows = await _content_rows(
        "SELECT count() AS total FROM memory_validation_executions "
        "WHERE organization_id = $org AND state IN ['running', 'recorded'] GROUP ALL;",
        org=group_id,
    )
    return _as_int(rows[0].get("total")) if rows else 0


async def usage_rows(group_id: str, since: datetime) -> list[dict[str, Any]]:
    return await _content_rows(
        "SELECT usage_json, request_json, state, created_at "
        "FROM memory_validation_executions "
        "WHERE organization_id = $org AND created_at >= $since;",
        org=group_id,
        since=since,
    )


async def capture_surface_counts(group_id: str) -> dict[str, int]:
    rows = await _content_rows(
        "SELECT capture_surface, count() AS total FROM raw_captures "
        "WHERE organization_id = $org GROUP BY capture_surface;",
        org=group_id,
    )
    return {str(row.get("capture_surface")): _as_int(row.get("total")) for row in rows}


async def review_state_counts(group_id: str) -> dict[str, int]:
    rows = await _content_rows(
        "SELECT review_state, count() AS total FROM raw_captures "
        "WHERE organization_id = $org GROUP BY review_state;",
        org=group_id,
    )
    return {str(row.get("review_state")): _as_int(row.get("total")) for row in rows}


async def returned_execution_source_ids(group_id: str) -> set[str]:
    rows = await _content_rows(
        "SELECT source_ids FROM memory_validation_executions "
        "WHERE organization_id = $org AND state = 'returned';",
        org=group_id,
    )
    covered: set[str] = set()
    for row in rows:
        value = row.get("source_ids")
        if isinstance(value, list | tuple):
            covered.update(str(item) for item in value)
    return covered


async def completed_checkpoint_source_ids(group_id: str) -> set[str]:
    rows = await _content_rows(
        "SELECT source_id FROM dream_source_checkpoints "
        "WHERE organization_id = $org AND completion_json != NONE;",
        org=group_id,
    )
    return {str(row["source_id"]) for row in rows if row.get("source_id")}


async def graph_runtime(group_id: str):
    from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

    return await get_surreal_graph_runtime(group_id)


async def entity_count(group_id: str) -> int:
    from sibyl_core.services.graph_common import normalize_graph_records

    runtime = await graph_runtime(group_id)
    rows = normalize_graph_records(
        await runtime.client.execute_query(
            "SELECT count() AS total FROM entity WHERE group_id = $group_id GROUP ALL;",
            group_id=group_id,
        )
    )
    return _as_int(dict(rows[0]).get("total")) if rows else 0


async def missing_embedding_candidate_ids(group_id: str) -> list[str]:
    """Scan promoted candidates whose ``name_embedding`` is absent or stale.

    Same projection and same currency test as ``repair_promoted_embeddings``,
    read only: this is predicate (c), not a repair.
    """
    from sibyl_core.embeddings.providers import configured_embedding_provider
    from sibyl_core.services.graph_common import normalize_graph_records
    from sibyl_core.services.memory_embedding import _embedding_current

    provider = configured_embedding_provider()
    if provider is None:
        return []
    runtime = await graph_runtime(group_id)
    missing: list[str] = []
    cursor = ""
    while True:
        rows = [
            dict(row)
            for row in normalize_graph_records(
                await runtime.client.execute_query(
                    "SELECT uuid, (name_embedding != NONE) AS embedding_present, "
                    "attributes.embedding_metadata AS embedding_metadata "
                    "FROM entity WITH INDEX idx_entity_reflection_candidate_uuid "
                    "WHERE group_id=$group_id AND uuid > $cursor AND derivation_required=true "
                    "AND attributes.reflection_identity.purpose='candidate' "
                    "AND attributes.operational_source_id IS NONE "
                    "ORDER BY uuid LIMIT $limit;",
                    group_id=group_id,
                    cursor=cursor,
                    limit=_SCAN_PAGE,
                )
            )
        ]
        if not rows:
            return missing
        cursor = str(rows[-1]["uuid"])
        missing.extend(
            str(row["uuid"])
            for row in rows
            if not _embedding_current(
                row.get("embedding_present") is True,
                row.get("embedding_metadata"),
                provider.metadata,
            )
        )
        if len(rows) < _SCAN_PAGE:
            return missing


async def repair_embeddings(group_id: str) -> dict[str, int]:
    """Re-enqueue lost embedding work through the product repair path."""
    from dataclasses import asdict

    from sibyl_core.embeddings.providers import configured_embedding_provider
    from sibyl_core.services.memory_embedding import repair_promoted_embeddings

    if configured_embedding_provider() is None:
        return {"checked": 0, "recovered": 0, "pending": 0, "failed": 0, "disabled": 1}
    runtime = await graph_runtime(group_id)
    return dict(asdict(await repair_promoted_embeddings(runtime)))


# --------------------------------------------------------------------------
# Receipt assembly
# --------------------------------------------------------------------------


def _archived_exception_reasons(receipts: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Count auto-archived route-to-review exceptions by reason.

    These are lost memories, not parked ones, so the receipt names them.
    """
    counts: dict[str, int] = {}
    for receipt in receipts:
        for candidate in receipt.get("candidates") or []:
            if candidate.get("archived") is not True:
                continue
            reasons = candidate.get("exception_reasons") or []
            named = [str(reason) for reason in reasons] or [
                str(candidate.get("reason") or "unnamed")
            ]
            for reason in named:
                counts[reason] = counts.get(reason, 0) + 1
    return counts


def _embedding_statuses(receipts: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for receipt in receipts:
        for candidate in receipt.get("candidates") or []:
            backfill = candidate.get("embedding_backfill")
            if isinstance(backfill, dict):
                status = str(backfill.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
    return counts


class _Evidence:
    """Durable partial evidence: one appended line per product invocation."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.receipt_path = output / _RECEIPT_NAME
        self.invocations_path = output / _INVOCATIONS_NAME
        self.records: list[dict[str, Any]] = []
        #: Every line the file carries, invocations and driver probes alike.
        self.lines = 0

    def reserve(self, header: dict[str, Any]) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        try:
            with self.receipt_path.open("x", encoding="utf-8") as handle:
                json.dump({**header, "status": "running"}, handle, indent=2, sort_keys=True)
                handle.write("\n")
        except FileExistsError as exc:
            raise CycleError(f"{self.receipt_path} already exists; refusing to overwrite") from exc

    def _write(self, record: dict[str, Any]) -> dict[str, Any]:
        stamped = {"index": self.lines, **record}
        with self.invocations_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stamped, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.lines += 1
        return stamped

    def note(self, record: dict[str, Any]) -> None:
        """Record a driver-side probe, which is not a product invocation.

        The line is durable like any other; ``records`` stays the count of
        product invocations, which the receipt and the usage roll-up read.
        """
        self._write(record)

    def append(self, record: dict[str, Any]) -> None:
        self.records.append(self._write(record))

    def finish(self, receipt: dict[str, Any]) -> None:
        with self.receipt_path.open("w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class _CostGuard:
    """Stop dispatching once measured spend passes the ceiling."""

    def __init__(self, config: CycleConfig, started: datetime) -> None:
        self.config = config
        self.started = started
        self.usage: dict[str, Any] = summarize_usage(
            [],
            price_input_per_million=config.price_input_per_million,
            price_output_per_million=config.price_output_per_million,
        )
        self.exceeded = False

    async def measure(self) -> dict[str, Any]:
        rows = await usage_rows(self.config.group_id, self.started)
        self.usage = summarize_usage(
            rows,
            price_input_per_million=self.config.price_input_per_million,
            price_output_per_million=self.config.price_output_per_million,
        )
        # The ceiling counts the estimate for billed-but-unmeasured attempts,
        # so a run whose spend hid in timeouts still stops.
        if Decimal(self.usage["ceiling_cost_usd_exact"]) > self.config.cost_ceiling_usd:
            self.exceeded = True
        return self.usage


async def _snapshot(config: CycleConfig) -> dict[str, Any]:
    cursor_id, cursor_revision = await load_cursor(config.group_id)
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "capture_surface_counts": await capture_surface_counts(config.group_id),
        "review_state_counts": await review_state_counts(config.group_id),
        "entity_count": await entity_count(config.group_id),
        "dream_cursor": {"source_id": cursor_id, "revision": cursor_revision},
        "eligible_sources_above_cursor": await count_eligible_sources(config.group_id, cursor_id),
        "live_executions": await live_execution_count(config.group_id),
        "pending_candidates": len(await pending_candidate_ids(config.group_id)),
    }


async def _run_invocation(
    config: CycleConfig,
    evidence: _Evidence,
    guard: _CostGuard,
    *,
    phase: str,
    source_limit: int,
    context: dict[str, Any],
) -> dict[str, Any]:
    started = datetime.now(UTC)
    receipt = await invoke_dream_cycle(
        group_id=config.group_id,
        source_limit=source_limit,
        candidate_limit=config.candidate_limit,
    )
    finished = datetime.now(UTC)
    usage = await guard.measure()
    evidence.append(
        {
            "phase": phase,
            "requested_source_limit": source_limit,
            "requested_candidate_limit": config.candidate_limit,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "measured_cost_usd": usage["cost_usd_exact"],
            "unrecorded_attempt_estimate_usd": usage["unrecorded_attempt_estimate_usd_exact"],
            "ceiling_cost_usd": usage["ceiling_cost_usd_exact"],
            "cost_ceiling_usd": str(config.cost_ceiling_usd),
            "cost_ceiling_exceeded": guard.exceeded,
            "context": context,
            "receipt": receipt,
        }
    )
    return receipt


async def _proposal_passes(
    config: CycleConfig, evidence: _Evidence, guard: _CostGuard
) -> dict[str, Any]:
    """Propose over every remaining source without ever wrapping the pager."""
    passes: list[dict[str, Any]] = []
    ring_wrap: dict[str, Any] | None = None
    stopped: str | None = None
    previous: tuple[str, int] | None = None
    while True:
        cursor_id, cursor_revision = await load_cursor(config.group_id)
        remaining = await count_eligible_sources(config.group_id, cursor_id)
        # A cursor-revision conflict leaves the cursor exactly where the pass
        # found it, and the next pass would buy the same page again.
        if previous == (cursor_id, remaining):
            stopped = STOP_CURSOR_STALLED
            break
        previous = (cursor_id, remaining)
        budget = plan_source_page(remaining, config.source_page)
        if budget <= 0:
            break
        context = {
            "cursor_source_id": cursor_id,
            "cursor_revision": cursor_revision,
            "remaining_above_cursor": remaining,
        }
        receipt = await _run_invocation(
            config,
            evidence,
            guard,
            phase="proposal",
            source_limit=budget,
            context=context,
        )
        consolidated = receipt_source_ids(receipt)
        cursor_after, revision_after = await load_cursor(config.group_id)
        ring_wrap = detect_ring_wrap(
            cursor_before=cursor_id, cursor_after=cursor_after, source_ids=consolidated
        )
        passes.append(
            {
                **context,
                "source_limit": budget,
                "sources_scanned": receipt.get("sources_scanned"),
                "sources_reflected": receipt.get("sources_reflected"),
                "run_id": receipt.get("run_id"),
                "cursor_source_id_after": cursor_after,
                "cursor_revision_after": revision_after,
                "consolidated_sources": len(consolidated),
                "ring_wrap": ring_wrap,
            }
        )
        if ring_wrap is not None:
            break
        if guard.exceeded:
            break
        if len(passes) > config.expected_sources:
            raise CycleError("proposal passes exceeded the source count; refusing to spin")
    cursor_id, _ = await load_cursor(config.group_id)
    return {
        "passes": passes,
        "remaining_above_cursor": await count_eligible_sources(config.group_id, cursor_id),
        "ring_wrap": ring_wrap,
        "stopped_reason": stopped,
    }


async def _drain_passes(
    config: CycleConfig, evidence: _Evidence, guard: _CostGuard
) -> dict[str, Any]:
    """Drain pending candidates with ``source_limit=0`` until nothing moves."""
    passes: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    stuck: list[str] = []
    for index in range(config.max_drain_passes):
        before = set(await pending_candidate_ids(config.group_id))
        for candidate_id in before:
            seen[candidate_id] = seen.get(candidate_id, 0) + 1
        if not before:
            break
        receipt = await _run_invocation(
            config,
            evidence,
            guard,
            phase="drain",
            source_limit=0,
            context={"pass_index": index, "pending_before": len(before)},
        )
        after = set(await pending_candidate_ids(config.group_id))
        passes.append(
            {
                "pass_index": index,
                "pending_before": sorted(before),
                "pending_after": sorted(after),
                "candidates_scanned": receipt.get("candidates_scanned"),
                "promoted": receipt.get("promoted"),
                "archived": receipt.get("archived"),
                "run_id": receipt.get("run_id"),
            }
        )
        if guard.exceeded:
            break
        if after == before:
            stuck = sorted(after)
            break
    return {
        "passes": passes,
        "stuck_pending": stuck,
        "pending_seen_counts": dict(sorted(seen.items())),
        "exhausted_pass_budget": len(passes) >= config.max_drain_passes and not stuck,
    }


async def _predicates(config: CycleConfig, originals: Sequence[str]) -> dict[str, Any]:
    pending = await pending_candidate_ids(config.group_id)
    live = await live_execution_count(config.group_id)
    missing = await missing_embedding_candidate_ids(config.group_id)
    covered = await returned_execution_source_ids(config.group_id)
    completed = await completed_checkpoint_source_ids(config.group_id)
    uncovered = sorted(set(originals) - covered - completed)
    return {
        "a_no_pending_candidates": {"satisfied": not pending, "pending_ids": pending},
        "b_no_live_executions": {"satisfied": live == 0, "live_executions": live},
        "c_no_missing_embeddings": {
            "satisfied": not missing,
            "missing_entity_ids": missing,
        },
        "d_every_source_terminal": {
            "satisfied": not uncovered and len(originals) == config.expected_sources,
            "original_source_count": len(originals),
            "expected_sources": config.expected_sources,
            "covered_by_returned_execution": len(set(originals) & covered),
            "covered_by_checkpoint_completion": len(set(originals) & completed),
            "uncovered_source_ids": uncovered,
        },
    }


def _header(config: CycleConfig, started: datetime) -> dict[str, Any]:
    return {
        "group_id": config.group_id,
        "principal_id": config.principal_id,
        "started_at": started.isoformat(),
        "config": {
            "source_page": config.source_page,
            "candidate_limit": config.candidate_limit,
            "max_drain_passes": config.max_drain_passes,
            "cost_ceiling_usd": str(config.cost_ceiling_usd),
            "price_input_per_million": str(config.price_input_per_million),
            "price_output_per_million": str(config.price_output_per_million),
            "expected_sources": config.expected_sources,
            "pricing_source": PRICING_SOURCE,
        },
    }


async def _dispatch_phases(
    config: CycleConfig, evidence: _Evidence, guard: _CostGuard, receipt: dict[str, Any]
) -> None:
    receipt["original_source_ids"] = await list_eligible_source_ids(config.group_id)
    receipt["snapshot_before"] = await _snapshot(config)
    receipt["proposal"] = await _proposal_passes(config, evidence, guard)
    wrapped = receipt["proposal"]["ring_wrap"] is not None
    if not guard.exceeded and not wrapped:
        receipt["drain"] = await _drain_passes(config, evidence, guard)
        # Embedding repair is the driver's one deliberate product write, and it
        # calls the embedding provider, so a tripped ceiling has to skip it.
        receipt["embedding_repair"] = await repair_embeddings(config.group_id)
    receipt["broker_health"] = await broker_health()


async def _finalize(
    config: CycleConfig, evidence: _Evidence, guard: _CostGuard, receipt: dict[str, Any]
) -> None:
    """Read the after-state while the runtime is still up."""
    receipt["snapshot_after"] = await _snapshot(config)
    receipt["predicates"] = await _predicates(config, receipt.get("original_source_ids") or [])
    receipt["usage"] = await guard.measure()
    invocation_receipts = [record["receipt"] for record in evidence.records]
    receipt["archived_exception_reasons"] = _archived_exception_reasons(invocation_receipts)
    receipt["embedding_enqueue_statuses"] = _embedding_statuses(invocation_receipts)


def _seal(
    receipt: dict[str, Any],
    guard: _CostGuard,
    evidence: _Evidence,
    errors: list[str],
    *,
    provider_unavailable: dict[str, Any] | None = None,
) -> None:
    if provider_unavailable is not None:
        _seal_provider_unavailable(receipt, guard, evidence, errors, provider_unavailable)
        return
    drain = receipt.get("drain") or {}
    proposal = receipt.get("proposal") or {}
    ring_wrap = proposal.get("ring_wrap") is not None
    predicates = receipt.get("predicates")
    if not isinstance(predicates, dict) or not predicates:
        predicates = {"a_no_pending_candidates": {"satisfied": False}}
    status, reasons = terminal_status(
        predicates,
        stuck_pending=drain.get("stuck_pending") or [],
        cost_ceiling_hit=guard.exceeded,
        ring_wrap=ring_wrap,
        proposal_stop=proposal.get("stopped_reason"),
    )
    if errors:
        if ring_wrap:
            status = STATUS_RING_WRAP
        else:
            status = STATUS_COST_CEILING if guard.exceeded else STATUS_INCOMPLETE
        reasons = [*reasons, *(f"driver_error:{error}" for error in errors)]
    _stamp_outcome(receipt, guard, evidence, errors, status=status, reasons=reasons)


def _stamp_outcome(
    receipt: dict[str, Any],
    guard: _CostGuard,
    evidence: _Evidence,
    errors: list[str],
    *,
    status: str,
    reasons: list[str],
) -> None:
    receipt.setdefault("usage", guard.usage)
    receipt["errors"] = errors
    receipt["invocations"] = len(evidence.records)
    receipt["status"] = status
    receipt["reasons"] = reasons
    receipt["finished_at"] = datetime.now(UTC).isoformat()


def _seal_provider_unavailable(
    receipt: dict[str, Any],
    guard: _CostGuard,
    evidence: _Evidence,
    errors: list[str],
    preflight: dict[str, Any],
) -> None:
    """Seal a run the provider refused before it cost anything.

    No predicate is folded in. Nothing was dispatched, so every predicate would
    read as unsatisfied and bury the one fact that matters under four that are
    merely downstream of it.
    """
    reasons = [f"provider_preflight_failed: {preflight.get('error_type')}"]
    reasons.extend(f"driver_error:{error}" for error in errors)
    _stamp_outcome(
        receipt,
        guard,
        evidence,
        errors,
        status=STATUS_PROVIDER_UNAVAILABLE,
        reasons=reasons,
    )


async def _preflight_phase(config: CycleConfig, evidence: _Evidence) -> dict[str, Any]:
    """Probe the provider once, durably, before the first cohort is packed.

    A probe that cannot even be built is still a stop, not a crash: resolving
    the MEMORY surface raises on a malformed ``SIBYL_LLM_MEMORY_*`` binding,
    and letting that escape would leave the reserved receipt at "running" with
    no evidence of why.
    """
    if config.skip_provider_preflight:
        return {
            "status": "ok",
            "skipped": True,
            "checked_at": datetime.now(UTC).isoformat(),
        }
    try:
        record = await preflight_provider(config)
    except Exception as exc:
        record = {
            "provider": None,
            "model": None,
            "status": "error",
            "status_code": None,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:PROVIDER_ERROR_MESSAGE_LIMIT],
            "checked_at": datetime.now(UTC).isoformat(),
        }
    evidence.note({"phase": "provider_preflight", "provider_preflight": record})
    return record


async def run_cycle(config: CycleConfig, output: Path) -> dict[str, Any]:
    """Drive the ordinary consolidation cycle and write a durable receipt."""
    started = datetime.now(UTC)
    evidence = _Evidence(output)
    receipt: dict[str, Any] = dict(_header(config, started))
    evidence.reserve(receipt)
    guard = _CostGuard(config, started)
    errors: list[str] = []
    refusal: dict[str, Any] | None = None
    try:
        await bootstrap_runtime()
        preflight = await _preflight_phase(config, evidence)
        receipt["provider_preflight"] = preflight
        if preflight["status"] == "ok":
            try:
                await _dispatch_phases(config, evidence, guard, receipt)
            except Exception as exc:  # a dispatch failure still owes evidence
                errors.append(f"{type(exc).__name__}: {exc}")
            try:
                await _finalize(config, evidence, guard, receipt)
            except Exception as exc:  # so does a read-back failure
                errors.append(f"finalize:{type(exc).__name__}: {exc}")
        else:
            # Nothing is dispatched, nothing is drained, nothing is repaired.
            # Stopping the database is the phase runner's job, so returning is
            # the whole of the driver's part.
            refusal = preflight
    finally:
        try:
            await shutdown_runtime()
        except Exception as exc:  # a drain failure must not eat the receipt
            errors.append(f"shutdown:{type(exc).__name__}: {exc}")
    _seal(receipt, guard, evidence, errors, provider_unavailable=refusal)
    evidence.finish(receipt)
    return receipt


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screen48-cycle", description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--ceiling-usd",
        type=Decimal,
        default=DEFAULT_COST_CEILING_USD,
        help="stop dispatching once measured spend passes this",
    )
    parser.add_argument(
        "--price-input",
        type=Decimal,
        default=DEFAULT_PRICE_INPUT_PER_MILLION,
        required=DEFAULT_PRICE_INPUT_PER_MILLION is None,
        help="USD per million input tokens",
    )
    parser.add_argument(
        "--price-output",
        type=Decimal,
        default=DEFAULT_PRICE_OUTPUT_PER_MILLION,
        required=DEFAULT_PRICE_OUTPUT_PER_MILLION is None,
        help="USD per million output tokens",
    )
    parser.add_argument("--group-id", default=DEFAULT_GROUP_ID)
    parser.add_argument("--principal-id", default=DEFAULT_PRINCIPAL_ID)
    parser.add_argument("--source-page", type=int, default=PRODUCT_SOURCE_LIMIT_CEILING)
    parser.add_argument("--candidate-limit", type=int, default=PRODUCT_CANDIDATE_LIMIT_CEILING)
    parser.add_argument("--max-drain-passes", type=int, default=6)
    parser.add_argument("--expected-sources", type=int, default=EXPECTED_SOURCES)
    parser.add_argument(
        "--skip-provider-preflight",
        action="store_true",
        help=(
            "dispatch without proving the LLM credential is live; needed offline, "
            "for a provider with no free probe (gemini), and wherever the key is "
            "bound only as SIBYL_ANTHROPIC_API_KEY, which the SDK cannot see"
        ),
    )
    return parser


def config_from_args(args: argparse.Namespace) -> CycleConfig:
    return CycleConfig(
        group_id=args.group_id,
        principal_id=args.principal_id,
        source_page=args.source_page,
        candidate_limit=args.candidate_limit,
        max_drain_passes=args.max_drain_passes,
        cost_ceiling_usd=args.ceiling_usd,
        price_input_per_million=args.price_input,
        price_output_per_million=args.price_output,
        expected_sources=args.expected_sources,
        skip_provider_preflight=args.skip_provider_preflight,
    )


#: Only a cycle that satisfied every predicate exits clean. Each guard status
#: is spelled out so the phase runner's caller can read the contract here
#: rather than infer it from a fallback.
EXIT_CODES: dict[str, int] = {
    STATUS_COMPLETE: 0,
    STATUS_INCOMPLETE: 1,
    STATUS_COST_CEILING: 1,
    STATUS_RING_WRAP: 1,
    STATUS_PROVIDER_UNAVAILABLE: 1,
}


def main(argv: Sequence[str] | None = None) -> int:
    import asyncio

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        receipt = asyncio.run(run_cycle(config_from_args(args), args.output))
    except CycleError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stdout.write(
        json.dumps(
            {
                "status": receipt["status"],
                "reasons": receipt["reasons"],
                "invocations": receipt["invocations"],
                "cost_usd": receipt["usage"]["cost_usd_exact"],
                "receipt": str(args.output / _RECEIPT_NAME),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return EXIT_CODES.get(receipt["status"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
