"""Immutable execution dependencies, distinct from original evidence sources."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sibyl_core.services.validation_result_codec import decode_validation_result
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.procedure_review import review_digest


class ExecutionDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def dependency_reference(row: dict[str, Any]) -> ExecutionDependency:
    """Bind a resolved result without copying its body into subsequent requests."""
    encoded = row.get("result_json")
    if not isinstance(encoded, str):
        raise ValueError("Execution dependency has no stored result")
    return ExecutionDependency(
        execution_id=row["uuid"], result_sha256=hashlib.sha256(encoded.encode()).hexdigest()
    )


def direct_dependencies(request: dict[str, Any]) -> tuple[ExecutionDependency, ...]:
    values = request.get("execution_dependencies", [])
    if not isinstance(values, list):
        raise ValueError("Execution dependency inventory must be a list")
    dependencies = [ExecutionDependency.model_validate(value) for value in values]
    if len({item.execution_id for item in dependencies}) != len(dependencies):
        raise ValueError("Duplicate execution dependency")
    history = request.get("progress_history")
    if history is not None:
        from sibyl_core.services.validation_progress_history import ProgressHistoryBinding

        prior = ProgressHistoryBinding.model_validate(history)
        reference = ExecutionDependency(
            execution_id=prior.execution_id, result_sha256=prior.result_sha256
        )
        for item in dependencies:
            if item.execution_id == reference.execution_id and item != reference:
                raise ValueError("Conflicting execution dependency")
        if reference not in dependencies:
            dependencies.append(reference)
    return tuple(dependencies)


def validate_dependency(
    row: dict[str, Any], reference: ExecutionDependency, org: str, principal: str
) -> dict[str, Any]:
    if (
        row.get("uuid") != reference.execution_id
        or row.get("request_sha256") != reference.execution_id
        or row.get("organization_id") != org
        or row.get("principal_id") != principal
        or row.get("state") != "returned"
        or row.get("purged") is not False
        or not isinstance(row.get("request_json"), str)
        or not isinstance(row.get("result_json"), str)
    ):
        raise ValueError("Execution dependency is unavailable")
    request = json.loads(row["request_json"])
    if (
        not isinstance(request, dict)
        or canonical(request) != row["request_json"]
        or review_digest(request) != reference.execution_id
        or request.get("org") != org
        or request.get("principal") != principal
        or request.get("parent") != row.get("parent_id")
        or request.get("policy") != row.get("policy_json")
        or hashlib.sha256(row["result_json"].encode()).hexdigest() != reference.result_sha256
    ):
        raise ValueError("Execution dependency identity differs")
    decode_validation_result(json.loads(row["result_json"]))
    return request


def dependency_closure(
    request: dict[str, Any],
    records: dict[str, dict[str, Any]],
    *,
    execution_id: str,
    org: str,
    principal: str,
    validated: dict[str, list[str]] | None = None,
    legacy: frozenset[str] = frozenset(),
) -> list[str]:
    """Validate the complete DAG independently of archive or query row ordering."""
    active = {execution_id}
    visited: dict[str, list[str]] = validated if validated is not None else {}
    initial = direct_dependencies(request)
    pending = [(reference, False) for reference in reversed(initial)]
    while pending:
        reference, expanded = pending.pop()
        identity = reference.execution_id
        row = records.get(identity)
        if row is None:
            raise ValueError("Execution dependency disappeared")
        prior = validate_dependency(row, reference, org, principal)
        children = direct_dependencies(prior)
        if expanded:
            closure = {child.execution_id for child in children}
            for child in children:
                closure.update(visited[child.execution_id])
            expected = sorted(closure)
            if identity in legacy:
                row["dependency_ids"] = expected
            if row.get("dependency_ids", []) != expected:
                raise ValueError("Execution dependency closure differs")
            visited[identity] = expected
            active.remove(identity)
        elif identity in active:
            raise ValueError("Execution dependency cycle")
        elif identity not in visited:
            active.add(identity)
            pending.append((reference, True))
            pending.extend((child, False) for child in reversed(children))
    closure = {reference.execution_id for reference in initial}
    for reference in initial:
        closure.update(visited[reference.execution_id])
    return sorted(closure)


async def resolve_dependencies(
    request: dict[str, Any],
    *,
    execution_id: str,
    org: str,
    principal: str,
    load: Callable[[str], Awaitable[dict[str, Any] | None]],
) -> tuple[list[str], str]:
    records: dict[str, dict[str, Any]] = {}
    pending = list(direct_dependencies(request))
    while pending:
        reference = pending.pop()
        if reference.execution_id in records:
            continue
        row = await load(reference.execution_id)
        if row is None:
            raise ValueError("Execution dependency disappeared")
        prior = validate_dependency(row, reference, org, principal)
        records[reference.execution_id] = row
        pending.extend(direct_dependencies(prior))
    closure = dependency_closure(
        request, records, execution_id=execution_id, org=org, principal=principal
    )
    guards = []
    for identity in closure:
        row = records[identity]
        guards.append(f"""
            LET $dependency = (SELECT * FROM memory_validation_executions
                WHERE uuid={canonical(identity)} AND organization_id={canonical(org)}
                    AND principal_id={canonical(principal)} LIMIT 1)[0];
            IF $dependency=NONE OR $dependency.state!='returned' OR $dependency.purged!=false
                OR $dependency.request_sha256!={canonical(identity)}
                OR crypto::sha256($dependency.request_json)!={canonical(identity)}
                OR crypto::sha256($dependency.result_json)!={canonical(dependency_reference(row).result_sha256)}
                OR $dependency.parent_id!={canonical(row["parent_id"])}
                OR $dependency.policy_json!={canonical(row["policy_json"])}
                OR ($dependency.dependency_ids ?? [])!={canonical(row.get("dependency_ids", []))} {{
                THROW 'Execution dependency changed';
            }};
            UPDATE memory_validation_executions
                SET promotion_write_witness=(promotion_write_witness ?? 0)+1
                WHERE uuid={canonical(identity)} AND organization_id={canonical(org)}
                    AND principal_id={canonical(principal)};
        """)
    return closure, "".join(guards)


def normalize_legacy_dependencies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive pre-inventory progress history without changing its request bytes."""
    records = {row["uuid"]: dict(row) for row in rows}
    if len(records) != len(rows):
        raise ValueError("Duplicate execution dependency inventory")
    legacy = frozenset(
        identity
        for identity, row in records.items()
        if "dependency_ids" not in row
        and "execution_dependencies" not in json.loads(row["request_json"])
    )
    validated: dict[str, list[str]] = {}
    for identity, row in records.items():
        if row["purged"]:
            continue
        closure = dependency_closure(
            json.loads(row["request_json"]),
            records,
            execution_id=identity,
            org=row["organization_id"],
            principal=row["principal_id"],
            validated=validated,
            legacy=legacy,
        )
        if identity in legacy:
            row["dependency_ids"] = closure
        if row.get("dependency_ids") != closure:
            raise ValueError("Execution dependency inventory differs")
    return list(records.values())


def archive_dependency_guard(request: dict[str, Any]) -> str:
    clauses = []
    for reference in direct_dependencies(request):
        identity = canonical(reference.execution_id)
        clauses.append(f"""
            LET $archive_prior=(SELECT * FROM memory_validation_executions
                WHERE uuid={identity} AND organization_id=$record.organization_id
                    AND principal_id=$record.principal_id LIMIT 1)[0];
            IF $archive_prior=NONE OR $archive_prior.state!='returned' OR $archive_prior.purged!=false
                OR crypto::sha256($archive_prior.request_json)!={identity}
                OR crypto::sha256($archive_prior.result_json)!={canonical(reference.result_sha256)} {{
                THROW 'Validation archive dependency was revoked';
            }};
            UPDATE $archive_prior.id SET promotion_write_witness=(promotion_write_witness ?? 0)+1;
        """)
    return "".join(clauses)
