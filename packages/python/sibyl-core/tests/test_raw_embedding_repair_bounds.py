"""The raw embedding repair is bounded, resumable, leased, and never blames a busy provider.

These run on the embedded engine through the ``content_store`` fixture; the
live module repeats the budget and refusal cases on a native server.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest

from sibyl_core.ai.bedrock import BedrockSettings
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.schema_embedding_states import (
    embedding_state_key,
    raw_embedding_refusal_key,
)
from sibyl_core.embeddings import bedrock as bedrock_module
from sibyl_core.embeddings.bedrock import BedrockEmbeddingProvider
from sibyl_core.embeddings.provenance import vector_space
from sibyl_core.embeddings.providers import CachedEmbeddingProvider, EmbeddingMetadata
from sibyl_core.services import content_client
from sibyl_core.services import content_raw_embedding_repair as repair_module
from sibyl_core.services.content_models import raw_memory_embedding_metadata
from sibyl_core.services.content_raw_embedding_repair import (
    RAW_CAPTURE_EMBEDDING_PLANE,
    repair_raw_capture_embeddings,
)
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.test_raw_capture_embedding_repair import provider, remember, stored
from tests.test_reflection_identity import content_store as content_store

PROBE_WORDING = "raw capture embedding health check"


class RefusedError(Exception):
    """An input the provider will not embed, carried the way SDK errors carry a 4xx."""

    status_code = 400


class ThrottledError(Exception):
    status_code = 429


class UnavailableError(Exception):
    status_code = 503


class BotocoreLikeError(Exception):
    """botocore puts the HTTP status in a response mapping, not an attribute."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.response = {
            "Error": {"Code": "ServiceUnavailableException"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class NoCredentialsLikeError(Exception):
    """A credential failure carries no HTTP status at all."""


class ScriptedProvider:
    """A deterministic provider that records every request and fails on cue."""

    def __init__(
        self, model: str, *, fail: Callable[[list[str]], Exception | None] | None = None
    ) -> None:
        self._inner = provider(model)
        self.metadata = self._inner.metadata
        self.fail = fail
        self.requests: list[list[str]] = []

    async def embed_texts(self, texts, *, input_kind: str = "document"):
        self.requests.append(list(texts))
        error = self.fail(list(texts)) if self.fail is not None else None
        if error is not None:
            raise error
        return await self._inner.embed_texts(texts, input_kind=input_kind)


async def capture(org: str, text: str) -> str:
    memory = await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id=f"source-{uuid4().hex[:8]}",
        raw_content=text,
        embedding_provider=None,
    )
    return memory.id


async def query(sql: str, **params: object) -> list[dict[str, object]]:
    async with content_client.surreal_content_client() as client:
        return await content_client.select_many(client, sql, **params)


async def raw_state(org: str) -> dict[str, object]:
    rows = await query(
        "SELECT * FROM type::record($key);",
        key=embedding_state_key(org, RAW_CAPTURE_EMBEDDING_PLANE),
    )
    return rows[0] if rows else {}


async def refusal(org: str, capture_id: str) -> dict[str, object] | None:
    rows = await query(
        "SELECT * FROM type::record($key);", key=raw_embedding_refusal_key(org, capture_id)
    )
    return rows[0] if rows else None


