"""Evidence for a plane's legacy verdict that no write after the upgrade can forge.

A plane's unstamped vectors were produced by whatever model the deployment
ran before this release. Three records speak to that model, and all of them
are fixed before this release writes a single vector:

- the stamps a graph namespace's rows carried when it upgraded, written onto
  its graph state row by the migration that creates the sweep's bookkeeping,
  and published once into ``embedding_deployment:graph_evidence`` so every
  organization's graph speaks for the others: one configuration embeds them
  all;
- the stamps raw captures carried across the whole deployment when the
  content schema upgraded (``embedding_deployment:evidence``), which speak
  for every organization's chunks because one content configuration embeds
  them all;
- the models lifecycle passes of this release have swept under
  (``embedding_deployment:models``), written only after a pass the provider
  did not refuse, which catches a switch made after the first such pass for
  a plane no pass has classified yet.

Only stamps that sit beside a vector count, so a model name a client
supplied on a row that was never embedded proves nothing.

Comparisons use the vector space (provider, model, dimensions): the stamps
belong to different text contracts, so only the model behind them matters.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_embedding_states import (
    DEPLOYMENT_EVIDENCE_KEY,
    DEPLOYMENT_EVIDENCE_WAIT_KEY,
    DEPLOYMENT_GRAPH_EVIDENCE_KEY,
    DEPLOYMENT_MODELS_KEY,
    GRAPH_EMBEDDING_STATE_PLANE,
    embedding_state_key,
)
from sibyl_core.embeddings.provenance import same_vector_space, vector_space
from sibyl_core.services.embedding_sweep import EmbeddingStamp, LegacyEvidence, SweepExecute

GRAPH_MODEL_KIND = "graph"
CONTENT_MODEL_KIND = "content"


@dataclass(frozen=True, slots=True)
class PlaneEvidence:
    """What the pre-upgrade records say about one plane on their own."""

    differs: bool = False
    matches: bool = False
    model_changed: bool = False

    @property
    def switched(self) -> bool:
        return self.differs or self.model_changed


@dataclass(frozen=True, slots=True)
class GatheredEvidence:
    """Each plane's evidence, including what the other plane says."""

    graph: LegacyEvidence
    document_chunks: LegacyEvidence


def classify_stamps(
    stamps: Sequence[Mapping[str, Any]], configured: EmbeddingStamp
) -> tuple[bool, bool]:
    """Whether any recorded stamp names another model, and whether any names this one."""
    differs = matches = False
    for stamp in stamps:
        if same_vector_space(stamp, configured):
            matches = True
        else:
            differs = True
    return differs, matches


async def _records(execute: SweepExecute, query: str, **params: object) -> list[dict[str, Any]]:
    return normalize_records(await execute(query, **params))


def _snapshot_stamps(snapshot: object) -> list[dict[str, Any]]:
    stamps = snapshot.get("stamps") if isinstance(snapshot, Mapping) else None
    if not isinstance(stamps, list):
        return []
    return [dict(stamp) for stamp in stamps if isinstance(stamp, Mapping)]


async def read_graph_snapshot(execute: SweepExecute, organization_id: str) -> list[dict[str, Any]]:
    """The model groups one graph namespace's stamps named when it upgraded."""
    rows = await _records(
        execute,
        "SELECT legacy_evidence FROM type::record($key);",
        key=embedding_state_key(organization_id, GRAPH_EMBEDDING_STATE_PLANE),
    )
    return _snapshot_stamps(rows[0].get("legacy_evidence") if rows else None)


async def publish_graph_snapshot(
    *, organization_id: str, graph_execute: SweepExecute, content_execute: SweepExecute
) -> bool:
    """Add one graph namespace's pre-upgrade models to the deployment's, once.

    The organization joins the deployment's published set in the same write,
    before its own marker is set, so an organization that published once
    counts as published for good even if later passes fail for it. Returns
    whether this call published. A namespace that has not taken its upgrade
    photograph yet publishes nothing.
    """
    key = embedding_state_key(organization_id, GRAPH_EMBEDDING_STATE_PLANE)
    rows = await _records(graph_execute, "SELECT legacy_evidence FROM type::record($key);", key=key)
    snapshot = rows[0].get("legacy_evidence") if rows else None
    if not isinstance(snapshot, Mapping) or snapshot.get("published_at") is not None:
        return False
    spaces = [space for stamp in _snapshot_stamps(snapshot) if (space := vector_space(stamp))]
    await _records(
        content_execute,
        f"UPSERT {DEPLOYMENT_GRAPH_EVIDENCE_KEY} SET kind = 'graph_evidence', data = {{"
        "stamps: array::union(data.stamps ?? [], $stamps), "
        "published: array::union(data.published ?? [], [$organization]), "
        "updated_at: time::now()"
        "}, updated_at = time::now() RETURN NONE;",
        stamps=spaces,
        organization=organization_id,
    )
    await _records(
        graph_execute,
        "UPDATE type::record($key) SET legacy_evidence.published_at = time::now() RETURN NONE;",
        key=key,
    )
    return True


async def read_published_organizations(execute: SweepExecute) -> set[str]:
    """Organizations whose graph snapshot the deployment has already heard."""
    rows = await _records(execute, f"SELECT data FROM {DEPLOYMENT_GRAPH_EVIDENCE_KEY};")
    data = rows[0].get("data") if rows else None
    published = data.get("published") if isinstance(data, Mapping) else None
    return {str(item) for item in published} if isinstance(published, list) else set()


