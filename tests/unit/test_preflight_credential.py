"""Unit: per-job fail-fast preflight LLM-credential (ADR-019 §Fix round 3 п.1, docs §G).

Нормативный источник — docs/modules/pipeline/03-architecture.md §G «Fail-fast preflight
LLM-credential», docs/06-testing-strategy.md (критерий round 3, unit preflight),
app/pipeline/graceful_fail.py (run_agent_task), app/workers/retry_policy.py
(llm_credential_present).

Покрывает критерий round 3 (unit preflight):
- run_agent_task(requires_llm=True) при пустой/whitespace-only Settings.anthropic_api_key →
  FAILED(agent_unavailable), тело coro_factory НЕ исполнилось (SDK не вызван), Celery-retry
  НЕ инкрементнут (исключение НЕ проброшено → Celery не ретраит);
- requires_llm=False (build/deploy) → preflight НЕ применяется (тело исполняется), даже при
  пустом ключе;
- непустой ключ + requires_llm=True → preflight пропускает, тело исполняется;
- llm_credential_present: контракт пустой/whitespace/None → False, непустой → True.

Терминализация (_graceful_fail_job) идёт через РЕАЛЬНЫЙ Postgres + Redis (per-task scope в
run_agent_task) — это единый транзакционный путь graceful-fail (§G), не мокается. _run_*
гоняют sync run_agent_task в отдельном потоке (у него свой asyncio.run, как у Celery-воркера).
"""

from __future__ import annotations

import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.auth.concurrency import count_active_jobs
from app.core.config import get_settings
from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, User
from app.db.session import session_scope, worker_engine_scope
from app.pipeline.graceful_fail import run_agent_task
from app.workers.retry_policy import llm_credential_present

UID = "u_preflight0000000000a"


# --- llm_credential_present: чистый предикат (без I/O) ---


@pytest.mark.parametrize("value", [None, "", " ", "\t", "\n", "   \t\n  "])
def test_llm_credential_present_false_for_empty_or_whitespace(value):
    assert llm_credential_present(value) is False


@pytest.mark.parametrize("value", ["sk-ant-xxx", " sk-ant-xxx ", "x"])
def test_llm_credential_present_true_for_nonempty(value):
    assert llm_credential_present(value) is True


# --- preflight в run_agent_task (реальный Postgres/Redis для graceful-fail) ---


def _fake_task(retries: int = 0):  # noqa: ANN202
    """Минимальный bound Celery-таск-стаб: только task.request.retries (что читает §G)."""
    return SimpleNamespace(request=SimpleNamespace(retries=retries))