async def test_repair_stops_at_its_budget_and_resumes_after_its_durable_cursor(
    content_store, monkeypatch
):
    org = str(uuid4())
    memories = sorted(
        [await remember(org, f"budget-{index}") for index in range(6)],
        key=lambda memory: memory.id,
    )
    now = [0.0]

    def advance(_texts: list[str]) -> None:
        now[0] += 10.0

    slow = ScriptedProvider("budgeted", fail=advance)
    monkeypatch.setattr(repair_module, "_clock", lambda: now[0])
    monkeypatch.setattr(repair_module, "RAW_EMBEDDING_REPAIR_BUDGET_SECONDS", 15.0)
    walked_from: list[str] = []
    select_many = content_client.select_many

    async def recording(client, sql, **params):
        if "uuid >= $cursor" in sql:
            walked_from.append(params["cursor"])
        return await select_many(client, sql, **params)

    monkeypatch.setattr(content_client, "select_many", recording)

    first = await repair_raw_capture_embeddings(org, page_size=2, embedding_provider=slow)

    # Two pages fit in the budget; the third waits for the next pass.
    embedded = [m.id for m in memories if (await stored(m.id))["embedding"] is not None]
    assert embedded == [memory.id for memory in memories[:4]]
    assert first.status == "partial"
    assert first.cursor == memories[3].id
    assert (first.checked, first.recovered, first.pending, first.failed) == (4, 4, 0, 0)
    # The cursor and the receipt live in the organization's raw plane state row.
    state = await raw_state(org)
    assert state["cursors"] == {RAW_CAPTURE_EMBEDDING_PLANE: memories[3].id}
    assert state.get("lease_owner") is None
    receipt = state["last_run"]
    assert receipt["status"] == "partial"
    assert receipt["space"] == vector_space(raw_memory_embedding_metadata(slow.metadata))
    assert (receipt["in_model"], receipt["pending"]) == (4, 2)

    walked_from.clear()
    second = await repair_raw_capture_embeddings(org, page_size=2, embedding_provider=slow)

    assert walked_from[0] == first.cursor
    assert walked_from[-1] == "", "the pass wraps round to the rows before its cursor"
    assert (second.status, second.cursor, second.recovered) == ("completed", "", 2)
    for memory in memories:
        assert (await stored(memory.id))["embedding"] is not None
    assert (await raw_state(org))["complete_metadata"] == raw_memory_embedding_metadata(
        slow.metadata
    )


async def test_a_refused_row_is_isolated_stored_and_not_resent(content_store):
    org = str(uuid4())
    memories = [await remember(org, f"refusal-{index}") for index in range(5)]
    poisoned = memories[2]

    def reject_poisoned(texts: list[str]) -> Exception | None:
        return (
            RefusedError("input rejected") if any("refusal-2" in text for text in texts) else None
        )

    refusing = ScriptedProvider("refusing", fail=reject_poisoned)

    first = await repair_raw_capture_embeddings(org, embedding_provider=refusing)

    # The refusal fails the whole page request, yet every other row gets its vector.
    for memory in memories:
        assert ((await stored(memory.id))["embedding"] is None) is (memory is poisoned)
    assert (first.checked, first.recovered, first.pending, first.failed) == (5, 4, 0, 0)
    assert (first.status, first.refused) == ("completed", 1)
    stored_refusal = await refusal(org, poisoned.id)
    assert stored_refusal is not None
    assert (stored_refusal["capture_id"], stored_refusal["organization_id"]) == (poisoned.id, org)
    assert (stored_refusal["error_type"], stored_refusal["status_code"]) == ("RefusedError", 400)

    # A later pass, in this process or any other, skips the stored refusal.
    refusing.requests.clear()
    second = await repair_raw_capture_embeddings(org, embedding_provider=refusing)
    assert refusing.requests == [], "a stored refusal is not sent again"
    assert (second.checked, second.recovered, second.refused) == (1, 0, 1)

    # A new revision is new text: it is offered again.
    await query("UPDATE raw_captures SET revision = revision + 1 WHERE uuid = $id;", id=poisoned.id)
    third = await repair_raw_capture_embeddings(org, embedding_provider=refusing)
    assert any("refusal-2" in text for request in refusing.requests for text in request)
    assert third.refused == 1


async def test_an_expired_refusal_is_retried_and_pruned(content_store):
    org = str(uuid4())
    memory = await remember(org, "expiring")
    refusing = ScriptedProvider("expiring", fail=lambda _texts: RefusedError("rejected"))
    # A probe must pass for a lone row to be refused; the scripted provider fails it too.
    refusing.fail = lambda texts: None if PROBE_WORDING in texts[0] else RefusedError("rejected")
    assert (await repair_raw_capture_embeddings(org, embedding_provider=refusing)).refused == 1

    await query(
        "UPDATE type::record($key) SET expires_at = time::now() - 1h;",
        key=raw_embedding_refusal_key(org, memory.id),
    )
    healed = await repair_raw_capture_embeddings(org, embedding_provider=provider("expiring"))

    assert (healed.status, healed.recovered, healed.refused) == ("completed", 1, 0)
    assert await refusal(org, memory.id) is None, "a completed pass prunes expired refusals"


