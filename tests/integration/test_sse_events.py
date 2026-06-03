"""Integration: SSE GET /jobs/{jid}/events (Sprint 5, ADR-012, docs/06 §S5 SSE).

Реальный Postgres (session_scope autonomous_db) + реальный Redis (pub/sub). Покрывает:
  - без Last-Event-ID → первый кадр = снимок (последнее state_changed) + retry-hint;
  - уже-терминальная джоба → снимок + event: done + закрытие;
  - с Last-Event-ID → catch-up из job_events id>N (дедуп: id<=N не приходят);
  - heartbeat (": ping") в idle-окне без событий;
  - live: pub/sub-wake → дочитывание новых job_events; терминал → done + закрытие;
  - cross-tenant: чужая джоба → 404 (router _load_owned_job, через HTTP client);
  - лимит SSE_MAX_STREAMS_PER_KEY → 429; слот освобождается при закрытии стрима.

event_stream драйвится напрямую как async-генератор (детерминированное завершение без
зависших ASGI-стримов). Cross-tenant/429 — через HTTP client (router-уровень).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select

from app.api import sse
from app.core.config import get_settings
from app.core.ids import new_job_id, new_project_id
from app.core.security import hash_api_key
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent, Project, User
from app.db.session import session_scope
from app.pipeline.events import record_event

pytestmark = pytest.mark.asyncio

UID = "u_sseowner00000000001"


async def _purge() -> None:
    async with session_scope() as s:
        job_ids = (
            (await s.execute(select(GenerationJob.id).where(GenerationJob.user_id == UID)))
            .scalars()
            .all()
        )
        if job_ids:
            await s.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
        await s.execute(delete(GenerationJob).where(GenerationJob.user_id == UID))
        await s.execute(delete(Project).where(Project.user_id == UID))
        await s.execute(delete(User).where(User.id == UID))
        await s.commit()


async def _make_job(state: JobState) -> str:
    pid = new_project_id()
    jid = new_job_id()
    async with session_scope() as s:
        existing = await s.get(User, UID)
        if existing is None:
            s.add(
                User(
                    id=UID,
                    api_key_hash=hash_api_key("sse-key"),
                    monthly_budget_usd=Decimal("50.0000"),
                    status="active",
                )
            )
        s.add(Project(id=pid, user_id=UID, prompt="x", title=None))
        s.add(GenerationJob(id=jid, project_id=pid, user_id=UID, state=state, kind="generation"))
        await s.commit()
    return jid


async def _add_event(jid: str, event_type: str, *, to_state: str | None = None) -> int:
    async with session_scope() as s:
        ev = await record_event(s, jid, event_type, to_state=to_state)
        await s.commit()
        return ev.id


async def _flush_sse_redis() -> None:
    client = aioredis.from_url(get_settings().redis_url)
    try:
        async for key in client.scan_iter("sse:streams:*"):
            await client.delete(key)
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def sse_env(autonomous_db):  # noqa: ANN001, ANN201
    await _purge()
    await _flush_sse_redis()
    yield
    await _purge()
    await _flush_sse_redis()


def _parse_frames(raw: bytes) -> list[str]:
    """Разбивает SSE-поток на кадры (разделитель \\n\\n)."""
    text = raw.decode("utf-8")
    return [f for f in text.split("\n\n") if f.strip()]


async def _collect_until_done(jid: str, *, last_event_id=None, max_frames=20):  # noqa: ANN001
    """Собирает кадры event_stream до event: done или max_frames (страховка от зависания)."""
    frames: list[bytes] = []
    gen = sse.event_stream(jid, last_event_id=last_event_id)
    try:
        async for frame in gen:
            frames.append(frame)
            if b"event: done" in frame or len(frames) >= max_frames:
                break
    finally:
        await gen.aclose()
    return frames


# --- снимок без Last-Event-ID ---


async def test_snapshot_first_frame_with_retry_hint(sse_env):
    jid = await _make_job(JobState.BUILDING)
    await _add_event(jid, "state_changed", to_state="BUILDING")

    frames = await asyncio.wait_for(_collect_until_done(jid, max_frames=1), timeout=10)
    first = frames[0].decode()
    # Первый кадр — снимок последнего state_changed + retry-hint.
    assert f"retry: {get_settings().sse_retry_ms}" in first
    assert "event: state_changed" in first
    assert '"to_state": "BUILDING"' in first
    assert "id: " in first


async def test_already_terminal_job_snapshot_then_done(sse_env):
    jid = await _make_job(JobState.LIVE)
    await _add_event(jid, "state_changed", to_state="LIVE")

    frames = await asyncio.wait_for(_collect_until_done(jid), timeout=10)
    joined = b"".join(frames).decode()
    assert "event: state_changed" in joined
    assert "event: done" in joined
    # Последний кадр — done (стрим закрылся на терминале).
    assert b"event: done" in frames[-1]


# --- catch-up по Last-Event-ID (дедуп) ---


async def test_catch_up_from_last_event_id_dedup(sse_env):
    jid = await _make_job(JobState.LIVE)
    id1 = await _add_event(jid, "state_changed", to_state="BUILDING")
    id2 = await _add_event(jid, "state_changed", to_state="DEPLOYING")
    id3 = await _add_event(jid, "state_changed", to_state="LIVE")

    # Подключение с Last-Event-ID = id1 → приходят id2, id3 (id1 НЕ дублируется), затем done.
    frames = await asyncio.wait_for(_collect_until_done(jid, last_event_id=id1), timeout=10)
    joined = b"".join(frames).decode()
    assert f"id: {id1}" not in joined  # дедуп: уже виденный не приходит
    assert f"id: {id2}" in joined
    assert f"id: {id3}" in joined
    assert "event: done" in joined  # терминал LIVE


# --- heartbeat в idle ---


async def test_heartbeat_emitted_in_idle(sse_env):
    """Non-terminal джоба без новых событий → после catch-up приходит ": ping" (heartbeat)."""
    jid = await _make_job(JobState.BUILDING)
    await _add_event(jid, "state_changed", to_state="BUILDING")

    frames: list[bytes] = []
    gen = sse.event_stream(jid, last_event_id=None)

    async def _drain() -> None:
        async for frame in gen:
            frames.append(frame)
            if b": ping" in frame:
                break

    try:
        # SSE_HEARTBEAT_S=1 (conftest) → heartbeat в пределах таймаута.
        await asyncio.wait_for(_drain(), timeout=10)
    finally:
        await gen.aclose()
    assert any(b": ping" in f for f in frames)


# --- live: pub/sub wake → дочитывание новых событий → терминал ---


async def test_live_event_then_terminal_done(sse_env):
    jid = await _make_job(JobState.BUILDING)
    await _add_event(jid, "state_changed", to_state="BUILDING")

    frames: list[bytes] = []
    gen = sse.event_stream(jid, last_event_id=None)

    async def _drive() -> None:
        got_snapshot = False
        async for frame in gen:
            frames.append(frame)
            if not got_snapshot and b"state_changed" in frame:
                got_snapshot = True
                # После снимка добавляем терминальное событие + wake через pub/sub.
                await _add_event(jid, "state_changed", to_state="LIVE")
                client = aioredis.from_url(get_settings().redis_url)
                try:
                    await client.publish(f"job:{jid}", "{}")
                finally:
                    await client.aclose()
            if b"event: done" in frame:
                break

    try:
        await asyncio.wait_for(_drive(), timeout=15)
    finally:
        await gen.aclose()
    joined = b"".join(frames).decode()
    assert '"to_state": "LIVE"' in joined
    assert "event: done" in joined


# --- cross-tenant 404 (через HTTP router) ---


async def test_cross_tenant_events_404(client, session, seeded_user, other_user):
    # Джоба принадлежит other_user; Bearer-ключ — у seeded_user (другой тенант) → 404.
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=other_user.id, prompt="x", title=None))
    session.add(
        GenerationJob(id=jid, project_id=pid, user_id=other_user.id, state=JobState.BUILDING)
    )
    await session.flush()
    resp = await client.get(
        f"/v1/jobs/{jid}/events", headers={"Authorization": "Bearer qa-test-bearer-key"}
    )
    assert resp.status_code == 404


# --- лимит стримов на ключ → 429; слот освобождается при закрытии ---


async def test_stream_slot_limit_then_release():
    """acquire/release слотов: при count > SSE_MAX_STREAMS_PER_KEY → 429; release освобождает."""
    await _flush_sse_redis()
    settings = get_settings()
    key = "qa-sse-limit-key"
    limit = settings.sse_max_streams_per_key

    slots = []
    for _ in range(limit):
        slot = await sse.acquire_stream_slot(key)
        assert slot.acquired is True
        slots.append(slot)
    # Превышение лимита → 429 (acquired=False), счётчик не «протекает» вверх.
    over = await sse.acquire_stream_slot(key)
    assert over.acquired is False

    # Освобождаем один слот → снова можно взять.
    await sse.release_stream_slot(key)
    again = await sse.acquire_stream_slot(key)
    assert again.acquired is True

    # cleanup
    for _ in range(limit):
        await sse.release_stream_slot(key)
    await _flush_sse_redis()


async def test_events_endpoint_429_when_limit_exhausted(client, session, seeded_user, monkeypatch):
    """HTTP-уровень: при исчерпанном лимите слотов GET /events → 429.

    Джоба создаётся под seeded_user (владелец Bearer-ключа), чтобы пройти владение и
    дойти до acquire_stream_slot (которое мокаем на отказ).
    """
    pid = new_project_id()
    jid = new_job_id()
    session.add(Project(id=pid, user_id=seeded_user.id, prompt="x", title=None))
    session.add(
        GenerationJob(id=jid, project_id=pid, user_id=seeded_user.id, state=JobState.BUILDING)
    )
    await session.flush()

    async def _no_slot(key):  # noqa: ANN001, ANN202
        return sse.StreamSlot(acquired=False, count=999)

    monkeypatch.setattr(sse, "acquire_stream_slot", _no_slot)
    resp = await client.get(
        f"/v1/jobs/{jid}/events", headers={"Authorization": "Bearer qa-test-bearer-key"}
    )
    assert resp.status_code == 429
