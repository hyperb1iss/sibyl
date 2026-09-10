"""Write conflicts bind source snapshots to transactions on derived records."""

SOURCE_STATE_WITNESS_DEFINITION = """
DEFINE FIELD IF NOT EXISTS validation_write_witness ON source_states TYPE option<string>;
"""

# A no-op UPDATE does not enlist a write conflict in native SurrealDB. The
# random value is bookkeeping only and must be omitted from evidence hashes.
SOURCE_STATE_WRITE_WITNESS = """
FOR $source_state IN $source_states_to_fence {
    UPDATE $source_state.id SET validation_write_witness = type::string(rand::uuid());
};
"""
