"""Agent communication and orchestration CLI commands.

Commands for inter-agent messaging during distributed execution:
- progress: Report progress to orchestrator
- blocker: Signal a blocker
- query: Ask another agent a question
- delegate: Delegate work to another agent
- review: Request code review
- inbox: Check pending messages
- respond: Respond to a message
- conversation: View conversation history with another agent

Orchestration commands for multi-agent task execution:
- orchestrate init: Get or create a MetaOrchestrator
- orchestrate queue: Add tasks to the processing queue
- orchestrate start: Begin processing tasks
- orchestrate pause/resume: Control orchestration
- orchestrate status: Check orchestration status
- orchestrate strategy: Set execution strategy
- orchestrate budget: Set cost limits
- dispatch: High-level command to queue and run tasks

These commands enable Claude Code agents to communicate and coordinate through Sibyl.
"""

from typing import Annotated

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_table,
    handle_client_error,
    info,
    print_json,
    run_async,
    success,
    warn,
)
from sibyl_cli.config_store import resolve_project_from_cwd

app = typer.Typer(
    name="agent",
    help="Inter-agent communication for distributed execution",
    no_args_is_help=True,
)


def _validate_agent_id(agent_id: str) -> str:
    """Validate agent ID format."""
    if not agent_id.startswith("agent_"):
        raise SibylClientError(
            f"Invalid agent ID format: {agent_id}. Expected format: agent_<hex>",
            status_code=400,
            detail=f"Invalid agent ID: {agent_id}",
        )
    return agent_id


def _format_message_type(msg_type: str) -> str:
    """Format message type for display."""
    type_colors = {
        "progress": "green",
        "query": "cyan",
        "response": "blue",
        "review_request": "yellow",
        "review_result": "yellow",
        "blocker": "red",
        "delegation": "magenta",
        "system": "dim",
    }
    color = type_colors.get(msg_type, "white")
    return f"[{color}]{msg_type}[/{color}]"


