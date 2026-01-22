"""Agent communication CLI commands.

Commands for inter-agent messaging during distributed execution:
- progress: Report progress to parent agent
- blocker: Signal a blocker
- query: Ask another agent a question
- delegate: Delegate work to another agent
- review: Request code review
- inbox: Check pending messages
- respond: Respond to a message
- conversation: View conversation history with another agent

These commands enable Claude Code agents to communicate and coordinate through Sibyl.
"""

from typing import Annotated

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_table,
    handle_client_error,
    info,
    print_json,
    run_async,
    success,
)

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
