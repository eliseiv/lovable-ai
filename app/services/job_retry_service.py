"""Повтор упавшей генерации внутри того же проекта (ADR-053).

Пользователь получил `FAILED` и хочет попробовать ещё раз, не пересоздавая проект и не
вводя всё заново. Новая джоба создаётся в ТОМ ЖЕ проекте, поэтому промпт, локаль, выбранная
модель и приложенные фото переезжают сами (они хранятся на проекте), а вопросы и ответы
интервью копируются из упавшей джобы — их владелец джоба.

Ретрай **не списывает** генерацию: упавшая попытка уже списала, и брать деньги второй раз за
наш сбой неправильно. Поэтому число бесплатных повторов на одну оплаченную генерацию
ограничено `GENERATION_RETRY_MAX_ATTEMPTS`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import conflict, not_found, payment_required
from app.auth.concurrency import is_within_concurrency_cap
from app.core.config import get_settings
from app.core.ids import new_answer_id, new_job_id, new_question_id
from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.models import Answer, GenerationJob, Project, Question
from app.pipeline.dispatcher import dispatch_for_state
from app.pipeline.events import record_event

logger = get_logger(__name__)

# Маркер «списание для этой джобы улажено» — тот же тип события, которым `usage.py` защищается
# от двойного списания. Записав его заранее, ретрай проходит фазу интервью, не тратя квоту.
_USAGE_COUNTED_EVENT = "usage_counted"


@dataclass(frozen=True)
class RetriedJob:
    """Созданная (или уже существующая) повторная попытка."""

    job_id: str
    project_id: str
    retry_of_job_id: str
    state: str
    created: bool


async def _retry_chain_length(session: AsyncSession, job: GenerationJob) -> int:
    """Сколько повторов уже сделано в цепочке, к которой принадлежит `job`.

    Считаются джобы проекта с непустым `retry_of_job_id`: цепочка живёт внутри проекта, и
    лимит ограничивает именно повторы одной оплаченной генерации, а не проект целиком.
    """
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(GenerationJob)
                .where(
                    GenerationJob.project_id == job.project_id,
                    GenerationJob.retry_of_job_id.is_not(None),
                )
            )
        ).scalar_one()
    )


async def _existing_retry(session: AsyncSession, job_id: str) -> GenerationJob | None:
    """Уже созданный повтор этой джобы, если он есть (естественная идемпотентность)."""
    return (
        (
            await session.execute(
                select(GenerationJob).where(GenerationJob.retry_of_job_id == job_id)
            )
        )
        .scalars()
        .first()
    )


async def _copy_interview(session: AsyncSession, *, source_job_id: str, target_job_id: str) -> bool:
    """Копирует вопросы и ответы упавшей джобы в новую. True, если ответы полные.

    Вопросы принадлежат джобе, поэтому переносятся копией, а не ссылкой: иначе история
    упавшей попытки начала бы меняться задним числом. Полнота ответов решает, нужно ли снова
    гонять интервью.
    """
    questions = list(
        (
            await session.execute(
                select(Question).where(Question.job_id == source_job_id).order_by(Question.position)
            )
        )
        .scalars()
        .all()
    )
    if not questions:
        return False

    answers_by_question: dict[str, Answer] = {
        answer.question_id: answer
        for answer in (await session.execute(select(Answer).where(Answer.job_id == source_job_id)))
        .scalars()
        .all()
    }

    answered = 0
    for question in questions:
        copy = Question(
            id=new_question_id(),
            job_id=target_job_id,
            position=question.position,
            text=question.text,
            kind=question.kind,
            options=question.options,
        )
        session.add(copy)
        source_answer = answers_by_question.get(question.id)
        if source_answer is not None:
            session.add(
                Answer(
                    id=new_answer_id(),
                    question_id=copy.id,
                    job_id=target_job_id,
                    text=source_answer.text,
                )
            )
            answered += 1

    return answered == len(questions)


async def retry_failed_job(session: AsyncSession, *, user_id: str, job_id: str) -> RetriedJob:
    """Создаёт повтор упавшей генерации в том же проекте (ADR-053).

    Отказы: чужая/несуществующая джоба → 404; не `FAILED` или не генерация → 409; исчерпан
    лимит бесплатных повторов → 409; занят слот одновременных задач → 402 (тот же reason, что
    у обычного старта). Повторный вызов на той же джобе возвращает уже созданный повтор.
    """
    settings = get_settings()
    job = (
        await session.execute(
            select(GenerationJob).where(
                GenerationJob.id == job_id, GenerationJob.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if job is None:
        # Чужая джоба неотличима от несуществующей — существование чужих задач не раскрываем.
        raise not_found("Job not found.")

    existing = await _existing_retry(session, job.id)
    if existing is not None:
        return RetriedJob(
            job_id=existing.id,
            project_id=existing.project_id,
            retry_of_job_id=job.id,
            state=existing.state.value,
            created=False,
        )

    if job.kind != "generation":
        raise conflict("Only generation jobs can be retried.", current_state=job.state.value)
    if job.state != JobState.FAILED:
        raise conflict("Only a failed job can be retried.", current_state=job.state.value)

    project = await session.get(Project, job.project_id)
    if project is None or project.deleted_at is not None:
        # Проект удаляется/удалён — повторять нечего (ADR-011 §C).
        raise not_found("Project not found.")

    if await _retry_chain_length(session, job) >= settings.generation_retry_max_attempts:
        raise conflict("Retry limit reached for this generation.", current_state=job.state.value)

    if not await is_within_concurrency_cap(session, user_id):
        # Тот же ответ, что у обычного старта при занятом слоте (ADR-009 §D).
        raise payment_required(
            "Concurrent jobs limit reached.",
            reason="concurrency_limit",
            required_entitlement="pro",
        )

    retry = GenerationJob(
        id=new_job_id(),
        project_id=job.project_id,
        user_id=user_id,
        state=JobState.CREATED,
        kind="generation",
        # Idempotency-Key не наследуется: он уникален на (user, key) и принадлежит исходному
        # запросу клиента. Повторный вызов ретрая идемпотентен через `_existing_retry`.
        idempotency_key=None,
        retry_of_job_id=job.id,
        max_fix_attempts=settings.max_fix_attempts,
        budget_usd=settings.job_budget_usd,
        wall_clock_deadline=datetime.now(UTC) + timedelta(seconds=settings.job_wall_clock_budget_s),
    )
    session.add(retry)
    await session.flush()

    interview_complete = await _copy_interview(
        session, source_job_id=job.id, target_job_id=retry.id
    )

    # Списание улажено заранее: маркер того же типа, которым usage.py защищается от двойного
    # счёта, — фаза интервью увидит его и не спишет вторую генерацию (ADR-053 §B).
    await record_event(
        session,
        retry.id,
        _USAGE_COUNTED_EVENT,
        payload={"source": "retry", "retry_of": job.id},
    )
    await record_event(
        session,
        retry.id,
        "job_created",
        to_state=JobState.CREATED.value,
        payload={"retry_of": job.id},
    )

    if interview_complete:
        # Ответы уже есть — интервью не переспрашиваем, идём сразу к спеке.
        retry.state = JobState.SPECCING
        await record_event(
            session,
            retry.id,
            "state_changed",
            from_state=JobState.CREATED.value,
            to_state=JobState.SPECCING.value,
            payload={"retry_of": job.id},
        )

    await session.commit()
    dispatch_for_state(retry.id, retry.state, kind="generation")
    logger.info(
        "job_retry_created",
        extra={
            "job_id": retry.id,
            "retry_of": job.id,
            "project_id": retry.project_id,
            "state": retry.state.value,
        },
    )
    return RetriedJob(
        job_id=retry.id,
        project_id=retry.project_id,
        retry_of_job_id=job.id,
        state=retry.state.value,
        created=True,
    )


__all__ = ["RetriedJob", "retry_failed_job"]