def _run_sync(coro_factory):  # noqa: ANN001, ANN202
    """Прогон корутины в свежем asyncio.run-loop в отдельном потоке (модель Celery-задачи)."""
    import asyncio

    result: dict = {}

    def _w() -> None:
        try:
            result["v"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001
            result["e"] = exc

    t = threading.Thread(target=_w)
    t.start()
    t.join()
    if "e" in result:
        raise result["e"]
    return result.get("v")


def _run_task_in_thread(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
    """Прогон sync run_agent_task в отдельном потоке (у него свой asyncio.run, как у Celery)."""
    out: dict = {}

    def _w() -> None:
        try:
            out["v"] = run_agent_task(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            out["e"] = exc

    t = threading.Thread(target=_w)
    t.start()
    t.join()
    if "e" in out:
        raise out["e"]
    return out.get("v")


async def _setup() -> None:
    async with worker_engine_scope(), session_scope() as s:
        if await s.get(User, UID) is None:
            s.add(
                User(
                    id=UID,
                    api_key_hash=hash_api_key("preflight-key"),
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


async def _state_and_slot(jid: str) -> tuple[JobState, str | None, int]:
    async with worker_engine_scope(), session_scope() as s:
        job = await s.get(GenerationJob, jid)
        active = await count_active_jobs(s, UID)
        return job.state, job.failure_reason, active


@pytest.fixture
def _env(autonomous_db, monkeypatch):  # noqa: ANN001, ANN202
    monkeypatch.setattr("app.notify.trigger.enqueue_push_if_significant", lambda *a, **k: None)
    _run_sync(_purge)
    _run_sync(_setup)
    yield
    _run_sync(_purge)


def _set_key(monkeypatch, value: str) -> None:
    """Подменяет anthropic_api_key на cached Settings (preflight читает get_settings())."""
    monkeypatch.setattr(get_settings(), "anthropic_api_key", SecretStr(value), raising=False)


# --- 1. пустой ключ + requires_llm=True → FAILED(agent_unavailable), тело НЕ исполнено ---


@pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
def test_preflight_empty_key_terminalizes_without_running_body(_env, monkeypatch, empty):
    """Пустой/whitespace-only ANTHROPIC_API_KEY → FAILED(agent_unavailable), SDK НЕ вызван."""
    _set_key(monkeypatch, empty)
    body_ran: list[str] = []

    async def _body():  # noqa: ANN202
        body_ran.append("ran")  # реальная агент-таска тут зовёт SDK — НЕ должно случиться

    jid = _run_sync(lambda: _make_job(JobState.INTERVIEWING))
    task = _fake_task(retries=0)
    # Исключение НЕ должно пробрасываться (иначе Celery бы ретраил) — preflight поглощает.
    _run_task_in_thread(task, _body, jid, requires_llm=True)

    assert body_ran == []  # тело (вызов SDK) НЕ исполнилось — fail-fast ДО тела
    state, reason, active = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.FAILED
    assert reason == "agent_unavailable"
    assert active == 0  # concurrency-слот освобождён


def test_preflight_no_retry_increment(_env, monkeypatch):
    """Preflight НЕ инкрементит Celery-retry: исключение не проброшено → Celery не ретраит.

    run_agent_task возвращается нормально (None) без re-raise — Celery autoretry опирается на
    проброс исключения из тела таски; preflight терминализирует и возвращает, retries не растут.
    """
    _set_key(monkeypatch, "")

    async def _body():  # noqa: ANN202
        raise AssertionError("body must not run on empty-key preflight")

    jid = _run_sync(lambda: _make_job(JobState.INTERVIEWING))
    task = _fake_task(retries=0)
    # Нормальный возврат (None), без исключения — Celery не увидит retry-сигнала.
    result = _run_task_in_thread(task, _body, jid, requires_llm=True)
    assert result is None
    assert task.request.retries == 0  # стаб-таска не трогается — ретраи не сожжены


# --- 2. requires_llm=False (build/deploy) → preflight НЕ применяется ---


def test_preflight_not_applied_for_non_llm_task(_env, monkeypatch):
    """requires_llm=False (build/deploy) при пустом ключе → preflight НЕ срабатывает, тело идёт."""
    _set_key(monkeypatch, "")
    body_ran: list[str] = []

    async def _body():  # noqa: ANN202
        body_ran.append("ran")  # build/deploy Claude не зовут → preflight их не касается

    jid = _run_sync(lambda: _make_job(JobState.BUILDING))
    # requires_llm по умолчанию False (как task_build_request / task_deploy).
    _run_task_in_thread(_fake_task(retries=0), _body, jid)

    assert body_ran == ["ran"]  # тело исполнилось — preflight не применён
    state, reason, _ = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.BUILDING  # не терминализировано
    assert reason is None


# --- 3. непустой ключ + requires_llm=True → preflight пропускает, тело идёт ---


def test_preflight_nonempty_key_runs_body(_env, monkeypatch):
    """Непустой ANTHROPIC_API_KEY → preflight пропускает, тело LLM-таски исполняется."""
    _set_key(monkeypatch, "sk-ant-valid-key")
    body_ran: list[str] = []

    async def _body():  # noqa: ANN202
        body_ran.append("ran")

    jid = _run_sync(lambda: _make_job(JobState.INTERVIEWING))
    _run_task_in_thread(_fake_task(retries=0), _body, jid, requires_llm=True)

    assert body_ran == ["ran"]  # preflight не отсёк — тело исполнилось
    state, reason, _ = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.INTERVIEWING  # тело-стаб state не двигало
    assert reason is None
