"""Per-plane bookkeeping for the embedding model sweep.

One row per organization and plane records how vectors written before Sibyl
stamped their model were classified, which model the plane was last swept
for, where the resumable walk stopped, which rows the provider refused, who
holds the sweep lease, and the last pass's receipt. The graph namespace and
the shared content namespace both carry the table, keyed the same way.

The shared content namespace also carries ``embedding_deployment``: the
stamps raw captures carried when the content schema upgraded, the models
every organization's graph stamps named when it upgraded, and the models the
deployment has been configured with since.

The stamps a plane's verdict weighs are photographed by the migration that
creates this bookkeeping. Only stamps in the previous release's format count
(stamps this release writes carry a version), so nothing a new process wrote,
even before the migration ran, passes for evidence of the model that
preceded the upgrade. Only stamps that sit beside a vector count: a stamp on
a row without one was never proven by an embedding call.

Nothing that reads or rewrites embedding evidence may run against a
namespace whose schema predates these migrations (see
``embedding_sweep_schema_ready``): a process that ticks before the upgrade
would otherwise rewrite the stamps the upgrade is about to photograph.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_version import (
    GRAPH_SCHEMA_NAME,
    schema_version_record_id,
)
from sibyl_core.embeddings.provenance import UNVERIFIED_EMBEDDING_PROVIDER

EMBEDDING_STATES_TABLE = "embedding_states"
EMBEDDING_DEPLOYMENT_TABLE = "embedding_deployment"
GRAPH_EMBEDDING_STATE_PLANE = "graph"
DEPLOYMENT_EVIDENCE_KEY = "embedding_deployment:evidence"
DEPLOYMENT_GRAPH_EVIDENCE_KEY = "embedding_deployment:graph_evidence"
DEPLOYMENT_EVIDENCE_WAIT_KEY = "embedding_deployment:evidence_wait"
DEPLOYMENT_MODELS_KEY = "embedding_deployment:models"
CONTENT_SCHEMA_RECORD_NAME = "content"
# The schema versions whose migrations create the sweep bookkeeping and take
# the evidence snapshots.
GRAPH_SWEEP_SCHEMA_VERSION = 31
CONTENT_SWEEP_SCHEMA_VERSION = 47

EMBEDDING_STATE_DEFINITIONS = """
DEFINE TABLE IF NOT EXISTS embedding_states SCHEMAFULL;
ALTER TABLE IF EXISTS embedding_states SCHEMAFULL;
ALTER TABLE IF EXISTS embedding_states PERMISSIONS NONE;
DEFINE FIELD IF NOT EXISTS organization_id ON embedding_states TYPE string;
DEFINE FIELD IF NOT EXISTS plane ON embedding_states TYPE string;
DEFINE FIELD IF NOT EXISTS legacy_evidence ON embedding_states TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS legacy_decision ON embedding_states TYPE option<string>;
DEFINE FIELD IF NOT EXISTS legacy_basis ON embedding_states TYPE option<string>;
DEFINE FIELD IF NOT EXISTS legacy_metadata ON embedding_states TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS legacy_warning ON embedding_states TYPE option<string>;
DEFINE FIELD IF NOT EXISTS legacy_notice ON embedding_states TYPE option<string>;
DEFINE FIELD IF NOT EXISTS legacy_provisional ON embedding_states TYPE option<bool>;
DEFINE FIELD IF NOT EXISTS legacy_deferred_at ON embedding_states TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS decided_at ON embedding_states TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS active_metadata ON embedding_states TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS complete_metadata ON embedding_states TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS complete_at ON embedding_states TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS generation ON embedding_states TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS cursors ON embedding_states TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS rejections ON embedding_states TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS lease_owner ON embedding_states TYPE option<string>;
DEFINE FIELD IF NOT EXISTS lease_until ON embedding_states TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS last_run ON embedding_states TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS updated_at ON embedding_states TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_embedding_states_scope ON embedding_states
    FIELDS organization_id, plane UNIQUE;
"""

# Raw captures the raw repair sets aside, one row per organization and
# capture. ``refused`` marks a capture the provider rejected as unacceptable
# input; ``deferred`` marks one the model kept failing on, which is never
# refused and waits out an expiry that grows with ``attempts``. The repair
# skips a capture while its row still matches the capture's revision and the
# configured vector identity and has not expired. The table sits apart from
# raw_captures because every raw capture column outside the embedding fields
# feeds the sealed cohort snapshot digest.
RAW_EMBEDDING_REFUSALS_TABLE = "raw_embedding_refusals"
RAW_EMBEDDING_REFUSAL_SCHEMA_VERSION = 48
RAW_EMBEDDING_REFUSAL_DEFINITIONS = """
DEFINE TABLE IF NOT EXISTS raw_embedding_refusals SCHEMAFULL;
ALTER TABLE IF EXISTS raw_embedding_refusals SCHEMAFULL;
ALTER TABLE IF EXISTS raw_embedding_refusals PERMISSIONS NONE;
DEFINE FIELD IF NOT EXISTS organization_id ON raw_embedding_refusals TYPE string;
DEFINE FIELD IF NOT EXISTS capture_id ON raw_embedding_refusals TYPE string;
DEFINE FIELD IF NOT EXISTS revision ON raw_embedding_refusals TYPE int;
DEFINE FIELD IF NOT EXISTS identity ON raw_embedding_refusals TYPE string;
DEFINE FIELD IF NOT EXISTS kind ON raw_embedding_refusals TYPE string DEFAULT 'refused'
    ASSERT $value IN ['refused', 'deferred'];
