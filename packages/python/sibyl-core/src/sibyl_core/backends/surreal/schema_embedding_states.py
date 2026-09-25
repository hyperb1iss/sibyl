"""Per-plane bookkeeping for the embedding model sweep.

One row per organization and plane records how vectors written before Sibyl
stamped their model were classified, which model the plane was last swept
for, where the resumable walk stopped, who holds the sweep lease, and the last
pass's receipt. The graph namespace and the shared content namespace both
carry the table, keyed the same way.
"""

EMBEDDING_STATES_TABLE = "embedding_states"

EMBEDDING_STATE_DEFINITIONS = """
DEFINE TABLE IF NOT EXISTS embedding_states SCHEMAFULL;
ALTER TABLE IF EXISTS embedding_states SCHEMAFULL;
ALTER TABLE IF EXISTS embedding_states PERMISSIONS NONE;
DEFINE FIELD IF NOT EXISTS organization_id ON embedding_states TYPE string;
DEFINE FIELD IF NOT EXISTS plane ON embedding_states TYPE string;
DEFINE FIELD IF NOT EXISTS legacy_decision ON embedding_states TYPE option<string>;
DEFINE FIELD IF NOT EXISTS legacy_basis ON embedding_states TYPE option<string>;
DEFINE FIELD IF NOT EXISTS legacy_metadata ON embedding_states TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS decided_at ON embedding_states TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS active_metadata ON embedding_states TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS complete_metadata ON embedding_states TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS complete_at ON embedding_states TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS generation ON embedding_states TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS cursors ON embedding_states TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS lease_owner ON embedding_states TYPE option<string>;
DEFINE FIELD IF NOT EXISTS lease_until ON embedding_states TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS last_run ON embedding_states TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS updated_at ON embedding_states TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_embedding_states_scope ON embedding_states
    FIELDS organization_id, plane UNIQUE;
"""

# Imports and dimension rebuilds can hand a plane rows that need vectors while
# its state still says a full pass found nothing, so they reopen the plane.
# A pass that started before the reopen sees the generation move and does
# not record itself complete. The predicate is spelled CONTAINS because the
# embedded 2.x test engine drops IN matches that the compound scope index
# answers.
REOPEN_EMBEDDING_STATES = (
    "UPDATE embedding_states SET complete_metadata = NONE, complete_at = NONE, "
    "generation = (generation ?? 0) + 1, updated_at = time::now() "
    "WHERE $organizations CONTAINS organization_id RETURN NONE;"
)

__all__ = [
    "EMBEDDING_STATES_TABLE",
    "EMBEDDING_STATE_DEFINITIONS",
    "REOPEN_EMBEDDING_STATES",
]