@app.command()
def progress(
    agent_id: Annotated[str, typer.Argument(help="Your agent ID")],
    message: Annotated[str, typer.Argument(help="Progress message")],
    percent: Annotated[
        int | None, typer.Option("--percent", "-p", help="Progress percentage (0-100)")
    ] = None,
    to: Annotated[
        str | None, typer.Option("--to", "-t", help="Target agent (default: orchestrator)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Report progress to orchestrator or another agent.

    Example:
        sibyl agent progress agent_abc123 "Completed code review" --percent 75
    """

    @run_async
    async def _run() -> None:
        client = get_client()
        context = {}
        if percent is not None:
            context["progress_percent"] = percent

        result = await client.send_agent_message(
            from_agent_id=agent_id,
            message_type="progress",
            subject=f"Progress: {percent}%" if percent else "Progress update",
            content=message,
            to_agent_id=to,
            context=context if context else None,
        )

        if json_out:
            print_json(result)
        else:
            success(f"Progress reported: {message[:50]}{'...' if len(message) > 50 else ''}")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@app.command()
def blocker(
    agent_id: Annotated[str, typer.Argument(help="Your agent ID")],
    subject: Annotated[str, typer.Argument(help="Short blocker description")],
    details: Annotated[str, typer.Argument(help="Full details about the blocker")],
    resource: Annotated[
        str | None, typer.Option("--resource", "-r", help="Blocking resource (file, API, etc.)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Signal a blocker to the orchestrator.

    Example:
        sibyl agent blocker agent_abc123 "API rate limit" "Hit GitHub API rate limit, need to wait"
    """

    @run_async
    async def _run() -> None:
        client = get_client()
        context = {}
        if resource:
            context["blocking_resource"] = resource

        result = await client.send_agent_message(
            from_agent_id=agent_id,
            message_type="blocker",
            subject=subject,
            content=details,
            priority=7,  # Blockers are high priority
            context=context if context else None,
        )

        if json_out:
            print_json(result)
        else:
            console.print(f"[red]Blocker signaled:[/red] {subject}")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@app.command()
def query(
    agent_id: Annotated[str, typer.Argument(help="Your agent ID")],
    to_agent: Annotated[str, typer.Argument(help="Target agent ID to query")],
    subject: Annotated[str, typer.Argument(help="Query subject")],
    question: Annotated[str, typer.Argument(help="Your question")],
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Ask another agent a question (requires response).

    Example:
        sibyl agent query agent_abc123 agent_def456 "Auth approach" "How should I handle token refresh?"
    """

    @run_async
    async def _run() -> None:
        client = get_client()
        result = await client.send_agent_message(
            from_agent_id=agent_id,
            message_type="query",
            subject=subject,
            content=question,
            to_agent_id=to_agent,
            requires_response=True,
            priority=5,
        )

        if json_out:
            print_json(result)
        else:
            msg_id = result.get("id", "unknown")
            console.print(f"[{NEON_CYAN}]Query sent to {to_agent}[/{NEON_CYAN}]")
            console.print(f"Message ID: [{CORAL}]{msg_id}[/{CORAL}]")
            info("Check inbox later for response")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@app.command()
def delegate(
    agent_id: Annotated[str, typer.Argument(help="Your agent ID")],
    to_agent: Annotated[str, typer.Argument(help="Agent to delegate to")],
    subject: Annotated[str, typer.Argument(help="Delegation title")],
    work: Annotated[str, typer.Argument(help="Work description")],
    task_id: Annotated[str | None, typer.Option("--task", "-t", help="Associated task ID")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Delegate work to another agent.

    Example:
        sibyl agent delegate agent_abc123 agent_def456 "Write tests" "Add unit tests for auth module"
    """

    @run_async
    async def _run() -> None:
        client = get_client()
        context = {}
        if task_id:
            context["task_id"] = task_id

        result = await client.send_agent_message(
            from_agent_id=agent_id,
            message_type="delegation",
            subject=subject,
            content=work,
            to_agent_id=to_agent,
            priority=5,
            context=context if context else None,
        )

        if json_out:
            print_json(result)
        else:
            console.print(f"[{ELECTRIC_PURPLE}]Work delegated to {to_agent}[/{ELECTRIC_PURPLE}]")
            console.print(f"Subject: {subject}")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@app.command()
def review(
    agent_id: Annotated[str, typer.Argument(help="Your agent ID")],
    to_agent: Annotated[str, typer.Argument(help="Agent to request review from")],
    subject: Annotated[str, typer.Argument(help="Review request title")],
    description: Annotated[str, typer.Argument(help="What to review and why")],
    files: Annotated[
        str | None, typer.Option("--files", "-f", help="Comma-separated list of files")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Request code review from another agent.

    Example:
        sibyl agent review agent_abc123 agent_def456 "Auth changes" "Please review the OAuth implementation" --files "auth.py,login.py"
    """

    @run_async
    async def _run() -> None:
        client = get_client()
        context = {}
        if files:
            context["files"] = [f.strip() for f in files.split(",")]

        result = await client.send_agent_message(
            from_agent_id=agent_id,
            message_type="review_request",
            subject=subject,
            content=description,
            to_agent_id=to_agent,
            requires_response=True,
            priority=5,
            context=context if context else None,
        )

        if json_out:
            print_json(result)
        else:
            msg_id = result.get("id", "unknown")
            console.print(f"[yellow]Review requested from {to_agent}[/yellow]")
            console.print(f"Message ID: [{CORAL}]{msg_id}[/{CORAL}]")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@app.command()
def inbox(
    agent_id: Annotated[str, typer.Argument(help="Your agent ID")],
    limit: Annotated[int, typer.Option("--limit", "-l", help="Max messages to show")] = 20,
    include_read: Annotated[
        bool, typer.Option("--all", "-a", help="Include read messages")
    ] = False,
    digest: Annotated[
        bool, typer.Option("--digest", "-d", help="Output as formatted digest")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Check pending messages for an agent.

    Example:
        sibyl agent inbox agent_abc123
        sibyl agent inbox agent_abc123 --digest  # For Claude Code injection
    """

    @run_async
    async def _run() -> None:
        client = get_client()

        if digest:
            result = await client.get_message_digest(agent_id, limit=limit)
            if json_out:
                print_json(result)
            else:
                digest_text = result.get("digest", "")
                if digest_text:
                    console.print(digest_text)
                else:
                    info("No pending messages")
            return

        result = await client.get_pending_messages(agent_id, limit=limit, include_read=include_read)

        if json_out:
            print_json(result)
            return

        messages = result.get("messages", [])
        if not messages:
            info("No pending messages")
            return

        table = create_table(f"Messages for {agent_id}", "Subject", "From", "Type", "Priority")
        for msg in messages:
            table.add_row(
                msg.get("subject", "")[:40],
                msg.get("from_agent_id", "")[:15],
                _format_message_type(msg.get("message_type", "")),
                str(msg.get("priority", 0)),
            )
        console.print(table)
        console.print(f"\nTotal: {result.get('count', 0)} messages")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@app.command()
def respond(
    agent_id: Annotated[str, typer.Argument(help="Your agent ID")],
    message_id: Annotated[str, typer.Argument(help="Message ID to respond to")],
    response: Annotated[str, typer.Argument(help="Your response")],
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Respond to a message.

    Example:
        sibyl agent respond agent_abc123 <message-uuid> "Use the retry pattern with exponential backoff"
    """

    @run_async
    async def _run() -> None:
        client = get_client()
        result = await client.respond_to_message(
            message_id=message_id,
            from_agent_id=agent_id,
            content=response,
        )

        if json_out:
            print_json(result)
        else:
            success(f"Response sent (ID: {result.get('id', 'unknown')[:12]}...)")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@app.command()
def conversation(
    agent_id: Annotated[str, typer.Argument(help="Your agent ID")],
    other_agent: Annotated[str, typer.Argument(help="Other agent ID")],
    limit: Annotated[int, typer.Option("--limit", "-l", help="Max messages to show")] = 50,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """View conversation history with another agent.

    Example:
        sibyl agent conversation agent_abc123 agent_def456
    """

    @run_async
    async def _run() -> None:
        client = get_client()
        result = await client.get_agent_conversation(agent_id, other_agent, limit=limit)

        if json_out:
            print_json(result)
            return

        messages = result.get("messages", [])
        if not messages:
            info(f"No conversation history with {other_agent}")
            return

        console.print(f"\n[bold]Conversation: {agent_id} <-> {other_agent}[/bold]\n")
        for msg in messages:
            sender = msg.get("from_agent_id", "")
            is_you = sender == agent_id
            color = NEON_CYAN if is_you else CORAL
            label = "You" if is_you else sender[:12]

            console.print(f"[{color}]{label}[/{color}] [{msg.get('message_type', '')}]")
            console.print(f"  {msg.get('subject', '')}")
            content = msg.get("content", "")
            if len(content) > 100:
                content = content[:100] + "..."
            console.print(f"  [dim]{content}[/dim]\n")

        console.print(f"Total: {result.get('count', 0)} messages")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


# =============================================================================
# Orchestrate Subcommands - Multi-Agent Task Execution
# =============================================================================

orchestrate_app = typer.Typer(
    name="orchestrate",
    help="Multi-agent task orchestration (MetaOrchestrator)",
    no_args_is_help=True,
)
app.add_typer(orchestrate_app, name="orchestrate")


@orchestrate_app.command("init")
def orchestrate_init(
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Project ID (auto-resolves from cwd)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Initialize or get the MetaOrchestrator for a project.

    Creates a new MetaOrchestrator if one doesn't exist, or returns the existing one.
    MetaOrchestrators are singletons per project.

    Example:
        sibyl agent orchestrate init
        sibyl agent orchestrate init --project proj_abc123
    """

    @run_async
    async def _run() -> None:
        project_id = project or resolve_project_from_cwd()
        if not project_id:
            console.print(
                f"[{CORAL}]Error:[/{CORAL}] No project specified and no project linked to cwd"
            )
            console.print(f"  Run [{NEON_CYAN}]sibyl project link <project_id>[/{NEON_CYAN}] first")
            raise typer.Exit(1)

        client = get_client()
        result = await client.get_or_create_orchestrator(project_id)

        if json_out:
            print_json(result)
        else:
            orch_id = result.get("id", "unknown")
            status = result.get("status", "unknown")
            strategy = result.get("strategy", "sequential")
            console.print(f"\n[{ELECTRIC_PURPLE}]MetaOrchestrator[/{ELECTRIC_PURPLE}]")
            console.print(f"  ID: [{CORAL}]{orch_id}[/{CORAL}]")
            console.print(f"  Project: [{NEON_CYAN}]{project_id}[/{NEON_CYAN}]")
            console.print(f"  Status: {status}")
            console.print(f"  Strategy: {strategy}")
            console.print(f"  Queue: {result.get('queue_size', 0)} tasks")
            console.print(f"  Active: {result.get('active_count', 0)} agents")
            console.print()

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@orchestrate_app.command("queue")
def orchestrate_queue(
    task_ids: Annotated[list[str], typer.Argument(help="Task IDs to queue")],
    orchestrator: Annotated[
        str | None, typer.Option("--orchestrator", "-o", help="Orchestrator ID")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Project ID (to find orchestrator)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Add tasks to the orchestration queue.

    Tasks will be processed according to the configured strategy when started.

    Example:
        sibyl agent orchestrate queue task_abc123 task_def456
        sibyl agent orchestrate queue task_abc123 --orchestrator orch_xyz789
    """

    @run_async
    async def _run() -> None:
        client = get_client()

        # Get orchestrator ID
        orch_id = orchestrator
        if not orch_id:
            project_id = project or resolve_project_from_cwd()
            if not project_id:
                console.print(f"[{CORAL}]Error:[/{CORAL}] No orchestrator or project specified")
                raise typer.Exit(1)
            # Get or create orchestrator for the project
            orch = await client.get_or_create_orchestrator(project_id)
            orch_id = orch.get("id")
            assert orch_id is not None, "Orchestrator ID not returned"

        result = await client.queue_orchestrator_tasks(orch_id, task_ids)

        if json_out:
            print_json(result)
        else:
            queue_size = result.get("queue_size", 0)
            success(f"Queued {len(task_ids)} task(s)")
            console.print(f"  Total in queue: [{NEON_CYAN}]{queue_size}[/{NEON_CYAN}]")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@orchestrate_app.command("start")
def orchestrate_start(
    orchestrator: Annotated[
        str | None, typer.Option("--orchestrator", "-o", help="Orchestrator ID")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Project ID (to find orchestrator)")
    ] = None,
    gates: Annotated[
        str | None,
        typer.Option("--gates", "-g", help="Quality gates (comma-separated: lint,test,typecheck)"),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Start processing the task queue.

    Spawns TaskOrchestrators according to the configured strategy.
    Each TaskOrchestrator runs the implement → review → rework loop.

    Example:
        sibyl agent orchestrate start
        sibyl agent orchestrate start --gates lint,test
    """

    @run_async
    async def _run() -> None:
        client = get_client()

        # Get orchestrator ID
        orch_id = orchestrator
        if not orch_id:
            project_id = project or resolve_project_from_cwd()
            if not project_id:
                console.print(f"[{CORAL}]Error:[/{CORAL}] No orchestrator or project specified")
                raise typer.Exit(1)
            orch = await client.get_or_create_orchestrator(project_id)
            orch_id = orch.get("id")
            assert orch_id is not None, "Orchestrator ID not returned"

        gate_config = [g.strip() for g in gates.split(",")] if gates else None
        result = await client.start_orchestrator(orch_id, gate_config=gate_config)

        if json_out:
            print_json(result)
        else:
            console.print(
                f"\n[{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] {result.get('message', 'Started')}"
            )
            console.print(f"  Orchestrator: [{CORAL}]{orch_id}[/{CORAL}]")
            if gate_config:
                console.print(f"  Quality gates: {', '.join(gate_config)}")
            console.print()

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@orchestrate_app.command("pause")
def orchestrate_pause(
    orchestrator: Annotated[
        str | None, typer.Option("--orchestrator", "-o", help="Orchestrator ID")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Project ID (to find orchestrator)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Pause orchestration.

    Active TaskOrchestrators continue their work, but no new ones are spawned.

    Example:
        sibyl agent orchestrate pause
    """

    @run_async
    async def _run() -> None:
        client = get_client()

        orch_id = orchestrator
        if not orch_id:
            project_id = project or resolve_project_from_cwd()
            if not project_id:
                console.print(f"[{CORAL}]Error:[/{CORAL}] No orchestrator or project specified")
                raise typer.Exit(1)
            orch = await client.get_or_create_orchestrator(project_id)
            orch_id = orch.get("id")
            assert orch_id is not None, "Orchestrator ID not returned"

        result = await client.pause_orchestrator(orch_id)

        if json_out:
            print_json(result)
        else:
            console.print(f"[{ELECTRIC_YELLOW}]⏸[/{ELECTRIC_YELLOW}] Orchestration paused")
            console.print(f"  Orchestrator: [{CORAL}]{orch_id}[/{CORAL}]")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@orchestrate_app.command("resume")
def orchestrate_resume(
    orchestrator: Annotated[
        str | None, typer.Option("--orchestrator", "-o", help="Orchestrator ID")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Project ID (to find orchestrator)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Resume paused orchestration.

    Example:
        sibyl agent orchestrate resume
    """

    @run_async
    async def _run() -> None:
        client = get_client()

        orch_id = orchestrator
        if not orch_id:
            project_id = project or resolve_project_from_cwd()
            if not project_id:
                console.print(f"[{CORAL}]Error:[/{CORAL}] No orchestrator or project specified")
                raise typer.Exit(1)
            orch = await client.get_or_create_orchestrator(project_id)
            orch_id = orch.get("id")
            assert orch_id is not None, "Orchestrator ID not returned"

        result = await client.resume_orchestrator(orch_id)

        if json_out:
            print_json(result)
        else:
            console.print(f"[{SUCCESS_GREEN}]▶[/{SUCCESS_GREEN}] Orchestration resumed")
            console.print(f"  Orchestrator: [{CORAL}]{orch_id}[/{CORAL}]")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@orchestrate_app.command("status")
def orchestrate_status(
    orchestrator: Annotated[
        str | None, typer.Option("--orchestrator", "-o", help="Orchestrator ID")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Project ID (to find orchestrator)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Get detailed orchestration status.

    Shows queue size, active agents, budget, and metrics.

    Example:
        sibyl agent orchestrate status
    """

    @run_async
    async def _run() -> None:
        client = get_client()

        orch_id = orchestrator
        if not orch_id:
            project_id = project or resolve_project_from_cwd()
            if not project_id:
                console.print(f"[{CORAL}]Error:[/{CORAL}] No orchestrator or project specified")
                raise typer.Exit(1)
            orch = await client.get_or_create_orchestrator(project_id)
            orch_id = orch.get("id")
            assert orch_id is not None, "Orchestrator ID not returned"

        result = await client.get_orchestrator_status(orch_id)

        if json_out:
            print_json(result)
        else:
            status = result.get("status", "unknown")
            strategy = result.get("strategy", "sequential")

            # Status color
            status_colors = {
                "idle": "dim",
                "running": SUCCESS_GREEN,
                "paused": ELECTRIC_YELLOW,
                "completed": SUCCESS_GREEN,
                "failed": CORAL,
            }
            status_color = status_colors.get(status, "white")

            console.print(f"\n[{ELECTRIC_PURPLE}]MetaOrchestrator Status[/{ELECTRIC_PURPLE}]")
            console.print(f"  ID: [{CORAL}]{orch_id}[/{CORAL}]")
            console.print(f"  Status: [{status_color}]{status}[/{status_color}]")
            console.print(f"  Strategy: {strategy}")
            console.print()

            # Queue & Activity
            console.print(f"  [{NEON_CYAN}]Queue & Activity[/{NEON_CYAN}]")
            console.print(f"    Queued: {result.get('queue_size', 0)}")
            console.print(f"    Active: {result.get('active_count', 0)}")
            console.print(f"    Completed: {result.get('tasks_completed', 0)}")
            console.print(f"    Failed: {result.get('tasks_failed', 0)}")
            console.print(f"    Rework cycles: {result.get('total_rework_cycles', 0)}")
            console.print()

            # Budget
            budget_usd = result.get("budget_usd", 0)
            spent_usd = result.get("spent_usd", 0)
            remaining = result.get("budget_remaining", budget_usd - spent_usd)
            utilization = result.get("budget_utilization", 0)

            console.print(f"  [{NEON_CYAN}]Budget[/{NEON_CYAN}]")
            console.print(f"    Budget: ${budget_usd:.2f}")
            console.print(f"    Spent: ${spent_usd:.2f}")
            console.print(f"    Remaining: ${remaining:.2f}")
            console.print(f"    Utilization: {utilization:.1%}")
            console.print()

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@orchestrate_app.command("strategy")
def orchestrate_strategy(
    strategy: Annotated[str, typer.Argument(help="Strategy: sequential, parallel, priority")],
    max_concurrent: Annotated[
        int | None, typer.Option("--max", "-m", help="Max concurrent agents (for parallel)")
    ] = None,
    orchestrator: Annotated[
        str | None, typer.Option("--orchestrator", "-o", help="Orchestrator ID")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Project ID (to find orchestrator)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Set the orchestration strategy.

    Strategies:
      sequential - One task at a time (safe, predictable)
      parallel   - Multiple concurrent tasks (fast, uses more resources)
      priority   - One at a time, highest priority first

    Example:
        sibyl agent orchestrate strategy parallel --max 3
    """

    @run_async
    async def _run() -> None:
        client = get_client()

        orch_id = orchestrator
        if not orch_id:
            project_id = project or resolve_project_from_cwd()
            if not project_id:
                console.print(f"[{CORAL}]Error:[/{CORAL}] No orchestrator or project specified")
                raise typer.Exit(1)
            orch = await client.get_or_create_orchestrator(project_id)
            orch_id = orch.get("id")
            assert orch_id is not None, "Orchestrator ID not returned"

        result = await client.set_orchestrator_strategy(orch_id, strategy, max_concurrent)

        if json_out:
            print_json(result)
        else:
            success(f"Strategy set to: {strategy}")
            if max_concurrent:
                console.print(f"  Max concurrent: {max_concurrent}")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


@orchestrate_app.command("budget")
def orchestrate_budget(
    budget_usd: Annotated[float, typer.Argument(help="Budget limit in USD")],
    alert: Annotated[float, typer.Option("--alert", "-a", help="Alert threshold (0-1)")] = 0.8,
    orchestrator: Annotated[
        str | None, typer.Option("--orchestrator", "-o", help="Orchestrator ID")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Project ID (to find orchestrator)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Set budget limits for orchestration.

    Example:
        sibyl agent orchestrate budget 50.00
        sibyl agent orchestrate budget 100.00 --alert 0.9
    """

    @run_async
    async def _run() -> None:
        client = get_client()

        orch_id = orchestrator
        if not orch_id:
            project_id = project or resolve_project_from_cwd()
            if not project_id:
                console.print(f"[{CORAL}]Error:[/{CORAL}] No orchestrator or project specified")
                raise typer.Exit(1)
            orch = await client.get_or_create_orchestrator(project_id)
            orch_id = orch.get("id")
            assert orch_id is not None, "Orchestrator ID not returned"

        result = await client.set_orchestrator_budget(orch_id, budget_usd, alert)

        if json_out:
            print_json(result)
        else:
            success(f"Budget set to ${budget_usd:.2f}")
            console.print(f"  Alert at: {alert:.0%}")

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)


# =============================================================================
# High-Level Dispatch Command
# =============================================================================


@app.command()
def dispatch(
    task_ids: Annotated[
        list[str] | None, typer.Argument(help="Task IDs to dispatch (optional)")
    ] = None,
    status_filter: Annotated[
        str | None, typer.Option("--status", "-s", help="Filter tasks by status (e.g., todo,doing)")
    ] = None,
    priority_filter: Annotated[
        str | None, typer.Option("--priority", help="Filter tasks by priority")
    ] = None,
    strategy: Annotated[str, typer.Option("--strategy", help="Execution strategy")] = "parallel",
    max_concurrent: Annotated[int, typer.Option("--max", "-m", help="Max concurrent agents")] = 3,
    budget: Annotated[
        float | None, typer.Option("--budget", "-b", help="Budget limit in USD")
    ] = None,
    gates: Annotated[
        str | None, typer.Option("--gates", "-g", help="Quality gates (comma-separated)")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be dispatched without running")
    ] = False,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project ID")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Dispatch tasks to multi-agent orchestration.

    This is the high-level command that:
    1. Finds tasks matching the criteria (or uses provided IDs)
    2. Gets/creates the MetaOrchestrator
    3. Sets strategy and budget
    4. Queues the tasks
    5. Starts orchestration

    Examples:
        # Dispatch specific tasks
        sibyl agent dispatch task_abc123 task_def456

        # Dispatch all todo tasks with parallel strategy
        sibyl agent dispatch --status todo --strategy parallel --max 4

        # Dispatch with budget limit
        sibyl agent dispatch --status todo,doing --budget 25.00

        # Dry run to see what would be dispatched
        sibyl agent dispatch --status todo --dry-run
    """

    @run_async
    async def _run() -> None:
        client = get_client()
        project_id = project or resolve_project_from_cwd()

        if not project_id:
            console.print(
                f"[{CORAL}]Error:[/{CORAL}] No project specified and no project linked to cwd"
            )
            console.print(f"  Run [{NEON_CYAN}]sibyl project link <project_id>[/{NEON_CYAN}] first")
            raise typer.Exit(1)

        # Step 1: Get task IDs
        tasks_to_dispatch = list(task_ids) if task_ids else []

        if not tasks_to_dispatch and (status_filter or priority_filter):
            # Fetch tasks matching criteria
            result = await client.explore(
                mode="list",
                types=["task"],
                project=project_id,
                status=status_filter,
                priority=priority_filter,
                limit=100,
            )
            entities = result.get("entities", [])
            tasks_to_dispatch = [e.get("id") for e in entities if e.get("id")]

        if not tasks_to_dispatch:
            warn("No tasks to dispatch")
            console.print("  Provide task IDs or use --status/--priority filters")
            raise typer.Exit(1)

        # Dry run - just show what would happen
        if dry_run:
            console.print(f"\n[{ELECTRIC_PURPLE}]Dry Run - Would dispatch:[/{ELECTRIC_PURPLE}]")
            console.print(f"  Tasks: {len(tasks_to_dispatch)}")
            for tid in tasks_to_dispatch[:10]:
                console.print(f"    [{CORAL}]{tid}[/{CORAL}]")
            if len(tasks_to_dispatch) > 10:
                console.print(f"    ... and {len(tasks_to_dispatch) - 10} more")
            console.print(f"  Strategy: {strategy}")
            console.print(f"  Max concurrent: {max_concurrent}")
            if budget:
                console.print(f"  Budget: ${budget:.2f}")
            if gates:
                console.print(f"  Quality gates: {gates}")
            return

        # Initialize output dict for json mode
        output: dict = {"tasks": tasks_to_dispatch}

        # Step 2: Get/create orchestrator
        console.print(
            f"\n[{ELECTRIC_PURPLE}]Dispatching {len(tasks_to_dispatch)} task(s)[/{ELECTRIC_PURPLE}]"
        )
        orch = await client.get_or_create_orchestrator(project_id)
        orch_id = orch.get("id")
        assert orch_id is not None, "Orchestrator ID not returned"
        console.print(f"  Orchestrator: [{CORAL}]{orch_id}[/{CORAL}]")

        output["orchestrator_id"] = orch_id

        # Step 3: Set strategy
        await client.set_orchestrator_strategy(orch_id, strategy, max_concurrent)
        console.print(f"  Strategy: {strategy} (max {max_concurrent})")

        # Step 4: Set budget if specified
        if budget:
            await client.set_orchestrator_budget(orch_id, budget)
            console.print(f"  Budget: ${budget:.2f}")

        # Step 5: Queue tasks
        queue_result = await client.queue_orchestrator_tasks(orch_id, tasks_to_dispatch)
        queue_size = queue_result.get("queue_size", len(tasks_to_dispatch))
        console.print(f"  Queued: {queue_size} tasks")

        # Step 6: Start orchestration
        gate_config = [g.strip() for g in gates.split(",")] if gates else None
        start_result = await client.start_orchestrator(orch_id, gate_config=gate_config)

        console.print()
        console.print(
            f"[{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] {start_result.get('message', 'Orchestration started')}"
        )

        if gate_config:
            console.print(f"  Quality gates: {', '.join(gate_config)}")

        console.print()
        console.print(
            f"[dim]Monitor with:[/dim] [{NEON_CYAN}]sibyl agent orchestrate status[/{NEON_CYAN}]"
        )

        if json_out:
            output["status"] = "started"
            output["queue_size"] = queue_size
            print_json(output)

    try:
        _run()
    except SibylClientError as e:
        handle_client_error(e)
