"""Integration: POST /answers матрица 200/202/409/422 + резюм пайплайна (Q-PIPELINE-2).

Реальный Postgres. Покрывает: первый валидный сабмит (202→SPECCING), идемпотентный
повтор (200), другие ответы на продвинувшейся (409), сабмит в FAILED (409),
частичные/дублирующиеся/чужие question_id (422). Проверяет append-only job_events.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.enums import JobState
from app.db.models import Answer, GenerationJob, JobEvent, Project, Question
from app.schemas.api import AnswerItem
from app.services.answers_service import AnswersOutcome, submit_answers

pytestmark = pytest.mark.asyncio


async def _make_job(session, user_id, state=JobState.AWAITING_CLARIFICATION, n_questions=2):  # noqa: ANN001, ANN201
    from app.core.ids import new_job_id, new_project_id, new_question_id

    project = Project(id=new_project_id(), user_id=user_id, prompt="p", title=None)
    job = GenerationJob(
        id=new_job_id(),
        project_id=project.id,
        user_id=user_id,
        state=state,
        kind="generation",
        budget_usd=Decimal("5.0000"),
    )
    session.add(project)
    session.add(job)
    await session.flush()
    qids = []
    for i in range(n_questions):
        qid = new_question_id()
        qids.append(qid)
        session.add(Question(id=qid, job_id=job.id, position=i + 1, text=f"Q{i}", kind="free_text"))
    await session.flush()
    return job, qids


@pytest_asyncio.fixture
async def job_awaiting(session, seeded_user):  # noqa: ANN201
    return await _make_job(session, seeded_user.id, JobState.AWAITING_CLARIFICATION)


# --- 202 первый валидный сабмит ---


async def test_first_valid_submit_applies_and_moves_to_speccing(
    session, job_awaiting, no_side_effects
):
    job, qids = job_awaiting
    items = [AnswerItem(question_id=qids[0], text="a0"), AnswerItem(question_id=qids[1], text="a1")]
    result = await submit_answers(session, job=job, items=items)
    assert result.outcome == AnswersOutcome.APPLIED
    # Состояние → SPECCING (зафиксировано в БД).
    refreshed = await session.get(GenerationJob, job.id)
    assert refreshed.state == JobState.SPECCING
    # Ответы записаны (по одному на вопрос).
    answers = (await session.execute(select(Answer).where(Answer.job_id == job.id))).scalars().all()
    assert len(answers) == 2
    # Резюм: ровно один enqueue SPECCING + один publish.
    assert no_side_effects["dispatched"] == [(job.id, JobState.SPECCING)]
    assert any(p[1] == "state_changed" for p in no_side_effects["published"])
    # job_events append-only: есть answers_submitted и state_changed.
    events = (
        (await session.execute(select(JobEvent).where(JobEvent.job_id == job.id))).scalars().all()
    )
    types = {e.event_type for e in events}
    assert "answers_submitted" in types
    assert "state_changed" in types


# --- 200 идемпотентный повтор тех же ответов ---


async def test_idempotent_repeat_same_answers_returns_200(session, job_awaiting, no_side_effects):
    job, qids = job_awaiting
    items = [AnswerItem(question_id=qids[0], text="a0"), AnswerItem(question_id=qids[1], text="a1")]
    first = await submit_answers(session, job=job, items=items)
    assert first.outcome == AnswersOutcome.APPLIED
    # Повтор тех же ответов на продвинувшейся (SPECCING) джобе → идемпотентно.
    second = await submit_answers(session, job=job, items=items)
    assert second.outcome == AnswersOutcome.IDEMPOTENT
    # Не задвоили dispatch (только первый APPLIED дёрнул).
    assert no_side_effects["dispatched"] == [(job.id, JobState.SPECCING)]


# --- 409 другие ответы на продвинувшейся ---


async def test_different_answers_on_progressed_job_conflict(session, job_awaiting, no_side_effects):
    job, qids = job_awaiting
    items = [AnswerItem(question_id=qids[0], text="a0"), AnswerItem(question_id=qids[1], text="a1")]
    await submit_answers(session, job=job, items=items)
    other = [AnswerItem(question_id=qids[0], text="X"), AnswerItem(question_id=qids[1], text="Y")]
    result = await submit_answers(session, job=job, items=other)
    assert result.outcome == AnswersOutcome.CONFLICT
    assert result.current_state == JobState.SPECCING.value


# --- 409 сабмит в FAILED ---


async def test_submit_to_failed_conflict(session, seeded_user):
    job, qids = await _make_job(session, seeded_user.id, JobState.FAILED)
    items = [AnswerItem(question_id=qids[0], text="a"), AnswerItem(question_id=qids[1], text="b")]
    result = await submit_answers(session, job=job, items=items)
    assert result.outcome == AnswersOutcome.CONFLICT
    assert result.current_state == JobState.FAILED.value


# --- 422 частичные / дубли / чужие question_id ---


async def test_partial_answers_invalid(session, job_awaiting):
    job, qids = job_awaiting
    result = await submit_answers(
        session, job=job, items=[AnswerItem(question_id=qids[0], text="a")]
    )
    assert result.outcome == AnswersOutcome.INVALID
    assert "not all required" in (result.detail or "")


async def test_duplicate_question_id_invalid(session, job_awaiting):
    job, qids = job_awaiting
    items = [AnswerItem(question_id=qids[0], text="a"), AnswerItem(question_id=qids[0], text="b")]
    result = await submit_answers(session, job=job, items=items)
    assert result.outcome == AnswersOutcome.INVALID
    assert "duplicate" in (result.detail or "")


async def test_foreign_question_id_invalid(session, job_awaiting):
    job, qids = job_awaiting
    items = [
        AnswerItem(question_id=qids[0], text="a"),
        AnswerItem(question_id="q_doesnotbelong0000000000", text="b"),
    ]
    result = await submit_answers(session, job=job, items=items)
    assert result.outcome == AnswersOutcome.INVALID
    assert "does not belong" in (result.detail or "")
