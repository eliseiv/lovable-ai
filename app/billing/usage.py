"""Учёт usage_counters: инкремент на успешный старт генерации (docs/billing/03 §5).

Инкремент generations_used (атомарный upsert ON CONFLICT (user_id, period)) — на
УСПЕШНОМ старте генерации (kind='generation'), не на POST /projects и не на /answers.
Идемпотентность по job_id: guard от двойного инкремента при Celery acks_late/реплее —
job_events-маркер usage_counted. period = YYYY-MM (UTC) на момент старта.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import entitlements
from app.core.logging import get_logger
from app.db.models import EditUsageCounter, GenerationJob, JobEvent, UsageCounter, User
from app.pipeline.events import record_event

logger = get_logger(__name__)

_USAGE_COUNTED_EVENT = "usage_counted"
_EDIT_USAGE_COUNTED_EVENT = "edit_usage_counted"


def current_period(now: datetime | None = None) -> str:
    """Текущий биллинговый период YYYY-MM (UTC)."""
    moment = now or datetime.now(UTC)
    return moment.strftime("%Y-%m")


async def _already_counted(session: AsyncSession, job_id: str) -> bool:
    """True, если для job_id уже зафиксирован usage_counted (guard от двойного инкремента)."""
    result = await session.execute(
        select(func.count())
        .select_from(JobEvent)
        .where(JobEvent.job_id == job_id, JobEvent.event_type == _USAGE_COUNTED_EVENT)
    )
    return int(result.scalar_one()) > 0


async def _plan_quota_exhausted(session: AsyncSession, user_id: str, period: str) -> bool:
    """True, если плановая месячная квота генераций уже исчерпана (generations_used >= лимит).

    Источник лимита — plan_quotas по реальному access_level (Adapty-кэш). При отсутствии
    plan_quotas (несидировано) — трактуем как нулевой потолок (исчерпана), чтобы кредиты
    тратились вместо несуществующей плановой квоты, а не «зависали».
    """
    access_level = await entitlements.resolve_access_level(session, user_id)
    quota = await entitlements.get_plan_quota(session, access_level)
    monthly = quota.monthly_generations if quota is not None else 0
    used = await get_usage(session, user_id, period)
    return used >= monthly


async def count_generation_start(session: AsyncSession, job: GenerationJob) -> bool:
    """Списание ОДНОЙ величины на успешном старте генерации. True, если списание применено.

    Порядок (docs/modules/billing/03 §10.3, ADR-021 §D): плановая квота ТРАТИТСЯ ПЕРВОЙ —
    пока usage_counters.generations_used < monthly_generations, инкрементируется счётчик;
    по её исчерпании и при bonus_generations_balance > 0 — декрементируется баланс кредитов
    (строку credit_grants списание НЕ создаёт). Меняется РОВНО ОДНА величина на старт.

    Идемпотентно по job_id (общий job_events-маркер usage_counted покрывает обе ветки —
    нет двойного списания при Celery acks_late/реплее). Только kind='generation' (правки —
    отдельный счётчик S5). Коммит — на стороне вызывающего (одна транзакция с переходом джобы).
    """
    if job.kind != "generation":
        return False
    if await _already_counted(session, job.id):
        # Повтор (acks_late/crash-resume того же job_id) — no-op (общий guard для обеих веток).
        return False

    period = current_period()

    # Плановая квота исчерпана и есть кредиты → декремент баланса вместо инкремента счётчика.
    if await _plan_quota_exhausted(session, job.user_id, period):
        decremented = await _try_decrement_credit(session, job.user_id)
        if decremented:
            await record_event(
                session, job.id, _USAGE_COUNTED_EVENT, payload={"period": period, "source": "bonus"}
            )
            logger.info(
                "generation_counted_bonus",
                extra={"job_id": job.id, "user_id": job.user_id, "period": period},
            )
            return True
        # Кредитов нет — гейт квоту пропустил по плановой (race) либо free без кредитов:
        # фиксируем плановый инкремент (счётчик мог не достичь лимита между гейтом и стартом).

    stmt = (
        pg_insert(UsageCounter)
        .values(user_id=job.user_id, period=period, generations_used=1)
        .on_conflict_do_update(
            index_elements=[UsageCounter.user_id, UsageCounter.period],
            set_={"generations_used": UsageCounter.generations_used + 1},
        )
    )
    await session.execute(stmt)
    # Маркер идемпотентности: повторный старт того же job_id не инкрементит снова.
    await record_event(
        session, job.id, _USAGE_COUNTED_EVENT, payload={"period": period, "source": "plan"}
    )
    logger.info(
        "generation_counted", extra={"job_id": job.id, "user_id": job.user_id, "period": period}
    )
    return True


async def _try_decrement_credit(session: AsyncSession, user_id: str) -> bool:
    """Атомарный декремент users.bonus_generations_balance на 1, если баланс > 0.

    Условный UPDATE (WHERE balance > 0) — конкурентно-безопасен (одна джоба декрементит).
    True, если списание прошло (была хотя бы 1 единица кредита). Инвариант >= 0 сохраняется.
    """
    result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
        update(User)
        .where(User.id == user_id, User.bonus_generations_balance > 0)
        .values(bonus_generations_balance=User.bonus_generations_balance - 1)
    )
    return result.rowcount > 0


async def get_usage(session: AsyncSession, user_id: str, period: str | None = None) -> int:
    """generations_used пользователя за период (текущий по умолчанию). 0, если строки нет."""
    counter = await session.get(UsageCounter, (user_id, period or current_period()))
    return counter.generations_used if counter is not None else 0


# --- Sprint 5: отдельный счётчик правок (kind='edit', ADR-014, docs/billing §7) ---


async def _edit_already_counted(session: AsyncSession, job_id: str) -> bool:
    """True, если для edit-job_id уже зафиксирован edit_usage_counted (guard от двойного инкр.)."""
    result = await session.execute(
        select(func.count())
        .select_from(JobEvent)
        .where(JobEvent.job_id == job_id, JobEvent.event_type == _EDIT_USAGE_COUNTED_EVENT)
    )
    return int(result.scalar_one()) > 0


async def count_edit_start(session: AsyncSession, job: GenerationJob) -> bool:
    """Инкремент edit_usage_counters на успешном старте edit-джобы. True, если применён.

    Только kind='edit'. Точка инкремента (ADR-014 §A / docs/billing §7) — успешный старт
    edit-джобы (постановка первой task_fix-edit), НЕ на POST /edits и НЕ на rollback.
    Идемпотентно по job_id (job_events-маркер edit_usage_counted). Коммит — на стороне
    вызывающего (одна транзакция с переходом edit-джобы в FIXING).
    """
    if job.kind != "edit":
        return False
    if await _edit_already_counted(session, job.id):
        return False

    period = current_period()
    stmt = (
        pg_insert(EditUsageCounter)
        .values(user_id=job.user_id, period=period, edits_used=1)
        .on_conflict_do_update(
            index_elements=[EditUsageCounter.user_id, EditUsageCounter.period],
            set_={"edits_used": EditUsageCounter.edits_used + 1},
        )
    )
    await session.execute(stmt)
    await record_event(session, job.id, _EDIT_USAGE_COUNTED_EVENT, payload={"period": period})
    logger.info("edit_counted", extra={"job_id": job.id, "user_id": job.user_id, "period": period})
    return True


async def get_edit_usage(session: AsyncSession, user_id: str, period: str | None = None) -> int:
    """edits_used пользователя за период (текущий по умолчанию). 0, если строки нет."""
    counter = await session.get(EditUsageCounter, (user_id, period or current_period()))
    return counter.edits_used if counter is not None else 0