DEFINE FIELD IF NOT EXISTS attempts ON raw_embedding_refusals TYPE int DEFAULT 1;
DEFINE FIELD IF NOT EXISTS error_type ON raw_embedding_refusals TYPE option<string>;
DEFINE FIELD IF NOT EXISTS status_code ON raw_embedding_refusals TYPE option<int>;
DEFINE FIELD IF NOT EXISTS refused_at ON raw_embedding_refusals TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS expires_at ON raw_embedding_refusals TYPE datetime;
DEFINE INDEX IF NOT EXISTS idx_raw_embedding_refusals_capture ON raw_embedding_refusals
    FIELDS organization_id, capture_id UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_raw_embedding_refusals_expiry ON raw_embedding_refusals
    FIELDS organization_id, expires_at;
"""

EMBEDDING_DEPLOYMENT_DEFINITIONS = """
DEFINE TABLE IF NOT EXISTS embedding_deployment SCHEMAFULL;
ALTER TABLE IF EXISTS embedding_deployment SCHEMAFULL;
ALTER TABLE IF EXISTS embedding_deployment PERMISSIONS NONE;
DEFINE FIELD IF NOT EXISTS kind ON embedding_deployment TYPE string;
DEFINE FIELD IF NOT EXISTS data ON embedding_deployment TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS updated_at ON embedding_deployment TYPE datetime DEFAULT time::now();
"""

# Imports and dimension rebuilds can hand a plane rows that need vectors while
# its state still says a full pass found nothing, so they reopen the plane.
# A pass that started before the reopen sees the generation move and does
# not record itself complete. Remembered provider refusals are forgotten, so
# rows the reopen hands over are tried at least once. The predicate is spelled
# CONTAINS because the
# embedded 2.x test engine drops IN matches that the compound scope index
# answers.
REOPEN_EMBEDDING_STATES = (
    "UPDATE embedding_states SET complete_metadata = NONE, complete_at = NONE, "
    "rejections = {}, generation = (generation ?? 0) + 1, updated_at = time::now() "
    "WHERE $organizations CONTAINS organization_id RETURN NONE;"
)

type _Execute = Callable[..., Awaitable[object]]


class _Ownership(Protocol):
    mutate: _Execute


def embedding_state_key(organization_id: str, plane: str) -> str:
    """Record id of one organization's state row for one plane."""
    digest = hashlib.sha256(f"{organization_id}\x1f{plane}".encode()).hexdigest()
    return f"embedding_states:s{digest[:40]}"


def raw_embedding_refusal_key(organization_id: str, capture_id: str) -> str:
    """Record id of one organization's refusal row for one raw capture."""
    digest = hashlib.sha256(f"{organization_id}\x1f{capture_id}".encode()).hexdigest()
    return f"{RAW_EMBEDDING_REFUSALS_TABLE}:r{digest[:40]}"


async def embedding_sweep_schema_ready(execute_query: _Execute, *, graph: bool) -> bool:
    """Whether a namespace's schema has taken the sweep's evidence snapshot.

    Reads the version record by id, which a namespace without the version
    table answers with no rows rather than an error.
    """
    name = GRAPH_SCHEMA_NAME if graph else CONTENT_SCHEMA_RECORD_NAME
    required = GRAPH_SWEEP_SCHEMA_VERSION if graph else CONTENT_SWEEP_SCHEMA_VERSION
    rows = normalize_records(
        await execute_query(f"SELECT version FROM [{schema_version_record_id(name)}];")
    )
    version = rows[0].get("version") if rows else None
    return isinstance(version, int | float) and version >= required


def _stamp_groups_query(
    table: str, metadata_path: str, *, vector_field: str, scope_field: str | None
) -> str:
    scope = f"{scope_field} = $scope AND " if scope_field else ""
    return (
        f"SELECT {metadata_path}.provider AS provider, {metadata_path}.model AS model, "
        f"{metadata_path}.dimensions AS dimensions, count() AS rows FROM {table} "
        f"WHERE {scope}{vector_field} != NONE AND ({metadata_path} ?? NONE) != NONE "
        f"AND ({metadata_path}.provider ?? NONE) != NONE AND ({metadata_path}.model ?? NONE) != NONE "
        f"AND {metadata_path}.provider != $unverified "
        f"AND ({metadata_path}.stamp_version ?? NONE) = NONE "
        "GROUP BY provider, model, dimensions;"
    )


