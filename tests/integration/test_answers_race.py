"""Integration: POST /answers double-submit race (Q-PIPELINE-2).

Два конкурентных сабмита в AWAITING_CLARIFICATION → ровно один 202 (один переход
SPECCING, один enqueue, answers не задвоены), второй 200/409. Реальный Postgres,
автономные транзакции (раздельные соединения) — без общей rollback-сессии.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.core.ids import new_job_id, new_project_id, new_question_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import Answer, GenerationJob, Project, Question, User
from app.db.session import session_scope
from app.schemas.api import AnswerItem
from app.services.answers_service import AnswersOutcome, submit_answers

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def awaiting_job_committed(autonomous_db, monkeypatch):  # noqa: ANN001, ANN201
    """Создаёт committed job в AWAITING_CLARIFICATION + 2 вопроса; чистит в teardown."""
    import app.pipeline.dispatcher as disp

    monkeypatch.setattr(disp, "dispatch_for_state", lambda *a, **k: None)

    uid = "u_raceowner0000000000000"
    pid = new_project_id()
    jid = new_job_id()
    qids = [new_question_id(), new_question_id()]
    # Идемпотентная пред-очистка (если предыдущий прогон оборвался до teardown).
    async with session_scope() as s:
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == uid)))
            .scalars()
            .all()
        )
        old_pids = (
            (await s.execute(select(GenerationJob.project_id).where(GenerationJob.user_id == uid)))
            .scalars()
            .all()
        )
        from app.db.models import JobEvent

        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
            await s.execute(delete(Answer).where(Answer.job_id.in_(job_ids)))
            await s.execute(delete(Question).where(Question.job_id.in_(job_ids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == uid))
        for opid in set(old_pids):
            await s.execute(delete(Project).where(Project.id == opid))
        await s.execute(delete(User).where(User.id == uid))
        await s.commit()
    async with session_scope() as s:
        s.add(
            User(
                id=uid,
                api_key_hash=hash_api_key("race-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        s.add(Project(id=pid, user_id=uid, prompt="p", title=None))
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=uid,
                state=JobState.AWAITING_CLARIFICATION,
                kind="generation",
                budget_usd=Decimal("5.0000"),
            )
        )
        for i, qid in enumerate(qids):
            s.add(Question(id=qid, job_id=jid, position=i + 1, text=f"Q{i}", kind="free_text"))
        await s.commit()
    yield jid, qids
    async with session_scope() as s:
        from app.db.models import JobEvent

        await s.execute(delete(JobEvent).where(JobEvent.job_id == jid))
        await s.execute(delete(Answer).where(Answer.job_id == jid))
        await s.execute(delete(Question).where(Question.job_id == jid))
        await s.execute(delete(GenerationJob).where(GenerationJob.id == jid))
        await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == uid))
        await s.commit()


async def test_double_submit_one_winner(awaiting_job_committed, monkeypatch):
    jid, qids = awaiting_job_committed
    # publish_event → no-op (без Redis в этом юнит-сценарии гонки).
    import app.services.answers_service as answers_mod

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(answers_mod, "publish_event", _noop_publish)
    monkeypatch.setattr(answers_mod, "dispatch_for_state", lambda *a, **k: None)

    items = [AnswerItem(question_id=qids[0], text="a0"), AnswerItem(question_id=qids[1], text="a1")]

    async def _submit():  # noqa: ANN202
        async with session_scope() as s:
            job = await s.get(GenerationJob, jid)
            return await submit_answers(s, job=job, items=items)

    results = await asyncio.gather(_submit(), _submit(), return_exceptions=True)
    assert all(not isinstance(r, Exception) for r in results), results

    outcomes = sorted(r.outcome for r in results)
    # Один APPLIED (202); второй — IDEMPOTENT (200) или CONFLICT (409), но НЕ второй APPLIED.
    assert outcomes.count(AnswersOutcome.APPLIED) == 1
    assert AnswersOutcome.APPLIED in outcomes
    other = [o for o in outcomes if o != AnswersOutcome.APPLIED][0]
    assert other in (AnswersOutcome.IDEMPOTENT, AnswersOutcome.CONFLICT)

    # Ровно один переход в SPECCING и answers НЕ задвоены (2 ответа, не 4).
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.SPECCING
        n_answers = await s.scalar(
            select(func.count()).select_from(Answer).where(Answer.job_id == jid)
        )
    assert n_answers == 2, f"answers задвоены: {n_answers}"
