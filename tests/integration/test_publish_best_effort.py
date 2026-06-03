"""§H: publish_event best-effort — сбой Redis НЕ валит уже-закоммиченный переход (ADR-019 §Fix).

Реальный Postgres + Redis (conftest). Нормативный источник — docs/modules/pipeline/
03-architecture.md §H (критерий приёмки qa), ADR-019 §Fix п.3, app/pipeline/events.py
(publish_event ловит `(RedisError, OSError, RuntimeError)`, WARN, не пробрасывает).

Контракт §H: publish в Redis pub/sub — best-effort нотификация для SSE; источник истины статуса
— `generation_jobs.state` + append-only `job_events`. Сбой publish НЕ должен валить транзакцию/
таску перехода: переход уже закоммичен ДО publish (потеря publish → SSE дочитает из job_events
по Last-Event-ID). publish_event ловит `(RedisError, OSError, RuntimeError)` (RuntimeError —
предохранитель от остаточной loop-affinity `Event loop is closed`), логирует WARN, не пробрасывает.

Покрывает критерий приёмки §H:
- инъекция RedisError / OSError / RuntimeError из Redis-клиента в publish_event → НЕ пробрасывает;
- уже-закоммиченный переход state НЕ откатывается (state в БД продвинулся, job_events записан).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import redis.asyncio as aioredis

from app.core.ids import new_job_id, new_project_id
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, Project
from app.pipeline import events as events_mod
from app.pipeline.events import publish_event, transition

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


class _BoomClient:
    """Redis-клиент-дублёр: publish бросает заданное исключение (инъекция сбоя нотификации)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def publish(self, *_a, **_k) -> int:  # noqa: ANN002, ANN003
        raise self._exc


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):  # noqa: ANN001, ANN202
    # enqueue_push — no-op (внешняя граница APNs не в скоупе теста publish best-effort).
    monkeypatch.setattr("app.notify.trigger.enqueue_push_if_significant", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# Unit-уровень: publish_event при инъекции сбоя НЕ пробрасывает.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        aioredis.RedisError("redis down"),
        aioredis.ConnectionError("conn refused"),  # подкласс RedisError
        OSError("socket error"),
        RuntimeError("Event loop is closed"),
        RuntimeError("Future attached to a different loop"),
    ],
)
async def test_publish_event_swallows_injected_failure(monkeypatch, exc):  # noqa: ANN001
    """publish_event при RedisError/OSError/RuntimeError из клиента → не пробрасывает."""
    monkeypatch.setattr(events_mod, "get_redis", lambda: _BoomClient(exc))
    # Не должно пробросить — иначе таска перехода падала бы (прод-инцидент ADR-019).
    await publish_event("j_test", "state_changed", to_state="INTERVIEWING")


async def test_publish_event_logs_warning_on_failure(monkeypatch, caplog):  # noqa: ANN001
    """Сбой publish логируется WARN (не фатально), без проброса (§H: WARN, не пробрасывает)."""
    import logging

    monkeypatch.setattr(events_mod, "get_redis", lambda: _BoomClient(RedisErr()))
    with caplog.at_level(logging.WARNING):
        await publish_event("j_warn", "state_changed", to_state="LIVE")
    assert any("redis_publish_failed" in r.message for r in caplog.records)


def RedisErr() -> aioredis.RedisError:  # noqa: N802 — фабрика-хелпер для caplog-теста
    return aioredis.RedisError("boom")


# ---------------------------------------------------------------------------
# Integration: переход state закоммичен, publish-сбой его НЕ откатывает.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: aioredis.RedisError("redis down"),
        lambda: OSError("socket error"),
        lambda: RuntimeError("Event loop is closed"),
    ],
)
async def test_transition_commits_even_if_publish_fails(
    session, seeded_user, monkeypatch, exc_factory
):  # noqa: ANN001
    """transition: сбой publish НЕ откатывает закоммиченный переход (state продвинут, event есть).

    transition коммитит state + job_events ДО publish_event. publish бросает RedisError/OSError/
    RuntimeError, но best-effort catch его поглощает → переход остаётся в БД (§H).
    """
    monkeypatch.setattr(events_mod, "get_redis", lambda: _BoomClient(exc_factory()))

    job = await _make_job(session, seeded_user.id, JobState.SPECCING)
    # Не должно пробросить, несмотря на сбойный publish.
    await transition(session, job, JobState.BUILDING, payload={"source_ref": "x"})

    # State продвинулся в БД (переход закоммичен ДО publish — сбой нотификации не откатил его).
    refreshed = await session.get(GenerationJob, job.id)
    assert refreshed.state == JobState.BUILDING

    # job_events записан (append-only источник истины для SSE-replay).
    rows = (
        await session.execute(JobEvent.__table__.select().where(JobEvent.job_id == job.id))
    ).all()
    assert len(rows) == 1
    assert rows[0]._mapping["to_state"] == "BUILDING"


async def test_fail_job_terminalizes_even_if_publish_fails(session, seeded_user, monkeypatch):  # noqa: ANN001
    """fail_job: терминализация в FAILED доходит даже при сбойном publish (§H/§G согласованность).

    Это инвариант, ради которого введён best-effort publish: graceful-fail / reconciler-fail-stuck
    надёжно терминализируют джобу и освобождают слот, даже если нотификация в Redis сбоит.
    """
    from app.pipeline.events import fail_job

    monkeypatch.setattr(
        events_mod, "get_redis", lambda: _BoomClient(RuntimeError("Event loop is closed"))
    )

    job = await _make_job(session, seeded_user.id, JobState.INTERVIEWING)
    await fail_job(session, job, failure_reason="agent_unavailable")

    refreshed = await session.get(GenerationJob, job.id)
    assert refreshed.state == JobState.FAILED
    assert refreshed.failure_reason == "agent_unavailable"
