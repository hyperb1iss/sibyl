"""Settle an organization's graph and chunk legacy verdicts against all the evidence.

Each plane can decide on its own records, but the strongest verdict weighs
the whole deployment: a graph whose rows carried no stamps is still known to
be stale when another organization's graph stamps, or the deployment's raw
captures, prove a provider moved; chunks with no raw captures anywhere are
still known to be stale when a graph's stamps prove the graph provider moved.
Lifecycle repair settles both verdicts here, once, before any pass embeds or
adopts a vector.

Every organization publishes its graph photograph before its own verdicts
are weighed, and joins the deployment's published set for good. A plane with
no evidence at all is deferred, and lifecycle repair settles it once every
organization has published, so the verdict never depends on which
organization went first. The wait is bounded: after the configured evidence
wait the plane is settled on what has been published, and the organizations
that never published are named in status and logs.

Neither namespace's evidence is read or written before its schema has taken
the upgrade snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.schema_embedding_states import embedding_sweep_schema_ready
from sibyl_core.embeddings.providers import EmbeddingProvider
from sibyl_core.services import content_client
from sibyl_core.services.document_embedding_sweep import (
    ChunkEmbedder,
    content_session,
    document_chunk_embedding_plane,
    require_content_sweep_schema,
)
from sibyl_core.services.embedding_evidence import (
    GatheredEvidence,
    gather_legacy_evidence,
    publish_graph_snapshot,
)
from sibyl_core.services.embedding_sweep import (
    EmbeddingSchemaPendingError,
    EmbeddingStamp,
    LegacyEvidence,
    ensure_legacy_decision,
)
from sibyl_core.services.graph_embedding_sweep import graph_embedding_plane

type PlaneVerdict = dict[str, Any] | BaseException | None


@dataclass(frozen=True, slots=True)
class LegacyVerdicts:
    """Each plane's persisted state, the error that kept it unsettled, or ``None``
    when no provider is configured for it. A deferred plane's state carries
    ``legacy_deferred``."""

    graph: PlaneVerdict
    document_chunks: PlaneVerdict

    @property
    def deferred(self) -> bool:
        return any(
            isinstance(verdict, dict) and verdict.get("legacy_deferred")
            for verdict in (self.graph, self.document_chunks)
        )


def verdict_settled(verdict: PlaneVerdict) -> bool:
    """Whether a plane may sweep: its verdict is recorded, or it has no provider."""
    if isinstance(verdict, BaseException):
        return False
    return not (isinstance(verdict, dict) and verdict.get("legacy_deferred"))


class _Evidence:
    """Read the evidence at most once, and only if a plane still needs a verdict."""

    def __init__(
        self,
        *,
        organization_id: str,
        graph_client: Any,
        graph_stamp: EmbeddingStamp | None,
        content: SurrealContentClient,
        chunk_stamp: EmbeddingStamp | None,
    ) -> None:
        self._organization_id = organization_id
        self._graph_client = graph_client
        self._graph_stamp = graph_stamp
        self._content = content
        self._chunk_stamp = chunk_stamp
        self._gathered: GatheredEvidence | None = None

    async def _gather(self) -> GatheredEvidence:
        if self._gathered is None:
            content = self._content

            async def content_execute(query: str, **params: object) -> object:
                return await content_client.select_many(content, query, **params)

            self._gathered = await gather_legacy_evidence(
                organization_id=self._organization_id,
                content_execute=content_execute,
                content_stamp=self._chunk_stamp,
                graph_execute=self._graph_client.execute_query,
                graph_stamp=self._graph_stamp,
            )
        return self._gathered

    async def graph(self) -> LegacyEvidence:
        return (await self._gather()).graph

    async def document_chunks(self) -> LegacyEvidence:
        return (await self._gather()).document_chunks


async def settle_legacy_verdicts(
    organization_id: str,
    *,
    graph_client: Any,
    graph_provider: EmbeddingProvider | None,
    chunk_stamp: EmbeddingStamp | None,
    embed_chunks: ChunkEmbedder,
    client: SurrealContentClient | None = None,
    allow_unproven: bool = False,
    defer_limit_seconds: float | None = None,
) -> LegacyVerdicts:
    """Persist both planes' verdicts for one organization if they are not recorded yet.

    A plane whose verdict fails to settle reports the error and is left for
    the next pass; it is never decided on partial evidence. Unless
    ``allow_unproven``, a plane with no evidence anywhere is deferred rather
    than adopted with a warning, for at most ``defer_limit_seconds`` after its
    first deferral.

    Raises ``EmbeddingSchemaPendingError`` while the content namespace has
    not taken its upgrade snapshot; a graph namespace that has not reports
    the same error as its plane's verdict and publishes nothing.
    """
    graph_stamp = graph_provider.metadata.to_dict() if graph_provider is not None else None
    async with content_session(client) as content:
        await require_content_sweep_schema(content)

        async def content_execute(query: str, **params: object) -> object:
            return await content_client.select_many(content, query, **params)

        graph_ready = await embedding_sweep_schema_ready(graph_client.execute_query, graph=True)
        if graph_ready:
            await publish_graph_snapshot(
                organization_id=organization_id,
                graph_execute=graph_client.execute_query,
                content_execute=content_execute,
            )
        evidence = _Evidence(
            organization_id=organization_id,
            graph_client=graph_client,
            graph_stamp=graph_stamp,
            content=content,
            chunk_stamp=chunk_stamp,
        )
        graph: PlaneVerdict = None
        if graph_provider is not None and not graph_ready:
            graph = EmbeddingSchemaPendingError(
                f"graph namespace for {organization_id} has not run its embedding sweep migration"
            )
        elif graph_provider is not None:
            try:
                graph = await ensure_legacy_decision(
                    await graph_embedding_plane(
                        graph_client, graph_provider, evidence=evidence.graph
                    ),
                    defer_unproven=not allow_unproven,
                    defer_limit_seconds=defer_limit_seconds,
                )
            except Exception as exc:
                graph = exc
        chunks: PlaneVerdict = None
        if chunk_stamp is not None and not graph_ready:
            # The chunk verdict weighs the graph's evidence, which does not
            # exist until the graph namespace upgrades.
            chunks = EmbeddingSchemaPendingError(
                f"graph namespace for {organization_id} has not run its embedding sweep migration"
            )
        elif chunk_stamp is not None:
            try:
                chunks = await ensure_legacy_decision(
                    await document_chunk_embedding_plane(
                        organization_id,
                        client=content,
                        stamp=chunk_stamp,
                        embed_chunks=embed_chunks,
                        evidence=evidence.document_chunks,
                    ),
                    defer_unproven=not allow_unproven,
                    defer_limit_seconds=defer_limit_seconds,
                )
            except Exception as exc:
                chunks = exc
    return LegacyVerdicts(graph=graph, document_chunks=chunks)


__all__ = ["LegacyVerdicts", "PlaneVerdict", "settle_legacy_verdicts", "verdict_settled"]
