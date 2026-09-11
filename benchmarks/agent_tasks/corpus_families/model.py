"""Authored development fixtures, never recorded agent experiences."""

from dataclasses import dataclass, field
from textwrap import dedent
from typing import Any, Literal


@dataclass(frozen=True)
class Family:
    id: str
    split: Literal["learning", "development"]
    lineage: str
    mechanism_cluster: str = field(kw_only=True)
    contract: str
    workspace: dict[str, str]
    reference: dict[str, str]
    partial: dict[str, str]
    public_cases: list[dict[str, Any]]
    private_cases: list[dict[str, Any]]


def source(text: str) -> str:
    return dedent(text).lstrip()


def case(name: str, inputs: dict[str, Any], expected: Any) -> dict[str, Any]:
    return {"id": name, "input": inputs, "expected": expected}
