"""Legacy job visibility helpers backed by the current relational runtime."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from sibyl.db.connection import get_session
from sibyl.db.models import CrawlSource, Organization


async def _source_visible_to_org(
    *,
    org_id: UUID,
    source_uuid: UUID,
    session: AsyncSession,
) -> bool:
    result = await session.execute(
        select(CrawlSource).where(
            col(CrawlSource.id) == source_uuid,
            col(CrawlSource.organization_id) == org_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def _job_visible_to_org(
    job: Any,
    *,
    org: Organization,
    session: AsyncSession | None = None,
    legacy_source_ids: set[UUID] | None = None,
) -> bool:
    """Return True if job metadata belongs to the given organization."""
    fn = getattr(job, "function", "") or ""
    args: list[Any] = list(getattr(job, "args", None) or ())
    kwargs = dict(getattr(job, "kwargs", None) or {})

    if fn == "create_entity" and len(args) >= 3:
        return str(args[2]) == str(org.id)
    if fn == "update_entity" and len(args) >= 4:
        return str(args[3]) == str(org.id)
    if fn in {"consolidate_org", "priority_decay"} and args:
        return str(args[0]) == str(org.id)

    if fn in {"crawl_source", "sync_source"} and len(args) >= 1:
        metadata_org_id = kwargs.get("organization_id")
        if metadata_org_id is not None:
            return str(metadata_org_id) == str(org.id)
        try:
            source_uuid = UUID(str(args[0]))
        except ValueError:
            return False
        if legacy_source_ids is not None:
            return source_uuid in legacy_source_ids
        if session is not None:
            return await _source_visible_to_org(
                org_id=org.id,
                source_uuid=source_uuid,
                session=session,
            )
        async with get_session() as db_session:
            return await _source_visible_to_org(
                org_id=org.id,
                source_uuid=source_uuid,
                session=db_session,
            )

    return False


async def _resolve_visible_legacy_source_ids(
    jobs: list[Any],
    *,
    org: Organization,
    session: AsyncSession | None = None,
) -> set[UUID]:
    source_ids: set[UUID] = set()

    for job in jobs:
        fn = getattr(job, "function", "") or ""
        if fn not in {"crawl_source", "sync_source"}:
            continue

        args: list[Any] = list(getattr(job, "args", None) or ())
        kwargs = dict(getattr(job, "kwargs", None) or {})
        if kwargs.get("organization_id") is not None or not args:
            continue

        try:
            source_ids.add(UUID(str(args[0])))
        except ValueError:
            continue

    if not source_ids:
        return set()

    if session is not None:
        result = await session.execute(
            select(col(CrawlSource.id)).where(
                col(CrawlSource.organization_id) == org.id,
                col(CrawlSource.id).in_(source_ids),
            )
        )
        return set(result.scalars().all())

    async with get_session() as db_session:
        result = await db_session.execute(
            select(col(CrawlSource.id)).where(
                col(CrawlSource.organization_id) == org.id,
                col(CrawlSource.id).in_(source_ids),
            )
        )
        return set(result.scalars().all())
