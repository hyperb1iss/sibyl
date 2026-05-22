"""DB-backed LLM budget enforcement."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sibyl.locks import entity_lock
from sibyl.persistence.surreal.auth import surreal_auth_client_scope
from sibyl.services.settings import SettingsService
from sibyl_core.ai.errors import LLMBudgetExceededError, LLMConfigError
from sibyl_core.ai.llm.budget import LLMBudgetContext

DEFAULT_MONTHLY_USER_TOKENS = 1_000_000
DEFAULT_MONTHLY_ORG_TOKENS = 30_000_000
USER_BUDGET_SETTING = "llm.budget.monthly_user_tokens"
ORG_BUDGET_SETTING = "llm.budget.monthly_org_tokens"

type SurrealRecord = dict[str, object]


@dataclass(frozen=True, slots=True)
class LLMBudgetLimit:
    key: str
    subject_type: str
    subject_id: str
    organization_id: str | None
    limit: int


class DBLLMBudgetEnforcer:
    def __init__(self, settings_service: SettingsService) -> None:
        self._settings = settings_service

    async def reserve(
        self,
        context: LLMBudgetContext,
        *,
        surface: str,
        estimated_tokens: int,
    ) -> None:
        now = _utcnow()
        month = now.strftime("%Y-%m")
        limits = await self._limits(context, month=month)
        if not limits:
            return

        async with AsyncExitStack() as locks:
            for limit in sorted(limits, key=lambda item: item.key):
                await locks.enter_async_context(entity_lock("auth", f"llm-budget:{limit.key}"))
            async with surreal_auth_client_scope() as client:
                await self._reserve_with_locks(
                    client,
                    limits=limits,
                    month=month,
                    now=now,
                    surface=surface,
                    estimated_tokens=estimated_tokens,
                )

    async def _reserve_with_locks(
        self,
        client,
        *,
        limits: list[LLMBudgetLimit],
        month: str,
        now: datetime,
        surface: str,
        estimated_tokens: int,
    ) -> None:
        buckets = [await _ensure_bucket(client, limit, month=month, now=now) for limit in limits]
        for limit, bucket in zip(limits, buckets, strict=True):
            used_tokens = _int_value(bucket.get("used_tokens"))
            if used_tokens + estimated_tokens > limit.limit:
                raise LLMBudgetExceededError(
                    f"LLM {limit.subject_type} monthly budget exceeded",
                    surface=surface,
                    details={
                        "subject_type": limit.subject_type,
                        "subject_id": limit.subject_id,
                        "monthly_limit": limit.limit,
                        "used_tokens": used_tokens,
                        "requested_tokens": estimated_tokens,
                        "bucket_month": month,
                    },
                )

        for limit, bucket in zip(limits, buckets, strict=True):
            await client.execute_query(
                """
                    UPDATE llm_usage_buckets
                    SET used_tokens = $used_tokens,
                        updated_at = $updated_at
                    WHERE bucket_key = $bucket_key;
                """,
                bucket_key=str(bucket["bucket_key"]),
                used_tokens=_int_value(bucket.get("used_tokens")) + estimated_tokens,
                updated_at=now,
            )

    async def _limits(self, context: LLMBudgetContext, *, month: str) -> list[LLMBudgetLimit]:
        limits: list[LLMBudgetLimit] = []
        if context.user_id:
            user_limit = await _read_limit(
                self._settings,
                USER_BUDGET_SETTING,
                default=DEFAULT_MONTHLY_USER_TOKENS,
            )
            limits.append(
                LLMBudgetLimit(
                    key=f"user:{context.user_id}:{month}",
                    subject_type="user",
                    subject_id=context.user_id,
                    organization_id=context.organization_id,
                    limit=user_limit,
                )
            )
        if context.organization_id:
            org_limit = await _read_limit(
                self._settings,
                ORG_BUDGET_SETTING,
                default=DEFAULT_MONTHLY_ORG_TOKENS,
            )
            limits.append(
                LLMBudgetLimit(
                    key=f"org:{context.organization_id}:{month}",
                    subject_type="org",
                    subject_id=context.organization_id,
                    organization_id=context.organization_id,
                    limit=org_limit,
                )
            )
        return limits


async def _read_limit(
    settings: SettingsService,
    key: str,
    *,
    default: int,
) -> int:
    raw_value = await settings.get(key)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise LLMConfigError(f"Invalid LLM budget setting {key}: {raw_value}") from exc
    if value <= 0:
        raise LLMConfigError(f"Invalid LLM budget setting {key}: {raw_value}")
    return value


async def _ensure_bucket(
    client,
    limit: LLMBudgetLimit,
    *,
    month: str,
    now: datetime,
) -> SurrealRecord:
    existing = _normalize_records(
        await client.execute_query(
            "SELECT * FROM llm_usage_buckets WHERE bucket_key = $bucket_key LIMIT 1;",
            bucket_key=limit.key,
        )
    )
    if existing:
        return existing[0]

    record: SurrealRecord = {
        "uuid": str(uuid4()),
        "bucket_key": limit.key,
        "bucket_month": month,
        "subject_type": limit.subject_type,
        "subject_id": limit.subject_id,
        "organization_id": limit.organization_id,
        "used_tokens": 0,
        "created_at": now,
        "updated_at": now,
    }
    created = _normalize_records(
        await client.execute_query("CREATE llm_usage_buckets CONTENT $record;", record=record)
    )
    return created[0] if created else record


def _normalize_records(result: object) -> list[SurrealRecord]:
    if result is None:
        return []
    if isinstance(result, dict):
        return [{str(key): value for key, value in result.items()}]
    if not isinstance(result, list):
        return []

    records: list[SurrealRecord] = []
    for item in result:
        if isinstance(item, list):
            records.extend(_normalize_records(item))
        elif isinstance(item, dict):
            records.append({str(key): value for key, value in item.items()})
    return records


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value:
        return int(value)
    return 0


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
