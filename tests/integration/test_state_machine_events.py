"""Integration: transition пишет job_events (append-only) + publish в Redis (events.py)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.ids import new_job_id, new_project_id
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, Project
from app.pipeline.events import fail_job, record_event, transition

pytestmark = pytest.mark.asyncio


async def _make_job(session, user_id, state=JobState.SPECCING):  # noqa: ANN001, ANN201
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=user_id, prompt="p", title=None))
    job = GenerationJob(
        id=jid,
        project_id=pid,
        user_id=user_id,
        state=state,
        kind="generation",
        budget_usd=Decimal("5.0000"),
    )
    session.add(job)
    await session.flush()
    return job


async def test_transition_updates_state_and_appends_event(session, seeded_user, monkeypatch):
    # publish_event → no-op (Redis проверяется отдельно).
    import app.pipeline.events as events

    async def _noop(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(events, "publish_event", _noop)

    job = await _make_job(session, seeded_user.id, JobState.SPECCING)
    await transition(session, job, JobState.BUILDING, payload={"source_ref": "x"})
    refreshed = await session.get(GenerationJob, job.id)
    assert refreshed.state == JobState.BUILDING
    events_rows = (
        await session.execute(JobEvent.__table__.select().where(JobEvent.job_id == job.id))
    ).all()
    assert len(events_rows) == 1
    row = events_rows[0]._mapping
    assert row["from_state"] == "SPECCING"
    assert row["to_state"] == "BUILDING"


async def test_record_event_is_append_only(session, seeded_user):
    job = await _make_job(session, seeded_user.id, JobState.CREATED)
    await record_event(session, job.id, "a", to_state="CREATED")
    await record_event(session, job.id, "b", to_state="INTERVIEWING")
    await session.flush()
    rows = (
        await session.execute(JobEvent.__table__.select().where(JobEvent.job_id == job.id))
    ).all()
    assert len(rows) == 2


async def test_fail_job_sets_reason_and_failed_state(session, seeded_user, monkeypatch):
    import app.pipeline.events as events

    async def _noop(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(events, "publish_event", _noop)
    job = await _make_job(session, seeded_user.id, JobState.BUILDING)
    await fail_job(
        session,
        job,
        failure_reason="build_unrecoverable",
        last_failure_signature="vite_build_failed",
    )
    refreshed = await session.get(GenerationJob, job.id)
    assert refreshed.state == JobState.FAILED
    assert refreshed.failure_reason == "build_unrecoverable"
    assert refreshed.last_failure_signature == "vite_build_failed"


async def test_publish_event_to_real_redis_does_not_raise(seeded_user):
    """publish_event к реальному Redis: публикация не падает (best-effort)."""
    from app.pipeline.events import publish_event

    # Достаточно, что вызов отрабатывает без исключения (нет подписчика — 0 получателей).
    await publish_event("j_pubtest", "state_changed", to_state="LIVE", payload={"x": 1})
