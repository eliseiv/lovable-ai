"""Integration: beat-периодика sweeper + reconciler (docs §E, ADR-019).

Реальный Postgres + реальный Redis (dispatch-lock). dispatch_for_state — спай.

Покрывает (нормативный источник — docs/06-testing-strategy.md §Integration «Sweeper +
reconciler», «Reconciler — все активные состояния + concurrency-leak», ADR-019):
- sweeper: AWAITING_CLARIFICATION старше TTL → FAILED(clarification_timeout);
  идемпотентно (повтор не трогает уже-FAILED); свежую джобу не трогает;
- reconciler ветвь (1) resumable: stuck BUILDING/DEPLOYING/FIXING старше STUCK_THRESHOLD_S →
  ре-диспетчеризация по текущему state (state НЕ меняется);
- reconciler ветвь (2) LLM-фаза: stuck CREATED/INTERVIEWING/SPECCING → FAILED(stuck_timeout),
  concurrency-слот освобождён;
- AWAITING_CLARIFICATION reconciler'ом НЕ трогается (его экспайрит только sweeper);
- stuck-критерий — last_transition_at (НЕ updated_at): свежий updated_at (cost-ledger) при
  старом last_transition_at → джоба всё равно stuck;
- TOCTOU: если джоба продвинулась (last_transition_at обновился) между снимком кандидатов и
  терминализацией — reconciler делает no-op;
- last_transition_at обновляется при КАЖДОЙ смене state (transition);
- свежую (не stuck) джобу reconciler не трогает;
- Redis dispatch-lock: повторный прогон reconciler в окне TTL lock'а НЕ дублирует постановку.

Сигнатуры прод-кода (ADR-019, observability §7): `_sweep_clarifications(sessionmaker)` и
`_reconcile_stuck(sessionmaker)` принимают per-task sessionmaker. Тесты прогоняют их через
`task_engine_scope()` — per-task async-engine в текущем loop (как Celery-обёртки
sweep_clarifications/reconcile_stuck).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select, text

from app.auth.concurrency import count_active_jobs
from app.core.config import get_settings
from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, Project, User
from app.db.session import session_scope, task_engine_scope

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


async def _make_job(
    state: JobState,
    *,
    age_seconds: int = 0,
    transition_age_seconds: int | None = None,
) -> str:
    """Создаёт джобу в state.

    age_seconds → updated_at = now - age_seconds (для sweeper TTL-предиката, он смотрит
    updated_at). transition_age_seconds → last_transition_at = now - transition_age_seconds
    (для reconciler stuck-предиката по heartbeat прогресса, ADR-019). Если
    transition_age_seconds не задан — last_transition_at = updated_at (как backfill миграции).
    """
    if transition_age_seconds is None:
        transition_age_seconds = age_seconds
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
    # Принудительно состарить updated_at И last_transition_at напрямую (onupdate/transition
    # перебили бы значения при ORM-апдейте). Reconciler смотрит last_transition_at, sweeper —
    # updated_at; задаём оба независимо для проверки, что reconciler НЕ ведётся на updated_at.
    now = datetime.now(UTC)
    upd = now - timedelta(seconds=age_seconds)
    lta = now - timedelta(seconds=transition_age_seconds)
    async with session_scope() as s:
        await s.execute(
            text(
                "UPDATE generation_jobs SET updated_at = :upd, last_transition_at = :lta "
                "WHERE id = :id"
            ),
            {"upd": upd, "lta": lta, "id": jid},
        )
        await s.commit()
    return jid


async def _get_last_transition_at(jid: str) -> datetime:
    async with session_scope() as s:
        return await s.scalar(
            select(GenerationJob.last_transition_at).where(GenerationJob.id == jid)
        )


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


def _noop_publish_patch(monkeypatch) -> None:  # noqa: ANN001
    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr("app.pipeline.events.publish_event", _noop_publish)
    # APNs trigger дёргается transition'ом при FAILED — нейтрализуем (нет Celery-брокера).
    monkeypatch.setattr(
        "app.notify.trigger.enqueue_push_if_significant", lambda *a, **k: None
    )


# --- sweeper ---


async def test_sweeper_expires_old_clarification(beat_env, monkeypatch):
    settings = get_settings()
    _noop_publish_patch(monkeypatch)
    # Старше TTL (7 дней): возраст = TTL + запас.
    jid = await _make_job(
        JobState.AWAITING_CLARIFICATION, age_seconds=settings.clarification_ttl_s + 100
    )

    import app.workers.beat_tasks as beat

    async with task_engine_scope() as sm:
        swept = await beat._sweep_clarifications(sm)
    assert swept >= 1

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert job.failure_reason == "clarification_timeout"


async def test_sweeper_idempotent_no_double_fail(beat_env, monkeypatch):
    settings = get_settings()
    _noop_publish_patch(monkeypatch)
    jid = await _make_job(
        JobState.AWAITING_CLARIFICATION, age_seconds=settings.clarification_ttl_s + 100
    )

    import app.workers.beat_tasks as beat

    async with task_engine_scope() as sm:
        await beat._sweep_clarifications(sm)
    async with task_engine_scope() as sm:
        second = await beat._sweep_clarifications(sm)  # уже FAILED → предикат state не выберет
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
    _noop_publish_patch(monkeypatch)
    jid = await _make_job(JobState.AWAITING_CLARIFICATION, age_seconds=10)

    import app.workers.beat_tasks as beat

    async with task_engine_scope() as sm:
        await beat._sweep_clarifications(sm)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.AWAITING_CLARIFICATION  # свежую не трогаем


# --- reconciler: ветвь (1) resumable — ре-диспетчеризация без смены state ---


@pytest.mark.parametrize("state", [JobState.BUILDING, JobState.DEPLOYING, JobState.FIXING])
async def test_reconciler_redispatches_stuck_without_state_change(beat_env, monkeypatch, state):
    settings = get_settings()
    _noop_publish_patch(monkeypatch)
    jid = await _make_job(state, transition_age_seconds=settings.stuck_threshold_s + 100)

    import app.workers.beat_tasks as beat

    dispatched: list = []
    monkeypatch.setattr(
        beat,
        "dispatch_for_state",
        lambda jid_, st, **kw: dispatched.append((jid_, st)),
    )

    async with task_engine_scope() as sm:
        count = await beat._reconcile_stuck(sm)
    assert count >= 1
    assert (jid, state) in dispatched

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == state  # state НЕ меняется — только ре-диспетчеризация


# --- reconciler: ветвь (2) LLM-фаза — fail-stuck → FAILED(stuck_timeout) ---


@pytest.mark.parametrize(
    "state",
    [JobState.CREATED, JobState.INTERVIEWING, JobState.SPECCING],
)
async def test_reconciler_fail_stuck_llm_phase(beat_env, monkeypatch, state):
    """LLM-фаза CREATED/INTERVIEWING/SPECCING застряла → FAILED(stuck_timeout), state меняется."""
    settings = get_settings()
    _noop_publish_patch(monkeypatch)
    jid = await _make_job(state, transition_age_seconds=settings.stuck_threshold_s + 100)

    import app.workers.beat_tasks as beat

    dispatched: list = []
    monkeypatch.setattr(
        beat,
        "dispatch_for_state",
        lambda jid_, st, **kw: dispatched.append((jid_, st)),
    )

    async with task_engine_scope() as sm:
        count = await beat._reconcile_stuck(sm)
    assert count >= 1
    # LLM-фаза НЕ ре-диспетчеризуется — терминализуется.
    assert all(d[0] != jid for d in dispatched)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert job.failure_reason == "stuck_timeout"


async def test_reconciler_fail_stuck_releases_concurrency_slot(beat_env, monkeypatch):
    """После fail-stuck LLM-фазной джобы concurrency-слот юзера освобождён (ADR-019 §A)."""
    settings = get_settings()
    _noop_publish_patch(monkeypatch)
    jid = await _make_job(
        JobState.INTERVIEWING, transition_age_seconds=settings.stuck_threshold_s + 100
    )

    # До reconciler: джоба активна (держит слот).
    async with session_scope() as s:
        assert await count_active_jobs(s, UID) == 1

    import app.workers.beat_tasks as beat

    monkeypatch.setattr(beat, "dispatch_for_state", lambda *a, **k: None)
    async with task_engine_scope() as sm:
        await beat._reconcile_stuck(sm)

    # После fail-stuck: FAILED ∈ PAUSED_STATES → активных джоб 0 (слот свободен).
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert await count_active_jobs(s, UID) == 0


async def test_reconciler_ignores_awaiting_clarification(beat_env, monkeypatch):
    """AWAITING_CLARIFICATION reconciler'ом НЕ трогается (свой TTL-sweeper, ADR-019)."""
    settings = get_settings()
    _noop_publish_patch(monkeypatch)
    jid = await _make_job(
        JobState.AWAITING_CLARIFICATION,
        transition_age_seconds=settings.stuck_threshold_s + 100,
    )

    import app.workers.beat_tasks as beat

    monkeypatch.setattr(beat, "dispatch_for_state", lambda *a, **k: None)
    async with task_engine_scope() as sm:
        handled = await beat._reconcile_stuck(sm)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.AWAITING_CLARIFICATION  # reconciler не трогает
    assert handled == 0