async def _stamp_groups(
    execute_query: _Execute, query: str, **params: object
) -> list[dict[str, Any]]:
    rows = normalize_records(
        await execute_query(query, unverified=UNVERIFIED_EMBEDDING_PROVIDER, **params)
    )
    return [
        {
            "provider": row.get("provider"),
            "model": row.get("model"),
            "dimensions": row.get("dimensions"),
            "rows": count if isinstance(count := row.get("rows"), int) else 0,
        }
        for row in rows
    ]


async def snapshot_graph_embedding_evidence(
    execute_query: _Execute,
    *,
    group_id: str | None = None,
    ownership: _Ownership | None = None,
) -> None:
    """Record which models this graph namespace's stamped vectors name, as of its upgrade.

    Runs inside the migration that introduces the sweep, before the namespace
    reports its schema current, so every stamp it counts predates this
    release. A second run keeps the first photograph.
    """
    if not group_id:
        return
    stamps: list[dict[str, Any]] = []
    for table, vector_field in (("entity", "name_embedding"), ("relates_to", "fact_embedding")):
        stamps.extend(
            await _stamp_groups(
                execute_query,
                _stamp_groups_query(
                    table,
                    "attributes.embedding_metadata",
                    vector_field=vector_field,
                    scope_field="group_id",
                ),
                scope=group_id,
            )
        )
    mutate = ownership.mutate if ownership is not None else execute_query
    await mutate(
        "UPSERT type::record($key) SET organization_id = $scope, plane = $plane, "
        "legacy_evidence = legacy_evidence ?? {stamps: $stamps, taken_at: time::now()}, "
        "updated_at = time::now() RETURN NONE;",
        key=embedding_state_key(group_id, GRAPH_EMBEDDING_STATE_PLANE),
        scope=group_id,
        plane=GRAPH_EMBEDDING_STATE_PLANE,
        stamps=stamps,
    )


# The persisted settings the previous release's crawler read before the
# environment, by the field of the content embedding they set.
CRAWLER_SETTING_KEYS = {
    "embedding_provider": "provider",
    "embedding_model": "model",
    "embedding_dimensions": "dimensions",
}


async def snapshot_content_embedding_evidence(execute_query: _Execute) -> None:
    """Record what speaks for the deployment's chunk vectors, as of its upgrade.

    Chunks carried no stamp before this release, and one content
    configuration embeds every organization's chunks and raw captures, so
    the raw captures of any organization speak for every chunk plane, with
    one exception. The previous release's crawler read the content
    embedding settings saved in the settings UI before the environment,
    while raw captures read the environment first, so where a saved setting
    exists it, not the raw captures, names what embedded the chunks. The
    saved settings are recorded beside the raw capture stamps.
    """
    stamps = await _stamp_groups(
        execute_query,
        _stamp_groups_query(
            "raw_captures",
            "metadata.embedding_metadata",
            vector_field="embedding",
            scope_field=None,
        ),
    )
    rows = normalize_records(
        await execute_query(
            "SELECT key, value FROM system_settings WHERE $keys CONTAINS key;",
            keys=list(CRAWLER_SETTING_KEYS),
        )
    )
    crawler_settings = {
        CRAWLER_SETTING_KEYS[str(row["key"])]: value
        for row in rows
        if str(row.get("key")) in CRAWLER_SETTING_KEYS
        and isinstance(row.get("value"), str)
        and (value := str(row["value"]).strip())
    }
    await execute_query(
        f"UPSERT {DEPLOYMENT_EVIDENCE_KEY} SET kind = 'evidence', "
        "data = IF data.taken_at = NONE THEN "
        "{stamps: $stamps, crawler_settings: $crawler_settings, taken_at: time::now()} "
        "ELSE data END, updated_at = time::now() RETURN NONE;",
        stamps=stamps,
        crawler_settings=crawler_settings,
    )


__all__ = [
    "CONTENT_SWEEP_SCHEMA_VERSION",
    "CRAWLER_SETTING_KEYS",
    "DEPLOYMENT_EVIDENCE_KEY",
    "DEPLOYMENT_EVIDENCE_WAIT_KEY",
    "DEPLOYMENT_GRAPH_EVIDENCE_KEY",
    "DEPLOYMENT_MODELS_KEY",
    "EMBEDDING_DEPLOYMENT_DEFINITIONS",
    "EMBEDDING_DEPLOYMENT_TABLE",
    "EMBEDDING_STATES_TABLE",
    "EMBEDDING_STATE_DEFINITIONS",
    "GRAPH_EMBEDDING_STATE_PLANE",
    "GRAPH_SWEEP_SCHEMA_VERSION",
    "RAW_EMBEDDING_REFUSALS_TABLE",
    "RAW_EMBEDDING_REFUSAL_DEFINITIONS",
    "RAW_EMBEDDING_REFUSAL_SCHEMA_VERSION",
    "REOPEN_EMBEDDING_STATES",
    "embedding_state_key",
    "embedding_sweep_schema_ready",
    "raw_embedding_refusal_key",
    "snapshot_content_embedding_evidence",
    "snapshot_graph_embedding_evidence",
]
