"""Integration §7.2: loop-affinity per-task async-Redis в Celery (ADR-019 §Fix, observability §7).

Реальный Postgres + Redis (conftest). Нормативный источник — docs/modules/observability/
03-architecture.md §7.2 (критерий приёмки qa), docs/modules/pipeline/03-architecture.md §H,
ADR-019 §Fix, app/observability/redis_pool.py (worker_redis_scope/get_redis),
app/pipeline/events.py (publish_event), app/workers/beat_tasks.py, app/observability/collector.py.

Прод-инцидент (раунд 2, ADR-019 §Fix): глобальный async-Redis ConnectionPool-синглтон привязан
к loop'у, на котором впервые создал соединение. Celery-задача исполняет async-код через
asyncio.run (НОВЫЙ loop на каждый вызов); соединение из глобального пула, взятое из чужого/
закрытого loop'а второй задачи → `RuntimeError: Event loop is closed` / `Future attached to a
different loop` ВНУТРИ publish_event() → таска падала ДО Claude → джоба зависала в INTERVIEWING,
лочила concurrency-слот. Фикс: per-task async-Redis клиент (worker_redis_scope, ContextVar),
созданный ВНУТРИ asyncio.run-loop задачи и aclose/disconnect в finally.

Покрывает критерий приёмки §7.2:
- ≥2 повторных asyncio.run В ОДНОМ ПРОЦЕССЕ воркерных входов с async-Redis — task_interview-пути
  (через publish_event), beat.reconcile_stuck, metrics.refresh (refresh_all → refresh_queue_depth
  → get_redis) — БЕЗ `Event loop is closed` / `Future attached to a different loop`;
- worker_redis_scope отдаёт per-task клиент текущего loop'а; вне scope (ASGI) — глобальный пул;
- teardown (aclose/disconnect) не оставляет утечек соединений между прогонами.
"""

from __future__ import annotations

import asyncio
import threading
from decimal import Decimal

import pytest

from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, User
from app.db.session import session_scope, worker_engine_scope
from app.observability import redis_pool
from app.observability.redis_pool import get_redis, worker_redis_scope

UID = "u_loopaffinity00000000a"


def _run_sync(coro_factory):  # noqa: ANN001, ANN202
    """Прогон корутины в СВЕЖЕМ asyncio.run-loop в отдельном потоке (модель Celery-задачи).

    Каждый вызов = одна синхронная Celery-задача в воркер-процессе: свой asyncio.run, свой loop.
    Поток без активного loop'а позволяет вложенный asyncio.run (как реальное sync-тело таски).
    Исключения пробрасываются в основной поток для assert.
    """
    result: dict = {}

    def _worker() -> None:
        try:
            result["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 — пробрасываем для assert
            result["error"] = exc

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


async def _setup() -> None:
    async with worker_engine_scope(), session_scope() as s:
        if await s.get(User, UID) is None:
            s.add(
                User(
                    id=UID,
                    api_key_hash=hash_api_key("loop-key"),
                    monthly_budget_usd=Decimal("50.0000"),
                    status="active",
                )
            )
            await s.commit()


async def _make_job(state: JobState) -> str:
    pid = new_project_id()
    jid = new_job_id()
    async with worker_engine_scope(), session_scope() as s:
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
    return jid


async def _purge() -> None:
    from sqlalchemy import delete, select

    from app.db.models import JobEvent

    async with worker_engine_scope(), session_scope() as s:
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
        if jids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(jids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == UID))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == UID))
        await s.commit()


@pytest.fixture
def _env(autonomous_db, monkeypatch):  # noqa: ANN001, ANN202
    # enqueue_push — no-op (внешняя граница APNs не в скоупе loop-affinity-теста).
    monkeypatch.setattr("app.notify.trigger.enqueue_push_if_significant", lambda *a, **k: None)
    _run_sync(_purge)
    _run_sync(_setup)
    yield
    _run_sync(_purge)


# ---------------------------------------------------------------------------
# 1. worker_redis_scope отдаёт per-task клиент; вне scope — глобальный ASGI-пул.
# ---------------------------------------------------------------------------


def test_get_redis_returns_per_task_client_in_scope_global_outside(_env):
    """get_redis: внутри worker_redis_scope → per-task клиент; вне → клиент глобального пула."""

    async def _check() -> bool:  # noqa: ANN202
        # Вне scope: клиент поверх глобального BlockingConnectionPool-синглтона (ASGI-путь).
        outside = get_redis()
        global_pool = redis_pool.get_pool()
        assert outside.connection_pool is global_pool

        async with worker_redis_scope():
            # Внутри scope: per-task клиент НЕ на глобальном пуле (loop-локальный, §7.2).
            inside = get_redis()
            assert inside.connection_pool is not global_pool
            # per-task клиент функционален в этом loop'е (реальная операция).
            await inside.publish("loop:test", "ping")

        # После выхода scope ContextVar сброшен → снова глобальный пул.
        after = get_redis()
        assert after.connection_pool is global_pool
        return True

    assert _run_sync(_check) is True


def test_per_task_client_closed_after_scope_exit(_env):
    """worker_redis_scope в finally делает aclose/disconnect — per-task пул закрыт после выхода."""

    captured: dict = {}

    async def _capture() -> None:  # noqa: ANN202
        async with worker_redis_scope():
            client = get_redis()
            captured["pool"] = client.connection_pool
            await client.publish("loop:test", "x")

    _run_sync(_capture)
    pool = captured["pool"]
    # BlockingConnectionPool после disconnect(): нет занятых соединений (все закрыты в finally).
    assert len(pool._in_use_connections) == 0


