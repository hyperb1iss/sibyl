"""Evaluation helpers for Sibyl context packs."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path
from typing import Any

from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextItemQualityMetadata,
    ContextLayer,
    ContextPack,
    ContextSection,
)
from sibyl_core.tools.context import context_pack_to_markdown

APPROX_CHARS_PER_TOKEN = 4
APPROX_TOKEN_SAFETY_MARGIN = 1.2
FROZEN_CONTEXT_PACK_SUITE_NAMES = frozenset(
    {
        "agent-diary",
        "coding-handoff",
        "delegated-recall",
        "personal-memory",
        "private-leak-negative",
        "project-recall",
        "source-grounding",
        "stale-decision-replacement",
    }
)


@dataclass(frozen=True)
class ContextPackFixture:
    """Expected behavior for a context-pack dogfood fixture."""

    name: str
    required_item_ids: set[str] = field(default_factory=set)
    forbidden_item_ids: set[str] = field(default_factory=set)
    required_facets: set[ContextFacet] = field(default_factory=set)
    required_facet_order: list[ContextFacet] = field(default_factory=list)
    required_layer: ContextLayer | None = None
    required_terms: set[str] = field(default_factory=set)
    forbidden_terms: set[str] = field(default_factory=set)
    required_item_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    required_metadata_by_type: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_items: int | None = None
    max_markdown_chars: int | None = None
    max_estimated_tokens: int | None = None
    max_latency_ms: float | None = None
    require_source_metadata: bool = False


@dataclass(frozen=True)
class ContextPackEvalResult:
    """Result from evaluating one context pack against one fixture."""

    fixture: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextPackEvalCase:
    """One context-pack benchmark case."""

    name: str
    goal: str
    fixture: ContextPackFixture
    intent: ContextIntent = ContextIntent.BUILD
    layer: ContextLayer = ContextLayer.RECALL
    domain: str | None = None
    project: str | None = None
    agent_id: str | None = None
    limit: int = 24
    include_related: bool = True
    related_limit: int = 3


@dataclass(frozen=True)
class ContextPackCaseResult:
    """Evaluation output for one context-pack case."""

    case: ContextPackEvalCase
    result: ContextPackEvalResult
    latency_ms: float = 0.0
    error: str | None = None
    repeat_index: int = 1


def _numeric_case_values(cases: list[ContextPackCaseResult], key: str) -> list[float]:
    values: list[float] = []
    for case in cases:
        value = case.result.metrics.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            values.append(float(value))
    return values


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _max(values: list[float]) -> float:
    return max(values) if values else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = ceil((percentile / 100) * len(ordered))
    index = min(max(rank - 1, 0), len(ordered) - 1)
    return ordered[index]


def _bool_case_rate(cases: list[ContextPackCaseResult], key: str) -> float:
    values = [
        case.result.metrics[key] for case in cases if isinstance(case.result.metrics.get(key), bool)
    ]
    return sum(1 for value in values if value) / len(values) if values else 0.0


def _numeric_case_value(case: ContextPackCaseResult, key: str) -> float:
    value = case.result.metrics.get(key)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _sum_case_leak_counts(cases: list[ContextPackCaseResult]) -> float:
    return sum(
        max(
            _numeric_case_value(case, "forbidden_item_matches"),
            _numeric_case_value(case, "forbidden_term_matches"),
        )
        for case in cases
    )


def _budgeted_token_values(cases: list[ContextPackCaseResult]) -> list[float]:
    values: list[float] = []
    for case in cases:
        value = case.result.metrics.get("budgeted_estimated_tokens")
        if isinstance(value, int | float) and not isinstance(value, bool):
            values.append(float(value))
            continue
        estimated = case.result.metrics.get("estimated_tokens")
        if isinstance(estimated, int | float) and not isinstance(estimated, bool):
            values.append(float(ceil(float(estimated) * APPROX_TOKEN_SAFETY_MARGIN)))
    return values


@dataclass
class ContextPackEvalReport:
    """Complete context-pack benchmark report."""

    cases: list[ContextPackCaseResult]
    label: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    @property
    def passed(self) -> bool:
        return all(case.result.passed and case.error is None for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        case_count = len(self.cases)
        passed_cases = sum(1 for case in self.cases if case.result.passed and case.error is None)
        repeat_count = max((case.repeat_index for case in self.cases), default=0)
        latencies = [case.latency_ms for case in self.cases]
        item_counts = _numeric_case_values(self.cases, "items")
        markdown_chars = _numeric_case_values(self.cases, "markdown_chars")
        estimated_tokens = _numeric_case_values(self.cases, "estimated_tokens")
        budgeted_estimated_tokens = _budgeted_token_values(self.cases)
        source_metadata_coverage = _numeric_case_values(self.cases, "source_metadata_coverage")
        forbidden_term_matches = _numeric_case_values(self.cases, "forbidden_term_matches")
        forbidden_item_matches = _numeric_case_values(self.cases, "forbidden_item_matches")
        return {
            "timestamp": self.timestamp,
            "label": self.label,
            "search_type": "context-pack",
            "metadata": dict(self.metadata),
            "token_estimator": {
                "method": "approximate_character_count",
                "characters_per_token": APPROX_CHARS_PER_TOKEN,
                "safety_margin_multiplier": APPROX_TOKEN_SAFETY_MARGIN,
            },
            "metrics": {
                "cases": case_count,
                "passed": passed_cases,
                "failed": case_count - passed_cases,
                "repeat_count": repeat_count,
                "case_count_per_repeat": case_count / repeat_count if repeat_count else 0.0,
                "pass_rate": passed_cases / case_count if case_count else 0.0,
                "latency_ms": _average(latencies),
                "latency_p95_ms": _percentile(latencies, 95),
                "max_latency_ms": _max(latencies),
                "avg_items": _average(item_counts),
                "max_items": _max(item_counts),
                "avg_markdown_chars": _average(markdown_chars),
                "max_markdown_chars": _max(markdown_chars),
                "avg_estimated_tokens": _average(estimated_tokens),
                "max_estimated_tokens": _max(estimated_tokens),
                "avg_budgeted_estimated_tokens": _average(budgeted_estimated_tokens),
                "max_budgeted_estimated_tokens": _max(budgeted_estimated_tokens),
                "source_metadata_coverage": _average(source_metadata_coverage),
                "facet_order_match_rate": _bool_case_rate(self.cases, "facet_order_matches"),
                "forbidden_item_matches": sum(forbidden_item_matches),
                "forbidden_term_matches": sum(forbidden_term_matches),
                "leak_count": _sum_case_leak_counts(self.cases),
            },
            "per_case": [
                {
                    "name": case.case.name,
                    "repeat_index": case.repeat_index,
                    "goal": case.case.goal,
                    "intent": case.case.intent.value,
                    "layer": case.case.layer.value,
                    "domain": case.case.domain,
                    "project": case.case.project,
                    "agent_id": case.case.agent_id,
                    "fixture": case.result.fixture,
                    "passed": case.result.passed and case.error is None,
                    "error": case.error,
                    "failures": list(case.result.failures),
                    "metrics": dict(case.result.metrics),
                    "latency_ms": case.latency_ms,
                }
                for case in self.cases
            ],
        }

    def save(self, output_dir: Path, path: Path | None = None) -> Path:
        if path is None:
            output_dir.mkdir(parents=True, exist_ok=True)
            label = ""
            if self.label:
                slug = re.sub(r"[^a-z0-9]+", "_", self.label.lower()).strip("_")
                if slug:
                    label = f"_{slug}"
            path = output_dir / f"eval_context_pack{label}_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    def print_summary(self) -> None:
        report = self.to_dict()
        metrics = report["metrics"]
        print("\nSibyl context-pack evaluation summary")
        print(f"  cases: {metrics['cases']}")
        print(f"  repeat_count: {metrics['repeat_count']}")
        print(f"  pass_rate: {metrics['pass_rate']:.3f}")
        print(f"  failed: {metrics['failed']}")
        print(f"  latency_ms: {metrics['latency_ms']:.1f}")
        print(f"  latency_p95_ms: {metrics['latency_p95_ms']:.1f}")
        failures = [case for case in report["per_case"] if not case["passed"]]
        if failures:
            print("\nFailed context-pack cases")
            seen: set[tuple[str, tuple[str, ...]]] = set()
            for case in failures:
                reasons = tuple(str(reason) for reason in case["failures"])
                signature = (str(case["name"]), reasons)
                if signature in seen:
                    continue
                seen.add(signature)
                print(f"  {case['name']} (repeat {case['repeat_index']}):")
                for reason in reasons:
                    print(f"    - {reason}")


def _quality_value(item: ContextItem, key: str) -> Any:
    quality = getattr(item, "quality", None)
    if isinstance(quality, dict):
        return quality.get(key)
    return getattr(quality, key, None)


def _has_source_metadata(item: ContextItem) -> bool:
    metadata = item.metadata or {}
    return any(
        value
        for value in (
            item.source,
            metadata.get("source_id"),
            metadata.get("source"),
            metadata.get("source_file"),
            metadata.get("url"),
            _quality_value(item, "source"),
            _quality_value(item, "url"),
        )
    )


def _metadata_value(item: ContextItem, key: str) -> Any:
    metadata = item.metadata or {}
    if key in metadata:
        return metadata[key]
    if hasattr(item, key):
        return getattr(item, key)
    return _quality_value(item, key)


def _searchable_text(pack: ContextPack) -> str:
    chunks: list[str] = [pack.goal, pack.query, pack.domain or "", pack.project or ""]
    for item in pack.items:
        chunks.extend([item.name, item.content, item.reason, item.source or ""])
    return "\n".join(chunks).lower()


def _selected_context_text(pack: ContextPack) -> str:
    chunks: list[str] = []
    for item in pack.items:
        chunks.extend([item.name, item.content, item.reason, item.source or ""])
    return "\n".join(chunks).lower()


def _estimate_tokens(text: str) -> int:
    return (len(text) + APPROX_CHARS_PER_TOKEN - 1) // APPROX_CHARS_PER_TOKEN


def evaluate_context_pack(
    pack: ContextPack,
    fixture: ContextPackFixture,
) -> ContextPackEvalResult:
    """Evaluate whether a context pack satisfies a dogfood fixture."""

    failures: list[str] = []
    item_ids = {item.id for item in pack.items}
    facets = {section.facet for section in pack.sections}

    missing_ids = sorted(fixture.required_item_ids - item_ids)
    if missing_ids:
        failures.append(f"missing required items: {', '.join(missing_ids)}")

    forbidden_ids = sorted(fixture.forbidden_item_ids & item_ids)
    if forbidden_ids:
        failures.append(f"forbidden items present: {', '.join(forbidden_ids)}")

    missing_facets = sorted(facet.value for facet in fixture.required_facets - facets)
    if missing_facets:
        failures.append(f"missing required facets: {', '.join(missing_facets)}")

    facet_order = [section.facet for section in pack.sections]
    facet_order_matches = (
        not fixture.required_facet_order
        or facet_order[: len(fixture.required_facet_order)] == fixture.required_facet_order
    )
    if not facet_order_matches:
        expected = ", ".join(facet.value for facet in fixture.required_facet_order)
        actual = ", ".join(
            facet.value for facet in facet_order[: len(fixture.required_facet_order)]
        )
        failures.append(f"facet order mismatch: expected prefix {expected} got {actual}")

    if fixture.required_layer is not None and pack.layer != fixture.required_layer:
        failures.append(
            f"wrong context layer: expected {fixture.required_layer.value} got {pack.layer.value}"
        )

    if fixture.max_items is not None and pack.total_items > fixture.max_items:
        failures.append(f"too many items: {pack.total_items} > {fixture.max_items}")

    markdown = context_pack_to_markdown(pack, max_items=max(pack.total_items, 1))
    if fixture.max_markdown_chars is not None and len(markdown) > fixture.max_markdown_chars:
        failures.append(f"markdown too large: {len(markdown)} chars > {fixture.max_markdown_chars}")

    estimated_tokens = _estimate_tokens(markdown)
    budgeted_estimated_tokens = ceil(estimated_tokens * APPROX_TOKEN_SAFETY_MARGIN)
    if (
        fixture.max_estimated_tokens is not None
        and budgeted_estimated_tokens > fixture.max_estimated_tokens
    ):
        failures.append(
            "estimated tokens too high: "
            f"{budgeted_estimated_tokens} > {fixture.max_estimated_tokens} "
            "(includes 20% safety margin)"
        )

    text = _searchable_text(pack)
    missing_terms = sorted(term for term in fixture.required_terms if term.lower() not in text)
    if missing_terms:
        failures.append(f"missing required terms: {', '.join(missing_terms)}")

    selected_context_text = _selected_context_text(pack)
    forbidden_terms = sorted(
        term for term in fixture.forbidden_terms if term.lower() in selected_context_text
    )
    if forbidden_terms:
        failures.append(f"forbidden terms present: {', '.join(forbidden_terms)}")

    pack_items = pack.items
    unsourced = sorted(item.id for item in pack_items if not _has_source_metadata(item))
    if fixture.require_source_metadata and unsourced:
        failures.append(f"items missing source metadata: {', '.join(unsourced)}")

    items_by_id = {item.id: item for item in pack.items}
    metadata_checks = 0
    metadata_matches = 0
    for item_id, expected_metadata in sorted(fixture.required_item_metadata.items()):
        item = items_by_id.get(item_id)
        if item is None:
            failures.append(f"missing metadata target item: {item_id}")
            continue
        for key, expected_value in sorted(expected_metadata.items()):
            metadata_checks += 1
            actual_value = _metadata_value(item, key)
            if actual_value == expected_value:
                metadata_matches += 1
                continue
            failures.append(
                f"item {item_id} metadata {key} expected {expected_value!r} got {actual_value!r}"
            )

    scoped_metadata_checks = 0
    scoped_metadata_matches = 0
    for item in pack.items:
        expected_metadata = fixture.required_metadata_by_type.get(item.type)
        if expected_metadata is None:
            continue
        for key, expected_value in sorted(expected_metadata.items()):
            scoped_metadata_checks += 1
            actual_value = _metadata_value(item, key)
            if actual_value == expected_value:
                scoped_metadata_matches += 1
                continue
            failures.append(
                f"{item.type} item {item.id} metadata {key} expected {expected_value!r} "
                f"got {actual_value!r}"
            )

    metrics = {
        "items": pack.total_items,
        "facets": sorted(facet.value for facet in facets),
        "facet_order": [facet.value for facet in facet_order],
        "facet_order_matches": facet_order_matches,
        "markdown_chars": len(markdown),
        "estimated_tokens": estimated_tokens,
        "budgeted_estimated_tokens": budgeted_estimated_tokens,
        "source_metadata_coverage": (
            1.0 if not pack_items else (len(pack_items) - len(unsourced)) / len(pack_items)
        ),
        "required_item_coverage": (
            1.0
            if not fixture.required_item_ids
            else (len(fixture.required_item_ids) - len(missing_ids))
            / len(fixture.required_item_ids)
        ),
        "forbidden_item_matches": len(forbidden_ids),
        "metadata_requirement_coverage": (
            1.0
            if not metadata_checks + scoped_metadata_checks
            else (metadata_matches + scoped_metadata_matches)
            / (metadata_checks + scoped_metadata_checks)
        ),
        "forbidden_term_matches": len(forbidden_terms),
    }
    return ContextPackEvalResult(
        fixture=fixture.name,
        passed=not failures,
        failures=failures,
        metrics=metrics,
    )


def _string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list):
        msg = "expected a list of strings"
        raise TypeError(msg)
    return {str(item) for item in value}


def _facet_set(value: Any) -> set[ContextFacet]:
    return {ContextFacet(item) for item in _string_set(value)}


def _facet_list(value: Any) -> list[ContextFacet]:
    if value is None:
        return []
    if not isinstance(value, list):
        msg = "expected a list of facets"
        raise TypeError(msg)
    return [ContextFacet(str(item)) for item in value]


def _metadata_requirements(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        msg = "expected a mapping of item IDs to metadata requirements"
        raise TypeError(msg)
    requirements: dict[str, dict[str, Any]] = {}
    for item_id, expected_metadata in value.items():
        if not isinstance(expected_metadata, dict):
            msg = f"expected metadata requirements for {item_id!r} to be a mapping"
            raise TypeError(msg)
        requirements[str(item_id)] = dict(expected_metadata)
    return requirements


def _metadata_requirements_by_type(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        msg = "expected a mapping of item types to metadata requirements"
        raise TypeError(msg)
    requirements: dict[str, dict[str, Any]] = {}
    for item_type, expected_metadata in value.items():
        if not isinstance(expected_metadata, dict):
            msg = f"expected metadata requirements for {item_type!r} to be a mapping"
            raise TypeError(msg)
        requirements[str(item_type)] = dict(expected_metadata)
    return requirements


def _fixture_from_dict(name: str, data: dict[str, Any]) -> ContextPackFixture:
    return ContextPackFixture(
        name=str(data.get("name") or name),
        required_item_ids=_string_set(data.get("required_item_ids")),
        forbidden_item_ids=_string_set(data.get("forbidden_item_ids")),
        required_facets=_facet_set(data.get("required_facets")),
        required_facet_order=_facet_list(data.get("required_facet_order")),
        required_layer=_layer_from_value(data["required_layer"])
        if data.get("required_layer")
        else None,
        required_terms=_string_set(data.get("required_terms")),
        forbidden_terms=_string_set(data.get("forbidden_terms")),
        required_item_metadata=_metadata_requirements(data.get("required_item_metadata")),
        required_metadata_by_type=_metadata_requirements_by_type(
            data.get("required_metadata_by_type")
        ),
        max_items=data.get("max_items"),
        max_markdown_chars=data.get("max_markdown_chars"),
        max_estimated_tokens=data.get("max_estimated_tokens"),
        max_latency_ms=data.get("max_latency_ms"),
        require_source_metadata=bool(data.get("require_source_metadata", False)),
    )


def _intent_from_value(value: Any) -> ContextIntent:
    if isinstance(value, ContextIntent):
        return value
    try:
        return ContextIntent(str(value).lower())
    except ValueError:
        return ContextIntent.GENERAL


def _layer_from_value(value: Any) -> ContextLayer:
    if isinstance(value, ContextLayer):
        return value
    try:
        return ContextLayer(str(value).lower())
    except ValueError:
        return ContextLayer.RECALL


def _case_from_dict(data: dict[str, Any]) -> ContextPackEvalCase:
    name = str(data["name"])
    fixture_data = data.get("fixture")
    if not isinstance(fixture_data, dict):
        msg = f"context-pack eval case {name!r} is missing fixture"
        raise ValueError(msg)
    return ContextPackEvalCase(
        name=name,
        goal=str(data["goal"]),
        intent=_intent_from_value(data.get("intent", ContextIntent.BUILD)),
        layer=_layer_from_value(data.get("layer", ContextLayer.RECALL)),
        domain=data.get("domain"),
        project=data.get("project"),
        agent_id=data.get("agent_id"),
        limit=int(data.get("limit", 24)),
        include_related=bool(data.get("include_related", True)),
        related_limit=int(data.get("related_limit", 3)),
        fixture=_fixture_from_dict(name, fixture_data),
    )


def get_sample_context_pack_cases() -> list[ContextPackEvalCase]:
    """Return a smoke-test case for the live context-pack endpoint."""

    return [
        ContextPackEvalCase(
            name="context-pack-smoke",
            goal="ship faster with Sibyl memory",
            domain="sibyl",
            limit=8,
            fixture=ContextPackFixture(
                name="context-pack-smoke",
                required_facets={ContextFacet.DECISIONS, ContextFacet.RECENT_MEMORY},
                max_items=8,
                max_markdown_chars=8000,
                max_estimated_tokens=1200,
                max_latency_ms=3000.0,
                require_source_metadata=True,
            ),
        )
    ]


def load_context_pack_cases(path: Path) -> list[ContextPackEvalCase]:
    """Load context-pack benchmark cases from JSON."""

    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases")
    if not isinstance(cases, list):
        msg = "context-pack eval file must contain a cases list"
        raise ValueError(msg)
    return [_case_from_dict(item) for item in cases]


def context_pack_from_dict(data: dict[str, Any]) -> ContextPack:
    """Parse a JSON context-pack response into core dataclasses."""

    sections: list[ContextSection] = []
    for section_data in data.get("sections", []):
        section_facet = ContextFacet(section_data["facet"])
        items: list[ContextItem] = []
        for item_data in section_data.get("items", []):
            quality_data = item_data.get("quality") or {}
            items.append(
                ContextItem(
                    id=str(item_data["id"]),
                    type=str(item_data.get("type") or ""),
                    name=str(item_data.get("name") or ""),
                    content=str(item_data.get("content") or ""),
                    score=float(item_data.get("score") or 0.0),
                    facet=ContextFacet(item_data.get("facet") or section_facet),
                    reason=str(item_data.get("reason") or ""),
                    source=item_data.get("source"),
                    quality=ContextItemQualityMetadata(
                        origin=quality_data.get("origin"),
                        source=quality_data.get("source"),
                        url=quality_data.get("url"),
                        created_at=quality_data.get("created_at"),
                        updated_at=quality_data.get("updated_at"),
                        valid_at=quality_data.get("valid_at"),
                        project_id=quality_data.get("project_id"),
                    ),
                    metadata=dict(item_data.get("metadata") or {}),
                )
            )
        sections.append(
            ContextSection(
                facet=section_facet,
                title=str(section_data.get("title") or section_facet.value),
                items=items,
            )
        )

    return ContextPack(
        goal=str(data["goal"]),
        intent=_intent_from_value(data.get("intent", ContextIntent.GENERAL)),
        layer=_layer_from_value(data.get("layer", ContextLayer.RECALL)),
        query=str(data.get("query") or data["goal"]),
        domain=data.get("domain"),
        project=data.get("project"),
        sections=sections,
        total_items=int(data.get("total_items", sum(len(section.items) for section in sections))),
        usage_hint=str(data.get("usage_hint") or ContextPack.usage_hint),
    )


__all__ = [
    "FROZEN_CONTEXT_PACK_SUITE_NAMES",
    "ContextPackCaseResult",
    "ContextPackEvalCase",
    "ContextPackEvalReport",
    "ContextPackEvalResult",
    "ContextPackFixture",
    "context_pack_from_dict",
    "evaluate_context_pack",
    "get_sample_context_pack_cases",
    "load_context_pack_cases",
]
