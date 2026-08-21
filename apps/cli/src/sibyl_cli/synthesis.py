"""Source-grounded synthesis CLI commands."""

from __future__ import annotations

from typing import Any, cast

import typer

from sibyl_cli import command_support
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import print_json, print_json_result, run_async
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_cli.memory_views import (
    print_synthesis_artifact,
    print_synthesis_plan,
    print_synthesis_remember,
    print_synthesis_verification,
)

app = typer.Typer(help="Source-grounded synthesis commands")


def _parse_section_specs(value: str | None) -> list[dict[str, object]]:
    sections: list[dict[str, object]] = []
    for spec in (value or "").split("|"):
        title, _, rest = spec.strip().partition("::")
        if not title:
            continue
        prompt, _, required_source_ids = rest.partition("::")
        section: dict[str, object] = {"title": title.strip()}
        if prompt.strip():
            section["prompt"] = prompt.strip()
        if required_source_ids.strip():
            section["required_source_ids"] = command_support.parse_csv_ids(required_source_ids)
        sections.append(section)
    return sections


def _synthesis_options(
    *,
    goal: str,
    output_type: str,
    audience: str | None,
    depth: str,
    seed_query: str | None,
    project: str | None,
    all_projects: bool,
    domain: str | None,
    entity_ids: str | None,
    decision_ids: str | None,
    task_ids: str | None,
    artifact_ids: str | None,
    sections: str | None,
    constraints: str | None,
    max_sections: int,
    include_neighborhoods: bool,
) -> dict[str, Any]:
    return {
        "goal": goal,
        "output_type": output_type,
        "audience": audience,
        "depth": depth,
        "seed_query": seed_query,
        "project": project or (None if all_projects else resolve_project_from_cwd()),
        "domain": domain,
        "entity_ids": command_support.parse_csv_ids(entity_ids),
        "decision_ids": command_support.parse_csv_ids(decision_ids),
        "task_ids": command_support.parse_csv_ids(task_ids),
        "artifact_ids": command_support.parse_csv_ids(artifact_ids),
        "required_sections": _parse_section_specs(sections),
        "constraints": command_support.parse_csv_ids(constraints),
        "max_sections": max_sections,
        "include_neighborhoods": include_neighborhoods,
    }


