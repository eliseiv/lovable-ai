"""Учёт usage_counters: инкремент на успешный старт генерации (docs/billing/03 §5).

Инкремент generations_used (атомарный upsert ON CONFLICT (user_id, period)) — на
УСПЕШНОМ старте генерации (kind='generation'), не на POST /projects и не на /answers.
Идемпотентность по job_id: guard от двойного инкремента при Celery acks_late/реплее —
job_events-маркер usage_counted. period = YYYY-MM (UTC) на момент старта.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import EditUsageCounter, GenerationJob, JobEvent, UsageCounter
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


async def count_generation_start(session: AsyncSession, job: GenerationJob) -> bool:
    """Инкремент usage_counters на успешном старте генерации. True, если инкремент применён.

    Идемпотентно по job_id (job_events-маркер). Только kind='generation' (правки — отдельный
    счётчик S5). Коммит — на стороне вызывающего (одна транзакция с переходом джобы).
    """
    if job.kind != "generation":
        return False
    if await _already_counted(session, job.id):
        # Повтор (acks_late/crash-resume того же job_id) — no-op.
        return False

    period = current_period()
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
    await record_event(session, job.id, _USAGE_COUNTED_EVENT, payload={"period": period})
    logger.info(
        "generation_counted", extra={"job_id": job.id, "user_id": job.user_id, "period": period}
    )
    return True


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