async def test_a_model_change_offers_refused_rows_to_the_new_model(content_store):
    org = str(uuid4())
    memory = await remember(org, "switching")
    old = ScriptedProvider(
        "old-model",
        fail=lambda texts: None if PROBE_WORDING in texts[0] else RefusedError("rejected"),
    )
    assert (await repair_raw_capture_embeddings(org, embedding_provider=old)).refused == 1

    result = await repair_raw_capture_embeddings(org, embedding_provider=provider("new-model"))

    assert (result.recovered, result.refused) == (1, 0)
    assert (await stored(memory.id))["embedding"] is not None


@pytest.mark.parametrize(
    "failure",
    [
        ThrottledError("slow down"),
        UnavailableError("service unavailable"),
        BotocoreLikeError(503),
        NoCredentialsLikeError("unable to locate credentials"),
    ],
    ids=["throttled", "unavailable", "botocore-503", "no-credentials"],
)
async def test_a_provider_side_failure_is_never_split_probed_or_blamed(content_store, failure):
    org = str(uuid4())
    memories = sorted(
        [await remember(org, f"busy-{index}") for index in range(4)], key=lambda memory: memory.id
    )
    busy = ScriptedProvider("busy", fail=lambda _texts: failure)

    result = await repair_raw_capture_embeddings(org, page_size=2, embedding_provider=busy)

    assert len(busy.requests) == 1
    assert result.status == "provider_failing"
    assert (result.checked, result.failed, result.refused) == (2, 2, 0)
    # The next pass moves on past the page the provider failed on.
    assert result.cursor == memories[1].id
    assert (
        await query("SELECT * FROM raw_embedding_refusals WHERE organization_id = $o;", o=org)
    ) == []


async def test_an_outage_after_a_good_page_never_becomes_a_refusal(content_store):
    org = str(uuid4())
    memories = sorted(
        [await remember(org, f"outage-{index}") for index in range(4)],
        key=lambda memory: memory.id,
    )
    served: list[int] = []

    def fail_after_first(_texts: list[str]) -> Exception | None:
        served.append(1)
        return UnavailableError("service unavailable") if len(served) > 1 else None

    outage = ScriptedProvider("outage", fail=fail_after_first)

    result = await repair_raw_capture_embeddings(org, page_size=2, embedding_provider=outage)

    assert result.status == "provider_failing"
    assert (result.recovered, result.failed, result.refused) == (2, 2, 0)
    assert result.cursor == memories[3].id
    assert len(outage.requests) == 2, "a request the provider could not serve is not split"


async def test_a_refusal_needs_a_probe_the_cache_cannot_answer(content_store):
    """A caching provider must not vouch for a provider that stopped serving."""
    org = str(uuid4())
    await remember(org, "cache-probe")
    inner = ScriptedProvider("cache-probe")
    cached = CachedEmbeddingProvider(inner)
    # Warm the cache while healthy with the probe's fixed wording, then break the provider.
    await cached.embed_texts([PROBE_WORDING], input_kind="document")
    inner.fail = lambda _texts: RefusedError("model access revoked")
    inner.requests.clear()

    result = await repair_raw_capture_embeddings(org, embedding_provider=cached)

    assert result.status == "provider_failing"
    assert (result.refused, result.failed) == (0, 1)
    assert len(inner.requests) == 2, "the probe reached the provider"
    assert PROBE_WORDING in inner.requests[1][0]