async def record_evidence_wait(
    execute: SweepExecute, *, waiting_on: Sequence[str], deferred_planes: int
) -> None:
    """Note which organizations have not published while planes wait on them.

    Status surfaces read it so an operator can see why a plane is waiting.
    An empty list clears the note.
    """
    await _records(
        execute,
        f"UPSERT {DEPLOYMENT_EVIDENCE_WAIT_KEY} SET kind = 'evidence_wait', data = {{"
        "organizations: $sample, count: $count, deferred_planes: $deferred, "
        "checked_at: time::now()}, updated_at = time::now() RETURN NONE;",
        sample=list(waiting_on)[:20],
        count=len(waiting_on),
        deferred=deferred_planes,
    )


async def read_evidence_wait(execute: SweepExecute) -> dict[str, Any]:
    rows = await _records(execute, f"SELECT data FROM {DEPLOYMENT_EVIDENCE_WAIT_KEY};")
    data = rows[0].get("data") if rows else None
    return dict(data) if isinstance(data, Mapping) else {}


async def read_deployment_graph_snapshot(execute: SweepExecute) -> list[dict[str, Any]]:
    """The models any organization's graph stamps named when it upgraded."""
    rows = await _records(execute, f"SELECT data FROM {DEPLOYMENT_GRAPH_EVIDENCE_KEY};")
    return _snapshot_stamps(rows[0].get("data") if rows else None)


async def read_content_snapshot(execute: SweepExecute) -> list[dict[str, Any]]:
    """The model groups raw captures named across the deployment when it upgraded."""
    rows = await _records(execute, f"SELECT data FROM {DEPLOYMENT_EVIDENCE_KEY};")
    return _snapshot_stamps(rows[0].get("data") if rows else None)


async def read_deployment_models(execute: SweepExecute) -> dict[str, Any]:
    rows = await _records(execute, f"SELECT data FROM {DEPLOYMENT_MODELS_KEY};")
    data = rows[0].get("data") if rows else None
    return dict(data) if isinstance(data, Mapping) else {}


async def record_deployment_models(
    execute: SweepExecute,
    *,
    graph: EmbeddingStamp | None,
    content: EmbeddingStamp | None,
) -> dict[str, Any]:
    """Note the models this process swept under.

    The first models ever recorded are kept, so a later pass on another
    model is evidence of a switch for every plane no pass has classified.
    A plane with no configured provider keeps its last recorded model.
    """
    rows = await _records(
        execute,
        f"UPSERT {DEPLOYMENT_MODELS_KEY} SET kind = 'models', data = {{"
        "first_graph: data.first_graph ?? $graph, "
        "first_content: data.first_content ?? $content, "
        "first_recorded_at: data.first_recorded_at ?? time::now(), "
        "graph: $graph ?? data.graph, "
        "content: $content ?? data.content, "
        "recorded_at: time::now()"
        "}, updated_at = time::now() RETURN AFTER;",
        graph=dict(graph) if graph is not None else None,
        content=dict(content) if content is not None else None,
    )
    data = rows[0].get("data") if rows else None
    return dict(data) if isinstance(data, Mapping) else {}


def _plane_evidence(
    stamps: Sequence[Mapping[str, Any]],
    configured: EmbeddingStamp | None,
    models: Mapping[str, Any],
    kind: str,
) -> PlaneEvidence:
    current = configured if configured is not None else models.get(kind)
    if not isinstance(current, Mapping):
        return PlaneEvidence()
    differs, matches = classify_stamps(stamps, dict(current))
    first = models.get(f"first_{kind}")
    changed = isinstance(first, Mapping) and not same_vector_space(first, current)
    return PlaneEvidence(differs=differs, matches=matches, model_changed=changed)


async def gather_legacy_evidence(
    *,
    organization_id: str,
    content_execute: SweepExecute,
    content_stamp: EmbeddingStamp | None,
    graph_execute: SweepExecute | None = None,
    graph_stamp: EmbeddingStamp | None = None,
) -> GatheredEvidence:
    """Read every pre-upgrade record for one organization and weigh both planes.

    A plane whose configured stamp is not supplied is judged against the
    model the deployment last recorded for it. Without ``graph_execute`` the
    graph namespace's own stamps are not read.
    """
    models = await read_deployment_models(content_execute)
    graph_stamps = (
        await read_graph_snapshot(graph_execute, organization_id)
        if graph_execute is not None
        else []
    )
    graph = _plane_evidence(graph_stamps, graph_stamp, models, GRAPH_MODEL_KIND)
    graph_current = graph_stamp if graph_stamp is not None else models.get(GRAPH_MODEL_KIND)
    deployment_differs = deployment_matches = False
    if isinstance(graph_current, Mapping):
        deployment_differs, deployment_matches = classify_stamps(
            await read_deployment_graph_snapshot(content_execute), dict(graph_current)
        )
    content = _plane_evidence(
        await read_content_snapshot(content_execute), content_stamp, models, CONTENT_MODEL_KIND
    )
    return GatheredEvidence(
        graph=LegacyEvidence(
            differs=graph.differs,
            matches=graph.matches,
            deployment_differs=deployment_differs,
            deployment_matches=deployment_matches,
            model_changed=graph.model_changed,
            other_plane_switched=content.switched,
        ),
        document_chunks=LegacyEvidence(
            differs=content.differs,
            matches=content.matches,
            model_changed=content.model_changed,
            other_plane_switched=graph.switched or deployment_differs,
        ),
    )


__all__ = [
    "CONTENT_MODEL_KIND",
    "GRAPH_MODEL_KIND",
    "GatheredEvidence",
    "PlaneEvidence",
    "classify_stamps",
    "gather_legacy_evidence",
    "publish_graph_snapshot",
    "read_content_snapshot",
    "read_deployment_graph_snapshot",
    "read_deployment_models",
    "read_evidence_wait",
    "read_graph_snapshot",
    "read_published_organizations",
    "record_deployment_models",
    "record_evidence_wait",
]
