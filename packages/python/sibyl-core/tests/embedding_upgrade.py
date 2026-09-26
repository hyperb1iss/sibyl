"""Replay the upgrade that introduces the embedding sweep, as a deployment would.

Tests write rows through today's write paths, then rewind the recorded
schema version to the release before the sweep and bootstrap again. The
sweep's migrations then photograph those rows' stamps exactly as they would
on a store written by the previous release.
"""

from __future__ import annotations

from sibyl_core.backends.surreal import bootstrap_content_schema
from sibyl_core.backends.surreal.content_schema import CONTENT_SCHEMA_NAME
from sibyl_core.backends.surreal.schema_version import GRAPH_SCHEMA_NAME, schema_version_record_id
from sibyl_core.services.graph_client import mark_graph_schema_dirty, prepare_graph_schema

GRAPH_VERSION_BEFORE_SWEEP = 30
CONTENT_VERSION_BEFORE_SWEEP = 46


async def upgrade_graph_to_sweep(client) -> None:
    await client.execute_query("REMOVE TABLE IF EXISTS embedding_states;")
    await client.execute_query(
        "UPDATE type::record($record) SET version = $version;",
        record=schema_version_record_id(GRAPH_SCHEMA_NAME),
        version=GRAPH_VERSION_BEFORE_SWEEP,
    )
    mark_graph_schema_dirty(client.group_id)
    await prepare_graph_schema(client)


async def upgrade_content_to_sweep(client) -> None:
    await client.execute_query("REMOVE TABLE IF EXISTS embedding_states;")
    await client.execute_query("REMOVE TABLE IF EXISTS embedding_deployment;")
    await client.execute_query(
        "UPDATE type::record($record) SET version = $version;",
        record=schema_version_record_id(CONTENT_SCHEMA_NAME),
        version=CONTENT_VERSION_BEFORE_SWEEP,
    )
    await bootstrap_content_schema(client)
