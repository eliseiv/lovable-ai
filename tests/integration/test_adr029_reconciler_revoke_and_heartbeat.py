"""ADR-029 §C/§watchdog — reconciler revoke на fail-stuck + heartbeat витка без смены state.

Нормативный источник — docs/adr/ADR-029-... §Decision C / §Связь с watchdog,
docs/06-testing-strategy.md §unit «reconciler revoke» + §integration/watchdog «heartbeat»,
app/workers/beat_tasks.py (_reconcile_one зовёт _maybe_revoke_live_task на fail-stuck/wall-clock),
app/workers/tasks.py (_handle_invalid_patch → touch_heartbeat), app/pipeline/dispatcher.py
(dispatch_for_state пишет job_task:{job_id}).

Реальный Postgres + Redis (autonomous_db/session_scope, beat-паттерн). celery revoke и dispatch
постановка тасок мокаются (без брокера).

Покрывает:
- §C integration: fail-stuck LLM-фазной джобы зовёт revoke best-effort по job_task:{job_id};
  промах ключа (нет job_task) НЕ ломает fail-stuck (джоба всё равно FAILED(stuck_timeout));
- dispatch_for_state пишет Redis job_task:{job_id} = task_id поставленной таски (точка для revoke);
- §Связь с watchdog: _handle_invalid_patch (виток agent_output_invalid в FIXING без смены state и
  без retry_count++) двигает last_transition_at (heartbeat) → reconciler НЕ считает живую
  fix-джобу stuck между витками.
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
from app.db.models import GenerationJob, JobEvent, Project, Revision, User
from app.db.session import session_scope, task_engine_scope
from app.pipeline.dispatcher import job_task_key

pytestmark = pytest.mark.asyncio

UID = "u_adr029revoke000000a"


async def _purge() -> None:
    async with session_scope() as s:
        jids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == UID)))
            .scalars()
            .all()
        )
        pids = list(
            set(
                (
                    await s.execute(
                        select(GenerationJob.project_id).where(GenerationJob.user_id == UID)
                    )
                )
                .scalars()
                .all()
            )
        )
        for pid in pids:
            proj = await s.get(Project, pid)
            if proj is not None:
                proj.current_revision_id = None
        await s.flush()
        if jids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(jids)))
            await s.execute(delete(Revision).where(Revision.created_from_job_id.in_(jids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == UID))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == UID))
        await s.commit()


@pytest_asyncio.fixture
async def env(autonomous_db, monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr("app.notify.trigger.enqueue_push_if_significant", lambda *a, **k: None)

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr("app.pipeline.events.publish_event", _noop_publish)
    await _purge()
    async with session_scope() as s:
        s.add(
            User(
                id=UID,
                api_key_hash=hash_api_key("adr029r-key"),
                monthly_budget_usd=Decimal("50.0000"),
                status="active",
            )
        )
        await s.commit()
    # Чистим Redis dispatch/job_task-ключи прошлых прогонов.
    client = aioredis.from_url(get_settings().redis_url)
    try:
        async for key in client.scan_iter("dispatch:*"):
            await client.delete(key)
        async for key in client.scan_iter("job_task:*"):
            await client.delete(key)
    finally:
        await client.aclose()
    yield
    await _purge()


async def _make_job(state: JobState, *, transition_age_seconds: int = 0) -> str:
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
    lta = datetime.now(UTC) - timedelta(seconds=transition_age_seconds)
    async with session_scope() as s:
        await s.execute(
            text("UPDATE generation_jobs SET last_transition_at = :lta WHERE id = :id"),
            {"lta": lta, "id": jid},
        )
        await s.commit()
    return jid


async def _state(jid: str) -> JobState:
    async with session_scope() as s:
        return await s.scalar(select(GenerationJob.state).where(GenerationJob.id == jid))


# ---------------------------------------------------------------------------
# 1. §C integration: fail-stuck зовёт revoke best-effort; промах ключа не ломает fail-stuck.
# ---------------------------------------------------------------------------


async def test_fail_stuck_invokes_revoke_with_task_id(env, monkeypatch):
    """fail-stuck LLM-фазной джобы → revoke(task_id, terminate=True) по job_task:{job_id}.

    Кладём task_id в Redis job_task:{jid}; reconciler терминализирует stuck INTERVIEWING-джобу
    и должен best-effort ревокнуть живую таску. Джоба → FAILED(stuck_timeout).
    """
    settings = get_settings()
    jid = await _make_job(
        JobState.INTERVIEWING, transition_age_seconds=settings.stuck_threshold_s + 100
    )
    # Точка хранения task_id (как dispatch_for_state при постановке таски).
    client = aioredis.from_url(settings.redis_url)
    try:
        await client.set(job_task_key(jid), "live-task-id-1", ex=3600)
    finally:
        await client.aclose()

    import app.workers.beat_tasks as beat

    revoke_calls: list = []
    monkeypatch.setattr(
        beat.celery_app.control, "revoke", lambda tid, **kw: revoke_calls.append((tid, kw))
    )

    async with task_engine_scope() as sm:
        handled = await beat._reconcile_stuck(sm)

    assert handled >= 1
    assert await _state(jid) == JobState.FAILED
    # revoke вызван по сохранённому task_id с terminate=True.
    assert ("live-task-id-1", {"terminate": True, "signal": "SIGTERM"}) in revoke_calls


async def test_fail_stuck_terminalizes_even_without_task_id(env, monkeypatch):
    """Промах job_task-ключа (нет live-таски) НЕ ломает fail-stuck → джоба всё равно FAILED.

    Барьер A держит корректность; revoke — лишь оптимизация. Отсутствие ключа → revoke no-op,
    но терминализация stuck-джобы доходит (FAILED(stuck_timeout), слот освобождён).
    """
    settings = get_settings()
    jid = await _make_job(
        JobState.SPECCING, transition_age_seconds=settings.stuck_threshold_s + 100
    )
    # job_task-ключ НЕ ставим (имитация истёкшего TTL / не было таски).

    import app.workers.beat_tasks as beat

    revoke_calls: list = []
    monkeypatch.setattr(beat.celery_app.control, "revoke", lambda *a, **kw: revoke_calls.append(a))

    async with task_engine_scope() as sm:
        handled = await beat._reconcile_stuck(sm)

    assert handled >= 1
    assert await _state(jid) == JobState.FAILED  # fail-stuck дошёл несмотря на отсутствие revoke
    assert revoke_calls == []  # нечего ревокать — ключа нет


async def test_fail_stuck_survives_revoke_broker_error(env, monkeypatch):
    """Ошибка брокера на revoke → НЕ валит fail-stuck (best-effort §C). Джоба → FAILED."""
    settings = get_settings()
    jid = await _make_job(JobState.CREATED, transition_age_seconds=settings.stuck_threshold_s + 100)
    client = aioredis.from_url(settings.redis_url)
    try:
        await client.set(job_task_key(jid), "task-x", ex=3600)
    finally:
        await client.aclose()

    import app.workers.beat_tasks as beat

    def _boom(tid, **kw):  # noqa: ANN001, ANN202
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(beat.celery_app.control, "revoke", _boom)

    async with task_engine_scope() as sm:
        handled = await beat._reconcile_stuck(sm)

    assert handled >= 1
    assert await _state(jid) == JobState.FAILED  # revoke-сбой не помешал терминализации


# ---------------------------------------------------------------------------
# 2. dispatch_for_state пишет job_task:{job_id} = task_id (точка для revoke).
# ---------------------------------------------------------------------------


async def test_dispatch_records_job_task_id_in_redis(env, monkeypatch):
    """dispatch_for_state(BUILDING) пишет Redis job_task:{job_id} = id поставленной таски."""
    from app.pipeline import dispatcher

    class _FakeResult:
        id = "dispatched-task-id-9"

    class _FakeTask:
        def apply_async(self, *a, **kw):  # noqa: ANN002, ANN003, ANN202
            return _FakeResult()

    import app.workers.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "task_build_request", _FakeTask())

    jid = "j_dispatch_record_1"
    dispatcher.dispatch_for_state(jid, JobState.BUILDING)

    client = aioredis.from_url(get_settings().redis_url)
    try:
        raw = await client.get(job_task_key(jid))
        await client.delete(job_task_key(jid))
    finally:
        await client.aclose()
    assert raw is not None
    assert (raw.decode() if isinstance(raw, bytes) else raw) == "dispatched-task-id-9"


# ---------------------------------------------------------------------------
# 3. §Связь с watchdog: _handle_invalid_patch двигает heartbeat → не stuck между витками.
# ---------------------------------------------------------------------------


async def _make_fixing_job_with_log(transition_age_seconds: int) -> str:
    """FIXING-джоба со старым last_transition_at + failure_log_ref (вход _handle_invalid_patch)."""
    pid = new_project_id()
    jid = new_job_id()
    async with session_scope() as s:
        s.add(Project(id=pid, user_id=UID, prompt="x", title=None))
        s.add(
            GenerationJob(
                id=jid,
                project_id=pid,
                user_id=UID,
                state=JobState.FIXING,
                kind="generation",
                budget_usd=Decimal("5.0000"),
                spend_usd=Decimal("0.0000"),
                failure_log_ref="logs/x/build.log",
                retry_count=1,
            )
        )
        await s.commit()
    lta = datetime.now(UTC) - timedelta(seconds=transition_age_seconds)
    async with session_scope() as s:
        await s.execute(
            text("UPDATE generation_jobs SET last_transition_at = :lta WHERE id = :id"),
            {"lta": lta, "id": jid},
        )
        await s.commit()
    return jid


class _FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_text(self, key, text, content_type="text/plain"):  # noqa: ANN001, ANN202
        self.objects[key] = text.encode("utf-8")
        return key


async def test_handle_invalid_patch_moves_heartbeat_no_state_change(env, monkeypatch):
    """_handle_invalid_patch (виток agent_output_invalid в FIXING) двигает last_transition_at,
    НЕ меняя state и НЕ инкрементируя retry_count.

    Это и есть фикс ложного stuck живой fix-джобы (ADR-029 §Связь с watchdog): heartbeat
    обновляется на distinct failure-event витка, поэтому reconciler НЕ помечает живую джобу
    stuck между витками Agent 4. Проверяем именно сдвиг heartbeat + неизменность state/retry_count.
    """
    import app.workers.tasks as tasks
    from app.schemas.agent_output import AgentOutputError

    # dispatch (переставляет task_fix) — no-op; storage — in-memory (пишет agent.N.log).
    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)
    storage = _FakeStorage()

    jid = await _make_fixing_job_with_log(transition_age_seconds=10_000)
    async with session_scope() as s:
        before_lta = await s.scalar(
            select(GenerationJob.last_transition_at).where(GenerationJob.id == jid)
        )

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        exc = AgentOutputError("patch rejected", signature="agent4_bad_tree")
        await tasks._handle_invalid_patch(s, storage, job, exc, revision=None)

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.state == JobState.FIXING  # state НЕ меняется
        assert job.retry_count == 1  # retry_count НЕ инкрементирован на витке
        assert job.failure_event_pending is True  # помечен для no-progress гарда
        assert job.last_transition_at > before_lta  # heartbeat сдвинут вперёд (НЕ stuck)


async def test_progressing_fix_job_not_failed_stuck_after_heartbeat(env, monkeypatch):
    """После _handle_invalid_patch (heartbeat обновлён) reconciler НЕ помечает джобу stuck.

    End-to-end §Связь с watchdog: джоба была stale (>STUCK_THRESHOLD), но виток сдвинул heartbeat
    на now() → reconciler-проход НЕ терминализирует её (живая прогрессирующая, не мёртвая).
    """
    import app.workers.beat_tasks as beat
    import app.workers.tasks as tasks
    from app.schemas.agent_output import AgentOutputError

    monkeypatch.setattr(tasks, "dispatch_for_state", lambda *a, **k: None)
    monkeypatch.setattr(beat, "dispatch_for_state", lambda *a, **k: None)
    storage = _FakeStorage()

    settings = get_settings()
    jid = await _make_fixing_job_with_log(transition_age_seconds=settings.stuck_threshold_s + 100)

    # Виток agent_output_invalid двигает heartbeat на now().
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        await tasks._handle_invalid_patch(
            s, storage, job, AgentOutputError("x", signature="sig"), revision=None
        )

    # Reconciler-проход: джоба уже НЕ stale (heartbeat свежий) → НЕ терминализируется как stuck.
    async with task_engine_scope() as sm:
        await beat._reconcile_stuck(sm)

    assert await _state(jid) == JobState.FIXING  # живая fix-джоба НЕ получила ложный FAILED
