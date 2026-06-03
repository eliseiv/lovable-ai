"""Integration: beat-периодика sweeper + reconciler (docs §E).

Реальный Postgres + реальный Redis (dispatch-lock). dispatch_for_state — спай.

Покрывает:
- sweeper: AWAITING_CLARIFICATION старше TTL → FAILED(clarification_timeout);
  идемпотентно (повтор не трогает уже-FAILED); свежую джобу не трогает;
- reconciler: stuck BUILDING/DEPLOYING/FIXING старше STUCK_THRESHOLD_S →
  ре-диспетчеризация по текущему state (state НЕ меняется);
- свежую (не stuck) джобу reconciler не трогает;
- Redis dispatch-lock: повторный прогон reconciler в окне TTL lock'а НЕ дублирует
  постановку (двойная доставка beat-tick безопасна).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select, text

from app.core.config import get_settings
from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, Project, User
from app.db.session import session_scope

pytestmark = pytest.mark.asyncio

UID = "u_beatowner00000000000"


async def _purge(uid: str) -> None:
    async with session_scope() as s:
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == uid)))
            .scalars()
            .all()
        )
        pids = list(
            set(
                (
                    await s.execute(
                        select(GenerationJob.project_id).where(GenerationJob.user_id == uid)
                    )
                )
                .scalars()
                .all()
            )
        )
        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == uid))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == uid))
        await s.commit()


async def _ensure_user() -> None:
    async with session_scope() as s:
        existing = await s.get(User, UID)
        if existing is None:
            s.add(
                User(
                    id=UID,
                    api_key_hash=hash_api_key("beat-key"),
                    monthly_budget_usd=Decimal("50.0000"),
                    status="active",
                )
            )
            await s.commit()


async def _make_job(state: JobState, *, age_seconds: int) -> str:
    """Создаёт джобу в state с updated_at = now - age_seconds (для TTL/stuck-предикатов)."""
    pid = new_project_id()
    jid = new_job_id()
    async with session_scope() as s:
        s.add(Project(id=pid, user_id=UID, prompt="x", title=None))
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=UID,
                state=state,
                kind="generation",
                budget_usd=Decimal("5.0000"),
                spend_usd=Decimal("0.0000"),
            )
        )
        await s.commit()
    # Принудительно состарить updated_at (onupdate перебил бы значение при ORM-апдейте).
    old = datetime.now(UTC) - timedelta(seconds=age_seconds)
    async with session_scope() as s:
        await s.execute(
            text("UPDATE generation_jobs SET updated_at = :ts WHERE id = :id"),
            {"ts": old, "id": jid},
        )
        await s.commit()
    return jid


@pytest_asyncio.fixture
async def beat_env(autonomous_db):  # noqa: ANN001, ANN201
    await _purge(UID)
    await _ensure_user()
    # Чистим dispatch-локи в Redis (от прошлых прогонов).
    settings = get_settings()
    client = aioredis.from_url(settings.redis_url)
    try:
        async for key in client.scan_iter("dispatch:*"):
            await client.delete(key)
    finally:
        await client.aclose()
    yield
    await _purge(UID)


# --- sweeper ---


async def test_sweeper_expires_old_clarification(beat_env, monkeypatch):
    settings = get_settings()
    # Старше TTL (7 дней): возраст = TTL + запас.
    jid = await _make_job(
        JobState.AWAITING_CLARIFICATION, age_seconds=settings.clarification_ttl_s + 100
    )

    import app.workers.beat_tasks as beat

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr("app.pipeline.events.publish_event", _noop_publish)

    swept = await beat._sweep_clarifications()
    assert swept >= 1

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert job.failure_reason == "clarification_timeout"


async def test_sweeper_idempotent_no_double_fail(beat_env, monkeypatch):
    settings = get_settings()
    jid = await _make_job(
        JobState.AWAITING_CLARIFICATION, age_seconds=settings.clarification_ttl_s + 100
    )

    import app.workers.beat_tasks as beat

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr("app.pipeline.events.publish_event", _noop_publish)

    await beat._sweep_clarifications()
    second = await beat._sweep_clarifications()  # уже FAILED → предикат state не выберет
    assert second == 0
    async with session_scope() as s:
        # Ровно одно событие failed (нет дабл-перехода).
        events = (
            (
                await s.execute(
                    select(JobEvent).where(JobEvent.job_id == jid, JobEvent.event_type == "failed")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1


async def test_sweeper_skips_fresh_clarification(beat_env, monkeypatch):
    jid = await _make_job(JobState.AWAITING_CLARIFICATION, age_seconds=10)

    import app.workers.beat_tasks as beat

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr("app.pipeline.events.publish_event", _noop_publish)

    await beat._sweep_clarifications()
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.AWAITING_CLARIFICATION  # свежую не трогаем


# --- reconciler ---


@pytest.mark.parametrize("state", [JobState.BUILDING, JobState.DEPLOYING, JobState.FIXING])
async def test_reconciler_redispatches_stuck_without_state_change(beat_env, monkeypatch, state):
    settings = get_settings()
    jid = await _make_job(state, age_seconds=settings.stuck_threshold_s + 100)

    import app.workers.beat_tasks as beat

    dispatched: list = []
    monkeypatch.setattr(
        beat,
        "dispatch_for_state",
        lambda jid_, st, **kw: dispatched.append((jid_, st)),
    )

    count = await beat._reconcile_stuck()
    assert count >= 1
    assert (jid, state) in dispatched

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == state  # state НЕ меняется — только ре-диспетчеризация


async def test_reconciler_skips_fresh_job(beat_env, monkeypatch):
    jid = await _make_job(JobState.BUILDING, age_seconds=10)

    import app.workers.beat_tasks as beat

    dispatched: list = []
    monkeypatch.setattr(
        beat,
        "dispatch_for_state",
        lambda jid_, st, **kw: dispatched.append((jid_, st)),
    )

    await beat._reconcile_stuck()
    assert all(d[0] != jid for d in dispatched)  # свежую не трогаем


async def test_reconciler_dispatch_lock_prevents_double_dispatch(beat_env, monkeypatch):
    """Redis dispatch-lock: повторный прогон в окне lock-TTL НЕ дублирует постановку."""
    settings = get_settings()
    jid = await _make_job(JobState.FIXING, age_seconds=settings.stuck_threshold_s + 100)

    import app.workers.beat_tasks as beat

    dispatched: list = []
    monkeypatch.setattr(
        beat,
        "dispatch_for_state",
        lambda jid_, st, **kw: dispatched.append((jid_, st)),
    )

    first = await beat._reconcile_stuck()
    second = await beat._reconcile_stuck()  # lock ещё держится → пропуск
    assert first >= 1
    # Второй прогон не должен повторно поставить эту же джобу (lock busy).
    assert dispatched.count((jid, JobState.FIXING)) == 1
    assert second == 0 or (jid, JobState.FIXING) not in dispatched[first:]
