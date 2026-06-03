"""ADR-019 integration: надёжная терминализация джобы при недоступном LLM + сбойном publish.

Реальный Postgres + Redis (conftest, publish идёт в РЕАЛЬНЫЙ Redis через per-task клиент).
Нормативный источник — docs/modules/observability/03-architecture.md §7.2 (критерий приёмки),
docs/modules/pipeline/03-architecture.md §H/§G, ADR-019 §Fix/§G/§E2, app/workers/tasks.py
(task_interview), app/pipeline/graceful_fail.py (run_agent_task), app/workers/beat_tasks.py
(reconcile_stuck).

Инвариант ADR-019: при невалидном/пустом ANTHROPIC_API_KEY (AuthenticationError/401) агент-таска
НАДЁЖНО терминализирует джобу в FAILED(agent_unavailable) и освобождает concurrency-слот —
ДАЖЕ при сбойном publish (best-effort §H). reconcile_stuck терминализирует stuck LLM-фазную
джобу без флапа (§E2 ветвь 2 → FAILED(stuck_timeout)).

Покрывает критерий приёмки §7.2 / §H / §G:
- task_interview с AuthenticationError(401) → FAILED(agent_unavailable), слот освобождён, даже
  когда publish_event сбоит (RuntimeError/RedisError инъекция);
- ≥2 task_interview подряд в одном процессе (per-task Redis на каждый) — обе терминализированы;
- reconcile_stuck терминализирует stuck INTERVIEWING-джобу в FAILED(stuck_timeout) без флапа,
  освобождая слот; повторный прогон идемпотентен.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from anthropic import AuthenticationError
from pydantic import SecretStr

from app.auth.concurrency import count_active_jobs, is_within_concurrency_cap
from app.core.config import get_settings
from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, User
from app.db.session import session_scope, worker_engine_scope

UID = "u_adr019owner0000000a"
_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _run_sync(coro_factory):  # noqa: ANN001, ANN202
    """Прогон корутины в свежем asyncio.run-loop в отдельном потоке (модель Celery-задачи)."""
    result: dict = {}

    def _worker() -> None:
        try:
            result["value"] = asyncio_run(coro_factory())
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def asyncio_run(coro):  # noqa: ANN001, ANN202
    import asyncio

    return asyncio.run(coro)


def _run_task_in_thread(fn, *args):  # noqa: ANN001, ANN202
    """Прогон sync Celery-таски (у неё свой asyncio.run) в отдельном потоке без активного loop'а."""
    out: dict = {}

    def _w() -> None:
        try:
            out["v"] = fn(*args)
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
                    api_key_hash=hash_api_key("adr019-key"),
                    monthly_budget_usd=Decimal("50.0000"),
                    status="active",
                )
            )
            await s.commit()


async def _make_job(state: JobState, *, last_transition_at: datetime | None = None) -> str:
    pid = new_project_id()
    jid = new_job_id()
    async with worker_engine_scope(), session_scope() as s:
        s.add(Project(id=pid, user_id=UID, prompt="build me a site", title=None))
        job = GenerationJob(
            id=jid,
            project_id=pid,
            user_id=UID,
            state=state,
            kind="generation",
            budget_usd=Decimal("5.0000"),
            spend_usd=Decimal("0.0000"),
        )
        if last_transition_at is not None:
            job.last_transition_at = last_transition_at
        s.add(job)
        await s.commit()
    return jid


async def _purge() -> None:
    from sqlalchemy import delete, select

    from app.db.models import JobEvent, UsageCounter

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
        # task_interview инкрементит usage_counters (count_generation_start) — FK на users.
        await s.execute(delete(UsageCounter).where(UsageCounter.user_id == UID))
        await s.execute(delete(User).where(User.id == UID))
        await s.commit()


@pytest.fixture
def _env(autonomous_db, monkeypatch):  # noqa: ANN001, ANN202
    monkeypatch.setattr("app.notify.trigger.enqueue_push_if_significant", lambda *a, **k: None)
    _run_sync(_purge)
    _run_sync(_setup)
    yield
    _run_sync(_purge)


