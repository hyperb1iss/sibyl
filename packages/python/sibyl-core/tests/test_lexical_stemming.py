"""The index stems, and Python stems the same way.

Morphological normalization belongs to the search engine: every BM25 index is
analyzed with `snowball(english)`, and the MATCHES operator runs the query
string through the same chain. These checks pin that arrangement from both
ends, so neither the analyzer chain nor the Python normalizer can drift away
from the other without a failure, and hand-written morphology tables cannot
quietly grow back.
"""

from __future__ import annotations

import re

import pytest
from surrealdb import AsyncSurreal

from sibyl_core.backends.surreal.content_schema import (
    CONTENT_ANALYZER_DEFINITIONS,
    CONTENT_SCHEMA_DEFINITIONS,
)
from sibyl_core.backends.surreal.schema import ANALYZER_DEFINITIONS, NODE_DEFINITIONS
from sibyl_core.query_anchors import (
    _GRADED_ADJECTIVE_ALIASES,
    is_derivational_form,
    normalize_keyword_token,
)
from sibyl_core.retrieval.fact_frames import (
    _SENSE_VOCABULARY,
    extract_evidence_fact_frames,
)

_ANALYZER_SOURCES = f"{ANALYZER_DEFINITIONS}\n{CONTENT_ANALYZER_DEFINITIONS}"
_INDEX_SOURCES = f"{NODE_DEFINITIONS}\n{CONTENT_SCHEMA_DEFINITIONS}"

# Source code is the one analyzer that must not stem: identifiers are not
# English and folding them collides unrelated symbols.
_UNSTEMMED_ANALYZERS = {"code_analyzer"}

_PROBE_TEXT = (
    "meetings attended policies batteries analysis running subscribed "
    "presentations classes watches boxes prices companies resources studying "
    "volunteered documentaries recommendations cafe naive developer"
)


def _analyzer_chains() -> dict[str, str]:
    return {
        match.group("name"): match.group("chain")
        for match in re.finditer(
            r"DEFINE ANALYZER (?:IF NOT EXISTS |OVERWRITE )?(?P<name>\w+)(?P<chain>.*?);",
            _ANALYZER_SOURCES,
            re.DOTALL,
        )
    }


def test_every_fulltext_index_is_backed_by_a_stemming_analyzer() -> None:
    chains = _analyzer_chains()
    referenced = set(re.findall(r"FULLTEXT ANALYZER (\w+)", _INDEX_SOURCES))

    assert referenced, "no fulltext indexes found; the source constants moved"
    # Pinned rather than subtracted: adding an analyzer to the exemption set is
    # how a non-stemming index would otherwise slip past this check.
    assert referenced & _UNSTEMMED_ANALYZERS == {"code_analyzer"}
    for analyzer in sorted(referenced - _UNSTEMMED_ANALYZERS):
        assert "snowball(english)" in chains[analyzer], (
            f"{analyzer} backs a BM25 index but does not stem, so query-side and "
            f"index-side morphology would diverge"
        )


def test_the_code_analyzer_deliberately_does_not_stem() -> None:
    assert "snowball" not in _analyzer_chains()["code_analyzer"]


@pytest.mark.asyncio
async def test_python_normalization_matches_the_engine_analyzer() -> None:
    """The ranker folds a token to exactly what the index stored for it."""
    db = AsyncSurreal("memory://")
    await db.connect()
    await db.use("stemming", "graph")
    for statement in ANALYZER_DEFINITIONS.split(";"):
        if statement.strip():
            await db.query(f"{statement};")

    engine = await db.query(
        "RETURN search::analyze('content_analyzer', $text);", {"text": _PROBE_TEXT}
    )

    assert engine == [normalize_keyword_token(word) for word in _PROBE_TEXT.split()]

    # The graded adjectives are the sole deliberate divergence: Python folds
    # them, the index does not, so they are the one place a query term and an
    # indexed term can differ.
    graded = await db.query(
        "RETURN search::analyze('content_analyzer', $text);", {"text": "fastest strongest"}
    )
    assert graded == ["fastest", "strongest"]

    # normalize_keyword_token folds accents the way the analyzer's ascii filter
    # does, checked here at the function boundary only. It is NOT end-to-end
    # parity: every tokenizer feeding this function matches ASCII characters
    # only, so an accented word is split before it ever arrives, and no
    # production path reaches the branch below. Widening those patterns is a
    # separate change; as it stands the text is under-tokenized rather than
    # mismatched, which fails safe.
    accented = await db.query(
        "RETURN search::analyze('content_analyzer', $text);", {"text": "café naïve Zürich"}
    )
    assert accented == [normalize_keyword_token(word) for word in ("café", "naïve", "Zürich")]
    assert [normalize_keyword_token(word) for word in ("fastest", "strongest")] == [
        "fast",
        "strong",
    ]
    await db.close()