# --- reconciler: heartbeat по last_transition_at, НЕ updated_at ---


async def test_reconciler_uses_last_transition_at_not_updated_at(beat_env, monkeypatch):
    """Свежий updated_at (cost-ledger) при старом last_transition_at → джоба всё равно stuck."""
    settings = get_settings()
    _noop_publish_patch(monkeypatch)
    # updated_at свежий (10 c назад — будто cost-ledger дёрнул строку), но last_transition_at
    # старый (heartbeat прогресса) → reconciler ДОЛЖЕН детектить stuck по last_transition_at.
    jid = await _make_job(
        JobState.SPECCING,
        age_seconds=10,
        transition_age_seconds=settings.stuck_threshold_s + 100,
    )

    import app.workers.beat_tasks as beat

    monkeypatch.setattr(beat, "dispatch_for_state", lambda *a, **k: None)
    async with task_engine_scope() as sm:
        handled = await beat._reconcile_stuck(sm)
    assert handled >= 1
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FAILED
        assert job.failure_reason == "stuck_timeout"


async def test_transition_updates_last_transition_at(beat_env, monkeypatch):
    """last_transition_at обновляется при КАЖДОЙ смене state (ADR-019 heartbeat)."""
    _noop_publish_patch(monkeypatch)
    jid = await _make_job(JobState.CREATED, transition_age_seconds=1000)
    before = await _get_last_transition_at(jid)

    from app.pipeline.events import transition

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        await transition(s, job, JobState.INTERVIEWING)
    after = await _get_last_transition_at(jid)
    assert after > before  # heartbeat сдвинут вперёд при смене state


