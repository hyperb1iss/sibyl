"""Protected source generations for deterministic operational relationships."""

OPERATIONAL_RELATIONSHIP_DEFINITIONS = """
DEFINE FIELD IF NOT EXISTS operational_derivation_required ON relates_to TYPE option<bool> DEFAULT false;
DEFINE FIELD IF NOT EXISTS operational_source_binding ON relates_to TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS validation_write_witness ON memory_derivations TYPE option<string>;
"""