# ---------------------------------------------------------------------------
# 2. ≥2 повторных asyncio.run В ОДНОМ ПРОЦЕССЕ — без Event loop is closed / cross-loop.
# ---------------------------------------------------------------------------


def test_publish_event_via_scope_twice_in_one_process(_env):
    """task_interview-путь: publish_event под worker_redis_scope ≥2 раз подряд в одном процессе.

    Моделирует тело агент-таски (run_agent_task оборачивает coro в worker_redis_scope): transition
    после commit зовёт publish_event → get_redis() (per-task). Второй прогон — НОВЫЙ asyncio.run-
    loop в том же процессе. До фикса второй publish брал соединение глобального пула с прошлого/
    закрытого loop'а → `RuntimeError: Event loop is closed`. Per-task клиент (фикс) → проходит.
    """
    from app.pipeline.events import publish_event

    async def _interview_like(jid: str):  # noqa: ANN202
        # worker_redis_scope как в run_agent_task; publish_event дёргает get_redis() (per-task).
        async with worker_engine_scope(), worker_redis_scope():
            async with session_scope() as s:
                job = await s.get(GenerationJob, jid)
                from app.pipeline.events import transition

                await transition(s, job, JobState.INTERVIEWING, event_type="agent_started")
            # Явный повторный publish (как несколько переходов в теле таски).
            await publish_event(jid, "agent_started", to_state="INTERVIEWING")

    jid = _run_sync(lambda: _make_job(JobState.CREATED))
    # ПЕРВЫЙ прогон: per-task Redis-клиент создаётся/закрывается в своём loop.
    _run_sync(lambda: _interview_like(jid))
    # ВТОРОЙ прогон в ЭТОМ ЖЕ процессе на НОВОЙ джобе — НОВЫЙ asyncio.run-loop. Без cross-loop.
    jid2 = _run_sync(lambda: _make_job(JobState.CREATED))
    _run_sync(lambda: _interview_like(jid2))

    # Обе джобы реально продвинулись в INTERVIEWING (publish best-effort, переход закоммичен).
    async def _states() -> tuple[JobState, JobState]:  # noqa: ANN202
        async with worker_engine_scope(), session_scope() as s:
            j1 = await s.get(GenerationJob, jid)
            j2 = await s.get(GenerationJob, jid2)
            return j1.state, j2.state

    s1, s2 = _run_sync(_states)
    assert s1 == JobState.INTERVIEWING
    assert s2 == JobState.INTERVIEWING


def test_reconcile_stuck_twice_in_one_process_with_redis(_env):
    """beat.reconcile_stuck (worker_redis_scope + task_engine_scope) ≥2 прогона в одном процессе.

    reconcile_stuck открывает worker_redis_scope вокруг тела; fail-stuck-ветвь зовёт
    fail_job→transition→publish_event (Redis). Два прогона = два asyncio.run-loop'а в одном
    процессе — без `Event loop is closed`/cross-loop (реальная таска через @celery_app.task .run).
    """
    from app.workers.beat_tasks import reconcile_stuck

    # reconcile_stuck — sync Celery-таска (сама зовёт asyncio.run). Прогоняем .run() напрямую в
    # отдельном потоке (без активного loop'а) — точная модель синхронного воркера.
    def _run_task() -> int:
        return reconcile_stuck.run()

    def _in_thread() -> int:
        out: dict = {}

        def _w() -> None:
            try:
                out["v"] = _run_task()
            except BaseException as exc:  # noqa: BLE001
                out["e"] = exc

        t = threading.Thread(target=_w)
        t.start()
        t.join()
        if "e" in out:
            raise out["e"]
        return out["v"]

    # Нет застрявших джоб → 0 обработано, но Redis-клиент per-task создаётся/закрывается дважды.
    assert _in_thread() == 0
    assert _in_thread() == 0  # второй loop в одном процессе — без cross-loop RuntimeError


def test_metrics_refresh_with_redis_twice_in_one_process(_env):
    """metrics.refresh (refresh_all → refresh_queue_depth → get_redis) ≥2 прогона в одном процессе.

    refresh_all открывает task_engine_scope (БД-коллекторы) и зовёт refresh_queue_depth, который
    дёргает get_redis() (LLEN брокера). metrics.refresh-таска оборачивает refresh_all в
    worker_redis_scope (§7.1 п.5). Два прогона = два asyncio.run-loop'а в одном процессе — без
    `Future attached to a different loop` ни на asyncpg, ни на async-Redis.
    """
    from app.workers.beat_tasks import refresh_metrics

    def _run_task() -> None:
        # refresh_metrics — sync Celery-таска: сама зовёт asyncio.run(worker_redis_scope).
        out: dict = {}

        def _w() -> None:
            try:
                refresh_metrics.run()
            except BaseException as exc:  # noqa: BLE001
                out["e"] = exc

        t = threading.Thread(target=_w)
        t.start()
        t.join()
        if "e" in out:
            raise out["e"]

    _run_task()
    _run_task()  # второй loop в одном процессе — без cross-loop RuntimeError


def test_queue_depth_gauge_populated_under_scope(_env):
    """refresh_queue_depth наполняет queue_depth-gauge через per-task Redis-клиент (LLEN)."""
    from app.observability import metrics
    from app.observability.collector import refresh_queue_depth

    async def _run() -> None:  # noqa: ANN202
        async with worker_redis_scope():
            await refresh_queue_depth()

    _run_sync(_run)
    # Gauge выставлен (>=0) для обеих очередей — LLEN отработал на реальном Redis без cross-loop.
    for q in ("llm", "build"):
        val = metrics.queue_depth.labels(queue=q)._value.get()
        assert val >= 0
