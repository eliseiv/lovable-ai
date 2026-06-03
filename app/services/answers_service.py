"""Приём ответов и резюм пайплайна (docs/modules/api/02-api-contracts.md, Q-PIPELINE-2).

Детерминированная матрица 200/202/409/422:
- Первый валидный сабмит в AWAITING_CLARIFICATION → применить, → SPECCING, task → 202.
- Повтор тех же ответов на продвинувшейся джобе → идемпотентно 200.
- Другие ответы на продвинувшейся (не AWAITING) → 409.
- Сабмит в FAILED → 409.
- Частичные/конфликтующие/чужие question_id → 422.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_answer_id
from app.db.enums import JobState
from app.db.models import Answer, GenerationJob, Question
from app.pipeline.dispatcher import dispatch_for_state
from app.pipeline.events import publish_event, record_event
from app.schemas.api import AnswerItem


class AnswersOutcome(enum.StrEnum):
    APPLIED = "applied"  # 202
    IDEMPOTENT = "idempotent"  # 200
    CONFLICT = "conflict"  # 409
    INVALID = "invalid"  # 422


@dataclass(frozen=True)
class AnswersResult:
    outcome: AnswersOutcome
    job_id: str
    detail: str | None = None
    current_state: str | None = None


def _normalize(items: list[AnswerItem]) -> dict[str, str]:
    return {a.question_id: a.text for a in items}


async def submit_answers(
    session: AsyncSession,
    *,
    job: GenerationJob,
    items: list[AnswerItem],
) -> AnswersResult:
    """Применяет матрицу состояний/ответов. job уже загружена и принадлежит юзеру."""
    # Дубли question_id в одном теле → 422.
    incoming_ids = [a.question_id for a in items]
    if len(incoming_ids) != len(set(incoming_ids)):
        return AnswersResult(AnswersOutcome.INVALID, job.id, "duplicate question_id in body")

    questions = (
        (await session.execute(select(Question).where(Question.job_id == job.id))).scalars().all()
    )
    job_question_ids = {q.id for q in questions}

    # Любой question_id, не принадлежащий джобе → 422.
    if any(qid not in job_question_ids for qid in incoming_ids):
        return AnswersResult(AnswersOutcome.INVALID, job.id, "question_id does not belong to job")

    # Полнота: должны быть отвечены все обязательные (= все) вопросы джобы.
    if set(incoming_ids) != job_question_ids:
        return AnswersResult(AnswersOutcome.INVALID, job.id, "not all required questions answered")

    normalized_incoming = _normalize(items)

    # Уже сохранённые ответы джобы (для идемпотентности/конфликта).
    existing_answers = (
        (await session.execute(select(Answer).where(Answer.job_id == job.id))).scalars().all()
    )
    existing_normalized = {a.question_id: a.text for a in existing_answers}

    if job.state == JobState.AWAITING_CLARIFICATION:
        # Атомарный conditional UPDATE: только один из конкурентных POST /answers
        # переведёт AWAITING_CLARIFICATION → SPECCING (защита от двойного сабмита/гонки).
        update_result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
            update(GenerationJob)
            .where(
                GenerationJob.id == job.id,
                GenerationJob.state == JobState.AWAITING_CLARIFICATION,
            )
            .values(state=JobState.SPECCING)
        )
        if update_result.rowcount != 1:
            # Гонку выиграл другой запрос: джоба уже сдвинулась. Откатываем своё,
            # перечитываем актуальное состояние и ответы победителя, затем решаем
            # 200/409 по совпадению (как для уже продвинувшейся джобы).
            await session.rollback()
            await session.refresh(job)
            winner_answers = (
                (await session.execute(select(Answer).where(Answer.job_id == job.id)))
                .scalars()
                .all()
            )
            winner_normalized = {a.question_id: a.text for a in winner_answers}
            if winner_normalized == normalized_incoming:
                return AnswersResult(
                    AnswersOutcome.IDEMPOTENT, job.id, current_state=job.state.value
                )
            return AnswersResult(
                AnswersOutcome.CONFLICT,
                job.id,
                "answers already fixed and pipeline progressed",
                current_state=job.state.value,
            )

        # Победитель гонки — применяем ответы и резюмим пайплайн.
        for item in items:
            session.add(
                Answer(
                    id=new_answer_id(),
                    question_id=item.question_id,
                    job_id=job.id,
                    text=item.text,
                )
            )
        await record_event(
            session,
            job.id,
            "answers_submitted",
            payload={"count": len(items)},
        )
        await record_event(
            session,
            job.id,
            "state_changed",
            from_state=JobState.AWAITING_CLARIFICATION.value,
            to_state=JobState.SPECCING.value,
            payload={"reason": "answers_submitted"},
        )
        await session.commit()
        # ORM-объект синхронизируем с уже зафиксированным переходом.
        job.state = JobState.SPECCING
        await publish_event(
            job.id,
            "state_changed",
            to_state=JobState.SPECCING.value,
            payload={"reason": "answers_submitted"},
        )
        dispatch_for_state(job.id, JobState.SPECCING)
        return AnswersResult(AnswersOutcome.APPLIED, job.id)

    if job.state == JobState.FAILED:
        return AnswersResult(
            AnswersOutcome.CONFLICT,
            job.id,
            "job is in terminal FAILED state",
            current_state=job.state.value,
        )

    # Джоба уже продвинулась (SPECCING/BUILDING/DEPLOYING/LIVE/FIXING/...).
    # Совпали ли ответы с уже зафиксированными → идемпотентно 200; иначе 409.
    if existing_normalized == normalized_incoming:
        return AnswersResult(AnswersOutcome.IDEMPOTENT, job.id, current_state=job.state.value)
    return AnswersResult(
        AnswersOutcome.CONFLICT,
        job.id,
        "answers already fixed and pipeline progressed",
        current_state=job.state.value,
    )
