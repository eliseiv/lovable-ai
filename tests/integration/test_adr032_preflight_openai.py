"""Integration: provider-aware preflight LLM-credential (ADR-032 §5, ADR-019 §G, docs).

Источник истины — docs/adr/ADR-032-llm-provider-abstraction-openai.md §5 (preflight по credential
АКТИВНОГО провайдера, какой ключ проверять — по LLM_PROVIDER), docs/06-testing-strategy.md §Unit
«preflight OPENAI_API_KEY», app/pipeline/graceful_fail.py, config.active_llm_api_key.

Покрывает сценарий 5 ТЗ (provider-aware preflight):
- LLM_PROVIDER=openai + пустой/whitespace OPENAI_API_KEY → graceful FAILED(agent_unavailable) ДО
  тела/SDK, без инкремента Celery-retry, слот освобождён;
- LLM_PROVIDER=openai + НЕпустой OPENAI_API_KEY → preflight пропускает, тело идёт (пустой
  ANTHROPIC_API_KEY НЕ блокирует — проверяется ключ АКТИВНОГО провайдера);
- LLM_PROVIDER=anthropic → проверяется ANTHROPIC_API_KEY как прежде (зеркало; OPENAI_API_KEY
  не влияет).

Терминализация (_graceful_fail_job) идёт через РЕАЛЬНЫЙ Postgres + Redis (per-task scope в
run_agent_task) — единый транзакционный путь graceful-fail (§G), не мокается. run_agent_task
гоняется в отдельном потоке (свой asyncio.run, как у Celery-воркера).
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

UID = "u_adr032preflight0000a"


def _fake_task(retries: int = 0):  # noqa: ANN202
    return SimpleNamespace(request=SimpleNamespace(retries=retries))


def _run_sync(coro_factory):  # noqa: ANN001, ANN202
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
                    api_key_hash=hash_api_key("adr032-preflight-key"),
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


def _set_provider(monkeypatch, provider: str) -> None:
    monkeypatch.setattr(get_settings(), "llm_provider", provider, raising=False)


def _set_openai_key(monkeypatch, value: str) -> None:
    monkeypatch.setattr(get_settings(), "openai_api_key", SecretStr(value), raising=False)


def _set_anthropic_key(monkeypatch, value: str) -> None:
    monkeypatch.setattr(get_settings(), "anthropic_api_key", SecretStr(value), raising=False)


# --- 1. openai + пустой OPENAI_API_KEY → FAILED(agent_unavailable), тело НЕ исполнено ---


@pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
def test_openai_empty_key_preflight_fails_fast(_env, monkeypatch, empty):
    """LLM_PROVIDER=openai + пустой/whitespace OPENAI_API_KEY → graceful fail ДО SDK (§5)."""
    _set_provider(monkeypatch, "openai")
    _set_openai_key(monkeypatch, empty)
    # ANTHROPIC_API_KEY непустой — НЕ должен спасать (проверяется ключ активного провайдера).
    _set_anthropic_key(monkeypatch, "sk-ant-nonempty")
    body_ran: list[str] = []

    async def _body():  # noqa: ANN202
        body_ran.append("ran")

    jid = _run_sync(lambda: _make_job(JobState.INTERVIEWING))
    _run_task_in_thread(_fake_task(retries=0), _body, jid, requires_llm=True)

    assert body_ran == []  # тело (SDK) НЕ исполнилось — fail-fast по openai-ключу
    state, reason, active = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.FAILED
    assert reason == "agent_unavailable"
    assert active == 0  # слот освобождён


def test_openai_empty_key_no_retry_increment(_env, monkeypatch):
    """Preflight не пробрасывает исключение → Celery не ретраит (retries не растут)."""
    _set_provider(monkeypatch, "openai")
    _set_openai_key(monkeypatch, "")

    async def _body():  # noqa: ANN202
        raise AssertionError("body must not run on empty openai-key preflight")

    jid = _run_sync(lambda: _make_job(JobState.INTERVIEWING))
    task = _fake_task(retries=0)
    result = _run_task_in_thread(task, _body, jid, requires_llm=True)
    assert result is None
    assert task.request.retries == 0


# --- 2. openai + непустой OPENAI_API_KEY → preflight пропускает (пустой ANTHROPIC не мешает) ---


def test_openai_nonempty_key_runs_body_despite_empty_anthropic(_env, monkeypatch):
    """LLM_PROVIDER=openai + валидный OPENAI_API_KEY → тело идёт при пустом ANTHROPIC_API_KEY."""
    _set_provider(monkeypatch, "openai")
    _set_openai_key(monkeypatch, "sk-openai-valid")
    _set_anthropic_key(monkeypatch, "")  # пустой anthropic не блокирует под openai
    body_ran: list[str] = []

    async def _body():  # noqa: ANN202
        body_ran.append("ran")

    jid = _run_sync(lambda: _make_job(JobState.INTERVIEWING))
    _run_task_in_thread(_fake_task(retries=0), _body, jid, requires_llm=True)

    assert body_ran == ["ran"]  # preflight пропустил по openai-ключу
    state, reason, _ = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.INTERVIEWING
    assert reason is None


# --- 3. anthropic-путь: проверяется ANTHROPIC_API_KEY (зеркало, openai-ключ не влияет) ---


def test_anthropic_provider_checks_anthropic_key(_env, monkeypatch):
    """LLM_PROVIDER=anthropic + пустой ANTHROPIC_API_KEY → fail-fast при заданном OPENAI_API_KEY."""
    _set_provider(monkeypatch, "anthropic")
    _set_anthropic_key(monkeypatch, "")
    _set_openai_key(monkeypatch, "sk-openai-nonempty")  # не активный провайдер — не спасает
    body_ran: list[str] = []

    async def _body():  # noqa: ANN202
        body_ran.append("ran")

    jid = _run_sync(lambda: _make_job(JobState.INTERVIEWING))
    _run_task_in_thread(_fake_task(retries=0), _body, jid, requires_llm=True)

    assert body_ran == []
    state, reason, active = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.FAILED
    assert reason == "agent_unavailable"
    assert active == 0