@pytest.mark.asyncio
async def test_a_query_finds_the_document_that_spells_the_word_differently() -> None:
    """End to end: index-side and query-side inflections meet in the middle."""
    db = AsyncSurreal("memory://")
    await db.connect()
    await db.use("stemming", "graph")
    await db.query(
        "DEFINE ANALYZER content_analyzer TOKENIZERS blank, class "
        "FILTERS lowercase, ascii, snowball(english);"
    )
    await db.query("DEFINE TABLE note SCHEMALESS;")
    await db.query("DEFINE FIELD body ON note TYPE string;")
    await db.query(
        "DEFINE INDEX idx_note_body ON note FIELDS body SEARCH ANALYZER content_analyzer BM25;"
    )
    await db.query(
        "CREATE note:standup SET body = 'I attended three standup meetings about pricing';"
    )

    for term in ("meeting", "meetings", "attend", "attended", "attending", "price"):
        found = await db.query("SELECT id FROM note WHERE body @@ $term;", {"term": term})
        assert found, f"{term!r} should reach the indexed document"

    await db.close()


@pytest.mark.asyncio
async def test_graded_adjectives_are_the_gap_the_stemmer_leaves() -> None:
    """Why the one surviving table survives: Snowball unifies none of it.

    Comparatives and superlatives are the documented exception, so this is the
    receipt for keeping _GRADED_ADJECTIVE_ALIASES rather than a habit. If a
    future Snowball starts folding them, this fails and the table can go.
    """
    db = AsyncSurreal("memory://")
    await db.connect()
    await db.use("stemming", "graph")
    await db.query(
        "DEFINE ANALYZER content_analyzer TOKENIZERS blank, class "
        "FILTERS lowercase, ascii, snowball(english);"
    )

    async def stem(word: str) -> str:
        analyzed = await db.query(
            "RETURN search::analyze('content_analyzer', $text);", {"text": word}
        )
        return str(analyzed[0])

    unified = [
        inflected
        for inflected, base in _GRADED_ADJECTIVE_ALIASES.items()
        if await stem(inflected) == await stem(base)
    ]

    assert unified == []
    # Regular inflection, by contrast, needs no table at all.
    for inflected, base in (("attended", "attend"), ("classes", "class"), ("policies", "policy")):
        assert await stem(inflected) == await stem(base) == normalize_keyword_token(inflected)

    await db.close()


def test_a_listed_vocabulary_word_is_never_treated_as_a_derivation() -> None:
    """Membership decides before the suffix rule does.

    Snowball folds derivations onto their verb, so sense groups must ignore
    "useful" and "playful". English nouns end the same way, though, and
    "family" is the relative group's own entry, so a bare suffix rule silently
    stopped a memory about family from reading as one.
    """
    assert is_derivational_form("useful") is True
    assert is_derivational_form("playful") is True
    assert is_derivational_form("completely") is True

    for word in ("family", "supply", "assembly"):
        assert is_derivational_form(word, vocabulary={word}) is False
    assert is_derivational_form("family", vocabulary=_SENSE_VOCABULARY) is False
    # "friendly" is not listed anywhere, so it stays out of the friend relation.
    assert is_derivational_form("friendly", vocabulary=_SENSE_VOCABULARY) is True


def test_family_still_reads_as_a_relative() -> None:
    for text in ("I visited my family", "I visited my families"):
        frames = extract_evidence_fact_frames(text)
        assert frames and "relative" in frames[0].relations, text

    for text in ("that was really useful", "a playful puppy", "the attendant helped"):
        frames = extract_evidence_fact_frames(text)
        assert not (frames and frames[0].actions), text