async def _state_and_slot(jid: str) -> tuple[JobState, str | None, int]:
    async with worker_engine_scope(), session_scope() as s:
        job = await s.get(GenerationJob, jid)
        active = await count_active_jobs(s, UID)
        return job.state, job.failure_reason, active


def _auth_error() -> AuthenticationError:
    """401 — моделирует пустой/невалидный ANTHROPIC_API_KEY (Anthropic SDK AuthenticationError)."""
    return AuthenticationError(
        "invalid x-api-key", response=httpx.Response(401, request=_REQ), body=None
    )


# ---------------------------------------------------------------------------
# 1. task_interview при невалидном ключе → FAILED(agent_unavailable) + слот освобождён.
# ---------------------------------------------------------------------------


def test_task_interview_invalid_key_terminalizes_and_frees_slot(_env, monkeypatch):
    """task_interview с AuthenticationError(401) → FAILED(agent_unavailable), слот свободен.

    Agent 1 (run_agent1) мокается на 401 (пустой/невалидный ANTHROPIC_API_KEY). task_interview
    проходит CREATED→INTERVIEWING (count_generation_start + transition с РЕАЛЬНЫМ publish в Redis),
    затем run_agent1 бросает 401 → run_agent_task делает graceful-fail в FAILED(agent_unavailable)
    БЕЗ ретраев (is_non_retryable_llm_failure). Слот освобождён (джоба терминальна).
    """

    async def _boom_agent1(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise _auth_error()

    monkeypatch.setattr("app.workers.tasks.run_agent1", _boom_agent1)

    from app.workers.tasks import task_interview

    jid = _run_sync(lambda: _make_job(JobState.CREATED))
    # task_interview.run() — sync-тело: asyncio.run + worker_redis_scope + РЕАЛЬНЫЙ publish.
    _run_task_in_thread(task_interview.run, jid)

    state, reason, active = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.FAILED
    assert reason == "agent_unavailable"
    assert active == 0  # концетрентный слот освобождён (джоба терминальна)


def test_task_interview_terminalizes_even_with_failing_publish(_env, monkeypatch):
    """ДАЖЕ при сбойном publish (RuntimeError инъекция) джоба доходит до FAILED, слот свободен (§H).

    Инъекция RuntimeError('Event loop is closed') в Redis-клиент publish_event — best-effort catch
    (§H) поглощает → переход CREATED→INTERVIEWING и терминализация FAILED(agent_unavailable) идут.
    Это инвариант, ради которого best-effort publish и введён (ADR-019: иначе таска падала бы ДО
    терминализации и джоба зависала в INTERVIEWING, лоча слот).
    """

    async def _boom_agent1(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise _auth_error()

    monkeypatch.setattr("app.workers.tasks.run_agent1", _boom_agent1)

    class _BoomClient:
        async def publish(self, *_a, **_k) -> int:  # noqa: ANN002, ANN003
            raise RuntimeError("Event loop is closed")

    # Инъекция сбойного Redis-клиента в publish_event (через get_redis в events).
    monkeypatch.setattr("app.pipeline.events.get_redis", lambda: _BoomClient())

    from app.workers.tasks import task_interview

    jid = _run_sync(lambda: _make_job(JobState.CREATED))
    _run_task_in_thread(task_interview.run, jid)

    state, reason, active = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.FAILED
    assert reason == "agent_unavailable"
    assert active == 0


def test_two_invalid_key_interviews_in_one_process(_env, monkeypatch):
    """≥2 task_interview подряд в ОДНОМ процессе (per-task Redis каждый) — обе терминализированы.

    Второй asyncio.run-loop в том же процессе: до фикса per-task Redis второй publish брал
    соединение глобального пула с закрытого loop'а → `Event loop is closed` → таска падала ДО
    графейла → джоба #2 зависала в INTERVIEWING. Per-task клиент (фикс §7.2) → обе доходят.
    """

    async def _boom_agent1(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise _auth_error()

    monkeypatch.setattr("app.workers.tasks.run_agent1", _boom_agent1)

    from app.workers.tasks import task_interview

    jid1 = _run_sync(lambda: _make_job(JobState.CREATED))
    _run_task_in_thread(task_interview.run, jid1)
    jid2 = _run_sync(lambda: _make_job(JobState.CREATED))
    _run_task_in_thread(task_interview.run, jid2)

    for jid in (jid1, jid2):
        state, reason, _ = _run_sync(lambda j=jid: _state_and_slot(j))
        assert state == JobState.FAILED
        assert reason == "agent_unavailable"
    # Оба слота освобождены.
    _, _, active = _run_sync(lambda: _state_and_slot(jid2))
    assert active == 0


# ---------------------------------------------------------------------------
# 2. reconcile_stuck терминализирует stuck LLM-фазную джобу без флапа (§E2 ветвь 2).
# ---------------------------------------------------------------------------


def test_reconcile_terminalizes_stuck_interviewing_job(_env):
    """reconcile_stuck: stale INTERVIEWING → FAILED(stuck_timeout), слот свободен (§E2 ветвь 2).

    Без живой таски (смерть воркера до записи перехода) reconciler — предохранитель §E2 ветвь 2:
    fail-stuck в FAILED(stuck_timeout), освобождая concurrency-слот. publish best-effort (§H) идёт
    в РЕАЛЬНЫЙ Redis через per-task клиент (worker_redis_scope в reconcile_stuck) — без флапа.
    """
    from app.workers.beat_tasks import reconcile_stuck

    threshold = get_settings().stuck_threshold_s
    stale = datetime.now(UTC) - timedelta(seconds=threshold + 60)
    jid = _run_sync(lambda: _make_job(JobState.INTERVIEWING, last_transition_at=stale))

    handled = _run_task_in_thread(reconcile_stuck.run)
    assert handled == 1

    state, reason, active = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.FAILED
    assert reason == "stuck_timeout"
    assert active == 0


def test_reconcile_stuck_idempotent_second_run_noop(_env):
    """Повторный reconcile_stuck не трогает уже-терминальную джобу (идемпотентность, без флапа)."""
    from app.workers.beat_tasks import reconcile_stuck

    threshold = get_settings().stuck_threshold_s
    stale = datetime.now(UTC) - timedelta(seconds=threshold + 60)
    jid = _run_sync(lambda: _make_job(JobState.INTERVIEWING, last_transition_at=stale))

    assert _run_task_in_thread(reconcile_stuck.run) == 1
    # Второй прогон в том же процессе (новый loop): джоба уже FAILED → 0 обработано, без cross-loop.
    assert _run_task_in_thread(reconcile_stuck.run) == 0

    state, reason, _ = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.FAILED
    assert reason == "stuck_timeout"


# ---------------------------------------------------------------------------
# 3. Fail-fast preflight ПУСТОГО ANTHROPIC_API_KEY (round 3, основной кейс) — pipeline §G.
#    Источник истины: docs/modules/pipeline/03-architecture.md §G «Критерий приёмки
#    (integration, пустой ключ, основной кейс)», docs/06-testing-strategy.md (round 3 §1).
#    Существующие кейсы выше моделировали AuthenticationError/401 (невалидный ключ через SDK);
#    эти — ПУСТОЙ ключ, отсекаемый preflight'ом ДО SDK-вызова (fail-fast, version-agnostic).
# ---------------------------------------------------------------------------


def _set_empty_key(monkeypatch, value: str = "") -> None:
    """Подменяет anthropic_api_key на cached Settings (preflight читает get_settings())."""
    monkeypatch.setattr(get_settings(), "anthropic_api_key", SecretStr(value), raising=False)


async def _last_transition_at(jid: str) -> datetime:
    async with worker_engine_scope(), session_scope() as s:
        job = await s.get(GenerationJob, jid)
        return job.last_transition_at


@pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
def test_empty_key_preflight_terminalizes_agent_unavailable_fast(_env, monkeypatch, empty):
    """Пустой/whitespace ANTHROPIC_API_KEY → task_interview за секунды → FAILED(agent_unavailable).

    Основной round-3 кейс (pipeline §G): preflight отсекает пустой ключ ДО Anthropic SDK.
    reason ИМЕННО agent_unavailable (НЕ wall_clock_exceeded/stuck_timeout); слот освобождён;
    run_agent1 (SDK) НЕ вызван; без проброса RuntimeError/TypeError (таска не в «unexpected»).
    """
    _set_empty_key(monkeypatch, empty)

    agent1_calls: list[int] = []

    async def _spy_agent1(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        agent1_calls.append(1)  # preflight fail-fast ДО тела → Agent 1 не должен вызваться
        raise AssertionError("run_agent1 must not be called when key is empty (preflight)")

    monkeypatch.setattr("app.workers.tasks.run_agent1", _spy_agent1)

    from app.workers.tasks import task_interview

    jid = _run_sync(lambda: _make_job(JobState.CREATED))
    # Без проброса RuntimeError/TypeError — taska не падает в «unexpected» (preflight поглощает).
    _run_task_in_thread(task_interview.run, jid)

    assert agent1_calls == []  # SDK не вызван — fail-fast ДО тела
    state, reason, active = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.FAILED
    assert reason == "agent_unavailable"  # НЕ wall_clock_exceeded / stuck_timeout
    assert active == 0  # concurrency-слот освобождён


def test_empty_key_preflight_frees_slot_next_job_not_blocked(_env, monkeypatch):
    """После preflight-фейла слот свободен → следующая джоба того же юзера НЕ упирается в cap.

    Освобождённый слот (джоба #1 терминальна) означает: следующий POST /projects не получит
    402 concurrency_limit. Проверяем DB-уровень cap (is_within_concurrency_cap) — после фейла
    #1 active=0 < max → второй старт разрешён (free cap=1).
    """
    _set_empty_key(monkeypatch)

    async def _spy_agent1(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("run_agent1 must not be called (preflight)")

    monkeypatch.setattr("app.workers.tasks.run_agent1", _spy_agent1)

    from app.workers.tasks import task_interview

    jid1 = _run_sync(lambda: _make_job(JobState.CREATED))
    _run_task_in_thread(task_interview.run, jid1)

    async def _cap_ok() -> bool:
        async with worker_engine_scope(), session_scope() as s:
            return await is_within_concurrency_cap(s, UID)

    # Слот #1 освобождён → cap позволяет ещё одну джобу (иначе POST /projects → 402).
    assert _run_sync(_cap_ok) is True


def test_empty_key_preflight_reconciler_not_involved(_env, monkeypatch):
    """Терминализация пустого ключа — preflight (за секунды), reconciler НЕ участвует.

    Переход в FAILED произошёл ДО STUCK_THRESHOLD_S: last_transition_at свежий (не stale).
    Прогон reconcile_stuck НЕ находит stuck-джоб (джоба уже терминальна / не stale) → 0 обработано.
    Это доказывает, что кейс закрыт fail-fast preflight'ом, а не reconciler-backstop'ом по TTL.
    """
    _set_empty_key(monkeypatch)
    monkeypatch.setattr(
        "app.workers.tasks.run_agent1",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("agent1 must not run")),
    )

    from app.workers.beat_tasks import reconcile_stuck
    from app.workers.tasks import task_interview

    jid = _run_sync(lambda: _make_job(JobState.CREATED))
    before = datetime.now(UTC)
    _run_task_in_thread(task_interview.run, jid)

    # Переход случился сразу (за секунды), не через STUCK_THRESHOLD_S (900 s).
    transition_at = _run_sync(lambda: _last_transition_at(jid))
    if transition_at.tzinfo is None:
        transition_at = transition_at.replace(tzinfo=UTC)
    threshold = get_settings().stuck_threshold_s
    assert (transition_at - before) < timedelta(seconds=threshold)

    state, reason, _ = _run_sync(lambda: _state_and_slot(jid))
    assert state == JobState.FAILED
    assert reason == "agent_unavailable"

    # reconciler не участвует: джоба уже терминальна → 0 обработано (не stuck_timeout).
    assert _run_task_in_thread(reconcile_stuck.run) == 0
    _, reason_after, _ = _run_sync(lambda: _state_and_slot(jid))
    assert reason_after == "agent_unavailable"  # reason НЕ перетёрт на stuck_timeout
