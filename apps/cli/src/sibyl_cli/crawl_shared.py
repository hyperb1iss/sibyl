from collections.abc import Callable

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    error,
    info,
    print_json,
    run_async,
    success,
)


def add_crawl_source(
    url: str,
    *,
    name: str | None,
    source_type: str,
    depth: int,
    include_patterns: list[str] | None,
    json_out: bool,
    handle_client_error: Callable[[SibylClientError], None],
    next_step_command: str,
) -> None:
    @run_async
    async def _add() -> None:
        client = get_client()

        try:
            source_name = name or url.split("//")[-1].split("/")[0]

            response = await client.create_crawl_source(
                name=source_name,
                url=url,
                source_type=source_type,
                crawl_depth=depth,
                include_patterns=include_patterns or [],
            )

            if json_out:
                print_json(response)
                return

            if response.get("id"):
                success(f"Source added: {response['id']}")
                info(f"Run '{next_step_command} {response['id']}' to start crawling")
            else:
                error("Failed to add source")

        except SibylClientError as e:
            handle_client_error(e)

    _add()


def start_crawl_source(
    source_id: str,
    *,
    max_pages: int = 50,
    max_depth: int = 3,
    generate_embeddings: bool = True,
    json_out: bool = False,
    handle_client_error: Callable[[SibylClientError], None],
    status_command: str,
) -> None:
    @run_async
    async def _crawl() -> None:
        client = get_client()

        try:
            response = await client.start_crawl(
                source_id=source_id,
                max_pages=max_pages,
                max_depth=max_depth,
                generate_embeddings=generate_embeddings,
            )
            status = response.get("status", "unknown")

            if json_out:
                print_json(response)
                return

            if status in {"queued", "started"}:
                success(response.get("message", "Crawl queued"))
                info(f"Use '{status_command}' to check progress")
            elif status == "already_running":
                info(response.get("message", "Crawl already in progress"))
            else:
                error(f"Crawl failed: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            handle_client_error(e)

    _crawl()


def show_source_status(
    source_id: str,
    *,
    json_out: bool,
    handle_client_error: Callable[[SibylClientError], None],
) -> None:
    @run_async
    async def _status() -> None:
        client = get_client()

        try:
            source = await client.get_crawl_source(source_id)
            status_response = await client.get_crawl_status(source_id)

            if json_out:
                status_data = {
                    "id": source.get("id"),
                    "name": source.get("name"),
                    "url": source.get("url"),
                    "crawl_status": status_response.get(
                        "crawl_status", source.get("crawl_status", "pending")
                    ),
                    "document_count": status_response.get(
                        "document_count", source.get("document_count", 0)
                    ),
                    "chunk_count": status_response.get("chunk_count", source.get("chunk_count", 0)),
                    "current_job_id": status_response.get("current_job_id"),
                    "last_crawled_at": status_response.get("last_crawled_at"),
                    "last_error": status_response.get("last_error"),
                }
                print_json(status_data)
                return

            console.print(f"\n[{ELECTRIC_PURPLE}]Source Status[/{ELECTRIC_PURPLE}]\n")
            console.print(f"  Name: [{NEON_CYAN}]{source.get('name', '')}[/{NEON_CYAN}]")
            console.print(f"  URL: {source.get('url', '-')}")
            console.print(
                f"  Status: {status_response.get('crawl_status', source.get('crawl_status', 'pending'))}"
            )
            console.print(
                f"  Documents: {status_response.get('document_count', source.get('document_count', 0))}"
            )
            console.print(
                f"  Chunks: {status_response.get('chunk_count', source.get('chunk_count', 0))}"
            )
            console.print(
                f"  Last Crawled: {status_response.get('last_crawled_at', source.get('last_crawled_at', 'never')) or 'never'}"
            )

            if status_response.get("current_job_id"):
                console.print(f"  Job ID: {status_response['current_job_id']}")
            if status_response.get("last_error"):
                error(f"Last Error: {status_response['last_error']}")

        except SibylClientError as e:
            handle_client_error(e)

    _status()
