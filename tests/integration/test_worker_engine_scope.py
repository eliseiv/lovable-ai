"""Integration A2: per-task async-engine для sync Celery-задач (observability §7, ADR-019).

Реальный Postgres (asyncpg). Нормативный источник — docs/06-testing-strategy.md §Integration
«Async-код из синхронной Celery-задачи (прод-фикс баг A2)», observability §7,
app/db/session.py (worker_engine_scope/task_engine_scope), app/observability/collector.py.

Прод-баг A2: sync Celery-задача исполняет async-код через asyncio.run (новый loop на каждый
вызов); глобальный модуль-уровневый async-engine (привязан к прежнему loop) при
переиспользовании из второй задачи роняет `RuntimeError: Future attached to a different loop`
(asyncpg биндит Future/соединение к loop). Фикс: каждая задача создаёт/dispose'ит per-task
engine ВНУТРИ своего asyncio.run-loop (task_engine_scope / worker_engine_scope).

Покрывает:
- ≥2 прогона metrics.refresh (refresh_all) в одном процессе без RuntimeError, gauge наполнены;
- ≥2 прогона + агент-/async-Celery-задачи (run_agent_task, reconcile_stuck) подряд в одном
  процессе без RuntimeError: Future attached to a different loop;
- FastAPI-путь (session_scope без активного ContextVar) использует ГЛОБАЛЬНЫЙ engine.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, User
from app.db.session import session_scope, worker_engine_scope

UID = "u_a2owner0000000000000a"


def _run_sync(coro_factory):  # noqa: ANN001, ANN202
    """Прогон корутины в СВЕЖЕМ asyncio.run-loop в отдельном потоке (модель Celery-задачи).

    Каждый вызов = одна синхронная Celery-задача в воркер-процессе: свой asyncio.run, свой loop.
    Поток без активного loop'а позволяет вложенный asyncio.run (как реальное sync-тело таски).
    """
    import threading

    result: dict = {}

    def _worker() -> None:
        try:
            result["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 — пробрасываем в основной поток для assert
            result["error"] = exc

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


async def _setup() -> None:
    # per-call engine (как Celery-задача): иначе глобальный engine на чужом loop _run_sync.
    async with worker_engine_scope(), session_scope() as s:
        if await s.get(User, UID) is None:
            s.add(
                User(
                    id=UID,
                    api_key_hash=hash_api_key("a2-key"),
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
    _run_sync(_purge)
    _run_sync(_setup)

    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr("app.pipeline.events.publish_event", _noop_publish)
    monkeypatch.setattr("app.notify.trigger.enqueue_push_if_significant", lambda *a, **k: None)
    yield
    _run_sync(_purge)


# --- metrics.refresh (refresh_all) ≥2 прогона в одном процессе ---


def test_metrics_refresh_twice_in_one_process(_env):
    """≥2 прогона refresh_all (metrics.refresh) подряд в одном процессе → нет RuntimeError."""
    from app.observability.collector import refresh_all

    # Первый прогон: создаёт per-task engine (task_engine_scope) в своём loop и dispose'ит.
    _run_sync(refresh_all)
    # Второй прогон в ЭТОМ ЖЕ процессе: НОВЫЙ asyncio.run-loop. Если бы collector переиспользовал
    # глобальный engine (привязанный к loop'у первого прогона) → RuntimeError: Future attached
    # to a different loop. Per-task engine (фикс A2) → проходит.
    _run_sync(refresh_all)


def test_metrics_refresh_populates_gauge_each_run(_env):
    """jobs_in_state gauge наполняется на каждом прогоне (есть джоба → gauge>0)."""
    import app.workers.beat_tasks  # noqa: F401 — регистрация (косвенно тянет метрики)
    from app.observability import metrics
    from app.observability.collector import refresh_all

    _run_sync(lambda: _make_job(JobState.BUILDING))
    _run_sync(refresh_all)
    val1 = metrics.jobs_in_state.labels(state="BUILDING", kind="generation")._value.get()
    assert val1 >= 1
    # Второй прогон в новом loop'е — снова наполняет (нет RuntimeError, gauge консистентен).
    _run_sync(refresh_all)
    val2 = metrics.jobs_in_state.labels(state="BUILDING", kind="generation")._value.get()
    assert val2 >= 1


# --- metrics.refresh + агент-/async-Celery-задача подряд в одном процессе ---


def test_refresh_then_agent_task_no_cross_loop_error(_env):
    """metrics.refresh, затем агент-таска (run_agent_task), затем reconciler — один процесс,
    разные asyncio.run-loop'ы — без RuntimeError: Future attached to a different loop (A2).
    """
    from types import SimpleNamespace

    import httpx
    from anthropic import AuthenticationError

    from app.observability.collector import refresh_all
    from app.pipeline.graceful_fail import run_agent_task

    jid = _run_sync(lambda: _make_job(JobState.INTERVIEWING))

    # 1) async-Celery-задача metrics.refresh (per-task engine #1).
    _run_sync(refresh_all)

    # 2) агент-таска: graceful-fail на 401 (per-task engine #2 внутри своего asyncio.run в потоке).
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    async def _body():  # noqa: ANN202
        raise AuthenticationError("no key", response=httpx.Response(401, request=req), body=None)

    task = SimpleNamespace(request=SimpleNamespace(retries=0))
    # run_agent_task — sync, сам зовёт asyncio.run: запускаем в отдельном потоке (свой loop).
    import threading

    err: dict = {}

    def _w() -> None:
        try:
            run_agent_task(task, _body, jid)
        except BaseException as exc:  # noqa: BLE001
            err["e"] = exc

    t = threading.Thread(target=_w)
    t.start()
    t.join()
    assert "e" not in err, f"agent task raised cross-loop/other error: {err.get('e')!r}"

    # 3) снова metrics.refresh (per-task engine #3) — третий loop в одном процессе.
    _run_sync(refresh_all)

    # Джоба терминализирована агент-таской (per-task engine отработал в своём loop).
    async def _check():  # noqa: ANN202
        async with worker_engine_scope(), session_scope() as s:
            job = await s.get(GenerationJob, jid)
            return job.state, job.failure_reason

    state, reason = _run_sync(_check)
    assert state == JobState.FAILED
    assert reason == "agent_unavailable"


def test_reconcile_stuck_twice_in_one_process(_env):
    """reconcile_stuck (async-Celery-задача через task_engine_scope) ≥2 прогона в одном процессе."""
    import app.workers.beat_tasks as beat
    from app.db.session import task_engine_scope

    async def _run_once():  # noqa: ANN202
        async with task_engine_scope() as sm:
            return await beat._reconcile_stuck(sm)

    _run_sync(_run_once)
    _run_sync(_run_once)  # второй loop в одном процессе — без cross-loop RuntimeError


# --- FastAPI-путь: session_scope без активного ContextVar → ГЛОБАЛЬНЫЙ engine ---


def test_session_scope_uses_global_engine_without_contextvar(_env):
    """Вне Celery-задачи (ContextVar не установлен) session_scope биндится к ГЛОБАЛЬНОМУ engine."""
    import app.db.session as db_session

    async def _check():  # noqa: ANN202
        # ContextVar пуст (нет worker_engine_scope) → session_scope берёт глобальный sessionmaker.
        assert db_session._task_sessionmaker.get() is None
        global_engine = db_session.get_engine()
        async with session_scope() as s:
            assert s.bind is global_engine  # сессия на глобальном engine (FastAPI-путь)
        return True

    assert _run_sync(_check) is True


def test_worker_engine_scope_binds_per_task_engine(_env):
    """Внутри worker_engine_scope session_scope биндится к per-task engine (НЕ глобальному)."""
    import app.db.session as db_session
    from app.db.session import worker_engine_scope

    async def _check():  # noqa: ANN202
        global_engine = db_session.get_engine()
        async with worker_engine_scope():
            assert db_session._task_sessionmaker.get() is not None
            async with session_scope() as s:
                assert s.bind is not global_engine  # per-task engine, не глобальный
        # После выхода ContextVar сброшен.
        assert db_session._task_sessionmaker.get() is None
        return True

    assert _run_sync(_check) is True
