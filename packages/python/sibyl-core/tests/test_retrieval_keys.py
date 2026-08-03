"""Writer-declared retrieval keys: normalization, and the query detector matrix."""

from __future__ import annotations

import pytest

from sibyl_core.memory_pipeline.retrieval_keys import (
    MAX_RETRIEVAL_KEY_LENGTH,
    MAX_RETRIEVAL_KEYS,
    coerce_retrieval_keys,
    normalize_retrieval_keys,
    retrieval_key_match_form,
)
from sibyl_core.retrieval.identifier_query import (
    MAX_IDENTIFIER_PROBE_TOKENS,
    identifier_probe_tokens,
    is_identifier_shaped_query,
    is_identifier_shaped_token,
)
from sibyl_core.tools.helpers import MAX_TITLE_LENGTH

# ---------------------------------------------------------------------------
# Key normalization
# ---------------------------------------------------------------------------


def test_key_length_bound_is_the_title_bound() -> None:
    """The cited derivation, enforced: a key names a memory, so it is title-sized."""

    assert MAX_RETRIEVAL_KEY_LENGTH == MAX_TITLE_LENGTH


def test_none_declares_nothing() -> None:
    assert normalize_retrieval_keys(None) == ([], [])
    assert coerce_retrieval_keys(None) is None


def test_empty_list_is_a_statement_not_a_silence() -> None:
    assert coerce_retrieval_keys([]) == ([], [])


def test_display_form_keeps_casing_and_match_form_casefolds() -> None:
    display, match = normalize_retrieval_keys(["ERR_CONN_RESET_0x7F31"])

    assert display == ["ERR_CONN_RESET_0x7F31"]
    assert match == ["err_conn_reset_0x7f31"]


def test_keys_are_stripped_and_internal_whitespace_collapses() -> None:
    display, match = normalize_retrieval_keys(["  connection   reset\nby peer  "])

    assert display == ["connection reset by peer"]
    assert match == ["connection reset by peer"]


def test_dedupe_is_case_insensitive_and_keeps_the_first_declaration() -> None:
    display, match = normalize_retrieval_keys(["ERR_X", "err_x", "Err_X"])

    assert display == ["ERR_X"]
    assert match == ["err_x"]


def test_blank_keys_are_dropped_rather_than_stored() -> None:
    display, match = normalize_retrieval_keys(["", "   ", "\t\n", "real_key"])

    assert display == ["real_key"]
    assert match == ["real_key"]


def test_match_form_is_unicode_casefold_not_ascii_lower() -> None:
    assert retrieval_key_match_form("STRASSE") == retrieval_key_match_form("straße")


def test_over_long_key_is_refused_at_the_write_boundary() -> None:
    with pytest.raises(ValueError, match="exceeds 200 characters"):
        normalize_retrieval_keys(["k" * (MAX_RETRIEVAL_KEY_LENGTH + 1)])


def test_key_at_exactly_the_bound_is_accepted() -> None:
    display, _match = normalize_retrieval_keys(["k" * MAX_RETRIEVAL_KEY_LENGTH])

    assert display == ["k" * MAX_RETRIEVAL_KEY_LENGTH]


def test_too_many_keys_are_refused_rather_than_truncated() -> None:
    with pytest.raises(ValueError, match="at most 16 retrieval keys"):
        normalize_retrieval_keys([f"key_{index}" for index in range(MAX_RETRIEVAL_KEYS + 1)])


def test_control_characters_are_refused() -> None:
    with pytest.raises(ValueError, match="control characters"):
        normalize_retrieval_keys(["err\x00null"])


def test_non_string_key_is_refused() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        normalize_retrieval_keys([42])  # type: ignore[list-item]


def test_storage_edge_coerces_where_the_boundary_refuses() -> None:
    """The asymmetry: one bad entry must not fail a whole projection."""

    coerced = coerce_retrieval_keys(["good_key", 42, None, "k" * 400, ""])

    assert coerced is not None
    display, match = coerced
    assert display[0] == "good_key"
    assert len(display[1]) == MAX_RETRIEVAL_KEY_LENGTH
    assert len(display) == len(match) == 2


def test_storage_edge_caps_instead_of_raising() -> None:
    coerced = coerce_retrieval_keys([f"key_{index}" for index in range(40)])

    assert coerced is not None
    assert len(coerced[0]) == MAX_RETRIEVAL_KEYS


def test_storage_edge_accepts_a_bare_string() -> None:
    assert coerce_retrieval_keys("ERR_X") == (["ERR_X"], ["err_x"])


def test_storage_edge_ignores_a_shape_it_cannot_read() -> None:
    assert coerce_retrieval_keys({"not": "a list"}) is None


# ---------------------------------------------------------------------------
# The identifier-shaped query detector: positives
# ---------------------------------------------------------------------------

IDENTIFIER_POSITIVES = [
    # letters and digits in one token
    "ERR_CONN_RESET_0x7f31",
    "0x7f31",
    "sha256",
    "v2beta",
    "utf8mb4",
    # underscores
    "SIBYL_FUSION_BACKEND",
    "snake_case_name",
    "retrieval_keys",
    # scope separator
    "std::vector",
    "sibyl::core::Entity",
    # dotted segments
    "search.py",
    "sibyl_core.retrieval.search",
    "package.json",
    # hex run with a digit
    "89684d05a1b2c3d4",
    "deadbeef01",
    # internal case transition
    "EntityManager",
    "getUserById",
    "MemoryCaptureRequest",
]


