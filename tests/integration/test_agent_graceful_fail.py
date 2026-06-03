"""Integration: graceful-fail шага агента при недоступности LLM (ADR-019 §G/§B).

Реальный Postgres (per-task engine через worker_engine_scope в run_agent_task). Нормативный
источник — docs/06-testing-strategy.md §Integration «Graceful-fail агента при недоступности
LLM», ADR-019 §B, app/pipeline/graceful_fail.py.

Покрывает классификацию исхода run_agent_task (единственная точка решения retry_policy):
- не-транзиентный LLM-сбой (AuthenticationError/401) → немедленный FAILED(agent_unavailable)
  БЕЗ ретраев (исключение НЕ пробрасывается → Celery не ретраит);
- транзиентный LLM-сбой (RateLimitError/429, APIStatusError/5xx) и ретраи НЕ исчерпаны →
  исключение пробрасывается (Celery autoretry);
- транзиентный LLM-сбой и ретраи ИСЧЕРПАНЫ → FAILED(agent_unavailable), слот освобождён;
- транзиентный НЕ-LLM инфра-сбой исчерпан → FAILED(infra_error) (отдельный reason-код);
- успешное тело → продвигает state, без терминализации;
- идемпотентность graceful-fail на уже-терминальной джобе (no-op).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from anthropic import APIStatusError, AuthenticationError, RateLimitError

from app.auth.concurrency import count_active_jobs
from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, User
from app.db.session import session_scope
from app.pipeline.graceful_fail import run_agent_task
from app.workers.retry_policy import MAX_RETRIES, TransientInfraError

pytestmark = pytest.mark.asyncio

UID = "u_gfowner000000000000a"
_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _fake_task(retries: int):  # noqa: ANN202
    """Минимальный bound Celery-таск-стаб: только task.request.retries (что читает код §G)."""
    return SimpleNamespace(request=SimpleNamespace(retries=retries))


async def _run_agent_task_in_thread(task, coro_factory, job_id):  # noqa: ANN001, ANN202
    """Прогон sync run_agent_task в отдельном потоке (у него СВОЙ asyncio.run).

    run_agent_task — sync-тело bound Celery-таски: оно само зовёт asyncio.run (observability §7).
    Вызвать его из async-теста напрямую нельзя (asyncio.run из активного loop'а запрещён). Поток
    без активного loop'а — точная модель синхронного Celery-воркера. Исключения проброса (Celery
    autoretry) поднимаются обратно в await.
    """
    return await asyncio.to_thread(run_agent_task, task, coro_factory, job_id)


def _auth_error() -> AuthenticationError:
    return AuthenticationError(
        "no key", response=httpx.Response(401, request=_REQ), body=None
    )


def _rate_limit_error() -> RateLimitError:
    return RateLimitError(
        "rate limited", response=httpx.Response(429, request=_REQ), body=None
    )


def _server_error() -> APIStatusError:
    return APIStatusError(
        "server error", response=httpx.Response(503, request=_REQ), body=None
    )


async def _setup_user() -> None:
    async with session_scope() as s:
        if await s.get(User, UID) is None:
            s.add(
                User(
                    id=UID,
                    api_key_hash=hash_api_key("gf-key"),
                    monthly_budget_usd=Decimal("50.0000"),
                    status="active",
                )
            )
            await s.commit()


async def _make_job(state: JobState) -> str:
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
    return jid


async def _purge() -> None:
    from sqlalchemy import delete, select

    from app.db.models import JobEvent

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
        if jids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(jids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == UID))
        for pid in pids:
            await s.execute(delete(Project).where(Project.id == pid))
        await s.execute(delete(User).where(User.id == UID))
        await s.commit()


@pytest.fixture(autouse=True)
def _no_publish(monkeypatch):  # noqa: ANN001, ANN202
    async def _noop_publish(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr("app.pipeline.events.publish_event", _noop_publish)
    monkeypatch.setattr(
        "app.notify.trigger.enqueue_push_if_significant", lambda *a, **k: None
    )


@pytest.fixture(autouse=True)
async def _env(autonomous_db):  # noqa: ANN001, ANN202
    await _purge()
    await _setup_user()
    yield
    await _purge()


async def _state_of(jid: str) -> tuple[JobState, str | None]:
    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        return job.state, job.failure_reason


# --- не-транзиентный LLM-сбой (401) → немедленный FAILED(agent_unavailable) без ретраев ---


async def test_auth_error_immediate_agent_unavailable_no_retry():
    jid = await _make_job(JobState.INTERVIEWING)

    async def _body():  # noqa: ANN202
        raise _auth_error()

    task = _fake_task(retries=0)  # ретраи НЕ исчерпаны — но 401 всё равно НЕ ретраится
    # Не должно пробросить исключение (Celery бы ретраил) — graceful-fail поглощает 401.
    await _run_agent_task_in_thread(task, _body, jid)

    state, reason = await _state_of(jid)
    assert state == JobState.FAILED
    assert reason == "agent_unavailable"


# --- транзиентный LLM-сбой, ретраи НЕ исчерпаны → пробрасываем (Celery autoretry) ---


@pytest.mark.parametrize("exc_factory", [_rate_limit_error, _server_error])
async def test_transient_llm_not_exhausted_reraises_for_retry(exc_factory):
    jid = await _make_job(JobState.SPECCING)

    async def _body():  # noqa: ANN202
        raise exc_factory()

    task = _fake_task(retries=0)  # ретраи есть → пробрасываем для Celery autoretry
    with pytest.raises((RateLimitError, APIStatusError)):
        await _run_agent_task_in_thread(task, _body, jid)

    # state НЕ терминализирован — джоба ждёт Celery-ретрая.
    state, reason = await _state_of(jid)
    assert state == JobState.SPECCING
    assert reason is None


# --- транзиентный LLM-сбой, ретраи ИСЧЕРПАНЫ → FAILED(agent_unavailable) ---


@pytest.mark.parametrize("exc_factory", [_rate_limit_error, _server_error])
async def test_transient_llm_exhausted_agent_unavailable(exc_factory):
    jid = await _make_job(JobState.INTERVIEWING)

    async def _body():  # noqa: ANN202
        raise exc_factory()

    task = _fake_task(retries=MAX_RETRIES)  # ретраи исчерпаны
    await _run_agent_task_in_thread(task, _body, jid)

    state, reason = await _state_of(jid)
    assert state == JobState.FAILED
    assert reason == "agent_unavailable"
    # Слот освобождён.
    async with session_scope() as s:
        assert await count_active_jobs(s, UID) == 0


# --- транзиентный НЕ-LLM инфра-сбой, ретраи исчерпаны → FAILED(infra_error) ---


async def test_transient_non_llm_exhausted_infra_error():
    jid = await _make_job(JobState.BUILDING)

    async def _body():  # noqa: ANN202
        raise TransientInfraError("docker daemon unreachable")

    task = _fake_task(retries=MAX_RETRIES)
    await _run_agent_task_in_thread(task, _body, jid)

    state, reason = await _state_of(jid)
    assert state == JobState.FAILED
    assert reason == "infra_error"  # не-LLM инфра → отдельный reason-код от agent_unavailable


# --- успешное тело → продвигает state, без терминализации ---


async def test_success_body_no_terminalization():
    jid = await _make_job(JobState.INTERVIEWING)
    marker: list[str] = []

    async def _body():  # noqa: ANN202
        # Тело само двигает state (как реальная агент-таска); здесь — маркер вызова.
        async with session_scope() as s:
            job = await s.get(GenerationJob, jid)
            from app.pipeline.events import transition

            await transition(s, job, JobState.SPECCING)
        marker.append("ran")

    await _run_agent_task_in_thread(_fake_task(retries=0), _body, jid)
    assert marker == ["ran"]
    state, reason = await _state_of(jid)
    assert state == JobState.SPECCING  # продвинулась, не FAILED
    assert reason is None


# --- идемпотентность graceful-fail на уже-терминальной джобе (no-op) ---


async def test_graceful_fail_noop_on_already_terminal():
    jid = await _make_job(JobState.FAILED)

    async def _body():  # noqa: ANN202
        raise _auth_error()

    await _run_agent_task_in_thread(_fake_task(retries=0), _body, jid)
    # Джоба уже FAILED — graceful-fail no-op, failure_reason не перетёрт (остался None из make).
    state, _ = await _state_of(jid)
    assert state == JobState.FAILED
