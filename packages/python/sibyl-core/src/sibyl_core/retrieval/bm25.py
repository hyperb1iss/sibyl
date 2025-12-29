"""BM25 keyword search for exact matching.

Implements Okapi BM25 algorithm for keyword-based retrieval.
Complements vector search by finding exact term matches.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger()


@dataclass
class BM25Config:
    """Configuration for BM25 search.

    Attributes:
        k1: Term frequency saturation parameter (1.2-2.0 typical).
        b: Length normalization parameter (0.75 typical).
        min_token_length: Minimum token length to index.
        stop_words: Words to exclude from indexing.
    """

    k1: float = 1.5
    b: float = 0.75
    min_token_length: int = 2
    stop_words: set[str] = field(
        default_factory=lambda: {
            "the",
            "a",
            "an",
            "and",
            "or",
            "but",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "with",
            "by",
            "from",
            "as",
            "is",
            "was",
            "are",
            "were",
            "been",
            "be",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "could",
            "should",
            "may",
            "might",
            "must",
            "shall",
            "can",
            "need",
            "this",
            "that",
            "these",
            "those",
            "it",
            "its",
            "they",
            "them",
            "their",
        }
    )


def tokenize(text: str, min_length: int = 2, stop_words: set[str] | None = None) -> list[str]:
    """Tokenize text into lowercase words.

    Args:
        text: Input text.
        min_length: Minimum token length.
        stop_words: Words to exclude.

    Returns:
        List of tokens.
    """
    if not text:
        return []

    # Simple tokenization: split on non-alphanumeric, lowercase
    tokens = re.findall(r"\b[a-zA-Z0-9]+\b", text.lower())

    # Filter by length and stop words
    stop = stop_words or set()
    return [t for t in tokens if len(t) >= min_length and t not in stop]


def extract_text(entity: Any, fields: list[str] | None = None) -> str:
    """Extract searchable text from an entity.

    Args:
        entity: Entity object or dict.
        fields: Fields to extract (default: name, description, content).

    Returns:
        Combined text from all fields.
    """
    if fields is None:
        fields = ["name", "title", "description", "content"]

    parts: list[str] = []

    for field_name in fields:
        if isinstance(entity, dict):
            value = entity.get(field_name, "")
        else:
            value = getattr(entity, field_name, "")

        if value and isinstance(value, str):
            parts.append(value)

    return " ".join(parts)


class BM25Index:
    """In-memory BM25 index for entity search.

    Usage:
        index = BM25Index()
        index.add(entity1)
        index.add(entity2)
        results = index.search("python async", limit=10)
    """

    def __init__(
        self,
        config: BM25Config | None = None,
        text_extractor: Callable[[Any], str] | None = None,
        id_extractor: Callable[[Any], str] | None = None,
    ) -> None:
        """Initialize BM25 index.

        Args:
            config: BM25 configuration.
            text_extractor: Function to extract text from entity.
            id_extractor: Function to extract ID from entity.
        """
        self.config = config or BM25Config()
        self._text_extractor = text_extractor or extract_text
        self._id_extractor = id_extractor or (
            lambda e: str(e.get("id") if isinstance(e, dict) else getattr(e, "id", id(e)))
        )

        # Index structures
        self._entities: dict[str, Any] = {}  # id -> entity
        self._doc_lengths: dict[str, int] = {}  # id -> token count
        self._term_freqs: dict[str, dict[str, int]] = {}  # id -> {term: count}
        self._doc_freqs: dict[str, int] = defaultdict(int)  # term -> doc count
        self._avg_doc_length: float = 0.0
        self._total_docs: int = 0

    def add(self, entity: Any) -> str:
        """Add an entity to the index.

        Args:
            entity: Entity to index.

        Returns:
            Entity ID.
        """
        entity_id = self._id_extractor(entity)
        text = self._text_extractor(entity)
        tokens = tokenize(text, self.config.min_token_length, self.config.stop_words)

        # Check if new entity BEFORE storing
        is_new = entity_id not in self._entities

        # Store entity
        self._entities[entity_id] = entity

        # Update term frequencies
        term_freq: dict[str, int] = defaultdict(int)
        for token in tokens:
            term_freq[token] += 1

        # Update document frequencies (count unique terms per doc)
        old_terms = set(self._term_freqs.get(entity_id, {}).keys())
        new_terms = set(term_freq.keys())

        # Decrement counts for removed terms
        for term in old_terms - new_terms:
            self._doc_freqs[term] -= 1

        # Increment counts for new terms
        for term in new_terms - old_terms:
            self._doc_freqs[term] += 1

        self._term_freqs[entity_id] = dict(term_freq)
        self._doc_lengths[entity_id] = len(tokens)

        # Update averages
        if is_new:
            self._total_docs += 1
        self._avg_doc_length = (
            sum(self._doc_lengths.values()) / self._total_docs if self._total_docs > 0 else 0.0
        )

        return entity_id

    def remove(self, entity_id: str) -> bool:
        """Remove an entity from the index.

        Args:
            entity_id: ID of entity to remove.

        Returns:
            True if removed, False if not found.
        """
        if entity_id not in self._entities:
            return False

        # Decrement doc frequencies
        for term in self._term_freqs.get(entity_id, {}):
            self._doc_freqs[term] -= 1

        # Remove from all structures
        del self._entities[entity_id]
        del self._doc_lengths[entity_id]
        del self._term_freqs[entity_id]
        self._total_docs -= 1

        # Recalculate average
        self._avg_doc_length = (
            sum(self._doc_lengths.values()) / self._total_docs if self._total_docs > 0 else 0.0
        )

        return True

    def _idf(self, term: str) -> float:
        """Calculate inverse document frequency for a term."""
        n = self._total_docs
        df = self._doc_freqs.get(term, 0)

        if df == 0:
            return 0.0

        # Standard IDF formula with smoothing
        return math.log((n - df + 0.5) / (df + 0.5) + 1.0)

    def _score_document(self, entity_id: str, query_terms: list[str]) -> float:
        """Calculate BM25 score for a document against query terms."""
        if entity_id not in self._entities:
            return 0.0

        doc_length = self._doc_lengths.get(entity_id, 0)
        term_freqs = self._term_freqs.get(entity_id, {})

        k1 = self.config.k1
        b = self.config.b
        avgdl = self._avg_doc_length if self._avg_doc_length > 0 else 1.0

        score = 0.0
        for term in query_terms:
            if term not in term_freqs:
                continue

            tf = term_freqs[term]
            idf = self._idf(term)

            # BM25 formula
            numerator = tf * (k1 + 1)
            denominator = tf + k1 * (1 - b + b * (doc_length / avgdl))
            score += idf * (numerator / denominator)

        return score

    def search(
        self,
        query: str,
        limit: int = 10,
        min_score: float = 0.0,
    ) -> list[tuple[Any, float]]:
        """Search the index for matching entities.

        Args:
            query: Search query.
            limit: Maximum results to return.
            min_score: Minimum score threshold.

        Returns:
            List of (entity, score) tuples sorted by score descending.
        """
        query_terms = tokenize(query, self.config.min_token_length, self.config.stop_words)

        if not query_terms:
            return []

        # Score all documents
        scores: list[tuple[str, float]] = []
        for entity_id in self._entities:
            score = self._score_document(entity_id, query_terms)
            if score > min_score:
                scores.append((entity_id, score))

        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)

        # Build result list
        results: list[tuple[Any, float]] = []
        for entity_id, score in scores[:limit]:
            results.append((self._entities[entity_id], score))

        log.debug(
            "bm25_search",
            query=query[:50],
            terms=query_terms,
            results=len(results),
        )

        return results

    @property
    def size(self) -> int:
        """Number of indexed documents."""
        return self._total_docs

    def clear(self) -> None:
        """Clear the entire index."""
        self._entities.clear()
        self._doc_lengths.clear()
        self._term_freqs.clear()
        self._doc_freqs.clear()
        self._avg_doc_length = 0.0
        self._total_docs = 0


# Global index instance
_bm25_index: BM25Index | None = None


def get_bm25_index() -> BM25Index:
    """Get the global BM25 index."""
    global _bm25_index
    if _bm25_index is None:
        _bm25_index = BM25Index()
    return _bm25_index


def reset_bm25_index() -> None:
    """Reset the global BM25 index."""
    global _bm25_index
    if _bm25_index is not None:
        _bm25_index.clear()
    _bm25_index = None


def bm25_search(
    query: str,
    limit: int = 10,
    min_score: float = 0.0,
) -> list[tuple[Any, float]]:
    """Search the global BM25 index.

    Convenience function using the global index.
    """
    return get_bm25_index().search(query, limit, min_score)
