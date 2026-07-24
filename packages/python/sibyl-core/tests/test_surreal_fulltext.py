"""Tests for the conjunctive-safe fulltext query helpers."""

from __future__ import annotations

import pytest

from sibyl_core.backends.surreal.fulltext import (
    build_fulltext_query,
    build_fulltext_terms,
    build_match_disjunction,
)


class TestBuildFulltextTerms:
    def test_sentence_keeps_all_salient_terms(self) -> None:
        terms = build_fulltext_terms(
            "Which task covers replay verification and migration acceptance?"
        )
        assert terms == [
            "task",
            "covers",
            "replay",
            "verification",
            "migration",
            "acceptance",
        ]

    def test_stopwords_and_short_tokens_dropped(self) -> None:
        assert build_fulltext_terms("is it a to be or not") == []
        assert build_fulltext_terms("a fix") == ["fix"]

    def test_punctuation_stripped_and_lowercased(self) -> None:
        assert build_fulltext_terms("Redis: TTL-mismatch!") == ["redis", "ttl", "mismatch"]

    def test_dedupes_preserving_order(self) -> None:
        assert build_fulltext_terms("replay replay migration replay") == [
            "replay",
            "migration",
        ]

    def test_cap_prefers_longer_terms_earlier_position_ties(self) -> None:
        terms = build_fulltext_terms("auth token refresh regeneration fallback wiring", max_terms=3)
        assert len(terms) == 3
        assert "regeneration" in terms

    def test_empty_and_unprintable_input(self) -> None:
        assert build_fulltext_terms("") == []
        assert build_fulltext_terms("   ") == []
        assert build_fulltext_terms("???!!!") == []


class TestBuildMatchDisjunction:
    def test_none_when_no_terms_or_fields(self) -> None:
        assert build_match_disjunction(["name"], []) is None
        assert build_match_disjunction([], ["replay"]) is None

    def test_single_field_single_term(self) -> None:
        match = build_match_disjunction(["content"], ["replay"])
        assert match is not None
        assert match.where_clause == "(content @0@ $ft_term0)"
        assert match.score_expr == "((search::score(0) ?? 0))"
        assert match.params == {"ft_term0": "replay"}

    def test_multi_field_multi_term_operator_numbering(self) -> None:
        match = build_match_disjunction(["name", "content"], ["replay", "migration"])
        assert match is not None
        assert match.where_clause == (
            "(name @0@ $ft_term0 OR name @1@ $ft_term1"
            " OR content @2@ $ft_term0 OR content @3@ $ft_term1)"
        )
        assert match.score_expr == (
            "math::max([((search::score(0) ?? 0) + (search::score(1) ?? 0)),"
            " ((search::score(2) ?? 0) + (search::score(3) ?? 0))])"
        )
        assert match.params == {"ft_term0": "replay", "ft_term1": "migration"}

    def test_operator_offset_shifts_indices(self) -> None:
        match = build_match_disjunction(["fact"], ["replay"], operator_offset=5)
        assert match is not None
        assert match.where_clause == "(fact @5@ $ft_term0)"
        assert "search::score(5)" in match.score_expr

    def test_sentence_query_produces_matchable_clause(self) -> None:
        terms = build_fulltext_terms(
            "Which task covers replay verification and migration acceptance?"
        )
        match = build_match_disjunction(["name", "summary", "description", "content"], terms)
        assert match is not None
        assert match.where_clause.count("@") == 2 * 4 * len(terms)
        for param, term in match.params.items():
            assert f"${param}" in match.where_clause
            assert " " not in term


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("fix", "fix"),
        ('quo"ted', "quoted"),
        ("x" * 200, "x" * 128),
    ],
)
def test_build_fulltext_query_sanitizes(query: str, expected: str) -> None:
    assert build_fulltext_query(query) == expected
