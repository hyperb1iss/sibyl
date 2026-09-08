-- A checkpoint is an observed revision, never a reconstructed correction time.
DEFINE FIELD OVERWRITE legacy_content_checkpoint ON raw_captures TYPE option<object> FLEXIBLE VALUE {
    LET $fields = $input.metadata ?? metadata ?? {};
    LET $history = $fields.correction_history ?? [];
    IF type::is::array($history) = false {
        THROW 'correction_history must be an array';
    };
    LET $legacy = $history.filter(|$entry| type::is::object($entry)
        AND string::lowercase(string::trim(type::string($entry.action ?? ''))) = 'revise'
        AND $entry.prior_revision = NONE);
    LET $clock = revision ?? 1;
    IF $before != NONE AND $before.entries = $legacy {
        RETURN $before;
    };
    IF $before != NONE
        AND array::slice($legacy, 0, array::len($before.entries)) != $before.entries {
        THROW 'legacy correction history cannot be replaced or removed';
    };
    IF $before = NONE AND $value != NONE AND $value.entries = $legacy
        AND type::is::int($value.observed_revision)
        AND $value.observed_revision >= 0 AND $value.observed_revision <= $clock
        AND (array::len($legacy) = 0 OR $value.observed_revision > 0) {
        RETURN $value;
    };
    RETURN {entries: $legacy, observed_revision: IF array::len($legacy) > 0 { $clock } ELSE { 0 }};
};
