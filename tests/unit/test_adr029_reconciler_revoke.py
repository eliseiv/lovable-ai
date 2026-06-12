"""ADR-029 §C — best-effort reconciler revoke живой Celery-таски при терминализации + job_task-ключ.

Нормативный источник — docs/adr/ADR-029-terminal-state-invariant-no-overwrite-deploy-guard-
reconciler-revoke.md §Decision C, docs/06-testing-strategy.md §unit «reconciler revoke»,
app/workers/beat_tasks.py::_maybe_revoke_live_task, app/pipeline/dispatcher.py::job_task_key/
_record_job_task/dispatch_for_state.

§C: при терминализации reconciler'ом (stuck_timeout/wall_clock_exceeded) делается best-effort
`revoke(task_id, terminate=True, signal='SIGTERM')` по task_id из Redis job_task:{job_id} (пишет
dispatch_for_state при постановке таски). Назначение — прекратить бесполезный compute/деплой
уже-FAILED джобы, НЕ корректность (барьер A держит корректность). Поэтому revoke best-effort:
промах ключа (TTL истёк / таски не было) или ошибка Redis/брокера НЕ валит fail-stuck.

Все внешние границы (Redis get/celery revoke) мокаются — unit без реального брокера.
"""

from __future__ import annotations

import pytest

import app.workers.beat_tasks as beat
from app.pipeline.dispatcher import job_task_key

pytestmark = pytest.mark.asyncio


class _FakeRedis:
    """Async Redis-дублёр: get отдаёт заданное значение или бросает заданное исключение."""

    def __init__(self, value=None, exc=None) -> None:  # noqa: ANN001
        self._value = value
        self._exc = exc
        self.get_calls: list[str] = []

    async def get(self, key):  # noqa: ANN001, ANN202
        self.get_calls.append(key)
        if self._exc is not None:
            raise self._exc
        return self._value


async def test_job_task_key_format():
    """Ключ хранения task_id — job_task:{job_id} (точка чтения revoke/записи dispatch)."""
    assert job_task_key("j_abc123") == "job_task:j_abc123"


async def test_revoke_called_with_terminate_when_task_id_present(monkeypatch):
    """task_id присутствует в Redis → revoke(task_id, terminate=True, signal='SIGTERM') вызван."""
    client = _FakeRedis(value=b"celery-task-id-xyz")

    revoke_calls: list = []
    monkeypatch.setattr(
        beat.celery_app.control,
        "revoke",
        lambda task_id, **kw: revoke_calls.append((task_id, kw)),
    )

    await beat._maybe_revoke_live_task(client, "j_revoke1")

    # Ключ прочитан и revoke вызван с terminate=True.
    assert client.get_calls == [job_task_key("j_revoke1")]
    assert len(revoke_calls) == 1
    task_id, kw = revoke_calls[0]
    assert task_id == "celery-task-id-xyz"
    assert kw.get("terminate") is True
    assert kw.get("signal") == "SIGTERM"


async def test_revoke_decodes_str_task_id(monkeypatch):
    """task_id из Redis как str (не bytes) тоже корректно ревокается (defensive decode)."""
    client = _FakeRedis(value="plain-str-task-id")
    revoke_calls: list = []
    monkeypatch.setattr(
        beat.celery_app.control, "revoke", lambda task_id, **kw: revoke_calls.append(task_id)
    )

    await beat._maybe_revoke_live_task(client, "j_revoke2")
    assert revoke_calls == ["plain-str-task-id"]


async def test_revoke_noop_when_key_missing(monkeypatch):
    """Промах ключа (TTL истёк / таски не было) → revoke НЕ вызван, без ошибки (best-effort)."""
    client = _FakeRedis(value=None)
    revoke_calls: list = []
    monkeypatch.setattr(beat.celery_app.control, "revoke", lambda *a, **kw: revoke_calls.append(a))

    # Не должно бросить — отсутствие ключа штатно (барьер A держит корректность).
    await beat._maybe_revoke_live_task(client, "j_revoke3")
    assert revoke_calls == []  # нечего ревокать


async def test_revoke_swallows_redis_read_error(monkeypatch):
    """Ошибка Redis при чтении ключа → НЕ пробрасывается (best-effort), revoke НЕ вызван."""
    import redis.asyncio as aioredis

    client = _FakeRedis(exc=aioredis.RedisError("redis down"))
    revoke_calls: list = []
    monkeypatch.setattr(beat.celery_app.control, "revoke", lambda *a, **kw: revoke_calls.append(a))

    # Не должно бросить — fail-stuck не валится из-за недоступности Redis.
    await beat._maybe_revoke_live_task(client, "j_revoke4")
    assert revoke_calls == []


async def test_revoke_swallows_broker_error(monkeypatch):
    """Ошибка брокера при revoke → НЕ пробрасывается (best-effort §C: не валит fail-stuck)."""
    client = _FakeRedis(value=b"task-id-1")

    def _boom_revoke(task_id, **kw):  # noqa: ANN001, ANN202
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(beat.celery_app.control, "revoke", _boom_revoke)

    # Не должно бросить — корректность держит барьер A, revoke лишь оптимизация.
    await beat._maybe_revoke_live_task(client, "j_revoke5")