# A decade and a duration cannot be told apart by their characters, and a
# duration appears verbatim in the error strings this arm exists to match, so the
# exclusion stops at ordinals and clock times and these all still fire.
KEPT_POSITIVES_NEAR_THE_NUMBER_EXCLUSION = [
    "2fa",
    "v2beta",
    "sha256",
    "utf8mb4",
    "0x7f31",
    "utf-8",
    "30s",
    "60s",
    "300s",
    "443s",
    "1990s",
]


@pytest.mark.parametrize("token", KEPT_POSITIVES_NEAR_THE_NUMBER_EXCLUSION)
def test_number_suffix_exclusion_keeps_real_identifiers(token: str) -> None:
    """The suffix list is closed, so digit-bearing identifiers still fire."""

    assert is_identifier_shaped_token(token)


@pytest.mark.parametrize("token", IDENTIFIER_POSITIVES)
def test_identifier_shaped_tokens_fire(token: str) -> None:
    assert is_identifier_shaped_token(token)
    assert identifier_probe_tokens(token) == (retrieval_key_match_form(token),)


# ---------------------------------------------------------------------------
# The identifier-shaped query detector: negatives
# ---------------------------------------------------------------------------

IDENTIFIER_NEGATIVES = [
    # ordinary English words, including ones that spell in hex
    "authentication",
    "database",
    "pooling",
    "connection",
    "deadbeefcafe",
    "accede",
    # capitalized proper nouns without an internal transition
    "Redis",
    "Sibyl",
    "Postgres",
    # acronyms without an underscore
    "TTL",
    "HTTP",
    "JWT",
    # bare numbers and years
    "2026",
    "42",
    # ordinals and clock times, including hyphen-joined and pluralized forms
    "3rd",
    "21st",
    "3rds",
    "10am",
    "7pm",
    "10am-11am",
    # dotted abbreviations, which is why segments must be two characters
    "e.g",
    "i.e",
    "a.m",
    "U.S",
    # hyphenated English
    "state-of-the-art",
    "well-known",
    # too short to carry a shape
    "a_",
    "x1",
    "ok",
    # punctuation alone
    "...",
    "---",
]


@pytest.mark.parametrize("token", IDENTIFIER_NEGATIVES)
def test_plain_language_tokens_do_not_fire(token: str) -> None:
    assert not is_identifier_shaped_token(token)


PROSE_QUERIES = [
    "how do we handle authentication",
    "what happened on the 3rd attempt",
    "the meeting is at 10am tomorrow",
    "the window is 10am-11am on the 2nd",
    "we tried it three 3rds of the way in",
    "database connection pooling",
    "what is the plan for 2026",
    "e.g. the first one we tried",
    "i.e. this approach",
    "state-of-the-art retrieval",
    "why does Redis expire the TTL",
    "the error was a reset",
    "who decided this and when",
    "",
    "   ",
]


@pytest.mark.parametrize("query", PROSE_QUERIES)
def test_prose_queries_leave_the_arm_inert(query: str) -> None:
    assert identifier_probe_tokens(query) == ()
    assert not is_identifier_shaped_query(query)


# ---------------------------------------------------------------------------
# The detector in whole queries
# ---------------------------------------------------------------------------


def test_one_identifier_inside_prose_fires_on_that_token_only() -> None:
    assert identifier_probe_tokens("why does ERR_CONN_RESET_0x7f31 keep firing on startup") == (
        "err_conn_reset_0x7f31",
    )


def test_trailing_sentence_punctuation_is_not_part_of_the_probe() -> None:
    assert identifier_probe_tokens("what about search.py?") == ("search.py",)
    assert identifier_probe_tokens("(see EntityManager),") == ("entitymanager",)


def test_generic_type_syntax_survives_trimming() -> None:
    assert identifier_probe_tokens("std::vector<int> blows up") == ("std::vector<int>",)


def test_a_quoted_span_is_taken_whole_including_spaces() -> None:
    assert identifier_probe_tokens('the log said "connection reset by peer" again') == (
        "connection reset by peer",
    )


def test_a_backtick_span_is_a_quoted_span() -> None:
    assert identifier_probe_tokens("the flag is `defer embeddings`") == ("defer embeddings",)


def test_an_apostrophe_is_not_a_quote() -> None:
    assert identifier_probe_tokens("what didn't work in the plan") == ()


def test_probes_dedupe_case_insensitively_and_preserve_order() -> None:
    assert identifier_probe_tokens("ERR_X then err_x then OTHER_KEY") == ("err_x", "other_key")


def test_probe_count_is_bounded_by_the_writer_cap() -> None:
    query = " ".join(f"key_{index}" for index in range(MAX_IDENTIFIER_PROBE_TOKENS * 3))

    assert len(identifier_probe_tokens(query)) == MAX_IDENTIFIER_PROBE_TOKENS


def test_a_quoted_span_and_a_bare_identifier_both_probe() -> None:
    probes = identifier_probe_tokens('"connection reset by peer" from ERR_CONN_RESET_0x7f31')

    assert probes == ("connection reset by peer", "err_conn_reset_0x7f31")