@app.command("plan")
def synthesis_plan_command(
    goal: str = typer.Argument(..., help="Synthesis goal"),
    output_type: str = typer.Option("documentation", "--type", help="Output type"),
    audience: str | None = typer.Option(None, "--audience", help="Intended audience"),
    depth: str = typer.Option("standard", "--depth", help="brief, standard, or deep"),
    seed_query: str | None = typer.Option(None, "--seed", help="Search seed query"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(False, "--all-projects", help="Skip cwd project scope"),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    entity_ids: str | None = typer.Option(None, "--entity", help="Comma-separated entity IDs"),
    decision_ids: str | None = typer.Option(
        None, "--decision", help="Comma-separated decision IDs"
    ),
    task_ids: str | None = typer.Option(None, "--task", help="Comma-separated task IDs"),
    artifact_ids: str | None = typer.Option(
        None, "--artifact", help="Comma-separated artifact IDs"
    ),
    sections: str | None = typer.Option(
        None,
        "--section",
        help="Pipe-separated Title::Prompt::source-id specs",
    ),
    constraints: str | None = typer.Option(
        None, "--constraint", help="Comma-separated constraints"
    ),
    max_sections: int = typer.Option(6, "--max-sections", min=1, max=12),
    include_neighborhoods: bool = typer.Option(
        True,
        "--neighborhoods/--no-neighborhoods",
        help="Include one-hop graph neighborhoods",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output full JSON"),
) -> None:
    """Plan source-grounded synthesis from authorized memory."""
    options = _synthesis_options(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        all_projects=all_projects,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        sections=sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )

    @run_async
    async def run_synthesis_plan() -> None:
        try:
            async with get_client() as client:
                data = await client.synthesis_plan(**options)
            if json_output:
                print_json(data)
                return
            print_synthesis_plan(cast("dict[str, object]", data))
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_synthesis_plan()


@app.command("draft")
def synthesis_draft_command(
    goal: str = typer.Argument(..., help="Synthesis goal"),
    output_type: str = typer.Option("documentation", "--type", help="Output type"),
    output_format: str = typer.Option("markdown", "--format", help="markdown or json"),
    audience: str | None = typer.Option(None, "--audience", help="Intended audience"),
    depth: str = typer.Option("standard", "--depth", help="brief, standard, or deep"),
    seed_query: str | None = typer.Option(None, "--seed", help="Search seed query"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(False, "--all-projects", help="Skip cwd project scope"),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    entity_ids: str | None = typer.Option(None, "--entity", help="Comma-separated entity IDs"),
    decision_ids: str | None = typer.Option(
        None, "--decision", help="Comma-separated decision IDs"
    ),
    task_ids: str | None = typer.Option(None, "--task", help="Comma-separated task IDs"),
    artifact_ids: str | None = typer.Option(
        None, "--artifact", help="Comma-separated artifact IDs"
    ),
    sections: str | None = typer.Option(
        None,
        "--section",
        help="Pipe-separated Title::Prompt::source-id specs",
    ),
    constraints: str | None = typer.Option(
        None, "--constraint", help="Comma-separated constraints"
    ),
    max_sections: int = typer.Option(6, "--max-sections", min=1, max=12),
    include_neighborhoods: bool = typer.Option(
        True,
        "--neighborhoods/--no-neighborhoods",
        help="Include one-hop graph neighborhoods",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output full JSON"),
) -> None:
    """Draft a verified synthesis artifact."""
    options = _synthesis_options(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        all_projects=all_projects,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        sections=sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )

    @run_async
    async def run_synthesis_draft() -> None:
        try:
            async with get_client() as client:
                data = await client.synthesis_draft(
                    **options,
                    output_format=output_format,
                )
            if json_output:
                print_json(data)
                return
            print_synthesis_artifact(
                cast("dict[str, object]", data),
                output_format=output_format,
            )
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_synthesis_draft()


@app.command("verify")
def synthesis_verify_command(
    goal: str = typer.Argument(..., help="Synthesis goal"),
    output_type: str = typer.Option("documentation", "--type", help="Output type"),
    audience: str | None = typer.Option(None, "--audience", help="Intended audience"),
    depth: str = typer.Option("standard", "--depth", help="brief, standard, or deep"),
    seed_query: str | None = typer.Option(None, "--seed", help="Search seed query"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(False, "--all-projects", help="Skip cwd project scope"),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    entity_ids: str | None = typer.Option(None, "--entity", help="Comma-separated entity IDs"),
    decision_ids: str | None = typer.Option(
        None, "--decision", help="Comma-separated decision IDs"
    ),
    task_ids: str | None = typer.Option(None, "--task", help="Comma-separated task IDs"),
    artifact_ids: str | None = typer.Option(
        None, "--artifact", help="Comma-separated artifact IDs"
    ),
    sections: str | None = typer.Option(
        None,
        "--section",
        help="Pipe-separated Title::Prompt::source-id specs",
    ),
    constraints: str | None = typer.Option(
        None, "--constraint", help="Comma-separated constraints"
    ),
    max_sections: int = typer.Option(6, "--max-sections", min=1, max=12),
    include_neighborhoods: bool = typer.Option(
        True,
        "--neighborhoods/--no-neighborhoods",
        help="Include one-hop graph neighborhoods",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output full JSON"),
) -> None:
    """Verify synthesis citation, freshness, redaction, and gap coverage."""
    options = _synthesis_options(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        all_projects=all_projects,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        sections=sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )

    @run_async
    async def run_synthesis_verify() -> None:
        try:
            async with get_client() as client:
                data = await client.synthesis_draft(**options, output_format="json")
            verification = cast("dict[str, object]", data.get("verification") or {})
            passed = str(verification.get("status") or "unknown") == "pass"

            if json_output:
                print_json_result(data, succeeded=passed)
                return
            print_synthesis_verification(cast("dict[str, object]", data))
            if not passed:
                raise typer.Exit(1)
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_synthesis_verify()


@app.command("remember")
def synthesis_remember_command(
    goal: str = typer.Argument(..., help="Synthesis goal"),
    output_type: str = typer.Option("documentation", "--type", help="Output type"),
    output_format: str = typer.Option("markdown", "--format", help="markdown or json"),
    audience: str | None = typer.Option(None, "--audience", help="Intended audience"),
    depth: str = typer.Option("standard", "--depth", help="brief, standard, or deep"),
    seed_query: str | None = typer.Option(None, "--seed", help="Search seed query"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(False, "--all-projects", help="Skip cwd project scope"),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    entity_ids: str | None = typer.Option(None, "--entity", help="Comma-separated entity IDs"),
    decision_ids: str | None = typer.Option(
        None, "--decision", help="Comma-separated decision IDs"
    ),
    task_ids: str | None = typer.Option(None, "--task", help="Comma-separated task IDs"),
    artifact_ids: str | None = typer.Option(
        None, "--artifact", help="Comma-separated artifact IDs"
    ),
    sections: str | None = typer.Option(
        None,
        "--section",
        help="Pipe-separated Title::Prompt::source-id specs",
    ),
    constraints: str | None = typer.Option(
        None, "--constraint", help="Comma-separated constraints"
    ),
    max_sections: int = typer.Option(6, "--max-sections", min=1, max=12),
    include_neighborhoods: bool = typer.Option(
        True,
        "--neighborhoods/--no-neighborhoods",
        help="Include one-hop graph neighborhoods",
    ),
    memory_scope: str = typer.Option("private", "--scope", help="Artifact memory scope"),
    scope_key: str | None = typer.Option(None, "--scope-key", help="Artifact scope key"),
    tags: str | None = typer.Option(
        None,
        "--tags",
        help="Comma-separated browse-only artifact metadata tags",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output full JSON"),
) -> None:
    """Draft, verify, and remember a synthesis artifact."""
    options = _synthesis_options(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        all_projects=all_projects,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        sections=sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )

    @run_async
    async def run_synthesis_remember() -> None:
        try:
            async with get_client() as client:
                data = await client.synthesis_draft(
                    **options,
                    output_format=output_format,
                    remember=True,
                    memory_scope=memory_scope,
                    scope_key=scope_key,
                    tags=command_support.parse_csv_ids(tags),
                )
            artifact = cast("dict[str, object]", data.get("artifact") or {})
            remembered = bool(artifact.get("remembered_memory_id"))

            if json_output:
                print_json_result(data, succeeded=remembered)
                return
            print_synthesis_remember(cast("dict[str, object]", data))
            if not remembered:
                raise typer.Exit(1)
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_synthesis_remember()