# --- reconciler: TOCTOU — джоба продвинулась между снимком и терминализацией → no-op ---


async def test_reconciler_toctou_noop_when_job_progressed(beat_env, monkeypatch):
    """Если реальная таска продвинула джобу (свежий last_transition_at) между снимком
    кандидатов и пер-джоба транзакцией — reconciler делает no-op (ре-проверка stuck-инварианта).
    """
    settings = get_settings()
    _noop_publish_patch(monkeypatch)
    jid = await _make_job(
        JobState.INTERVIEWING, transition_age_seconds=settings.stuck_threshold_s + 100
    )

    import app.workers.beat_tasks as beat

    dispatched: list = []
    monkeypatch.setattr(
        beat, "dispatch_for_state", lambda jid_, st, **kw: dispatched.append((jid_, st))
    )

    original_reconcile_one = beat._reconcile_one

    async def _reconcile_one_after_progress(sessionmaker, client, job_id, now, cutoff):  # noqa: ANN001, ANN202
        # Симулируем acks_late-таску: между снимком кандидатов и пер-джоба транзакцией джоба
        # продвинулась (last_transition_at стал свежим) → ре-проверка stuck-инварианта в
        # _reconcile_one должна вернуть False (no-op).
        if job_id == jid:
            async with session_scope() as s:
                await s.execute(
                    text("UPDATE generation_jobs SET last_transition_at = now() WHERE id = :id"),
                    {"id": jid},
                )
                await s.commit()
        return await original_reconcile_one(sessionmaker, client, job_id, now, cutoff)

    monkeypatch.setattr(beat, "_reconcile_one", _reconcile_one_after_progress)

    async with task_engine_scope() as sm:
        handled = await beat._reconcile_stuck(sm)

    # Джоба продвинулась → reconciler не терминализировал и не ре-диспетчеризовал.
    assert handled == 0
    assert all(d[0] != jid for d in dispatched)
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.INTERVIEWING  # state НЕ изменён reconciler'ом


# --- reconciler: свежую джобу не трогает ---


async def test_reconciler_skips_fresh_job(beat_env, monkeypatch):
    _noop_publish_patch(monkeypatch)
    jid = await _make_job(JobState.BUILDING, transition_age_seconds=10)

    import app.workers.beat_tasks as beat

    dispatched: list = []
    monkeypatch.setattr(
        beat,
        "dispatch_for_state",
        lambda jid_, st, **kw: dispatched.append((jid_, st)),
    )

    async with task_engine_scope() as sm:
        await beat._reconcile_stuck(sm)
    assert all(d[0] != jid for d in dispatched)  # свежую не трогаем


# --- reconciler: Redis dispatch-lock против двойной постановки ---


async def test_reconciler_dispatch_lock_prevents_double_dispatch(beat_env, monkeypatch):
    """Redis dispatch-lock: повторный прогон в окне lock-TTL НЕ дублирует постановку."""
    settings = get_settings()
    _noop_publish_patch(monkeypatch)
    jid = await _make_job(
        JobState.FIXING, transition_age_seconds=settings.stuck_threshold_s + 100
    )

    import app.workers.beat_tasks as beat

    dispatched: list = []
    monkeypatch.setattr(
        beat,
        "dispatch_for_state",
        lambda jid_, st, **kw: dispatched.append((jid_, st)),
    )

    async with task_engine_scope() as sm:
        first = await beat._reconcile_stuck(sm)
    async with task_engine_scope() as sm:
        second = await beat._reconcile_stuck(sm)  # lock ещё держится → пропуск
    assert first >= 1
    # Второй прогон не должен повторно поставить эту же джобу (lock busy).
    assert dispatched.count((jid, JobState.FIXING)) == 1
    assert second == 0 or (jid, JobState.FIXING) not in dispatched[first:]
