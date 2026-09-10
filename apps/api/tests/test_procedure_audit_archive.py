"""Content archives preserve the lossless audit storage representation."""

import json

from sibyl.persistence.content_archive import _deserialize_value, _serialize_value
from sibyl_core.memory_pipeline.audit import decode_audit_metadata, encode_audit_metadata


def test_procedure_audit_archive_codec_retains_nulls():
    original = {"conditional_procedure": {"usage": {"cost_usd": None}, "nested": [None]}}
    storage = encode_audit_metadata(original)
    archived = json.loads(json.dumps(_serialize_value(storage)))
    restored = _deserialize_value(archived)
    assert restored == storage
    assert decode_audit_metadata(restored) == original
