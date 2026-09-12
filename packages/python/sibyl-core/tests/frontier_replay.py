"""Fresh-process replay against the calling test's isolated native content store."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from urllib.parse import urlparse

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.config import settings
from sibyl_core.services import automatic_procedure, content_client, procedure_validation
from sibyl_core.services.automatic_reflection import automatically_review_reflection
from sibyl_core.services.eval_publication import _ExtractorPolicy
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.tasks.memory_validation import CriticOutput


async def main():
    args = json.load(sys.stdin)
    assert urlparse(args["url"]).hostname in {"127.0.0.1", "localhost"}
    assert args["namespace"].startswith("retention_")
    settings.surreal_url = args["url"]
    client = SurrealContentClient(
        url=args["url"], namespace=args["namespace"], username="", password=""
    )

    @asynccontextmanager
    async def session():
        yield client

    async def factory(output_type=CriticOutput):
        return Extractor(output_type), '{"model":"offline"}'

    async def forbidden(*args, **kwargs):
        raise AssertionError("Fresh-process replay attempted provider extraction")

    content_client.surreal_content_client = session
    procedure_validation.validation_extractor = factory
    procedure_validation._validation_extractor = factory
    Extractor.extract_with_usage = forbidden
    automatic_procedure._extractor_policy = AsyncMock(
        return_value=_ExtractorPolicy("test", "r", 100000, 2048, "tool", None)
    )
    try:
        for identity in (args["root"], args["candidate"]):
            if args["kind"] == "procedure":
                result = await automatic_procedure.automatically_reconsider_procedure(
                    organization_id=args["org"],
                    principal_id="owner",
                    parent_id=identity,
                    authorize=AsyncMock(),
                )
                candidate = result.candidate_id
            else:
                result = await automatically_review_reflection(
                    args["org"],
                    "owner",
                    identity,
                    AsyncMock(return_value=SourceReadAuthority("owner")),
                )
                candidate = result.candidate.id
            assert candidate == args["candidate"]
            assert list(result.executions) == args["executions"]
        print("FRESH_PROCESS_ROOT_CHILD_REPLAY_PASS", flush=True)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