async def test_a_page_of_one_behind_a_refused_row_does_not_spin(content_store):
    """The inclusive walk drops its cursor row, so a refused row at the cursor is passed."""
    org = str(uuid4())
    await capture(org, "POISON row 0")
    for index in range(1, 4):
        await capture(org, f"row {index}")
    refusing = ScriptedProvider(
        "one-by-one", fail=lambda texts: RefusedError("rejected") if "POISON" in texts[0] else None
    )

    result = await repair_raw_capture_embeddings(org, page_size=1, embedding_provider=refusing)

    assert (result.status, result.refused, result.recovered, result.checked) == (
        "completed",
        1,
        3,
        4,
    )
    assert len(refusing.requests) <= 5


async def test_a_provider_request_that_never_returns_is_abandoned_at_its_cap(
    content_store, monkeypatch
):
    """A hung request reads as a transient failure: the pass ends and blames no row."""
    org = str(uuid4())
    for index in range(3):
        await remember(org, f"hanging-{index}")

    class Hanging:
        metadata = provider("hanging").metadata

        async def embed_texts(self, texts, *, input_kind: str = "document"):
            await asyncio.Event().wait()

    monkeypatch.setattr(repair_module, "_PROVIDER_CALL_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()

    result = await asyncio.wait_for(
        repair_raw_capture_embeddings(org, embedding_provider=Hanging()), timeout=5.0
    )

    assert time.monotonic() - started < 2.0
    assert result.status == "provider_failing"
    assert (result.checked, result.failed, result.refused) == (3, 3, 0)


async def test_a_provider_slower_than_the_budget_still_moves_each_pass_forward(
    content_store, monkeypatch
):
    """A request sent before the deadline is paid for, so it finishes and its vectors land."""
    org = str(uuid4())
    for index in range(4):
        await remember(org, f"slow-provider-{index}")

    class Slow:
        metadata = provider("slow-provider").metadata
        _inner = provider("slow-provider")

        async def embed_texts(self, texts, *, input_kind: str = "document"):
            await asyncio.sleep(0.2)
            return await self._inner.embed_texts(texts, input_kind=input_kind)

    monkeypatch.setattr(repair_module, "RAW_EMBEDDING_REPAIR_BUDGET_SECONDS", 0.01)
    passes = [
        await repair_raw_capture_embeddings(org, page_size=2, embedding_provider=Slow())
        for _ in range(3)
    ]

    assert [result.recovered for result in passes][:2] == [2, 2]
    assert passes[-1].status == "completed"


async def test_the_budget_never_cancels_a_database_query(content_store, monkeypatch):
    """The deadline passes while a walk is in flight; the walk finishes, then the pass stops."""
    org = str(uuid4())
    for index in range(3):
        await remember(org, f"slow-read-{index}")
    select_many = content_client.select_many
    now = [0.0]
    walks = {"issued": 0, "finished": 0, "cancelled": 0}

    async def walk_past_the_deadline(client, sql, **params):
        if "uuid >= $cursor" in sql:
            walks["issued"] += 1
            if walks["issued"] == 2:
                try:
                    await asyncio.sleep(0.3)
                except asyncio.CancelledError:
                    walks["cancelled"] += 1
                    raise
                now[0] += 100.0
            walks["finished"] += 1
        return await select_many(client, sql, **params)

    monkeypatch.setattr(content_client, "select_many", walk_past_the_deadline)
    monkeypatch.setattr(repair_module, "_clock", lambda: now[0])
    monkeypatch.setattr(repair_module, "RAW_EMBEDDING_REPAIR_BUDGET_SECONDS", 10.0)

    result = await repair_raw_capture_embeddings(
        org, page_size=2, embedding_provider=provider("slow-read")
    )

    assert walks == {"issued": 2, "finished": 2, "cancelled": 0}
    assert result.status == "partial"
    # The first page landed; the second page's walk finished but its rows wait.
    assert (result.checked, result.recovered, result.pending) == (3, 2, 1)


async def test_a_pass_finishes_its_first_page_even_when_the_walk_outlasts_the_budget(
    content_store, monkeypatch
):
    org = str(uuid4())
    for index in range(3):
        await remember(org, f"first-page-{index}")
    select_many = content_client.select_many

    async def slow_walk(client, sql, **params):
        if "uuid >= $cursor" in sql:
            await asyncio.sleep(0.2)
        return await select_many(client, sql, **params)

    monkeypatch.setattr(content_client, "select_many", slow_walk)
    monkeypatch.setattr(repair_module, "RAW_EMBEDDING_REPAIR_BUDGET_SECONDS", 0.01)

    result = await repair_raw_capture_embeddings(org, embedding_provider=provider("first-page"))

    assert (result.recovered, result.pending) == (3, 0)


async def test_a_write_that_outlives_the_budget_still_lands(content_store, monkeypatch):
    org = str(uuid4())
    memory = await remember(org, "slow-write")
    write = repair_module._write_embedding

    async def slow_write(client, memory, organization_id, *, observed):
        await asyncio.sleep(0.3)
        return await write(client, memory, organization_id, observed=observed)

    monkeypatch.setattr(repair_module, "_write_embedding", slow_write)
    monkeypatch.setattr(repair_module, "RAW_EMBEDDING_REPAIR_BUDGET_SECONDS", 0.1)

    result = await repair_raw_capture_embeddings(org, embedding_provider=provider("slow-write"))

    assert (result.status, result.recovered, result.pending) == ("completed", 1, 0)
    assert (await stored(memory.id))["embedding"] is not None


async def test_a_short_budget_on_the_embedded_engine_never_aborts_or_loses_a_write(
    content_store, monkeypatch
):
    """Hundreds of concurrent writes meet a deadline that has long passed, pass after pass."""
    org = str(uuid4())
    for start in range(0, 300, 100):
        for index in range(start, start + 100):
            await capture(org, f"cut {index}")
    monkeypatch.setattr(repair_module, "RAW_EMBEDDING_REPAIR_BUDGET_SECONDS", 0.01)
    current = provider("cut")
    recovered = 0
    for _ in range(20):
        result = await repair_raw_capture_embeddings(org, embedding_provider=current)
        recovered += result.recovered
        embedded = await query(
            "SELECT count() AS n FROM raw_captures WHERE organization_id = $o "
            "AND embedding != NONE GROUP ALL;",
            o=org,
        )
        # Every write a pass reported landed; none was cut off midway.
        assert (embedded[0]["n"] if embedded else 0) == recovered
        if result.status == "completed":
            break
    assert recovered == 300


async def test_a_second_pass_for_the_same_organization_waits_for_the_lease(content_store):
    org = str(uuid4())
    await capture(org, "POISON first")
    await capture(org, "late row")
    parked = asyncio.Event()
    release = asyncio.Event()
    embedded: list[str] = []

    class Gated:
        metadata = provider("gated").metadata
        _inner = provider("gated")

        async def embed_texts(self, texts, *, input_kind: str = "document"):
            if any("POISON" in text for text in texts):
                raise RefusedError("rejected")
            if any("late row" in text for text in texts) and not parked.is_set():
                parked.set()
                await release.wait()
            embedded.extend(texts)
            return await self._inner.embed_texts(texts, input_kind=input_kind)

    first = asyncio.create_task(
        repair_raw_capture_embeddings(org, page_size=1, embedding_provider=Gated())
    )
    await asyncio.wait_for(parked.wait(), 10)
    second = await repair_raw_capture_embeddings(org, page_size=1, embedding_provider=Gated())
    release.set()
    first_result = await first

    assert second.status == "busy"
    assert (second.checked, second.recovered) == (0, 0)
    assert (first_result.status, first_result.recovered, first_result.refused) == (
        "completed",
        1,
        1,
    )
    assert sum("late row" in text for text in embedded) == 1, "the late row was paid for once"
    third = await repair_raw_capture_embeddings(org, page_size=1, embedding_provider=Gated())
    assert (third.refused, third.recovered) == (1, 0), "the refusal survived both passes"


async def test_restamps_do_not_start_after_the_deadline(content_store, monkeypatch):
    issued: list[str] = []

    async def forbidden(client, sql, **params):
        issued.append(sql)
        return []

    monkeypatch.setattr(content_client, "select_many", forbidden)
    current = provider("late-restamp")
    run = repair_module._Pass(
        organization_id="org",
        provider=current,
        client=object(),  # type: ignore[arg-type]
        expected_metadata=raw_memory_embedding_metadata(current.metadata),
        identity="identity",
        owner="owner",
        lease="60s",
        deadline=repair_module._clock() - 1.0,
        progressed=True,
    )
    rows = [await remember(str(uuid4()), f"legacy-{index}") for index in range(3)]

    outcomes, stop = await repair_module._restamp(run, rows)

    assert (outcomes, stop) == (["pending"] * 3, "partial")
    assert issued == []


# The Bedrock error shapes the embedding provider meets, through the real provider
# in the production cache wrapper, as a deployment on Cohere Embed v4 would see them.
_CORAL = ":http://internal.amazon.com/coral/com.amazon.bedrock/"
_BEDROCK_STATUS = {
    "ServiceQuotaExceededException": 400,
    "ModelErrorException": 424,
    "ValidationException": 400,
    "AccessDeniedException": 403,
}


def bedrock_provider(rule: Callable[[list[str], int], str | None]):
    state = {"requests": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["texts"]
        state["requests"] += 1
        kind = rule(texts, state["requests"])
        if kind:
            return httpx.Response(
                _BEDROCK_STATUS[kind],
                headers={"x-amzn-ErrorType": kind + _CORAL},
                json={"message": f"{kind} message"},
            )
        return httpx.Response(
            200,
            json={"embeddings": {"float": [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]}},
        )

    wrapped = CachedEmbeddingProvider(
        BedrockEmbeddingProvider(
            metadata=EmbeddingMetadata(
                provider="bedrock",
                model="us.cohere.embed-v4:0",
                dimensions=EMBEDDING_DIM,
                cache_namespace="raw-memory",
                tokenizer_estimate_method="provider-default",
            ),
            settings=BedrockSettings(region="us-east-1", api_key="test-key"),
            client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ),
        max_size=2000,
    )
    return wrapped, state


async def _refusal_rows(org: str, kind: str = "refused") -> int:
    rows = await query(
        "SELECT count() AS n FROM raw_embedding_refusals WHERE organization_id = $o "
        "AND kind = $kind GROUP ALL;",
        o=org,
        kind=kind,
    )
    return int(rows[0]["n"]) if rows else 0


async def _expire_set_aside(org: str) -> None:
    await query(
        "UPDATE raw_embedding_refusals SET expires_at = time::now() - 1s "
        "WHERE organization_id = $o;",
        o=org,
    )


async def test_a_transient_bedrock_quota_error_never_refuses_a_row(content_store, monkeypatch):
    monkeypatch.setattr(bedrock_module, "_backoff", lambda _attempt: 0.0)
    org = str(uuid4())
    await capture(org, "row 0")
    quota_once, _state = bedrock_provider(
        lambda _texts, request: "ServiceQuotaExceededException" if request == 1 else None
    )

    first = await repair_raw_capture_embeddings(org, embedding_provider=quota_once)
    second = await repair_raw_capture_embeddings(org, embedding_provider=quota_once)

    assert (first.status, first.refused) == ("provider_failing", 0)
    assert (second.status, second.recovered, second.refused) == ("completed", 1, 0)
    assert await _refusal_rows(org) == 0


async def test_a_size_dependent_bedrock_quota_never_refuses_the_rows_it_blocks(
    content_store, monkeypatch
):
    monkeypatch.setattr(bedrock_module, "_backoff", lambda _attempt: 0.0)
    org = str(uuid4())
    for index in range(12):
        await capture(org, f"row {index} " + "x" * 3000)
    # Any request above 2,000 characters exceeds the quota; the probe fits.
    sized, state = bedrock_provider(
        lambda texts, _request: (
            "ServiceQuotaExceededException" if sum(map(len, texts)) > 2000 else None
        )
    )

    for _ in range(2):
        result = await repair_raw_capture_embeddings(org, embedding_provider=sized)
        assert (result.status, result.refused, result.recovered) == ("provider_failing", 0, 0)
    assert state["requests"] == 2, "one request per pass: no split, no probe"
    assert await _refusal_rows(org) == 0


async def test_a_transient_bedrock_model_error_never_refuses_a_lone_row(content_store, monkeypatch):
    monkeypatch.setattr(bedrock_module, "_backoff", lambda _attempt: 0.0)
    org = str(uuid4())
    await capture(org, "row 0")
    model_error_once, _state = bedrock_provider(
        lambda _texts, request: "ModelErrorException" if request == 1 else None
    )

    first = await repair_raw_capture_embeddings(org, embedding_provider=model_error_once)
    second = await repair_raw_capture_embeddings(org, embedding_provider=model_error_once)
    await _expire_set_aside(org)
    third = await repair_raw_capture_embeddings(org, embedding_provider=model_error_once)

    # The row is deferred, skipped while the deferral holds, then embedded.
    assert (first.recovered, first.deferred, first.refused) == (0, 1, 0)
    assert (second.recovered, second.deferred, second.refused) == (0, 1, 0)
    assert (third.recovered, third.deferred, third.refused) == (1, 0, 0)
    assert await _refusal_rows(org) == 0


async def test_a_bedrock_model_error_on_one_row_cannot_hold_up_its_page(content_store, monkeypatch):
    """ModelError never blames, but the rest of the page still gets its vectors."""
    monkeypatch.setattr(bedrock_module, "_backoff", lambda _attempt: 0.0)
    org = str(uuid4())
    ids = [await capture(org, f"row {index}") for index in range(12)]
    row_three, _state = bedrock_provider(
        lambda texts, _request: "ModelErrorException" if "row 3" in texts else None
    )

    first = await repair_raw_capture_embeddings(org, embedding_provider=row_three)
    requests_before = _state["requests"]
    second = await repair_raw_capture_embeddings(org, embedding_provider=row_three)

    assert (first.recovered, first.deferred, first.refused) == (11, 1, 0)
    assert (second.checked, second.recovered, second.deferred, second.refused) == (1, 0, 1, 0)
    assert _state["requests"] == requests_before, "a held deferral costs no request"
    assert (await _refusal_rows(org), await _refusal_rows(org, "deferred")) == (0, 1)
    assert (await stored(ids[3]))["embedding"] is None


async def test_good_rows_beside_many_failing_ones_all_land_within_a_few_passes(
    content_store, monkeypatch
):
    """Forty rows the model always fails on cannot starve the twenty it can embed."""
    monkeypatch.setattr(bedrock_module, "_backoff", lambda _attempt: 0.0)
    org = str(uuid4())
    good = [await capture(org, f"good {index}") for index in range(20)]
    bad = {await capture(org, f"bad {index}") for index in range(40)}
    failing, _state = bedrock_provider(
        lambda texts, _request: (
            "ModelErrorException" if any(text.startswith("bad ") for text in texts) else None
        )
    )

    passes = []
    for _ in range(6):
        passes.append(await repair_raw_capture_embeddings(org, embedding_provider=failing))
        if all([(await stored(memory_id))["embedding"] is not None for memory_id in good]):
            break

    assert all([(await stored(memory_id))["embedding"] is not None for memory_id in good])
    assert len(passes) <= 4, [(result.recovered, result.deferred) for result in passes]
    assert sum(result.refused for result in passes) == 0
    assert await _refusal_rows(org) == 0
    deferred = {
        str(row["capture_id"])
        for row in await query(
            "SELECT capture_id FROM raw_embedding_refusals WHERE organization_id = $o "
            "AND kind = 'deferred';",
            o=org,
        )
    }
    # Only rows the model fails on are ever set aside, and never as refused.
    assert deferred
    assert deferred <= bad


async def test_a_deferred_row_is_retried_after_expiry_for_longer_and_never_refused(
    content_store, monkeypatch
):
    monkeypatch.setattr(bedrock_module, "_backoff", lambda _attempt: 0.0)
    org = str(uuid4())
    memory_id = await capture(org, "always fails")
    failing, state = bedrock_provider(
        lambda texts, _request: None if PROBE_WORDING in texts[0] else "ModelErrorException"
    )

    async def deferral() -> dict[str, object]:
        rows = await query(
            "SELECT kind, attempts, duration::secs(expires_at - time::now()) AS left "
            "FROM type::record($key);",
            key=raw_embedding_refusal_key(org, memory_id),
        )
        return rows[0]

    first = await repair_raw_capture_embeddings(org, embedding_provider=failing)
    assert (first.deferred, first.refused) == (1, 0)
    after_first = await deferral()
    assert (after_first["kind"], after_first["attempts"]) == ("deferred", 1)
    assert 3500 <= after_first["left"] <= 3600

    held = state["requests"]
    assert (await repair_raw_capture_embeddings(org, embedding_provider=failing)).deferred == 1
    assert state["requests"] == held, "a held deferral is not sent again"

    await _expire_set_aside(org)
    await repair_raw_capture_embeddings(org, embedding_provider=failing)
    after_second = await deferral()
    assert state["requests"] > held, "an expired deferral is offered again"
    assert (after_second["kind"], after_second["attempts"]) == ("deferred", 2)
    assert 6 * 3600 - 100 <= after_second["left"] <= 6 * 3600

    await _expire_set_aside(org)
    await repair_raw_capture_embeddings(org, embedding_provider=failing)
    await _expire_set_aside(org)
    await repair_raw_capture_embeddings(org, embedding_provider=failing)
    capped = await deferral()
    assert (capped["kind"], capped["attempts"]) == ("deferred", 4)
    assert 24 * 3600 - 100 <= capped["left"] <= 24 * 3600
    assert await _refusal_rows(org) == 0


async def test_a_model_that_fails_every_real_request_ends_the_pass(content_store, monkeypatch):
    """A probe that passes cannot drag a pass through bisecting a whole failing page."""
    monkeypatch.setattr(bedrock_module, "_backoff", lambda _attempt: 0.0)
    org = str(uuid4())
    for index in range(64):
        await capture(org, f"row {index}")
    failing, state = bedrock_provider(
        lambda texts, _request: None if PROBE_WORDING in texts[0] else "ModelErrorException"
    )

    result = await repair_raw_capture_embeddings(org, embedding_provider=failing)

    assert (result.status, result.recovered, result.refused) == ("provider_failing", 0, 0)
    assert state["requests"] < 60
    assert await _refusal_rows(org) == 0


async def test_a_bedrock_validation_error_refuses_only_the_row_it_names(content_store, monkeypatch):
    monkeypatch.setattr(bedrock_module, "_backoff", lambda _attempt: 0.0)
    org = str(uuid4())
    ids = [await capture(org, f"row {index}") for index in range(12)]
    validation, _state = bedrock_provider(
        lambda texts, _request: "ValidationException" if "row 3" in texts else None
    )

    result = await repair_raw_capture_embeddings(org, embedding_provider=validation)

    assert (result.status, result.recovered, result.refused) == ("completed", 11, 1)
    stored_refusal = await refusal(org, ids[3])
    assert stored_refusal is not None
    assert stored_refusal["status_code"] == 400
    assert await _refusal_rows(org) == 1


async def test_bedrock_access_denied_ends_the_pass_without_a_probe(content_store, monkeypatch):
    monkeypatch.setattr(bedrock_module, "_backoff", lambda _attempt: 0.0)
    org = str(uuid4())
    for index in range(4):
        await capture(org, f"row {index}")
    denied, state = bedrock_provider(lambda _texts, _request: "AccessDeniedException")

    result = await repair_raw_capture_embeddings(org, embedding_provider=denied)

    assert (result.status, result.refused, result.failed) == ("provider_failing", 0, 4)
    assert state["requests"] == 1
