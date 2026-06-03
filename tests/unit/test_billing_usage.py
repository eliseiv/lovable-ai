"""Unit: usage_counters инкремент на успешном старте генерации (docs/billing/03 §5).

Инкремент только kind='generation'; идемпотентно по job_id (job_events-маркер usage_counted,
acks_late/реплей не двоит); current_period = YYYY-MM (UTC). НЕ инкрементит на /projects,
/answers — инкремент привязан к count_generation_start, который вызывается только из
pipeline-старта (CREATED→INTERVIEWING), не из этих сервисов.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.billing import usage
from app.core.ids import new_job_id, new_project_id
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, Project, User

pytestmark = pytest.mark.asyncio


async def _job(session, *, kind: str = "generation", uid: str) -> GenerationJob:  # noqa: ANN001
    session.add(
        User(id=uid, api_key_hash=None, monthly_budget_usd=Decimal("50.0000"), status="active")
    )
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=uid, prompt="p", title=None))
    job = GenerationJob(
        id=jid,
        project_id=pid,
        user_id=uid,
        state=JobState.CREATED,
        kind=kind,
        budget_usd=Decimal("5.0000"),
    )
    session.add(job)
    await session.flush()
    return job


def test_current_period_format():
    from datetime import UTC, datetime

    assert usage.current_period(datetime(2026, 6, 2, tzinfo=UTC)) == "2026-06"


async def test_increment_on_generation_start(session):
    job = await _job(session, uid="u_usage_inc000000000001")
    applied = await usage.count_generation_start(session, job)
    assert applied is True
    assert await usage.get_usage(session, job.user_id) == 1


async def test_increment_idempotent_by_job_id(session):
    job = await _job(session, uid="u_usage_idem00000000001")
    assert await usage.count_generation_start(session, job) is True
    # В production transition() коммитит после count_generation_start (одна транзакция со
    # стартом). Повтор — отдельная task-транзакция (acks_late/crash-resume). flush делает
    # маркер usage_counted видимым для guard'а следующего вызова (autoflush=False в тестах).
    await session.flush()
    assert await usage.count_generation_start(session, job) is False
    await session.flush()
    assert await usage.count_generation_start(session, job) is False
    assert await usage.get_usage(session, job.user_id) == 1
    # Ровно один маркер usage_counted.
    marker_count = await session.scalar(
        select(func.count())
        .select_from(JobEvent)
        .where(JobEvent.job_id == job.id, JobEvent.event_type == "usage_counted")
    )
    assert marker_count == 1


async def test_no_increment_for_non_generation_kind(session):
    job = await _job(session, kind="edit", uid="u_usage_edit00000000001")
    assert await usage.count_generation_start(session, job) is False
    assert await usage.get_usage(session, job.user_id) == 0


async def test_two_distinct_jobs_each_increment(session):
    uid = "u_usage_two0000000000001"
    job1 = await _job(session, uid=uid)
    # Второй джоб тому же пользователю.
    pid = new_project_id()
    session.add(Project(id=pid, user_id=uid, prompt="p2", title=None))
    job2 = GenerationJob(
        id=new_job_id(),
        project_id=pid,
        user_id=uid,
        state=JobState.CREATED,
        kind="generation",
        budget_usd=Decimal("5.0000"),
    )
    session.add(job2)
    await session.flush()
    assert await usage.count_generation_start(session, job1) is True
    assert await usage.count_generation_start(session, job2) is True
    assert await usage.get_usage(session, uid) == 2


async def test_get_usage_zero_when_no_counter(session):
    user = User(
        id="u_usage_zero0000000001",
        api_key_hash=None,
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    assert await usage.get_usage(session, user.id) == 0
