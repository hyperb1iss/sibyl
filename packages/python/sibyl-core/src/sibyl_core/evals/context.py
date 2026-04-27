"""Evaluation helpers for Sibyl context packs."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ContextPackFixture:
    """Expected behavior for a context-pack dogfood fixture."""

    name: str
    required_item_ids: set[str] = field(default_factory=set)
    forbidden_item_ids: set[str] = field(default_factory=set)
    required_facets: set[ContextFacet] = field(default_factory=set)
    required_layer: ContextLayer | None = None
    required_terms: set[str] = field(default_factory=set)
    required_item_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_items: int | None = None
    max_markdown_chars: int | None = None
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
        latency_ms = (
            sum(case.latency_ms for case in self.cases) / case_count if case_count else 0.0
        )
        return {
            "timestamp": self.timestamp,
            "label": self.label,
            "search_type": "context-pack",
            "metadata": dict(self.metadata),
            "metrics": {
                "cases": case_count,
                "passed": passed_cases,
                "failed": case_count - passed_cases,
                "pass_rate": passed_cases / case_count if case_count else 0.0,
                "latency_ms": latency_ms,
            },
            "per_case": [
                {
                    "name": case.case.name,
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
        metrics = self.to_dict()["metrics"]
        print("\nSibyl context-pack evaluation summary")
        print(f"  cases: {metrics['cases']}")
        print(f"  pass_rate: {metrics['pass_rate']:.3f}")
        print(f"  failed: {metrics['failed']}")
        print(f"  latency_ms: {metrics['latency_ms']:.1f}")


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

    if fixture.required_layer is not None and pack.layer != fixture.required_layer:
        failures.append(
            f"wrong context layer: expected {fixture.required_layer.value} got {pack.layer.value}"
        )

    if fixture.max_items is not None and pack.total_items > fixture.max_items:
        failures.append(f"too many items: {pack.total_items} > {fixture.max_items}")

    markdown = context_pack_to_markdown(pack, max_items=max(pack.total_items, 1))
    if fixture.max_markdown_chars is not None and len(markdown) > fixture.max_markdown_chars:
        failures.append(
            f"markdown too large: {len(markdown)} chars > {fixture.max_markdown_chars}"
        )

    text = _searchable_text(pack)
    missing_terms = sorted(
        term for term in fixture.required_terms if term.lower() not in text
    )
    if missing_terms:
        failures.append(f"missing required terms: {', '.join(missing_terms)}")

    if fixture.require_source_metadata:
        unsourced = sorted(item.id for item in pack.items if not _has_source_metadata(item))
        if unsourced:
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
                f"item {item_id} metadata {key} expected {expected_value!r} "
                f"got {actual_value!r}"
            )

    metrics = {
        "items": pack.total_items,
        "facets": sorted(facet.value for facet in facets),
        "markdown_chars": len(markdown),
        "required_item_coverage": (
            1.0
            if not fixture.required_item_ids
            else (len(fixture.required_item_ids) - len(missing_ids))
            / len(fixture.required_item_ids)
        ),
        "metadata_requirement_coverage": (
            1.0 if not metadata_checks else metadata_matches / metadata_checks
        ),
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


def _fixture_from_dict(name: str, data: dict[str, Any]) -> ContextPackFixture:
    return ContextPackFixture(
        name=str(data.get("name") or name),
        required_item_ids=_string_set(data.get("required_item_ids")),
        forbidden_item_ids=_string_set(data.get("forbidden_item_ids")),
        required_facets=_facet_set(data.get("required_facets")),
        required_layer=_layer_from_value(data["required_layer"])
        if data.get("required_layer")
        else None,
        required_terms=_string_set(data.get("required_terms")),
        required_item_metadata=_metadata_requirements(data.get("required_item_metadata")),
        max_items=data.get("max_items"),
        max_markdown_chars=data.get("max_markdown_chars"),
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
                max_items=8,
                max_markdown_chars=8000,
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
