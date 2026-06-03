"""SSE-стрим статуса джобы (Sprint 5, ADR-012, docs/modules/api/02-api-contracts.md).

Нормативная семантика reconnect/Last-Event-ID/heartbeat/завершения (ADR-012):
  - Event-id = job_events.id (bigserial, монотонный per-job). Каждый кадр несёт id:.
  - Порядок (ADR-012 §2): СНАЧАЛА подписка на Redis job:{jid}, ЗАТЕМ catch-up из
    job_events (id > Last-Event-ID), дедуп по id (отбрасываем id <= последнего отданного).
    Pub/sub — at-most-once wake-сигнал; источник истины replay — Postgres job_events.
  - Реализация дедупа/id: live-кадры читаются НЕ из payload pub/sub (он не несёт id), а
    повторным чтением job_events WHERE id > last_seen_id при каждом wake-сигнале/heartbeat —
    так id и порядок гарантированы Postgres, дубли невозможны (last_seen_id монотонен).
  - Heartbeat: каждые SSE_HEARTBEAT_S — комментарий ": ping" (keepalive idle-соединения).
  - Первый кадр несёт retry: {SSE_RETRY_MS} (hint клиенту по reconnect).
  - Завершение: на терминальном state (LIVE/FAILED) — финальные события + кадр event: done,
    стрим закрывается. Уже-терминальная джоба при подключении → снимок + done + закрытие.
  - Лимит SSE_MAX_STREAMS_PER_KEY на ключ → 429 (счётчик в Redis, incr на открытии /
    decr на закрытии).

Cross-tenant (владение джобы → 404) проверяется ДО вызова стрима (router _load_owned_job).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.models import JobEvent
from app.db.session import session_scope
from app.observability import metrics
from app.observability.redis_pool import get_redis

logger = get_logger(__name__)

# Терминальные state джобы — на них стрим шлёт event: done и закрывается (ADR-012 §6).
_TERMINAL_STATES: frozenset[str] = frozenset({JobState.LIVE.value, JobState.FAILED.value})

# Имя SSE-события завершения (ADR-012 §6) — клиент по нему НЕ переподключается.
_DONE_EVENT = "done"


def _redis_channel(job_id: str) -> str:
    return f"job:{job_id}"


def _stream_count_key(key_id: str) -> str:
    return f"sse:streams:{key_id}"


@dataclass(frozen=True)
class StreamSlot:
    """Слот лимита SSE-стримов на ключ: acquired=False → 429 (превышение)."""

    acquired: bool
    count: int


async def acquire_stream_slot(key_id: str) -> StreamSlot:
    """Атомарный INCR счётчика стримов ключа. count > SSE_MAX_STREAMS_PER_KEY → не взяли (429).

    При превышении немедленно откатывает INCR (DECR), чтобы счётчик не «протекал» вверх на
    отказанных подключениях. TTL счётчика — страховка от утечки при краше воркера (стрим
    обычно сам делает release_stream_slot в finally).
    """
    settings = get_settings()
    client = get_redis()  # Sprint 6 (TD-007): переиспользуемый ConnectionPool, не from_url.
    metrics.redis_pool_in_use.labels(pool="sse").inc()
    try:
        count = await client.incr(_stream_count_key(key_id))
        # TTL-страховка: счётчик не должен жить вечно при аварийном незакрытии стрима.
        await client.expire(_stream_count_key(key_id), 3600)
        if count > settings.sse_max_streams_per_key:
            await client.decr(_stream_count_key(key_id))
            metrics.sse_rejected_total.labels(reason="max_streams_per_key").inc()
            return StreamSlot(acquired=False, count=count - 1)
        return StreamSlot(acquired=True, count=count)
    finally:
        metrics.redis_pool_in_use.labels(pool="sse").dec()


async def release_stream_slot(key_id: str) -> None:
    """DECR счётчика стримов ключа при закрытии стрима (не уходит ниже 0)."""
    client = get_redis()  # Sprint 6 (TD-007): переиспользуемый ConnectionPool.
    metrics.redis_pool_in_use.labels(pool="sse").inc()
    try:
        count = await client.decr(_stream_count_key(key_id))
        if count < 0:
            await client.set(_stream_count_key(key_id), 0)
    finally:
        metrics.redis_pool_in_use.labels(pool="sse").dec()


def _format_frame(event: JobEvent, *, retry_ms: int | None = None) -> bytes:
    """SSE-кадр из job_events: id:, event:, data: (JSON полей контракта). retry: — в первом."""
    data = {
        "event_type": event.event_type,
        "from_state": event.from_state,
        "to_state": event.to_state,
        "payload": event.payload,
        "created_at": event.created_at.isoformat() if event.created_at is not None else None,
    }
    lines: list[str] = []
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    lines.append(f"id: {event.id}")
    lines.append(f"event: {event.event_type}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _done_frame() -> bytes:
    """Кадр event: done (ADR-012 §6) — сервер закрывает стрим, клиент не переподключается."""
    return f"event: {_DONE_EVENT}\ndata: {{}}\n\n".encode()


def _heartbeat_frame() -> bytes:
    """SSE-комментарий ": ping" (keepalive, не событие; клиент игнорирует)."""
    return b": ping\n\n"


async def _fetch_events_after(session: AsyncSession, job_id: str, after_id: int) -> list[JobEvent]:
    """job_events WHERE job_id=:jid AND id > :after_id ORDER BY id (catch-up + live, ADR-012 §2)."""
    result = await session.execute(
        select(JobEvent)
        .where(JobEvent.job_id == job_id, JobEvent.id > after_id)
        .order_by(JobEvent.id)
    )
    return list(result.scalars().all())


async def _latest_state_changed(session: AsyncSession, job_id: str) -> JobEvent | None:
    """Последнее state_changed-событие джобы (снимок текущего состояния, ADR-012 §2).

    Используется при подключении БЕЗ Last-Event-ID — первый кадр = текущий снимок, чтобы
    клиент сразу знал состояние.
    """
    result = await session.execute(
        select(JobEvent)
        .where(JobEvent.job_id == job_id, JobEvent.to_state.is_not(None))
        .order_by(JobEvent.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _is_terminal_event(event: JobEvent) -> bool:
    return event.to_state in _TERMINAL_STATES


async def event_stream(job_id: str, *, last_event_id: int | None) -> AsyncIterator[bytes]:
    """Async-генератор SSE-кадров статуса джобы (ADR-012). Закрывается на терминале/отмене.

    Порядок (ADR-012 §2): подписка на Redis job:{jid} ДО чтения catch-up из БД — окно между
    catch-up и подпиской не теряет событий (дедуп по last_seen_id). Live-кадры читаются из
    job_events при каждом wake-сигнале pub/sub либо по таймауту heartbeat.
    """
    settings = get_settings()
    heartbeat_s = float(settings.sse_heartbeat_s)
    client = get_redis()  # Sprint 6 (TD-007): переиспользуемый ConnectionPool.
    pubsub = client.pubsub()
    # 1. СНАЧАЛА подписка на Redis-канал (ADR-012 §2) — до чтения catch-up из БД.
    await pubsub.subscribe(_redis_channel(job_id))
    first_frame_sent = False
    # Sprint 6 (ADR-015 §2.3): наблюдаемость открытых стримов + длительность по close_reason.
    metrics.sse_streams_open.inc()
    started = time.monotonic()
    close_reason = "client_disconnect"
    try:
        # 2. Снимок/catch-up из job_events (источник истины replay).
        async with session_scope() as session:
            if last_event_id is None:
                # Без Last-Event-ID: первый кадр = текущий снимок (последнее state_changed).
                snapshot = await _latest_state_changed(session, job_id)
                last_seen_id = 0
                if snapshot is not None:
                    yield _format_frame(snapshot, retry_ms=settings.sse_retry_ms)
                    first_frame_sent = True
                    last_seen_id = snapshot.id
                    if _is_terminal_event(snapshot):
                        # Джоба уже терминальна при подключении → снимок + done + закрытие.
                        yield _done_frame()
                        close_reason = "done"
                        return
            else:
                last_seen_id = last_event_id

            # Catch-up: все события с id > last_seen_id (после снимка/Last-Event-ID).
            backlog = await _fetch_events_after(session, job_id, last_seen_id)
            for event in backlog:
                retry = None if first_frame_sent else settings.sse_retry_ms
                yield _format_frame(event, retry_ms=retry)
                first_frame_sent = True
                last_seen_id = event.id
                if _is_terminal_event(event):
                    yield _done_frame()
                    close_reason = "done"
                    return

        # 3. Live: ждём wake-сигнал pub/sub либо heartbeat-таймаут, затем дочитываем
        #    job_events WHERE id > last_seen_id (дедуп по last_seen_id, id из Postgres).
        while True:
            message = await _wait_for_message(pubsub, heartbeat_s)
            if message is None:
                # Heartbeat-таймаут (TD-011): СТРАХОВКА от потерянного at-most-once pub/sub —
                # дочитываем job_events WHERE id > last_seen_id (комментарий §2 ≡ поведение).
                # Терминальное событие, чей wake-сигнал потерян → отдаём хвост + done без
                # необходимости reconnect. Иначе — keepalive ": ping".
                async with session_scope() as session:
                    tail = await _fetch_events_after(session, job_id, last_seen_id)
                if not tail:
                    metrics.sse_heartbeat_catchup_total.labels(result="noop").inc()
                    yield _heartbeat_frame()
                    continue
                metrics.sse_heartbeat_catchup_total.labels(result="tail_replayed").inc()
                terminal = False
                for event in tail:
                    yield _format_frame(event)
                    last_seen_id = event.id
                    if _is_terminal_event(event):
                        terminal = True
                if terminal:
                    yield _done_frame()
                    close_reason = "heartbeat_timeout"
                    return
                continue
            async with session_scope() as session:
                new_events = await _fetch_events_after(session, job_id, last_seen_id)
            terminal = False
            for event in new_events:
                yield _format_frame(event)
                last_seen_id = event.id
                if _is_terminal_event(event):
                    terminal = True
            if terminal:
                yield _done_frame()
                close_reason = "done"
                return
    finally:
        metrics.sse_streams_open.dec()
        metrics.sse_stream_duration_seconds.labels(close_reason=close_reason).observe(
            time.monotonic() - started
        )
        with suppress(aioredis.RedisError, OSError):
            await pubsub.unsubscribe(_redis_channel(job_id))
            await pubsub.aclose()  # type: ignore[attr-defined]  # redis 5.x async; stub устарел


async def _wait_for_message(pubsub: aioredis.client.PubSub, timeout_s: float) -> dict | None:
    """Ждёт pub/sub-сообщение до timeout_s. None при таймауте (→ heartbeat).

    get_message(timeout=) возвращает None по таймауту; служебные subscribe-кадры
    (type != 'message') игнорируются (трактуются как таймаут → heartbeat-цикл повторит).
    """
    try:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout_s)
    except (aioredis.RedisError, OSError) as exc:
        logger.warning("sse_pubsub_error", extra={"error": str(exc)})
        # Транзиентная ошибка чтения — трактуем как heartbeat-окно (стрим продолжается).
        await asyncio.sleep(0)
        return None
    if message is None or message.get("type") != "message":
        return None
    return message
