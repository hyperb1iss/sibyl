from __future__ import annotations

import hashlib
from pathlib import Path

from sibyl_core.evals import (
    FROZEN_CONTEXT_PACK_SUITE_NAMES,
    GOLDEN_EVAL_SCHEMA_VERSION,
    load_context_pack_cases,
    load_golden_eval_dataset,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
DATASET_PATH = REPO_ROOT / "benchmarks/golden_context_retrieval_dataset.json"
CONTEXT_CASES_PATH = REPO_ROOT / "benchmarks/context_pack_cases.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def test_golden_dataset_loads_with_schema_and_eval_queries() -> None:
    dataset = load_golden_eval_dataset(DATASET_PATH)

    assert dataset.schema_version == GOLDEN_EVAL_SCHEMA_VERSION
    assert dataset.dataset_id == "sibyl-rc-context-retrieval-golden"
    assert len(dataset.documents) == 9
    assert len(dataset.retrieval_queries) == 8
    assert len(dataset.context_pack_cases) == 8

    eval_queries = dataset.to_eval_queries()
    assert eval_queries[0].query == dataset.retrieval_queries[0].query
    assert eval_queries[0].expected_ids == ["golden:silver-delta"]
    assert eval_queries[0].relevance_grades == {"golden:silver-delta": 3}
    assert eval_queries[0].metadata["id"] == "retrieval.coding-handoff"


def test_golden_dataset_hashes_declared_corpus_fixtures() -> None:
    dataset = load_golden_eval_dataset(DATASET_PATH)
    fixtures = dataset.corpus["fixtures"]

    for fixture in fixtures:
        path = REPO_ROOT / fixture["path"]
        assert path.exists(), fixture["path"]
        assert fixture["sha256"] == _sha256_file(path)


def test_golden_context_cases_cover_frozen_suite_labels() -> None:
    dataset = load_golden_eval_dataset(DATASET_PATH)
    context_cases = {case.name: case for case in load_context_pack_cases(CONTEXT_CASES_PATH)}

    assert dataset.context_case_names == FROZEN_CONTEXT_PACK_SUITE_NAMES
    assert set(context_cases) == FROZEN_CONTEXT_PACK_SUITE_NAMES

    for golden_case in dataset.context_pack_cases:
        fixture = context_cases[golden_case.case].fixture
        required_terms = {
            label.value for label in golden_case.labels if label.kind == "required_term"
        }
        forbidden_terms = {
            label.value for label in golden_case.negative_labels if label.kind == "forbidden_term"
        }

        assert required_terms == fixture.required_terms
        assert forbidden_terms == fixture.forbidden_terms


def test_golden_retrieval_queries_match_context_case_goals() -> None:
    dataset = load_golden_eval_dataset(DATASET_PATH)
    context_goals = {case.name: case.goal for case in load_context_pack_cases(CONTEXT_CASES_PATH)}

    for query in dataset.retrieval_queries:
        context_case = query.metadata["context_case"]
        assert query.query == context_goals[context_case]
        assert set(query.expected_ids).issubset(dataset.document_ids)
        assert set(query.expected_ids).issubset(query.relevance_grades)
